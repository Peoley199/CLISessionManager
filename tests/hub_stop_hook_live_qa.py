"""Read-only synthetic Stop judgments on deployed provider 127, plus native Claude verification."""
import copy
import datetime
import json
from pathlib import Path
import subprocess
import time
import uuid

import provider127_probe as probe
from hub_stop_hook_qa import FIXTURE, run_hook_cli

settings = json.loads((Path.home() / '.claude/settings.json').read_text(encoding='utf-8'))['env']
base = settings['ANTHROPIC_BASE_URL'].rstrip('/')
key = settings['ANTHROPIC_AUTH_TOKEN']
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
cases = []
for expected in [True, False]:
    payload = json.loads(FIXTURE.read_text(encoding='utf-8'))
    payload.update(model='DeepSeek-V4.1-Flash', max_tokens=2048, metadata={'session_id': str(uuid.uuid4())})
    question = payload['messages'][-1]
    payload['messages'] = [{'role': 'user', 'content': 'Synthetic transcript: reply with a marker.'},
        {'role': 'assistant', 'content': 'PROBE_OK' if expected else 'PROBE_WAIT'}, question]
    name = 'deployed-r3-stop-' + str(expected).lower()
    message, result = probe.request_case(name, payload, base=base, key=key)
    assert result.get('http') == 200 and not result.get('parse_errors'), result
    judgment = json.loads(''.join(b.get('text', '') for b in message['content']))
    assert judgment['ok'] is expected, judgment
    time.sleep(1)
    sid = payload['metadata']['session_id']
    rows = json.loads(probe.sql("SELECT json_agg(t) FROM (SELECT id,provider_id,status_code,request_sequence "
        f"FROM message_request WHERE session_id='{sid}' ORDER BY id) t"))
    assert rows and all(r['provider_id'] == 127 and r['status_code'] == 200 for r in rows), rows
    seq = rows[-1]['request_sequence']
    cache_key = f'session:{sid}:req:{seq}:snapshot:request:after:body'
    cached = subprocess.check_output(['docker', 'exec', 'claude-code-hub-redis-1', 'redis-cli', '--raw', 'GET', cache_key])
    wire = json.loads(cached)
    assert wire.get('stream') in [True, False]
    cases.append({'expected_ok': expected, 'judgment': judgment, 'rows': rows,
                  'last_upstream_stream': wire['stream'], 'session_id': sid})
report = {'passed': False, 'judgments_passed': True, 'started_at': started, 'cases': cases,
          'real_client': {'status': 'pending'}}
(probe.OUT / 'stop-live-qa-r3.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
client = run_hook_cli(base, key=key, model='DeepSeek-V4.1-Flash', tag='r3-live', prompt=(
    'Write three lines. First line: Synthetic opening paragraph for a protocol test with no external actions. '
    'Second line: PROBE_OK. Third line: Synthetic closing paragraph confirming that this is only a harmless protocol test. '
    'Include all three lines.'))
logs = subprocess.run(['docker', 'logs', '--since', started, 'claude-code-hub-app-1'], capture_output=True)
recovery_lines = [line for line in (logs.stdout + logs.stderr).decode('utf-8', errors='replace').splitlines()
                  if 'ProxyForwarder: Stop hook JSON recovery' in line]
report = {'passed': True, 'started_at': started, 'cases': cases, 'real_client': client,
          'stop_recovery_log_count': len(recovery_lines)}
(probe.OUT / 'stop-live-qa-r3.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
