#!/usr/bin/env node
// Retakes the screenshots the setup guides show, from the real UI.
//
//   extension-setup/recorder-setup      panel, not connected
//   extension-setup/recorder-connected  panel, just paired
//   extension-setup/recorder-ready      panel, ready to record
//   getting-started/author-with-ai      catalog draft card
//   getting-started/try-walkthrough     player, You try
//
// Needs (see README.md in this folder): Chrome for Testing, Captcher on E2E_APP
// with at least one draft in the authoring queue (record one with
// `run.cjs <flow> --no-author`), the sample sites on E2E_SITES, and E2E_OUT.
// The player shot is taken from examples/log-a-new-deal, an export of the same
// player, so no authoring pass is needed. Every run uses a fresh browser
// profile, so the panel really is unpaired for the first shot.
//
//   node tests/e2e/setup-shots.cjs            writes into $E2E_OUT/shots/
//
// Copy them into server/captcher/static/ under a NEW filename carrying the
// month (`recorder-ready-2026-11.png`): /static/ serves PNGs as immutable for a
// year, so new bytes under an old name never reach a browser that has the old
// ones. Then update the names and width/height (half the pixel size) in
// templates/getting-started.html and templates/extension.html.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { CDP, sleep, until } = require("./cdp.cjs");

const EXT = path.resolve(process.env.E2E_EXT || path.join(__dirname, "../.."));
const REPO = path.resolve(__dirname, "../../..");
const OUT = path.join(process.env.E2E_OUT || path.join(EXT, ".e2e"), "shots");
const APP = process.env.E2E_APP || "http://127.0.0.1:8080";
const SITES = process.env.E2E_SITES || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_TEST_BINARY ||
  "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
const PANEL_WIDTH = 400;

let cdp, child;

async function launch() {
  const profile = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "captcher-shots-"));
  child = spawn(CHROME, [
    "--headless=new", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--hide-scrollbars", "--force-color-profile=srgb",
    "--user-data-dir=" + profile, "--remote-debugging-port=0",
    "--disable-extensions-except=" + EXT, "--load-extension=" + EXT,
    "--window-size=1860,1040", "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  await until(() => fs.existsSync(path.join(profile, "DevToolsActivePort")),
              { timeout: 30000, what: "Chrome debug port" });
  const [port, endpoint] = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").trim().split("\n");
  cdp = new CDP("ws://127.0.0.1:" + port + endpoint);
  await cdp.ready;
}

async function findWorker() {
  return until(async () => {
    const { targetInfos } = await cdp.call("Target.getTargets");
    for (const t of targetInfos.filter((t) => t.type === "service_worker" && t.url.endsWith("/background.js"))) {
      const s = await cdp.attach(t.targetId);
      const name = await cdp.eval(s, 'typeof chrome !== "undefined" && chrome.runtime?.getManifest().name').catch(() => null);
      if (name === "Captcher Recorder") return { session: s, extensionId: new URL(t.url).host };
    }
    return null;
  }, { timeout: 15000, what: "extension service worker" });
}

// Same route as run.cjs: a user gesture on an extension page opens the native panel.
async function openPanel(worker, extensionId, tab) {
  const help = await cdp.call("Target.createTarget", { url: `chrome-extension://${extensionId}/help.html` });
  const hs = await cdp.attach(help.targetId);
  await until(() => cdp.eval(hs, 'location.pathname === "/help.html" && document.readyState === "complete" && !!document.body'),
              { what: "help page" });
  await cdp.eval(hs, `(() => { document.body.replaceChildren();
    const b = document.createElement("button"); b.textContent = "Open recorder";
    b.style.cssText = "position:fixed;left:20px;top:20px;width:200px;height:50px";
    b.onclick = () => chrome.sidePanel.open({ windowId: ${tab.windowId} });
    document.body.append(b); })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await cdp.call("Input.dispatchMouseEvent", { type, button: "left", clickCount: 1, x: 100, y: 40 }, hs);
  }
  const panelUrl = `chrome-extension://${extensionId}/sidepanel/panel.html`;
  const panelId = await until(async () => {
    const { targetInfos } = await cdp.call("Target.getTargets");
    return targetInfos.find((t) => t.url === panelUrl)?.targetId;
  }, { what: "native side panel" });
  await cdp.call("Target.closeTarget", { targetId: help.targetId });
  const ps = await cdp.attach(panelId);
  await until(() => cdp.eval(ps, 'document.readyState === "complete" && typeof el !== "undefined"'), { what: "panel ready" });
  await cdp.eval(worker, `chrome.tabs.update(${tab.id}, { active: true })`);
  return ps;
}

// The panel at its real width, 2x, cropped just below the last thing shown.
async function shootPanel(ps, name) {
  await cdp.call("Emulation.setDeviceMetricsOverride",
                 { width: PANEL_WIDTH, height: 900, deviceScaleFactor: 2, mobile: false }, ps);
  await sleep(300);
  const bottom = await cdp.eval(ps, `(() => {
    let max = 0;
    for (const n of document.querySelectorAll("body *")) {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      if (!r.width || !r.height || s.visibility === "hidden" || s.display === "none") continue;
      if (n.closest("[hidden]")) continue;
      max = Math.max(max, r.bottom);
    }
    return Math.ceil(max);
  })()`);
  const height = Math.min(900, bottom + 24);
  const shot = await cdp.call("Page.captureScreenshot", {
    format: "png", clip: { x: 0, y: 0, width: PANEL_WIDTH, height, scale: 1 } }, ps);
  save(name, shot.data);
}

function save(name, b64) {
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(b64, "base64"));
  console.log("wrote", path.join(OUT, name));
}

