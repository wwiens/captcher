// Recorder regressions from a HubSpot recording, run against the real
// recorder.js in headless Chrome for Testing with synthetic pages only:
//
//  1. A dropdown that opens inside a same-origin iframe must reach the change
//     detector (it used to run in the top frame only, so the open list was
//     never captured).
//  2. A choice committed on mousedown, whose option is removed before the
//     click event, must still be recorded as a click on that option.
//  3. A plain click is recorded once, not twice.
//  4. A tiny hidden frame that churns constantly stays quiet.
//
// No extension is loaded: recorder.js is injected into every frame with a
// stubbed `chrome` (a port that starts recording at once, and sendMessage
// collecting into the top window). Override CHROME_TEST_BINARY for your
// installation.
const {spawn} = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), http = require('node:http');
const assert = require('node:assert/strict');

const chromeBinary = process.env.CHROME_TEST_BINARY ||
  '/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const recorderSource = fs.readFileSync(path.join(__dirname, '..', 'recorder.js'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class CDP {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.ws = new WebSocket(url);
    this.ready = new Promise((r, j) => { this.ws.addEventListener('open', r, {once: true}); this.ws.addEventListener('error', j, {once: true}); });
    this.ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (!m.id || !this.pending.has(m.id)) return;
      const {resolve, reject} = this.pending.get(m.id); this.pending.delete(m.id);
      m.error ? reject(new Error(m.error.message)) : resolve(m.result);
    });
  }
  async call(method, params = {}, sessionId) {
    await this.ready; const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {resolve, reject});
      this.ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  async eval(sessionId, expression) {
    const r = await this.call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, sessionId);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}

// Stub injected ahead of recorder.js in every frame.
const stub = `
(() => {
  const sink = (m) => { try { (window.top.__msgs ||= []).push({...m, frame: window !== window.top}); } catch {} };
  const port = {
    onDisconnect: {addListener() {}},
    onMessage: {addListener(fn) { setTimeout(() => fn({type: 'start', sessionId: 'test'}), 0); }},
    postMessage() {}, disconnect() {}
  };
  Object.defineProperty(window, 'chrome', {configurable: true, writable: true, value: {
    runtime: {connect: () => port, sendMessage: (m) => { sink(m); return Promise.resolve({auto: true, capturing: false}); }}
  }});
  self.scSensitive = {isSensitive: (el) => !!el && el.type === 'password', notePasswords() {}, REDACTED: '********'};
})();
`;

const OPTIONS = ['Appointment Scheduled', 'Qualified To Buy', 'Presentation Scheduled', 'Decision Maker Bought-In',
                 'Contract Sent', 'Closed Won', 'Closed Lost', 'On Hold', 'Re-engage', 'Disqualified'];
const pages = {
  '/': `<!doctype html><title>Deals</title><style>body{margin:0;font:16px Arial}h1{height:60px;margin:0}iframe{border:0}</style>
<h1>Deals</h1>
<iframe id="object-builder-ui" src="/form.html" style="position:absolute;left:0;top:60px;width:900px;height:640px"></iframe>
<iframe id="sync" src="/sync.html" style="position:absolute;left:0;top:0;width:1px;height:1px"></iframe>`,
  '/form.html': `<!doctype html><title>Create Deal</title><style>body{margin:0;font:16px Arial}
button{position:absolute;left:20px;top:20px;width:300px;height:40px}
#save{top:500px}
[role=listbox]{position:absolute;left:20px;top:64px;width:300px;background:#fff;border:1px solid #888}
[role=option]{height:36px;line-height:36px;padding:0 8px}</style>
<button id="stage" data-selenium-test="property-input-dealstage">Appointment Scheduled</button>
<div id="pop"></div>
<button id="save" data-selenium-test="create">Create</button>
<script>
  const OPTIONS = ${JSON.stringify(OPTIONS)};
  stage.addEventListener('click', () => {
    pop.innerHTML = '<div role="listbox">' + OPTIONS.map((o) => '<div role="option">' + o + '</div>').join('') + '</div>';
  });
  // Like HubSpot's list: the choice commits on mousedown and the list is torn down at once.
  pop.addEventListener('mousedown', (e) => {
    const o = e.target.closest('[role=option]');
    if (!o) return;
    e.preventDefault();
    stage.textContent = o.textContent;
    pop.innerHTML = '';
  });
</script>`,
  '/sync.html': `<!doctype html><title>sync</title><div id=log></div>
<script>setInterval(() => { for (let i = 0; i < 60; i++) log.appendChild(document.createElement('span')); }, 300);</script>`
};

let child, cdp, server;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-frame-press-'));
const deadline = setTimeout(() => { console.error('frame/press test exceeded 60 seconds'); process.exitCode = 1; cleanup(); }, 60000);
function cleanup() {
  clearTimeout(deadline);
  try { cdp?.ws.close(); } catch {}
  child?.kill('SIGKILL');
  server?.close();
  fs.rmSync(profile, {recursive: true, force: true});
}

