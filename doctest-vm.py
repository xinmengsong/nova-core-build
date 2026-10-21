#!/usr/bin/env python3
"""Run a kernel in an owned, persistent libvirt doctest domain (Python 3.9+)."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET


MANAGER = 'nova-doctest-vm-v1'
NS = 'urn:nvidia:nova-rust-doctest'
BASE = Path('/var/lib/libvirt/images')
CMDLINE = ('console=ttyS0 kunit.enable=1 kunit.autorun=1 '
           'kunit.filter_glob=rust_doctests_kernel kunit_shutdown=poweroff')
ET.register_namespace('nova', NS)


class Failure(Exception):
    pass


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise Failure('Expected a regular, non-symlink file: ' + str(path))


def atomic_copy(source, destination, gid, expected=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(destination.parent),
                                         prefix='.' + destination.name + '-',
                                         delete=False) as output:
            temporary = Path(output.name)
            with source.open('rb') as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if expected and checksum(temporary) != expected:
            raise Failure('Copied kernel checksum does not match --sha256')
        os.chown(str(temporary), 0, gid)
        os.chmod(str(temporary), 0o640)
        os.replace(str(temporary), str(destination))
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    temporary = path.with_name('.' + path.name + '.new')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    os.chmod(str(temporary), 0o640)
    os.replace(str(temporary), str(path))


def qemu_identity():
    for name in ('qemu', 'libvirt-qemu'):
        try:
            account = pwd.getpwnam(name)
            return account.pw_uid, account.pw_gid
        except KeyError:
            pass
    raise Failure('Cannot find the standard qemu or libvirt-qemu account')


def definition(name, domain_uuid, state):
    root = ET.Element('domain', type='kvm')
    ET.SubElement(root, 'name').text = name
    ET.SubElement(root, 'uuid').text = domain_uuid
    metadata = ET.SubElement(root, 'metadata')
    ET.SubElement(metadata, '{%s}doctest' % NS,
                  manager=MANAGER, state_dir=str(state))
    ET.SubElement(root, 'memory', unit='MiB').text = '2048'
    ET.SubElement(root, 'currentMemory', unit='MiB').text = '2048'
    ET.SubElement(root, 'vcpu', placement='static').text = '2'
    operating_system = ET.SubElement(root, 'os')
    ET.SubElement(operating_system, 'type', arch='x86_64', machine='q35').text = 'hvm'
    ET.SubElement(operating_system, 'kernel').text = str(state / 'bzImage')
    ET.SubElement(operating_system, 'cmdline').text = CMDLINE
    features = ET.SubElement(root, 'features')
    ET.SubElement(features, 'acpi')
    ET.SubElement(features, 'apic')
    ET.SubElement(root, 'cpu', mode='host-passthrough', check='none', migratable='off')
    ET.SubElement(root, 'clock', offset='utc')
    for event in ('on_poweroff', 'on_reboot', 'on_crash'):
        ET.SubElement(root, event).text = 'destroy'
    devices = ET.SubElement(root, 'devices')
    ET.SubElement(devices, 'emulator').text = '/usr/libexec/qemu-kvm'
    ET.SubElement(devices, 'controller', type='usb', model='none')
    serial = ET.SubElement(devices, 'serial', type='file')
    ET.SubElement(serial, 'source', path=str(state / 'console.log'), append='off')
    ET.SubElement(serial, 'target', type='isa-serial', port='0')
    video = ET.SubElement(devices, 'video')
    ET.SubElement(video, 'model', type='none')
    ET.SubElement(devices, 'memballoon', model='none')
    return ET.tostring(root, encoding='unicode') + '\n'


def verify_definition(text, name, domain_uuid, state):
    root = ET.fromstring(text)
    owner = root.find('./metadata/{%s}doctest' % NS)
    if (root.tag != 'domain' or root.get('type') != 'kvm'
            or root.findtext('name') != name or root.findtext('uuid') != domain_uuid
            or owner is None or owner.get('manager') != MANAGER
            or owner.get('state_dir') != str(state)):
        raise Failure('Refusing a domain without matching ownership metadata and UUID')
    expected = {
        'os/kernel': str(state / 'bzImage'), 'os/cmdline': CMDLINE,
        'vcpu': '2', 'devices/emulator': '/usr/libexec/qemu-kvm',
        'on_poweroff': 'destroy', 'on_reboot': 'destroy', 'on_crash': 'destroy',
    }
    if any(root.findtext(key) != value for key, value in expected.items()):
        raise Failure('Owned domain configuration differs from the managed configuration')
    os_type = root.find('os/type')
    cpu = root.find('cpu')
    if (os_type is None or os_type.get('arch') != 'x86_64'
            or 'q35' not in os_type.get('machine', '') or os_type.text != 'hvm'
            or cpu is None or cpu.get('mode') != 'host-passthrough'):
        raise Failure('Unexpected VM architecture, machine, or CPU configuration')
    scales = {'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3,
              'bytes': 1, 'b': 1, 'k': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3}
    for key in ('memory', 'currentMemory'):
        node = root.find(key)
        if node is None or int(node.text) * scales.get(node.get('unit', 'KiB'), 0) != 2 * 1024 ** 3:
            raise Failure('Unexpected VM memory size')
    forbidden = ('disk', 'interface', 'hostdev', 'filesystem', 'redirdev',
                 'smartcard', 'tpm', 'shmem', 'graphics', 'channel', 'parallel')
    if any(root.findall('devices/' + item) for item in forbidden):
        raise Failure('Unexpected disk, network, passthrough, or peripheral in the domain')
    serials = root.findall('devices/serial')
    if len(serials) != 1 or serials[0].get('type') != 'file':
        raise Failure('Unexpected VM serial configuration')
    source = serials[0].find('source')
    if (source is None or source.get('path') != str(state / 'console.log')
            or source.get('append') != 'off'):
        raise Failure('Unexpected VM serial log path or append setting')
    for video in root.findall('devices/video'):
        model = video.find('model')
        if model is None or model.get('type') != 'none':
            raise Failure('Unexpected VM video device')


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    run = subparsers.add_parser('run')
    run.add_argument('--name', default='nova-rust-doctest')
    run.add_argument('--state-dir', type=Path, default=BASE / 'nova-rust-doctest')
    run.add_argument('--kernel', type=Path, required=True)
    run.add_argument('--sha256', required=True)
    run.add_argument('--run-id', required=True)
    run.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', args.name):
        parser.error('Invalid VM name')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', args.run_id):
        parser.error('Invalid run ID')
    if not re.fullmatch(r'[0-9a-f]{64}', args.sha256):
        parser.error('--sha256 must be a lowercase SHA256 digest')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.state_dir != BASE / args.name:
        parser.error('--state-dir must be /var/lib/libvirt/images/<name>')
    args.kernel = args.kernel.absolute()
    return args


def main():
    args = arguments()
    state = args.state_dir
    run_dir = state / 'runs' / args.run_id
    report = {'result': 'FAIL', 'persistent_vm': True, 'name': args.name,
              'uuid': None, 'kernel_sha256': args.sha256, 'state': 'unknown',
              'run_id': args.run_id, 'console_path': str(run_dir / 'console.log'),
              'domain_xml_path': str(state / 'domain.xml'),
              'result_path': str(run_dir / 'result.json'), 'commands': []}
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    lock = None
    ready = False
    start_attempted = False
    domain_uuid = None

    def command(argv, check=True, timeout=30):
        item = {'argv': argv}
        report['commands'].append(item)
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env, timeout=timeout)
        item.update(exit_code=completed.returncode, stdout=completed.stdout,
                    stderr=completed.stderr)
        if check and completed.returncode:
            raise Failure('Command failed: %s\n%s' % (' '.join(argv), completed.stderr.strip()))
        return completed.stdout

    def virsh(*items):
        return command(['virsh', '-c', 'qemu:///system', *items])

    def domain_state():
        current = virsh('domstate', args.name).strip()
        report['state'] = current
        return current

    def stop_owned():
        # Recheck identity immediately before a destructive lifecycle operation.
        verify_definition(virsh('dumpxml', args.name, '--inactive'),
                          args.name, domain_uuid, state)
        if domain_state() != 'shut off':
            virsh('destroy', args.name)
            domain_state()

    def interrupted(signum, frame):
        raise Failure('Interrupted by signal %d' % signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        if os.geteuid() != 0:
            raise Failure('Run this helper with sudo -n python3')
        if not BASE.is_dir() or BASE.is_symlink():
            raise Failure('Expected the standard /var/lib/libvirt/images directory')
        regular(args.kernel)
        if checksum(args.kernel) != args.sha256:
            raise Failure('Uploaded kernel checksum does not match --sha256')
        if state.is_symlink() or (state.exists() and not state.is_dir()):
            raise Failure('Refusing a symlink or non-directory VM state path')
        marker = state / 'manager.json'
        if state.exists() and any(state.iterdir()) and not marker.exists():
            raise Failure('Refusing an existing nonempty, unmanaged VM directory')
        state.mkdir(mode=0o750, exist_ok=True)
        lock_fd = os.open(str(state / '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(lock_fd, 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Another operation is using this VM directory')
        names = virsh('list', '--all', '--name').splitlines()
        exists = args.name in names
        if marker.exists():
            regular(marker)
            if marker.stat().st_uid != 0:
                raise Failure('VM ownership marker is not owned by root')
            owner = json.loads(marker.read_text())
            if (owner.get('manager') != MANAGER or owner.get('name') != args.name
                    or owner.get('state_dir') != str(state)):
                raise Failure('Invalid VM ownership marker')
            domain_uuid = str(uuid.UUID(owner['uuid']))
        else:
            if exists:
                raise Failure('Refusing an existing libvirt domain without a local ownership marker')
            domain_uuid = str(uuid.uuid4())
            write_json(marker, {'manager': MANAGER, 'name': args.name,
                                'state_dir': str(state), 'uuid': domain_uuid})
        report['uuid'] = domain_uuid
        if exists:
            verify_definition(virsh('dumpxml', args.name, '--inactive'),
                              args.name, domain_uuid, state)
            if domain_state() != 'shut off':
                raise Failure('Refusing to change or restart an active VM')
        qemu_uid, qemu_gid = qemu_identity()
        os.chown(str(state), 0, qemu_gid)
        os.chmod(str(state), 0o750)
        for directory in (state / 'kernels', state / 'runs'):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise Failure('Unexpected VM artifact directory: ' + str(directory))
            directory.mkdir(mode=0o750, exist_ok=True)
            os.chown(str(directory), 0, qemu_gid)
            os.chmod(str(directory), 0o750)
        if run_dir.exists() or run_dir.is_symlink():
            raise Failure('Run ID already exists; choose a new --run-id')
        run_dir.mkdir(mode=0o750)
        os.chown(str(run_dir), 0, qemu_gid)
        ready = True
        (run_dir / 'vms-before.txt').write_text(virsh('list', '--all'))
        archived = state / 'kernels' / (args.sha256 + '.bzImage')
        if archived.exists() or archived.is_symlink():
            regular(archived)
            if checksum(archived) != args.sha256:
                raise Failure('Existing immutable kernel has the wrong checksum')
        else:
            atomic_copy(args.kernel, archived, qemu_gid, args.sha256)
        current = state / 'bzImage'
        if current.is_symlink() or (current.exists() and not current.is_file()):
            raise Failure('Unexpected current kernel file')
        atomic_copy(archived, current, qemu_gid, args.sha256)
        report['kernel_archive_path'] = str(archived)
        console = state / 'console.log'
        if console.is_symlink() or (console.exists() and not console.is_file()):
            raise Failure('Unexpected console log file')
        console.write_text('')
        os.chown(str(console), qemu_uid, qemu_gid)
        os.chmod(str(console), 0o660)
        xml_path = state / 'domain.xml'
        if xml_path.is_symlink():
            raise Failure('Refusing a symlink domain XML path')
        if not exists:
            xml_path.write_text(definition(args.name, domain_uuid, state))
            virsh('define', str(xml_path))
        report['definition_created'] = not exists
        text = virsh('dumpxml', args.name, '--inactive')
        verify_definition(text, args.name, domain_uuid, state)
        xml_path.write_text(text)
        (run_dir / 'domain.xml').write_text(text)
        virsh('autostart', args.name, '--disable')
        restorecon = shutil.which('restorecon')
        if restorecon:
            command([restorecon, '-R', str(state)])
        if domain_state() != 'shut off':
            raise Failure('VM became active before start; refusing to modify it')
        start_attempted = True
        virsh('start', args.name)
        deadline = time.monotonic() + args.timeout
        while domain_state() != 'shut off':
            if time.monotonic() >= deadline:
                raise Failure('VM did not shut off within %d seconds' % args.timeout)
            time.sleep(1)
        report['result'] = 'PASS'
    except (Failure, OSError, ValueError, KeyError, ET.ParseError,
            subprocess.SubprocessError, KeyboardInterrupt) as exc:
        report['error'] = str(exc) or 'Interrupted'
        if start_attempted and domain_uuid is not None:
            try:
                stop_owned()
            except Exception as cleanup_error:
                report['stop_error'] = str(cleanup_error)
        print(report['error'], file=sys.stderr)
    finally:
        if ready:
            try:
                console = state / 'console.log'
                if console.exists() and not console.is_symlink():
                    shutil.copyfile(str(console), str(run_dir / 'console.log'))
                else:
                    (run_dir / 'console.log').write_text('')
                (run_dir / 'vms-after.txt').write_text(virsh('list', '--all'))
            except Exception as capture_error:
                report['capture_error'] = str(capture_error)
                report['result'] = 'FAIL'
            try:
                write_json(run_dir / 'result.json', report)
            except Exception as receipt_error:
                report['result'] = 'FAIL'
                report['receipt_error'] = str(receipt_error)
        print(json.dumps(report, sort_keys=True), flush=True)
        if lock is not None:
            lock.close()
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
