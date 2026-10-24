// Execute released Hub protocol helpers against captured synthetic responses.
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const { createRequire } = require('node:module');
const repo = process.argv[2] || 'E:/claude-code-hub';
const out = path.resolve(__dirname, '../output/qa/provider127');
const ts = createRequire(path.join(repo, 'package.json'))('typescript');
const root = 'src/app/v1/_lib/proxy/';
const cache = new Map();
function load(file) {
  if (cache.has(file)) return cache.get(file).exports;
  const source = cp.execFileSync('git', ['show', 'v0.9.7:' + file], { cwd: repo, encoding: 'utf8' });
  const mod = { exports: {} };
  cache.set(file, mod);
  const localRequire = (name) => {
    if (!name.startsWith('.')) throw new Error('Unexpected dependency: ' + name);
    return load(path.posix.normalize(path.posix.join(path.posix.dirname(file), name)) + '.ts');
  };
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('exports', 'require', 'module', js)(mod.exports, localRequire, mod);
  return mod.exports;
}
const emitter = load(root + 'fake-streaming/emitters.ts');
const validator = load(root + 'fake-streaming/response-validator.ts');
const classifier = load(root + 'stream-gate/frame-classifier.ts');
const parser = load(root + 'stream-gate/sse-frames.ts');
const report = [];
for (const name of fs.readdirSync(out).filter(x => x.endsWith('.summary.json'))) {
  const summary = JSON.parse(fs.readFileSync(path.join(out, name), 'utf8'));
  if (summary.api !== 'native' || summary.http !== 200) continue;
  const body = fs.readFileSync(path.join(out, name.replace('.summary.json', '.response.txt')), 'utf8');
  const result = { name: summary.name };
  if (summary.stream) {
    const frames = parser.parseSseBody(body);
    result.frameVerdicts = {};
    for (const f of frames) {
      const v = classifier.classifyFrame('anthropic', f.eventName, f.data);
      result.frameVerdicts[v] = (result.frameVerdicts[v] || 0) + 1;
    }
    result.fakeStreamValidator = validator.validateUpstreamResponse({ family: 'anthropic', status: 200, body, isStream: true });
  } else {
    result.nonStreamValidator = validator.validateUpstreamResponse({ family: 'anthropic', status: 200, body, isStream: false });
    const converted = emitter.emitFinalStream({ family: 'anthropic', finalBody: body });
    result.emittedToolStarts = parser.parseSseBody(converted).map(f => JSON.parse(f.data))
      .filter(e => e.type === 'content_block_start' && e.content_block.type === 'tool_use').length;
    result.originalToolCount = summary.tool_count;
    if (result.emittedToolStarts !== summary.tool_count) throw new Error('Emitter lost a tool');
    fs.writeFileSync(path.join(out, summary.name + '.hub-emitter.sse'), converted);
  }
  report.push(result);
}
report.push({ negativeControl: 'tool_use stop without tool block but with visible text', result:
  validator.validateUpstreamResponse({ family: 'anthropic', status: 200, isStream: false,
    body: JSON.stringify({ role: 'assistant', content: [{ type: 'text', text: 'Calling now.' }], stop_reason: 'tool_use' }) }) });
fs.writeFileSync(path.join(out, 'offline-hub-v0.9.7.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
