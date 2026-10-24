"""Isolated container acceptance; production credentials stay in process memory."""
import importlib.util
import json
import os
import pathlib
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hub_tool_retry_cli_qa import cli_response, run_cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/qa/provider127'
STAMP = time.strftime('%Y%m%d_%H%M%S')
DB = 'cch_tool_retry_test_' + STAMP
APP = 'cch-tool-retry-qa-' + STAMP
REDIS = APP + '-redis'
IMAGE = os.environ.get('CCH_QA_IMAGE', 'claude-code-hub:tool-retry-v0.9.7-r2')
TAG = 'r3' if IMAGE.endswith('-r3') else 'r2'
spec = importlib.util.spec_from_file_location('probe', ROOT / 'tests/provider127_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
valid = (OUT / 'patch-retry-nonstream.response.txt').read_text(encoding='utf-8')
bad = (OUT / 'round1-native-think1-stream1-t1.response.txt').read_text(encoding='utf-8')
state = {'requests': [], 'invalid': False}


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def sql(query):
    return run(['docker', 'exec', 'claude-code-hub-postgres-1', 'psql', '-U', 'postgres',
                '-d', DB, '-At', '-v', 'ON_ERROR_STOP=1', '-c', query]).decode().strip()


def frame(kind, payload):
    return 'event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **payload}) + '\n\n'


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        state['requests'].append({'stream': body.get('stream'), 'path': self.path})
        streaming = body.get('stream') is True
        if state.get('mode') == 'stop':
            from hub_stop_hook_qa import hook_response
            raw = hook_response(body, state)
        elif state.get('mode') == 'cli':
            raw = cli_response(body, state)
        elif not streaming:
            raw = json.dumps({'type': 'message', 'stop_reason': 'tool_use', 'content': []}) if state['invalid'] else valid
        elif isinstance(body['messages'][-1]['content'], list) and body['messages'][-1]['content'][0].get('type') == 'tool_result':
            raw = frame('message_start', {'message': {'id': 'msg_follow', 'type': 'message', 'role': 'assistant',
                        'model': body['model'], 'content': [], 'usage': {'input_tokens': 50, 'output_tokens': 0}}})
            raw += frame('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}})
            raw += frame('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': '42'}})
            raw += frame('content_block_stop', {'index': 0})
            raw += frame('message_delta', {'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}})
            raw += frame('message_stop', {})
        else:
            raw = bad
        data = raw.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if state.get('delay_ordinary'):
            # Include real text before pausing; the existing stream gate may wait past metadata.
            boundary = data.index(b'event: content_block_stop')
            self.wfile.write(data[:boundary])
            self.wfile.flush()
            time.sleep(0.9)
            self.wfile.write(data[boundary:])
        else:
            self.wfile.write(data)


mock = ThreadingHTTPServer(('0.0.0.0', 0), Mock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
created_db = False
report = {}
try:
    run(['docker', 'exec', 'claude-code-hub-postgres-1', 'createdb', '-U', 'postgres', DB])
    created_db = True
    schema = run(['docker', 'exec', 'claude-code-hub-postgres-1', 'pg_dump', '-U', 'postgres',
                  '-d', 'claude_code_hub', '--schema-only', '--no-owner', '--no-privileges'])
    run(['docker', 'exec', '-i', 'claude-code-hub-postgres-1', 'psql', '-U', 'postgres', '-d', DB,
         '-v', 'ON_ERROR_STOP=1'], input=schema)
    sql("INSERT INTO users(id,name) VALUES (1,'synthetic-qa'); "
        "INSERT INTO keys(id,user_id,key,name) VALUES (1,1,'synthetic-qa-only','synthetic-qa'); "
        "INSERT INTO keys(id,user_id,key,name,provider_group) VALUES (2,1,'synthetic-no-recovery','synthetic-other','other'); "
        f"INSERT INTO providers(id,name,url,key,group_tag,max_retry_attempts) VALUES "
        f"(127,'synthetic-upstream','http://host.docker.internal:{mock.server_port}','synthetic','default',1), "
        f"(128,'synthetic-other','http://host.docker.internal:{mock.server_port}','synthetic','other',1)")
    run(['docker', 'run', '-d', '--name', REDIS, '--network', 'claude-code-hub_default', 'redis:7-alpine'])
    production = json.loads(run(['docker', 'inspect', 'claude-code-hub-app-1']))[0]
    original_env = dict(item.split('=', 1) for item in production['Config']['Env'] if '=' in item)
    env = dict(os.environ)
    env['DSN'] = original_env['DSN'].rsplit('/', 1)[0] + '/' + DB
    env['ADMIN_TOKEN'] = secrets.token_hex(32)
    run(['docker', 'run', '-d', '--name', APP, '--network', 'claude-code-hub_default',
         '-p', '127.0.0.1::3000', '-e', 'DSN', '-e', 'ADMIN_TOKEN', '-e', f'REDIS_URL=redis://{REDIS}:6379',
         '-e', 'AUTO_MIGRATE=false', '-e', 'ENABLE_RATE_LIMIT=false', '-e', 'ANTHROPIC_TOOL_RETRY_RECOVERY_PROVIDERS=127',
         '-e', 'ANTHROPIC_STOP_HOOK_RECOVERY_PROVIDERS=' + ('127' if TAG == 'r3' else ''),
         '-e', 'MAX_RETRY_ATTEMPTS_DEFAULT=1', '-e', 'PORT=3000', '-e', 'HOSTNAME=0.0.0.0', IMAGE], env=env)
    port = run(['docker', 'port', APP, '3000/tcp']).decode().strip().rsplit(':', 1)[1]
    base = 'http://127.0.0.1:' + port
    for _ in range(45):
        try:
            with urllib.request.urlopen(base + '/api/health', timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError('isolated container health timeout')

    def request(payload, api_key='synthetic-qa-only'):
        req = urllib.request.Request(base + '/v1/messages', data=json.dumps(payload).encode(), headers={
            'Content-Type': 'application/json', 'x-api-key': api_key,
            'anthropic-version': '2023-06-01', 'User-Agent': 'claude-cli/2.1.0 (external, cli)',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    payload = {'model': 'deepseek-v4.1-flash', 'max_tokens': 2048, 'stream': True,
               'tools': [probe.TOOL], 'messages': [{'role': 'user', 'content': probe.PROMPT}]}
    status, raw = request(payload)
    assert status == 200, (status, raw)
    first, _ = probe.parse_native(raw, True)
    assert first.get('stop_reason') == 'tool_use' and not any(b.get('type') == 'tool_use' for b in first['content'])
    payload['messages'] += [{'role': 'assistant', 'content': first['content']},
                            {'role': 'user', 'content': 'Your tool call was malformed and could not be parsed. Please retry.'}]
    status, raw = request(payload)
    assert status == 200, (status, raw)
    recovered, errors = probe.parse_native(raw, True)
    calls = [b for b in recovered['content'] if b.get('type') == 'tool_use']
    assert not errors and len(calls) == 2, (errors, recovered)
    assert {b['input']['label'] for b in calls} == {'alpha', 'beta'}
    retry_payload = json.loads(json.dumps(payload))
    payload['messages'] += [{'role': 'assistant', 'content': recovered['content']},
                            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': b['id'],
                             'content': '17' if b['input']['label'] == 'alpha' else '25'} for b in calls]}]
    status, raw = request(payload)
    assert status == 200, (status, raw)
    follow, errors = probe.parse_native(raw, True)
    assert not errors and any(b.get('text') == '42' for b in follow['content'])
    state['invalid'] = True
    retry_payload['messages'][0]['content'] += ' Separate negative-control request.'
    status, raw = request(retry_payload)
    assert status >= 400, (status, raw)
    invalid_recovery_status = status
    time.sleep(2)
    rows = json.loads(sql("SELECT coalesce(json_agg(t),'[]') FROM (SELECT provider_id,status_code,input_tokens,output_tokens FROM message_request ORDER BY id) t"))
    assert len(rows) == 4, rows
    assert [r['status_code'] for r in rows[:3]] == [200, 200, 200], rows
    assert rows[-1]['status_code'] >= 400, rows
    assert rows[1]['input_tokens'] > 0 and rows[1]['output_tokens'] > 0, rows
    assert [r['stream'] for r in state['requests']] == [True, False, True, False], state['requests']
    state['invalid'] = False
    for shape in ['merged', 'adjacent']:
        retry_text = {'type': 'text', 'text': 'Your tool call was malformed and could not be parsed. Please retry.'}
        results = [{'type': 'tool_result', 'tool_use_id': 'call_history', 'content': 'synthetic result'}]
        shape_payload = {'model': 'deepseek-v4.1-flash', 'max_tokens': 2048, 'stream': True, 'tools': [probe.TOOL],
                         'messages': [{'role': 'user', 'content': 'Synthetic ' + shape + ' retry check.'},
                                      {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_history',
                                                                       'name': 'lookup_probe', 'input': {'label': 'old'}}]},
                                      {'role': 'user', 'content': results + [retry_text] if shape == 'merged' else results}]}
        if shape == 'adjacent':
            shape_payload['messages'].append({'role': 'user', 'content': [retry_text]})
        status, raw = request(shape_payload)
        recovered_shape, errors = probe.parse_native(raw, True)
        assert status == 200 and not errors
        assert len([b for b in recovered_shape['content'] if b.get('type') == 'tool_use']) == 2
        assert state['requests'][-1]['stream'] is False
    # An explicit retry on a provider outside the allowlist must remain streaming.
    status, raw = request(shape_payload, api_key='synthetic-no-recovery')
    assert status == 200 and state['requests'][-1]['stream'] is True
    unlisted_row = json.loads(sql("SELECT row_to_json(t) FROM (SELECT provider_id FROM message_request ORDER BY id DESC LIMIT 1) t"))
    assert unlisted_row['provider_id'] == 128, unlisted_row
    real_client = run_cli(base, state)
    stop_report = None
    if TAG == 'r3':
        from hub_stop_hook_qa import run_hook_checks
        stop_report = run_hook_checks(base, state, request)
    report = {'passed': True, 'image': IMAGE, 'upstream_requests': state['requests'], 'request_rows': rows,
              'recovered_tool_count': len(calls), 'followup_text': '42', 'invalid_recovery_status': invalid_recovery_status,
              'database': DB, 'isolated': True, 'real_client': real_client, 'normalized_shapes': ['merged', 'adjacent'],
              'non_allowlisted_provider_streams': True, 'stop_hook': stop_report}
    print(json.dumps(report))
finally:
    logs = subprocess.run(['docker', 'logs', APP], capture_output=True).stdout.decode('utf-8', errors='replace')
    (OUT / f'container-qa-{TAG}.log').write_text(logs, encoding='utf-8')
    for name in [APP, REDIS]:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    if created_db:
        run(['docker', 'exec', 'claude-code-hub-postgres-1', 'dropdb', '-U', 'postgres', '--force', DB])
    mock.shutdown()
    if report:
        report['temporary_resources_removed'] = True
        (OUT / f'container-qa-{TAG}.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
