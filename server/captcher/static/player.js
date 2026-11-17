/* player.js — walkthrough player state machine.
 * Modes: "show" (animated demonstration) and "try" (learner performs actions).
 * Talks to bridge.js inside the capture iframe via postMessage.
 */
"use strict";

/* Standalone export support: export.py bakes the walkthrough definition and all
 * captured pages into a JSON block with id "sc-embed". When present, the
 * player runs fully offline — no /api or /capture requests, pages load via
 * iframe srcdoc, and authoring UI (edit mode, catalog links) is hidden.
 * EMBED.chrome is "full" (default) or "viewport" — the latter strips all
 * player chrome for bare inline embeds; see body.sc-viewport-only below and
 * finishWalkthrough(). */
const EMBED = (() => {
  const el = document.getElementById("sc-embed");
  try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
})();

const walkthroughId = EMBED
  ? (EMBED.walkthrough.id || "walkthrough")
  : decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());
const $ = (id) => document.getElementById(id);
const sim = $("sim");

let walkthrough = null;
let mode = "show";          // "show" | "try" | "edit"
let stepIdx = 0;
let currentPage = null;      // capture-relative path currently in the iframe
let bridgeReady = false;
let pendingCmd = null;       // command waiting for bridge-ready
let wrongCount = 0;          // wrong attempts on current step
let totalWrong = 0;
let startedAt = null;

/* show-me start pacing: give the learner time to read the step text before
 * the demonstration begins */
let pendingShowme = null;    // the step whose demo is waiting to start
let skipStartPause = false;  // one-shot: Replay starts immediately

/* ---- button countdowns ----
 * One helper drives every timed action (Start now, Continue, Finish): the
 * button shows "label · Ns", ticks down, fires onDone at zero. Clicking the
 * button runs its normal onclick, which cancels the countdown — so a click
 * always means "do it now". */
const countdowns = new Map();   // button el -> { tick, timer }

function buttonCountdown(btn, label, ms, onDone) {
  cancelCountdown(btn);
  let remaining = Math.max(1, Math.ceil(ms / 1000));
  btn.textContent = `${label} · ${remaining}s`;
  btn.title = "Click to go now";
  btn.classList.remove("hidden");
  btn.classList.add("counting");
  const tick = setInterval(() => {
    remaining--;
    if (remaining > 0) btn.textContent = `${label} · ${remaining}s`;
  }, 1000);
  const timer = setTimeout(() => { cancelCountdown(btn); onDone(); }, ms);
  countdowns.set(btn, { tick, timer });
}

function cancelCountdown(btn) {
  const c = countdowns.get(btn);
  if (c) { clearInterval(c.tick); clearTimeout(c.timer); countdowns.delete(btn); }
  btn.classList.remove("counting");
  btn.removeAttribute("title");
}

/* playback settings (persisted across walkthroughs)
 * localStorage can throw entirely (sandboxed iframes, opaque origins — e.g.
 * an exported walkthrough embedded in an LMS). Settings then simply don't persist. */
const storeGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const storeSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* no persistence */ } };

/* Playback preferences, per browser. The "sc-" prefix replaced "rc-" at an
 * earlier rename with no fallback read: these are four playback
 * settings a learner re-picks in seconds, and carrying a second key forever to
 * save them one click is not a trade worth making. The catalog's collapse key
 * IS carried forward — see templates/_catalog-script.html for why the two
 * decisions differ. */
const settings = {
  speed: parseFloat(storeGet("sc-speed")) || 1,
  autoAdvance: storeGet("sc-auto") !== "0",
  startDelay: storeGet("sc-start-delay") || "auto", // "auto" | seconds
  nextDelay: parseFloat(storeGet("sc-next-delay")) || 2, // seconds
};

function initSettings() {
  const speedSel = $("opt-speed");
  const autoChk = $("opt-auto");
  const startSel = $("opt-start-delay");
  const nextSel = $("opt-next-delay");
  speedSel.value = String(settings.speed);
  if (!speedSel.value) { speedSel.value = "1"; settings.speed = 1; } // stale stored value
  autoChk.checked = settings.autoAdvance;
  startSel.value = settings.startDelay;
  if (!startSel.value) { startSel.value = "auto"; settings.startDelay = "auto"; }
  nextSel.value = String(settings.nextDelay);
  if (!nextSel.value) { nextSel.value = "2"; settings.nextDelay = 2; }

  startSel.addEventListener("change", () => {
    settings.startDelay = startSel.value;
    storeSet("sc-start-delay", settings.startDelay);
    if (pendingShowme) {
      // a demo is waiting to start — restage under the new wait
      const step = pendingShowme;
      clearStartPause();
      beginShowme(step);
    }
  });
  nextSel.addEventListener("change", () => {
    settings.nextDelay = parseFloat(nextSel.value) || 2;
    storeSet("sc-next-delay", String(settings.nextDelay));
    if (countdowns.has($("btn-next"))) scheduleAdvance(); // restart the running countdown
  });

  speedSel.addEventListener("change", () => {
    settings.speed = parseFloat(speedSel.value) || 1;
    storeSet("sc-speed", String(settings.speed));
    // every fresh page gets the speed on bridge-ready; only an already-live
    // bridge needs telling (don't queue via sendToBridge — it would clobber
    // a showme/expect waiting for the page to load)
    if (bridgeReady) sim.contentWindow.postMessage(bridgeConfig(), "*");
  });
  autoChk.addEventListener("change", () => {
    settings.autoAdvance = autoChk.checked;
    storeSet("sc-auto", autoChk.checked ? "1" : "0");
    if (!settings.autoAdvance && countdowns.has($("btn-next"))) {
      // an advance was already counting down — hand control back to the user
      cancelCountdown($("btn-next"));
      showContinueButton();
    }
    if (pendingShowme) {
      // a demo is waiting to start — restage it under the new policy
      // (countdown when auto, ▶ Play when manual)
      const step = pendingShowme;
      clearStartPause();
      beginShowme(step);
    }
    syncSettingsVisibility(); // wait fields dim when auto-advance is off
  });

  // Not a viewer preference: the shape belongs to the walkthrough. In edit
  // mode this writes it and marks the editor dirty; in playback it is only a
  // session override, so a viewer can try the other shape without changing
  // anything for anyone else. Either way it is pushed to the live page at once
  // so the choice can be judged against the real captured UI, not the label.
  $("opt-cursor").addEventListener("change", () => {
    const value = $("opt-cursor").value;
    if (mode === "edit") {
      walkthrough.cursor = value;
      cursorOverride = null;   // what you see is now what will be saved
      markDirty();
    } else {
      cursorOverride = value;
    }
    if (bridgeReady) sim.contentWindow.postMessage(bridgeConfig(), "*");
  });
}

/* editor state */
let snapshots = [];          // capture snapshot paths (valid `page` values)
let dirty = false;           // unsaved editor changes
let probeTimer = null;

/* ================= boot ================= */
// The walkthrough now starts immediately in the player chrome; the intro
// (scenario/objectives) is a popup over it — see openIntroModal()/dismissIntro().
function afterLoad() {
  renderIntro(); initEditor(); initAiPanel(); initSettings();
  const viewportOnly = EMBED && EMBED.chrome === "viewport";
  // A host wrapper (e.g. static/scorm-api.js) that runs before this script
  // can set window.RC_RESUME = {mode, stepIdx} to resume a prior session —
  // read once, here, at boot.
  const resume = window.RC_RESUME;
  // catalog card "Edit" menu item links here with ?edit=1 to jump straight
  // into edit mode, skipping the intro popup — meaningless for exports/embeds
  const autoEdit = !EMBED && new URLSearchParams(location.search).get("edit") === "1";
  if (resume && resume.mode && Number.isInteger(resume.stepIdx)) {
    // resuming a prior session: skip the intro popup entirely
    setTimeout(() => startWalkthrough(resume.mode, resume.stepIdx), 0);
  } else if ((EMBED && EMBED.skip_intro) || viewportOnly) {
    // exported without the intro popup — start straight in the walkthrough.
    // Viewport-chrome embeds always skip it (it's chrome too) and only ever
    // run the "Show me" demo — there's no panel left to drive "You try".
    setTimeout(() => startWalkthrough("show"), 0);
  } else if (autoEdit) {
    setTimeout(() => startWalkthrough("edit"), 0);
  } else {
    openIntroModal();
  }
  // Deferred via setTimeout above: in the EMBED path this all runs
  // synchronously at script-load time, before later top-level bindings
  // (bridgeWaiter, the message listener, and related state) are declared —
  // starting mid-script would hit a temporal-dead-zone ReferenceError.
}

if (EMBED) {
  document.body.classList.add("sc-embedded");
  if (EMBED.chrome === "viewport") document.body.classList.add("sc-viewport-only");
  walkthrough = EMBED.walkthrough;
  snapshots = Object.keys(EMBED.pages || {});
  // The standalone export runs this branch while the script is still being
  // evaluated. Defer boot until every later top-level `let`/`const` binding
  // (beatOf, bridge state, etc.) has been initialized.
  setTimeout(afterLoad, 0);
} else {
  Promise.all([
    fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}`)
      .then((r) => { if (!r.ok) throw new Error("Walkthrough not found"); return r.json(); }),
    fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}/pages`)
      .then((r) => (r.ok ? r.json() : [])),
  ])
    .then(([data, pages]) => { walkthrough = data; snapshots = pages; afterLoad(); })
    .catch((e) => { document.body.innerHTML = `<p style="padding:40px">Could not load walkthrough: ${e.message}</p>`; });
}

window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

// Step counts are machine strings, so they are set in mono and zero-padded:
// "STEP 03 / 07", never "Step 3 of 7". Padding is what keeps the readout from
// changing width as it advances — the mono faces already ship uniform-width
// digits, and this stops the field itself growing a character at step 10.
const pad2 = (n) => String(n).padStart(2, "0");

