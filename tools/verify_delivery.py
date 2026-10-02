"""Verify a running single-port deployment and persistence after restart.

Uses fake AI and a paused test world. Intended for an isolated Compose project.
"""
import argparse
import ast
import json
import subprocess
import tomllib
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:18000')
parser.add_argument('--project', default='skycity-delivery')
args = parser.parse_args()
compose = ['docker', 'compose', '-p', args.project, '-f', str(root / 'docker-compose.yml')]


def request(path, method='GET', data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(args.url + path, data=body, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


health = request('/health')
assert health['status'] == 'ok'
version = tomllib.loads((root / 'backend/pyproject.toml').read_text())['project']['version']
assert health['app_version'] == version, health
# Read revision literals without importing the backend or requiring its
# dependencies on the host. The service must match the packaged migration head.
revisions, parents = set(), set()
for path in (root / 'backend/migrations/versions').glob('*.py'):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'revision', 'down_revision'}:
                    value = ast.literal_eval(node.value)
                    if target.id == 'revision':
                        revisions.add(value)
                    elif value is not None:
                        parents.update(value if isinstance(value, tuple) else [value])
heads = revisions - parents
assert len(heads) == 1 and health['database_revision'] in heads, (heads, health)
with urllib.request.urlopen(args.url, timeout=15) as response:
    assert b'<div id="app">' in response.read()
world = request('/api/worlds', 'POST', {'name': '交付验收', 'autonomous': False})
world_id = world['world_id']
request(f'/api/worlds/{world_id}/pause', 'POST')
before = request(f'/api/worlds/{world_id}/snapshot')
subprocess.run(compose + ['restart', 'skycity'], cwd=root, check=True)
subprocess.run(compose + ['up', '-d', '--wait', '--wait-timeout', '120'], cwd=root, check=True)
after = request(f'/api/worlds/{world_id}/snapshot')
assert before['world'] == after['world'], 'World state changed after restart'
assert before['agents'] == after['agents'], 'Resident state changed after restart'
assert any(row['world_id'] == world_id for row in request('/api/worlds'))
# The container's configured log directory must survive beside the database.
subprocess.run(compose + ['exec', '-T', 'skycity', 'python', '-c',
    "from pathlib import Path; assert Path('/data/ai_tiny_world.db').is_file(); assert Path('/data/logs/app.log').is_file()"], check=True)
print(f'Delivery OK: app={health["app_version"]}, schema={health["database_revision"]}, world={world_id}; restart preserved world and agents')
