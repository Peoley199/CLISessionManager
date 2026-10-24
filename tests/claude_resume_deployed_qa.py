"""Verify the reported transcript via a read-only fork against deployed provider 127."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from hub_tool_retry_cli_qa import ProbeDirectory

original = Path.home() / '.claude/projects/E--AI-Project-AITapTapMaker-Life-Journey/a46d0641-6cad-4fde-b1f4-c085d8d504fb.jsonl'
before = hashlib.sha256(original.read_bytes()).hexdigest()
configured = json.loads((Path.home() / '.claude/settings.json').read_text(encoding='utf-8'))['env']
exe = Path(os.environ['APPDATA']) / 'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
with ProbeDirectory(prefix='hub-cli-recovery-') as folder:
    root = Path(folder)
    marker = 'DEPLOYMENT_READ_OK_' + uuid.uuid4().hex
    sample = root / 'verification.txt'
    sample.write_text(marker, encoding='utf-8')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLAUDECODE'))}
    env.update(ANTHROPIC_BASE_URL=configured['ANTHROPIC_BASE_URL'],
               ANTHROPIC_AUTH_TOKEN=configured['ANTHROPIC_AUTH_TOKEN'],
               CLAUDE_CONFIG_DIR=str(root / 'config'), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    prompt = ('This turn is only a deployment connection verification. Do not continue the previous project task. '
              'Use the Read tool to read this exact file: ' + str(sample) + '. '
              'Return its complete content verbatim. Do not edit files, execute commands, or call any other tools.')
    result = subprocess.run([str(exe), '-p', prompt, '--resume', str(original), '--fork-session',
                             '--no-session-persistence', '--model', 'DeepSeek-V4.1-Flash',
                             '--setting-sources', '', '--strict-mcp-config', '--tools', 'Read',
                             '--allowedTools', 'Read', '--max-turns', '4',
                             '--output-format', 'stream-json', '--verbose'],
                            cwd=root, env=env, capture_output=True, timeout=120)
    events = []
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    tool_names = [b.get('name') for e in events if e.get('type') == 'assistant'
                  for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    report = {'exit_code': result.returncode, 'fork_session_id': final.get('session_id'),
              'read_verified': marker in final.get('result', ''), 'tools_called': tool_names,
              'original_sha256_before': before, 'original_sha256_after': hashlib.sha256(original.read_bytes()).hexdigest(),
              'is_error': final.get('is_error'), 'result_subtype': final.get('subtype'),
              'result_text': final.get('result', '')[:500],
              'target_read_called': any(b.get('name') == 'Read' and b.get('input', {}).get('file_path') == str(sample)
                                        for e in events if e.get('type') == 'assistant'
                                        for b in e.get('message', {}).get('content', [])),
              'tool_result_verified': any(marker in json.dumps(e.get('message', {}).get('content', []))
                                          for e in events if e.get('type') == 'user')}
    out = Path(__file__).resolve().parents[1] / 'output/qa/provider127/original-session-fork-r2.json'
    out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))
    assert result.returncode == 0 and report['read_verified'] and tool_names and set(tool_names) == {'Read'}, report
    assert report['original_sha256_after'] == before and not report['is_error'], report