function renderIntro() {
  document.title = walkthrough.title;
  $("intro-track").textContent = `${walkthrough.track || "walkthrough"} · ${walkthrough.level || ""}`;
  $("intro-title").textContent = walkthrough.title;
  $("intro-scenario").textContent = walkthrough.intro?.scenario || "";
  $("intro-objectives").innerHTML = (walkthrough.intro?.objectives || []).map((o) => `<li>${esc(o)}</li>`).join("");
  $("tb-title").textContent = walkthrough.title;
  computeBeats();
  renderStepList();
}

// Play modes list one entry per beat: a merged run is one thing the learner is
// doing, so three fields must not read as three items on the rail. Edit mode
// lists every step — each field has its own hint and demo value, and this is
// the only way to reach them. `data-step` carries the real index either way.
function renderStepList() {
  const full = mode === "edit";
  $("step-list").innerHTML = walkthrough.steps
    .map((s, i) => (full || !continuesRun(i) ? `<li data-step="${i}">${esc(s.title)}</li>` : ""))
    .join("");
}

/* ================= walkthrough flow ================= */
function startWalkthrough(m, startIndex) {
  if (EMBED && m === "edit") return; // exported walkthroughs are playback-only
  mode = m;
  renderStepList();  // ?edit=1 and SCORM resume both land here, not in switchMode
  stepIdx = 0;
  totalWrong = 0;
  startedAt = Date.now();
  currentPage = null; // force fresh page load (resets any typed state)
  resetWindowView();
  show("player");
  syncModeButtons();
  // loadPage assigns fresh srcdoc in an export and reassigns the capture URL
  // in the live player, so a separate about:blank navigation is neither
  // needed for reset nor safe: it can win the navigation race and stay blank.
  gotoStep(Number.isInteger(startIndex) ? startIndex : 0);
}

function switchMode(m) {
  if (m === mode) return;
  if (EMBED && m === "edit") return; // exported walkthroughs are playback-only
  if (mode === "edit") sendToBridge({ cmd: "clear" });   // leave picker/highlights
  if (m !== "edit") closeAiPanel();  // the AI panel belongs to the editor
  // A playback experiment must not be mistaken for the saved value once the
  // editor is showing — otherwise Save would quietly write the other shape.
  if (m === "edit") cursorOverride = null;
  mode = m;
  syncModeButtons();
  renderStepList();  // the rail collapses merged runs in play modes, not in edit
  currentPage = null;
  resetWindowView(); // edit mode is always full page; play modes re-resolve on load
  gotoStep(stepIdx); // redo current step in the new mode
}

function effectivePage(i) {
  // the page the learner is on at step i (steps with page:null continue
  // on the previous page) — needed when jumping between steps in the editor
  for (let k = i; k >= 0; k--) if (walkthrough.steps[k].page) return walkthrough.steps[k].page;
  return null;
}

/* ================= merged runs =================
 * A step marked `continues: true` runs on from the one before it with no pause
 * between them: the three fields of an address are one thing to teach, not
 * three. The flag sits on the 2nd..Nth step of a run, so a run is always
 * contiguous and an absent flag is exactly the old behaviour.
 *
 * Steps stay steps. Each field keeps its own hint, min_length, demo_value and
 * wrong_value_feedback, which is the whole reason this is a marker rather than
 * one step holding a list of actions. What a run changes is pacing and
 * counting, not structure.
 *
 * author.validate() rejects the flag on the first step and on any step that
 * loads a page, so a run can never straddle two snapshots — nothing here has
 * to defend against that.
 */
function continuesRun(i) {
  return i > 0 && !!(walkthrough.steps[i] || {}).continues;
}

function runStartOf(i) {
  while (continuesRun(i)) i--;
  return i;
}

// Beat numbering: a run counts once, so the learner's counter, rail and
// progress fill all read "one thing to do" where the steps array has three.
// Derived on load and after an edit, never stored.
let beatOf = [];       // step index -> 0-based beat index
let beatCount = 0;

function computeBeats() {
  beatOf = [];
  let b = -1;
  walkthrough.steps.forEach((s, i) => { if (!continuesRun(i)) b++; beatOf[i] = b; });
  beatCount = b + 1;
}

function gotoStep(i) {
  clearAdvance();
  clearStartPause();
  pendingShowme = null;
  if (i >= walkthrough.steps.length) {
    if (mode === "edit") i = walkthrough.steps.length - 1;
    else return finishWalkthrough();
  }
  stepIdx = i;
  wrongCount = 0;
  // A pick is armed against the page in the frame right now. Moving steps can
  // swap that page out from under it, and the fresh bridge comes up unarmed.
  aiCancelPick();
  renderStepPanel();
  // generic instrumentation hook for a host wrapper (e.g. static/scorm-api.js)
  // to track progress — no listeners in the plain player/embed cases
  window.dispatchEvent(new CustomEvent("captcher:step", { detail: { index: i, total: walkthrough.steps.length, mode } }));

  const step = walkthrough.steps[i];
  const page = mode === "edit" ? effectivePage(i) : step.page;
  if (page && page !== currentPage) {
    loadPage(page, () => actOnStep(step));
  } else {
    actOnStep(step);
  }
}

function actOnStep(step) {
  // A step inside a run never takes the reading pause: the learner read the
  // run's instruction at its first step, and re-pausing per field is exactly
  // the stutter merging is meant to remove.
  const skipPause = skipStartPause || continuesRun(stepIdx);
  skipStartPause = false;
  if (mode === "edit") {
    if (step.action.type === "observe") sendToBridge({ cmd: "clear" });
    else probeTarget();
    return;
  }
  if (step.action.type === "observe") {
    // nothing to perform — learner reads the page and continues
    showContinueButton();
    return;
  }
  if (mode === "show") {
    beginShowme(step, skipPause);
  } else {
    sendToBridge({ cmd: "expect", action: step.action });
  }
}

/* ---- show-me start pacing ---- */
// With auto-advance ON: start after a reading pause sized to the step text
// (skippable via the countdown button). With auto-advance OFF: never start
// on our own — wait for an explicit ▶ Play click.
function beginShowme(step, skipPause) {
  pendingShowme = step;
  const btn = $("btn-play");

  if (skipPause) { // Replay: the learner has read it — go now
    return startShowmeNow();
  }

  if (!settings.autoAdvance) {
    btn.textContent = "▶ Play step";
    btn.classList.remove("hidden");
    return;
  }

  const delay = startDelayMs(step);
  if (delay <= 0) return startShowmeNow(); // "None" — no pause at all
  buttonCountdown(btn, "▶ Start now", delay, startShowmeNow);
}

// user-fixed seconds, or Auto: ~300 wpm skim rate + a beat to orient, clamped.
// Fast playback shortens the Auto pause; slow playback does NOT stretch it
// (reading speed ≠ demo speed). Fixed waits are taken literally.
function startDelayMs(step) {
  if (settings.startDelay !== "auto") {
    return Math.max(0, parseFloat(settings.startDelay) || 0) * 1000;
  }
  const text = `${step.title} ${step.showme_caption || step.instruction || ""}`;
  const words = text.trim().split(/\s+/).length;
  return Math.max(1000, Math.min(6000, 600 + words * 200) / Math.max(1, settings.speed));
}

function startShowmeNow() {
  const step = pendingShowme;
  clearStartPause();
  if (!step) return;
  pendingShowme = null;
  sendToBridge({ cmd: "showme", action: step.action });
}

function clearStartPause() {
  const btn = $("btn-play");
  cancelCountdown(btn);
  btn.classList.add("hidden");
}

function clearAdvance() {
  const btn = $("btn-next");
  cancelCountdown(btn);
  btn.classList.add("hidden");
}

/* auto-advance with a visible, skippable countdown on the Continue button —
 * wait length comes from the user's Continue-wait setting */
function scheduleAdvance() {
  const last = stepIdx === walkthrough.steps.length - 1;
  buttonCountdown($("btn-next"), last ? "Finish walkthrough" : "Continue →", settings.nextDelay * 1000, nextStep);
}

function nextStep() { gotoStep(stepIdx + 1); }

function replayStep() {
  const step = walkthrough.steps[stepIdx];
  if (step.page) { currentPage = null; }
  skipStartPause = true; // they've read the text — replay immediately
  gotoStep(stepIdx);
}

function giveHint() {
  const step = walkthrough.steps[stepIdx];
  sendToBridge({ cmd: "hint", action: step.action });
  if (step.hint) feedback(step.hint, "hint");
}

function finishWalkthrough() {
  // fires before any early return below (e.g. an LMS-hosted iframe hits the
  // window.parent !== window branch) so a host wrapper always sees completion
  window.dispatchEvent(new CustomEvent("captcher:complete"));
  // The Help widget owns its iframe and closes it on completion. Dedicated
  // example pages and viewport previews opt out with ?host=page: the former
  // needs the completion screen, and the latter needs to loop.
  const pageHosted = new URLSearchParams(location.search).get("host") === "page";
  if (EMBED && window.parent !== window && !pageHosted) {
    // Running inside a host page's Help modal (static/embed.js): the learner
    // opened this on demand and just finished it, so close the dialog for
    // them instead of leaving a "walkthrough complete" card to dismiss.
    try { window.parent.postMessage({ type: "sc-embed-close" }, "*"); } catch (e) { /* cross-origin-strict host: ignore */ }
    return;
  }
  if (EMBED && EMBED.chrome === "viewport") {
    // No chrome to show a completion card in, and no host modal to close
    // (e.g. /embed/<id> opened directly, outside the Help widget) — loop
    // the demonstration instead, like a silent, self-running preview.
    setTimeout(() => startWalkthrough("show"), 1600);
    return;
  }
  const mins = Math.max(1, Math.round((Date.now() - startedAt) / 60000));
  $("complete-summary").textContent = walkthrough.completion?.summary || "";
  $("complete-criteria").innerHTML = (walkthrough.completion?.success_criteria || []).map((c) => `<li>${esc(c)}</li>`).join("");
  $("complete-stats").textContent =
    mode === "try"
      ? `Completed in ${mins} min · ${totalWrong === 0 ? "no missed clicks — a clean run." : totalWrong + " missed attempt(s)"}`
      : `Demonstration finished in ${mins} min — now try it yourself.`;
  show("complete");
}

