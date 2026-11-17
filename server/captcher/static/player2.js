/* player2.js — the walkthrough player served at /player/ (popup instructions).
 * Forked from player.js: instructions float in a card over the stage, next to
 * the element the step is about, instead of a fixed side panel. This player is
 * PLAYBACK-ONLY — editing (and everything that writes the walkthrough) stays
 * in the classic player, reached via /player/<id>?edit=1 or /player1/<id>.
 *
 * Modes: "show" (animated demonstration) and "try" (learner performs actions).
 * Talks to bridge.js inside the capture iframe via postMessage. The card is
 * positioned from {cmd:"track"} / {type:"target-rect"} rect reports — the card
 * itself never enters the captured page (brand governs chrome only).
 */
"use strict";

/* Standalone export support: export.py bakes the walkthrough definition and all
 * captured pages into a JSON block with id "sc-embed". When present, the
 * player runs fully offline — no /api or /capture requests, pages load via
 * iframe srcdoc. Downloaded exports still build on v1; public landing-page
 * examples use this v2 path so they match normal playback. */
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
let mode = "show";          // "show" | "try"
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

/* pacing is suspended while nobody is watching — see watchVisibility() */
let paused = false;
let onScreen = true;

/* ---- button countdowns ----
 * One helper drives every timed action (Start now, Continue, Finish): the
 * button shows "label · Ns", ticks down, fires onDone at zero. Clicking the
 * button runs its normal onclick, which cancels the countdown — so a click
 * always means "do it now". */
const countdowns = new Map();   // button el -> { tick, timer, label, onDone, due }

function buttonCountdown(btn, label, ms, onDone) {
  cancelCountdown(btn);
  btn.classList.remove("hidden");
  if (paused) {                 // off screen: show the button, start no clock
    btn.textContent = label;
    countdowns.set(btn, { tick: null, timer: null, label, onDone, due: null, left: ms });
    return;
  }
  let remaining = Math.max(1, Math.ceil(ms / 1000));
  btn.textContent = `${label} · ${remaining}s`;
  btn.title = "Click to go now";
  btn.classList.add("counting");
  const tick = setInterval(() => {
    remaining--;
    if (remaining > 0) btn.textContent = `${label} · ${remaining}s`;
  }, 1000);
  const timer = setTimeout(() => { cancelCountdown(btn); onDone(); }, ms);
  countdowns.set(btn, { tick, timer, label, onDone, due: Date.now() + ms });
}

function cancelCountdown(btn) {
  const c = countdowns.get(btn);
  if (c) {
    if (c.tick) clearInterval(c.tick);
    if (c.timer) clearTimeout(c.timer);
    countdowns.delete(btn);
  }
  btn.classList.remove("counting");
  btn.removeAttribute("title");
}

/* ---- silent waits ----
 * With step buttons hidden (the default), the same pacing waits run with no
 * button to show them on. One slot is enough — a reading pause and an advance
 * pause never run at once (pendingShowme says which kind is live). Enter/Space
 * still skips the wait, exactly like clicking the button it replaces. */
let silentWait = null;   // { timer, onDone, due } — timer null while paused

function startSilentWait(ms, onDone) {
  clearSilentWait();
  if (paused) { silentWait = { timer: null, onDone, due: null, left: ms }; return; }
  silentWait = {
    timer: setTimeout(() => { silentWait = null; onDone(); }, ms),
    onDone,
    due: Date.now() + ms,
  };
}

function clearSilentWait() {
  if (silentWait) { if (silentWait.timer) clearTimeout(silentWait.timer); silentWait = null; }
}

function skipSilentWait() {
  if (!silentWait) return false;
  const done = silentWait.onDone;
  clearSilentWait();
  done();
  return true;
}

/* ---- pause while nobody is watching ----
 * The player autoplays, so an embed that scrolls out of the reader's view is
 * a walkthrough running to nobody: they come back to a finished demo, or get
 * pulled around by it. So every pacing clock stops when the player leaves the
 * screen and picks up where it left off when it comes back.
 *
 * It pauses at the seams, not mid-motion: a show-me already animating plays
 * out (a second or two) and the hold happens before the next step. Stopping a
 * cursor halfway across a form would need the bridge to unwind its own
 * animation, and buys nothing a reader would notice.
 *
 * Two ways to be unwatched, one rule: scrolled out of the host page (the
 * implicit IntersectionObserver root is the top-level viewport, so this sees
 * the host's scrolling even from inside the iframe), or on a tab that is not
 * frontmost. Where IntersectionObserver is missing the player simply never
 * pauses, which is the behaviour it had before. */
