#!/usr/bin/env python3
"""Build Zhi's Nova kernel, including nvidia-vgpu-vfio-pci, and package a deployable bundle."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


def select_python():
    """Bootstrap on the system Python before loading the build implementation."""
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument('--python')
    options, _ = selector.parse_known_args()
    executable = options.python or os.environ.get('NOVA_PYTHON')
    # A wrapper/pyenv shim may resolve to a different sys.executable. Restart
    # once instead of comparing paths, then remove the marker from build jobs.
    restarted = os.environ.pop('_NOVA_BUILD_PYTHON_REEXEC', None)
    if executable and not restarted:
        env = os.environ.copy()
        env['_NOVA_BUILD_PYTHON_REEXEC'] = '1'
        try:
            os.execvpe(executable, [executable, os.path.abspath(__file__)] + sys.argv[1:], env)
        except OSError as exc:
            sys.exit('Cannot start configured Python ' + repr(executable) + ': ' + str(exc))
    if sys.version_info < (3, 9):
        sys.exit('build-nova.py requires Python 3.9 or newer; running ' +
                 '.'.join(map(str, sys.version_info[:3])) +
                 '. Source nova-build.env or select --python python3.13.')


if __name__ == '__main__':
    select_python()

from nova_common import elf_build_id, run, sha256, verify_bundle, write_json

REPO = 'https://github.com/zhiwang-nvidia/nova-core.git'
VF = 'drivers/gpu/nvidia-vgpu-vfio-pci.ko'
DEPS = {'nova_core': 'drivers/gpu/nova-core.ko',
        'vfio_pci_core': 'drivers/vfio/pci/vfio-pci-core.ko',
        'vfio': 'drivers/vfio/vfio.ko'}


def parse_args(argv=None):
    """Resolve command-line overrides and exported NOVA_* settings without building."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, help='Existing checkout, or new clone destination (NOVA_SOURCE)')
    p.add_argument('--build-dir', type=Path, help='Persistent Kbuild O= directory, separate from source (NOVA_BUILD_DIR)')
    p.add_argument('--nova-tag', help='Build label; defaults to NOVA_TAG, then the UTC timestamp YYYYMMDD-HHMMSS')
    p.add_argument('--output', type=Path, help='Bundle path (NOVA_OUTPUT); default: <build-dir>/artifacts/nova[-vf]-<tag>.tar.gz')
    p.add_argument('--kind', choices=('kernel', 'module'), help='Bundle kind (NOVA_KIND; default: kernel)')
    p.add_argument('--config', type=Path, help='Seed for a new kernel build (NOVA_CONFIG); existing .config is reused')
    p.add_argument('--clone-ref', help='Clone ref for a new checkout (NOVA_CLONE_REF); existing checkouts are preserved')
    p.add_argument('--localversion', help='Full-kernel release suffix (NOVA_LOCALVERSION); default: unique commit/time suffix')
    p.add_argument('--toolchain', type=Path, help='Directory with bin/ and lib/ (NOVA_TOOLCHAIN); otherwise use PATH')
    p.add_argument('--python', help='Script and Kbuild interpreter (NOVA_PYTHON; default: current interpreter)')
    p.add_argument('--jobs', type=int, help='Parallel build tasks (NOVA_JOBS; default: CPU count capped at 24)')
    p.add_argument('--allow-dirty', action=argparse.BooleanOptionalAction, default=None,
                   help='Allow tracked edits and record their diff (NOVA_ALLOW_DIRTY=0|1; default: 0)')
    args = p.parse_args(argv)
    # Only read an environment value when the CLI did not specify that option.
    for field, variable, convert in (
        ('source', 'NOVA_SOURCE', Path), ('build_dir', 'NOVA_BUILD_DIR', Path),
        ('nova_tag', 'NOVA_TAG', str), ('output', 'NOVA_OUTPUT', Path),
        ('kind', 'NOVA_KIND', str), ('toolchain', 'NOVA_TOOLCHAIN', Path),
        ('python', 'NOVA_PYTHON', str), ('jobs', 'NOVA_JOBS', int),
    ):
        value = os.environ.get(variable)
        if getattr(args, field) is None and value:
            try:
                setattr(args, field, convert(value))
            except ValueError:
                p.error('Invalid ' + variable + ': ' + repr(value))
    args.kind = args.kind or 'kernel'
    args.python = args.python or sys.executable
    args.jobs = args.jobs if args.jobs is not None else min(os.cpu_count() or 1, 24)
    if args.kind not in ('kernel', 'module'):
        p.error('NOVA_KIND must be kernel or module')
    if args.jobs < 1:
        p.error('--jobs/NOVA_JOBS must be positive')
    if args.allow_dirty is None:
        value = os.environ.get('NOVA_ALLOW_DIRTY') or '0'
        if value not in ('0', '1'):
            p.error('NOVA_ALLOW_DIRTY must be 0 or 1')
        args.allow_dirty = value == '1'
    for field, variable in (('source', 'NOVA_SOURCE'), ('build_dir', 'NOVA_BUILD_DIR')):
        if getattr(args, field) is None:
            p.error('Set --' + field.replace('_', '-') + ' or ' + variable + '; source nova-build.env for this workspace')
    # A sourced file remains valid for reruns and for module-only iteration.
    if args.kind == 'kernel':
        if args.config is None and not (args.build_dir / '.config').exists():
            value = os.environ.get('NOVA_CONFIG')
            if value:
                args.config = Path(value)
        if args.localversion is None:
            args.localversion = os.environ.get('NOVA_LOCALVERSION') or None
    if args.clone_ref is None and not args.source.exists():
        args.clone_ref = os.environ.get('NOVA_CLONE_REF') or None
    return p, args