/* ================= iframe / bridge ================= */
function loadPage(page, onReady) {
  bridgeReady = false;
  currentPage = page;
  pendingCmd = null;
  sim.onload = null;
  if (EMBED) {
    const html = (EMBED.pages || {})[page];
    if (!html) { feedback("(This page snapshot is missing from the export.)", "bad"); return; }
    sim.srcdoc = html;
  } else {
    sim.src = `/capture/${encodeURIComponent(walkthroughId)}/${page.split("/").map(encodeURIComponent).join("/")}`;
  }
  // bridge-ready message (not iframe onload) is the real signal
  waitForBridge(onReady);
}

let bridgeWaiter = null;
function waitForBridge(cb) { bridgeWaiter = cb; }

/* A cursor chosen during playback: tried out, never written anywhere. Null
 * means "use the walkthrough's own". Deliberately not persisted like the
 * playback preferences beside it — the shape belongs to the walkthrough, and a
 * viewer's experiment must not outlive the tab or leak into someone else's
 * reading of it. Cleared on entering edit mode, so the editor always shows
 * what is actually stored. */
let cursorOverride = null;

function effectiveCursor() {
  const c = cursorOverride || (walkthrough && walkthrough.cursor);
  return c === "pointer" ? "pointer" : "dot";
}

/* The bridge's whole configuration: playback speed (a viewer preference) and
 * cursor shape (the walkthrough's, or a session override). Built in one place
 * so the bridge-ready and live-update paths cannot drift apart. */
function bridgeConfig() {
  return { cmd: "config", speed: settings.speed, cursor: effectiveCursor() };
}

function sendToBridge(msg) {
  if (bridgeReady) sim.contentWindow.postMessage(msg, "*");
  else pendingCmd = msg;
}

window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type === "bridge-ready") {
    bridgeReady = true;
    // Floating chrome separates from the capture with a two-tone edge; on a
    // dark page that edge has to flip to the light side. Per page, because a
    // walkthrough can cross a light app into a dark one.
    document.body.classList.toggle("sc-dark-capture", !!m.dark);
    // configure the fresh page before any queued showme/expect runs
    sim.contentWindow.postMessage(bridgeConfig(), "*");
    if (windowActive()) beginWindowMeasure(); // resolve the capture window on this page
    if (bridgeWaiter) { const cb = bridgeWaiter; bridgeWaiter = null; cb(); }
    if (pendingCmd) { sim.contentWindow.postMessage(pendingCmd, "*"); pendingCmd = null; }
    aiOnPageReady();
  } else if (m.type === "window-rect") {
    onWindowRect(m);
  } else if (m.type === "success") {
    onStepSuccess();
  } else if (m.type === "wrong") {
    onWrong(m);
  } else if (m.type === "showme-done") {
    onShowmeDone(m);
  } else if (m.type === "picked") {
    onPicked(m);
  } else if (m.type === "pick-cancelled") {
    // Escape inside the page; the bridge has already left picker mode
    if (aiPicking) { aiPicking = false; aiAfterPick(); }
  }
  // probe-result is highlight-only in the simplified editor
});

/* ================= windowed playback ================= */
/* walkthrough.window = { target?, rect?, viewport?, padding?, label? } —
 * a region of the page picked at capture time (the "capture window"), for
 * inline-help style playback. The page is always captured in full; the
 * player frames the iframe to the region: the iframe is laid out at the
 * captured viewport width and full document height (so it can never scroll
 * internally), then translated/scaled so the region fills the stage
 * (#sim.windowed in player.css).
 *
 * The frame is FIXED: the window element is resolved once, on the first
 * page of the run (recorded rect as fallback if the selector doesn't
 * match), and that exact crop is kept for every following step — the view
 * never pans or re-zooms as the walkthrough progresses, and playback runs
 * through every captured page (even after the window's element leaves the
 * page) until the walkthrough's recorded end. The ⛶ toolbar button lets
 * the learner switch to the full page at any time. */
let fullPageView = false;  // learner's ⛶ toggle
let pageWin = null;        // the frozen window region {x,y,w,h}, padded
let pageDoc = null;        // current page document size {width,height}
let winMeasuring = false;  // first measurement pass (at captured viewport size) in flight

function windowDef() {
  const w = walkthrough && walkthrough.window;
  return w && (w.target || w.rect) ? w : null;
}
function windowActive() { return !!windowDef() && mode !== "edit"; }

function resetWindowView() {
  fullPageView = false;
  pageWin = pageDoc = null;
  winMeasuring = false;
  applyView();
}

// The element the window is tied to: the drawn rectangle's anchor, or (for
// legacy element-picked windows) the window target itself.
function windowTrackTarget() {
  const wd = windowDef();
  return wd.anchor?.target || wd.target || null;
}

function requestWindowRect() {
  sim.contentWindow.postMessage({ cmd: "window-rect", target: windowTrackTarget() }, "*");
}

// The window element must be measured in the layout it will actually play
// in — fixed/centered elements (dialogs, toasts) position themselves
// relative to the viewport, so measuring at the stage's incidental size
// yields the wrong rect. Two passes: lay the iframe out at the captured
// viewport size and measure (→ document height), then re-lay it out at
// full document height (windowed pages must never scroll) and measure
// again; the second rect is where the element truly sits during playback.
function beginWindowMeasure() {
  if (pageWin) { requestWindowRect(); return; } // frame frozen — presence check only
  const wd = windowDef();
  winMeasuring = true;
  sim.style.visibility = "hidden"; // no flash of the unframed page
  sim.style.transition = "none";
  sim.classList.add("windowed");
  sim.style.width = `${wd.viewport?.width || 1280}px`;
  sim.style.height = `${wd.viewport?.height || 800}px`;
  requestWindowRect();
}

function onWindowRect(m) {
  if (!windowActive()) return;
  pageDoc = m.doc || null;
  const wd = windowDef();

  if (pageWin) {
    // the frame is already frozen — this page load only refreshes the
    // document size. The window's element disappearing (e.g. the dialog
    // closed) does NOT end playback: every captured page plays, in the
    // same frozen frame, until the walkthrough's recorded end.
    applyView();
    return;
  }

  if (winMeasuring) {
    // pass 2: final playback layout (full document height), then re-measure
    winMeasuring = false;
    sim.style.height = `${Math.max(pageDoc?.height || 0, wd.viewport?.height || 0)}px`;
    requestWindowRect();
    return;
  }

  const pad = wd.padding ?? 0;
  let r;
  if (wd.target) {
    // legacy element-picked window: frame the element itself
    r = m.rect || wd.rect || null;
  } else if (wd.anchor && m.rect && wd.rect) {
    // drawn rectangle, re-positioned relative to its anchor element — keeps
    // the frame correct for fixed/centred elements (dialogs) whose position
    // depends on the playback layout
    r = { x: m.rect.x + (wd.anchor.dx || 0), y: m.rect.y + (wd.anchor.dy || 0),
          width: wd.rect.width, height: wd.rect.height };
  } else {
    // raw rectangle (no anchor, or the anchor didn't resolve)
    r = wd.rect || null;
  }
  pageWin = r ? { x: Math.max(0, r.x - pad), y: Math.max(0, r.y - pad),
                  w: r.width + pad * 2, h: r.height + pad * 2 } : null;
  applyView();
  reportEmbedSize();
}

/* Embedded-in-a-host-page support (static/embed.js): when an exported,
 * windowed walkthrough plays inside an iframe (the customer-site Help
 * modal), tell the host how big the player wants to be so the region shows
 * life-sized: the frozen region plus this player's own chrome (top bar,
 * step panel, stage padding), measured rather than hard-coded. The host
 * caps it to the visitor's viewport. Fire-and-forget: a plain <iframe>
 * embed without embed.js simply ignores the message. */
function reportEmbedSize() {
  if (!EMBED || window.parent === window || !pageWin) return;
  const stage = $("sim-wrap").parentElement;
  const chromeW = window.innerWidth - Math.max(50, stage.clientWidth - 28);
  const chromeH = window.innerHeight - Math.max(50, stage.clientHeight - 28);
  try {
    window.parent.postMessage({
      type: "sc-embed-size",
      width: Math.round(pageWin.w + Math.max(0, chromeW)),
      height: Math.round(pageWin.h + Math.max(0, chromeH))
    }, "*");
  } catch (e) { /* host page may be cross-origin-strict; size stays default */ }
}

// The page-coordinate rect the stage should frame right now.
function viewRegion() {
  if (!pageWin) return null;
  if (fullPageView && pageDoc) return { x: 0, y: 0, w: pageDoc.width, h: pageDoc.height };
  return pageWin;
}