function pausePlayback() {
  if (paused) return;
  paused = true;
  const now = Date.now();
  if (silentWait && silentWait.timer) {
    clearTimeout(silentWait.timer);
    silentWait.left = Math.max(0, silentWait.due - now);
    silentWait.timer = null;
  }
  countdowns.forEach((c, btn) => {
    if (!c.timer) return;
    clearInterval(c.tick); clearTimeout(c.timer);
    c.tick = c.timer = null;
    c.left = Math.max(0, c.due - now);
    // The button stays visible and clickable: a reader who scrolls back and
    // wants to go on should not have to wait out a clock that stopped.
    btn.textContent = c.label;
    btn.classList.remove("counting");
  });
}

function resumePlayback() {
  if (!paused) return;
  paused = false;
  if (silentWait && !silentWait.timer) {
    const { onDone, left } = silentWait;
    silentWait = null;
    startSilentWait(left, onDone);
  }
  Array.from(countdowns.entries()).forEach(([btn, c]) => {
    if (c.timer) return;
    countdowns.delete(btn);
    buttonCountdown(btn, c.label, c.left, c.onDone);
  });
}

function watchVisibility() {
  const onHidden = () => pausePlayback();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) onHidden();
    else if (onScreen) resumePlayback();
  });
  if (typeof IntersectionObserver !== "function") return;
  new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      // Half of it showing is the line: less than that and the reader has
      // moved on to the copy below, which is exactly the case that used to
      // drag them back up.
      onScreen = e.isIntersecting && e.intersectionRatio >= 0.5;
      if (onScreen && !document.hidden) resumePlayback();
      else onHidden();
    });
  }, { threshold: [0, 0.5, 1] }).observe(document.documentElement);
}

watchVisibility();

/* playback settings (persisted across walkthroughs)
 * localStorage can throw entirely (sandboxed iframes, opaque origins — e.g.
 * an exported walkthrough embedded in an LMS). Settings then simply don't persist. */
const storeGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const storeSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* no persistence */ } };

/* The five demo speeds, shared by the topbar slider (by index) and the
 * settings modal's <select> (by value) — one list so the two cannot drift. */
const SPEED_STEPS = [0.5, 0.75, 1, 1.5, 2];

function nearestSpeedIndex(v) {
  let best = 2; // 1×
  SPEED_STEPS.forEach((s, i) => {
    if (Math.abs(s - v) < Math.abs(SPEED_STEPS[best] - v)) best = i;
  });
  return best;
}

/* Playback preferences, per browser. The "sc-" prefix replaced "rc-" at an
 * earlier rename with no fallback read: these are four playback
 * settings a learner re-picks in seconds, and carrying a second key forever to
 * save them one click is not a trade worth making. The catalog's collapse key
 * IS carried forward — see templates/_catalog-script.html for why the two
 * decisions differ. */
const settings = {
  speed: SPEED_STEPS[nearestSpeedIndex(parseFloat(storeGet("sc-speed")) || 1)],
  autoAdvance: storeGet("sc-auto") !== "0",
  startDelay: storeGet("sc-start-delay") || "auto", // "auto" | seconds
  nextDelay: parseFloat(storeGet("sc-next-delay")) || 2, // seconds
  // Default OFF: the card is pure text and the pacing timers run silently.
  // Only buttons that merely pre-empt a timer obey this — a button that is
  // the sole way forward (Hint, Continue with auto-advance off, an observe
  // step in try mode) always shows.
  showButtons: storeGet("sc-buttons") === "1",
};

