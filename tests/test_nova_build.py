"""Check environment-driven build selection without compiling or accessing a host."""
from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('nova_build_cli', ROOT / 'build-nova.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nova-env-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'build'
        self.seed = self.root / 'seed.config'
        self.seed.write_text('CONFIG_TEST_SEED=y\n')
        self.settings = {'NOVA_SOURCE': str(self.source), 'NOVA_BUILD_DIR': str(self.output),
                         'NOVA_CONFIG': str(self.seed), 'NOVA_KIND': 'kernel',
                         'NOVA_TOOLCHAIN': str(self.root / 'toolchain'), 'NOVA_PYTHON': 'python3.13',
                         'NOVA_JOBS': '20', 'NOVA_ALLOW_DIRTY': '0', 'NOVA_TAG': '',
                         'NOVA_OUTPUT': '', 'NOVA_LOCALVERSION': '-nova-test',
                         'NOVA_CLONE_REF': 'zhi/nova-vgpu-wip'}

    def parse(self, argv=(), changes=None):
        settings = dict(self.settings, **(changes or {}))
        with patch.dict(os.environ, settings, clear=True):
            return build.parse_args(list(argv))[1]

    def test_no_arguments_select_environment_and_preserve_existing_checkout(self):
        args = self.parse()
        self.assertEqual((args.source, args.build_dir, args.config), (self.source, self.output, self.seed))
        self.assertEqual((args.kind, args.python, args.jobs), ('kernel', 'python3.13', 20))
        self.assertEqual(args.toolchain, self.root / 'toolchain')
        self.assertFalse(args.allow_dirty)
        self.assertEqual(args.localversion, '-nova-test')
        self.assertIsNone(args.clone_ref)
        self.assertIsNone(args.nova_tag)
        self.assertIsNone(args.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.parse(changes={'NOVA_PYTHON': ''}).python, sys.executable)

    def test_existing_config_and_module_baseline_are_preserved(self):
        self.output.mkdir()
        active = self.output / '.config'
        active.write_text('CONFIG_KEEP_EXISTING=y\n')
        self.assertIsNone(self.parse().config)
        args = self.parse(['--kind', 'module'])
        self.assertIsNone(args.config)
        self.assertIsNone(args.localversion)
        self.assertEqual(args.build_dir, self.output)
        self.assertEqual(active.read_text(), 'CONFIG_KEEP_EXISTING=y\n')
        # Explicit CLI options are retained for the existing overwrite/mode guards.
        args = self.parse(['--config', str(self.seed)])
        self.assertEqual(args.config, self.seed)

    def test_cli_overrides_environment_including_boolean_and_invalid_defaults(self):
        args = self.parse(['--source', str(self.root / 'new-source'), '--build-dir', str(self.root / 'new-build'),
                           '--toolchain', str(self.root / 'new-tools'), '--python', 'custom-python',
                           '--jobs', '3', '--kind', 'module', '--no-allow-dirty', '--nova-tag', 'cli-tag',
                           '--output', str(self.root / 'explicit.tar.gz'), '--clone-ref', 'cli-ref'],
                          {'NOVA_KIND': 'invalid', 'NOVA_JOBS': 'invalid', 'NOVA_ALLOW_DIRTY': 'invalid',
                           'NOVA_TAG': 'env-tag', 'NOVA_OUTPUT': str(self.root / 'env.tar.gz')})
        self.assertEqual(args.source, self.root / 'new-source')
        self.assertEqual(args.build_dir, self.root / 'new-build')
        self.assertEqual(args.toolchain, self.root / 'new-tools')
        self.assertEqual((args.python, args.jobs, args.kind), ('custom-python', 3, 'module'))
        self.assertFalse(args.allow_dirty)
        self.assertEqual(args.nova_tag, 'cli-tag')
        self.assertEqual(args.output, self.root / 'explicit.tar.gz')
        self.assertEqual(args.clone_ref, 'cli-ref')
        self.assertTrue(self.parse(['--allow-dirty']).allow_dirty)

    def test_invalid_or_missing_settings_fail_before_build(self):
        for change in ({'NOVA_SOURCE': ''}, {'NOVA_BUILD_DIR': ''}, {'NOVA_KIND': 'invalid'},
                       {'NOVA_JOBS': 'bad'}, {'NOVA_JOBS': '0'}, {'NOVA_ALLOW_DIRTY': 'bad'}):
            with self.subTest(change=change), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    self.parse(changes=change)
                self.assertEqual(result.exception.code, 2)
        self.assertFalse(self.output.exists())


class BuildLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nova-launch-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = os.environ.copy()
        self.env.pop('_NOVA_BUILD_PYTHON_REEXEC', None)

    def launch(self, args=(), settings=None):
        env = dict(self.env, **(settings or {}))
        return subprocess.run([str(ROOT / 'build-nova.py')] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)

    def test_sourced_environment_runs_executable_without_parameters(self):
        # Intercept the first Git read so the exact no-argument launch never
        # creates a build or touches the user's source/configuration.
        bindir = self.root / 'bin'
        bindir.mkdir()
        marker = self.root / 'git-started'
        git = bindir / 'git'
        git.write_text('#!/bin/sh\n[ -z "${_NOVA_BUILD_PYTHON_REEXEC-}" ] || exit 99\n'
                       'printf "launcher-reached-git\\n" > "$NOVA_TEST_GIT_MARKER"\nexit 73\n')
        git.chmod(0o755)
        source = self.root / 'source'
        source.mkdir()
        output = self.root / 'build'
        result = subprocess.run(['bash', '-c',
            'source "$1"; export NOVA_PYTHON="$2" NOVA_SOURCE="$3" NOVA_BUILD_DIR="$4"; '
            'export PATH="$5:$PATH"; exec "$6"', 'nova-launch-test',
            str(ROOT / 'nova-build.env.example'), sys.executable, str(source), str(output),
            str(bindir), str(ROOT / 'build-nova.py')],
            env=dict(self.env, NOVA_TEST_GIT_MARKER=str(marker)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(marker.read_text(), 'launcher-reached-git\n')
        self.assertIn('Build directory: ' + str(output), result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(output.exists())

    def test_interpreter_wrapper_restarts_once_and_preserves_arguments(self):
        marker = self.root / 'launches'
        wrapper = self.root / 'python wrapper'
        wrapper.write_text('#!/bin/sh\nprintf "selected\\n" >> "$NOVA_TEST_MARKER"\n'
                           'exec "$NOVA_TEST_PYTHON" "$@"\n')
        wrapper.chmod(0o755)
        result = self.launch(['--help'], {'NOVA_PYTHON': str(wrapper),
                             'NOVA_TEST_PYTHON': sys.executable, 'NOVA_TEST_MARKER': str(marker)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-allow-dirty', result.stdout)
        self.assertEqual(marker.read_text(), 'selected\n')

    def test_cli_python_overrides_environment_and_missing_python_is_clear(self):
        missing = str(self.root / 'missing-python')
        result = self.launch(['--python', sys.executable, '--help'], {'NOVA_PYTHON': missing})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.launch(['--help'], {'NOVA_PYTHON': missing})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot start configured Python', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    @unittest.skipUnless(shutil.which('python3.6'), 'Python 3.6 is not installed')
    def test_old_configured_python_fails_cleanly_without_restart_loop(self):
        result = self.launch(['--help'], {'NOVA_PYTHON': shutil.which('python3.6')})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires Python 3.9 or newer', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
