"""Validate Rust-check receipts and VM/config guards without builds or hosts."""

from contextlib import redirect_stderr
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_module('nova_rust_checks_tested', 'check-rust.py')
VM = load_module('nova_doctest_vm_tested', 'doctest-vm.py')


class DoctestResultTests(unittest.TestCase):
    def setUp(self):
        self.names = ['rust_doctest_kernel_pci_rs_0',
                      'rust_doctest_kernel_alloc_kbox_rs_0']
        self.generated = '\n'.join('KUNIT_CASE(%s),' % name for name in self.names)
        self.suite = {
            'name': 'rust_doctests_kernel', 'sub_groups': [],
            'test_cases': [{'name': name, 'status': 'PASS'} for name in self.names],
            'misc': {'tests': 2, 'passed': 2, 'failed': 0,
                     'crashed': 0, 'errors': 0, 'skipped': 0},
        }

    def parsed(self, suite=None):
        return {'name': 'KUnit Test Group', 'sub_groups': [suite or self.suite]}

    def test_accepts_complete_passing_suite_with_pci(self):
        self.assertEqual(CHECK.check_results(self.generated, self.parsed()),
                         {'passed': 2, 'failed': 0, 'skipped': 0, 'pci_passed': 1})

    def test_rejects_empty_or_duplicated_generated_cases(self):
        for generated in ('', self.generated + '\n' + self.generated):
            with self.subTest(generated=generated), self.assertRaises(CHECK.CheckFailure):
                CHECK.check_results(generated, self.parsed())

    def test_rejects_empty_missing_or_duplicate_executed_cases(self):
        for change in ('empty', 'missing', 'duplicate'):
            suite = copy.deepcopy(self.suite)
            if change == 'empty':
                suite['test_cases'] = []
            elif change == 'missing':
                suite['test_cases'].pop()
            else:
                suite['test_cases'].append(copy.deepcopy(suite['test_cases'][0]))
            suite['misc']['tests'] = suite['misc']['passed'] = len(suite['test_cases'])
            with self.subTest(change=change), self.assertRaises(CHECK.CheckFailure):
                CHECK.check_results(self.generated, self.parsed(suite))

    def test_rejects_skipped_failed_or_crashed_case(self):
        for status in ('SKIP', 'FAIL', 'CRASH'):
            suite = copy.deepcopy(self.suite)
            suite['test_cases'][0]['status'] = status
            with self.subTest(status=status), self.assertRaises(CHECK.CheckFailure):
                CHECK.check_results(self.generated, self.parsed(suite))

    def test_rejects_inconsistent_or_unsuccessful_counts(self):
        for key, value in (('tests', 1), ('passed', 1), ('failed', 1),
                           ('crashed', 1), ('errors', 1), ('skipped', 1)):
            suite = copy.deepcopy(self.suite)
            suite['misc'][key] = value
            with self.subTest(key=key), self.assertRaises(CHECK.CheckFailure):
                CHECK.check_results(self.generated, self.parsed(suite))

    def test_rejects_missing_or_duplicate_suite(self):
        for groups in ([], [self.suite, copy.deepcopy(self.suite)]):
            with self.subTest(count=len(groups)), self.assertRaises(CHECK.CheckFailure):
                CHECK.check_results(self.generated, {'sub_groups': groups})


class VmDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.name = 'nova-rust-doctest'
        self.uuid = '12345678-1234-4234-8234-123456789abc'
        self.state = Path('/var/lib/libvirt/images') / self.name
        self.xml = VM.definition(self.name, self.uuid, self.state)

    def verify(self, xml):
        VM.verify_definition(xml, self.name, self.uuid, self.state)

    def test_accepts_owned_definition_and_libvirt_normalization(self):
        self.verify(self.xml)
        root = ET.fromstring(self.xml)
        for key in ('memory', 'currentMemory'):
            node = root.find(key)
            node.set('unit', 'KiB')
            node.text = str(2048 * 1024)
        root.find('os/type').set('machine', 'pc-q35-rhel9.4.0')
        self.verify(ET.tostring(root, encoding='unicode'))

    def test_rejects_wrong_identity_or_ownership(self):
        for change in ('uuid', 'owner', 'state'):
            root = ET.fromstring(self.xml)
            if change == 'uuid':
                root.find('uuid').text = '87654321-4321-4321-8321-cba987654321'
            else:
                owner = root.find('./metadata/{%s}doctest' % VM.NS)
                owner.set('manager' if change == 'owner' else 'state_dir', 'unexpected')
            with self.subTest(change=change), self.assertRaises(VM.Failure):
                self.verify(ET.tostring(root, encoding='unicode'))

    def test_rejects_disk_nic_or_gpu(self):
        for device in ('disk', 'interface', 'hostdev', 'video'):
            root = ET.fromstring(self.xml)
            if device == 'video':
                root.find('devices/video/model').set('type', 'vga')
            else:
                ET.SubElement(root.find('devices'), device)
            with self.subTest(device=device), self.assertRaises(VM.Failure):
                self.verify(ET.tostring(root, encoding='unicode'))

    def test_rejects_wrong_kernel_console_or_command_line(self):
        for change in ('kernel', 'console', 'cmdline'):
            root = ET.fromstring(self.xml)
            if change == 'console':
                root.find('devices/serial/source').set('path', '/unexpected/console.log')
            else:
                root.find('os/' + change).text = '/unexpected/bzImage'
            with self.subTest(change=change), self.assertRaises(VM.Failure):
                self.verify(ET.tostring(root, encoding='unicode'))


class RustCheckConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nova-rust-checks-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = self.root / 'rust-checks.json'
        self.filename = mock.patch.object(CHECK, '__file__', str(self.root / 'check-rust.py'))
        self.filename.start()
        self.addCleanup(self.filename.stop)
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configure(self, target):
        self.settings.write_text(json.dumps({'ssh_target': target}))

    def test_private_adjacent_configuration_preserves_bare_command(self):
        self.configure('file@file.invalid')
        self.assertEqual(CHECK.parse_args([]).ssh_target, 'file@file.invalid')

    def test_environment_overrides_private_file(self):
        self.configure('file@file.invalid')
        os.environ['NOVA_RUST_SSH_TARGET'] = 'environment@environment.invalid'
        self.assertEqual(CHECK.parse_args([]).ssh_target, 'environment@environment.invalid')

    def test_cli_overrides_environment_and_private_file(self):
        self.configure('file@file.invalid')
        os.environ['NOVA_RUST_SSH_TARGET'] = 'environment@environment.invalid'
        self.assertEqual(CHECK.parse_args(['--ssh-target', 'cli@cli.invalid']).ssh_target,
                         'cli@cli.invalid')

    def test_missing_target_fails_before_external_commands(self):
        with mock.patch.object(CHECK.subprocess, 'run') as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                CHECK.parse_args([])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_rejects_invalid_private_configuration(self):
        for contents in ('not JSON', '[]', '{"ssh_target": 123}',
                         '{"ssh_target":"file@file.invalid","unexpected":true}'):
            self.settings.write_text(contents)
            with self.subTest(contents=contents), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    CHECK.parse_args([])
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