function initSettings() {
  const speedSel = $("opt-speed");
  const autoChk = $("opt-auto");
  const startSel = $("opt-start-delay");
  const nextSel = $("opt-next-delay");
  speedSel.value = String(settings.speed); // always valid: snapped to SPEED_STEPS on boot
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
    // restart a running advance wait — the visible countdown or its silent twin
    if (countdowns.has($("btn-next")) || (silentWait && !pendingShowme)) scheduleAdvance();
  });

  speedSel.addEventListener("change", () => {
    applySpeed(parseFloat(speedSel.value) || 1);
  });
  $("tb-speed-slider").addEventListener("input", () => {
    applySpeed(SPEED_STEPS[Number($("tb-speed-slider").value)] || 1);
  });
  syncSpeedUI();
  autoChk.addEventListener("change", () => {
    settings.autoAdvance = autoChk.checked;
    storeSet("sc-auto", autoChk.checked ? "1" : "0");
    if (!settings.autoAdvance && !pendingShowme &&
        (countdowns.has($("btn-next")) || silentWait)) {
      // an advance was already running (counting down or silent) — hand
      // control back to the user
      cancelCountdown($("btn-next"));
      clearSilentWait();
      showContinueButton();
    }
    if (pendingShowme) {
      // a demo is waiting to start — restage it under the new policy
      // (countdown or silent wait when auto, ▶ Play when manual)
      const step = pendingShowme;
      clearStartPause();
      beginShowme(step);
    }
    syncSettingsVisibility(); // wait fields dim when auto-advance is off
  });

  // Step buttons on/off. Whatever wait is currently running is restaged at
  // once under the new policy, so the toggle can be judged live.
  const btnChk = $("opt-buttons");
  btnChk.checked = settings.showButtons;
  btnChk.addEventListener("change", () => {
    settings.showButtons = btnChk.checked;
    storeSet("sc-buttons", btnChk.checked ? "1" : "0");
    $("btn-replay").classList.toggle("hidden", mode !== "show" || !settings.showButtons);
    if (pendingShowme) {
      const step = pendingShowme;
      clearStartPause();
      beginShowme(step);
    } else if (countdowns.has($("btn-next")) || silentWait) {
      clearAdvance();
      scheduleAdvance();
    }
  });

  // Not a viewer preference: the shape belongs to the walkthrough. This player
  // is playback-only, so the choice is always a session override — tried out,
  // never written anywhere. Saving a shape happens in the v1 player's editor.
  // Pushed to the live page at once so the choice can be judged against the
  // real captured UI, not the label.
  $("opt-cursor").addEventListener("change", () => {
    cursorOverride = $("opt-cursor").value;
    if (bridgeReady) sim.contentWindow.postMessage(bridgeConfig(), "*");
  });
}

/* One writer for the demo speed, wherever it was changed (topbar slider or
 * settings select): store it, mirror both controls, and tell a live bridge.
 * Only an already-live bridge is told directly — every fresh page gets the
 * speed on bridge-ready, and queueing via sendToBridge would clobber a
 * showme/expect waiting for the page to load. */
function applySpeed(v) {
  settings.speed = v;
  storeSet("sc-speed", String(v));
  $("opt-speed").value = String(v);
  syncSpeedUI();
  if (bridgeReady) sim.contentWindow.postMessage(bridgeConfig(), "*");
}

function syncSpeedUI() {
  $("tb-speed-slider").value = String(nearestSpeedIndex(settings.speed));
  $("tb-speed-val").textContent = `${settings.speed}×`;
  // speed only shapes Show-me demos — same dimming as the settings row
  $("tb-speed").classList.toggle("dimmed", mode === "try");
  $("tb-speed-slider").disabled = mode === "try";
}

/* ================= boot ================= */
// The walkthrough starts immediately in the player chrome; the intro
// (scenario/objectives) is a popup over it — see openIntroModal()/dismissIntro().
function afterLoad() {
  renderIntro(); initSettings();
  const viewportOnly = EMBED && EMBED.chrome === "viewport";
  // A host wrapper (e.g. static/scorm-api.js) that runs before this script
  // can set window.RC_RESUME = {mode, stepIdx} to resume a prior session —
  // read once, here, at boot.
  const resume = window.RC_RESUME;
  if (resume && resume.mode && Number.isInteger(resume.stepIdx)) {
    // resuming a prior session: skip the intro popup entirely
    setTimeout(() => startWalkthrough(resume.mode, resume.stepIdx), 0);
  } else if ((EMBED && EMBED.skip_intro) || viewportOnly) {
    // exported without the intro popup — start straight in the walkthrough.
    setTimeout(() => startWalkthrough("show"), 0);
  } else {
    openIntroModal();
  }
  // Deferred via setTimeout above: in the EMBED path this all runs
  // synchronously at script-load time, before later top-level bindings
  // (bridgeWaiter, the message listener, and related state) are declared —
  // starting mid-script would hit a temporal-dead-zone ReferenceError.
}

