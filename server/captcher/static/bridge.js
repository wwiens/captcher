/* bridge.js — injected into every captured page served via /capture/.
 * Freezes the page (no navigation) and gives the parent player a small
 * command protocol over postMessage:
 *
 *   parent -> child:
 *     {cmd:"config",  speed, cursor}
 *                               speed: playback multiplier for showme (1 = normal)
 *                               cursor: "dot" (brand default) | "pointer"
 *     {cmd:"expect",  action}   arm "You try" validation for one action
 *     {cmd:"showme",  action}   animate the action (cursor, click, type, drag)
 *     {cmd:"hint",    action}   pulse-highlight the action target (both ends of a drag)
 *     {cmd:"clear"}             clear expectation + highlights
 *     {cmd:"probe",   target}   count matches for a target + highlight first
 *     {cmd:"pick"}              enter element-picker mode (editor)
 *     {cmd:"pick-off"}          leave picker mode
 *     {cmd:"mark",    target}   hold an outline on the element the author
 *                               pointed at (null target clears it)
 *     {cmd:"window-rect", target}  resolve the capture window element -> its page rect
 *     {cmd:"track",   target, avoid?, seq?, reveal?}  report the target's
 *                               viewport rect now and on every scroll/resize
 *                               (null target stops tracking); `avoid` lists
 *                               further targets whose rects ride along (a
 *                               drag's drop zone), so the player's card can
 *                               keep off them as well; `seq` is echoed on
 *                               every report; `reveal` scrolls the target on
 *                               screen first if it is not, as Show me's cursor would
 *
 *   child -> parent:
 *     {type:"bridge-ready", dark}  dark = the captured page paints a dark
 *                               background, so the player's floating chrome
 *                               should flip its edge tone
 *     {type:"success"}          expected action performed
 *     {type:"wrong", label}     a non-target click / wrong select value
 *     {type:"showme-done"}
 *     {type:"probe-result", count}
 *     {type:"pick-cancelled"}   Escape pressed inside the page while picking
 *     {type:"picked", target, matches, tag, html}  picker click -> suggested
 *                               target, plus a capped markup excerpt so the
 *                               AI editor can name the element to the model
 *     {type:"window-rect", rect|null, doc}   rect in page coords; doc = full document size
 *     {type:"target-rect", rect|null, avoid:[rect], seq}   tracked element's viewport
 *                               rect, plus one per resolvable `avoid` target,
 *                               plus the menu a tracked <select> opens
 */
