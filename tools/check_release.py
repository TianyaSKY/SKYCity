"""Fail if backend/frontend versions or the requested release tag disagree."""
import argparse
import json
from pathlib import Path
import tomllib

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--tag')
args = parser.parse_args()
backend = tomllib.loads((root / 'backend/pyproject.toml').read_text())['project']['version']
frontend = json.loads((root / 'frontend/package.json').read_text())['version']
lock = json.loads((root / 'frontend/package-lock.json').read_text())
assert backend == frontend == lock['version'] == lock['packages']['']['version'], 'Release versions disagree'
if args.tag:
    assert args.tag == f'v{backend}', f'Tag must be v{backend}, got {args.tag}'
assert f'## [{backend}]' in (root / 'CHANGELOG.md').read_text(), 'Missing changelog entry'
print(f'SKYCity {backend}: release metadata OK')
