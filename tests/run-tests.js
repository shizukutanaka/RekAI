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

/* ---- region crop: selection (display px) -> source px ---- */
ok(R.MAX_SOURCE_EDGE > R.MAX_IMAGE_EDGE, 'source keeps more pixels than what is sent');
{
  const full = { x: 0, y: 0, w: 2560, h: 1440 };
  eq(R.selectionToSource({ x0: 0, y0: 0, x1: 640, y1: 360 }, 640, 360, full), { x: 0, y: 0, w: 2560, h: 1440 }, 'crop: whole preview -> whole source');
  eq(R.selectionToSource({ x0: 100, y0: 50, x1: 300, y1: 162.5 }, 640, 360, full), { x: 400, y: 200, w: 800, h: 450 }, 'crop: scaled to source px');
  eq(R.selectionToSource({ x0: 300, y0: 162.5, x1: 100, y1: 50 }, 640, 360, full), { x: 400, y: 200, w: 800, h: 450 }, 'crop: any drag direction');
  eq(R.selectionToSource({ x0: -50, y0: -50, x1: 9999, y1: 9999 }, 640, 360, full), full, 'crop: clamped to the image');
  eq(R.selectionToSource({ x0: 10, y0: 10, x1: 15, y1: 200 }, 640, 360, full), null, 'crop: too narrow -> null');
  eq(R.selectionToSource({ x0: 10, y0: 10, x1: 10, y1: 10 }, 640, 360, full), null, 'crop: click without drag -> null');
  const view = { x: 400, y: 200, w: 800, h: 450 };
  eq(R.selectionToSource({ x0: 0, y0: 0, x1: 400, y1: 225 }, 800, 450, view), { x: 400, y: 200, w: 400, h: 225 }, 'crop: nested crop is relative to the current view');
  eq(R.selectionToSource({ x0: 0, y0: 0, x1: 800, y1: 450 }, 800, 450, view), view, 'crop: nested crop never exceeds the view');
  eq(R.selectionToSource(null, 640, 360, full), null, 'crop: null selection');
  eq(R.selectionToSource({ x0: 0, y0: 0, x1: 100, y1: 100 }, 0, 360, full), null, 'crop: zero display size');
  eq(R.selectionToSource({ x0: 0, y0: 0, x1: 100, y1: 100 }, 640, 360, null), null, 'crop: no view');
  eq(R.selectionToSource({ x0: NaN, y0: 0, x1: 100, y1: 100 }, 640, 360, full), null, 'crop: NaN rejected');
  // Sending only the region keeps its resolution: 800x450 of a 2560x1440 screen
  // is sent 1:1 instead of at half scale inside a 1280x720 full-screen image.
  eq(R.downscaleDims(800, 450, R.MAX_IMAGE_EDGE), { width: 800, height: 450 }, 'crop: region sent at native resolution');
  eq(R.downscaleDims(2560, 1440, R.MAX_IMAGE_EDGE), { width: 1280, height: 720 }, 'crop: full screen is halved');
}

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

/* ---- share intake: core helpers ---- */
eq(R.SHARE_INBOX_CACHE, 'rekai-share-inbox', 'share inbox cache name');
ok(R.SHARE_IMAGE_KEY.length > 0 && R.SHARE_META_KEY.length > 0 && R.SHARE_IMAGE_KEY !== R.SHARE_META_KEY, 'share inbox keys distinct');
eq(R.SHARE_ACTION_SUFFIX, '/share-target', 'share action suffix');
eq(R.parseSharedLaunch('?shared=1'), true, 'shared launch: flag present');
eq(R.parseSharedLaunch('?x=2&shared=1'), true, 'shared launch: flag among others');
eq(R.parseSharedLaunch('?shared=0'), false, 'shared launch: 0 is not a launch');
eq(R.parseSharedLaunch('?shared=10'), false, 'shared launch: 10 is not a launch');
eq(R.parseSharedLaunch(''), false, 'shared launch: empty');
eq(R.parseSharedLaunch(null), false, 'shared launch: null');
eq(R.parseSharedLaunch(undefined), false, 'shared launch: undefined');
eq(R.sharedQuestionText({ title: 'T', text: 'body', url: 'https://example.com/a' }), 'T\nbody\nhttps://example.com/a', 'shared text joins title/text/url');
eq(R.sharedQuestionText({ text: '  padded  ' }), 'padded', 'shared text trims');
eq(R.sharedQuestionText({ title: 42, text: 'ok', url: null }), 'ok', 'shared text drops non-strings');
eq(R.sharedQuestionText({}), '', 'shared text empty meta');
eq(R.sharedQuestionText(null), '', 'shared text null meta');
eq(R.sharedQuestionText('str'), '', 'shared text non-object');
{
  const long = R.sharedQuestionText({ text: 'x'.repeat(5000) });
  eq(long.length, R.SHARE_QUESTION_MAX, 'shared text capped at max length');
}

