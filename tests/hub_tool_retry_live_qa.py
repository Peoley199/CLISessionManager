"""Synthetic recovery and ordinary follow-up against the deployed Hub/upstream."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import provider127_probe as probe

TAG = os.environ.get('CCH_QA_TAG', 'r2')

settings = json.loads((Path.home() / '.claude/settings.json').read_text(encoding='utf-8'))['env']
base = settings['ANTHROPIC_BASE_URL'].rstrip('/')
key = settings['ANTHROPIC_AUTH_TOKEN']
sid = str(uuid.uuid4())
retry = 'Your tool call was malformed and could not be parsed. Please retry.'
payload = {'model': 'DeepSeek-V4.1-Flash', 'max_tokens': 2048, 'stream': True,
           'metadata': {'session_id': sid}, 'thinking': {'type': 'adaptive'},
           'tools': [probe.TOOL], 'messages': [
               {'role': 'user', 'content': probe.PROMPT},
               {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_setup',
                                                'name': 'lookup_probe', 'input': {'label': 'setup'}}]},
               {'role': 'user', 'content': [
                   {'type': 'tool_result', 'tool_use_id': 'call_setup', 'content': '0'},
                   {'type': 'text', 'text': 'Setup finished. Now call lookup_probe for alpha and beta.'},
                   {'type': 'text', 'text': retry}]}]}
message, recovery = probe.request_case(f'deployed-{TAG}-merged-recovery', payload, base=base, key=key)
assert recovery.get('http') == 200 and not recovery.get('parse_errors'), recovery
assert recovery.get('tool_count') == 2 and {t['input'].get('label') for t in recovery['tools']} == {'alpha', 'beta'}, recovery
payload['messages'] += [{'role': 'assistant', 'content': message['content']},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': t['id'],
                                                     'content': str(17 if t['input']['label'] == 'alpha' else 25)}
                                                    for t in recovery['tools']]}]
_, follow = probe.request_case(f'deployed-{TAG}-ordinary-followup', payload, base=base, key=key)
assert follow.get('http') == 200 and not follow.get('parse_errors'), follow
follow_ok = '42' in follow.get('visible_text', '') and not follow.get('missing_tools_at_tool_stop')
time.sleep(1)
rows = json.loads(probe.sql("SELECT json_agg(t) FROM (SELECT id,provider_id,status_code,request_sequence,is_replay "
                           f"FROM message_request WHERE session_id='{sid}' ORDER BY id) t"))
assert len(rows) == 2 and all(r['provider_id'] == 127 and r['status_code'] == 200 for r in rows), rows
flags = []
for row in rows:
    cache_key = f"session:{sid}:req:{row['request_sequence']}:snapshot:request:after:body"
    data = subprocess.check_output(['docker', 'exec', 'claude-code-hub-redis-1', 'redis-cli', '--raw', 'GET', cache_key])
    flags.append(json.loads(data)['stream'])
assert flags == [False, True], flags
report = {'passed': follow_ok, 'tool_recovery_passed': True, 'ordinary_content_passed': follow_ok,
          'session_id': sid, 'request_rows': rows, 'upstream_stream_flags': flags,
          'recovered_tool_count': recovery['tool_count'], 'followup_text': follow['visible_text']}
(probe.OUT / f'live-qa-{TAG}.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
assert follow_ok, 'Ordinary upstream answer is truncated; see saved report (recovery and streaming flags passed).'
