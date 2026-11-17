// Unused image variables: custom properties holding a data: URL that no var()
// in the snapshot names are cut from <style> elements after capture.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../sidepanel/panel.js'), 'utf8');
const between = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
const pruneSource = between('const CSS_VAR_NAME', 'function blobToDataUrl(');

const context = vm.createContext({ Date, RegExp, Set });
vm.runInContext(pruneSource, context);
const prune = (html) => context.pruneUnusedImageVars(html);

const IMG = 'data:image/png;base64,AAAA';

test('an unreferenced image variable is dropped, a referenced one kept', () => {
  const html = `<style>:root{--unused:url(${IMG});--used:url(${IMG});--color:red}.a{background:var(--used)}</style>`;
  const r = prune(html);
  assert.equal(r.dropped, 1);
  assert.equal(r.content, `<style>:root{--used:url(${IMG});--color:red}.a{background:var(--used)}</style>`);
  assert.ok(r.after < r.before);
});

test('variables without a data: URL are never touched', () => {
  const html = '<style>:root{--a:url(https://x.test/a.png);--b:12px;--c:"x"}</style>';
  assert.equal(prune(html).content, html);
});

test('a reference anywhere in the document counts — inline styles and fallbacks too', () => {
  const html = `<style>:root{--inline:url(${IMG});--fallback:url(${IMG})}</style>` +
    '<div style="background:var( --inline )"></div><style>.b{color:var(--missing, var(--fallback))}</style>';
  assert.equal(prune(html).dropped, 0);
});

test('dropping a variable orphans the --sf-img it pointed at, which goes next', () => {
  // groupDuplicateImages leaves the app's variable as an alias of the engine's.
  const html = `<style>:root{--sf-img-1:url("${IMG}")}.x{--appImg:var(--sf-img-1)}</style>` +
    `<style>.y{--keep:var(--other);--used:var(--sf-img-2);--sf-img-2:url(${IMG})}.z{b:var(--used)}</style>`;
  const r1 = prune(html);
  assert.equal(r1.dropped, 2, '--appImg, then --sf-img-1');
  assert.equal(r1.content, '<style>:root{}.x{}</style>' +
    `<style>.y{--keep:var(--other);--used:var(--sf-img-2);--sf-img-2:url(${IMG})}.z{b:var(--used)}</style>`);
  const chained = `<style>:root{--sf-img-1:url("${IMG}");--appImg:url(${IMG}),var(--sf-img-1)}</style>`;
  const r = prune(chained);
  assert.equal(r.dropped, 2);
  assert.equal(r.content, '<style>:root{}</style>');
});

test('quoted values with parentheses and semicolons are cut whole', () => {
  const svg = `data:image/svg+xml,<svg><path style='fill:url(#a);stroke:red'/></svg>`;
  const html = `<style>:root{--a:url("${svg}");--b:blue}</style>`;
  assert.equal(prune(html).content, '<style>:root{--b:blue}</style>');
});

test('the last declaration in a block, without a semicolon', () => {
  const html = `<style>@media screen{:root{--b:blue;--a:url(${IMG})}}</style>`;
  assert.equal(prune(html).content, '<style>@media screen{:root{--b:blue;}}</style>');
});

test('text outside <style> elements is left alone', () => {
  const html = `<p>{--a:url(${IMG});}</p><div style="--a:url(${IMG})"></div>`;
  assert.equal(prune(html).content, html);
});