def main():
    p, args = parse_args()
    tag = args.nova_tag
    if tag is None:
        tag = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}', tag):
        p.error('--nova-tag/NOVA_TAG must be 1-64 ASCII letters, digits, dots, +, - or _, starting with a letter or digit')
    src, out = args.source.resolve(), args.build_dir.resolve()
    prefix = 'nova-vf-' if args.kind == 'module' else 'nova-'
    output = args.output.resolve() if args.output else out / 'artifacts' / (prefix + tag + '.tar.gz')
    if out == src or args.jobs < 1:
        p.error('Choose a separate build directory and positive --jobs')
    if any(path.exists() for path in (output, output.with_name(output.name + '.json'),
                                      output.with_name(output.name + '.build.log'))):
        p.error('Output bundle or sidecar already exists; choose a new --nova-tag or --output')
    print('Build tag:', tag, '\nBuild directory:', out, '\nOutput bundle:', output, flush=True)
    if not src.exists():
        if not args.clone_ref:
            p.error('Source does not exist; supply --clone-ref zhi/nova-vgpu-wip to clone')
        run(['git', 'clone', REPO, src])
        run(['git', '-C', src, 'checkout', '--detach', args.clone_ref])
    elif args.clone_ref:
        p.error('--clone-ref only applies to a new checkout; switch existing branches yourself')
    commit = run(['git', '-C', src, 'rev-parse', 'HEAD'], capture=True).stdout.strip()
    status = run(['git', '-C', src, 'status', '--porcelain'], capture=True).stdout
    # Untracked source can affect a build without being represented by git diff.
    if any(line.startswith('??') for line in status.splitlines()):
        p.error('Untracked files in source: add intended source files to Git or move build artifacts outside it')
    if status and not args.allow_dirty:
        p.error('Source has edits; use --allow-dirty to record and build them')
    diff = subprocess.check_output(['git', '-C', str(src), 'diff', 'HEAD', '--binary'])
    env = os.environ.copy()
    if args.toolchain:
        tc = args.toolchain.resolve()
        env['PATH'] = str(tc / 'bin') + os.pathsep + env['PATH']
        env['LIBCLANG_PATH'] = str(tc / 'lib/libclang.so')
    out.mkdir(parents=True, exist_ok=True)
    make = ['make', '-C', src, 'O=' + str(out), 'LLVM=1', 'PYTHON3=' + args.python]
    if args.kind == 'module':
        if args.config or args.localversion:
            p.error('Module mode reuses the exact existing build config; do not supply --config/--localversion')
        for name in ('.config', 'Module.symvers', 'vmlinux', 'arch/x86/boot/bzImage'):
            if not (out / name).is_file():
                p.error('Module mode needs a complete baseline build: missing ' + name)
        baseline_id = elf_build_id(out / 'vmlinux')
    else:
        if args.config:
            if (out / '.config').exists():
                p.error('Refusing to overwrite an existing .config; omit --config or use a new build directory')
            shutil.copyfile(args.config, out / '.config')
        if not (out / '.config').is_file():
            p.error('Supply a host-appropriate --config; this script does not guess storage or NIC drivers')
        suffix = args.localversion or ('-nova-' + commit[:12] + '-' + datetime.now(timezone.utc).strftime('%m%d%H%M%S'))
        if not suffix.startswith('-') or not all(c.isalnum() or c in '-._+' for c in suffix):
            p.error('Use a safe --localversion suffix beginning with -')
        run([src / 'scripts/config', '--file', out / '.config', '--set-str', 'LOCALVERSION', suffix,
             '--disable', 'LOCALVERSION_AUTO', '--enable', 'RUST', '--enable', 'FWCTL',
             '--enable', 'PCI_IOV', '--module', 'NOVA_CORE', '--module', 'DRM_NOVA',
             '--module', 'NVIDIA_VGPU_VFIO_PCI', '--module', 'VFIO_PCI'], env=env)
        run(make + ['olddefconfig'], env=env)
    if not (src / 'drivers/vfio/pci/nvidia-vgpu/nvidia_vgpu.rs').is_file():
        p.error('Expected the newer Zhi Rust nvidia-vgpu-vfio-pci source tree')
    config = (out / '.config').read_text().splitlines()
    for required in ('CONFIG_RUST=y', 'CONFIG_FWCTL=y', 'CONFIG_NOVA_CORE=m',
                     'CONFIG_NVIDIA_VGPU_VFIO_PCI=m', 'CONFIG_VFIO_PCI=m', 'CONFIG_PCI_IOV=y'):
        if required not in config:
            p.error('Required config missing: ' + required)
    run(make + ['rustavailable'], env=env)
    output.parent.mkdir(parents=True, exist_ok=True)
    log = output.with_name(output.name + '.build.log')
    with log.open('x') as stream:
        result = subprocess.run(list(map(str, make + ['-j' + str(args.jobs), 'bzImage', 'modules'])),
                                env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError('Build failed; see ' + str(log))
    release = run(make + ['-s', 'kernelrelease'], capture=True, env=env).stdout.strip().splitlines()[-1]
    if not release.startswith(('6.', '7.')) or '/' in release:
        raise ValueError('Unexpected kernelrelease: ' + release)
    kernel_id = elf_build_id(out / 'vmlinux')
    if args.kind == 'module' and kernel_id != baseline_id:
        raise ValueError('Built-in kernel changed. Do not live-replace the VF; build --kind kernel instead.')
    if commit != run(['git', '-C', src, 'rev-parse', 'HEAD'], capture=True).stdout.strip() or diff != subprocess.check_output(['git', '-C', str(src), 'diff', 'HEAD', '--binary']):
        raise ValueError('Source changed during build; rerun from a stable checkout')
    manifest = {'format': 1, 'kind': args.kind, 'nova_tag': tag, 'release': release, 'kernel_build_id': kernel_id,
                'source_repository': REPO, 'source_path': str(src), 'source_commit': commit,
                'source_dirty': bool(status), 'dependencies': {}, 'files': {},
                'toolchain': {name: run([name, '--version'], capture=True, env=env).stdout.splitlines()[0]
                              for name in ('clang', 'rustc', 'bindgen')}}
    with tempfile.TemporaryDirectory(prefix='nova-package-', dir=output.parent) as temporary:
        stage = Path(temporary)
        (stage / 'provenance').mkdir()
        shutil.copy2(out / '.config', stage / 'provenance/kernel.config')
        shutil.copy2(out / 'Module.symvers', stage / 'provenance/Module.symvers')
        (stage / 'provenance/source.patch').write_bytes(diff)
        manifest['vermagic'] = run(['modinfo', '-F', 'vermagic', out / VF], capture=True).stdout.strip()
        if args.kind == 'kernel':
            run(make + ['INSTALL_MOD_PATH=' + str(stage), 'INSTALL_MOD_STRIP=1', 'modules_install'], env=env)
            for path in stage.rglob('*'):
                if path.is_symlink():
                    path.unlink()
            (stage / 'boot').mkdir()
            for original, installed in [('arch/x86/boot/bzImage', 'vmlinuz'), ('System.map', 'System.map'), ('.config', 'config')]:
                shutil.copy2(out / original, stage / 'boot' / (installed + '-' + release))
        else:
            (stage / 'modules').mkdir()
            shutil.copy2(out / VF, stage / 'modules/nvidia-vgpu-vfio-pci.ko')
            for module, path in DEPS.items():
                manifest['dependencies'][module] = elf_build_id(out / path)
        for path in stage.rglob('*'):
            if path.is_file():
                manifest['files'][str(path.relative_to(stage))] = sha256(path)
        write_json(stage / 'manifest.json', manifest)
        verify_bundle(stage)
        with tarfile.open(output, 'x:gz') as archive:
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
    write_json(output.with_name(output.name + '.json'), manifest)
    print('Created:', output, 'SHA256:', sha256(output), '\nBuild log:', log)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
