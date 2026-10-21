#!/usr/bin/env python3.13
"""Check Nova Rust formatting, build with Clippy, and reuse a permanent doctest VM.

Defaults use the sibling nova-core checkout, this directory's pinned toolchain
and configs/nova-kvm.config. Select the KVM target in the ignored rust-checks.json,
with NOVA_RUST_SSH_TARGET, or with --ssh-target.
Build output is reusable; each invocation saves separate logs and result.json.
VM definitions, kernels and results persist under vms/<name> and the KVM host's
/var/lib/libvirt/images/<name>. The same defined VM boots each new test kernel
and powers off after the suite; it is never undefined or deleted after a run.
Requires SSH key access, a known host key and passwordless sudo for libvirt.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import uuid


class CheckFailure(Exception):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def source_state(source):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args])
    untracked = {}
    for raw in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
        if raw:
            name = os.fsdecode(raw)
            path = source / name
            if path.is_file():
                untracked[name] = sha256(path)
            elif path.is_symlink():
                untracked[name] = 'symlink:' + os.readlink(path)
            else:
                raise CheckFailure('Cannot fingerprint untracked file: ' + name)
    return {
        'commit': git('rev-parse', 'HEAD').decode().strip(),
        'status': git('status', '--porcelain=v1', '--untracked-files=all').decode(),
        'patch': git('diff', '--binary', 'HEAD'),
        'untracked_sha256': untracked,
    }


def check_results(generated, parsed):
    expected = re.findall(r'\bKUNIT_CASE\((rust_doctest_kernel_\w+)\)', generated)
    if not expected or len(expected) != len(set(expected)):
        raise CheckFailure('Generated doctest cases are empty or duplicated')

    def groups(group):
        yield group
        for child in group.get('sub_groups', []):
            yield from groups(child)
    suites = [g for g in groups(parsed) if g.get('name') == 'rust_doctests_kernel']
    if len(suites) != 1:
        raise CheckFailure('Expected exactly one rust_doctests_kernel suite')
    suite = suites[0]
    cases = suite.get('test_cases', [])
    if Counter(case['name'] for case in cases) != Counter(expected):
        raise CheckFailure('Executed doctests do not match the current generated cases')
    if any(case.get('status') != 'PASS' for case in cases):
        raise CheckFailure('A doctest failed, crashed, or was skipped')
    counts = suite.get('misc', {})
    if counts.get('tests') != len(expected) or counts.get('passed') != len(expected):
        raise CheckFailure('Doctest result counts are inconsistent')
    if any(counts.get(key) != 0 for key in ('failed', 'crashed', 'errors', 'skipped')):
        raise CheckFailure('Doctest result contains failures, crashes, errors, or skips')
    pci = sum(name.startswith('rust_doctest_kernel_pci') for name in expected)
    if not pci:
        raise CheckFailure('No PCI examples were generated; check the kernel configuration')
    return {'passed': len(expected), 'failed': 0, 'skipped': 0, 'pci_passed': pci}


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return result


def parse_args(argv=None):
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    settings_path = root / 'rust-checks.json'
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            if not isinstance(settings, dict) or set(settings) - {'ssh_target'}:
                raise ValueError('expected an object containing only ssh_target')
            if 'ssh_target' in settings and not isinstance(settings['ssh_target'], str):
                raise ValueError('ssh_target must be a string')
        except (OSError, ValueError) as error:
            parser.error('Invalid ' + str(settings_path) + ': ' + str(error))
    parser.add_argument('--source', type=Path, default=root.parent / 'nova-core')
    parser.add_argument('--build-dir', type=Path, default=root / 'build-rust-checks',
                        help='Dedicated persistent output directory (default: %(default)s)')
    parser.add_argument('--config', type=Path, default=root / 'configs/nova-kvm.config',
                        help='Seed config for a new build; existing .config is reused')
    parser.add_argument('--toolchain', type=Path,
                        default=root / 'toolchains/llvm-22.1.8-rust-1.97.0-x86_64')
    parser.add_argument('--ssh-target',
                        default=os.environ.get('NOVA_RUST_SSH_TARGET') or settings.get('ssh_target'),
                        help='KVM user@host; overrides NOVA_RUST_SSH_TARGET and rust-checks.json')
    parser.add_argument('--vm-name', default='nova-rust-doctest',
                        help='Permanent libvirt VM name (default: %(default)s)')
    parser.add_argument('--vm-dir', type=Path,
                        help='Local VM definition/artifact directory (default: vms/<vm-name>)')
    parser.add_argument('--jobs', type=positive, default=16)
    parser.add_argument('--timeout', type=positive, default=300,
                        help='QEMU execution limit in seconds (default: %(default)s)')
    args = parser.parse_args(argv)
    if not args.ssh_target:
        parser.error('Set --ssh-target, NOVA_RUST_SSH_TARGET, or ssh_target in rust-checks.json')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*',
                        args.ssh_target):
        parser.error('--ssh-target must have the form user@hostname or user@IPv4-address')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', args.vm_name):
        parser.error('--vm-name must contain only letters, digits, dots, underscores or dashes')
    args.vm_dir = (args.vm_dir or root / 'vms' / args.vm_name).resolve()
    args.vm_manager = root / 'doctest-vm.py'
    for field in ('source', 'build_dir', 'config', 'toolchain'):
        setattr(args, field, getattr(args, field).resolve())
    if args.source == args.build_dir or args.source in args.build_dir.parents:
        parser.error('--build-dir must be outside the kernel source tree')
    if args.source == args.vm_dir or args.source in args.vm_dir.parents:
        parser.error('--vm-dir must be outside the kernel source tree')
    return args


def main():
    args = parse_args()
    for tool in ('clang', 'rustc', 'rustdoc', 'rustfmt', 'clippy-driver', 'bindgen'):
        if not (args.toolchain / 'bin' / tool).is_file():
            raise CheckFailure('Missing toolchain executable: ' + tool)
    if not (args.source / 'rust/.kunitconfig').is_file():
        raise CheckFailure('--source must point to the Nova kernel checkout')
    if not args.vm_manager.is_file():
        raise CheckFailure('Missing VM manager: ' + str(args.vm_manager))
    args.build_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.build_dir / '.rust-checks.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise CheckFailure('Another Rust check is using this build directory')

    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:6]
    logs = args.build_dir / 'rust-checks-runs' / run_id
    logs.mkdir(parents=True)
    result = {'result': 'FAIL', 'source': str(args.source), 'build_dir': str(args.build_dir),
              'ssh_target': args.ssh_target, 'logs': str(logs), 'commands': []}
    print('Logs: ' + str(logs), flush=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('KBUILD_') or key in {
            'MAKEFLAGS', 'GNUMAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES', 'MAKEFILES', 'MAKELEVEL',
            'ARCH', 'SUBARCH', 'CROSS_COMPILE', 'O', 'M', 'MO', 'KCONFIG_CONFIG',
            'KCONFIG_ALLCONFIG', 'KCONFIG_OVERWRITECONFIG', 'KCONFIG_NOSILENTUPDATE',
        }:
            env.pop(key)
    env['PATH'] = str(args.toolchain / 'bin') + os.pathsep + env.get('PATH', '')
    env['LIBCLANG_PATH'] = str(args.toolchain / 'lib/libclang.so')
    make = ['make', 'ARCH=x86_64', 'LLVM=1', 'PYTHON3=' + sys.executable]
    build_make = make + ['O=' + str(args.build_dir),
                         'KCONFIG_CONFIG=' + str(args.build_dir / '.config')]
    ssh_options = ['-oBatchMode=yes', '-oStrictHostKeyChecking=yes', '-oConnectTimeout=10',
                   '-oServerAliveInterval=15', '-oServerAliveCountMax=3']
    ssh = ['ssh', '-T', *ssh_options, args.ssh_target]

    def run(label, command, filename, must_pass=True):
        print(label + ' ...', flush=True)
        result['commands'].append({'step': label, 'argv': command, 'log': filename})
        with (logs / filename).open('w') as output:
            completed = subprocess.run(command, cwd=args.source, env=env,
                                       stdout=output, stderr=subprocess.STDOUT)
        result['commands'][-1]['exit_code'] = completed.returncode
        if completed.returncode:
            print('FAILED: ' + str(logs / filename), flush=True)
            if must_pass:
                raise CheckFailure(label + ' failed; see ' + str(logs / filename))
        return completed.returncode

    initial = source_state(args.source)
    result['source_commit'] = initial['commit']
    result['source_patch_sha256'] = hashlib.sha256(initial['patch']).hexdigest()
    result['untracked_files_sha256'] = initial['untracked_sha256']
    (logs / 'source.patch').write_bytes(initial['patch'])
    (logs / 'source-status.txt').write_text(initial['status'])
    try:
        run('Toolchain versions', [str(args.toolchain / 'bin/rustc'), '--version'], 'rustc.log')
        run('Clippy version', [str(args.toolchain / 'bin/clippy-driver'), '--version'], 'clippy.log')
        format_status = run('1/3 Rust formatting check', make + ['rustfmtcheck'],
                            'rustfmtcheck.log', must_pass=False)
        result['formatting'] = 'PASS' if format_status == 0 else 'FAIL'

        config = args.build_dir / '.config'
        if not config.exists():
            shutil.copyfile(args.config, config)
        shutil.copyfile(config, logs / 'config-before')
        run('Enable KUnit and doctests',
            ['scripts/config', '--file', str(config), '--enable', 'RUST',
             '--enable', 'KUNIT', '--enable', 'RUST_KERNEL_DOCTESTS'], 'enable-doctests.log')
        run('Resolve configuration', build_make + ['olddefconfig'], 'olddefconfig.log')
        text = config.read_text()
        for symbol in ('RUST', 'KUNIT', 'RUST_KERNEL_DOCTESTS', 'PCI', 'PCI_IOV',
                       'SERIAL_8250', 'SERIAL_8250_CONSOLE'):
            if not re.search(r'^CONFIG_' + symbol + '=y$', text, re.MULTILINE):
                raise CheckFailure('Required CONFIG_' + symbol + '=y is missing')
        for symbol in ('NOVA_CORE', 'VFIO', 'VFIO_PCI_CORE', 'NVIDIA_VGPU_VFIO_PCI'):
            if not re.search(r'^CONFIG_' + symbol + '=[ym]$', text, re.MULTILINE):
                raise CheckFailure('Required CONFIG_' + symbol + ' is disabled')
        shutil.copyfile(config, logs / 'config')
        result['config_sha256'] = sha256(config)
        run('2/3 Full kernel/modules build with Clippy and doctests',
            build_make + ['W=1', 'CLIPPY=1', 'KRUSTFLAGS=-Dclippy::missing_safety_doc',
                          '-j' + str(args.jobs), 'bzImage', 'modules'], 'build.log')
        result['build'] = 'PASS'
        if source_state(args.source) != initial:
            raise CheckFailure('Source changed during the build; rerun on a stable checkout')

        image = args.build_dir / 'arch/x86/boot/bzImage'
        image_sha = sha256(image)
        result['kernel_sha256'] = image_sha
        generated = (args.build_dir / 'rust/doctests_kernel_generated_kunit.c').read_text()
        args.vm_dir.mkdir(parents=True, exist_ok=True)
        settings_file = args.vm_dir / 'settings.json'
        if settings_file.exists():
            previous = json.loads(settings_file.read_text())
            if previous.get('ssh_target') != args.ssh_target or previous.get('name') != args.vm_name:
                raise CheckFailure('VM directory belongs to another target; select another --vm-dir')
        local_kernel_dir = args.vm_dir / 'kernels' / image_sha
        local_kernel_dir.mkdir(parents=True, exist_ok=True)
        local_kernel = local_kernel_dir / 'bzImage'
        if not local_kernel.exists():
            shutil.copyfile(image, local_kernel)
        if sha256(local_kernel) != image_sha:
            raise CheckFailure('Persistent kernel artifact has a checksum mismatch')
        run('Locate permanent VM upload area', ssh + ['printf \'%s\\n\' "$HOME"'],
            'remote-home.log')
        home = (logs / 'remote-home.log').read_text().strip()
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', home) or '..' in PurePosixPath(home).parts:
            raise CheckFailure('Unexpected remote home directory: ' + repr(home))
        remote = home + '/deployment/nova-rust-checks/' + args.vm_name
        remote_kernel_dir = remote + '/kernels/' + image_sha
        state_dir = '/var/lib/libvirt/images/' + args.vm_name
        settings = {'name': args.vm_name, 'ssh_target': args.ssh_target,
                    'libvirt_uri': 'qemu:///system', 'host_state_dir': state_dir,
                    'upload_dir': remote, 'local_dir': str(args.vm_dir),
                    'vcpus': 2, 'memory_mib': 2048, 'persistent': True}
        settings_file.write_text(json.dumps(settings, indent=2) + '\n')
        result['vm'] = settings.copy()
        vm_runs = args.vm_dir / 'runs'
        vm_runs.mkdir(exist_ok=True)
        (vm_runs / run_id).symlink_to(logs, target_is_directory=True)
        run('Prepare permanent VM artifact directories',
            ssh + [shlex.join(['mkdir', '-p', '--', remote_kernel_dir])], 'remote-directories.log')
        run('Copy test kernel',
            ['scp', *ssh_options, str(local_kernel), args.ssh_target + ':' + remote_kernel_dir + '/'],
            'copy-kernel.log')
        run('Copy persistent VM manager',
            ['scp', *ssh_options, str(args.vm_manager), args.ssh_target + ':' + remote + '/'],
            'copy-manager.log')
        operation_status = run('3/3 Run doctests in the permanent libvirt VM',
            ssh + [shlex.join(['sudo', '-n', 'python3', remote + '/doctest-vm.py', 'run',
                              '--name', args.vm_name, '--state-dir', state_dir,
                              '--kernel', remote_kernel_dir + '/bzImage', '--sha256', image_sha,
                              '--run-id', run_id, '--timeout', str(args.timeout)])],
            'vm-operation.log', must_pass=False)
        run('Fetch VM console',
            ssh + [shlex.join(['sudo', '-n', 'cat', '--',
                              state_dir + '/runs/' + run_id + '/console.log'])],
            'console.log', must_pass=(operation_status == 0))
        if operation_status:
            raise CheckFailure('Persistent VM run failed; see vm-operation.log and console.log')
        run('Fetch persistent VM state',
            ssh + [shlex.join(['sudo', '-n', 'cat', '--',
                              state_dir + '/runs/' + run_id + '/result.json'])], 'vm-state.json')
        vm_state = json.loads((logs / 'vm-state.json').read_text())
        if vm_state.get('kernel_sha256') != image_sha or vm_state.get('name') != args.vm_name:
            raise CheckFailure('VM runtime identity does not match the requested kernel/VM')
        result['vm'].update(vm_state)
        run('Save permanent VM definition',
            ssh + [shlex.join(['sudo', '-n', 'cat', '--', state_dir + '/domain.xml'])], 'domain.xml')
        shutil.copyfile(logs / 'domain.xml', args.vm_dir / 'domain.xml')
        parsed_path = logs / 'doctests.json'
        run('Parse KUnit results',
            [sys.executable, 'tools/testing/kunit/kunit.py', 'parse',
             '--json=' + str(parsed_path), str(logs / 'console.log')], 'doctests-summary.log')
        console = (logs / 'console.log').read_text()
        if re.search(r'ASSERTION FAILED|\bnot ok\b|BUG:|Oops:|Kernel panic', console):
            raise CheckFailure('Kernel fault or assertion failure in console.log')
        result['doctests'] = check_results(generated, json.loads(parsed_path.read_text()))
        if sha256(image) != image_sha or source_state(args.source) != initial:
            raise CheckFailure('Source or kernel image changed during the checks; rerun')
        result['persistent_vm_retained'] = True
        if format_status:
            raise CheckFailure('Build/doctests passed, but rustfmtcheck failed')
        result['result'] = 'PASS'
        print('PASS: formatting, Clippy build, and ' + str(result['doctests']['passed'])
              + ' doctests (' + str(result['doctests']['pci_passed']) + ' PCI examples)', flush=True)
        return 0
    except (CheckFailure, OSError, subprocess.SubprocessError, ValueError, KeyboardInterrupt) as exc:
        result['error'] = str(exc) or 'Interrupted'
        print('FAIL: ' + result['error'], file=sys.stderr, flush=True)
        return 1
    finally:
        (logs / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print('Result: ' + str(logs / 'result.json'), flush=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (CheckFailure, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