function applyView() {
  const btn = $("btn-window");
  const active = windowActive();
  btn.classList.toggle("hidden", !active);
  const region = active ? viewRegion() : null;
  const wrap = $("sim-wrap");
  if (!region) {
    sim.classList.remove("windowed");
    sim.style.width = sim.style.height = sim.style.transform = "";
    sim.style.visibility = sim.style.transition = "";
    wrap.style.width = wrap.style.height = "";
    return;
  }
  btn.title = fullPageView ? "Back to the recorded region" : "View the full page";
  btn.classList.toggle("windowed-off", fullPageView);

  // life-sized: the region renders at its captured pixel size (never zoomed
  // in), only scaled DOWN if the stage is too small for it. The wrap shrinks
  // to the region and sits centered in the stage, so nothing outside the
  // region can peek around the crop.
  const stage = wrap.parentElement;
  const availW = Math.max(50, stage.clientWidth - 28);  // minus stage padding
  const availH = Math.max(50, stage.clientHeight - 28);
  const wd = windowDef();
  const layoutW = wd.viewport?.width || 1280;
  const layoutH = Math.max(pageDoc?.height || 0, region.y + region.h, wd.viewport?.height || 0);
  const s = Math.min(availW / region.w, availH / region.h, 1); // 1 = life-sized cap
  sim.classList.add("windowed");
  wrap.style.width = `${Math.round(region.w * s)}px`;
  wrap.style.height = `${Math.round(region.h * s)}px`;
  sim.style.width = `${layoutW}px`;
  sim.style.height = `${layoutH}px`;
  sim.style.transform = `translate(${-region.x * s}px, ${-region.y * s}px) scale(${s})`;
  if (sim.style.visibility === "hidden" || sim.style.transition === "none") {
    // first frame after measuring: snap into place (no opening zoom), then
    // re-enable the transition for later toggle/resize changes
    sim.style.visibility = "";
    requestAnimationFrame(() => { sim.style.transition = ""; });
  }
}

function toggleWindowView() {
  fullPageView = !fullPageView;
  applyView();
}

window.addEventListener("resize", () => { if (windowActive()) applyView(); });

/* ================= step outcomes ================= */
const SUCCESS_MESSAGES = [
  "Nice — that's it.",
  "Exactly right.",
  "Got it — well done.",
  "That's the one.",
  "Perfect.",
  "Spot on.",
  "Yes — just like that.",
  "Right on target.",
  "Nailed it.",
  "Good — keep going.",
];
const recentSuccessMsgs = []; // last few shown, to avoid quick repeats

function successMessage() {
  const pool = SUCCESS_MESSAGES.filter((m) => !recentSuccessMsgs.includes(m));
  const msg = pool[Math.floor(Math.random() * pool.length)];
  recentSuccessMsgs.push(msg);
  if (recentSuccessMsgs.length > 3) recentSuccessMsgs.shift();
  return msg;
}

function showContinueButton() {
  const last = stepIdx === walkthrough.steps.length - 1;
  $("btn-next").textContent = last ? "Finish walkthrough" : "Continue →";
  $("btn-next").classList.remove("hidden");
}

function onStepSuccess() {
  // Mid-run: straight on, with no praise and no Continue. The run is one beat,
  // so the congratulation belongs at the end of it — "Nice!" after every field
  // of an address is the same noise as a Continue button after every field.
  if (continuesRun(stepIdx + 1)) return nextStep();
  feedback(successMessage(), "good");
  if (settings.autoAdvance) {
    scheduleAdvance();
  } else {
    showContinueButton();
  }
}

function onWrong(m) {
  wrongCount++; totalWrong++;
  const step = walkthrough.steps[stepIdx];
  if (m.wrongValue && step.action.wrong_value_feedback) {
    feedback(step.action.wrong_value_feedback, "bad");
  } else if (wrongCount >= 2) {
    feedback("Not quite — use the Hint button if you're stuck.", "bad");
    $("btn-hint").classList.remove("hidden");
  } else {
    feedback("Not that one — look again at the instruction.", "bad");
  }
}

function onShowmeDone(m) {
  if (mode === "edit") {
    // step playback in the editor: report, don't advance
    if (m.missing) edStatus("bad", "Play failed — the step's target was not found on this page.");
    else edStatus("good", "Step played.", true);
    return;
  }
  if (m.missing) { feedback("(Could not locate the demo target on this page.)", "bad"); return; }
  // Mid-run the cursor carries straight on to the next field — one continuous
  // motion down the form rather than three demos with a wait between them.
  if (continuesRun(stepIdx + 1)) return nextStep();
  if (settings.autoAdvance) {
    scheduleAdvance();
  } else {
    showContinueButton();
  }
}

/* ================= UI helpers ================= */
function renderStepPanel() {
  const step = walkthrough.steps[stepIdx];
  const editing = mode === "edit";
  document.querySelector(".panel").classList.toggle("editing", editing);
  $("editor").classList.toggle("hidden", !editing);
  syncSettingsVisibility();
  // Learners are counted in beats, editors in steps: an editor is working on
  // the field in front of them and needs to know which one it is.
  const beat = beatOf[stepIdx] || 0;
  $("panel-step-num").textContent = editing
    ? `STEP ${pad2(stepIdx + 1)} / ${pad2(walkthrough.steps.length)} · EDITING`
    : `STEP ${pad2(beat + 1)} / ${pad2(beatCount)}`;
  if (editing) {
    fillEditor(step);
  } else {
    // The run's own title heads the whole run, so the heading holds steady
    // while the caption below it advances field by field.
    $("panel-title").textContent = walkthrough.steps[runStartOf(stepIdx)].title;
    $("panel-instruction").textContent = mode === "show" ? (step.showme_caption || step.instruction) : step.instruction;
    $("panel-feedback").className = "feedback hidden";
    $("btn-next").classList.add("hidden");
    $("btn-play").classList.add("hidden");
    $("btn-hint").classList.toggle("hidden", !(mode === "try" && step.action.type !== "observe" && wrongCount >= 2));
    $("btn-replay").classList.toggle("hidden", mode !== "show");
  }
  $("tb-progress").textContent = editing
    ? `STEP ${pad2(stepIdx + 1)} / ${pad2(walkthrough.steps.length)}`
    : `STEP ${pad2(beat + 1)} / ${pad2(beatCount)}`;
  $("progress-fill").style.width =
    `${(editing ? stepIdx / walkthrough.steps.length : beat / beatCount) * 100}%`;
  // The rail holds run starts in play modes and every step in edit mode, so
  // both the marker and the comparison run on the real index in data-step.
  const marker = editing ? stepIdx : runStartOf(stepIdx);
  [...$("step-list").children].forEach((li) => {
    const i = Number(li.dataset.step);
    li.className = i < marker ? "done" : i === marker ? "current" : "";
    // what the last AI edit touched, until it is undone or superseded
    li.classList.toggle("ai-changed", editing && aiChanged.has(i));
  });
  if (editing) {
    aiRenderContext();
    aiResume();
  }
}

/* ---- playback settings modal ---- */
function openSettings() {
  syncSettingsVisibility();
  $("settings-modal").classList.remove("hidden");
}

function closeSettings() {
  $("settings-modal").classList.add("hidden");
}

function settingsModalOpen() {
  return !$("settings-modal").classList.contains("hidden");
}

// clicking the dimmed backdrop (not the card) closes the modal
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) closeSettings();
});

function syncSettingsVisibility() {
  // speed only shapes Show-me demos — fade it in You-try to say "not now"
  $("opt-speed").closest(".ps-row").classList.toggle("dimmed", mode === "try");
  // the wait timers only run when auto-advance is on
  const noAuto = !settings.autoAdvance;
  $("opt-start-delay").closest(".ps-row").classList.toggle("dimmed", noAuto || mode === "try");
  $("opt-next-delay").closest(".ps-row").classList.toggle("dimmed", noAuto);
  // The cursor row is always offered, but where the choice lands depends on
  // the mode, and the label is the only thing that says so — a <select> looks
  // identical either way.
  $("ps-cursor-scope").textContent = mode === "edit"
    ? "Saved with this walkthrough" : "This session only";
  // re-read so a revert, a mode switch or a dropped override resyncs it
  $("opt-cursor").value = effectiveCursor();
}

/* Enter / Space triggers the visible primary action (skip wait / continue);
 * Esc closes the settings modal */
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && settingsModalOpen()) { closeSettings(); return; }
  if (mode === "edit" || settingsModalOpen()) return;
  if (introOpen()) return;   // the player is live behind the intro — don't drive it
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName)) return;
  if ($("player").classList.contains("hidden")) return;
  if (!$("btn-next").classList.contains("hidden")) { e.preventDefault(); nextStep(); }
  else if (!$("btn-play").classList.contains("hidden")) { e.preventDefault(); startShowmeNow(); }
});

function feedback(text, kind) {
  const el = $("panel-feedback");
  el.textContent = text;
  el.className = `feedback ${kind}`;
}

function show(screenId) {
  ["player", "complete"].forEach((s) => $(s).classList.toggle("hidden", s !== screenId));
}

function openIntroModal() {
  $("intro-modal").classList.remove("hidden");
}

function introOpen() {
  return !$("intro-modal").classList.contains("hidden");
}

// The intro's three actions are three different answers — start the demo,
// practise it, or leave — so every way out of the modal has to name one.
// That rules out a backdrop-click dismiss (a stray click would pick a mode)
// and it is why ✕ exits rather than doubling as Start.
function dismissIntro(m) {
  $("intro-modal").classList.add("hidden");
  startWalkthrough(m === "try" ? "try" : "show");
}

function exitIntro() {
  // Hidden in exports/embeds (there is no catalog to go back to) — see the
  // #intro-close rule in player.css.
  location.href = "/";
}

/* The hue contract, applied. Everything that has to agree with the switch —
 * the step eyebrow, the progress fill, the spotlight ring — reads --mode-hue,
 * and --mode-hue is set by these two body classes and nowhere else. The
 * learner should know which mode they are in before reading a word. */
function syncModeButtons() {
  $("mode-show").classList.toggle("active", mode === "show");
  $("mode-try").classList.toggle("active", mode === "try");
  document.body.classList.toggle("mode-showme", mode === "show");
  document.body.classList.toggle("mode-youtry", mode !== "show");
}