if (EMBED) {
  // Public examples are full player pages, so they retain the same navigation
  // chrome as /player/<id>. Other embedded builds have no catalog to return to.
  if (!EMBED.example) document.body.classList.add("sc-embedded");
  if (EMBED.chrome === "viewport") document.body.classList.add("sc-viewport-only");
  if (EMBED.example) document.body.classList.add("sc-example");
  walkthrough = EMBED.walkthrough;
  // The standalone export runs this branch while the script is still being
  // evaluated. Defer boot until every later top-level `let`/`const` binding
  // (beatOf, bridge state, etc.) has been initialized.
  setTimeout(afterLoad, 0);
} else {
  fetch(`/api/walkthroughs/${encodeURIComponent(walkthroughId)}`)
    .then((r) => { if (!r.ok) throw new Error("Walkthrough not found"); return r.json(); })
    .then((data) => { walkthrough = data; afterLoad(); })
    .catch((e) => { document.body.innerHTML = `<p style="padding:40px">Could not load walkthrough: ${e.message}</p>`; });
}

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
}

/* ================= walkthrough flow ================= */
function startWalkthrough(m, startIndex) {
  mode = m === "try" ? "try" : "show";   // playback-only: no edit mode here
  stepIdx = 0;
  totalWrong = 0;
  pinnedCard = null;   // a fresh run gets automatic card placement again
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
  // From the final step the walkthrough is effectively over, and "redo the
  // current step in the new mode" would show nothing but the Finish button.
  // Switching modes there means "run the whole thing that way" — the same
  // thing the completion screen's buttons do — so restart from the top.
  if (stepIdx === walkthrough.steps.length - 1) return startWalkthrough(m);
  mode = m;
  syncModeButtons();
  currentPage = null;
  resetWindowView(); // play modes re-resolve the capture window on load
  gotoStep(stepIdx); // redo current step in the new mode
}

/* ================= merged runs =================
 * A step marked `continues: true` runs on from the one before it with no pause
 * between them: the three fields of an address are one thing to teach, not
 * three. The flag sits on the 2nd..Nth step of a run, so a run is always
 * contiguous and an absent flag is exactly the old behaviour.
 *
 * author.validate() rejects the flag on the first step and on any step that
 * loads a page, so a run can never straddle two snapshots — nothing here has
 * to defend against that.
 */
function continuesRun(i) {
  return i > 0 && !!(walkthrough.steps[i] || {}).continues;
}

// Beat numbering: a run counts once, so the learner's counter and progress
// fill both read "one thing to do" where the steps array has three.
// Derived on load, never stored.
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
  if (i >= walkthrough.steps.length) return finishWalkthrough();
  stepIdx = i;
  wrongCount = 0;
  // Hold the card until this step's own rect arrives (trackStepTarget) — set
  // before renderStepCard, whose re-anchor would otherwise use the previous
  // step's rect and send the card there first.
  awaitingRect = true;
  renderStepCard();
  // generic instrumentation hook for a host wrapper (e.g. static/scorm-api.js)
  // to track progress — no listeners in the plain player/embed cases
  window.dispatchEvent(new CustomEvent("captcher:step", { detail: { index: i, total: walkthrough.steps.length, mode } }));

  const step = walkthrough.steps[i];
  // The page the learner is on at step i — steps with page:null continue on
  // the previous page. Resolved (not just step.page) so a mode switch or a
  // resume landing mid-page reloads the right snapshot instead of leaving
  // the iframe on about:blank.
  const page = effectivePage(i);
  if (page && page !== currentPage) {
    loadPage(page, () => actOnStep(step));
  } else {
    actOnStep(step);
  }
}

function effectivePage(i) {
  for (let k = i; k >= 0; k--) if (walkthrough.steps[k].page) return walkthrough.steps[k].page;
  return null;
}

