"""Build and bundle helpers for nova-core-build (Python >= 3.9)."""
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess
import tempfile


def run(args, capture=False, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def elf_build_id(path):
    path = Path(path)
    if path.suffix in ('.xz', '.gz', '.zst'):
        with tempfile.TemporaryDirectory(prefix='nova-module-') as directory:
            plain = Path(directory) / 'module.ko'
            if path.suffix == '.xz':
                plain.write_bytes(lzma.decompress(path.read_bytes()))
            elif path.suffix == '.gz':
                plain.write_bytes(gzip.decompress(path.read_bytes()))
            else:
                with plain.open('wb') as output:
                    subprocess.run(['zstd', '-q', '-d', '-c', str(path)], check=True, stdout=output)
            return elf_build_id(plain)
    output = run(['readelf', '-n', path], capture=True).stdout
    found = re.search(r'Build ID: ([0-9a-fA-F]+)', output)
    if not found:
        raise ValueError('No GNU build ID in ' + str(path))
    return found.group(1).lower()


def verify_bundle(root):
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('kind') not in ('kernel', 'module'):
        raise ValueError('Unsupported Nova bundle format')
    for name, digest in manifest['files'].items():
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or not p.parts:
            raise ValueError('Unsafe manifest path: ' + name)
        target = root / p
        if target.is_symlink() or not target.is_file() or sha256(target) != digest:
            raise ValueError('Artifact checksum mismatch: ' + name)
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    if actual != set(manifest['files']) | {'manifest.json'}:
        raise ValueError('Bundle contains missing or unlisted files')
    return manifest