(async () => {
  server = http.createServer((req, res) => {
    const body = pages[req.url.split('?')[0]];
    if (!body) { res.statusCode = 404; return res.end(); }
    res.setHeader('Content-Type', 'text/html'); res.end(body);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = 'http://127.0.0.1:' + server.address().port;

  child = spawn(chromeBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--user-data-dir=' + profile, '--remote-debugging-port=0',
    '--window-size=1000,800', 'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
  const wsUrl = await new Promise((resolve, reject) => {
    let err = '';
    child.stderr.on('data', (b) => { err += b; const m = err.match(/DevTools listening on (ws:\/\/\S+)/); if (m) resolve(m[1]); });
    child.on('exit', () => reject(new Error('Chrome exited: ' + err.slice(-500))));
  });
  cdp = new CDP(wsUrl);
  const {targetInfos} = await cdp.call('Target.getTargets');
  const pageTarget = targetInfos.find((t) => t.type === 'page');
  const {sessionId} = await cdp.call('Target.attachToTarget', {targetId: pageTarget.targetId, flatten: true});
  await cdp.call('Page.enable', {}, sessionId);
  await cdp.call('Page.addScriptToEvaluateOnNewDocument', {source: stub + '\n' + recorderSource}, sessionId);
  await cdp.call('Page.navigate', {url: origin + '/'}, sessionId);
  await sleep(2500);   // load, plus the detector's 1.5s arm delay

  const msgs = () => cdp.eval(sessionId, 'JSON.parse(JSON.stringify(window.__msgs || []))');
  const reset = () => cdp.eval(sessionId, 'window.__msgs = []; true');
  const click = async (x, y) => {
    for (const type of ['mousePressed', 'mouseReleased']) {
      await cdp.call('Input.dispatchMouseEvent', {type, x, y, button: 'left', clickCount: 1}, sessionId);
    }
  };
  const actions = (list) => list.filter((m) => m.type === 'rc-action').map((m) => m.action);

  // The frame sits at top:60px, so frame coordinates are offset by 60.
  const recorders = await cdp.eval(sessionId,
    '[!!window.__labRecorder, !!document.getElementById("object-builder-ui").contentWindow.__labRecorder]');
  assert.deepEqual(recorders, [true, true], 'recorder runs in the top page and the same-origin frame');

  // 4 (early): the hidden sync frame has been churning since load.
  await sleep(1500);
  const quiet = (await msgs()).filter((m) => m.type === 'rc-remind');
  assert.equal(quiet.length, 0, 'a 1px frame that churns never reminds: ' + JSON.stringify(quiet.map((m) => m.detail)));

  // 1. Open the dropdown inside the frame.
  await reset();
  await click(170, 60 + 40);
  await sleep(1500);   // QUIET_MS 600 + margin
  let got = await msgs();
  const reminds = got.filter((m) => m.type === 'rc-remind');
  assert.ok(reminds.length >= 1, 'opening a list inside the frame reaches the detector');
  assert.equal(reminds[0].frame, true, 'the remind came from the frame');
  assert.equal(reminds[0].detail.frame, true, 'the remind says it was in a frame');
  assert.equal(reminds[0].detail.url, origin + '/', 'the remind names the top page');
  const openClicks = actions(got).filter((a) => a.type === 'click');
  assert.equal(openClicks.length, 1, 'opening the list is one click: ' + JSON.stringify(openClicks));
  assert.ok(!openClicks[0].viaPress, 'an ordinary click is recorded by the click listener');

  // 2. Choose "Qualified To Buy" — the second option, committed on mousedown.
  await reset();
  const optionY = 60 + 64 + 36 + 18;
  await click(170, optionY);
  await sleep(800);    // PRESS_CLICK_MS 400 + margin
  got = await msgs();
  const chosen = actions(got).filter((a) => a.type === 'click');
  assert.equal(await cdp.eval(sessionId,
    'document.getElementById("object-builder-ui").contentDocument.getElementById("stage").textContent'),
    'Qualified To Buy', 'the page applied the choice');
  assert.equal(chosen.length, 1, 'the choice is recorded exactly once: ' + JSON.stringify(chosen));
  assert.equal(chosen[0].label, 'Qualified To Buy', 'the recorded click names the option');
  assert.equal(chosen[0].viaPress, true, 'recorded from the press');
  assert.ok(chosen[0].framePath && chosen[0].framePath.length, 'the click carries its frame path');
  assert.equal(chosen[0].url, origin + '/', 'the click belongs to the top page');

  // 3. A plain button click is recorded once.
  await reset();
  await click(170, 60 + 520);
  await sleep(800);
  const saves = actions(await msgs()).filter((a) => a.type === 'click');
  assert.equal(saves.length, 1, 'a plain click is recorded once: ' + JSON.stringify(saves));
  assert.ok(!saves[0].viaPress, 'and by the click listener');

  console.log('frame/press recorder cases passed');
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(cleanup);
