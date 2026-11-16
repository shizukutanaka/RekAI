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
  await js("document.getElementById('question').value = 'What is this?'; document.getElementById('askBtn').click(); true");
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
  let errShown = false;
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    if (await js("document.getElementById('status').className === 'error' && document.getElementById('status').textContent.length > 0")) { errShown = true; break; }
  }
  check('network failure shows error message', errShown);

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
  await js("document.getElementById('closeSettingsBtn').click(); true");
  await sleep(100);

  check('zero console errors', consoleErrors.length === 0, consoleErrors.join(' | ').slice(0, 300));

  const failed = results.filter(r => !r.ok).length;
  console.log('SUMMARY: ' + (results.length - failed) + '/' + results.length + ' checks passed; consoleErrors=' + consoleErrors.length);
  app.exit(failed ? 1 : 0);
}).catch(e => { console.error('VERIFY CRASH: ' + (e && e.stack || e)); app.exit(2); });