(function () {
  "use strict";

  var expectation = null; // {action}
  var inputTimer = null;
  var speed = 1;          // showme playback multiplier, set by {cmd:"config"}
  var cursorStyle = "dot";  // "dot" | "pointer", set by {cmd:"config"}

  // scale a demo delay by the playback speed (clamped so 2× stays visible)
  function ms(base) { return Math.max(30, Math.round(base / speed)); }

  /* ---------- styles ---------- */
  /* This is the ONE place Captcher brand is allowed inside a captured page:
   * the cursor and the spotlight, and nothing else. Brand governs chrome only,
   * and a captured page is not chrome — it is the customer's real product,
   * and it is never generated, stylised or tinted.
   *
   * Two things follow from that. Radius is 0 on everything here, because
   * anything sitting against a captured screen is a container, not a control.
   * And the spotlight is drawn OUTSIDE the target's box and never baked into
   * the capture, so nothing we draw can be mistaken for part of the product.
   *
   * DELIBERATE DUPLICATE of static/player.css's .spotlight construction and of
   * the tokens in static/captcher.css. This file is injected into a
   * captured document — a separate document, often a same-origin iframe, whose
   * network access has been frozen — so it cannot link a stylesheet and cannot
   * read a custom property declared on the player's :root. Every value is
   * spelled out. If one moves in player.css, move it here in the same commit.
   *
   * THE HUE CONTRACT, spelled out here because this file cannot inherit it:
   *   sky    #29B6FF  the hint target — "look here" (Brand Guide: info)
   *   mint   #3ECFA2  checked, correct
   *   coral  #FF6A5C  try again, with a reason
   *   sky    #0A6FA8  (deep step) Show me — the cursor's click, the travelling
   *                    ghost. Darker than the hint's sky so a Show me drag
   *                    (deep ghost) and its drop zone (bright ring) stay apart
   *   indigo #3B49C6  You try, and the author's own picking tools
   * These are the Brand Guide v1 hues at full strength (Show me excepted, see
   * above): they draw rings and dots over a captured page, never text.
   * There is no purple anywhere in the product.
   * A hue is ASSIGNED, never decorative. The spotlight's CONSTRUCTION is the
   * signature, not its hue: 2px solid ring, 1px near-black hairline outside
   * it, no glow. There is no shoulder and no falloff any more — a blurred edge
   * makes an assigned colour look like decoration, and this is the most
   * load-bearing colour in the product. */
  var BR_CSS =
    /* The ring arrives rather than appearing: opacity only, because position
     * and scale do not transition anywhere in this system. It breathes at
     * 2.4s on opacity — not brightness, which would walk the hue off the
     * spectrum. Only the hint breathes; success and error state a fact and
     * hold it.
     *
     * The keyframes animate the RING's alpha, never the element's opacity:
     * animating `opacity` faded the very control the learner is told to use.
     * And the ring lives only in the keyframes because animated values beat
     * every normal page rule whatever its specificity: a captured app's own
     * `.topbar .btn { box-shadow: … }` silently replaced a plain
     * `.br-hint { box-shadow }`, so on such buttons no ring drew at all. */
    "@keyframes brSpot{from{box-shadow:0 0 0 1px rgba(20,21,26,.2),0 0 0 3px rgba(41,182,255,.2)}" +
    "to{box-shadow:0 0 0 1px #14151A,0 0 0 3px #29B6FF}}" +
    "@keyframes brPulse{0%,100%{box-shadow:0 0 0 1px #14151A,0 0 0 3px #29B6FF}" +
    "50%{box-shadow:0 0 0 1px rgba(20,21,26,.62),0 0 0 3px rgba(41,182,255,.62)}}" +
    ".br-hint{border-radius:0;position:relative;z-index:5;" +
    "animation:brSpot .14s cubic-bezier(.2,0,.2,1),brPulse 2.4s ease-in-out .14s infinite}" +
    /* Success is green and is never derived from the hint hue: yellow means
     * "look here" and green means "you were right". If those shared a colour
     * the spotlight would stop meaning anything. */
    ".br-success{box-shadow:0 0 0 1px #14151A,0 0 0 3px #3ECFA2!important;border-radius:0;" +
    "animation:none;transition:box-shadow .14s cubic-bezier(.2,0,.2,1)}" +
    ".br-error{box-shadow:0 0 0 1px #14151A,0 0 0 3px #FF6A5C!important;border-radius:0;" +
    "animation:none;transition:box-shadow .14s cubic-bezier(.2,0,.2,1)}" +
    /* 14px near-white dot, 2px near-black ring — legible on any captured UI,
     * light or dark. Centred on the click point (the arrow variant is anchored
     * at its tip), no drop shadow, no trail. This is the one piece of motion
     * the brand owns, and it moves at the pace the learner sets. */
    "#br-cursor{position:fixed;z-index:99999;width:14px;height:14px;margin:0;pointer-events:none;" +
    "border-radius:50%;background:#F4F5F8;border:2px solid #14151A;box-sizing:content-box;" +
    "transform:translate(-50%,-50%);" +
    "transition:left .52s cubic-bezier(.2,0,.2,1),top .52s cubic-bezier(.2,0,.2,1)}" +
    /* Per-walkthrough alternative, and the one the brand draws: a near-black
     * arrow at 1.4x OS size, so it survives a busy capture and is never
     * mistaken for a stock pointer. Drawn as a background-image data URI, not
     * an inline <svg>, for the same reason the dot is pure CSS — a captured
     * page's own `svg{...}` rules cannot reach inside a background image.
     * Tip-anchored, so it drops the dot centring transform; the ripple keeps
     * its own and stays centred on the click point either way. */
    "#br-cursor.br-pointer{width:24px;height:24px;border:none;border-radius:0;" +
    "background:transparent no-repeat 0 0/24px 24px;transform:none;" +
    "background-image:url(\"data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\'" +
    " viewBox=\'0 0 12 19\'><path d=\'M1 1.2 L1 14.9 L4.55 11.6 L6.9 17.6 L9.3 16.6 L6.95 10.8 L11 10.8 Z\'" +
    " fill=\'%230B0B10\' stroke=\'%23ECECF5\' stroke-width=\'1.5\' stroke-linejoin=\'round\'" +
    " stroke-linecap=\'round\' paint-order=\'stroke\'/></svg>\")}" +
    /* The click. Deep sky, because a click only ever happens in Show me and Show
     * me is sky — the learner should be able to tell the mode from a single
     * frame of this. A ring (UI v2: squares are kept for the logo alone). */
    ".br-ripple{position:fixed;z-index:99998;width:14px;height:14px;border-radius:50%;" +
    "border:2px solid #0A6FA8;pointer-events:none;transform:translate(-50%,-50%);" +
    "animation:brRip .42s cubic-bezier(.2,0,.2,1) forwards}" +
    "@keyframes brRip{from{transform:translate(-50%,-50%) scale(.6);opacity:1}" +
    "to{transform:translate(-50%,-50%) scale(3.4);opacity:0}}" +
    /* The narration bubble is chrome, so it takes the chrome surface: a raised
     * step, a 1px hairline, soft 10px corners (UI v2), no shadow. Sora, because a person
     * wrote the words in it. */
    ".br-callout{position:fixed;z-index:99999;background:#14151A;color:#F4F5F8;" +
    "border:1px solid #3A3F4D;" +
    "font:400 13px \'Sora\',-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;" +
    "line-height:1.5;padding:8px 12px;border-radius:10px;pointer-events:none;max-width:280px}" +
    /* Show-me\'s stand-in for a native dropdown (see "faux dropdown" below).
     * It impersonates the product\'s own menu, not Captcher chrome, so it
     * is deliberately unbranded — a plain white list with a neutral hairline
     * and the select\'s own font, which is what an unstyled menu looks like on
     * the light pages that make up most captures. The one brand-coloured thing
     * in it is the yellow ring on the row the cursor is about to pick: same
     * instrument as the spotlight, same meaning. */
    ".br-options{position:fixed;z-index:99997;box-sizing:border-box;margin:0;padding:4px 0;" +
    "background:#FFFFFF;color:#111111;border:1px solid #8A8A8A;border-radius:0;" +
    "max-height:264px;overflow-y:auto;pointer-events:none;text-align:left}" +
    ".br-option{padding:5px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5}" +
    ".br-option.br-current{background:#EFEFEF}" +
    ".br-option.br-disabled{color:#8A8A8A}" +
    ".br-option.br-pick{box-shadow:inset 0 0 0 2px #29B6FF}" +
    /* Drag: the picked-up element travels as a ghost — a clone of itself,
     * outlined in deep sky because a demonstration is Show me — while the
     * original dims in place; the zone under it takes the yellow ring while it
     * hovers. No lift and no tilt: nothing in this system rotates, and a drop
     * shadow here would be the only one on screen. */
    ".br-ghost{position:fixed!important;z-index:99996!important;margin:0!important;" +
    "pointer-events:none!important;opacity:.92;box-sizing:border-box;" +
    "box-shadow:0 0 0 2px #0A6FA8!important;" +
    "transition:left .52s cubic-bezier(.2,0,.2,1),top .52s cubic-bezier(.2,0,.2,1)}" +
    ".br-lifted{opacity:.35!important}" +
    ".br-drop{box-shadow:0 0 0 1px #14151A,0 0 0 3px #29B6FF!important;border-radius:0}" +
    /* Authoring only — the learner never sees this one, so it takes the lead
     * hue rather than a state hue: it is the author\'s tool, not a mode. */
    ".br-picking,.br-picking *{cursor:crosshair!important}" +
    /* The element the author pointed at, held while they write about it.
     * Solid where the hover is dashed: chosen, not under consideration. The
     * hover rule follows it so a re-pick over the same element still reads. */
    ".br-pick-mark{outline:2px solid #3B49C6!important;outline-offset:2px}" +
    ".br-pick-hover{outline:2px dashed #3B49C6!important;outline-offset:2px}" +
    /* Under reduced motion: cut, no travel; spotlight only, and it holds still
     * rather than disappearing — it is the instruction, not decoration. */
    "@media(prefers-reduced-motion:reduce){" +
    "#br-cursor{transition:none}.br-ripple{display:none}" +
    /* held still, so no animation carries the ring: !important does instead */
    ".br-hint{animation:none;box-shadow:0 0 0 1px #14151A,0 0 0 3px #29B6FF!important}" +
    ".br-ghost{transition:none}}";

  // The hint/success classes land on elements inside captured iframes too,
  // and a class only styles what its document's own sheets describe — so the
  // bridge CSS goes into every reachable frame document, not just this one.
  // The cursor/ripple/callout stay in the TOP document only (their rules are
  // harmless elsewhere) with frame coords translated by topRect().
  function installCss(doc) {
    var css = doc.createElement("style");
    css.textContent = BR_CSS;
    (doc.head || doc.documentElement).appendChild(css);
  }
  installCss(document);

  /* ---------- target lookup ---------- */
  function norm(s) { return (s || "").replace(/\s+/g, " ").trim(); }

  /* Captured same-origin iframes are documents of their own; a target that
   * lives in one carries `frames`: one selector per hop from this (top)
   * document down. Each hop is querySelector — FIRST match, no text filter —
   * and digest.match() on the server mirrors exactly that. Requires the
   * serve-time sandbox rewrite (allow-same-origin) or contentDocument is
   * null and the target simply does not resolve. */
  function contextDocFor(t) {
    var doc = document;
    var frames = (t && t.frames) || [];
    for (var i = 0; i < frames.length; i++) {
      var fe;
      try { fe = doc.querySelector(frames[i]); } catch (e) { return null; }
      if (!fe) return null;
      var d = null;
      try { d = fe.contentDocument; } catch (e) { /* opaque origin */ }
      if (!d) return null;
      doc = d;
    }
    return doc;
  }

  function targetMatches(t) {
    if (!t || !t.selector) return [];
    var doc = contextDocFor(t);
    if (!doc) return [];
    var els;
    try { els = Array.prototype.slice.call(doc.querySelectorAll(t.selector)); }
    catch (e) { return []; }
    if (t.text_exact) {
      return els.filter(function (e) { return norm(e.textContent) === t.text_exact; });
    }
    if (t.text) {
      return els.filter(function (e) { return norm(e.textContent).indexOf(t.text) !== -1; });
    }
    return els;
  }

  function findTarget(t) { return targetMatches(t)[0] || null; }

  /* Viewport rect in THIS document's coordinates. An element in a captured
   * frame reports its rect relative to the frame's own viewport; each hop up
   * adds the frame element's offset, landing in top-viewport coords — which
   * is where the cursor, ripple and callout live (position:fixed here). */
  function topRect(el) {
    var r = el.getBoundingClientRect();
    var rect = { left: r.left, top: r.top, width: r.width, height: r.height };
    var win = el.ownerDocument.defaultView;
    while (win && win !== window) {
      var fe = win.frameElement;
      if (!fe) break;
      var fr = fe.getBoundingClientRect();
      rect.left += fr.left;
      rect.top += fr.top;
      win = fe.ownerDocument.defaultView;
    }
    return rect;
  }

  /* Every reachable frame document, walked recursively. Captured pages are
   * frozen, so the set only changes as srcdoc frames finish loading — each
   * load re-runs initFrames() via the listeners initDoc() plants. */
  function frameDocs() {
    var out = [];
    function walk(doc) {
      var frames = doc.querySelectorAll("iframe,frame");
      for (var i = 0; i < frames.length; i++) {
        var d = null;
        try { d = frames[i].contentDocument; } catch (e) { /* cross-origin */ }
        if (d && d.documentElement) {
          out.push(d);
          walk(d);
        }
      }
    }
    walk(document);
    return out;
  }

  function allDocs() { return [document].concat(frameDocs()); }

  function send(msg) { parent.postMessage(msg, "*"); }

  /* ---------- freeze the network ----------
   * auto/ snapshots keep the product's live scripts, which call its real
   * API (fetch "/api/..."). Against the walkthrough server that 404s and the
   * product pops its own error toasts. Frozen pages must stay silent, so
   * requests are swallowed: fetch never settles, XHR goes nowhere. */
  try {
    window.fetch = function () { return new Promise(function () {}); };
    if (window.XMLHttpRequest) {
      window.XMLHttpRequest.prototype.open = function () {};
      window.XMLHttpRequest.prototype.send = function () {};
    }
    if (navigator.sendBeacon) navigator.sendBeacon = function () { return true; };
    if (window.WebSocket) window.WebSocket = function () { return { send: function () {}, close: function () {} }; };
  } catch (e) { /* best effort */ }

  /* ---------- strip capture-tool artifacts ----------
   * the capture tool's cursor-tracking dot (a fixed, round, max-z-index,
   * pointer-events:none div) sometimes gets serialized into snapshots —
   * it's tool UI, not product UI, so remove it. */
  Array.prototype.forEach.call(
    document.querySelectorAll('div[style*="position:fixed"][style*="border-radius:50%"][style*="pointer-events:none"]'),
    function (el) { if (!el.id && !el.className && !el.childNodes.length) el.remove(); }
  );

  /* ---------- freeze the page ----------
   * Listeners are installed per document — events inside a captured iframe
   * never cross its boundary, so the top document's handlers alone would
   * leave frame content unfrozen and frame actions unvalidated. initDoc()
   * runs on this document now and on every frame document as it loads. */
  function onSubmit(e) { e.preventDefault(); }

  function onKeydown(e) {
    // Focus is in here once the author has clicked into the page, so the
    // editor's own Escape never hears it — answer it here instead.
    if (picking && e.key === "Escape") {
      e.preventDefault();
      setPicking(false);
      send({ type: "pick-cancelled" });
      return;
    }
    // allow typing in form fields; block anything else browser-y
    if (e.key === "Enter" && expectation && expectation.action.type === "input") {
      e.preventDefault();
      settleInput();
    }
  }

  /* ---------- restore captured scroll offsets ----------
   * The serializer keeps the DOM but not where each scroll box stood, so the
   * recorder stamps every scrolled element with data-sc-scroll="top left"
   * just before capture. Without this a form scrolled half-way down a modal
   * replays at its top, and anything positioned against the screen — an open
   * dropdown's listbox — lands over the wrong fields.
   *
   * Applied as each document initialises, and once more on window load for
   * any box that was still too short to reach its offset (images and fonts
   * arrive late). The retry only touches a box still where this left it, so
   * it can never undo a scroll the bridge itself made to reveal a target. */
  function stampedIn(root, out) {
    var all = root.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      if (all[i].hasAttribute("data-sc-scroll")) out.push(all[i]);
      if (all[i].shadowRoot) stampedIn(all[i].shadowRoot, out);
    }
    return out;
  }

  function restoreScroll(doc, retry) {
    var els;
    try { els = stampedIn(doc, []); } catch (e) { return; }
    els.forEach(function (el) {
      var want = (el.getAttribute("data-sc-scroll") || "").split(" ").map(Number);
      if (want.length !== 2 || isNaN(want[0]) || isNaN(want[1])) return;
      var at = el.__scAt;
      if (retry) {
        // only a box the first pass fell short on, and only if nothing has
        // scrolled it since
        if (!at || at.done || el.scrollTop !== at.top || el.scrollLeft !== at.left) return;
      } else if (at) {
        return;
      }
      el.scrollTop = want[0];
      el.scrollLeft = want[1];
      el.__scAt = { top: el.scrollTop, left: el.scrollLeft,
                    done: Math.abs(el.scrollTop - want[0]) < 1 &&
                          Math.abs(el.scrollLeft - want[1]) < 1 };
    });
  }

  function retryScroll() { allDocs().forEach(function (d) { restoreScroll(d, true); }); }

  function initDoc(doc) {
    if (doc.__brInit) return;
    doc.__brInit = true;
    restoreScroll(doc, false);
    if (doc !== document) installCss(doc);
    doc.addEventListener("click", onClick, true);
    doc.addEventListener("submit", onSubmit, true);
    doc.addEventListener("keydown", onKeydown, true);
    doc.addEventListener("mousemove", onMouseMove, true);
    doc.addEventListener("change", onChange, true);
    doc.addEventListener("input", onInput, true);
    doc.addEventListener("dragstart", onDragStart, true);
    doc.addEventListener("dragover", onDragOver, true);
    doc.addEventListener("drop", onDrop, true);
    doc.addEventListener("dragend", onDragEnd, true);
    doc.addEventListener("mousedown", onDragMouseDown, true);
    doc.addEventListener("mousemove", onDragMouseMove, true);
    doc.addEventListener("mouseup", onDragMouseUp, true);
    if (doc !== document && doc.defaultView) {
      // frame-local scrolls move a tracked target on screen too
      doc.defaultView.addEventListener("scroll", scheduleTrackedRect, true);
    }
    var frames = doc.querySelectorAll("iframe,frame");
    for (var i = 0; i < frames.length; i++) {
      frames[i].removeEventListener("load", initFrames);
      frames[i].addEventListener("load", initFrames);
    }
  }

  function initFrames() { allDocs().forEach(initDoc); }

  /* ---------- element picker (editor mode) ---------- */
  var picking = false;
  var hoverEl = null;

  function setPicking(on) {
    picking = on;
    allDocs().forEach(function (d) {
      d.documentElement.classList.toggle("br-picking", on);
    });
    if (!on && hoverEl) { hoverEl.classList.remove("br-pick-hover"); hoverEl = null; }
  }

  function onMouseMove(e) {
    if (!picking) return;
    var el = pickable(e.target);
    if (el === hoverEl) return;
    if (hoverEl) hoverEl.classList.remove("br-pick-hover");
    hoverEl = el;
    if (hoverEl) hoverEl.classList.add("br-pick-hover");
  }

  // The pointed-at element's outline. Not a hint: clearHints() leaves it, so
  // a step's spotlight and the author's selection can sit side by side.
  var markEl = null;

  function setMark(target) {
    if (markEl) { markEl.classList.remove("br-pick-mark"); markEl = null; }
    if (!target) return;
    markEl = findTarget(target);
    if (markEl) markEl.classList.add("br-pick-mark");
  }

  function pickable(el) {
    if (!el || !el.ownerDocument ||
        el === el.ownerDocument.body || el === el.ownerDocument.documentElement) return null;
    // snap to the interactive element the author almost certainly means
    return (el.closest &&
            el.closest("button, a, select, textarea, input, tr, [role=button], label")) || el;
  }

  // Enough of the element's markup for the AI editor to be sure which node the
  // author meant, and no more. Capped because this rides into a model prompt.
  var PICK_HTML_MAX = 600;

  function markupExcerpt(el) {
    var html = el.outerHTML || "";
    // Drop this file's own classes. br-pick-hover is on the element at the
    // moment it is clicked, and a class the bridge invented is not in the
    // snapshot on disk — leaving it in would describe an element that does
    // not exist. cssFor() filters the same prefix for the same reason.
    html = html.replace(/ class="([^"]*)"/g, function (_m, cls) {
      var kept = cls.split(/\s+/).filter(function (c) {
        return c && c.indexOf("br-") !== 0;
      });
      return kept.length ? ' class="' + kept.join(" ") + '"' : "";
    });
    // One inline base64 image would otherwise be the entire excerpt
    html = html.replace(/(["'(])data:[^"')]{40,}/g, "$1data:…");
    return html.length > PICK_HTML_MAX ? html.slice(0, PICK_HTML_MAX) + "…" : html;
  }

  function cssFor(el) {
    var tag = el.tagName.toLowerCase();
    var cls = Array.prototype.filter.call(el.classList, function (c) {
      return c.indexOf("br-") !== 0;
    }).slice(0, 2);
    return tag + cls.map(function (c) { return "." + CSS.escape(c); }).join("");
  }

  /* One verified hop selector per frame between the top document and el's,
   * outermost first — [] for a top-document element, null when a hop cannot
   * be named uniquely (that element cannot be routed to, so the picker
   * reports 0 matches rather than a selector for the wrong document). */
  function frameChainFor(el) {
    var chain = [];
    var win = el.ownerDocument.defaultView;
    try {
      while (win && win !== window) {
        var fe = win.frameElement;
        if (!fe) return null;
        var sel = frameSelFor(fe);
        if (!sel) return null;
        chain.unshift(sel);
        win = fe.ownerDocument.defaultView;
      }
    } catch (e) { return null; }
    return chain;
  }

  function frameSelFor(fe) {
    var doc = fe.ownerDocument;
    var cands = [];
    if (fe.id) {
      cands.push(/^[A-Za-z_][A-Za-z0-9_-]*$/.test(fe.id)
        ? "#" + fe.id
        : '[id="' + fe.id.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"]');
    }
    var name = fe.getAttribute("name");
    if (name) {
      cands.push('iframe[name="' + name.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"]');
    }
    cands.push(cssFor(fe));
    for (var i = 0; i < cands.length; i++) {
      try { if (doc.querySelectorAll(cands[i]).length === 1) return cands[i]; }
      catch (e) { /* try the next rung */ }
    }
    return null;
  }

  function suggestTarget(raw) {
    var el = pickable(raw) || raw;
    var chain = frameChainFor(el);
    var isField = /^(input|textarea|select)$/i.test(el.tagName);
    var fullText = norm(el.textContent);
    var text = isField ? "" : fullText.slice(0, 40).trim();
    // rows concatenate all their cells — the first cell's text (e.g. the
    // member name) is the human-readable match an author actually wants
    if (el.tagName === "TR") {
      var cell = el.querySelector("td, th");
      var cellText = cell ? norm(cell.textContent).slice(0, 40).trim() : "";
      if (cellText) text = cellText;
    }
    if (chain === null) {
      return { target: { selector: cssFor(el) }, matches: 0, el: el };
    }
    var withFrames = function (t) {
      if (chain.length) t.frames = chain.slice();
      return t;
    };
    var target = withFrames({ selector: cssFor(el) });
    if (text) target.text = text;

    var hit = function (t) { var m = targetMatches(t); return m.length === 1 && m[0] === el; };

    // scope with ancestors until the match is unique and correct
    if (!hit(target)) {
      var anc = el.parentElement;
      while (anc && anc !== el.ownerDocument.body) {
        var aSel = anc.id ? "#" + CSS.escape(anc.id)
                          : (anc.classList.length ? cssFor(anc) : null);
        if (aSel) {
          var scoped = withFrames({ selector: aSel + " " + target.selector.split(" ").pop() });
          if (text) scoped.text = text;
          if (hit(scoped)) { target = scoped; break; }
        }
        anc = anc.parentElement;
      }
    }
    // still colliding? try exact text
    if (!hit(target) && fullText) {
      var exact = withFrames({ selector: target.selector, text_exact: fullText });
      if (hit(exact)) target = exact;
    }
    return { target: target, matches: targetMatches(target).length, el: el };
  }

  /* ---------- checkable controls ---------- */
  // A frozen snapshot is meant to stay put, so onClick preventDefaults every
  // click: a captured page must not navigate or fire its own handlers. On a
  // checkbox or radio that guarantee backfires, because the tick IS the click's
  // only visible result — suppress it and the walkthrough tells the learner to
  // check a box that then sits there empty. Show-me had the same hole from the
  // other side: it animates a cursor and never touches the control, while the
  // select and input demos both write their result into the element.
  //
  // Driven TO checked, never toggled. Show-me replays, and stepping away and
  // back does not reload the page (gotoStep only reloads when the page really
  // changes), so a toggle would come back UNCHECKED on the second pass.
  //
  // No `change` event is dispatched. The snapshot still carries the captured
  // page's own scripts and waking them is the thing the freeze exists to
  // prevent; CSS :checked renders on its own, which is all this needs. The
  // select and input demos set .value the same way, for the same reason.
  function isCheckable(el) {
    if (!el || el.tagName !== "INPUT") return false;
    var t = (el.type || "").toLowerCase();
    return t === "checkbox" || t === "radio";
  }

  function checkableOf(el) {
    if (!el || el.nodeType !== 1) return null;
    if (isCheckable(el)) return el;
    // pickable() snaps to `label`, so an authored target very often names the
    // label rather than the input it points at or wraps
    var id = el.getAttribute && el.getAttribute("for");
    if (id) {
      var byFor = el.ownerDocument.getElementById(id);
      if (isCheckable(byFor)) return byFor;
    }
    var inner = el.querySelector &&
                el.querySelector("input[type=checkbox], input[type=radio]");
    return isCheckable(inner) ? inner : null;
  }

  function reflectCheck(el) {
    var box = checkableOf(el);
    if (!box || box.checked) return false;
    box.checked = true;   // radios: the DOM clears the rest of the group itself
    return true;
  }

  /* ---------- click handling ---------- */
  function onClick(e) {
    if (picking) {
      e.preventDefault();
      e.stopPropagation();
      var s = suggestTarget(e.target);
      // after setPicking, so the hover outline's class is already off the node
      setPicking(false);
      send({ type: "picked", target: s.target, matches: s.matches,
             tag: s.el.tagName.toLowerCase(), html: markupExcerpt(s.el) });
      return;
    }

    var a = e.target.closest && e.target.closest("a");
    if (a) e.preventDefault();

    if (!expectation) return;
    var act = expectation.action;

    if (act.type === "click") {
      e.preventDefault();
      var target = findTarget(act.target);
      if (target && (target === e.target || target.contains(e.target))) {
        // only the right click ticks the box — preventDefault above still
        // stops a wrong one from toggling something the step never named
        reflectCheck(target);
        succeed(target);
      } else {
        send({ type: "wrong", label: norm(e.target.textContent).slice(0, 60) });
      }
    }
    // for select/input expectations stray clicks are ignored (learner may
    // click around to focus fields) — forgiving by design.
  }

  /* ---------- drag handling ----------
   * A drag step is judged on the drop: the element released must be the
   * step's target and the place it lands must be inside its drop_target.
   * Two kinds of drag reach here. NATIVE — the element carries
   * draggable="true", so the browser runs HTML5 drag and drop itself and
   * only dragstart/dragover/drop/dragend arrive; dragover is preventDefault'd
   * everywhere so that a drop can happen (and be judged) anywhere. POINTER —
   * anything else: mousedown on the target, real movement, mouseup; the
   * bridge draws the ghost itself, since nothing else will. A press that
   * never moves is a click and is left alone.
   *
   * The captured page has no drop handlers of its own (its scripts are
   * gone), so after a correct drop the bridge moves the node into the zone
   * — into a container there that matches the node's original parent
   * (tag and classes) when one exists, else the zone itself. */
  var drag = null;   // {el, native, ghost?, x, y, gx, gy, moved, doc}

  function dragAction() {
    return expectation && expectation.action.type === "drag" ? expectation.action : null;
  }

  // On screen enough to drop into: horizontally inside the viewport, and
  // some of it vertically (a column taller than the window never fits whole).
  function inView(el) {
    var r = topRect(el);
    return r.left >= 0 && r.left + r.width <= window.innerWidth &&
           r.top < window.innerHeight && r.top + r.height > 0;
  }

  function within(container, node) {
    return !!container && !!node && (container === node || container.contains(node));
  }

  /* ---------- scrolling a target into view ----------
   * scrollIntoView() scrolls EVERY ancestor scrolling box the element sits in,
   * and the spec does not stop at an iframe boundary. Inside an embedded
   * player that means the host page scrolls too: a reader who had scrolled
   * past the frame was yanked back to it every time a step advanced. So the
   * scroll is done by hand instead, one scrolling box at a time, and the walk
   * stops at this capture's own window. Nothing above the capture ever moves,
   * which also means the same call is safe on a customer's page, in an LMS
   * chrome, or in the marketing site's hero.
   *
   * Cross-origin frames throw on .defaultView; captured pages are same-origin
   * srcdoc copies, so this is belt and braces. */
  var SCROLLY = /(auto|scroll|overlay)/;

  function scrollableAncestors(el) {
    var out = [], node = el.parentElement, st;
    while (node) {
      try { st = node.ownerDocument.defaultView.getComputedStyle(node); } catch (e) { st = null; }
      if (st && ((SCROLLY.test(st.overflowY) && node.scrollHeight > node.clientHeight) ||
                 (SCROLLY.test(st.overflowX) && node.scrollWidth > node.clientWidth))) {
        out.push(node);
      }
      node = node.parentElement;
    }
    return out;
  }

  /* How far a scrolling box must move for `start`/`size` (the target, in the
   * box's own coordinates) to sit where `block` asks.
   *
   * "reveal" is what every step uses. A target sitting at least
   * REVEAL_BORDER inside the visible area stays exactly where it is: the
   * page used to re-centre on every step, and a screen that jumps when
   * nothing needed reaching is a distraction. A target inside that border —
   * off screen, cut by an edge, or merely close to one — moves the least
   * that brings it to the border.
   *
   * The border is not decoration. "Inside the box" is geometry, and products
   * draw over the edges of their own scroll areas: Salesforce's modal footer
   * (Cancel / Save) covers the last ~57px of the form that scrolls under it,
   * so a field there was "visible" and hidden at once. 80px clears footers
   * and headers of that kind. It shrinks to fit a view too small for it, and
   * a target bigger than the view is left alone while its start is visible
   * and otherwise brought in start-first.
   *
   * "nearest" moves only when the target is actually outside, and never
   * pushes its near edge out (the drag drop-zone case). "center" is kept for
   * callers that want it; nothing per-step does. */
  var REVEAL_BORDER = 80;

  function scrollDelta(start, size, view, block) {
    if (block === "reveal") {
      if (size > view) return (start >= 0 && start < view) ? 0 : start;
      var border = Math.min(REVEAL_BORDER, (view - size) / 2);
      if (start >= border && start + size <= view - border) return 0;
      return start < border ? start - border : start + size + border - view;
    }
    if (block === "center") return start - (view - size) / 2;
    if (start < 0) return start;
    if (start + size > view) return Math.min(start, start + size - view);
    return 0;
  }

  function scrollBoxBy(box, dx, dy, smooth) {
    if (!dx && !dy) return;
    try {
      box.scrollBy({ left: dx, top: dy, behavior: smooth ? "smooth" : "auto" });
    } catch (e) {
      // older engines, and any captured page that shadowed scrollBy
      if (box.scrollTop !== undefined) { box.scrollTop += dy; box.scrollLeft += dx; }
      else box.scrollBy(dx, dy);
    }
  }

  function scrollLocal(el, block, smooth) {
    if (!el) return;
    block = block || "reveal";
    var node = el;
    while (node) {
      var win;
      try { win = node.ownerDocument.defaultView; } catch (e) { win = null; }
      if (!win) break;
      // the scrolling boxes inside this document, innermost first: each is
      // re-measured after the one before it moved
      scrollableAncestors(node).forEach(function (box) {
        var er = node.getBoundingClientRect(), br = box.getBoundingClientRect();
        scrollBoxBy(box,
          scrollDelta(er.left - br.left, er.width, box.clientWidth, block),
          scrollDelta(er.top - br.top, er.height, box.clientHeight, block),
          smooth);
      });
      // then this document's own viewport
      var r = node.getBoundingClientRect();
      scrollBoxBy(win,
        scrollDelta(r.left, r.width, win.innerWidth, block),
        scrollDelta(r.top, r.height, win.innerHeight, block),
        smooth);
      if (win === window) break;   // this capture's own window: go no higher
      node = win.frameElement;
    }
  }

  function makeGhost(el) {
    var doc = el.ownerDocument;
    var r = el.getBoundingClientRect();
    var g = el.cloneNode(true);
    g.removeAttribute("id");
    g.classList.remove("br-hint", "br-success", "br-error", "br-lifted");
    g.classList.add("br-ghost");
    g.style.left = r.left + "px";
    g.style.top = r.top + "px";
    g.style.width = r.width + "px";
    g.style.height = r.height + "px";
    g.style.transitionDuration = (0.52 / speed) + "s, " + (0.52 / speed) + "s";
    (doc.body || doc.documentElement).appendChild(g);
    el.classList.add("br-lifted");
    return g;
  }

  function moveInto(el, zone) {
    el.classList.remove("br-lifted");
    var p = el.parentNode, container = zone;
    if (p && p !== zone && p.nodeType === 1) {
      var sel = p.tagName.toLowerCase();
      Array.prototype.forEach.call(p.classList, function (c) {
        if (/^-?[A-Za-z_][\w-]*$/.test(c) && c.indexOf("br-") !== 0) sel += "." + c;
      });
      try {
        var match = zone.querySelector(sel);
        if (match && !match.contains(el)) container = match;
      } catch (e) { /* unusual class token: the zone itself will do */ }
    }
    if (container !== el.parentNode) container.appendChild(el);
  }

  function setDropHover(act, over) {
    var zone = findTarget(act.drop_target);
    if (zone) zone.classList.toggle("br-drop", within(zone, over));
  }

  function endDrag() {
    if (!drag) return;
    if (drag.ghost) drag.ghost.remove();
    drag.el.classList.remove("br-lifted");
    if (drag.doc && drag.doc.body) drag.doc.body.style.userSelect = drag.userSelect || "";
    allDocs().forEach(function (d) {
      Array.prototype.forEach.call(d.querySelectorAll(".br-drop"), function (z) {
        z.classList.remove("br-drop");
      });
    });
    drag = null;
  }

  function finishDrag(act, over) {
    var target = findTarget(act.target), zone = findTarget(act.drop_target);
    var el = drag.el;
    endDrag();
    if (el === target && within(zone, over)) {
      moveInto(el, zone);
      succeed(el);
    } else if (el !== target) {
      send({ type: "wrong", label: norm(el.textContent).slice(0, 60) });
    } else {
      // right thing, wrong place: name where it was dropped, briefly
      var where = over && over.closest && over.closest("h1,h2,h3,h4,h5,h6,section,[role=region],li,td,div");
      send({ type: "wrong", label: norm(where ? where.textContent : "").slice(0, 60) });
    }
  }

  function onDragStart(e) {
    var act = dragAction();
    if (!act) return;                    // not a drag step: the page's own drag runs untouched
    if (drag && !drag.native) endDrag(); // native DnD took over a pointer press
    var target = findTarget(act.target);
    var picked = within(target, e.target) ? target : e.target;
    drag = { el: picked, native: true, doc: e.target.ownerDocument };
    if (picked === target) picked.classList.add("br-lifted");
    try {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", "captcher");
    } catch (x) { /* some browsers refuse setData on synthetic events */ }
  }

  function onDragOver(e) {
    var act = dragAction();
    if (!act || !drag) return;
    e.preventDefault();                  // permit the drop anywhere; the drop is what gets judged
    try { e.dataTransfer.dropEffect = "move"; } catch (x) { /* ok */ }
    setDropHover(act, e.target);
  }

  function onDrop(e) {
    var act = dragAction();
    if (!act || !drag) return;
    e.preventDefault();
    e.stopPropagation();
    finishDrag(act, e.target);
  }

  function onDragEnd() { if (drag && drag.native) endDrag(); }

  function onDragMouseDown(e) {
    var act = dragAction();
    if (!act || e.button !== 0) return;
    var target = findTarget(act.target);
    if (!within(target, e.target)) return;
    if (target.getAttribute("draggable") === "true") return;   // native path
    drag = { el: target, native: false, x: e.clientX, y: e.clientY, moved: false,
             doc: e.target.ownerDocument };
  }

  function onDragMouseMove(e) {
    if (!drag || drag.native) return;
    var act = dragAction();
    if (!act) { endDrag(); return; }
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 4) return;
      drag.moved = true;
      drag.ghost = makeGhost(drag.el);
      drag.ghost.style.transition = "none";
      drag.gx = parseFloat(drag.ghost.style.left) - drag.x;
      drag.gy = parseFloat(drag.ghost.style.top) - drag.y;
      if (drag.doc.body) {
        drag.userSelect = drag.doc.body.style.userSelect;
        drag.doc.body.style.userSelect = "none";
      }
    }
    e.preventDefault();
    drag.ghost.style.left = (e.clientX + drag.gx) + "px";
    drag.ghost.style.top = (e.clientY + drag.gy) + "px";
    setDropHover(act, e.target);
  }

  function onDragMouseUp(e) {
    if (!drag || drag.native) return;
    if (!drag.moved) { drag = null; return; }   // a press without travel is a click
    var act = dragAction();
    if (!act) { endDrag(); return; }
    finishDrag(act, e.target);
  }

  /* ---------- select / input handling ---------- */
  // Fields the learner has actually typed in or changed on this page. Merged
  // runs need this: the checks below only ever fire on a live event, so a field
  // filled before its step came round would leave that step waiting for an
  // event that has already happened. armExpectation() re-checks against this
  // set — and only this set, so a snapshot that ships with a field already
  // filled cannot auto-complete a step nobody has touched.
  var touched = new WeakSet();

  function onChange(e) {
    if (e.target) touched.add(e.target);
    if (!expectation || expectation.action.type !== "select") return;
    var act = expectation.action;
    var target = findTarget(act.target);
    if (!target || e.target !== target) return;
    // accept_any: the walkthrough demos act.value but any real choice is right
    if (selectMatches(target, act.value) || (act.accept_any && target.value)) {
      succeed(target);
    } else if (target.value) {
      send({ type: "wrong", label: target.value, wrongValue: true });
    }
  }

  function onInput(e) {
    if (e.target) touched.add(e.target);
    if (!expectation || expectation.action.type !== "input") return;
    var act = expectation.action;
    var target = findTarget(act.target);
    if (!target || e.target !== target) return;
    clearTimeout(inputTimer);
    if (norm(target.value).length >= (act.min_length || 1)) {
      inputTimer = setTimeout(settleInput, 1100); // settled typing = done
    }
  }

  function settleInput() {
    if (!expectation || expectation.action.type !== "input") return;
    var act = expectation.action;
    var target = findTarget(act.target);
    if (!target || norm(target.value).length < (act.min_length || 1)) return;
    if (act.expect_value && norm(target.value) !== String(act.expect_value)) {
      send({ type: "wrong", label: norm(target.value), wrongValue: true });
      return;
    }
    succeed(target);
  }

  // Arm an expectation, then test it once against what is already on the page.
  // Without this a merged run can deadlock: the learner fills Email before its
  // step comes round, the input listener discards it (a different action was
  // expected), and when Email is finally expected no further input event is
  // coming. Only a field in `touched` qualifies — see the note there.
  //
  // A touched-but-wrong value stays silent rather than reporting "wrong": the
  // learner is not acting right now, and feedback on something they did two
  // fields ago reads as the player malfunctioning. Correcting the field fires
  // a live event and takes the normal path.
  /* ---------- a frozen page's locked controls ----------
   * A snapshot is taken at one moment and never runs the page's own script,
   * so a control that the app switched on in reaction to the previous step
   * stays off: a Duplicate that makes a read-only form editable, a Save that
   * lights up once a field is filled. When the recorder does not snapshot
   * again after that step, the next one plays on the locked copy — Show me
   * still works (setValue ignores `disabled`), and You try strands the
   * learner in front of a field that takes no typing or a button that fires
   * no click. The step is the evidence: it was recorded because the control
   * WAS usable at that moment. So the step's own target is unlocked, and
   * nothing else is — the rest of the page stays exactly as captured. */
  var LOCKABLE = /^(INPUT|TEXTAREA|SELECT|BUTTON)$/;

  function unlockTarget(el) {
    if (!el || !LOCKABLE.test(el.tagName)) return;
    if (el.disabled) el.disabled = false;
    if (el.readOnly) el.readOnly = false;
    // <fieldset disabled> locks every control inside it without touching
    // their own attributes; only the step's own fieldset is opened
    var fs = el.closest && el.closest("fieldset[disabled]");
    if (fs) fs.disabled = false;
  }

  function armExpectation(action) {
    expectation = { action: action };
    clearHints();
    var type = action.type;
    if (type !== "observe") unlockTarget(findTarget(action.target));
    if (type !== "input" && type !== "select") return;
    var el = findTarget(action.target);
    if (!el || !touched.has(el)) return;
    if (type === "select") {
      if (selectMatches(el, action.value) || (action.accept_any && el.value)) succeed(el);
      return;
    }
    if (norm(el.value).length < (action.min_length || 1)) return;
    if (action.expect_value && norm(el.value) !== String(action.expect_value)) return;
    succeed(el);
  }

  function succeed(el) {
    expectation = null;
    clearHints();
    if (el) {
      el.classList.add("br-success");
      setTimeout(function () { el.classList.remove("br-success"); }, 700);
    }
    setTimeout(function () { send({ type: "success" }); }, 250);
  }

  /* ---------- hints ---------- */
  function clearHints() {
    allDocs().forEach(function (d) {
      Array.prototype.forEach.call(d.querySelectorAll(".br-hint"), function (e) {
        e.classList.remove("br-hint");
      });
    });
  }

  function showHint(action) {
    clearHints();
    var el = findTarget(action.target);
    if (el) {
      scrollLocal(el, "reveal", true);
      el.classList.add("br-hint");
    }
    if (action.type === "drag") {
      var zone = findTarget(action.drop_target);
      if (zone) zone.classList.add("br-hint");
    }
  }

  /* ---------- show-me animation ---------- */
  var cursor = null;
  function ensureCursor() {
    if (cursor && document.body.contains(cursor)) { applyCursorConfig(); return cursor; }
    cursor = document.createElement("div");
    cursor.id = "br-cursor";
    // Both shapes are drawn entirely in CSS (see the style block) — no inner
    // SVG, so a captured page's own svg rules cannot restyle them.
    cursor.style.left = "45%";
    cursor.style.top = "80%";
    applyCursorConfig();
    document.body.appendChild(cursor);
    return cursor;
  }

  function applyCursorConfig() {
    if (!cursor) return;
    // the cursor's travel time is a CSS transition; scale it with the speed.
    // 0.52s is the brand's cursorTravel duration (tokens/motion).
    cursor.style.transitionDuration = (0.52 / speed) + "s, " + (0.52 / speed) + "s";
    cursor.classList.toggle("br-pointer", cursorStyle === "pointer");
  }

  function moveCursorTo(el, cb) {
    var c = ensureCursor();
    scrollLocal(el, "reveal");
    // after scroll settles, compute position (topRect: frame-aware coords)
    setTimeout(function () {
      var r = topRect(el);
      c.style.left = (r.left + Math.min(r.width / 2, 140)) + "px";
      c.style.top = (r.top + r.height / 2) + "px";
      setTimeout(cb, ms(1000));
    }, ms(250));
  }

  function ripple() {
    var c = ensureCursor();
    var rip = document.createElement("div");
    rip.className = "br-ripple";
    // cursor and ripple are both translate(-50%,-50%), so they share coords
    // exactly — the old -2px nudge compensated for the tip-anchored arrow.
    rip.style.left = c.style.left;
    rip.style.top = c.style.top;
    document.body.appendChild(rip);
    setTimeout(function () { rip.remove(); }, 460);   // ripple runs 420ms, once
  }

  function callout(el, text, cb) {
    var r = topRect(el);
    var box = document.createElement("div");
    box.className = "br-callout";
    box.textContent = text;
    box.style.left = Math.max(8, r.left) + "px";
    box.style.top = Math.max(8, r.top - 44) + "px";
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); if (cb) cb(); }, ms(1600));
  }

  /* ---------- writing a value the way a human would ----------
   * Show-me used to assign el.value and stop there. A captured page is FROZEN
   * in the sense that it fetches nothing, but its own scripts still run — and
   * plenty of real UIs recompute a total, relabel a submit button or enable a
   * control from an oninput/onchange handler. Assigning .value fires no
   * events, so none of that ran and Show-me quietly played a version of the
   * page the learner would never see. (Concretely: the partial-payment
   * capture already relabels "Record $3,712.15" and its running total from
   * the amount field's own oninput — it just never fired.)
   *
   * You-try was always fine here: the learner types for real.
   *
   * setValue goes through the prototype's native value setter before
   * dispatching, because a framework that has monkey-patched the instance
   * property tracks its own last-known value and will treat an event as a
   * no-op if the instance setter was bypassed. Falls back to a plain
   * assignment when the descriptor is missing. */
  function nativeSetter(el) {
    var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
              : HTMLInputElement.prototype;
    var d = Object.getOwnPropertyDescriptor(proto, "value");
    return d && d.set ? d.set : null;
  }

  /* The option a select step means. A step's value is the option's value
   * attribute when it has one, and its visible label when it does not —
   * ingest falls back to the label for `<option value="">All stages</option>`,
   * where the attribute is empty. Matched the way optionRows() rings it: by
   * value first, then by label. Returns the option's own value, or null when
   * nothing matches. Without this Show-me wrote the label into .value (no
   * option has it, so the select went blank) and You-try compared the real
   * pick's "" against "All stages" and never let the learner through. */
  function optionValueFor(el, value) {
    var want = norm(String(value == null ? "" : value));
    var opts = (el && el.options) || [];
    for (var i = 0; i < opts.length; i++) if (norm(opts[i].value) === want) return opts[i].value;
    for (var j = 0; j < opts.length; j++) {
      if (norm(opts[j].label || opts[j].textContent || opts[j].text) === want) return opts[j].value;
    }
    return null;
  }

  function selectMatches(el, value) {
    var v = optionValueFor(el, value);
    return el.value === (v === null ? value : v);
  }

  function setValue(el, value, events) {
    try {
      var set = nativeSetter(el);
      if (set) set.call(el, value); else el.value = value;
    } catch (e) { el.value = value; }
    for (var i = 0; i < events.length; i++) {
      try {
        el.dispatchEvent(new Event(events[i], { bubbles: true }));
      } catch (e) { /* a page that throws in its own handler is not our problem */ }
    }
  }

  /* A programmatic write moves the caret to the end but does not scroll the
   * field to it — the browser only follows the caret for real keystrokes.
   * Once the demo text passes the width of the box the typing therefore goes
   * on off-screen: the value keeps growing while the learner watches a line
   * of text sitting frozen at the right edge (log-a-new-ticket's "First
   * message" ends 31px past its input). So chase the caret by hand, on both
   * axes — a single-line input scrolls right, a textarea scrolls down.
   * setSelectionRange is NOT a substitute: measured, it leaves scrollLeft at
   * 0, and it throws outright on number/email/date inputs. */
  function followCaret(el) {
    try {
      el.scrollLeft = el.scrollWidth;
      el.scrollTop = el.scrollHeight;
    } catch (e) { /* not a field that scrolls */ }
  }

  /* ---------- faux dropdown ----------
   * A native <select> cannot be opened from script (showPicker() needs a
   * user gesture, and the OS menu it opens is not ours to animate or close),
   * so a demoed select used to be invisible: the cursor arrived, the value
   * changed in place, a callout named the choice. Learners read that as "the
   * dropdown never opened" — and authors reached for the wrong fix, editing
   * the captured page (`size="5"`) to make the options show. The page is the
   * customer's product and must not be bent to fit the player, so Show-me
   * draws the open menu itself: a list of the select's own options under the
   * control, the cursor travels to the one being chosen, it is picked, the
   * list closes and only then does the value change. You-try is untouched —
   * the learner clicks the real select and the browser opens it for real.
   *
   * A listbox (`multiple`, or `size` > 1) already shows its options, so it
   * keeps the old direct pick. */
  function optionRows(el, value) {
    // Pure: which rows to draw, and which one is the pick. Matched the way
    // setValue will match it — by option value first, then by visible label
    // — so the row ringed is the row that ends up selected.
    var want = norm(String(value == null ? "" : value));
    var rows = [], pick = -1;
    var opts = el.options || [];
    for (var i = 0; i < opts.length; i++) {
      var o = opts[i];
      if (o.hidden) continue;
      var label = norm(o.label || o.textContent || o.text);
      rows.push({ label: label, current: !!o.selected, disabled: !!o.disabled });
      if (pick < 0 && (norm(o.value) === want || label === want)) pick = rows.length - 1;
    }
    return { rows: rows, pick: pick };
  }

  function isListbox(el) {
    return !!(el.multiple || (el.size | 0) > 1);
  }

  /* Where a menu of height h goes: below the control like a native menu,
   * above it when that would run off the bottom of the viewport. Shared by
   * the faux menu and the footprint reported for card placement, so the
   * card keeps off the menu wherever it actually opens. */
  function menuTop(r, h) {
    var below = r.top + r.height;
    return below + h <= window.innerHeight - 8 || r.top - h < 8 ? below : r.top - h;
  }

  /* A closed <select> is bigger than it looks. Opening it drops a menu of
   * its options under the control — the browser's own in You-try, the faux
   * one above in Show-me — and "below the target" is exactly where the
   * player's step card likes to park when the sides are cramped (a select
   * at the right end of a top bar, say). So the tracked rect of a select
   * carries the menu's footprint as a box to avoid, sized the way
   * openOptions draws it: one row per visible option at the control's own
   * font, the list capped at .br-options' max-height. Pure: geometry in,
   * a viewport box out. */
  function menuFootprint(r, rowCount, fontPx, viewportW) {
    var rowH = Math.round(fontPx * 1.5) + 10;       // .br-option line-height + padding
    var h = Math.min(264, rowCount * rowH + 10);    // + .br-options padding and border
    var left = Math.max(0, r.left);
    var w = Math.min(Math.max(160, r.width), Math.max(160, viewportW - left - 8));
    return { x: left, y: menuTop(r, h), width: w, height: h };
  }

  function isMenuSelect(el) {
    return !!el && /^select$/i.test(el.tagName) && !isListbox(el);
  }

  function selectMenuRect(el) {
    var fontPx = 13;
    try { fontPx = parseFloat(el.ownerDocument.defaultView.getComputedStyle(el).fontSize) || 13; }
    catch (e) { /* default */ }
    return menuFootprint(topRect(el), optionRows(el, "").rows.length, fontPx, window.innerWidth);
  }

  function openOptions(el, value, cb) {
    var plan = optionRows(el, value);
    if (plan.pick < 0 || isListbox(el)) { cb(false); return; }

    var r = topRect(el);
    var list = document.createElement("div");
    list.className = "br-options";
    var font = "";
    try { font = el.ownerDocument.defaultView.getComputedStyle(el).font; } catch (e) { /* ok */ }
    if (font) list.style.font = font;
    list.style.left = Math.max(0, r.left) + "px";
    list.style.minWidth = Math.max(160, r.width) + "px";
    list.style.maxWidth = Math.max(160, window.innerWidth - r.left - 8) + "px";
    var rowEls = [];
    for (var i = 0; i < plan.rows.length; i++) {
      var row = document.createElement("div");
      row.className = "br-option" + (plan.rows[i].current ? " br-current" : "")
                                  + (plan.rows[i].disabled ? " br-disabled" : "");
      row.textContent = plan.rows[i].label;
      list.appendChild(row);
      rowEls.push(row);
    }
    document.body.appendChild(list);
    list.style.top = menuTop(r, list.offsetHeight) + "px";
    var pickEl = rowEls[plan.pick];
    // long lists scroll the pick into the visible window, as a real menu would
    list.scrollTop = Math.max(0, pickEl.offsetTop - list.clientHeight / 2 + pickEl.offsetHeight / 2);

    // the menu is "open"; travel to the pick, choose it, close
    setTimeout(function () {
      var c = ensureCursor();
      var pr = pickEl.getBoundingClientRect();
      c.style.left = (pr.left + Math.min(pr.width / 2, 140)) + "px";
      c.style.top = (pr.top + pr.height / 2) + "px";
      setTimeout(function () {
        pickEl.classList.add("br-pick");
        ripple();
        setTimeout(function () {
          list.remove();
          // the menu has closed on the control; the cursor rests there, as a
          // real pointer does after picking, so the "Selected" callout and
          // the success ring are read together
          var back = topRect(el);
          c.style.left = (back.left + Math.min(back.width / 2, 140)) + "px";
          c.style.top = (back.top + back.height / 2) + "px";
          cb(true);
        }, ms(420));
      }, ms(650));
    }, ms(350));
  }

  function showme(action) {
    var el = findTarget(action.target);
    if (!el) { send({ type: "showme-done", missing: true }); return; }
    // the same control You try unlocks, so the demonstration does not type
    // into a field that still looks greyed out
    unlockTarget(el);

    if (action.type === "click") {
      moveCursorTo(el, function () {
        ripple();
        reflectCheck(el);
        el.classList.add("br-success");
        setTimeout(function () {
          el.classList.remove("br-success");
          send({ type: "showme-done" });
        }, ms(700));
      });
    } else if (action.type === "select") {
      moveCursorTo(el, function () {
        ripple();   // the click that opens the menu
        openOptions(el, action.value, function () {
          // pick the option and show what was chosen. A real pick fires input
          // then change, and change is the one most select handlers listen on.
          var picked = optionValueFor(el, action.value);
          setValue(el, picked === null ? action.value : picked, ["input", "change"]);
          var opt = el.selectedOptions && el.selectedOptions[0];
          el.classList.add("br-success");
          callout(el, "Selected: " + (opt ? norm(opt.textContent) : action.value), function () {
            el.classList.remove("br-success");
            send({ type: "showme-done" });
          });
        });
      });
    } else if (action.type === "drag") {
      var zone = findTarget(action.drop_target);
      if (!zone) { send({ type: "showme-done", missing: true }); return; }
      moveCursorTo(el, function () {
        ripple();   // press and hold
        var c = ensureCursor();
        // A zone off-screen (a wide board, windowed playback) is brought to
        // the nearest edge before the pick-up, so the carry has somewhere
        // visible to go; the cursor then re-finds the element where the
        // scroll left it. "nearest" keeps the element itself on screen
        // whenever both fit.
        if (!inView(zone)) {
          try { scrollLocal(zone, "nearest"); } catch (e) { /* ok */ }
          var er = topRect(el);
          c.style.left = (er.left + Math.min(er.width / 2, 140)) + "px";
          c.style.top = (er.top + er.height / 2) + "px";
        }
        var ghost = el.ownerDocument === zone.ownerDocument ? makeGhost(el) : null;
        setTimeout(function () {
          // carry: the cursor heads for the zone, the ghost keeps its grip offset
          var zr = topRect(zone);
          var toX = zr.left + Math.min(zr.width / 2, 140);
          var toY = zr.top + Math.min(zr.height / 2, 160);
          var dx = toX - parseFloat(c.style.left), dy = toY - parseFloat(c.style.top);
          c.style.left = toX + "px";
          c.style.top = toY + "px";
          if (ghost) {
            ghost.style.left = (parseFloat(ghost.style.left) + dx) + "px";
            ghost.style.top = (parseFloat(ghost.style.top) + dy) + "px";
          }
          zone.classList.add("br-drop");
          setTimeout(function () {
            ripple();   // release
            if (ghost) ghost.remove();
            zone.classList.remove("br-drop");
            moveInto(el, zone);
            el.classList.add("br-success");
            setTimeout(function () {
              el.classList.remove("br-success");
              send({ type: "showme-done" });
            }, ms(700));
          }, ms(1000));
        }, ms(350));
      });
    } else if (action.type === "input") {
      moveCursorTo(el, function () {
        ripple();
        el.focus();
        var text = action.demo_value || "Case note";
        var i = 0;
        var t = setInterval(function () {
          // input per keystroke, exactly as typing does — a page that
          // recomputes as you type then animates along with the demo
          setValue(el, text.slice(0, ++i), ["input"]);
          followCaret(el);
          if (i >= text.length) {
            clearInterval(t);
            // change lands once at the end: real inputs commit on blur, and
            // firing it per keystroke would make an onchange handler run the
            // length of the value instead of once
            setValue(el, text, ["change"]);
            followCaret(el);
            setTimeout(function () { send({ type: "showme-done" }); }, ms(500));
          }
        }, ms(45));
      });
    } else { // observe
      send({ type: "showme-done" });
    }
  }

  /* ---------- command listener ---------- */
  window.addEventListener("message", function (e) {
    var m = e.data || {};
    if (m.cmd === "config") {
      speed = m.speed > 0 ? m.speed : 1;
      // anything unrecognised falls back to the brand default rather than
      // leaving the page with no cursor at all
      cursorStyle = m.cursor === "pointer" ? "pointer" : "dot";
      applyCursorConfig();
    }
    else if (m.cmd === "expect") { armExpectation(m.action); }
    else if (m.cmd === "showme") { expectation = null; clearHints(); showme(m.action); }
    else if (m.cmd === "hint") { showHint(m.action); }
    else if (m.cmd === "clear") { expectation = null; endDrag(); clearHints(); setPicking(false); if (cursor) cursor.remove(); }
    else if (m.cmd === "probe") {
      clearHints();
      var els = targetMatches(m.target);
      if (els[0]) {
        scrollLocal(els[0], "reveal", true);
        els[0].classList.add("br-hint");
      }
      send({ type: "probe-result", count: els.length });
    }
    else if (m.cmd === "track") {
      trackedTarget = m.target || null;
      trackedAvoid = Array.isArray(m.avoid) ? m.avoid : [];
      trackedSeq = m.seq;
      if (trackedTarget) {
        // Show me brings the target on screen (if it is not already)
        // before the cursor travels (moveCursorTo). Doing it now, with the
        // same rule, before the first report, is what lets the card be
        // placed once: the later scroll then moves nothing.
        if (m.reveal) {
          var shown = findTarget(trackedTarget);
          if (shown) { try { scrollLocal(shown, "reveal"); } catch (e) { /* ok */ } }
        }
        sendTrackedRect();
      }
    }
    else if (m.cmd === "pick") { clearHints(); setPicking(true); }
    else if (m.cmd === "pick-off") { setPicking(false); }
    else if (m.cmd === "mark") { setMark(m.target || null); }
    else if (m.cmd === "window-rect") {
      // deferred a tick: the parent resizes this iframe right before asking,
      // and the new viewport size must have propagated before measuring
      setTimeout(function () {
        send({ type: "window-rect", rect: pageRect(m.target), doc: docSize() });
      }, 0);
    }
  });

  /* ---------- target tracking (popup player) ----------
   * The popup player positions its instruction card next to the step's target,
   * which lives in this document. {cmd:"track"} names the target; the bridge
   * reports its viewport rect at once and again after every scroll/resize, so
   * the card can follow the element as the page moves. Viewport coords, not
   * page coords: the parent maps them through the iframe's own box, which
   * already accounts for windowed scale/translate. The v1 player never sends
   * "track", so this is inert under it. */
  var trackedTarget = null;
  var trackPending = false;
  // echoed on every report, so the player can drop one meant for an earlier
  // step's target that was already in flight when the next track arrived
  var trackedSeq = null;

  var trackedAvoid = [];

  function viewportRect(el) {
    var r = topRect(el);
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }

  function sendTrackedRect() {
    var el = trackedTarget ? findTarget(trackedTarget) : null;
    var avoid = [];
    for (var i = 0; i < trackedAvoid.length; i++) {
      var a = findTarget(trackedAvoid[i]);
      if (a) avoid.push(viewportRect(a));
    }
    // the menu a select opens is part of what the card must keep off
    if (isMenuSelect(el)) avoid.push(selectMenuRect(el));
    send({ type: "target-rect", rect: el ? viewportRect(el) : null, avoid: avoid, seq: trackedSeq });
  }

  function scheduleTrackedRect() {
    if (!trackedTarget || trackPending) return;
    trackPending = true;
    requestAnimationFrame(function () {
      trackPending = false;
      if (trackedTarget) sendTrackedRect();
    });
  }

  window.addEventListener("scroll", scheduleTrackedRect, true);
  window.addEventListener("resize", scheduleTrackedRect);

  /* ---------- geometry (windowed playback) ---------- */
  function pageRect(target) {
    var el = target ? findTarget(target) : null;
    if (!el) return null;
    var r = topRect(el);
    return { x: r.left + window.scrollX, y: r.top + window.scrollY,
             width: r.width, height: r.height };
  }

  function docSize() {
    var d = document.documentElement;
    return { width: Math.max(d.scrollWidth, d.clientWidth),
             height: Math.max(d.scrollHeight, d.clientHeight) };
  }

  /* ---------- is this capture a dark page? ----------
   * Reported once, on bridge-ready. The player's floating chrome separates
   * from the capture with a two-tone edge, and the tone has to flip: a
   * near-white hairline is invisible on a light page, a near-black one on a
   * dark page. Only the
   * player needs this — nothing here restyles the capture.
   *
   * Read the first background that actually paints, walking <body> then
   * <html>; a page that paints neither is white by default. The threshold is
   * not a guess: L = 0.179 is where a surface's contrast against white and
   * against black are equal, i.e. exactly the point where a page stops taking
   * black text and starts taking white. Below it, our shadow has nothing to
   * darken and the edge has to carry the card alone. */
  function firstPaintedBg() {
    var els = [document.body, document.documentElement], i, c;
    for (i = 0; i < els.length; i++) {
      if (!els[i]) continue;
      c = parseRGB(getComputedStyle(els[i]).backgroundColor);
      if (c) return c;
    }
    return null;
  }

  function parseRGB(s) {
    var m = /^rgba?\(([^)]+)\)/.exec(s || "");
    if (!m) return null;
    var p = m[1].split(",").map(parseFloat);
    if (p.length < 3 || p.some(isNaN)) return null;
    if (p.length > 3 && p[3] < 0.5) return null;   // see-through: keep looking
    return p;
  }

  // WCAG relative luminance.
  function luminance(p) {
    function ch(v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * ch(p[0]) + 0.7152 * ch(p[1]) + 0.0722 * ch(p[2]);
  }

  function pageIsDark() {
    var bg = firstPaintedBg();
    return bg ? luminance(bg) < 0.179 : false;
  }

  // Install on this document and every frame document already loaded;
  // initDoc() plants load listeners so late-loading srcdoc frames join too.
  initFrames();
  if (document.readyState === "complete") retryScroll();
  else window.addEventListener("load", retryScroll);

  send({ type: "bridge-ready", dark: pageIsDark() });
})();
