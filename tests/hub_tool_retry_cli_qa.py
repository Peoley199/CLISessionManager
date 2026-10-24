"""Real Claude client acceptance helpers for the isolated Hub container."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


class ProbeDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        # Claude can briefly leave child processes holding its working directory on Windows.
        target = Path(self.name).resolve()
        if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith('hub-cli-recovery-'):
            raise RuntimeError('Unexpected probe cleanup path')
        for attempt in range(20):
            try:
                return super().cleanup()
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.25)

RETRY = "Your tool call was malformed and could not be parsed. Please retry."


def cli_response(body, state):
    captured = state['cli_requests']
    captured.append(body)
    turn = len(captured)
    if turn == 1:
        blocks = [{'type': 'tool_use', 'id': 'call_before', 'name': 'Read',
                   'input': {'file_path': state['cli_file']}}]
        stop = 'tool_use'
    elif turn == 2:
        blocks = [{'type': 'thinking', 'thinking': 'Synthetic missing tool blocks.', 'signature': ''}]
        stop = 'tool_use'
    elif turn == 3:
        blocks = [{'type': 'tool_use', 'id': 'call_recovered', 'name': 'Read',
                   'input': {'file_path': state['cli_file']}}]
        stop = 'tool_use'
    else:
        blocks = [{'type': 'text', 'text': 'CLI_RECOVERY_OK'}]
        stop = 'end_turn'
    message = {'id': f'msg_cli_qa_{turn}', 'type': 'message', 'role': 'assistant',
               'model': body['model'], 'content': blocks, 'stop_reason': stop,
               'usage': {'input_tokens': 20, 'output_tokens': 10}}
    if not body.get('stream'):
        return json.dumps(message)

    def event(kind, **data):
        return 'event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n'

    raw = event('message_start', message={**message, 'content': [], 'stop_reason': None})
    for i, block in enumerate(blocks):
        if block['type'] == 'tool_use':
            raw += event('content_block_start', index=i, content_block={**block, 'input': {}})
            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
        elif block['type'] == 'thinking':
            raw += event('content_block_start', index=i, content_block={**block, 'thinking': ''})
            delta = {'type': 'thinking_delta', 'thinking': block['thinking']}
        else:
            raw += event('content_block_start', index=i, content_block={**block, 'text': ''})
            delta = {'type': 'text_delta', 'text': block['text']}
        raw += event('content_block_delta', index=i, delta=delta)
        raw += event('content_block_stop', index=i)
    return raw + event('message_delta', delta={'stop_reason': stop}, usage=message['usage']) + event('message_stop')


def run_cli(base, state):
    exe = Path(os.environ['APPDATA']) / 'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    with ProbeDirectory(prefix='hub-cli-recovery-') as folder:
        root = Path(folder)
        sample = root / 'sample.txt'
        sample.write_text('synthetic recovery verification', encoding='utf-8')
        state.update(mode='cli', cli_requests=[], cli_file=str(sample))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLAUDECODE'))}
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN='synthetic-qa-only',
                   CLAUDE_CONFIG_DIR=str(root / 'config'), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        result = subprocess.run([str(exe), '-p', 'Read sample.txt, then continue until done.',
                                 '--model', 'claude-sonnet-4-6', '--setting-sources', '',
                                 '--tools', 'Read', '--allowedTools', 'Read', '--max-turns', '5',
                                 '--no-session-persistence'],
                                cwd=root, env=env, capture_output=True, timeout=60)
        stdout = result.stdout.decode('utf-8', errors='replace').strip()
        requests = state['cli_requests']
        summary = {'exit_code': result.returncode, 'stdout': stdout,
                   'upstream_stream_flags': [r.get('stream') for r in requests]}
        assert result.returncode == 0 and stdout == 'CLI_RECOVERY_OK', summary
        assert summary['upstream_stream_flags'] == [True, True, False, True], summary
        # Every historical tool result survives recovery; the recovered tool executes too.
        results = [b for m in requests[-1]['messages'] if m['role'] == 'user'
                   for b in m['content'] if isinstance(m['content'], list) and b.get('type') == 'tool_result']
        assert [b['tool_use_id'] for b in results] == ['call_before', 'call_recovered'], results
        assert not any(b.get('is_error') for b in results), results
        summary['successful_tool_result_ids'] = [b['tool_use_id'] for b in results]
        return summary
