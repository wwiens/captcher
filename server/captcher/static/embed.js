/* Captcher embed — drop-in Help widget for customer sites.
 *
 * Loads an exported Captcher walkthrough in a modal, opened from a
 * floating Help button (default) or from the customer's own trigger.
 * One walkthrough per page. Zero dependencies; every DOM id/class is
 * sc-help-* namespaced and styles live in a dedicated <style> block, so
 * nothing leaks into (or in from) the host page.
 *
 * This is the most constrained surface in the product: it injects UI into
 * somebody else's page. So every injected element states its own box model,
 * font, line-height and colour rather than inheriting the host's, radius is 0
 * on everything, and there are no shadows at all: separation is a scrim and a
 * 1px hairline, so the widget cannot look like a floating card pasted onto
 * someone else's design.
 *
 * Usage — paste before </body> (the server's "Get embed code" fills this in):
 *
 *   <script src="https://captcher.example.com/static/embed.js" defer
 *           data-sc-src="https://captcher.example.com/embed/WALKTHROUGH-ID"
 *           data-sc-title="How to suspend a member"></script>
 *
 * data-sc-src may instead point at a self-hosted copy of the exported
 * HTML file (download it from Captcher and upload it next to your own
 * pages) — anything an <iframe> can load over http(s) works.
 *
 * The data-sc-* attribute prefix is the Captcher prefix, replacing the
 * old data-rc-* one. This IS a breaking change for snippets already pasted
 * into a customer's site: the rebrand chose one prefix over a permanent pair
 * of aliases, so anyone on an old snippet has to re-copy it from "Get embed
 * code". "Get embed code" only ever emits the new prefix.
 *
 * Options (attributes on the script tag):
 *   data-sc-src       (required) URL of the exported walkthrough / embed URL
 *   data-sc-title     accessible label + modal tooltip (default "Help")
 *   data-sc-button    "none" to suppress the floating button and use your
 *                     own trigger instead (see below)
 *   data-sc-position  "bottom-right" (default) | "bottom-left"
 *   data-sc-color     help-button fill (default: Indigo #3B49C6, the app
 *                     tile). Set your own and the mark goes single-colour,
 *                     white or Ink, whichever stays legible on it.
 *   data-sc-width / data-sc-height   fixed modal size in px; without these
 *                     the modal auto-sizes to the walkthrough's captured
 *                     region (the player reports its ideal size), capped to
 *                     the visitor's viewport
 *   data-sc-max-width   caps how wide the modal is allowed to grow; the
 *                     recorded region scales down to fit if it's wider.
 *                     Ignored if data-sc-width is also set.
 *   data-sc-backdrop  "0" to skip dimming the page behind the modal
 *                     (default: on — a translucent scrim over the host page)
 *
 * Custom triggers: any element with a data-captcher-help attribute opens
 * the modal on click, and there is a tiny API:
 *   window.CaptcherHelp.open() / .close() / .toggle()
 *
 * There is exactly one spelling of each name, and no aliases. Earlier
 * spellings were dropped outright at the rebrand rather than carried
 * forever: one name is easier to document than four, and the same re-copy
 * that fixes data-rc-* fixes these.
 */