function actOnStep(step) {
  // A step inside a run never takes the reading pause: the learner read the
  // run's instruction at its first step, and re-pausing per field is exactly
  // the stutter merging is meant to remove.
  const skipPause = skipStartPause || continuesRun(stepIdx);
  skipStartPause = false;
  trackStepTarget(step);
  if (step.action.type === "observe") {
    // Nothing to perform — the learner reads the page. With buttons hidden
    // in a show-me run, a reading-length silent wait keeps the walkthrough
    // flowing; everywhere else — including the last step, which never
    // advances on its own — the Continue button is the only way forward.
    if (mode === "show" && settings.autoAdvance && !settings.showButtons &&
        !lastStepWaits()) {
      startSilentWait(startDelayMs(step), nextStep);
    } else {
      showContinueButton();
    }
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
  // "None" means no reading pause, not "start while off screen": a zero delay
  // still goes through the wait so the pause below can hold it.
  if (delay <= 0) return paused ? startSilentWait(0, startShowmeNow) : startShowmeNow();
  if (!settings.showButtons) return startSilentWait(delay, startShowmeNow);
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
  clearSilentWait();
  btn.classList.add("hidden");
}

function clearAdvance() {
  const btn = $("btn-next");
  cancelCountdown(btn);
  clearSilentWait();
  btn.classList.add("hidden");
}

// The end of the walkthrough is never automatic: pacing carries the learner
// between steps, but leaving the last one is their decision, so it always
// waits on the Finish button. Viewport-only embeds are the exception — they
// hide the card (no button to click) and loop their demo by design.
function lastStepWaits() {
  return stepIdx === walkthrough.steps.length - 1 &&
         !(EMBED && EMBED.chrome === "viewport");
}

/* auto-advance with a visible, skippable countdown on the Continue button —
 * wait length comes from the user's Continue-wait setting */
function scheduleAdvance() {
  if (lastStepWaits()) return showContinueButton();
  if (!settings.showButtons) return startSilentWait(settings.nextDelay * 1000, nextStep);
  buttonCountdown($("btn-next"), "Continue →", settings.nextDelay * 1000, nextStep);
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
    // No chrome to show a completion card in, and no host modal to close —
    // loop the demonstration instead, like a silent, self-running preview.
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
    sim.src = captureUrl(page);
    watchPageLoad(page);
  }
  // bridge-ready message (not iframe onload) is the real signal
  waitForBridge(onReady);
}

function captureUrl(page) {
  return `/capture/${encodeURIComponent(walkthroughId)}/${page.split("/").map(encodeURIComponent).join("/")}`;
}

/* A snapshot that never announces its bridge — the request dropped by a
 * server restart or a network blip — used to leave the player waiting
 * forever: the previous page on screen under the new step's card, and no
 * word about why. Try the load once more, then say so. Live player only; an
 * export's pages are inline and cannot fail this way. */
const PAGE_LOAD_MS = 10000;
let pageLoadTimer = null;
function watchPageLoad(page, retried) {
  clearTimeout(pageLoadTimer);
  pageLoadTimer = setTimeout(() => {
    if (bridgeReady || currentPage !== page) return;
    if (!retried) {
      sim.src = captureUrl(page) + `?retry=${Date.now()}`;
      watchPageLoad(page, true);
    } else {
      feedback("This screen did not load. Check your connection and reload the page to try again.", "bad");
    }
  }, PAGE_LOAD_MS);
}

let bridgeWaiter = null;
function waitForBridge(cb) { bridgeWaiter = cb; }

/* A cursor chosen during playback: tried out, never written anywhere. Null
 * means "use the walkthrough's own". Deliberately not persisted like the
 * playback preferences beside it — the shape belongs to the walkthrough, and a
 * viewer's experiment must not outlive the tab or leak into someone else's
 * reading of it. */
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
    // The step card floats over the capture and separates from it with a
    // two-tone edge; on a dark page that edge flips to the light side. Per page,
    // because a walkthrough can cross a light app into a dark one.
    document.body.classList.toggle("sc-dark-capture", !!m.dark);
    // configure the fresh page before any queued showme/expect runs
    sim.contentWindow.postMessage(bridgeConfig(), "*");
    if (windowActive()) beginWindowMeasure(); // resolve the capture window on this page
    if (bridgeWaiter) { const cb = bridgeWaiter; bridgeWaiter = null; cb(); }
    if (pendingCmd) { sim.contentWindow.postMessage(pendingCmd, "*"); pendingCmd = null; }
  } else if (m.type === "window-rect") {
    onWindowRect(m);
  } else if (m.type === "target-rect") {
    // a report for an earlier step's target, already on its way when this
    // step's track went out — placing from it is the first of two moves
    if (m.seq !== trackSeq) return;
    awaitingRect = false;
    lastTargetRect = m.rect || null;
    lastAvoidRects = Array.isArray(m.avoid) ? m.avoid : [];
    positionStepCard(lastTargetRect);
  } else if (m.type === "success") {
    onStepSuccess();
  } else if (m.type === "wrong") {
    onWrong(m);
  } else if (m.type === "showme-done") {
    onShowmeDone(m);
  }
});

