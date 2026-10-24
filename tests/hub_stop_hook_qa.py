"""Synthetic native Stop hook acceptance helpers; never resumes a business task."""
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from hub_tool_retry_cli_qa import ProbeDirectory
from provider127_probe import parse_native

OUT = Path(__file__).resolve().parents[1] / 'output/qa/provider127'
FIXTURE = Path('E:/claude-code-hub-tool-recovery/tests/fixtures/anthropic-stop-hook/request.json')


def event(kind, **data):
    return 'event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n'


def response_message(body, text):
    message = {'id': 'msg_stop_probe', 'type': 'message', 'role': 'assistant', 'model': body['model'],
               'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
               'usage': {'input_tokens': 20, 'output_tokens': 10}}
    if body.get('stream') is not True:
        return json.dumps(message)
    raw = event('message_start', message={**message, 'content': [], 'stop_reason': None})
    raw += event('content_block_start', index=0, content_block={'type': 'text', 'text': ''})
    raw += event('content_block_delta', index=0, delta={'type': 'text_delta', 'text': text})
    raw += event('content_block_stop', index=0)
    return raw + event('message_delta', delta={'stop_reason': 'end_turn'}, usage={'output_tokens': 10}) + event('message_stop')


def hook_response(body, state):
    is_hook = body.get('output_config', {}).get('format', {}).get('type') == 'json_schema'
    state.setdefault('hook_requests', []).append({'hook': is_hook, 'stream': body.get('stream')})
    if is_hook:
        state['last_hook_body'] = body
        ok = state.get('hook_ok', True)
        if state.get('hook_mode') == 'broken_false':
            ok = state.get('ordinary_turns', 0) > 1
        text = json.dumps({'ok': ok, 'reason': 'PROBE_OK verified' if ok else 'PROBE_OK still missing. Continue.'})
        if (state.get('hook_mode', '').startswith('broken') and body.get('stream')) or state.get('hook_mode') == 'broken_twice':
            text = text[2:-5]
    else:
        state['ordinary_turns'] = state.get('ordinary_turns', 0) + 1
        text = 'PROBE_WAIT' if state.get('hook_mode') == 'broken_false' and state['ordinary_turns'] == 1 else 'PROBE_OK'
    return response_message(body, text)


def run_hook_cli(base, state=None, mode='healthy', key='synthetic-qa-only', model='claude-sonnet-4-6', tag='r3', prompt='Reply PROBE_OK.'):
    exe = Path(os.environ['APPDATA']) / 'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    with ProbeDirectory(prefix='hub-cli-recovery-') as folder:
        root = Path(folder)
        settings = root / 'hooks.json'
        debug = root / 'debug.log'
        settings.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'prompt',
            'prompt': 'Check whether the last assistant answer contains PROBE_OK. Input: $ARGUMENTS',
            'timeout': 60}]}]}}), encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLAUDECODE'))}
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN=key,
                   CLAUDE_CONFIG_DIR=str(root / 'config'), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        if state is not None:
            state.update(mode='stop', hook_mode=mode, hook_requests=[], ordinary_turns=0)
        result = subprocess.run([str(exe), '-p', prompt, '--model', model,
            '--system-prompt', 'Synthetic protocol probe only.', '--tools', '', '--settings', str(settings),
            '--setting-sources', '', '--strict-mcp-config', '--no-session-persistence', '--max-turns', '4',
            '--debug', 'hooks', '--debug-file', str(debug)], cwd=root, env=env, capture_output=True, timeout=150)
        log = debug.read_text(encoding='utf-8') if debug.exists() else ''
        (OUT / f'stop-cli-{tag}-{mode}.debug.log').write_text(log, encoding='utf-8')
        stdout = result.stdout.decode('utf-8', errors='replace').strip()
        judgments = []
        for line in log.splitlines():
            if 'Hooks: Model response:' in line:
                judgments.append(json.loads(line.split('Hooks: Model response:', 1)[1].strip()))
        summary = {'mode': mode, 'exit_code': result.returncode, 'stdout': stdout,
                   'judgments': judgments, 'condition_met_logged': 'Hooks: Prompt hook condition was met:' in log,
                   'json_validation_error': 'JSON validation failed' in log}
        assert result.returncode == 0 and 'PROBE_OK' in stdout, summary
        assert judgments and summary['condition_met_logged'] and not summary['json_validation_error'], summary
        assert judgments[-1]['ok'] is True, summary
        if mode == 'broken_false':
            assert judgments[0]['ok'] is False and len(judgments) == 2, summary
        if state is not None:
            requests = state['hook_requests']
            summary['requests'] = requests
            hook_flags = [r['stream'] for r in requests if r['hook']]
            expected = [True] if mode == 'healthy' else [True, False] * (2 if mode == 'broken_false' else 1)
            assert hook_flags == expected, summary
            assert all(r['stream'] is True for r in requests if not r['hook']), summary
            (OUT / 'stop-native-request-r3.json').write_text(json.dumps(state['last_hook_body'], indent=2) + '\n', encoding='utf-8')
        return summary


def run_hook_checks(base, state, request):
    payload = json.loads(FIXTURE.read_text(encoding='utf-8'))
    state.update(mode='stop', hook_mode='healthy', hook_requests=[])
    controls = []
    for mode, ok in [('healthy', True), ('healthy', False), ('broken', True), ('broken', False), ('broken_twice', True)]:
        state.update(hook_mode=mode, hook_ok=ok, hook_requests=[])
        status, raw = request(payload)
        flags = [r['stream'] for r in state['hook_requests']]
        assert flags == ([True] if mode == 'healthy' else [True, False]), flags
        if mode == 'broken_twice':
            assert status >= 400, status
            judgment = None
        else:
            message, errors = parse_native(raw, True)
            judgment = json.loads(''.join(b.get('text', '') for b in message['content']))
            assert status == 200 and not errors and judgment['ok'] is ok
        controls.append({'mode': mode, 'expected_ok': ok, 'status': status, 'streams': flags, 'judgment': judgment})
    state.update(hook_mode='broken', hook_ok=True, hook_requests=[])
    status, raw = request(payload, api_key='synthetic-no-recovery')
    assert status == 200 and [r['stream'] for r in state['hook_requests']] == [True]
    controls.append({'mode': 'unlisted', 'status': status, 'streams': [True]})

    # A delayed normal response must expose bytes before the upstream has finished.
    ordinary = copy.deepcopy(payload)
    ordinary.pop('output_config')
    ordinary['system'] = [{'type': 'text', 'text': 'Normal assistant.'}]
    state.update(delay_ordinary=True, hook_mode='healthy', hook_requests=[])
    req = urllib.request.Request(base + '/v1/messages', data=json.dumps(ordinary).encode(), headers={
        'Content-Type': 'application/json', 'x-api-key': 'synthetic-qa-only',
        'anthropic-version': '2023-06-01', 'User-Agent': 'claude-cli/2.1.220'})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as response:
        assert response.read1(256)
        first = time.monotonic() - start
        response.read()
        complete = time.monotonic() - start
    state['delay_ordinary'] = False
    assert first < 0.7 and complete >= 0.8, (first, complete)
    assert state['hook_requests'] == [{'hook': False, 'stream': True}]
    clients = [run_hook_cli(base, state, mode) for mode in ['healthy', 'broken', 'broken_false']]
    return {'passed': True, 'controls': controls, 'ordinary_first_byte_seconds': round(first, 3),
            'ordinary_complete_seconds': round(complete, 3), 'real_clients': clients}