(function () {
  "use strict";
  if (window.CaptcherHelp) return; // one instance per page

  // ---- Captcher palette, inline. DELIBERATE DUPLICATE of the tokens in
  // static/captcher.css: this file is injected into a page on somebody
  // else's domain, so it cannot link a stylesheet from this server without
  // adding a blocking cross-origin request the host did not ask for (and one
  // that fails outright behind a strict CSP). The ten-name contract is spelled
  // out here and nowhere else in the widget. If a value moves in
  // captcher.css, move it here in the same commit.
  var C = {
    primary: "#3B49C6",   // Indigo — the brand lead, and the You try state
    accent:  "#0A6FA8",   // Sky (deep) — Show me
    launcher: "#3B49C6",  // the Help button's default fill: the Indigo app tile
    bg:      "#F2F3F7",   // Mist
    surface: "#FFFFFF",
    text:    "#14151A",   // Ink
    muted:   "#6B6F7B",
    border:  "#E6E7EC",
    onLead:  "#FFFFFF"    // the only text colour allowed on a lead fill
  };
  // The host page will not have the webfont, so the fallback has to be a real
  // stack rather than a bare "sans-serif".
  var FONT = '"DM Sans", system-ui, -apple-system, "Segoe UI", ' +
             'Roboto, Helvetica, Arial, sans-serif';
  // One soft shadow for the two things that float over the host page: the
  // launcher and the modal. Stated here so a host page's own CSS can never
  // reach in and change it.
  var SHADOW = "0 10px 30px rgba(20,21,26,.18), 0 2px 6px rgba(20,21,26,.08)";

  var script = document.currentScript;
  var cfg = {
    src: script && script.getAttribute("data-sc-src"),
    title: (script && script.getAttribute("data-sc-title")) || "Help",
    button: (script && script.getAttribute("data-sc-button")) !== "none",
    position: (script && script.getAttribute("data-sc-position")) === "bottom-left"
      ? "bottom-left" : "bottom-right",
    color: (script && script.getAttribute("data-sc-color")) || C.launcher,
    width: parseInt(script && script.getAttribute("data-sc-width"), 10) || 0,
    height: parseInt(script && script.getAttribute("data-sc-height"), 10) || 0,
    maxWidth: parseInt(script && script.getAttribute("data-sc-max-width"), 10) || 0,
    backdrop: (script && script.getAttribute("data-sc-backdrop")) !== "0"
  };
  if (!cfg.src) {
    console.warn("[captcher-embed] data-sc-src is missing — nothing to embed.");
    return;
  }

  // The mark takes white on a dark fill and Ink on a light one, so a customer
  // who set data-sc-color still gets a legible mark. On the default Indigo fill
  // the squares keep their brand colours (the fifth goes white, as on the app
  // tile); on a fill we did not choose they take the frame colour (the mono
  // variant) rather than risk five hues against it.
  function isDarkFill(hex) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return true;                       // unknown format: assume dark
    var h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16) / 255;
    var g = parseInt(h.slice(2, 4), 16) / 255;
    var b = parseInt(h.slice(4, 6), 16) / 255;
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.55;
  }
  var markInk = isDarkFill(cfg.color) ? "#FFFFFF" : C.text;
  var brandFill = String(cfg.color).toUpperCase() === C.launcher;

  // Default size until the player reports the region's ideal size (an
  // sc-embed-size message). Fixed data-sc-width/height win over both.
  var wanted = { w: cfg.width || (cfg.maxWidth ? Math.min(1100, cfg.maxWidth) : 1100),
                 h: cfg.height || 720 };
  var sizedW = !!cfg.width, sizedH = !!cfg.height; // explicit sizes beat player hints

  // The embed modal's gutter: 24 on a desktop viewport, 16 on a narrow one.
  var GUTTER = 24, GUTTER_TIGHT = 16, TIGHT_AT = 640;
  var MIN_W = 320, MIN_H = 360;

  function gutter() {
    return window.innerWidth <= TIGHT_AT ? GUTTER_TIGHT : GUTTER;
  }

  var style = document.createElement("style");
  style.textContent =
    // Everything injected states its own box model and type: the host page's
    // reset is not ours, and neither is its font.
    ".sc-help-fab,.sc-help-backdrop,.sc-help-backdrop *{box-sizing:border-box}" +
    ".sc-help-fab,.sc-help-close{font-family:" + FONT + ";font-weight:500;" +
    "font-style:normal;letter-spacing:normal;text-transform:none;margin:0;" +
    "text-align:center;-webkit-font-smoothing:antialiased}" +
    // The launcher is the app tile: rounded, carrying the mark; there is no
    // glyph and no icon set here.
    ".sc-help-fab{position:fixed;bottom:22px;" +
    (cfg.position === "bottom-left" ? "left:22px;" : "right:22px;") +
    "z-index:2147483000;width:52px;height:52px;border:0;border-radius:14px;" +
    "background:" + cfg.color + ";color:" + markInk + ";cursor:pointer;" +
    "box-shadow:" + SHADOW + ";display:flex;align-items:center;" +
    "justify-content:center;padding:0;line-height:0;" +
    // Nothing scales, lifts or glows. Colour changes and that is all.
    "transition:filter 140ms cubic-bezier(.2,0,.2,1)}" +
    ".sc-help-fab:hover{filter:brightness(1.08)}" +
    // Focus is a 2px solid lead outline at 2px offset, the same ring the
    // signed-in product uses. An outline, not a shadow: the system has none.
    ".sc-help-fab:focus-visible{outline:2px solid " + C.primary +
    ";outline-offset:2px}" +
    ".sc-help-fab svg{display:block}" +
    ".sc-help-backdrop{position:fixed;inset:0;z-index:2147483001;" +
    "background:rgba(20,21,26,.55);display:flex;align-items:center;" +
    "justify-content:center;padding:" + GUTTER + "px;opacity:0;" +
    "transition:opacity 140ms cubic-bezier(.2,0,.2,1)}" +
    ".sc-help-backdrop.sc-no-backdrop{background:transparent}" +
    ".sc-help-backdrop.sc-open{opacity:1}" +
    // The modal sits against the host page, and the captured screen sits
    // against the modal: a scrim, a 1px hairline and one soft shadow.
    ".sc-help-modal{position:relative;background:" + C.surface + ";" +
    "border:1px solid " + C.border + ";border-radius:18px;overflow:hidden;" +
    "box-shadow:" + SHADOW + ";min-width:" + MIN_W + "px;" +
    "max-width:calc(100vw - " + (GUTTER * 2) + "px);" +
    "max-height:calc(100vh - " + (GUTTER * 2) + "px)}" +
    // This button overlays a captured screen, so it states every value.
    ".sc-help-close{position:absolute;top:8px;right:8px;z-index:2;width:28px;" +
    "height:28px;border:1px solid " + C.border + ";border-radius:999px;" +
    "background:" + C.surface + ";color:" + C.muted + ";font-size:14px;" +
    "line-height:1;padding:0;cursor:pointer;" +
    "transition:color 140ms cubic-bezier(.2,0,.2,1)," +
    "border-color 140ms cubic-bezier(.2,0,.2,1)}" +
    ".sc-help-close:hover{border-color:" + C.primary + ";color:" + C.text + "}" +
    ".sc-help-close:focus-visible{outline:2px solid " + C.primary +
    ";outline-offset:2px}" +
    ".sc-help-frame{display:block;border:0;width:100%;height:100%;" +
    "background:" + C.surface + "}" +
    "@media (max-width:" + TIGHT_AT + "px){" +
    ".sc-help-backdrop{padding:" + GUTTER_TIGHT + "px}" +
    ".sc-help-modal{max-width:calc(100vw - " + (GUTTER_TIGHT * 2) + "px);" +
    "max-height:calc(100vh - " + (GUTTER_TIGHT * 2) + "px)}}" +
    "@media (prefers-reduced-motion:reduce){" +
    ".sc-help-fab,.sc-help-backdrop,.sc-help-close{transition:none}}";
  document.head.appendChild(style);

  var backdrop = null, frame = null, fab = null, lastFocus = null;

  function modalSize() {
    var m = gutter();
    var maxW = Math.max(MIN_W, window.innerWidth - m * 2);
    var maxH = Math.max(MIN_H, window.innerHeight - m * 2);
    return { w: Math.min(Math.max(wanted.w, MIN_W), maxW),
             h: Math.min(Math.max(wanted.h, MIN_H), maxH) };
  }

  function applySize() {
    if (!backdrop) return;
    var s = modalSize();
    var m = backdrop.firstChild;
    m.style.width = s.w + "px";
    m.style.height = s.h + "px";
  }

  function build() {
    backdrop = document.createElement("div");
    backdrop.className = "sc-help-backdrop" + (cfg.backdrop ? "" : " sc-no-backdrop");
    backdrop.style.display = "none";
    var modal = document.createElement("div");
    modal.className = "sc-help-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-label", cfg.title);
    var close = document.createElement("button");
    close.className = "sc-help-close";
    close.type = "button";
    close.setAttribute("aria-label", "Close help");
    close.innerHTML = "&#10005;";
    close.addEventListener("click", api.close);
    frame = document.createElement("iframe");
    frame.className = "sc-help-frame";
    frame.title = cfg.title;
    frame.setAttribute("allow", "fullscreen");
    // src set on first open — no load cost for visitors who never click Help
    modal.appendChild(close);
    modal.appendChild(frame);
    backdrop.appendChild(modal);
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) api.close();
    });
    document.body.appendChild(backdrop);
  }

  // The exported player reports the ideal size for its captured region
  // (region + player chrome), and tells us when the learner has finished the
  // walkthrough — closing the modal for them beats leaving a "walkthrough
  // complete" card they'd have to dismiss by hand.
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || !d.type) return;
    if (frame && e.source !== frame.contentWindow) return; // only our iframe
    if (d.type === "sc-embed-size") {
      if (!sizedW && d.width > 0) wanted.w = cfg.maxWidth ? Math.min(d.width, cfg.maxWidth) : d.width;
      if (!sizedH && d.height > 0) wanted.h = d.height;
      applySize();
    } else if (d.type === "sc-embed-close") {
      api.close();
    }
  });

  window.addEventListener("resize", function () {
    if (backdrop && backdrop.style.display !== "none") applySize();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && backdrop && backdrop.style.display !== "none") api.close();
  });

  var api = {
    open: function () {
      if (!backdrop) build();
      if (!frame.src) frame.src = cfg.src; // lazy first load; kept alive after
      lastFocus = document.activeElement;
      applySize();
      backdrop.style.display = "flex";
      requestAnimationFrame(function () { backdrop.classList.add("sc-open"); });
      backdrop.querySelector(".sc-help-close").focus();
    },
    close: function () {
      if (!backdrop || backdrop.style.display === "none") return;
      backdrop.classList.remove("sc-open");
      // keep the iframe alive: reopening resumes where the learner left off
      setTimeout(function () { backdrop.style.display = "none"; }, 180);
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    },
    toggle: function () {
      (backdrop && backdrop.style.display !== "none") ? api.close() : api.open();
    }
  };
  window.CaptcherHelp = api;

  function init() {
    if (cfg.button) {
      fab = document.createElement("button");
      fab.className = "sc-help-fab";
      fab.type = "button";
      fab.title = cfg.title;
      fab.setAttribute("aria-label", cfg.title);
      fab.setAttribute("aria-haspopup", "dialog");
      // The Captcher mark: frame in currentColor; squares in brand colour on
      // the default Indigo tile (the Indigo centre square turns white so it
      // reads on the tile), mono on a customer-set fill.
      var sq = brandFill ? ["#FF6A5C", "#29B6FF", "#FFFFFF", "#3ECFA2", "#FF6A5C"]
                         : ["currentColor", "currentColor", "currentColor", "currentColor", "currentColor"];
      fab.innerHTML =
        '<svg width="26" height="26" viewBox="0 0 526 526" fill="none" aria-hidden="true">' +
        '<path d="M230 0h296v50H230zM476 0h50v270h-50zM0 256h50v270H0zM0 476h296v50H0z" fill="currentColor"/>' +
        '<path d="M0 0h72v72H0z" fill="' + sq[0] + '"/>' +
        '<path d="M92 92h90v90H92z" fill="' + sq[1] + '"/>' +
        '<path d="M202 202h122v122H202z" fill="' + sq[2] + '"/>' +
        '<path d="M344 344h90v90h-90z" fill="' + sq[3] + '"/>' +
        '<path d="M454 454h72v72h-72z" fill="' + sq[4] + '"/></svg>';
      fab.addEventListener("click", api.open);
      document.body.appendChild(fab);
    }
    // customer-provided triggers: any element with data-captcher-help.
    // One attribute, no legacy aliases — see the header note.
    document.addEventListener("click", function (e) {
      var t = e.target && e.target.closest && e.target.closest(
        "[data-captcher-help]");
      if (t) { e.preventDefault(); api.open(); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