/* ================= the step card ================= */
/* The card floats over the stage next to the step's target. The element lives
 * inside the capture iframe, so the card cannot measure it directly — bridge.js
 * tracks it ({cmd:"track"}) and reports its iframe-viewport rect here on every
 * scroll/resize. Mapping to stage coordinates goes through the iframe's own
 * box, which already carries the windowed-playback translate/scale. */
let lastTargetRect = null;   // latest iframe-viewport rect, or null (no target)
let lastAvoidRects = [];     // further rects the card must keep off (a drag's drop zone, a select's menu)

/* The card moves once per step. Three things used to move it twice: the
 * re-anchor on render using the previous step's rect, a report for the
 * previous target arriving after this step's track, and Show me scrolling
 * the target to centre after the card had already been placed against it.
 * So: every track carries a sequence number the bridge echoes, reports
 * from any other track are dropped, the card holds still until the first
 * report for this one (awaitingRect), and in Show me the bridge makes the
 * demonstration's scroll *before* it reports — the scroll the cursor makes
 * later is then a no-op. Try mode never scrolls for the learner, so it
 * does not reveal either. */
let trackSeq = 0;
let awaitingRect = false;

function trackStepTarget(step) {
  const target = step.action.type === "observe" ? null : step.action.target;
  // A drag has two ends. The card is placed against the element picked up,
  // but it must not sit on the zone the element is headed for either — that
  // is exactly where "to the right of the target" lands on a board.
  const avoid = step.action.type === "drag" && step.action.drop_target
    ? [step.action.drop_target] : [];
  lastTargetRect = null;
  lastAvoidRects = [];
  trackSeq += 1;
  awaitingRect = !!target;
  sendToBridge({ cmd: "track", target, avoid, seq: trackSeq,
                 reveal: !!target && mode === "show" });
  if (!target) positionStepCard(null);
}

/* ---- placement, as a pure function ----
 * Boxes are in stage coordinates. The card tries right, left, below, above
 * the anchor; covering the anchor or any `avoid` box is the one failure this
 * exists to prevent, so overlap dominates the score and the distance a
 * candidate had to be clamped by breaks ties in favour of the preferred
 * side. Returns the best {left, top}. */
function bestCardPlacement(anchor, avoid, popW, popH, stageW, stageH, pad) {
  const maxL = Math.max(pad, stageW - popW - pad);
  const maxT = Math.max(pad, stageH - popH - pad);
  const { x: ex, y: ey, w: ew, h: eh } = anchor;
  const gap = 18; // clears the spotlight's 1px hairline + 2px ring with air
  const candidates = [
    { left: ex + ew + gap, top: ey + eh / 2 - popH / 2 },   // right
    { left: ex - popW - gap, top: ey + eh / 2 - popH / 2 }, // left
    { left: ex + ew / 2 - popW / 2, top: ey + eh + gap },   // below
    { left: ex + ew / 2 - popW / 2, top: ey - popH - gap }, // above
  ];
  const overlap = (left, top, b) => {
    const ox = Math.max(0, Math.min(left + popW, b.x + b.w) - Math.max(left, b.x));
    const oy = Math.max(0, Math.min(top + popH, b.y + b.h) - Math.max(top, b.y));
    return ox * oy;
  };
  // Covering the target is the worst outcome by far — the learner cannot see
  // what the step is about. An avoid box (a select's menu, a drag's drop
  // zone) matters less: the menu exists only while Show-me has it open. With
  // the two weighted alike, a select near the left edge (Peoplebase's status
  // filter) had its card clamped squarely over it, because the candidate to
  // its right grazed the menu footprint by a little more than the select's
  // own area.
  let best = null;
  for (const c of candidates) {
    const left = Math.min(Math.max(c.left, pad), maxL);
    const top = Math.min(Math.max(c.top, pad), maxT);
    let avoided = 0;
    for (const b of avoid) avoided += overlap(left, top, b);
    const covered = overlap(left, top, anchor) * 1000 + avoided;
    const score = covered * 1000 + Math.abs(left - c.left) + Math.abs(top - c.top);
    if (!best || score < best.score) best = { left, top, score };
    if (score < 0.5) break;   // first candidate that fits untouched wins
  }
  return { left: best.left, top: best.top };
}