/* ---- share intake: PWA manifest ---- */
{
  const manifestPath = path.join(__dirname, '..', 'manifest.webmanifest');
  ok(fs.existsSync(manifestPath), 'manifest.webmanifest exists');
  let man = null;
  try { man = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch (e) { /* asserted below */ }
  ok(man && typeof man === 'object', 'manifest parses as JSON');
  if (man) {
    ok(typeof man.name === 'string' && man.name.length > 0, 'manifest has a name');
    eq(man.start_url, './index.html', 'manifest start_url is the app page');
    eq(man.display, 'standalone', 'manifest display standalone');
    const sizes = (man.icons || []).map(i => i.sizes);
    ok(sizes.indexOf('192x192') >= 0 && sizes.indexOf('512x512') >= 0, 'manifest icons cover 192 and 512');
    ok((man.icons || []).some(i => i.purpose === 'maskable'), 'manifest has a maskable icon');
    for (const ic of man.icons || []) {
      ok(fs.existsSync(path.join(__dirname, '..', ic.src)), 'manifest icon file exists: ' + ic.src);
    }
    const st = man.share_target || {};
    eq(st.method, 'POST', 'share_target method is POST (required for files)');
    eq(st.enctype, 'multipart/form-data', 'share_target enctype is multipart');
    ok(typeof st.action === 'string' && st.action.slice(-R.SHARE_ACTION_SUFFIX.length) === R.SHARE_ACTION_SUFFIX,
       'share_target action matches the core suffix');
    const files = (st.params && st.params.files) || [];
    eq(files.length, 1, 'share_target takes exactly one file param');
    if (files.length === 1) {
      eq(files[0].name, 'image', 'share_target file param is named image');
      ok((files[0].accept || []).every(a => typeof a === 'string' && a.indexOf('image/') === 0),
         'share_target accepts images only');
    }
    ok(!/https?:\/\//.test(JSON.stringify(man)), 'manifest contains no absolute URLs');
  }
}

/* ---- pre-send confirmation summary ---- */
{
  eq(R.confirmEnabled(undefined), true, 'confirm: on when settings missing');
  eq(R.confirmEnabled({}), true, 'confirm: on by default (old saved settings)');
  eq(R.confirmEnabled({ confirmBeforeSend: true }), true, 'confirm: on when enabled');
  eq(R.confirmEnabled({ confirmBeforeSend: false }), false, 'confirm: off only when explicitly disabled');
  eq(R.formatBytes(512), '512 B', 'formatBytes: bytes');
  eq(R.formatBytes(2048), '2.0 KB', 'formatBytes: KB');
  eq(R.formatBytes(3 * 1048576), '3.0 MB', 'formatBytes: MB');
  eq(R.formatBytes(-1), '', 'formatBytes: invalid');
  const img = 'data:image/jpeg;base64,' + 'A'.repeat(400);
  const hist = [R.makeEntry('q1', 'a1', 1), R.makeEntry('q2', 'a2', 2)];
  const body = R.buildRequestBody({ model: 'm1', question: 'What?', defaultQuestion: 'D', imageDataUrl: img, history: hist });
  const d = R.describeOutgoing({ endpoint: 'http://localhost:11434/v1', apiKey: '', body: body });
  eq(d.url, R.buildChatUrl('http://localhost:11434/v1'), 'describe: url is the chat URL that is posted to');
  eq(d.host, 'localhost:11434', 'describe: host');
  eq(d.local, true, 'describe: loopback is local');
  eq(d.insecure, false, 'describe: local http is not flagged');
  eq(d.model, 'm1', 'describe: model from body');
  eq(d.question, 'What?', 'describe: question from body');
  eq(d.hasImage, true, 'describe: image detected in body');
  eq(d.imageBytes, R.dataUrlBytes(img), 'describe: image size from body');
  eq(d.historyTurns, 2, 'describe: history turns counted from body');
  eq(d.sendsKey, false, 'describe: no key');
  const body2 = R.buildRequestBody({ model: 'm2', question: '', defaultQuestion: 'Describe', imageDataUrl: null, history: [] });
  const d2 = R.describeOutgoing({ endpoint: 'http://192.168.1.5:8080/v1', apiKey: ' sk-x ', body: body2 });
  eq(d2.local, false, 'describe: LAN host is not local');
  eq(d2.insecure, true, 'describe: non-local http flagged');
  eq(d2.hasImage, false, 'describe: no image');
  eq(d2.imageBytes, 0, 'describe: no image bytes');
  eq(d2.question, 'Describe', 'describe: default question is what is sent');
  eq(d2.historyTurns, 0, 'describe: no history');
  eq(d2.sendsKey, true, 'describe: key sent');
  const d3 = R.describeOutgoing({ endpoint: 'https://api.openai.com/v1', apiKey: 'k', body: body2 });
  eq(d3.local === false && d3.insecure === false, true, 'describe: https remote is not flagged insecure');
  const d4 = R.describeOutgoing({});
  eq(typeof d4.url === 'string' && d4.hasImage === false && d4.historyTurns === 0, true, 'describe: tolerates empty input');
  ok(/id="confirmDlg"/.test(html) && /id="confirmSendBtn"/.test(html) && /id="confirmCancelBtn"/.test(html), 'confirm dialog present');
  ok(/id="confirmInput" type="checkbox" checked/.test(html), 'confirm setting present and on by default');
}

/* ---- local engine setup commands (CORS origin) ---- */
{
  eq(R.pageOriginToAllow('file://'), '', 'origin: file page needs no allow');
  eq(R.pageOriginToAllow('null'), '', 'origin: opaque needs no allow');
  eq(R.pageOriginToAllow('http://localhost:8080'), '', 'origin: localhost needs no allow');
  eq(R.pageOriginToAllow('http://127.0.0.1:5500'), '', 'origin: loopback needs no allow');
  eq(R.pageOriginToAllow('https://shizukutanaka.github.io'), 'https://shizukutanaka.github.io', 'origin: pages origin must be allowed');
  eq(R.pageOriginToAllow('https://a.example:8443/x?y'), 'https://a.example:8443', 'origin: only scheme+host+port');
  eq(R.pageOriginToAllow('app://rekai'), '', 'origin: non-http scheme ignored');
  const local = R.localSetupCommands('file://');
  eq(local.ollama.map(c => c.cmd), ['ollama pull llama3.2-vision'], 'setup: file page shows only the model pull');
  eq(local.lmstudio.map(c => c.cmd), ['lms server start --cors'], 'setup: LM Studio always needs CORS');
  const web = R.localSetupCommands('https://shizukutanaka.github.io');
  eq(web.origin, 'https://shizukutanaka.github.io', 'setup: origin reported');
  eq(web.ollama.map(c => c.cmd), ['ollama pull llama3.2-vision',
    'OLLAMA_ORIGINS=https://shizukutanaka.github.io ollama serve',
    '$env:OLLAMA_ORIGINS="https://shizukutanaka.github.io"; ollama serve'], 'setup: web page adds OLLAMA_ORIGINS');
  ok(web.ollama.concat(web.lmstudio).every(c => typeof R.STRINGS.ja[c.key] === 'string'), 'setup: every step has a label');
}

/* ---- history export / import (the user's own file) ---- */
{
  const h = [R.makeEntry('q1', 'a1', 1000), R.makeEntry('q2', 'a2', 2000)];
  const txt = R.exportHistory(h, Date.UTC(2026, 10, 19));
  const obj = JSON.parse(txt);
  eq(obj.format, 'rekai-history', 'export: format tag');
  eq(obj.version, 1, 'export: version');
  eq(obj.entries, h, 'export: entries are the history');
  ok(txt.indexOf('data:image') < 0, 'export: no image data');
  eq(R.exportHistory([{ q: 'x', a: 'y', ts: 1, img: 'data:image/png;base64,AA' }, { bad: 1 }]).indexOf('data:image'), -1, 'export: strips unknown fields and bad entries');
  ok(/^rekai-history-\d{4}-\d{2}-\d{2}\.json$/.test(R.historyFileName(Date.now())), 'export: file name');
  eq(R.parseHistoryFile(txt), { ok: true, entries: h }, 'import: round trip');
  eq(R.parseHistoryFile(JSON.stringify(h)).ok, true, 'import: bare array accepted');
  eq(R.parseHistoryFile('').error, 'empty', 'import: empty');
  eq(R.parseHistoryFile('{nope').error, 'not-json', 'import: not json');
  eq(R.parseHistoryFile('{"format":"other","entries":[]}').error, 'wrong-format', 'import: other format rejected');
  eq(R.parseHistoryFile('[{"x":1}]').error, 'wrong-format', 'import: array without entries rejected');
  eq(R.parseHistoryFile('x'.repeat(R.MAX_HISTORY_FILE_BYTES + 1)).error, 'too-large', 'import: size cap');
  eq(R.parseHistoryFile('{"format":"rekai-history","entries":[{"q":"a","a":"b","ts":"x"}]}').entries, [{ q: 'a', a: 'b', ts: 0 }], 'import: bad ts normalized');
  const m1 = R.mergeHistory(h, [R.makeEntry('q2', 'a2', 2000), R.makeEntry('q0', 'a0', 500), R.makeEntry('q3', 'a3', 3000)]);
  eq(m1.added, 2, 'merge: duplicates skipped');
  eq(m1.history.map(e => e.q), ['q0', 'q1', 'q2', 'q3'], 'merge: sorted by time');
  eq(R.mergeHistory(h, h).added, 0, 'merge: same file twice adds nothing');
  const many = []; for (let i = 0; i < 80; i++) many.push(R.makeEntry('m' + i, 'a', i));
  const m2 = R.mergeHistory([], many);
  eq(m2.history.length, R.MAX_HISTORY, 'merge: trimmed to MAX_HISTORY');
  eq(m2.history[m2.history.length - 1].q, 'm79', 'merge: newest kept');
  ok(/id="historyFileInput" type="file" accept="application\/json,\.json" hidden/.test(html), 'import file input present');
}

/* ---- share intake: service worker contract ---- */
{
  const swPath = path.join(__dirname, '..', 'sw.js');
  ok(fs.existsSync(swPath), 'sw.js exists');
  const sw = fs.readFileSync(swPath, 'utf8');
  // The SW and the app must agree on the inbox contract, or shares vanish.
  ok(sw.indexOf("'" + R.SHARE_INBOX_CACHE + "'") >= 0, 'sw uses the same inbox cache name as the core');
  ok(sw.indexOf("'" + R.SHARE_IMAGE_KEY + "'") >= 0, 'sw uses the same image key as the core');
  ok(sw.indexOf("'" + R.SHARE_META_KEY + "'") >= 0, 'sw uses the same meta key as the core');
  ok(sw.indexOf("'" + R.SHARE_ACTION_SUFFIX + "'") >= 0, 'sw matches the same action suffix as the core');
  ok(/req\.method === 'POST'/.test(sw) && /formData\(\)/.test(sw), 'sw reads the share POST form data');
  ok(/Response\.redirect\(/.test(sw) && /303/.test(sw) && sw.indexOf('index.html?shared=1') >= 0,
     'sw redirects 303 to the app with the shared flag');
  ok(/image\//.test(sw), 'sw checks the shared payload is an image');
  // Activate cleanup must never delete the share inbox (a pending shared image).
  ok(/indexOf\('rekai-shell-'\)/.test(sw), 'sw cleanup is scoped to shell caches only');
  ok(!/https?:\/\//.test(sw), 'sw.js contains no absolute URLs (nothing external is contacted)');
}

/* ---- share intake: app wiring in index.html ---- */
{
  ok(/<link rel="manifest" href="manifest\.webmanifest">/.test(html), 'index.html links the manifest');
  ok(/serviceWorker/.test(html) && /location\.protocol !== 'http:'/.test(html),
     'service worker is registered only over http(s)');
  ok(/consumeShared/.test(html), 'app consumes the share inbox');
  ok(/SHARE_INBOX_CACHE/.test(html), 'app reads the inbox via the shared constant');
  for (const lang of R.LANGS) {
    ok(R.t(lang, 'sharedImageReady') !== 'sharedImageReady', 'sharedImageReady text in ' + lang);
    ok(R.t(lang, 'sharedFailed') !== 'sharedFailed', 'sharedFailed text in ' + lang);
  }
}

console.log('RekAI tests: ' + passed + ' passed, ' + failed + ' failed');
process.exit(failed ? 1 : 0);
