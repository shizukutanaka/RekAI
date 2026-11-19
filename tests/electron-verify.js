'use strict';
/* RekAI Electron UI verification (not run in CI).
   Opens index.html in a real Electron window, stubs fetch with a mock
   OpenAI-compatible response (no real API is contacted), then drives:
   settings via preset -> save -> set image -> ask -> mock answer shown ->
   history entry -> screenshots (en, ja) -> network-error path.
   Run: xvfb-run -a <electron-binary> tests/electron-verify.js
   Exit code 0 only if all checks pass and there were no console errors. */
const { app, BrowserWindow } = require('electron');
const path = require('path');
const fs = require('fs');

const ROOT = path.join(__dirname, '..');
const results = [];
let consoleErrors = [];
function check(name, cond, extra) {
  if (cond === '__JS_THREW__') cond = false;
  results.push({ name, ok: !!cond, extra: extra || '' });
  console.log((cond ? 'PASS' : 'FAIL') + ' ' + name + (extra ? ' — ' + extra : ''));
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.commandLine.appendSwitch('no-sandbox');
app.commandLine.appendSwitch('disable-gpu');

app.whenReady().then(async () => {
  // No display-media handler is registered: Electron denies getDisplayMedia
  // by default then, which exercises the app's capture error path.
  const win = new BrowserWindow({ width: 1100, height: 950, show: true });
  const t0 = Date.now();
  win.webContents.on('console-message', (e, level, message, line, sourceId) => {
    console.log('CONSOLE@' + (Date.now() - t0) + 'ms level=' + level + ' ' + String(message).slice(0, 140) + ' @line=' + line + ' src=' + sourceId);
    if (level >= 3) consoleErrors.push(message);
  });
  await win.loadFile(path.join(ROOT, 'index.html'));
  await sleep(400);
  const wc = win.webContents;
  const js = async code => {
    try { return await wc.executeJavaScript(code, true); }
    catch (e) { console.log('JS-THREW: ' + String(code).slice(0, 120) + ' :: ' + e.message); return '__JS_THREW__'; }
  };

  /* Reset persisted state from earlier runs (the Electron profile is shared),
     then reload so the app starts clean. The fetch stub is installed after this. */
  await js("localStorage.removeItem('rekai.settings.v1'); localStorage.removeItem('rekai.history.v1'); localStorage.removeItem('rekai.lang.v1'); location.reload(); 'reset'");
  await sleep(700);

  check('page loaded, REKAI core present', await js('!!(window.REKAI && window.REKAI.version)'));
  check('app hook present', await js('!!window.RekAIApp'));
  check('initial answer placeholder visible', await js("document.getElementById('answer').textContent.length > 0"));

  /* --- stub fetch with a mock OpenAI-compatible response --- */
  await js(`window.__lastRequest = null;
    window.fetch = function (url, opts) {
      window.__lastRequest = { url: String(url), headers: opts.headers, body: JSON.parse(opts.body) };
      return Promise.resolve({
        ok: true, status: 200,
        text: function () { return Promise.resolve(JSON.stringify({
          choices: [{ message: { content: 'MOCK ANSWER: a small test image.' } }]
        })); }
      });
    }; true`);

  /* --- settings: choose Ollama preset, save --- */
  await js("document.getElementById('openSettingsBtn').click(); true");
  await sleep(200);
  check('settings dialog open', await js("document.getElementById('settingsDlg').open"));
  await js(`var s = document.getElementById('presetSel'); s.value = 'ollama';
    s.dispatchEvent(new Event('change', { bubbles: true })); true`);
  await sleep(100);
  {
    const ep = await js("document.getElementById('endpointInput').value");
    const md = await js("document.getElementById('modelInput').value");
    check('preset filled endpoint', ep === 'http://localhost:11434/v1', ep);
    check('preset filled model', md === 'llama3.2-vision', md);
  }
  await js("document.getElementById('saveSettingsBtn').click(); true");
  await sleep(200);
  check('dialog closed after save', await js("!document.getElementById('settingsDlg').open"));
  check('settings persisted to localStorage',
    await js("(JSON.parse(localStorage.getItem('rekai.settings.v1') || '{}').endpoint) === 'http://localhost:11434/v1'"));

  /* --- set an image and ask --- */
  const png1x1 = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
  await js('window.RekAIApp.setImageDataUrl(' + JSON.stringify(png1x1) + '); true');
  check('preview shown after setImage', await js("document.getElementById('preview').style.display === 'block'"));
  await js("window.__lastRequest = null; document.getElementById('question').value = 'What is this?'; document.getElementById('askBtn').click(); true");
  await sleep(200);
  check('confirm: dialog opens before anything is sent',
    await js("document.getElementById('confirmDlg').open && window.__lastRequest === null"));
  const confirmTxt = await js("document.getElementById('confirmList').textContent");
  check('confirm: shows the exact chat URL, local, the question and no history',
    confirmTxt.indexOf('http://localhost:11434/v1/chat/completions') >= 0 && confirmTxt.indexOf('What is this?') >= 0 &&
    confirmTxt.indexOf('llama3.2-vision') >= 0 && confirmTxt.indexOf('(local)') >= 0, confirmTxt.slice(0, 200));
  check('confirm: thumbnail is the image that will be sent',
    await js("!document.getElementById('confirmImg').hidden && document.getElementById('confirmImg').src === window.RekAIApp.state.imageDataUrl"));
  await sleep(300);
  await win.capturePage().then(img => fs.writeFileSync(path.join(ROOT, 'docs', 'screenshot-confirm.png'), img.toPNG()));
  await js("document.getElementById('confirmCancelBtn').click(); true");
  await sleep(200);
  check('confirm: cancel sends nothing and says so',
    await js("!document.getElementById('confirmDlg').open && window.__lastRequest === null && document.getElementById('status').textContent.length > 0"));
  check('confirm: thumbnail copy dropped after closing', await js("!document.getElementById('confirmImg').getAttribute('src')"));
  await js("document.getElementById('askBtn').click(); true");
  await sleep(200);
  await js("document.getElementById('confirmSendBtn').click(); true");
  let answered = false;
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    if (await js("document.getElementById('answer').textContent.indexOf('MOCK ANSWER') >= 0")) { answered = true; break; }
  }
  check('mock answer displayed', answered);
  check('request went to preset endpoint chat URL',
    await js("window.__lastRequest && window.__lastRequest.url === 'http://localhost:11434/v1/chat/completions'"));
  check('request model is the preset model',
    await js("window.__lastRequest && window.__lastRequest.body.model === 'llama3.2-vision'"));
  check('request carries the image part',
    await js(`(function(){ var m = window.__lastRequest.body.messages;
      var last = m[m.length-1]; return Array.isArray(last.content) &&
        last.content.some(function(p){ return p.type === 'image_url'; }); })()`));
  check('history entry added', await js("document.querySelectorAll('#historyList li').length === 1"));
  check('history persisted without image data',
    await js("(localStorage.getItem('rekai.history.v1') || '').indexOf('data:image') < 0"));

  /* --- screenshots: en, then ja --- */
  await sleep(700);
  await win.capturePage().then(img => fs.writeFileSync(path.join(ROOT, 'docs', 'screenshot-en.png'), img.toPNG()));
  check('screenshot en written', fs.existsSync(path.join(ROOT, 'docs', 'screenshot-en.png')));
  await js(`var l = document.getElementById('langSel'); l.value = 'ja';
    l.dispatchEvent(new Event('change', { bubbles: true })); true`);
  await sleep(300);
  check('UI switched to Japanese',
    await js("document.querySelector('[data-i18n=\"captureScreen\"]').textContent === '画面を取り込む'"));
  check('answer survives language switch',
    await js("document.getElementById('answer').textContent.indexOf('MOCK ANSWER') >= 0"));
  await win.capturePage().then(img => fs.writeFileSync(path.join(ROOT, 'docs', 'screenshot-ja.png'), img.toPNG()));
  check('screenshot ja written', fs.existsSync(path.join(ROOT, 'docs', 'screenshot-ja.png')));

  /* --- screen capture denied -> a visible reason, not silence --- */
  await js("document.getElementById('captureBtn').click(); true");
  let capMsg = '';
  for (let i = 0; i < 20; i++) {
    await sleep(250);
    capMsg = await js("document.getElementById('status').textContent");
    if (capMsg) break;
  }
  check('screen-capture failure shows a reason', capMsg.length > 0, capMsg.slice(0, 60));

  /* --- network failure path: error is shown with a reason --- */
  await js("window.fetch = function () { return Promise.reject(new Error('Failed to fetch')); }; true");
  await js("document.getElementById('askBtn').click(); true");
  await sleep(200);
  const confirmTxt2 = await js("document.getElementById('confirmList').textContent");
  check('confirm: earlier turn is listed as history context', /1 (件|turn)/.test(confirmTxt2), confirmTxt2.slice(-120));
  await js("document.getElementById('confirmSendBtn').click(); true");
  let errShown = false;
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    if (await js("document.getElementById('status').className === 'error' && document.getElementById('status').textContent.length > 0")) { errShown = true; break; }
  }
  check('network failure shows error message', errShown);

  /* --- region crop: paste a 2560x1440 "screen" -> select an area -> only it is sent, at native resolution --- */
  await js(`(function(){
    var cv = document.createElement('canvas'); cv.width = 2560; cv.height = 1440;
    var g = cv.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, 2560, 1440);
    g.fillStyle = '#000'; g.font = '12px sans-serif';
    for (var y = 20; y < 1440; y += 18) g.fillText('Error 0x' + y + ': small text that must stay readable', 10 + (y % 300), y);
    cv.toBlob(function (b) {
      var dt = new DataTransfer(); dt.items.add(new File([b], 'screen.png', { type: 'image/png' }));
      document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }));
    }, 'image/png');
    return true; })()`);
  await sleep(800);
  const dimsOf = du => js(`new Promise(function (r) { var i = new Image(); i.onload = function () { r(i.naturalWidth + 'x' + i.naturalHeight); }; i.src = ${JSON.stringify(du)}; })`);
  const fullDu = await js('window.RekAIApp.state.imageDataUrl');
  check('crop: pasted image goes through intake (full frame sent at 1280 edge)', (await dimsOf(fullDu)) === '1280x720', await dimsOf(fullDu));
  check('crop: select button enabled once an image is in', await js("!document.getElementById('cropBtn').disabled"));
  check('crop: reset disabled before cropping', await js("document.getElementById('uncropBtn').disabled"));
  await js("document.getElementById('cropBtn').click(); document.getElementById('preview').scrollIntoView({ block: 'center' }); true");
  await sleep(200);
  check('crop: crop mode shows a hint', await js("document.getElementById('preview').classList.contains('cropping') && document.getElementById('status').textContent.length > 0"));
  const r = await js("(function(){ var b = document.getElementById('preview').getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; })()");
  const at = (fx, fy) => ({ x: Math.round(r[0] + r[2] * fx), y: Math.round(r[1] + r[3] * fy) });
  const p0 = at(400 / 2560, 200 / 1440), p1 = at(1200 / 2560, 650 / 1440);
  wc.sendInputEvent({ type: 'mouseDown', x: p0.x, y: p0.y, button: 'left', clickCount: 1 });
  for (let k = 1; k <= 5; k++) {
    wc.sendInputEvent({ type: 'mouseMove', x: Math.round(p0.x + (p1.x - p0.x) * k / 5), y: Math.round(p0.y + (p1.y - p0.y) * k / 5), button: 'left' });
    await sleep(30);
  }
  wc.sendInputEvent({ type: 'mouseUp', x: p1.x, y: p1.y, button: 'left', clickCount: 1 });
  await sleep(400);
  const view = await js('JSON.stringify(window.RekAIApp.state.view)');
  const v = JSON.parse(view || 'null') || {};
  check('crop: selection mapped to source px (~800x450 at 400,200)',
    Math.abs(v.x - 400) <= 8 && Math.abs(v.y - 200) <= 8 && Math.abs(v.w - 800) <= 12 && Math.abs(v.h - 450) <= 12, view);
  const cropDu = await js('window.RekAIApp.state.imageDataUrl');
  const cropDims = await dimsOf(cropDu);
  check('crop: region kept at native resolution (not halved)', cropDims === v.w + 'x' + v.h, cropDims);
  console.log('MEASURE full-frame jpeg bytes=' + Math.round(fullDu.length * 3 / 4) + ' (1280x720, scale 0.5)  cropped jpeg bytes=' + Math.round(cropDu.length * 3 / 4) + ' (' + cropDims + ', scale 1.0)');
  check('crop: reset enabled after cropping', await js("!document.getElementById('uncropBtn').disabled"));
  await js(`window.fetch = function (url, opts) {
    window.__lastRequest = { url: String(url), body: JSON.parse(opts.body) };
    return Promise.resolve({ ok: true, status: 200, text: function () { return Promise.resolve(JSON.stringify({ choices: [{ message: { content: 'CROP ANSWER' } }] })); } });
  }; document.getElementById('askBtn').click(); true`);
  await sleep(200);
  check('crop: confirmation shows the cropped image and its real size',
    await js("document.getElementById('confirmImg').src === window.RekAIApp.state.imageDataUrl && document.getElementById('confirmList').textContent.indexOf('" + cropDims.replace('x', ' × ') + " px') >= 0"));
  await js("document.getElementById('confirmSendBtn').click(); true");
  await sleep(500);
  check('crop: the request carries only the cropped image',
    await js("(function(){ var m = window.__lastRequest.body.messages; var c = m[m.length-1].content; return c[1].image_url.url === window.RekAIApp.state.imageDataUrl; })()"));
  check('crop: full frame never persisted', await js("JSON.stringify(localStorage).indexOf('data:image') < 0"));
  await js("document.getElementById('uncropBtn').click(); true");
  check('crop: reset restores the whole frame', (await dimsOf(await js('window.RekAIApp.state.imageDataUrl'))) === '1280x720');
  await js("document.getElementById('clearImageBtn').click(); true");
  check('crop: removing the image drops the in-memory frame', await js("window.RekAIApp.state.source === null && document.getElementById('cropBtn').disabled"));

  /* --- confirmation can be turned off in Settings; then a click sends directly --- */
  await js("document.getElementById('openSettingsBtn').click(); true");
  await sleep(150);
  check('confirm: setting is on by default', await js("document.getElementById('confirmInput').checked"));
  await js("document.getElementById('confirmInput').checked = false; document.getElementById('saveSettingsBtn').click(); true");
  await sleep(150);
  check('confirm: opt-out persisted', await js("JSON.parse(localStorage.getItem('rekai.settings.v1')).confirmBeforeSend === false"));
  await js("window.__lastRequest = null; document.getElementById('askBtn').click(); true");
  await sleep(300);
  check('confirm: off -> sent without the dialog', await js("!document.getElementById('confirmDlg').open && window.__lastRequest !== null"));
  await js("document.getElementById('openSettingsBtn').click(); true");
  await sleep(150);
  check('confirm: opt-out shown when reopening Settings', await js("document.getElementById('confirmInput').checked === false"));
  await js("document.getElementById('confirmInput').checked = true; document.getElementById('saveSettingsBtn').click(); true");
  await sleep(150);

  /* --- auto-detection: Ollama answers /models -> found -> confirm -> saved --- */
  await js("localStorage.removeItem('rekai.settings.v1'); true");
  await js(`window.fetch = function (url) {
    if (String(url) === 'http://localhost:11434/v1/models') {
      return Promise.resolve({ ok: true, status: 200, text: function () {
        return Promise.resolve(JSON.stringify({ data: [{ id: 'some-text-model' }, { id: 'llama3.2-vision' }] }));
      }});
    }
    return Promise.reject(new Error('Failed to fetch'));
  }; true`);
  await js("document.getElementById('openSettingsBtn').click(); true");
  await sleep(200);
  await js("document.getElementById('detectBtn').click(); true");
  let foundTxt = '';
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    foundTxt = await js("document.getElementById('detectResult').textContent");
    if (foundTxt.indexOf('Ollama') >= 0 && foundTxt.indexOf('llama3.2-vision') >= 0) break;
  }
  check('detection found the running engine', foundTxt.indexOf('Ollama') >= 0, foundTxt.slice(0, 80));
  check('detection picked the vision model', foundTxt.indexOf('llama3.2-vision') >= 0, foundTxt.slice(0, 120));
  check('nothing saved before the user confirms',
    await js("localStorage.getItem('rekai.settings.v1') === null"));
  await js("document.querySelector('#detectResult button').click(); true");
  await sleep(200);
  check('confirming closes the dialog', await js("!document.getElementById('settingsDlg').open"));
  check('detected settings persisted',
    await js("(function(){ var s = JSON.parse(localStorage.getItem('rekai.settings.v1') || '{}'); return s.endpoint === 'http://localhost:11434/v1' && s.model === 'llama3.2-vision' && s.preset === 'ollama'; })()"));

  /* --- auto-detection: nothing running -> reasons + setup guidance --- */
  await js("window.fetch = function () { return Promise.reject(new Error('Failed to fetch')); }; true");
  await js("document.getElementById('openSettingsBtn').click(); true");
  await sleep(200);
  await js("document.getElementById('detectBtn').click(); true");
  let noneTxt = '';
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    noneTxt = await js("document.getElementById('detectResult').textContent");
    if (noneTxt.indexOf('ollama.com') >= 0) break;
  }
  check('not-found shows setup guidance', noneTxt.indexOf('ollama.com') >= 0 && noneTxt.indexOf('lmstudio.ai') >= 0, noneTxt.slice(0, 80));
  check('setup guidance: copyable commands for this page (file: no OLLAMA_ORIGINS)',
    noneTxt.indexOf('ollama pull llama3.2-vision') >= 0 && noneTxt.indexOf('lms server start --cors') >= 0 && noneTxt.indexOf('OLLAMA_ORIGINS') < 0, noneTxt.slice(-200));
  await js("window.__copied = null; navigator.clipboard.writeText = function (v) { window.__copied = v; return Promise.resolve(); }; document.querySelector('#detectResult code.cmd + button').click(); true");
  await sleep(150);
  check('setup guidance: copy button copies the exact command',
    await js("window.__copied === 'ollama pull llama3.2-vision' && document.querySelector('#detectResult code.cmd + button').textContent !== ''"));
  await js("document.getElementById('closeSettingsBtn').click(); true");
  await sleep(100);

  /* --- history export / import: the user's own file, text only --- */
  {
    await js(`window.__dl = null; window.__origCreate = URL.createObjectURL; window.__origClick = HTMLAnchorElement.prototype.click;
      URL.createObjectURL = function (b) { window.__dlBlob = b; return 'blob:test'; };
      HTMLAnchorElement.prototype.click = function () { window.__dl = { name: this.download, href: this.getAttribute('href') }; };
      localStorage.setItem('rekai.history.v1', JSON.stringify([{ q: 'old q', a: 'old a', ts: 1000 }]));
      true`);
    await js("location.reload(); true");
    await sleep(800);
    await js(`URL.createObjectURL = function (b) { window.__dlBlob = b; return 'blob:test'; };
      HTMLAnchorElement.prototype.click = function () { window.__dl = { name: this.download }; }; true`);
    await js("document.getElementById('exportHistoryBtn').click(); true");
    await sleep(200);
    const dl = JSON.parse(await js("JSON.stringify(window.__dl)") || 'null') || {};
    const exported = await js("window.__dlBlob ? window.__dlBlob.text() : ''");
    let ex = {}; try { ex = JSON.parse(exported); } catch (e) {}
    check('history export: downloads a dated json file', /^rekai-history-\d{4}-\d{2}-\d{2}\.json$/.test(dl.name || ''), dl.name);
    check('history export: file holds the stored history, text only',
      ex.format === 'rekai-history' && Array.isArray(ex.entries) && ex.entries.length === 1 && ex.entries[0].q === 'old q' && exported.indexOf('data:image') < 0);
    const importFile = async (text) => js(`(function(){ var dt = new DataTransfer();
      dt.items.add(new File([${JSON.stringify(text)}], 'h.json', { type: 'application/json' }));
      var inp = document.getElementById('historyFileInput'); inp.files = dt.files;
      inp.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
    await js("localStorage.removeItem('rekai.history.v1'); document.getElementById('clearHistoryBtn').click(); true");
    await importFile(exported);
    await sleep(400);
    check('history import: restores the exported file after clearing',
      await js("document.querySelectorAll('#historyList li').length === 1 && document.getElementById('historyList').textContent.indexOf('old q') >= 0"));
    check('history import: persisted', await js("JSON.parse(localStorage.getItem('rekai.history.v1')).length === 1"));
    await importFile(JSON.stringify({ format: 'rekai-history', version: 1, entries: [{ q: 'old q', a: 'old a', ts: 1000 }, { q: 'new q', a: 'new a', ts: 2000 }] }));
    await sleep(400);
    check('history import: merges without duplicates',
      await js("document.querySelectorAll('#historyList li').length === 2 && document.getElementById('status').textContent.indexOf('1') >= 0"));
    await importFile('not json at all');
    await sleep(400);
    check('history import: bad file shows a reason and changes nothing',
      await js("document.getElementById('status').className === 'error' && document.querySelectorAll('#historyList li').length === 2"));
    await js("URL.createObjectURL = window.__origCreate || URL.createObjectURL; true");
  }

  /* --- share intake: serve over HTTP, install the SW, then post a real
         multipart form (image + text) to the share target, exactly like an
         OS share sheet would. The service worker must stash the payload,
         redirect to the app, and the app must consume it once. --- */
  {
    const http = require('http');
    const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.webmanifest': 'application/manifest+json', '.png': 'image/png', '.md': 'text/plain' };
    const server = http.createServer((req, res) => {
      try {
        let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
        if (p === '/') p = '/index.html';
        const file = path.join(ROOT, p);
        if (file.indexOf(ROOT) !== 0 || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
          res.writeHead(404); res.end('not found'); return;
        }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
        res.end(fs.readFileSync(file));
      } catch (e) { res.writeHead(500); res.end(String(e)); }
    });
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    const base = 'http://127.0.0.1:' + server.address().port;
    const win2 = new BrowserWindow({ width: 900, height: 900, show: true });
    win2.webContents.on('console-message', (e, level, message) => { if (level >= 3) consoleErrors.push('[share] ' + message); });
    const js2 = async code => {
      try { return await win2.webContents.executeJavaScript(code, true); }
      catch (e) { console.log('JS2-THREW: ' + String(code).slice(0, 120) + ' :: ' + e.message); return '__JS_THREW__'; }
    };
    await win2.loadURL(base + '/index.html');
    let swReady = false;
    for (let i = 0; i < 40 && !swReady; i++) {
      await sleep(250);
      swReady = await js2("navigator.serviceWorker.ready.then(function(){ return true; }).catch(function(){ return false; })");
      if (swReady) {
        const active = await js2("navigator.serviceWorker.getRegistration().then(function(r){ return !!(r && r.active); })");
        if (!active) swReady = false;
      }
    }
    check('service worker registered and active over HTTP', swReady === true);
    await win2.loadURL(base + '/index.html'); // reload so the SW controls the page
    await sleep(500);
    check('service worker controls the page', await js2('!!navigator.serviceWorker.controller'));
    check('manifest exposes a POST share target',
      await js2("fetch('manifest.webmanifest').then(function(r){ return r.json(); }).then(function(m){ return !!(m.share_target && m.share_target.method === 'POST' && m.share_target.params && m.share_target.params.files); }).catch(function(){ return false; })"));

    await js2(`(async function () {
      var resp = await fetch(${JSON.stringify('data:image/png;base64,' + Buffer.from(fs.readFileSync(path.join(ROOT, 'icon-192.png'))).toString('base64'))});
      var blob = await resp.blob();
      var file = new File([blob], 'shared-photo.png', { type: 'image/png' });
      var dt = new DataTransfer(); dt.items.add(file);
      var form = document.createElement('form');
      form.method = 'POST'; form.enctype = 'multipart/form-data'; form.action = './share-target';
      var fi = document.createElement('input'); fi.type = 'file'; fi.name = 'image'; fi.files = dt.files;
      var ti = document.createElement('input'); ti.type = 'text'; ti.name = 'text'; ti.value = 'Shared question text';
      form.appendChild(fi); form.appendChild(ti);
      document.body.appendChild(form);
      form.submit();
      return true;
    })()`);
    let sharedIn = false;
    for (let i = 0; i < 40 && !sharedIn; i++) {
      await sleep(250);
      sharedIn = await js2("document.getElementById('preview') && document.getElementById('preview').style.display === 'block'");
    }
    check('shared image arrived in the preview', sharedIn === true);
    check('shared image went through the normal intake (re-encoded jpeg)',
      await js2("(window.RekAIApp.state.imageDataUrl || '').indexOf('data:image/jpeg') === 0"));
    check('shared text prefilled the question box',
      await js2("document.getElementById('question').value.indexOf('Shared question text') >= 0"));
    check('share status message shown',
      await js2("document.getElementById('status').className === 'info' && document.getElementById('status').textContent.length > 0"));
    check('share flag cleaned from the address bar',
      await js2("location.search.indexOf('shared') < 0"));
    check('share inbox consumed (nothing lingers on the device)',
      await js2("caches.open('rekai-share-inbox').then(function(c){ return c.keys(); }).then(function(k){ return k.length === 0; }).catch(function(){ return false; })"));
    check('shared image is not persisted to localStorage',
      await js2("JSON.stringify(localStorage).indexOf('data:image') < 0"));
    win2.close();
    server.close();
  }

  check('zero console errors', consoleErrors.length === 0, consoleErrors.join(' | ').slice(0, 300));

  const failed = results.filter(r => !r.ok).length;
  console.log('SUMMARY: ' + (results.length - failed) + '/' + results.length + ' checks passed; consoleErrors=' + consoleErrors.length);
  app.exit(failed ? 1 : 0);
}).catch(e => { console.error('VERIFY CRASH: ' + (e && e.stack || e)); app.exit(2); });