/* ================= editor ================= */
function initEditor() {
  const step = () => walkthrough.steps[stepIdx];
  const bindText = (id, set) => $(id).addEventListener("input", () => {
    set(step(), $(id).value); markDirty();
    if ($(id).tagName === "TEXTAREA") autoGrow($(id));
  });
  bindText("ed-title", (s, v) => {
    s.title = v;
    // by data-step, not position: the rail collapses runs outside edit mode
    const li = $("step-list").querySelector(`li[data-step="${stepIdx}"]`);
    if (li) li.textContent = v;   // keep the side list in sync
  });
  bindText("ed-instruction", (s, v) => { s.instruction = v; });
  bindText("ed-caption", (s, v) => { s.showme_caption = v; });
  bindText("ed-hint", (s, v) => { s.hint = v; });

  // Absent means false, so the flag is deleted rather than written as `false`:
  // an untouched walkthrough must not grow a field on its next save.
  $("ed-continues").addEventListener("change", () => {
    const s = step();
    if ($("ed-continues").checked) s.continues = true; else delete s.continues;
    computeBeats();
    renderStepPanel();   // the counter and the run's heading both move
    markDirty();
  });

  $("step-list").addEventListener("click", (e) => {
    if (mode !== "edit") return;
    const li = e.target.closest("li");
    if (!li) return;
    const i = Number(li.dataset.step);
    if (i >= 0) gotoStep(i);
  });
}

function autoGrow(el) {
  // size the textarea to its content (min ~3 lines, no inner scrolling)
  el.style.height = "auto";
  el.style.height = Math.max(72, el.scrollHeight + 2) + "px";
}

function fillEditor(step) {
  $("ed-title").value = step.title || "";
  $("ed-instruction").value = step.instruction || "";
  $("ed-caption").value = step.showme_caption || "";
  $("ed-hint").value = step.hint || "";
  // The two shapes author.validate() rejects, refused here rather than at save:
  // nothing precedes the first step, and a run cannot straddle two snapshots.
  const cont = $("ed-continues");
  cont.checked = !!step.continues;
  cont.disabled = stepIdx === 0 || !!step.page;
  cont.title = stepIdx === 0 ? "The first step has nothing to run on from."
    : step.page ? "This step loads a new page, so it starts a new run."
    : "";
  $("ed-play").disabled = step.action.type === "observe";
  ["ed-instruction", "ed-caption", "ed-hint"].forEach((id) => autoGrow($(id)));
  syncSaveButton();
}

function probeTarget() {
  const step = walkthrough.steps[stepIdx];
  if (step.action.type === "observe") return;
  sendToBridge({ cmd: "probe", target: step.action.target });  // highlight only
}

function playStep() {
  const step = walkthrough.steps[stepIdx];
  if (step.action.type === "observe") return;
  // replay on a fresh page so typed/selected state doesn't accumulate
  currentPage = null;
  const page = effectivePage(stepIdx);
  if (page) loadPage(page, () => sendToBridge({ cmd: "showme", action: step.action }));
  else sendToBridge({ cmd: "showme", action: step.action });
}

function edStep(d) {
  gotoStep(Math.max(0, Math.min(walkthrough.steps.length - 1, stepIdx + d)));
}

function syncSaveButton() {
  const b = $("ed-save");
  b.disabled = !dirty;
  b.classList.toggle("dirty", dirty);
  b.textContent = dirty ? "Save changes" : "Save";
}

function markDirty() {
  dirty = true;
  syncSaveButton();
  $("ed-status").className = "feedback hidden";
}

/* editor status line — transient messages dismiss themselves */
let statusTimer = null;
function edStatus(kind, text, transient) {
  const st = $("ed-status");
  clearTimeout(statusTimer);
  st.style.opacity = "";
  st.className = `feedback ${kind}`;
  st.textContent = text;
  if (transient) {
    statusTimer = setTimeout(() => {
      st.style.opacity = "0";
      statusTimer = setTimeout(() => {
        st.className = "feedback hidden";
        st.style.opacity = "";
      }, 450);
    }, 2600);
  }
}

async function saveWalkthrough() {
  try {
    const r = await fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(walkthrough),
    });
    const d = await r.json();
    if (!r.ok) { edStatus("bad", d.error || `Save failed (${r.status})`); return false; }
    dirty = false;
    syncSaveButton();
    // a manual save on top of an AI edit makes the undo stamp stale — undoing
    // would silently discard the edits just saved
    lastAiStamp = null;
    aiSyncUndo();
    if (d.warnings && d.warnings.length) {
      edStatus("good", "Saved with warnings: " + d.warnings.join(" · "));  // stays visible
    } else {
      edStatus("good", "Saved.", true);
    }
    return true;
  } catch (e) {
    edStatus("bad", "Save failed: " + e);
    return false;
  }
}

async function revertWalkthrough() {
  if (dirty && !window.confirm("Discard unsaved changes and reload the saved walkthrough?")) return;
  const r = await fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}`);
  adoptWalkthrough(await r.json());
}

/* swap in a fresh server-side copy of the walkthrough and re-render around it.
 * The iframe reset forces the captured page to reload, so page edits show. */
function adoptWalkthrough(data) {
  walkthrough = data;
  dirty = false;
  syncSaveButton();
  $("ed-status").className = "feedback hidden";
  renderIntro();     // rebuilds titles + step list
  show("player");
  currentPage = null;
  gotoStep(Math.min(stepIdx, walkthrough.steps.length - 1));
}

/* ---- AI assist ----------------------------------------------------------
 * The rail's second tab in edit mode. It used to be a floating window, and a
 * floating window sits on top of exactly the page the author is pointing at;
 * as a tab it never covers the capture.
 *
 * A request runs as a background job (?background=1): the POST answers 202
 * {job_id} at once and the tab follows /api/jobs/<id>/events, so the author
 * watches what the model is doing and can stop it, rather than staring at a
 * spinner for a minute or two. run_edit decides whether the message was a
 * change (saved, undoable) or a question (answered, nothing saved) and says
 * which in `kind`.
 *
 * The thread is this tab's memory and nothing else's: it is not stored, it
 * does not survive a reload, and the model never sees it. Every request is
 * one-shot against the saved walkthrough. */
let aiTab = false;          // AI assist is the rail's open tab
let aiJob = null;           // the request in flight — see aiApply()
let aiUndoing = false;
let lastAiStamp = null;     // history stamp of the newest AI edit still undoable
let aiChanged = new Set();  // step indices the last AI edit touched (rail markers)
let aiSource = null;        // the live EventSource, if any
let aiTicker = null;        // repaints the running turn's clock
let aiResumeChecked = false;
let aiSuggestFresh = false; // a pick just landed: offer suggestions for it once

// Thin line icons, as the rest of the product draws them. Constant strings,
// so innerHTML is safe here.
const AI_ICONS = {
  target: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>',
  undo: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
  chevron: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
};

function aiEl(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function aiIcon(name) {
  const s = aiEl("span", "ai-icon");
  s.innerHTML = AI_ICONS[name];
  return s;
}

function aiClock(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${pad2(s % 60)}`;
}

/* ---- the rail's two tabs ---------------------------------------------- */
function railTab(which) {
  const ai = which === "ai";
  if (!ai) aiCancelPick();   // never leave the frame in crosshair mode behind a hidden tab
  aiTab = ai;
  document.querySelector(".panel").classList.toggle("ai-on", ai);
  $("ai-panel").classList.toggle("hidden", !ai);
  $("tab-step").classList.toggle("active", !ai);
  $("tab-ai").classList.toggle("active", ai);
  $("tab-step").setAttribute("aria-selected", String(!ai));
  $("tab-ai").setAttribute("aria-selected", String(ai));
  aiSyncMark();
  if (ai) {
    aiRenderContext();
    aiScrollThread(true);
    $("ai-instruction").focus();
  }
}

function openAiPanel() { railTab("ai"); }

function closeAiPanel() { if (aiTab) railTab("step"); }

/* What the author is looking at, which is what "this step" means to the
 * model. The pager is here so a step can be changed without leaving the tab. */
function aiRenderContext() {
  if (!walkthrough || mode !== "edit") return;
  const step = walkthrough.steps[stepIdx];
  $("ai-ctx-step").textContent = `step ${pad2(stepIdx + 1)} / ${pad2(walkthrough.steps.length)}`;
  $("ai-ctx-title").textContent = step.title || "Untitled step";
  aiRenderPickControls();
  aiRenderSuggestions();
}

/* ---- pointing at an element -------------------------------------------
 * The instruction box alone cannot say "this one" about a specific element,
 * so the author picks it on the page and it is named to the model alongside
 * the instruction. bridge.js already had the picker (it predates the
 * simplified step editor, which stopped consuming `picked`); this is now its
 * only caller.
 *
 * What comes back is worth more than a description the model could derive
 * itself: suggestTarget() scopes the selector with ancestors until it matches
 * exactly one node IN THE LIVE DOM. Nothing on the server can do that —
 * validate() has no CSS engine — so a picked selector is the one handle in
 * this pipeline that is known unique rather than assumed to be.
 *
 * The bridge holds a solid outline on the chosen element ({cmd:"mark"}) for
 * as long as it is selected and on screen, so the author can see what the
 * model is going to be told about. */
let aiSelection = null;   // {page, target, selector, text?, text_exact?, tag, matches, html}
let aiPicking = false;

function aiPickToggle() {
  if (aiPicking) { aiCancelPick(); return; }
  if (!currentPage) {   // nothing in the frame to point at yet
    aiAddNote("bad", "There is no page on screen to point at yet. Open a step first.");
    return;
  }
  aiPicking = true;
  sendToBridge({ cmd: "pick" });
  aiRenderPickControls();
  aiRenderSuggestions();
}

/* Guarded so the normal path never posts a stray command: sendToBridge holds
 * the message as pendingCmd when the frame is mid-load, where it would
 * displace the showme/expect that the page load is being done for. */
function aiCancelPick() {
  if (!aiPicking) return;
  aiPicking = false;
  sendToBridge({ cmd: "pick-off" });
  aiAfterPick();
}

