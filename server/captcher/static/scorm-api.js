/* scorm-api.js — SCORM RTE wrapper for exported walkthroughs.
 * Supports both SCORM 2004 (the API_1484_11 RTE) and SCORM 1.2 (the older
 * API RTE) — scorm.py sets window.RC_SCORM_VERSION ("2004" or "1.2") in an
 * inline <script> right before this file loads, picking which this package
 * targets. The two RTEs differ in their global object name, method names,
 * and CMI field names (see DRIVERS below); everything past API discovery
 * talks to a small per-version `driver` object so the actual walkthrough-
 * tracking logic doesn't need to branch.
 *
 * Loaded by scorm.py's SCORM export BEFORE player.js (script tags run in
 * document order), so it can:
 *   1. Find the LMS's SCORM API object and call Initialize()/LMSInitialize()
 *      synchronously.
 *   2. Read any saved progress and set window.RC_RESUME = {mode, stepIdx}
 *      before player.js's own boot code runs and reads it.
 *   3. Listen for player.js's generic "captcher:step" / "captcher:complete" CustomEvents
 *      (dispatched from static/player.js) to report progress/completion.
 *   4. Terminate cleanly on unload, saving enough to resume next launch.
 *
 * If no SCORM API is found (opened outside an LMS, e.g. double-clicked),
 * every call below is a no-op — the walkthrough still plays fine standalone.
 */
"use strict";

(function () {
  var VERSION = window.RC_SCORM_VERSION === "1.2" ? "1.2" : "2004";

  function findAPI(win, propName) {
    var attempts = 0;
    while (!win[propName] && win.parent && win.parent !== win && attempts < 500) {
      attempts++;
      try { win = win.parent; } catch (e) { break; }
    }
    return win[propName] || null;
  }

  function getAPI(propName) {
    var api = null;
    try { api = findAPI(window, propName); } catch (e) { /* cross-origin: give up this path */ }
    if (!api && window.opener) {
      try { api = findAPI(window.opener, propName); } catch (e) { /* same */ }
    }
    return api;
  }

  function ok(result) { return result !== "" && result !== "false"; }

  // ISO 8601 duration (SCORM 2004 cmi.session_time), e.g. "PT1M30S"
  function isoDuration(ms) {
    var totalSeconds = Math.max(0, Math.round(ms / 1000));
    var h = Math.floor(totalSeconds / 3600);
    var m = Math.floor((totalSeconds % 3600) / 60);
    var s = totalSeconds % 60;
    var out = "P";
    if (h) out += h + "H";
    out += "T" + m + "M" + s + "S";
    return out;
  }

  // CMITimespan (SCORM 1.2 cmi.core.session_time), HH:MM:SS.SS — NOT ISO 8601
  function cmiTimespan(ms) {
    var pad = function (n) { n = String(n); return n.length < 2 ? "0" + n : n; };
    var totalCentiseconds = Math.max(0, Math.round(ms / 10));
    var cs = totalCentiseconds % 100;
    var totalSeconds = Math.floor(totalCentiseconds / 100);
    var s = totalSeconds % 60;
    var totalMinutes = Math.floor(totalSeconds / 60);
    var m = totalMinutes % 60;
    var h = Math.floor(totalMinutes / 60);
    return pad(h) + ":" + pad(m) + ":" + pad(s) + "." + pad(cs);
  }

  // Per-version adapter: normalizes method names and CMI field names so the
  // tracking logic below is written once, against `driver`, regardless of RTE.
  var DRIVERS = {
    "2004": function (raw) {
      return {
        init: function () { return ok(raw.Initialize("")); },
        getValue: function (k) { return raw.GetValue(k); },
        setValue: function (k, v) { raw.SetValue(k, v); },
        commit: function () { raw.Commit(""); },
        terminate: function () { raw.Terminate(""); },
        location: "cmi.location",
        status: "cmi.completion_status",
        exit: "cmi.exit",
        sessionTime: "cmi.session_time",
        formatSessionTime: isoDuration,
        completedValue: "completed",
        incompleteValue: "incomplete",
        notStartedValues: ["not attempted", "unknown", ""],
        exitSuspendValue: "suspend",
        exitNormalValue: "",
      };
    },
    "1.2": function (raw) {
      return {
        init: function () { return ok(raw.LMSInitialize("")); },
        getValue: function (k) { return raw.LMSGetValue(k); },
        setValue: function (k, v) { raw.LMSSetValue(k, v); },
        commit: function () { raw.LMSCommit(""); },
        terminate: function () { raw.LMSFinish(""); },
        location: "cmi.core.lesson_location",
        status: "cmi.core.lesson_status",
        exit: "cmi.core.exit",
        sessionTime: "cmi.core.session_time",
        formatSessionTime: cmiTimespan,
        completedValue: "completed",
        incompleteValue: "incomplete",
        notStartedValues: ["not attempted", ""],
        exitSuspendValue: "suspend",
        exitNormalValue: "",
      };
    },
  };

  var raw = getAPI(VERSION === "1.2" ? "API" : "API_1484_11");
  if (!raw) {
    console.log("[scorm] no SCORM " + VERSION + " API found — playing standalone, progress will not be saved");
    return;
  }

  var driver = DRIVERS[VERSION](raw);
  var terminated = false;
  var completed = false;

  if (!driver.init()) {
    console.log("[scorm] Initialize failed — playing standalone, progress will not be saved");
    driver = null;
    return;
  }

  var startedAt = Date.now();

  // resume: pick up any saved {stepIdx, mode}, before player.js boots.
  // cmi.suspend_data is the same field name in both SCORM 1.2 and 2004.
  try {
    var saved = driver.getValue("cmi.suspend_data");
    if (saved) {
      var parsed = JSON.parse(saved);
      if (parsed && Number.isInteger(parsed.stepIdx) && (parsed.mode === "show" || parsed.mode === "try")) {
        window.RC_RESUME = { mode: parsed.mode, stepIdx: parsed.stepIdx };
      }
    }
  } catch (e) { /* malformed/missing suspend_data: start from the intro */ }

  // signal "the learner has launched this" unless the LMS already has a
  // more specific status (e.g. re-launching an already-completed SCO)
  var status = driver.getValue(driver.status);
  if (driver.notStartedValues.indexOf(status) !== -1) {
    driver.setValue(driver.status, driver.incompleteValue);
    driver.commit();
  }
  if (status === "completed") completed = true;

  window.addEventListener("captcher:step", function (e) {
    if (!driver || completed) return;
    var d = e.detail || {};
    driver.setValue(driver.location, String(d.index));
    try {
      driver.setValue("cmi.suspend_data", JSON.stringify({ stepIdx: d.index, mode: d.mode }));
    } catch (err) { /* ignore */ }
    driver.commit();
  });

  window.addEventListener("captcher:complete", function () {
    if (!driver) return;
    completed = true;
    driver.setValue(driver.status, driver.completedValue);
    driver.commit();
  });

  function terminate() {
    if (!driver || terminated) return;
    terminated = true;
    try {
      driver.setValue(driver.sessionTime, driver.formatSessionTime(Date.now() - startedAt));
      driver.setValue(driver.exit, completed ? driver.exitNormalValue : driver.exitSuspendValue);
      driver.commit();
      driver.terminate();
    } catch (e) { /* best-effort: the window is closing anyway */ }
  }

  window.addEventListener("pagehide", terminate);
  window.addEventListener("beforeunload", terminate);
})();
