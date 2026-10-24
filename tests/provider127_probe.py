"""Read-only, synthetic tool-call diagnostics. Credentials stay in memory."""
import argparse
import copy
import datetime
import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request
import uuid

OUT = pathlib.Path(__file__).resolve().parents[1] / 'output/qa/provider127'
TOOL = {
    'name': 'lookup_probe',
    'description': 'Return a synthetic numeric value for a label. No external actions.',
    'input_schema': {'type': 'object', 'properties': {'label': {'type': 'string'}},
                     'required': ['label'], 'additionalProperties': False},
}
PROMPT = ('Call lookup_probe for label alpha and label beta, both in this response. '
          'Wait for both tool results. Then report their numeric sum. '
          'Do not invent tool results. This is a synthetic protocol test.')


def sql(query):
    return subprocess.check_output([
        'docker', 'exec', 'claude-code-hub-postgres-1', 'psql', '-U', 'postgres',
        '-d', 'claude_code_hub', '-At', '-c', query,
    ]).decode('utf-8').strip()


def provider():
    return json.loads(sql("SELECT json_build_object('url',url,'key',key) FROM providers WHERE id=127"))


def parse_native(raw, stream):
    if not stream:
        msg = json.loads(raw)
        return msg, []
    msg = {'role': 'assistant', 'content': []}
    blocks, fragments, errors = {}, {}, []
    for line in raw.splitlines():
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if not data or data == '[DONE]':
            continue
        try:
            event = json.loads(data)
        except ValueError:
            errors.append('invalid SSE JSON')
            continue
        kind = event.get('type')
        if kind == 'message_start':
            msg.update(event.get('message', {}))
        elif kind == 'content_block_start':
            blocks[event['index']] = copy.deepcopy(event['content_block'])
        elif kind == 'content_block_delta':
            idx, delta = event['index'], event['delta']
            block = blocks.setdefault(idx, {})
            for key in ['text', 'thinking', 'signature']:
                if key in delta:
                    block[key] = block.get(key, '') + delta[key]
            if delta.get('type') == 'input_json_delta':
                fragments[idx] = fragments.get(idx, '') + delta.get('partial_json', '')
        elif kind == 'message_delta':
            msg.update(event.get('delta', {}))
            msg['usage'] = event.get('usage', msg.get('usage'))
        elif kind == 'error':
            errors.append(str(event.get('error')))
    for idx, text in fragments.items():
        if not text:
            continue
        try:
            blocks[idx]['input'] = json.loads(text)
        except ValueError as exc:
            errors.append(f'tool input JSON: {exc}')
    msg['content'] = [blocks[i] for i in sorted(blocks)]
    return msg, errors