// Starting a pick clears the step's spotlight inside the page; put it back.
function aiAfterPick() {
  aiRenderPickControls();
  aiRenderSuggestions();
  if (mode === "edit") probeTarget();
}

function onPicked(m) {
  if (!aiPicking) return;   // a stale pick from a page we have since left
  aiPicking = false;
  const t = m.target || {};
  if (!t.selector) {
    aiAddNote("bad", "Could not identify that element. Try one next to it.");
    aiAfterPick();
    return;
  }
  aiSuggestFresh = true;
  aiSelection = {
    page: currentPage,
    target: t,   // kept whole (frames included) so the bridge can find it again
    selector: t.selector,
    text: t.text || "",
    text_exact: t.text_exact || "",
    tag: m.tag || "",
    matches: typeof m.matches === "number" ? m.matches : null,
    html: m.html || "",
  };
  $("ai-sel-details").classList.add("hidden");
  $("ai-sel-toggle").setAttribute("aria-expanded", "false");
  aiAfterPick();
  aiSyncMark();
  $("ai-instruction").focus();
}

function aiClearSelection() {
  aiSelection = null;
  aiSyncMark();
  aiRenderPickControls();
  aiRenderSuggestions();
}

function aiToggleSelDetails() {
  const box = $("ai-sel-details");
  const open = box.classList.toggle("hidden") === false;
  $("ai-sel-toggle").setAttribute("aria-expanded", String(open));
}

/* Posted directly rather than through sendToBridge: a mark is never worth
 * displacing the pending command a page load is waiting on, and a fresh page
 * re-marks itself from bridge-ready anyway. */
function aiSyncMark() {
  if (!bridgeReady || mode !== "edit" || EMBED) return;
  const on = aiTab && aiSelection && aiSelection.page === currentPage;
  sim.contentWindow.postMessage({ cmd: "mark", target: on ? aiSelection.target : null }, "*");
}

/* gotoStep renders the rail before the new page is in the frame, so whether
 * the pointed-at element is on screen is only known once the bridge is up —
 * and a fresh page comes up unmarked. */
function aiOnPageReady() {
  if (EMBED || mode !== "edit") return;
  aiSyncMark();
  aiRenderPickControls();
}

/* A plain-words name for what was picked: the author thinks "the salutation
 * dropdown", not "button.slds-combobox__input". */
