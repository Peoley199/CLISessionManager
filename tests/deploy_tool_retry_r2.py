"""Deploy the verified local image, preserving Compose and rolling back on health failure."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request

OUT = Path(__file__).resolve().parents[1] / 'output/qa/provider127'
COMPOSE = Path('E:/claude-code-hub/docker-compose.yaml')
OLD = 'claude-code-hub:tool-retry-v0.9.7'
NEW = 'claude-code-hub:tool-retry-v0.9.7-r2'


def command(args):
    result = subprocess.run(args, cwd=COMPOSE.parent, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-1500:])
    return result.stdout


def health():
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:23000/api/health/ready', timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Hub readiness did not recover within 50 seconds')


qa = json.loads((OUT / 'container-qa-r2.json').read_text(encoding='utf-8'))
assert qa['passed'] and qa['image'] == NEW and qa['non_allowlisted_provider_streams']
assert qa['real_client']['upstream_stream_flags'] == [True, True, False, True]
image = json.loads(command(['docker', 'image', 'inspect', NEW]))[0]
assert image['Config']['Labels']['local.patch'] == 'anthropic-tool-retry-v2'
before = json.loads(command(['docker', 'inspect', 'claude-code-hub-app-1']))[0]
assert before['Config']['Image'] == OLD
original = COMPOSE.read_bytes()
old_line = ('image: ' + OLD).encode()
new_line = ('image: ' + NEW).encode()
assert original.count(old_line) == 1 and new_line not in original
updated = original.replace(old_line, new_line)
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = COMPOSE.with_name('docker-compose.yaml.backup-tool-retry-r2-' + stamp)
backup.write_bytes(original)
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
COMPOSE.write_bytes(updated)
try:
    command(['docker', 'compose', 'config', '--quiet'])
    command(['docker', 'compose', 'up', '-d', '--no-deps', '--pull', 'never', '--timeout', '45', 'app'])
    health()
    after = json.loads(command(['docker', 'inspect', 'claude-code-hub-app-1']))[0]
    assert after['Image'] == image['Id']
    old_env = dict(v.split('=', 1) for v in before['Config']['Env'] if '=' in v)
    new_env = dict(v.split('=', 1) for v in after['Config']['Env'] if '=' in v)
    assert old_env == new_env, 'Runtime environment unexpectedly changed'
    report = {'deployed': True, 'started_at': started, 'image': NEW, 'image_id': image['Id'],
              'previous_image_id': before['Image'], 'container_id': after['Id'], 'readiness': 200,
              'environment_unchanged': True, 'recovery_providers': new_env.get('ANTHROPIC_TOOL_RETRY_RECOVERY_PROVIDERS'),
              'compose_backup': str(backup), 'compose_sha256': hashlib.sha256(updated).hexdigest()}
    (OUT / 'deployment-r2.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))
except Exception:
    if COMPOSE.read_bytes() == updated:
        COMPOSE.write_bytes(original)
        command(['docker', 'compose', 'up', '-d', '--no-deps', '--pull', 'never', '--timeout', '45', 'app'])
        health()
        print('Deployment failed; previous image restored and healthy.')
    raise