function positionStepCard(rect) {
  const pop = $("step-pop");
  if (pop.classList.contains("hidden")) return;
  // between steps: stay put rather than re-anchor to a rect that is not
  // this step's (a pinned card has nowhere to go anyway)
  if (awaitingRect && !pinnedCard) return;
  const stage = document.querySelector(".stage");
  const stageR = stage.getBoundingClientRect();
  const popW = pop.offsetWidth, popH = pop.offsetHeight;
  const pad = 14;
  const maxL = Math.max(pad, stageR.width - popW - pad);
  const maxT = Math.max(pad, stageR.height - popH - pad);
  let best;

  if (pinnedCard) {
    // the learner put it here; keep it here (inside the stage) until they
    // double-click the header to hand placement back
    best = { left: Math.min(Math.max(pinnedCard.left, pad), maxL),
             top: Math.min(Math.max(pinnedCard.top, pad), maxT) };
  } else if (rect) {
    const simR = sim.getBoundingClientRect();
    // windowed playback scales the iframe; its box already includes that
    const scale = sim.offsetWidth ? simR.width / sim.offsetWidth : 1;
    const toStage = (r) => ({ x: simR.left - stageR.left + r.x * scale,
                              y: simR.top - stageR.top + r.y * scale,
                              w: r.width * scale, h: r.height * scale });
    best = bestCardPlacement(toStage(rect), lastAvoidRects.map(toStage),
                             popW, popH, stageR.width, stageR.height, pad);
  } else {
    // no target on this step (observe / not found): hold steady bottom-centre
    best = { left: (stageR.width - popW) / 2, top: maxT };
  }
  pop.style.left = `${Math.round(best.left)}px`;
  pop.style.top = `${Math.round(best.top)}px`;
}

/* ---- moving the card by hand ----
 * Placement is a heuristic, and some page will always defeat it. The header
 * (step number and title) is a grip: drag it and the card stays where it was
 * put for the rest of the walkthrough — re-anchoring a card the learner just
 * moved would read as the player fighting them. Double-click the header to
 * hand placement back. The grip is the header only, so the instruction text
 * stays selectable. */
let pinnedCard = null;   // {left, top} in stage coordinates, or null = automatic
const CARD_GRIP = ".step-eyebrow, h2";

function initCardDrag() {
  const pop = $("step-pop");
  const stage = document.querySelector(".stage");
  const DOUBLE_TAP_MS = 400;
  let drag = null;
  let lastTap = 0;
  for (const el of pop.querySelectorAll(CARD_GRIP)) {
    el.title = "Drag to move the card · double-click to put it back";
  }
  pop.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !e.target.closest(CARD_GRIP)) return;
    const stageR = stage.getBoundingClientRect();
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY,
             left: pop.offsetLeft, top: pop.offsetTop,
             stageW: stageR.width, stageH: stageR.height, moved: false };
    // Capture is what lets the drag cross the captured page: the moment the
    // pointer is over the iframe its events go to that document, and without
    // capture the card would stop at the iframe's edge and never see the
    // release. Capture retargets click/dblclick to the card, so the
    // double-click is detected here from two still releases instead.
    pop.setPointerCapture(e.pointerId);
  });
  pop.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    drag.moved = true;
    pop.classList.add("dragging");
    const pad = 14;
    const left = Math.min(Math.max(drag.left + dx, pad), Math.max(pad, drag.stageW - pop.offsetWidth - pad));
    const top = Math.min(Math.max(drag.top + dy, pad), Math.max(pad, drag.stageH - pop.offsetHeight - pad));
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
  });
  const release = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const moved = drag.moved;
    drag = null;
    pop.classList.remove("dragging");
    if (moved) {
      pinnedCard = { left: pop.offsetLeft, top: pop.offsetTop };
      lastTap = 0;
      return;
    }
    // a still release is a tap; two within DOUBLE_TAP_MS put the card back
    const now = Date.now();
    if (pinnedCard && now - lastTap < DOUBLE_TAP_MS) {
      pinnedCard = null;
      lastTap = 0;
      positionStepCard(lastTargetRect);
    } else {
      lastTap = now;
    }
  };
  pop.addEventListener("pointerup", release);
  pop.addEventListener("pointercancel", release);
}
initCardDrag();