function aiElementKind(sel) {
  const tag = (sel.tag || "").toLowerCase();
  const start = (sel.html || "").slice(0, 400).toLowerCase();
  if (tag === "select" || /combobox|listbox|picklist/.test(start)) return "dropdown";
  if (tag === "textarea") return "text box";
  if (tag === "input") {
    const type = (start.match(/type="?([a-z]+)/) || [])[1];
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "option";
    if (type === "submit" || type === "button") return "button";
    return "field";
  }
  if (tag === "button" || /role="?button/.test(start)) return "button";
  if (tag === "a") return "link";
  if (tag === "tr") return "table row";
  if (tag === "label") return "label";
  return "element";
}

function aiSelLabel(sel) {
  const text = (sel.text || sel.text_exact || "").trim();
  if (!text) return aiElementKind(sel);
  return `“${text.length > 36 ? text.slice(0, 35) + "…" : text}”`;
}

function aiRenderPickControls() {
  const pick = $("ai-pick");
  pick.classList.toggle("picking", aiPicking);
  pick.setAttribute("aria-pressed", String(aiPicking));
  $("ai-pick-label").textContent = aiPicking ? "Cancel" : "Point";
  pick.title = aiPicking ? "Stop pointing (Esc)" : "Point at an element on the page";
  $("ai-pick-banner").classList.toggle("hidden", !aiPicking);

  const box = $("ai-selection");
  box.classList.toggle("hidden", !aiSelection || aiPicking);
  if (!aiSelection) return;
  const s = aiSelection;
  const here = s.page === currentPage;
  box.classList.toggle("elsewhere", !here);
  $("ai-sel-what").textContent = aiSelLabel(s);
  $("ai-sel-kind").textContent = (s.text || s.text_exact ? aiElementKind(s) : "") +
    (here ? "" : (s.text || s.text_exact ? " · " : "") + "not on this page");
  $("ai-sel-where").textContent = s.selector + (s.page ? `\n${s.page}` : "");
  // suggestTarget scopes until it is unique, so anything else is a real
  // ambiguity the author should see before the model inherits it
  const warn = $("ai-sel-warn");
  const ambiguous = s.matches !== null && s.matches !== 1;
  warn.classList.toggle("hidden", !ambiguous);
  if (ambiguous) {
    warn.textContent = s.matches === 0
      ? "This element could not be pinned down exactly. The AI will be told so."
      : `This matches ${s.matches} elements on the page. Be specific about which one.`;
  }
}

/* ---- suggestions -------------------------------------------------------
 * Shown when the thread is empty or something has just been pointed at, and
 * only while the composer is empty and nothing is running: they fill the box,
 * they never send. Once a request goes out they step aside — a thread with a
 * conversation in it needs the height more than it needs prompting. */
function aiSuggestions() {
  if (aiSelection) {
    switch (aiElementKind(aiSelection)) {
      case "dropdown":
        return ["Do we have this list's options from the capture?",
                "Why doesn't this open when clicked?",
                "Make this the target of this step"];
      case "field":
      case "text box":
        return ["Make this field the target of this step",
                "Use a more realistic demo value here",
                "Is this field required on the page?"];
      case "button":
      case "link":
        return ["Make this the target of this step",
                "Add a step that clicks this",
                "Change the text shown on this"];
      default:
        return ["What is this element?",
                "Make this the target of this step",
                "Change the text shown here"];
    }
  }
  const type = ((walkthrough.steps[stepIdx] || {}).action || {}).type;
  const stuck = "Why might a learner get stuck on this step?";
  if (type === "select") return ["Let any value be chosen on this step", "Shorten this step's hint", stuck];
  if (type === "input") return ["Use a more realistic demo value", "Shorten this step's hint", stuck];
  if (type === "observe") return ["Rewrite the caption to explain why this matters", "Shorten this step's caption", stuck];
  return ["Shorten this step's hint", "Rewrite the caption to explain why", stuck];
}

function aiRenderSuggestions() {
  const wrap = $("ai-suggest");
  wrap.textContent = "";
  const show = mode === "edit" && walkthrough && !aiPicking && !aiJob &&
    !$("ai-instruction").value.trim() && (aiThreadEmpty() || (aiSelection && aiSuggestFresh));
  if (!show) return;
  for (const s of aiSuggestions()) {
    const b = aiEl("button", "ai-suggest-chip", s);
    b.type = "button";
    b.dataset.suggest = s;
    wrap.append(b);
  }
}

/* ---- the thread --------------------------------------------------------- */
function aiThreadEmpty() { return !$("ai-thread").querySelector(".ai-turn"); }

// Follow new content only when the author is already at the bottom: someone
// scrolled up to reread an answer should not be yanked away by a progress line.
function aiScrollThread(force) {
  const t = $("ai-thread");
  if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 80) t.scrollTop = t.scrollHeight;
}

function aiAppend(node) {
  $("ai-empty").classList.add("hidden");
  const t = $("ai-thread");
  const follow = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  t.appendChild(node);
  aiScrollThread(follow || node.classList.contains("ai-turn--you"));
}

function aiAddNote(kind, text) {
  const note = aiEl("div", "ai-sys" + (kind === "bad" ? " bad" : ""));
  note.append(aiEl("span", null, text));
  aiAppend(note);
}

function aiAddYouTurn(text, atStep, sel) {
  const turn = aiEl("div", "ai-turn ai-turn--you");
  turn.append(aiEl("div", "ai-bubble", text));
  const ctx = aiEl("div", "ai-you-ctx");
  ctx.append(aiEl("span", null, `Step ${atStep + 1}`));
  if (sel) {
    const s = aiEl("span", "ai-you-el");
    s.append(aiIcon("target"), document.createTextNode(aiSelLabel(sel)));
    ctx.append(s);
  }
  turn.append(ctx);
  aiAppend(turn);
}

function aiHead(turn, kind, label, time) {
  const head = aiEl("div", "ai-head");
  head.append(aiEl("span", `ai-dot ai-dot--${kind}`), aiEl("span", "ai-head-label", label));
  if (time != null) head.append(aiEl("span", "ai-time", time));
  turn.append(head);
  return head;
}

function aiAddRunTurn() {
  const turn = aiEl("div", "ai-turn ai-turn--ai is-running");
  aiHead(turn, "live", "working", "0:00");
  const now = aiEl("div", "ai-now");
  now.setAttribute("role", "status");
  now.setAttribute("aria-live", "polite");
  now.append(aiEl("span", "ai-spinner"), aiEl("span", "ai-now-text", "Sending your request"));
  turn.append(now, aiEl("ol", "ai-activity"),
              aiEl("p", "ai-run-note", ""));
  aiAppend(turn);
  return turn;
}

/* The pass reports at its own boundaries — every model turn and every tool
 * call — in the server's words. These are the author's words for the same
 * moments. Unknown phases fall through to the message, so a new one is still
 * legible before anyone writes a phrasing for it. */
function aiPhaseText(phase, message) {
  const msg = String(message || "");
  const cap = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : "");
  const n = (msg.match(/\d+/) || [])[0];
  const plural = n === "1" ? "" : "s";
  switch (phase) {
    case "thinking": return /^turn 1 of/.test(msg) ? "Reading the walkthrough and its pages" : "Thinking";
    case "tool": return cap(msg);
    case "validated": return msg === "no errors" ? "Checked the draft: no problems"
      : `Found ${n || "some"} problem${plural} to fix`;
    case "rejected": return `Fixing ${n || "the"} problem${plural} the checker found`;
    case "review": return "Double-checking the warnings";
    case "accepted": return "Saving the change";
    case "salvaged": return "Saving the last version that checked out";
    case "verifying": return "Replaying the changed steps to make sure they work";
    case "answered": return "Writing the answer";
    default: return cap(msg);
  }
}

function aiOnProgress(job, e) {
  if (!e || typeof e.seq !== "number" || e.seq <= job.lastSeq) return;
  job.lastSeq = e.seq;
  const text = aiPhaseText(e.phase, e.message);
  if (!text || text === job.now) return;
  // "Sending your request" filled the gap before the first event; it was not
  // something the model did, so it does not go into the trail
  if (job.now && !job.placeholder) job.lines.push(job.now);
  job.placeholder = false;
  job.now = text;
  aiPaintRun(job);
}

function aiPaintRun(job) {
  const t = job.turn;
  t.querySelector(".ai-head-label").textContent = job.stopping ? "stopping" : "working";
  t.querySelector(".ai-now-text").textContent =
    job.stopping ? "Stopping after the current step" : job.now;
  const ol = t.querySelector(".ai-activity");
  ol.textContent = "";
  for (const line of job.lines.slice(-3)) ol.append(aiEl("li", null, line));
  // Cancellation is cooperative: a model call already in flight completes
  // (and is billed) before the stop lands. Said plainly, not implied away.
  t.querySelector(".ai-run-note").textContent = job.stopping
    ? "A model call already under way finishes first, so this can take a moment."
    : "This usually takes a minute or two. You can write your next request meanwhile.";
  aiScrollThread();
}

function aiTick() {
  if (!aiJob) return;
  const time = aiJob.turn.querySelector(".ai-time");
  if (time) time.textContent = aiClock(Date.now() - aiJob.startedAt);
}

/* ---- sending ------------------------------------------------------------
 * `text` is given when a request comes from the thread (a suggested change,
 * Try again) rather than from the composer, which is then left alone — the
 * author may already be writing their next request in it. */
async function aiApply(text) {
  const box = $("ai-instruction");
  const fromComposer = typeof text !== "string";
  const instruction = (fromComposer ? box.value : text).trim();
  if (!instruction || aiJob) return;
  aiCancelPick();
  const atStep = stepIdx;          // "this step" means the step shown at submit time
  const selection = aiSelection;   // …and "this element" the one pointed at then
  // the AI edits the saved file, so unsaved manual edits go first — and a
  // save the validator rejects means nothing runs
  if (dirty && !(await saveWalkthrough())) {
    aiAddNote("bad", "Not sent: your unsaved step edits could not be saved. Fix the error in the Step tab first.");
    return;
  }
  if (fromComposer) {
    box.value = "";
    aiGrow(box);
  }
  aiSuggestFresh = false;
  aiAddYouTurn(instruction, atStep, selection);
  const job = {
    id: null, instruction, selection, startedAt: Date.now(), stopping: false,
    lastSeq: 0, lines: [], now: "Sending your request", placeholder: true, turn: aiAddRunTurn(),
  };
  aiJob = job;
  aiPaintRun(job);
  aiSyncBusy();

  const payload = selection ? {
    page: selection.page, selector: selection.selector, text: selection.text,
    text_exact: selection.text_exact, tag: selection.tag,
    matches: selection.matches, html: selection.html,
  } : null;
  try {
    const r = await fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}/ai-edit?background=1`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction, current_step: atStep, selection: payload }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const busy = r.status === 409;
      return aiFinish(job, { state: "error", busy,
        error: busy ? "Another AI task is already running on this walkthrough. Wait for it to finish, then try again."
                    : d.error || `The request failed (${r.status}).` });
    }
    job.id = d.job_id;
  } catch (e) {
    // The POST may or may not have reached the server. If it did, the job is
    // listed — follow it rather than guessing.
    const running = await aiFindRunningJob();
    if (!running) return aiFinish(job, { state: "error", error: "Could not reach the server. Check your connection and try again." });
    job.id = running.job_id;
  }
  aiSyncBusy();   // Stop goes live once there is a job to stop
  aiFinish(job, await aiWatch(job));
}

async function aiFindRunningJob() {
  try {
    const r = await fetch("/api/jobs");
    if (!r.ok) return null;
    const running = await r.json();
    return running.find((j) => j.kind === "ai-edit" && j.walkthrough_id === walkthroughId) || null;
  } catch (_) {
    return null;
  }
}

function aiCloseStream() {
  if (aiSource) { aiSource.close(); aiSource = null; }
}

/* Resolves with the job's terminal snapshot. The event stream is the fast
 * path; if it is missing or drops, the job is still running server-side and
 * still readable by id, so poll for it instead of stranding the turn. */
function aiWatch(job) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (snap) => {
      if (settled) return;
      settled = true;
      aiCloseStream();
      resolve(snap);
    };
    const poll = async () => {
      while (!settled) {
        try {
          const r = await fetch(`/api/jobs/${encodeURIComponent(job.id)}`);
          if (r.status === 404) {
            return done({ state: "error", lost: true,
              error: "Lost track of this request. It may still have finished." });
          }
          if (r.ok) {
            const snap = await r.json();
            for (const e of snap.events || []) aiOnProgress(job, e);
            if (snap.state !== "running") return done(snap);
          }
        } catch (_) { /* offline for a moment: keep trying */ }
        await new Promise((res) => setTimeout(res, 2000));
      }
    };
    let src;
    try {
      src = new EventSource(`/api/jobs/${encodeURIComponent(job.id)}/events`);
    } catch (_) {
      poll();
      return;
    }
    aiSource = src;
    src.addEventListener("progress", (ev) => {
      try { aiOnProgress(job, JSON.parse(ev.data)); } catch (_) { /* not worth failing over */ }
    });
    src.addEventListener("done", (ev) => {
      let snap = null;
      try { snap = JSON.parse(ev.data); } catch (_) { /* fall through to polling */ }
      if (snap && snap.state) done(snap);
      else { aiCloseStream(); poll(); }
    });
    src.onerror = () => {
      if (settled) return;
      aiCloseStream();
      poll();
    };
  });
}

async function aiStop() {
  const job = aiJob;
  if (!job || !job.id || job.stopping) return;
  job.stopping = true;
  aiPaintRun(job);
  aiSyncBusy();
  try {
    await fetch(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
  } catch (_) { /* the terminal event still says what happened */ }
}

/* ---- results ------------------------------------------------------------ */
async function aiFinish(job, snap) {
  const turn = job.turn;
  const time = aiClock(Date.now() - job.startedAt);
  turn.classList.remove("is-running");
  turn.textContent = "";
  aiJob = null;
  if (snap.state === "done" && (snap.result || {}).kind === "answer") {
    aiRenderAnswer(turn, snap.result, time, job);
  } else if (snap.state === "done") {
    aiRenderEdit(turn, snap.result || {}, time, job);
  } else if (snap.state === "cancelled") {
    // A cancel can land after the save — the post-save sweep reports
    // progress, and progress is where a cancel is raised.
    aiRenderStopped(turn, await aiAdoptIfChanged(), time, job);
  } else {
    aiRenderError(turn, snap, job, snap.lost ? await aiAdoptIfChanged() : false);
  }
  aiSyncBusy();
  aiRenderSuggestions();
  aiRevealTurn(turn);
}

// A finished turn is read from its top. Scrolling to the bottom of a long
// answer would open it on its last line.
function aiRevealTurn(turn) {
  const t = $("ai-thread");
  const tr = t.getBoundingClientRect();
  const r = turn.getBoundingClientRect();
  if (r.height > tr.height - 24) t.scrollTop += r.top - tr.top - 12;
  else if (r.bottom > tr.bottom) t.scrollTop += r.bottom - tr.bottom + 14;
}

function aiActivity(job) {
  const lines = job.lines.concat(job.now && !job.placeholder ? [job.now] : []);
  if (lines.length < 2) return null;
  const d = aiEl("details", "ai-trail");
  const sum = aiEl("summary", null);
  sum.append(aiIcon("chevron"), document.createTextNode(`What it did (${lines.length})`));
  const ol = aiEl("ol", "ai-activity");
  for (const line of lines) ol.append(aiEl("li", null, line));
  d.append(sum, ol);
  return d;
}

// A page an edit rewrote changes every step that plays on it, whether or not
// the step's own fields moved.
function aiPageAt(wt, i) {
  for (let k = i; k >= 0; k--) if (wt.steps[k].page) return wt.steps[k].page;
  return null;
}

function aiRenderEdit(turn, res, time, job) {
  const fresh = res.walkthrough || walkthrough;
  const prev = new Map(walkthrough.steps.map((s) => [s.id, JSON.stringify(s)]));
  const pages = new Set(res.pages_changed_paths || []);
  aiChanged = new Set();
  fresh.steps.forEach((s, i) => {
    if (prev.get(s.id) !== JSON.stringify(s) || pages.has(aiPageAt(fresh, i))) aiChanged.add(i);
  });
  lastAiStamp = res.stamp || null;
  // The edit may have rewritten the very markup that selector matched
  // (capture_edits apply to the snapshots), so it is retired with its
  // instruction rather than left pointing at a page that has moved on.
  if (aiSelection && aiSelection === job.selection) aiClearSelection();
  if (res.walkthrough) adoptWalkthrough(res.walkthrough);   // after aiChanged: the rail repaints from it

  turn.classList.add("is-edit");
  aiHead(turn, "edit", "changed", time);
  turn.append(aiEl("p", "ai-prose", res.summary || "The walkthrough was updated."));

  if (aiChanged.size) {
    const list = aiEl("div", "ai-steps");
    list.append(aiEl("div", "ai-mini-label", aiChanged.size === 1 ? "step changed" : "steps changed"));
    const chips = aiEl("div", "ai-step-chips");
    for (const i of [...aiChanged].sort((a, b) => a - b)) {
      const b = aiEl("button", "ai-step-chip");
      b.type = "button";
      b.title = `Go to step ${i + 1}`;
      b.append(aiEl("span", "ai-step-n", String(i + 1)),
               aiEl("span", "ai-step-t", walkthrough.steps[i].title || `Step ${i + 1}`));
      b.addEventListener("click", () => gotoStep(i));
      chips.append(b);
    }
    list.append(chips);
    turn.append(list);
  }

  const meta = [];
  if (res.steps_before !== res.steps_after) meta.push(`${res.steps_before} → ${res.steps_after} steps`);
  if (res.pages_changed) meta.push(`${res.pages_changed} captured page${res.pages_changed === 1 ? "" : "s"} rewritten`);
  if (meta.length) turn.append(aiEl("p", "ai-meta", meta.join(" · ")));

  const actions = aiEl("div", "ai-actions");
  if (lastAiStamp) {
    const undo = aiEl("button", "ai-link ai-undo");
    undo.type = "button";
    undo.dataset.undoStamp = lastAiStamp;
    undo.append(aiIcon("undo"), document.createTextNode("Undo this change"));
    undo.addEventListener("click", () => aiUndo(undo.dataset.undoStamp, turn));
    actions.append(undo);
  }
  turn.append(actions);
  // The validator's warnings are folded away, like the trail below them: the
  // summary and the changed steps are what the author reads, and a list of
  // replay-check notes on every edit buried both.
  if ((res.warnings || []).length) {
    const fold = aiEl("details", "ai-trail");
    const sum = aiEl("summary", null);
    sum.append(aiIcon("chevron"), document.createTextNode(`Worth checking (${res.warnings.length})`));
    const box = aiEl("div", "ai-warns");
    for (const w of res.warnings) box.append(aiEl("p", "ai-warn", w));
    fold.append(sum, box);
    turn.append(fold);
  }
  const trail = aiActivity(job);
  if (trail) turn.append(trail);
}

function aiRenderAnswer(turn, res, time, job) {
  turn.classList.add("is-answer");
  aiHead(turn, "answer", "answer", time);
  turn.append(aiEl("p", "ai-prose", res.answer || ""));
  const suggestion = (res.suggested_change || "").trim();
  if (suggestion) {
    const card = aiEl("div", "ai-suggestion");
    card.append(aiEl("div", "ai-mini-label", "suggested change"),
                aiEl("p", "ai-suggestion-text", suggestion));
    const row = aiEl("div", "ai-actions");
    const go = aiEl("button", "btn btn-secondary btn-sm ai-suggestion-go", "Make this change");
    go.type = "button";
    go.addEventListener("click", () => aiApply(suggestion));
    const edit = aiEl("button", "ai-link", "Edit first");
    edit.type = "button";
    edit.addEventListener("click", () => aiToComposer(suggestion));
    row.append(go, edit);
    card.append(row);
    turn.append(card);
  }
  turn.append(aiEl("p", "ai-meta", "Nothing was changed."));
  const trail = aiActivity(job);
  if (trail) turn.append(trail);
}

function aiRenderStopped(turn, saved, time, job) {
  turn.classList.add("is-stopped");
  aiHead(turn, "stopped", "stopped", time);
  turn.append(aiEl("p", "ai-prose", saved
    ? "It had already saved its change when the stop landed, so the change is in. The page now shows it."
    : "Stopped before anything was saved. Nothing was changed."));
  if (!saved) turn.append(aiRetryRow(job));
}

function aiRenderError(turn, snap, job, saved) {
  turn.classList.add("is-error");
  aiHead(turn, "error", "didn't finish", null);
  turn.append(aiEl("p", "ai-prose", snap.error || "The request failed."));
  if (saved) turn.append(aiEl("p", "ai-meta", "It had saved a change before this happened. The page now shows the saved version."));
  else turn.append(aiEl("p", "ai-meta", "Nothing was changed."));
  if (!saved) turn.append(aiRetryRow(job));
}

function aiRetryRow(job) {
  const row = aiEl("div", "ai-actions");
  if (!job.instruction) return row;   // a resumed job: the words never reached this tab
  const again = aiEl("button", "ai-link", "Try again");
  again.type = "button";
  again.addEventListener("click", () => {
    if (aiJob) return;
    aiRestoreSelection(job.selection);
    aiApply(job.instruction);
  });
  const edit = aiEl("button", "ai-link", "Edit and resend");
  edit.type = "button";
  edit.addEventListener("click", () => {
    aiRestoreSelection(job.selection);
    aiToComposer(job.instruction);
  });
  row.append(again, edit);
  return row;
}

function aiRestoreSelection(sel) {
  if (!sel) return;
  aiSelection = sel;
  aiSyncMark();
  aiRenderPickControls();
}

function aiToComposer(text) {
  const box = $("ai-instruction");
  box.value = text;
  aiGrow(box);
  aiSyncComposer();
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}

/* Key order is not a change, so compare canonically. */
function aiCanon(v) {
  if (Array.isArray(v)) return `[${v.map(aiCanon).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${aiCanon(v[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}

/* After a stop or a lost stream: whatever the outcome looked like, the saved
 * copy is the truth. Adopt it if it moved. */
async function aiAdoptIfChanged() {
  try {
    const r = await fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}`);
    if (!r.ok) return false;
    const fresh = await r.json();
    if (aiCanon(fresh) === aiCanon(walkthrough)) return false;
    adoptWalkthrough(fresh);
    return true;
  } catch (_) {
    return false;
  }
}

/* ---- busy state ----------------------------------------------------------
 * While a request runs the composer stays open — the author can write the
 * next one — and Send becomes Stop. The Step tab locks instead: the result
 * replaces the whole walkthrough, and a field typed into meanwhile would be
 * silently thrown away. */
function aiSyncBusy() {
  const busy = !!aiJob;
  $("ai-apply").classList.toggle("hidden", busy);
  $("ai-stop").classList.toggle("hidden", !busy);
  $("ai-stop").disabled = !busy || !aiJob.id || aiJob.stopping;
  $("ai-stop").textContent = busy && aiJob.stopping ? "Stopping…" : "Stop";
  $("rail-tabs").classList.toggle("ai-running", busy);
  $("tab-ai-dot").title = busy ? "AI assist is working" : "";
  $("editor").inert = busy;
  $("ed-ai-lock").classList.toggle("hidden", !busy);
  clearInterval(aiTicker);
  aiTicker = busy ? setInterval(aiTick, 1000) : null;
  aiSyncComposer();
  aiSyncUndo();
}

function aiSyncComposer() {
  $("ai-apply").disabled = !$("ai-instruction").value.trim() || !!aiJob;
  aiRenderSuggestions();
}

// Only the newest edit can be undone, and only while nothing has been saved
// on top of it — undoing then would discard the edits saved since.
function aiSyncUndo() {
  for (const b of document.querySelectorAll("#ai-thread [data-undo-stamp]")) {
    b.classList.toggle("hidden", b.dataset.undoStamp !== lastAiStamp);
    b.disabled = !!aiJob || aiUndoing;
  }
}

function aiGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(200, Math.max(44, el.scrollHeight)) + "px";
}

async function aiUndo(stamp, turn) {
  if (!stamp || stamp !== lastAiStamp || aiJob || aiUndoing) return;
  aiUndoing = true;
  aiSyncUndo();
  try {
    const r = await fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stamp }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      aiAddNote("bad", d.error || `Undo failed (${r.status}).`);
      return;
    }
    lastAiStamp = null;
    aiChanged = new Set();
    adoptWalkthrough(d.walkthrough);
    const actions = turn && turn.querySelector(".ai-actions");
    if (actions) {
      actions.textContent = "";
      actions.append(aiEl("span", "ai-undone", "undone"));
    }
    if (turn) turn.classList.add("is-undone");
    aiAddNote(null, "Restored the version from before this change.");
  } catch (e) {
    aiAddNote("bad", "Undo failed: " + e);
  } finally {
    aiUndoing = false;
    aiSyncUndo();
  }
}

/* A request survives a reload — it runs on the server — so entering the
 * editor checks for one and follows it, instead of offering a Send that
 * would only be refused with a 409. */
async function aiResume() {
  if (EMBED || aiJob || aiResumeChecked) return;
  aiResumeChecked = true;
  const running = await aiFindRunningJob();
  if (!running || aiJob) return;
  if (mode === "edit") railTab("ai");
  aiAddNote(null, "Picking up a request that was already running.");
  const job = {
    id: running.job_id, instruction: "", selection: null,
    startedAt: Date.now() - (running.elapsed || 0) * 1000,
    stopping: !!running.cancel_requested, lastSeq: 0, lines: [],
    now: "Reconnecting", placeholder: true, turn: aiAddRunTurn(),
  };
  aiJob = job;
  aiPaintRun(job);
  aiSyncBusy();
  aiFinish(job, await aiWatch(job));
}

function initAiPanel() {
  if (EMBED) return;   // exports are playback-only; the tab never shows
  const box = $("ai-instruction");
  // Enter sends, Shift+Enter is a new line — what every chat box does. The
  // old Cmd/Ctrl+Enter still works for anyone with it in their fingers.
  box.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    if (e.shiftKey && !(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    aiApply();
  });
  box.addEventListener("input", () => { aiGrow(box); aiSyncComposer(); });
  $("ai-kbd").textContent = "↵ to send";
  $("ai-kbd").title = "Enter sends. Shift+Enter adds a line.";
  // While picking, Escape backs out of the pick, wherever focus is in the
  // editor. Inside the captured page the bridge answers it instead.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && aiPicking) { e.preventDefault(); aiCancelPick(); }
  });
  $("ai-suggest").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-suggest]");
    if (b) aiToComposer(b.dataset.suggest);
  });
  aiGrow(box);
  aiSyncBusy();
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
