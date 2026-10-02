"""Exercise the standalone project without local settings or kernel outputs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nova_common import sha256, verify_bundle, write_json


class ProjectTests(unittest.TestCase):
    def test_example_settings_follow_checkout_from_another_working_directory(self):
        with tempfile.TemporaryDirectory(prefix='nova project ') as temporary:
            directory = Path(temporary)
            project = directory / 'checkout with spaces'
            project.mkdir()
            settings = project / 'nova-build.env'
            shutil.copyfile(ROOT / 'nova-build.env.example', settings)
            inspect = ('import json, os; print(json.dumps(dict('
                       'root=os.environ["NOVA_BUILD_ROOT"], '
                       'build=os.environ["NOVA_BUILD_DIR"], '
                       'source=os.environ["NOVA_SOURCE"], '
                       'config=os.environ["NOVA_CONFIG"], '
                       'path=os.environ["PATH"], cwd=os.getcwd())))')
            result = subprocess.run(['bash', '-c', 'source "$1" && "$2" -c "$3"',
                                     'nova-env-test', str(settings), sys.executable, inspect],
                                    cwd=directory, check=True, capture_output=True, text=True)
            actual = json.loads(result.stdout)
            self.assertEqual(actual['root'], str(project))
            self.assertEqual(actual['build'], str(project / 'build'))
            self.assertEqual(Path(actual['source']).resolve(), directory / 'nova-core')
            self.assertEqual(actual['config'], str(project / 'configs/nova-kvm.config'))
            self.assertEqual(actual['cwd'], str(directory))
            self.assertEqual(actual['path'], os.environ['PATH'])
            self.assertFalse((project / 'build').exists())

    def test_bundle_checksums_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'module.ko').write_bytes(b'original')
            manifest = {'format': 1, 'kind': 'module',
                        'files': {'module.ko': sha256(root / 'module.ko')}}
            write_json(root / 'manifest.json', manifest)
            self.assertEqual(verify_bundle(root), manifest)
            (root / 'extra').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'unlisted'):
                verify_bundle(root)
            (root / 'extra').unlink()
            (root / 'module.ko').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                verify_bundle(root)


if __name__ == '__main__':
    unittest.main()
