'use strict';
/* RekAI core tests. Zero-dependency: plain Node.
   Extracts the REKAI CORE section from index.html and exercises the pure
   logic (request building, response parsing, settings validation, i18n,
   history). Browser APIs are not touched here.
   Run: node tests/run-tests.js */
const fs = require('fs');
const path = require('path');

const htmlPath = path.join(__dirname, '..', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const m = html.match(/\/\* ==== REKAI CORE BEGIN ==== \*\/([\s\S]*?)\/\* ==== REKAI CORE END ==== \*\//);
if (!m) { console.error('FAIL: REKAI CORE section not found in index.html'); process.exit(1); }
global.window = {};
eval(m[1]);
const R = global.window.REKAI;
if (!R) { console.error('FAIL: REKAI not defined by core section'); process.exit(1); }

let passed = 0, failed = 0;
function ok(cond, name) {
  if (cond) { passed++; }
  else { failed++; console.error('FAIL: ' + name); }
}
function eq(a, b, name) { ok(JSON.stringify(a) === JSON.stringify(b), name + ' (got ' + JSON.stringify(a) + ')'); }

/* ---- identity / philosophy flags ---- */
eq(R.version, '1.0.0', 'version');
ok(R.config.singleFile && R.config.zeroDep && R.config.localFirst && R.config.zeroTelemetry, 'config flags');

/* ---- presets ---- */
const ids = R.PRESETS.map(p => p.id);
eq(new Set(ids).size, ids.length, 'preset ids unique');
eq(R.presetById('pasture').endpoint, 'http://localhost:8080/v1', 'pasture preset endpoint');
eq(R.presetById('ollama').endpoint, 'http://localhost:11434/v1', 'ollama preset endpoint');
eq(R.presetById('ollama').model, 'llama3.2-vision', 'ollama preset model is vision-capable');
eq(R.presetById('openai').needsKey, true, 'openai preset needs key');
eq(R.presetById('does-not-exist').id, 'custom', 'presetById falls back to custom');
for (const p of R.PRESETS) {
  for (const lang of R.LANGS) ok(typeof p.name[lang] === 'string' && p.name[lang].length > 0, 'preset ' + p.id + ' has name in ' + lang);
}

/* ---- endpoint normalization & settings validation ---- */
eq(R.normalizeEndpoint('  http://localhost:8080/v1/  '), 'http://localhost:8080/v1', 'normalize trims + strips slash');
eq(R.normalizeEndpoint('http://x/v1///'), 'http://x/v1', 'normalize strips many slashes');
eq(R.normalizeEndpoint(''), '', 'normalize empty');
eq(R.normalizeEndpoint(null), '', 'normalize null');
eq(R.normalizeEndpoint(42), '', 'normalize non-string');

eq(R.validateSettings({ endpoint: 'http://localhost:8080/v1', model: 'm' }), { ok: true, errors: [] }, 'valid settings');
eq(R.validateSettings({ endpoint: '', model: 'm' }).errors, ['endpoint-required'], 'endpoint required');
eq(R.validateSettings({ endpoint: 'ftp://x', model: 'm' }).errors, ['endpoint-invalid'], 'ftp endpoint invalid');
eq(R.validateSettings({ endpoint: 'not a url', model: 'm' }).errors, ['endpoint-invalid'], 'garbage endpoint invalid');
eq(R.validateSettings({ endpoint: 'https://api.openai.com/v1', model: ' ' }).errors, ['model-required'], 'model required');
eq(R.validateSettings({}).errors, ['endpoint-required', 'model-required'], 'both missing');
eq(R.validateSettings(null).ok, false, 'null settings not ok');

/* ---- chat URL & headers ---- */
eq(R.buildChatUrl('http://localhost:8080/v1'), 'http://localhost:8080/v1/chat/completions', 'chat url appended');
eq(R.buildChatUrl('http://localhost:8080/v1/'), 'http://localhost:8080/v1/chat/completions', 'chat url with trailing slash');
eq(R.buildChatUrl('http://x/v1/chat/completions'), 'http://x/v1/chat/completions', 'chat url not doubled');
eq(R.buildChatUrl(''), '', 'chat url empty');
eq(R.buildHeaders(''), { 'Content-Type': 'application/json' }, 'headers without key');
eq(R.buildHeaders('  sk-abc '), { 'Content-Type': 'application/json', 'Authorization': 'Bearer sk-abc' }, 'headers with trimmed key');
eq(R.buildHeaders(null), { 'Content-Type': 'application/json' }, 'headers null key');

/* ---- request body ---- */
{
  const b = R.buildRequestBody({ model: 'm1', question: '  what is this? ', defaultQuestion: 'DQ' });
  eq(b.model, 'm1', 'body model');
  eq(b.messages, [{ role: 'user', content: 'what is this?' }], 'body text-only, question trimmed');
}
{
  const img = 'data:image/jpeg;base64,AAAA';
  const b = R.buildRequestBody({ model: 'm1', question: '', defaultQuestion: 'DQ', imageDataUrl: img });
  eq(b.messages.length, 1, 'body with image: one message');
  eq(b.messages[0].content, [
    { type: 'text', text: 'DQ' },
    { type: 'image_url', image_url: { url: img } }
  ], 'body with image: default question + image part');
}
{
  const hist = [];
  for (let i = 0; i < 5; i++) hist.push({ q: 'q' + i, a: 'a' + i, ts: i });
  const b = R.buildRequestBody({ model: 'm', question: 'now', history: hist });
  eq(b.messages.length, 1 + R.HISTORY_CONTEXT_TURNS * 2, 'history context capped at N turns');
  eq(b.messages[0], { role: 'user', content: 'q2' }, 'history context starts at the right turn');
  eq(b.messages[b.messages.length - 1], { role: 'user', content: 'now' }, 'current question last');
}
{
  const b = R.buildRequestBody({ model: 'm', question: 'x', history: [{ q: 'only-q' }, null, { a: 'only-a' }] });
  eq(b.messages, [
    { role: 'user', content: 'only-q' },
    { role: 'assistant', content: 'only-a' },
    { role: 'user', content: 'x' }
  ], 'partial history entries handled');
}
eq(R.buildRequestBody({}).messages[0].content.length > 0, true, 'empty everything still yields a question');

/* ---- response parsing ---- */
eq(R.parseChatResponse({ choices: [{ message: { content: ' hello ' } }] }), { ok: true, text: 'hello' }, 'parse string content, trimmed');
eq(R.parseChatResponse({ choices: [{ message: { content: [{ type: 'text', text: 'a' }, { type: 'image_url', image_url: {} }, { type: 'text', text: 'b' }] } }] }),
   { ok: true, text: 'ab' }, 'parse array content joins text parts');
eq(R.parseChatResponse({ error: { message: 'bad key' } }), { ok: false, error: 'api-error', message: 'bad key' }, 'parse api error object');
eq(R.parseChatResponse({ error: 'oops' }), { ok: false, error: 'api-error', message: 'oops' }, 'parse api error string');
eq(R.parseChatResponse(null), { ok: false, error: 'bad-response' }, 'parse null');
eq(R.parseChatResponse('str'), { ok: false, error: 'bad-response' }, 'parse non-object');
eq(R.parseChatResponse({}), { ok: false, error: 'bad-response' }, 'parse missing choices');
eq(R.parseChatResponse({ choices: [] }), { ok: false, error: 'bad-response' }, 'parse empty choices');
eq(R.parseChatResponse({ choices: [{ message: { content: '   ' } }] }), { ok: false, error: 'empty-answer' }, 'parse whitespace-only answer');
eq(R.parseChatResponse({ choices: [{ message: {} }] }), { ok: false, error: 'empty-answer' }, 'parse missing content');

/* ---- HTTP error classification ---- */
eq(R.classifyHttpError(0), 'network', 'classify 0');
eq(R.classifyHttpError(400), 'bad-request', 'classify 400');
eq(R.classifyHttpError(401), 'auth', 'classify 401');
eq(R.classifyHttpError(403), 'auth', 'classify 403');
eq(R.classifyHttpError(404), 'not-found', 'classify 404');
eq(R.classifyHttpError(408), 'timeout', 'classify 408');
eq(R.classifyHttpError(429), 'rate-limit', 'classify 429');
eq(R.classifyHttpError(500), 'server', 'classify 500');
eq(R.classifyHttpError(503), 'server', 'classify 503');
eq(R.classifyHttpError(418), 'unknown-http', 'classify 418');

/* ---- auto-detection: loopback-only candidates, in order ---- */
eq(R.isLoopbackEndpoint('http://localhost:8080/v1'), true, 'loopback: localhost');
eq(R.isLoopbackEndpoint('http://127.0.0.1:11434/v1'), true, 'loopback: 127.0.0.1');
eq(R.isLoopbackEndpoint('http://[::1]:1234/v1'), true, 'loopback: [::1]');
eq(R.isLoopbackEndpoint('http://foo.localhost:8080/v1'), true, 'loopback: *.localhost');
eq(R.isLoopbackEndpoint('https://api.openai.com/v1'), false, 'not loopback: openai');
eq(R.isLoopbackEndpoint('http://192.168.1.10:11434/v1'), false, 'not loopback: LAN address is not probed');
eq(R.isLoopbackEndpoint('http://localhost.evil.example/v1'), false, 'not loopback: lookalike host');
eq(R.isLoopbackEndpoint(''), false, 'not loopback: empty');
eq(R.isLoopbackEndpoint('not a url'), false, 'not loopback: garbage');
eq(R.isLoopbackEndpoint(null), false, 'not loopback: null');
{
  const cands = R.detectCandidates();
  eq(cands.map(p => p.id), ['pasture', 'ollama', 'lmstudio'], 'detect candidates: local presets in order');
  for (const p of cands) ok(R.isLoopbackEndpoint(p.endpoint), 'detect candidate is loopback: ' + p.id);
  ok(cands.every(p => p.id !== 'openai' && p.id !== 'custom'), 'detect candidates exclude openai/custom');
}
ok(R.DETECT_TIMEOUT_MS > 0 && R.DETECT_TIMEOUT_MS <= 10000, 'detect timeout is short');

/* ---- auto-detection: /models URL, response parsing, model picking ---- */
eq(R.buildModelsUrl('http://localhost:11434/v1'), 'http://localhost:11434/v1/models', 'models url appended');
eq(R.buildModelsUrl('http://localhost:11434/v1/'), 'http://localhost:11434/v1/models', 'models url with trailing slash');
eq(R.buildModelsUrl('http://x/v1/models'), 'http://x/v1/models', 'models url not doubled');
eq(R.buildModelsUrl(''), '', 'models url empty');
eq(R.parseModelsResponse({ data: [{ id: 'a' }, { id: 'b' }] }), { ok: true, models: ['a', 'b'] }, 'parse models list');
eq(R.parseModelsResponse({ data: [{ id: 'a' }, { name: 'no-id' }, null, { id: '' }, { id: 7 }] }),
   { ok: true, models: ['a'] }, 'parse models drops entries without a string id');
eq(R.parseModelsResponse({ data: [] }), { ok: true, models: [] }, 'parse models empty list is ok');
eq(R.parseModelsResponse({ choices: [] }), { ok: false, models: [] }, 'parse models rejects chat-shaped body');
eq(R.parseModelsResponse(null), { ok: false, models: [] }, 'parse models null');
eq(R.parseModelsResponse('str'), { ok: false, models: [] }, 'parse models non-object');

eq(R.looksVisionCapable('llama3.2-vision'), true, 'vision hint: llama3.2-vision');
eq(R.looksVisionCapable('llava:7b'), true, 'vision hint: llava');
eq(R.looksVisionCapable('qwen2.5-vl-7b'), true, 'vision hint: qwen vl');
eq(R.looksVisionCapable('moondream'), true, 'vision hint: moondream');
eq(R.looksVisionCapable('llama3.1:8b'), false, 'vision hint: text model not flagged');
eq(R.looksVisionCapable(''), false, 'vision hint: empty');
eq(R.looksVisionCapable(null), false, 'vision hint: null');

eq(R.pickModel(['a', 'llama3.2-vision'], 'llama3.2-vision'), 'llama3.2-vision', 'pick: preferred present wins');
eq(R.pickModel(['text-only', 'llava:7b'], 'not-installed'), 'llava:7b', 'pick: vision-looking model');
eq(R.pickModel(['only-one'], ''), 'only-one', 'pick: single model');
eq(R.pickModel(['m1', 'm2'], ''), 'm1', 'pick: falls back to first reported');
eq(R.pickModel([], 'x'), null, 'pick: empty list -> null');
eq(R.pickModel(null, 'x'), null, 'pick: null list -> null');
eq(R.pickModel([42, 'real'], ''), 'real', 'pick: non-string entries ignored');

/* ---- images ---- */
ok(R.isImageDataUrl('data:image/png;base64,AAAA'), 'isImageDataUrl png');
ok(R.isImageDataUrl('data:image/jpeg;base64,/9j/'), 'isImageDataUrl jpeg');
ok(!R.isImageDataUrl('data:text/html;base64,AAAA'), 'isImageDataUrl rejects html');
ok(!R.isImageDataUrl('https://x/y.png'), 'isImageDataUrl rejects url');
eq(R.dataUrlBytes('data:image/png;base64,AAAA'), 3, 'dataUrlBytes 4 chars');
eq(R.dataUrlBytes('data:image/png;base64,AA=='), 1, 'dataUrlBytes pad 2');
eq(R.dataUrlBytes('data:image/png;base64,AAA='), 2, 'dataUrlBytes pad 1');
eq(R.dataUrlBytes('not-a-data-url'), -1, 'dataUrlBytes invalid');
eq(R.downscaleDims(4000, 3000, 1280), { width: 1280, height: 960 }, 'downscale landscape');
eq(R.downscaleDims(3000, 4000, 1280), { width: 960, height: 1280 }, 'downscale portrait');
eq(R.downscaleDims(800, 600, 1280), { width: 800, height: 600 }, 'no upscale');
eq(R.downscaleDims(0, 600, 1280), null, 'downscale zero width');
eq(R.downscaleDims(800, 600, 0), null, 'downscale zero edge');

/* ---- history ---- */
{
  const e = R.makeEntry('q', 'a', 123);
  eq(e, { q: 'q', a: 'a', ts: 123 }, 'makeEntry');
  eq(typeof R.makeEntry(null, undefined).ts, 'number', 'makeEntry defaults ts');
  eq(R.makeEntry(null, undefined).q, '', 'makeEntry null q -> empty string');
  const many = [];
  for (let i = 0; i < 80; i++) many.push({ q: 'q' + i, a: 'a' + i, ts: i });
  const trimmed = R.trimHistory(many);
  eq(trimmed.length, R.MAX_HISTORY, 'trimHistory caps at MAX_HISTORY');
  eq(trimmed[0].q, 'q30', 'trimHistory keeps the newest');
  eq(R.trimHistory('nope'), [], 'trimHistory non-array');
  eq(R.sanitizeHistory([{ q: 'a', a: 'b', ts: 1 }, { q: 5, a: 'b' }, null, { q: 'c', a: 'd' }]),
     [{ q: 'a', a: 'b', ts: 1 }, { q: 'c', a: 'd', ts: 0 }], 'sanitizeHistory drops invalid entries');
  eq(R.sanitizeHistory({}), [], 'sanitizeHistory non-array');
}

/* ---- i18n completeness ---- */
{
  const enKeys = Object.keys(R.STRINGS.en).sort();
  for (const lang of R.LANGS) {
    eq(Object.keys(R.STRINGS[lang]).sort(), enKeys, 'strings complete for ' + lang);
    ok(typeof R.SPEECH_LANG[lang] === 'string' && R.SPEECH_LANG[lang].length >= 5, 'speech lang for ' + lang);
    ok(R.t(lang, 'defaultQuestion').length > 0, 'default question in ' + lang);
  }
  eq(R.t('xx', 'ask'), R.STRINGS.en.ask, 't falls back to en for unknown lang');
  eq(R.t('ja', 'no-such-key'), 'no-such-key', 't returns key when missing');
  for (const code of ['network', 'auth', 'not-found', 'rate-limit', 'server', 'bad-request', 'timeout', 'bad-response', 'empty-answer', 'api-error', 'unknown-http']) {
    const key = R.errorKey(code);
    for (const lang of R.LANGS) ok(R.t(lang, key).length > 0 && R.t(lang, key) !== key, 'error text for ' + code + ' in ' + lang);
  }
}

/* ---- static safety / privacy invariants over the whole file ---- */
{
  // No innerHTML anywhere: untrusted answers must go through textContent.
  ok(!/\.innerHTML\s*=/.test(html), 'no innerHTML assignment in index.html');
  ok(!/document\.write/.test(html), 'no document.write');
  // Every absolute URL in the file must be a user-endpoint preset (localhost or OpenAI).
  const urls = html.match(/https?:\/\/[A-Za-z0-9.\-]+/g) || [];
  const allowed = ['http://localhost', 'https://api.openai.com'];
  for (const u of urls) ok(allowed.indexOf(u) >= 0, 'URL allowlist: ' + u);
  // No analytics / telemetry endpoints by name.
  ok(!/google-analytics|gtag\(|mixpanel|segment\.com|plausible\.io|analytics\./i.test(html), 'no telemetry/analytics code');
  // History persists text only: the entry builder must not accept an image field.
  ok(!/image/i.test(JSON.stringify(R.makeEntry('q', 'a', 0))), 'history entry has no image');
}

console.log('RekAI tests: ' + passed + ' passed, ' + failed + ' failed');
process.exit(failed ? 1 : 0);
