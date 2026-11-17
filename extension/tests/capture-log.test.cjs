// The capture log: every panel log line of a capture, kept beyond the panel's
// on-screen cap and sent with the upload as the `capture_log` form field.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../sidepanel/panel.js'), 'utf8');
const between = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
const logSource = between('const CAPTURE_LOG_SCHEMA', '// The elapsed counter is a separate');
const uploadSource = between('async function uploadCaptureZip(', 'el.send.addEventListener');

function panel(extra = {}) {
  const items = [];
  const list = { children: items, appendChild: (i) => items.push(i), get firstChild() { return { remove: () => items.shift() }; } };
  const context = vm.createContext({
    LOG_CAP: 800, t0: null, el: { log: list },
    document: { createElement: () => ({}) }, Date, Number, String, JSON, Blob, Math, Error,
    ...extra
  });
  vm.runInContext(`${logSource}\n;var __get=()=>captureLog, __set=(v)=>{captureLog=v}, CAP=CAPTURE_LOG_CAP, HEAD=CAPTURE_LOG_HEAD;`, context);
  return context;
}

test('nothing is collected outside a capture', () => {
  const c = panel();
  c.log('idle line');
  assert.equal(c.__get(), null);
});

test('every line is kept past the on-screen cap, with its level', () => {
  const c = panel();
  const log = c.newCaptureLog('walkthrough');
  c.__set(log);
  for (let i = 0; i < 1000; i++) c.log(`line ${i}`, i === 3 ? 'err' : undefined);
  assert.equal(log.schema, 'captcher-capture-log@1');
  assert.equal(log.kind, 'walkthrough');
  assert.equal(log.entries.length, 1000, 'the panel shows 800; the log keeps all');
  assert.equal(c.el.log.children.length, 800);
  assert.equal(log.entries[3].level, 'err');
  assert.equal(log.entries[999].text, 'line 999');
  assert.match(log.entries[0].at, /^\d{4}-\d\d-\d\dT/);
});

test('past its own cap the log keeps the head and the tail, and counts the gap', () => {
  const c = panel();
  const log = c.newCaptureLog('page');
  c.__set(log);
  const total = c.CAP + 50;
  for (let i = 0; i < total; i++) c.log(`line ${i}`);
  assert.equal(log.entries.length, c.CAP);
  assert.equal(log.omitted, 50);
  assert.equal(log.entries[0].text, 'line 0');
  assert.equal(log.entries[c.HEAD - 1].text, `line ${c.HEAD - 1}`);
  assert.equal(log.entries[c.HEAD].text, `line ${c.HEAD + 50}`);
  assert.equal(log.entries.at(-1).text, `line ${total - 1}`);
});

test('the upload carries the log as a form field, including its own upload line', async () => {
  let sent;
  const c = panel({
    serverConfig: { url: 'https://app.example', token: 'cap_test' },
    isSecureServer: () => true, maxUploadBytes: 0, uploadTimeoutFor: () => 1000,
    formatSize: (n) => `${n}B`, hostOf: (u) => u, confirm: () => true,
    zipTool: { buildZip: async () => new Blob(['zip']) },
    chrome: { runtime: { getManifest: () => ({ version: '9.9.9' }) } },
    FormData, AbortSignal,
    fetch: async (url, init) => { sent = init.body; return { ok: true, status: 201, json: async () => ({ walkthrough_id: 'w1' }) }; }
  });
  vm.runInContext(uploadSource, c);
  const log = c.newCaptureLog('page');
  c.__set(log);
  c.log('capture started');
  await c.uploadCaptureZip([], { textContent: '' }, null, log);
  const field = sent.get('capture_log');
  assert.ok(field, 'capture_log field present');
  const body = JSON.parse(await field.text());
  assert.deepEqual(body.entries.map(e => e.text.split(' —')[0]),
    ['capture started', 'upload: POST https://app.example/api/upload']);
  assert.ok(sent.get('capture_zip'), 'the capture itself is unchanged');
});

test('an upload without a log sends no field', async () => {
  let sent;
  const c = panel({
    serverConfig: { url: 'https://app.example', token: 'cap_test' },
    isSecureServer: () => true, maxUploadBytes: 0, uploadTimeoutFor: () => 1000,
    formatSize: (n) => `${n}B`, hostOf: (u) => u, confirm: () => true,
    zipTool: { buildZip: async () => new Blob(['zip']) },
    chrome: { runtime: { getManifest: () => ({ version: '9.9.9' }) } },
    FormData, AbortSignal,
    fetch: async (url, init) => { sent = init.body; return { ok: true, status: 201, json: async () => ({ walkthrough_id: 'w1' }) }; }
  });
  vm.runInContext(uploadSource, c);
  await c.uploadCaptureZip([], { textContent: '' }, null, null);
  assert.equal(sent.get('capture_log'), null);
});