def parse_chat(raw, stream):
    if not stream:
        obj = json.loads(raw)
        choice = obj.get('choices', [{}])[0]
        msg = choice.get('message', {})
        msg.update(model=obj.get('model'), stop_reason=choice.get('finish_reason'), usage=obj.get('usage'))
        return msg, []
    msg = {'role': 'assistant', 'content': '', 'reasoning_content': ''}
    calls, errors = {}, []
    for line in raw.splitlines():
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if not data or data == '[DONE]':
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            errors.append('invalid SSE JSON')
            continue
        if obj.get('error'):
            errors.append(str(obj['error']))
        msg['model'] = obj.get('model', msg.get('model'))
        if obj.get('usage'):
            msg['usage'] = obj['usage']
        for choice in obj.get('choices', []):
            delta = choice.get('delta', {})
            for key in ['content', 'reasoning_content']:
                if delta.get(key):
                    msg[key] += delta[key]
            for call in delta.get('tool_calls', []):
                c = calls.setdefault(call.get('index', 0), {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    c['id'] = call['id']
                for key in ['name', 'arguments']:
                    if call.get('function', {}).get(key):
                        c['function'][key] += call['function'][key]
            if choice.get('finish_reason'):
                msg['stop_reason'] = choice['finish_reason']
    msg['tool_calls'] = [calls[i] for i in sorted(calls)]
    return msg, errors


def request_case(name, payload, api='native', base=None, key=None):
    OUT.mkdir(parents=True, exist_ok=True)
    config = provider() if base is None else {'url': base, 'key': key}
    endpoint = '/v1/messages' if api == 'native' else '/v1/chat/completions'
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['key'],
               'anthropic-version': '2023-06-01', 'User-Agent': 'claude-cli/2.1.220 (external, cli)'}
    if api == 'native':
        headers['x-api-key'] = config['key']
    req = urllib.request.Request(config['url'].rstrip('/') + endpoint,
                                 data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    start = time.monotonic()
    result = {'name': name, 'api': api, 'stream': payload.get('stream'),
              'thinking': payload.get('thinking'), 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    raw = ''
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result['http'] = response.status
            result['content_type'] = response.headers.get('Content-Type')
            result['request_id'] = response.headers.get('x-request-id') or response.headers.get('request-id')
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        result['http'] = exc.code
        result['content_type'] = exc.headers.get('Content-Type')
        result['retry_after'] = exc.headers.get('Retry-After')
        result['server'] = exc.headers.get('Server')
        result['request_id'] = exc.headers.get('x-request-id') or exc.headers.get('request-id')
        raw = exc.read().decode('utf-8', errors='replace')
    except Exception as exc:
        result['transport_error'] = str(exc).replace(config['key'], '[REDACTED]')
    result['seconds'] = round(time.monotonic() - start, 3)
    raw = raw.replace(config['key'], '[REDACTED]')
    (OUT / (name + '.request.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / (name + '.response.txt')).write_text(raw, encoding='utf-8')
    msg = {}
    try:
        parser = parse_native if api == 'native' else parse_chat
        msg, errors = parser(raw, 'text/event-stream' in (result.get('content_type') or ''))
        result.update(model=msg.get('model'), stop_reason=msg.get('stop_reason'), parse_errors=errors, usage=msg.get('usage'))
        calls = ([b for b in msg.get('content', []) if b.get('type') == 'tool_use']
                 if api == 'native' else msg.get('tool_calls', []))
        result['tool_count'] = len(calls)
        result['tools'] = []
        for c in calls:
            if api == 'native':
                args, toolname = c.get('input'), c.get('name')
            else:
                toolname = c.get('function', {}).get('name')
                try:
                    args = json.loads(c['function']['arguments'])
                except (ValueError, KeyError):
                    args = None
                    errors.append('invalid tool arguments')
            result['tools'].append({'id': c.get('id'), 'name': toolname, 'input': args})
        result['missing_tools_at_tool_stop'] = msg.get('stop_reason') in ['tool_use', 'tool_calls'] and not calls
        result['visible_text'] = (''.join(b.get('text', '') for b in msg.get('content', []) if b.get('type') == 'text')
                                  if api == 'native' else msg.get('content', ''))
        if msg.get('error'):
            result['error'] = msg['error']
    except Exception as exc:
        result['decode_error'] = str(exc)
    (OUT / (name + '.summary.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return msg, result


def baseline(name, stream, thinking, api='native', follow=True):
    if api == 'native':
        p = {'model': 'DeepSeek-V4.1-Flash', 'max_tokens': 2048, 'stream': stream,
             'tools': [TOOL], 'messages': [{'role': 'user', 'content': PROMPT}],
             'thinking': {'type': 'adaptive' if thinking else 'disabled'}}
        if thinking:
            p['output_config'] = {'effort': 'high'}
    else:
        p = {'model': 'DeepSeek-V4.1-Flash', 'max_tokens': 2048, 'stream': stream,
             'tools': [{'type': 'function', 'function': {'name': TOOL['name'], 'description': TOOL['description'], 'parameters': TOOL['input_schema']}}],
             'messages': [{'role': 'user', 'content': PROMPT}],
             'thinking': {'type': 'enabled' if thinking else 'disabled'}}
    msg, result = request_case(name + '-t1', p, api)
    if follow and result.get('tool_count') and not result.get('parse_errors'):
        if api == 'native':
            p['messages'].append({'role': 'assistant', 'content': msg['content']})
            p['messages'].append({'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': t['id'], 'content': json.dumps({'value': 17 if t['input'].get('label') == 'alpha' else 25})}
                for t in result['tools']]})
        else:
            p['messages'].append({k: msg[k] for k in ['role', 'content', 'reasoning_content', 'tool_calls'] if k in msg})
            p['messages'].extend({'role': 'tool', 'tool_call_id': t['id'], 'content': json.dumps({'value': 17 if t['input'].get('label') == 'alpha' else 25})}
                                 for t in result['tools'])
        request_case(name + '-t2', p, api)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--api', choices=['native', 'chat'], default='native')
    parser.add_argument('--prefix', default='baseline')
    args = parser.parse_args()
    for thinking in [True, False]:
        for stream in [True, False]:
            baseline(f'{args.prefix}-{args.api}-think{int(thinking)}-stream{int(stream)}', stream, thinking, args.api)
