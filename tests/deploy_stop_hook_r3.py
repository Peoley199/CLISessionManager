"""Deploy the tested r3 image and narrowly enable Stop recovery, with health rollback."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request

OUT = Path(__file__).resolve().parents[1] / 'output/qa/provider127'
COMPOSE = Path('E:/claude-code-hub/docker-compose.yaml')
OLD = 'claude-code-hub:tool-retry-v0.9.7-r2'
NEW = 'claude-code-hub:tool-retry-v0.9.7-r3'


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


qa = json.loads((OUT / 'container-qa-r3.json').read_text(encoding='utf-8'))
assert qa['passed'] and qa['image'] == NEW and qa['non_allowlisted_provider_streams']
assert qa['real_client']['upstream_stream_flags'] == [True, True, False, True]
assert qa['stop_hook']['passed'] and len(qa['stop_hook']['real_clients']) == 3
assert qa['stop_hook']['real_clients'][-1]['judgments'][0]['ok'] is False
image = json.loads(command(['docker', 'image', 'inspect', NEW]))[0]
assert image['Config']['Labels']['local.patch'] == 'anthropic-tool-retry-v3-stop-hook'
before = json.loads(command(['docker', 'inspect', 'claude-code-hub-app-1']))[0]
assert before['Config']['Image'] == OLD
original = COMPOSE.read_bytes()
old_line = ('image: ' + OLD).encode()
new_line = ('image: ' + NEW).encode()
assert original.count(old_line) == 1 and new_line not in original
assert b'ANTHROPIC_STOP_HOOK_RECOVERY_PROVIDERS' not in original
tool_env = b'      ANTHROPIC_TOOL_RETRY_RECOVERY_PROVIDERS: "127"'
assert original.count(tool_env) == 1
newline = b'\r\n' if b'\r\n' in original else b'\n'
updated = original.replace(old_line, new_line).replace(tool_env, tool_env + newline +
    b'      ANTHROPIC_STOP_HOOK_RECOVERY_PROVIDERS: "127"')
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = COMPOSE.with_name('docker-compose.yaml.backup-stop-hook-r3-' + stamp)
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
    assert new_env == {**old_env, 'ANTHROPIC_STOP_HOOK_RECOVERY_PROVIDERS': '127'}
    report = {'deployed': True, 'started_at': started, 'image': NEW, 'image_id': image['Id'],
              'previous_image_id': before['Image'], 'container_id': after['Id'], 'readiness': 200,
              'only_environment_change': {'ANTHROPIC_STOP_HOOK_RECOVERY_PROVIDERS': '127'},
              'tool_recovery_providers': new_env.get('ANTHROPIC_TOOL_RETRY_RECOVERY_PROVIDERS'),
              'compose_backup': str(backup), 'compose_sha256': hashlib.sha256(updated).hexdigest()}
    (OUT / 'deployment-r3.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))
except Exception:
    if COMPOSE.read_bytes() == updated:
        COMPOSE.write_bytes(original)
        command(['docker', 'compose', 'up', '-d', '--no-deps', '--pull', 'never', '--timeout', '45', 'app'])
        health()
        print('Deployment failed; previous image restored and healthy.')
    raise