async function page(url, width, height) {
  const t = await cdp.call("Target.createTarget", { url: "about:blank" });
  const s = await cdp.attach(t.targetId);
  await cdp.call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile: false }, s);
  await cdp.call("Page.enable", {}, s);
  await cdp.call("Page.navigate", { url }, s);
  await until(() => cdp.eval(s, 'document.readyState === "complete"'), { what: url });
  return { targetId: t.targetId, session: s };
}

async function main() {
  await launch();
  const worker = await findWorker();

  // 1. Not connected: the active tab is some other site.
  const tab = await cdp.eval(worker.session,
    `chrome.tabs.create({ url: ${JSON.stringify(SITES + "/")}, active: true })`);
  const ps = await openPanel(worker.session, worker.extensionId, tab);
  await until(() => cdp.eval(ps, 'connectState === "open"'), { timeout: 20000, what: "connect state open" });
  await sleep(400);
  await shootPanel(ps, "recorder-setup.png");

  // 2. Connected: open Captcher in the same tab; the panel pairs by itself and
  //    holds "Connected" briefly before moving on.
  await cdp.eval(worker.session, `chrome.tabs.update(${tab.id}, { url: ${JSON.stringify(APP + "/")} })`);
  await until(() => cdp.eval(ps, 'connectState === "done"'), { timeout: 30000, interval: 30, what: "paired" });
  await shootPanel(ps, "recorder-connected.png");

  // 3. Ready to record.
  await until(() => cdp.eval(ps, 'document.body.dataset.surface === "main" && !!document.querySelector("#surface-main") && computeState() === "idle"'),
              { timeout: 20000, what: "ready to record" }).catch(async () => {
    console.log("state:", await cdp.eval(ps, "({ s: computeState(), surface: document.body.dataset.surface })"));
    throw new Error("panel never reached the ready-to-record state");
  });
  await sleep(500);
  await shootPanel(ps, "recorder-ready.png");

  // 4. The first card in the authoring queue.
  const cat = await page(APP + "/", 1440, 900);
  await until(() => cdp.eval(cat.session, '!!document.querySelector(".queue .card, [data-queue] .card, .card")'),
              { timeout: 15000, what: "a catalog card" });
  await sleep(1200);
  const box = await cdp.eval(cat.session, `(() => {
    const btn = [...document.querySelectorAll("button")].find((b) => /Author with AI/.test(b.textContent));
    if (!btn) return null;
    const card = btn.closest("article, li, .card, [class*=card]");
    const r = card.getBoundingClientRect();
    return { x: r.x, y: r.y + scrollY, width: r.width, height: r.height };
  })()`);
  if (!box) throw new Error("no draft with an Author with AI button: record one first");
  save("author-with-ai.png", (await cdp.call("Page.captureScreenshot",
    { format: "png", captureBeyondViewport: true, clip: { ...box, scale: 1 } }, cat.session)).data);

  // 5. The player in You try, from the example export.
  const ex = await page("file://" + path.join(REPO, "examples/log-a-new-deal/index.html"), 1210, 756);
  await until(() => cdp.eval(ex.session, 'typeof dismissIntro === "function"'), { what: "example player" });
  await cdp.eval(ex.session, 'dismissIntro("try")');
  await sleep(2500);
  save("try-walkthrough.png", (await cdp.call("Page.captureScreenshot", { format: "png" }, ex.session)).data);

}

main().then(() => { child.kill(); process.exit(0); },
            (e) => { console.error(e); if (child) child.kill(); process.exit(1); });