// The card's own size changes as feedback and buttons come and go — re-anchor
// so growth never slides it over the element it points at.
new ResizeObserver(() => positionStepCard(lastTargetRect)).observe($("step-pop"));

/* ================= windowed playback ================= */
/* walkthrough.window = { target?, rect?, viewport?, padding?, label? } —
 * a region of the page picked at capture time (the "capture window"), for
 * inline-help style playback. See player.js for the full notes; this is the
 * same machinery. The frame is FIXED: resolved once on the first page, kept
 * for every following step. The ⛶ toolbar button shows the full page. */
let fullPageView = false;  // learner's ⛶ toggle
let pageWin = null;        // the frozen window region {x,y,w,h}, padded
let pageDoc = null;        // current page document size {width,height}
let winMeasuring = false;  // first measurement pass (at captured viewport size) in flight

function windowDef() {
  const w = walkthrough && walkthrough.window;
  return w && (w.target || w.rect) ? w : null;
}
function windowActive() { return !!windowDef(); }

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
 * life-sized. Fire-and-forget: a plain <iframe> embed ignores the message. */
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

window.addEventListener("resize", () => {
  if (windowActive()) applyView();
  // the bridge re-reports the tracked rect on its own resize; this catches
  // stage-only changes (e.g. the window height moves the fallback anchor)
  positionStepCard(lastTargetRect);
});

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
  if (last && mode === "show") {
    // The demonstration is over. A Finish button only led to one more screen
    // of choices, so offer the two next moves directly; "You try" goes
    // through switchMode, which restarts from the top on the final step.
    // (Try mode keeps Finish — its completion screen carries the run's stats.)
    $("btn-catalog").classList.remove("hidden");
    $("btn-try").classList.remove("hidden");
    return;
  }
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
function renderStepCard() {
  const step = walkthrough.steps[stepIdx];
  $("step-pop").classList.remove("hidden");
  syncSettingsVisibility();
  // Learners are counted in beats: a merged run is one thing to do.
  const beat = beatOf[stepIdx] || 0;
  $("panel-step-num").textContent = `STEP ${pad2(beat + 1)} / ${pad2(beatCount)}`;
  // Each step's own title, even inside a run: the card sits next to the field
  // being worked on, so the heading follows it (the classic player instead
  // holds the run-start title across the run). An author who wants one steady
  // heading gives the run's steps matching titles.
  $("panel-title").textContent = step.title;
  $("panel-instruction").textContent = mode === "show" ? (step.showme_caption || step.instruction) : step.instruction;
  $("panel-feedback").className = "feedback hidden";
  $("btn-next").classList.add("hidden");
  $("btn-play").classList.add("hidden");
  $("btn-catalog").classList.add("hidden");
  $("btn-try").classList.add("hidden");
  $("btn-hint").classList.toggle("hidden", !(mode === "try" && step.action.type !== "observe" && wrongCount >= 2));
  $("btn-replay").classList.toggle("hidden", mode !== "show" || !settings.showButtons);
  $("tb-progress").textContent = `STEP ${pad2(beat + 1)} / ${pad2(beatCount)}`;
  $("progress-fill").style.width = `${(beat / beatCount) * 100}%`;
  // re-anchor now (content size changed); the fresh rect glides it later
  positionStepCard(lastTargetRect);
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
  // with auto-advance off the buttons are the only way forward, so they show
  // regardless of the toggle — dim it to say "not in charge right now"
  $("opt-buttons").closest(".ps-row").classList.toggle("dimmed", noAuto);
  // re-read so a mode switch or a dropped override resyncs it
  $("opt-cursor").value = effectiveCursor();
}

/* Enter / Space triggers the visible primary action (skip wait / continue);
 * Esc closes the settings modal */
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && settingsModalOpen()) { closeSettings(); return; }
  if (settingsModalOpen()) return;
  if (introOpen()) return;   // the player is live behind the intro — don't drive it
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName)) return;
  if ($("player").classList.contains("hidden")) return;
  if (!$("btn-next").classList.contains("hidden")) { e.preventDefault(); nextStep(); }
  else if (!$("btn-play").classList.contains("hidden")) { e.preventDefault(); startShowmeNow(); }
  else if (skipSilentWait()) { e.preventDefault(); } // hidden buttons: same skip, no button
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
  // #intro-close rule in player2.css.
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
  syncSpeedUI(); // the slider dims with the mode, like its settings row
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
