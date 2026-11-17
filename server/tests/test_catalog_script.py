"""Guards for the catalog's client-side library controls.

The catalog is rendered entirely in the browser: templates/catalog.html ships a
shell and templates/_catalog-script.html builds every card from
/api/walkthroughs. None of that is reachable from a Python test, so the few
properties that are load-bearing are pinned here against the template sources.

Each test below exists because getting it wrong fails silently — the page still
renders, and only the behaviour is quietly gone.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from captcher import templates

TEMPLATE_DIR = Path(templates.TEMPLATE_DIR)
CATALOG = (TEMPLATE_DIR / "catalog.html").read_text(encoding="utf-8")
SCRIPT = (TEMPLATE_DIR / "_catalog-script.html").read_text(encoding="utf-8")

NODE = shutil.which("node")


def poll_interval():
    """The 10s catalog poll's setInterval body.

    There is more than one interval on the page now — the pass-in-flight
    banner ticks a clock every second — so the poll is found by its period
    rather than by being the first one in the file.
    """
    js = script_js()
    end = js.index("}, 10000);")
    return js[js.rindex("setInterval(", 0, end):end]


def script_js():
    """The JS out of _catalog-script.html's single <script> block."""
    m = re.search(r"<script>(.*)</script>", SCRIPT, re.S)
    assert m, "_catalog-script.html no longer has a single <script> block"
    return m.group(1)


def function_source(name):
    """One top-level `function name(...) { … }`, brace-matched.

    Slicing between two function names would depend on the order they happen to
    appear in the file, which is not a property worth pinning.
    """
    js = script_js()
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\b", js, re.M)
    assert m, f"{name}() is no longer a top-level function"
    depth, start = 0, js.index("{", m.start())
    for i in range(start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


class FilterBarTests(unittest.TestCase):
    def test_the_controls_exist(self):
        # lib-track was a <select>; it is now #lib-tracks, a row of .chip
        # buttons built by populateTrackFilter(). See test_track_chips below.
        for element_id in ("lib-bar", "lib-q", "lib-tracks", "lib-sort",
                           "lib-status", "lib-count", "lib-clear"):
            with self.subTest(id=element_id):
                self.assertIn(f'id="{element_id}"', CATALOG,
                              f"catalog.html no longer ships #{element_id}")
                self.assertIn(f'"{element_id}"', script_js(),
                              f"the catalog script no longer reads #{element_id}")

    def test_the_filter_bar_lives_outside_the_repainted_box(self):
        """paintCatalog() does `box.innerHTML = ""` on #cards every 10 seconds.

        A control built inside #cards is therefore destroyed and rebuilt on the
        poll, which eats focus, the caret position and any half-typed search
        term — intermittently, on a 10-second cycle, and it reads as a browser
        bug rather than as ours. Keeping the bar in catalog.html *above* #cards
        makes the poll structurally unable to touch it.
        """
        # #cards is built by server.catalog_page() and spliced in at __CARDS__,
        # so the marker is what the bar has to come before.
        self.assertIn("__CARDS__", CATALOG)
        self.assertLess(
            CATALOG.index('id="lib-bar"'), CATALOG.index("__CARDS__"),
            "the filter bar must be rendered outside (and before) #cards")

    def test_the_authoring_queue_is_never_filtered(self):
        """The queue is a worklist, not a shelf. Hiding a draft that is waiting
        on you is a bug — and the head would read "0 waiting" on every search,
        which looks like the drafts were lost."""
        js = script_js()
        queue_line = next(ln for ln in js.splitlines()
                          if "const queue = list.filter" in ln)
        self.assertNotIn("matchesFilter", queue_line)

    def test_the_track_options_skip_unauthored_placeholders(self):
        """Drafts carry track "TODO" (ingest.py). Building the dropdown from
        the whole list would offer TODO as a track to filter by."""
        self.assertIn('!== "TODO"', script_js())

    def test_typing_repaints_without_refetching(self):
        """A fetch per keystroke would put a request on the wire per character
        for data that cannot have changed. The split is fetch -> lastList ->
        paintCatalog, with the listeners calling only the paint half."""
        js = script_js()
        self.assertIn("function paintCatalog(", js)
        self.assertIn("const repaint = () => paintCatalog(lastList);", js)
        listener = js[js.index('libQ.addEventListener("input"'):]
        listener = listener[:listener.index("});")]
        self.assertNotIn("fetch", listener)

    def test_the_poll_is_still_guarded(self):
        """renderCatalog rebuilds every card, so the poll must stand down while
        a menu is open or an upload/authoring pass is in flight."""
        interval = poll_interval()
        for guard in ("authoringPass", "uploadBusy", "openCardMenu"):
            with self.subTest(guard=guard):
                self.assertIn(guard, interval)

    def test_the_filter_bar_spends_no_accent(self):
        """The accent is capped at two uses per screen wherever a captured
        screen is present, and the catalog's two are already spent. The bar is
        utility chrome, and it is built from system controls rather than
        restyled ones — so it should be sizing them and nothing else."""
        style = SCRIPT[SCRIPT.index("---- library filter bar ----"):
                       SCRIPT.index("---- collections ----")]
        self.assertNotIn("--accent", style)
        self.assertNotIn("#E0921F", style.upper())
        for control in (".input", ".select", ".button"):
            with self.subTest(control=control):
                self.assertIn(control, style,
                              "the bar composes the system's controls rather "
                              "than declaring its own")


class CardStructureTests(unittest.TestCase):
    """The card was one <a> with the overflow-menu <button> appended inside
    it. A button nested in a link is invalid, recovered from differently by
    different browsers, and leaves a screen reader announcing a single link
    carrying a control it cannot reach."""

    def test_the_card_is_not_an_anchor(self):
        card = function_source("renderCard")
        self.assertNotIn('createElement("a")', card,
                         "the card is a <div>; the title carries the link")
        self.assertIn('<div class="card-title"><a href="/player/', card)

    def test_the_menu_button_is_not_inside_the_link(self):
        """It is a sibling of the body, above the title's stretched ::after."""
        card = function_source("renderCard")
        self.assertIn("card.appendChild(menuWrap);", card)
        self.assertIn('menuBtn.setAttribute("aria-label"', card,
                      "outside the link, the button needs its own name")

    def test_the_whole_card_is_still_one_target(self):
        """Moving the link into the title must not shrink the hit area to the
        title's own text box."""
        self.assertIn(".card-title a::after", SCRIPT)
        self.assertIn("position:absolute; inset:0", SCRIPT)

    def test_published_is_not_a_pill(self):
        """Every library card carried PUBLISHED, which is the same on every
        card — a constant is not a status. It cost a line each and diluted
        the states here that mean something."""
        self.assertNotIn('pill--success">PUBLISHED', SCRIPT)

    def test_queued_cards_say_not_authored_and_library_cards_carry_no_pill(self):
        """A queued card is "Not authored"; a library card has no status that
        differs card to card, so it carries no pill."""
        card = function_source("renderCard")
        self.assertIn('pill pill--draft">Not authored', card)
        self.assertNotIn("entitlement", SCRIPT)

    def test_download_and_embed_open_their_dialogs_directly(self):
        menu = function_source("toggleCardMenu")
        self.assertIn("onClick: () => openExportDialog(l)", menu)
        self.assertIn("onClick: () => openEmbedDialog(l)", menu)

    def test_authoring_has_no_purchase_path(self):
        """Open source: there is nothing to buy, so a draft always offers
        Author with AI and the catalog has no offers dialog."""
        self.assertIn("authorWalkthrough(l.id)", function_source("renderCard"))
        self.assertNotIn("402", function_source("authorWalkthrough"))
        for gone in ("unlock-dialog", "openOffersDialog", "checkout", "/plans"):
            self.assertNotIn(gone, SCRIPT)

    def test_review_capture_lives_in_the_menu_and_leads_a_drafts(self):
        self.assertNotIn('href="/review/', function_source("renderCard"))
        menu = function_source("toggleCardMenu")
        self.assertIn("if (ctx.queued) review();", menu)
        self.assertIn("if (!ctx.queued) review();", menu)

    def test_the_diagnostic_tools_are_always_offered(self):
        """Single user: whoever runs the server diagnoses it, so Review
        capture, the AI log and the failure link to it are never hidden."""
        self.assertNotIn("isTeam", SCRIPT)
        self.assertNotIn("is_admin", SCRIPT)
        self.assertIn('"AI log\\u2026"', function_source("toggleCardMenu"))
        outcome = function_source("showAuthoringOutcome")
        self.assertIn("data-open-log=", outcome)

    def test_a_draft_offers_no_download_or_embed(self):
        """Nothing is authored yet, so there is nothing to unlock, download or
        embed; the draft's panel says how to get there."""
        menu = function_source("toggleCardMenu")
        for label in ('"Download\\u2026"', '"Embed\\u2026"'):
            at = menu.index(label)
            self.assertIn("if (!ctx.queued) {", menu[at - 200:at], label)

    def test_the_card_shows_the_capture(self):
        """The product's only permitted imagery is a real captured frame, and
        the shelf is where recognising one matters most."""
        card = function_source("renderCard")
        self.assertIn("renderThumb(card, l);", card)
        thumb = function_source("renderThumb")
        self.assertIn('/thumb"', thumb)
        self.assertIn('img.setAttribute("loading", "lazy")', thumb,
                      "a long shelf must not fetch every frame at once")
        self.assertIn('img.setAttribute("alt", "")', thumb,
                      "decorative: the title beside it is the accessible name")
        self.assertIn('img.addEventListener("error"', thumb,
                      "captures without a frame fall back to the empty plate")
        self.assertNotIn("initialsFor", thumb,
                         "the plate is a spacer; initials read as a monogram "
                         "and repeat the title directly beneath them")

    def test_the_card_summarises_the_task_not_the_persona(self):
        """intro.scenario opens in character, so its first 160 characters name
        a fictional person and never say what the walkthrough teaches.
        completion.summary describes the task."""
        card = function_source("renderCard")
        self.assertIn("l.summary", card)
        self.assertNotIn("scenario.slice", card)
        self.assertIn("-webkit-line-clamp:2", SCRIPT,
                      "clamped by line, not cut at a character count")


class GridTests(unittest.TestCase):
    """The library was a stack in a 1.35fr column beside a 1fr column holding
    only the authoring queue — so ~40% of a desktop viewport was blank in the
    steady state where every walkthrough is authored."""

    def test_the_shelf_is_a_grid(self):
        self.assertIn("grid-template-columns:repeat(auto-fill, minmax(300px, 1fr))",
                      SCRIPT)

    def test_grid_children_are_clamped(self):
        """A grid child's default min-width is auto, so one long unbroken
        title would push its column past the container."""
        self.assertIn("minmax(300px, 1fr)", SCRIPT)
        self.assertIn("min-width:0", SCRIPT)

    def test_the_page_has_one_content_column(self):
        for gone in ('class="col-main"', 'class="col-side"',
                     "grid-template-columns:1.35fr"):
            with self.subTest(rule=gone):
                self.assertNotIn(gone, CATALOG)

    def test_a_card_and_its_panels_travel_as_one_cell(self):
        """A card can have a status strip and an authoring panel welded under
        it. Appending those to the grid box would make each a cell of its own
        and scatter one walkthrough across three columns."""
        card = function_source("renderCard")
        self.assertIn("box.appendChild(slot);", card)
        self.assertIn("slot.appendChild(strip);", card)
        self.assertIn("slot.appendChild(p);", card)

    def test_the_queue_is_a_strip_not_a_column(self):
        paint = function_source("paintCatalog")
        self.assertIn('queueBox.className = "queue-strip"', paint)


class PassInFlightTests(unittest.TestCase):
    """An authoring pass runs for minutes. It used to say so in one line of
    small text welded under one card in a grid of cards — legible only if you
    were already looking at that card, and invisible from anywhere else on the
    page. Everything here pins the block that replaced it.
    """

    def test_the_banner_is_outside_the_boxes_the_poll_wipes(self):
        """paintCatalog() does `innerHTML = ""` on #cards and #queue. A banner
        built inside either would be destroyed mid-pass on the repaint that
        starts one."""
        self.assertIn('id="authoring-now"', CATALOG)
        self.assertLess(CATALOG.index('id="authoring-now"'),
                        CATALOG.index('id="queue"'),
                        "the banner sits above the queue, not inside it")
        paint = function_source("paintCatalog")
        self.assertNotIn('authoringNow.innerHTML', paint)

    def test_the_banner_names_the_walkthrough(self):
        """"Authoring with AI — turn 3 of 8" on its own does not say which of
        the drafts is being authored, which is the first thing an author with
        a queue wants to know."""
        fn = function_source("renderAuthoringBanner")
        self.assertIn("authoringTitle()", fn)
        title = function_source("authoringTitle")
        self.assertIn("lastList", title,
                      "the title is resolved on every paint: a reconnected "
                      "pass is drawn before the first list lands")

    def test_the_banner_carries_a_clock_and_not_a_progress_bar(self):
        """The pass is an agentic loop that runs until the model stops asking
        for tools, so a fraction would be a guess dressed as a measurement."""
        js = script_js()
        self.assertIn('id="an-elapsed"', js)
        self.assertIn("function elapsedLabel(", js)
        banner = SCRIPT[SCRIPT.index("---- the pass in flight ----"):
                        SCRIPT.index("---- authoring queue ----")]
        self.assertNotIn("progress", banner)

    def test_the_running_state_is_a_word_and_a_fill(self):
        """Status never carries meaning by colour alone."""
        fn = function_source("renderAuthoringBanner")
        self.assertIn("dot dot--recording dot--live", fn)
        self.assertIn("authoring \\u00b7 running", fn)

    def test_the_phase_line_repaints_without_rebuilding_the_page(self):
        """A progress event arrives every few seconds. Rebuilding the catalog
        for one would tear down an open menu and restart the thumbnails."""
        fn = function_source("setAuthoringPhase")
        self.assertIn('getElementById("an-phase")', fn)
        self.assertNotIn("renderCatalog", fn)
        self.assertNotIn("renderAuthoringBanner", fn)

    def test_there_is_exactly_one_stop(self):
        """Stop used to live in the card's status strip as well. Two of them on
        one screen for one job reads as two jobs."""
        self.assertEqual(script_js().count("data-cancel-job="), 1)
        self.assertNotIn("data-cancel-job", function_source("authoringStrip"))

    def test_stop_is_withheld_until_there_is_a_job_to_stop(self):
        """The banner is drawn before /api/author answers, and a Stop that
        does nothing is worse than no Stop."""
        self.assertIn("p.jobId", function_source("renderAuthoringBanner"))

    def test_stopping_survives_a_repaint(self):
        """Cancellation is cooperative, so a progress event can land after the
        click; a rebuild must not draw a fresh, enabled Stop for a pass that is
        already winding down."""
        self.assertIn("stopping", function_source("renderAuthoringBanner"))

    def test_the_tab_title_carries_the_state_and_gives_it_back(self):
        """The block is in the flow rather than pinned, so the tab is what
        says "still running" from another tab — and a stale "Authoring…" left
        behind after the pass ends would be a lie in the one place a person
        cannot see the page to check."""
        js = script_js()
        self.assertIn("BASE_TITLE = document.title", js)
        fn = function_source("renderAuthoringBanner")
        self.assertIn("document.title = BASE_TITLE", fn)
        self.assertIn('document.title = "Authoring', fn)

    def test_the_queue_head_counts_the_running_one_separately(self):
        """"3 waiting" is a lie while one of the three is being authored."""
        self.assertIn('"1 running"', function_source("paintCatalog"))

    def test_the_running_card_is_marked(self):
        """The banner says which walkthrough; the card border is what ties
        that sentence to a cell in the queue."""
        self.assertIn("card--authoring", function_source("renderCard"))
        self.assertIn(".card--authoring", SCRIPT)

    def test_the_banner_is_not_pinned(self):
        """The system reserves fixed positioning for the player's top bar and
        a dialog scrim."""
        banner = SCRIPT[SCRIPT.index("---- the pass in flight ----"):
                        SCRIPT.index("---- authoring queue ----")]
        self.assertNotIn("position:fixed", banner)
        self.assertNotIn("position:sticky", banner)


class StyleBudgetTests(unittest.TestCase):
    """The catalog is where the accent budget is easiest to overspend: it is a
    grid of cards, and every card wants to be the important one."""

    def test_only_the_system_radii_are_used(self):
        """Radii come from the tokens (UI v2: 12px controls, 18px containers,
        pills), or 0 where a welded stack squares off a shared edge.
        A literal pixel radius here is a value drifting away from the system."""
        allowed = {"0", "var(--radius-control)", "var(--radius-container)",
                   "var(--radius-pill)", "50%"}
        for radius in set(re.findall(r"border-radius:\s*([^;}]+)", SCRIPT)):
            with self.subTest(radius=radius):
                self.assertTrue(set(radius.split()) <= allowed,
                                "radii come from the tokens: --radius-control, --radius-container, --radius-pill, or 0")

    def test_the_catalog_spends_no_accent_at_all(self):
        """The catalog's own two accents are spent by the toolbar lockup and
        the notch. Anything amber inside a card is a third."""
        self.assertNotIn("var(--accent)", SCRIPT)
        self.assertNotIn("#E0921F", SCRIPT.upper())

    def test_only_the_system_shadows(self):
        """Flat by default; the soft floating shadow tokens and the focus ring
        are the only elevation. The previous identity's hard offset shadows
        must not come back."""
        for shadow in re.findall(r"box-shadow:\s*([^;}]+)", SCRIPT):
            with self.subTest(shadow=shadow):
                self.assertTrue(
                    "none" in shadow or "inset" in shadow
                    or "--shadow-modal" in shadow or "--shadow-pop" in shadow
                    or "--shadow-soft" in shadow
                    or "--focus-ring" in shadow,
                    f"{shadow!r} is not a Captcher shadow or focus token")
                self.assertNotRegex(shadow, r"\b\d+px \d+px 0 ")


class CollectionsUiTests(unittest.TestCase):
    def test_the_collection_controls_exist(self):
        self.assertIn('id="lib-new"', CATALOG)
        for element_id in ("collection-dialog", "assign-dialog", "collection-name",
                           "collection-desc", "assign-list", "assign-new-name"):
            with self.subTest(id=element_id):
                self.assertIn(f'id="{element_id}"', SCRIPT)

    def test_a_collection_name_is_never_written_as_html(self):
        """Collection names are user input and reach the section head, the
        assign dialog and the card meta. sectionHead used to build its whole
        head with innerHTML, which was safe only while every caller passed a
        literal."""
        head = function_source("sectionHead")
        # the name is a text node whether it lands in the h2 or in the
        # disclosure button inside it
        self.assertIn("createTextNode(title)", head)
        self.assertNotIn("innerHTML", head)
        # the assign dialog's rows and the section description too
        self.assertIn("name.textContent = c.name;", function_source("openAssignDialog"))
        self.assertIn("d.textContent = opts.description;", function_source("paintCatalog"))
        # and the card meta escapes what it interpolates
        self.assertIn("esc(shown)", function_source("alsoInLabel"))

    def test_menu_labels_are_appended_as_text(self):
        self.assertIn("createTextNode(label)", function_source("menuItem"))

    def test_card_status_updates_every_copy_of_a_card(self):
        """A walkthrough in two collections renders two cards with the same
        data-walkthrough; querySelector (singular) would update only the
        first."""
        fn = function_source("setCardStatus")
        self.assertIn("querySelectorAll", fn)
        self.assertNotIn("document.querySelector(", fn)

    def test_a_collection_is_never_resorted_by_the_sort_control(self):
        """Stored order is the author's sequence — that is the whole point of a
        collection for training. The sort control governs Unsorted only."""
        paint = function_source("paintCatalog")
        loop = paint[paint.index("for (const c of collections) {"):paint.index("const loose =")]
        self.assertNotIn("sortComparator", loop)
        self.assertIn("sortComparator", paint[paint.index("const loose ="):])
        self.assertIn("Collections keep the order you filed them in", CATALOG)

    def test_reordering_is_withheld_while_a_filter_is_active(self):
        """Under a filter the order on screen is not the stored order, so
        "move up" would jump the card past something invisible."""
        self.assertIn("reorderable: !filterActive()", script_js())

    def test_the_poll_stands_down_while_a_dialog_is_open(self):
        self.assertIn("anyDialogOpen()", poll_interval())

    def test_the_poll_does_not_repaint_when_nothing_changed(self):
        """paintCatalog() wipes #cards and rebuilds every card, including a
        fresh <img> per card — six times a minute for a library that changes
        twice a day. The guard is on the fetched payload, so a real change
        still paints."""
        self.assertIn("renderCatalog(true)", poll_interval())
        fn = function_source("renderCatalog")
        self.assertIn("if (quiet && signature === lastSignature) return;", fn)

    def test_only_the_poll_may_decide_nothing_happened(self):
        """Every other caller has just changed something the payload cannot
        show — a pass starting or ending, a status strip, an upload notice — so
        they must repaint unconditionally."""
        js = script_js()
        calls = re.findall(r"renderCatalog\(([^)]*)\)", js)
        # the declaration itself and the poll are the only two with an argument
        self.assertEqual(sorted(c for c in calls if c),
                         ["quiet", "true"])

    def test_collapse_state_is_a_viewer_preference(self):
        """It belongs to the person looking, not to the collection — so
        localStorage, wrapped so a blocked store cannot break the catalog."""
        js = script_js()
        self.assertIn('COLLAPSE_KEY = "sc-collections-collapsed"', js)
        self.assertIn("try { return localStorage.getItem(k); } catch", js)

    def test_only_one_collapse_key_is_read(self):
        """There was briefly a second, pre-rebrand key read as a fallback so a
        viewer's collapsed sections survived the rename. That is gone with the
        rest of the old brand — this is a fresh install with no stored state to
        honour, the same reason `auth.API_TOKEN_PREFIX` could be changed freely
        — and a lingering fallback would be a second source of truth for a
        preference, silently winning on any browser that still had the old key.
        """
        js = script_js()
        self.assertNotIn("COLLAPSE_KEY_LEGACY", js)
        for stale in ("ap-collections", "cc-collections", "rc-collections"):
            self.assertNotIn(stale, js)

    def test_membership_is_one_request_not_one_per_collection(self):
        """Three ticked boxes must not be three writes that can half-apply."""
        save = function_source("saveAssign")
        self.assertEqual(save.count('"/api/walkthroughs/"'), 1)
        self.assertIn("collections: chosen", save)

    def test_the_collections_section_spends_no_accent(self):
        style = SCRIPT[SCRIPT.index("---- collections ----"):
                       SCRIPT.index("---- assign-to-collection dialog ----")]
        self.assertNotIn("--accent", style)
        self.assertNotIn("#E0921F", style.upper())

    def test_deleting_a_collection_says_the_walkthroughs_survive(self):
        self.assertIn("The walkthroughs stay in your library", script_js())


class ScriptSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is unavailable")
    def test_the_catalog_script_parses(self):
        """The script is spliced into a page as-is; a syntax error takes the
        whole catalog with it and no Python test would notice."""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(script_js())
            path = f.name
        try:
            proc = subprocess.run([NODE, "--check", path],
                                  capture_output=True, text=True)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_template_carries_a_stray_control_character(self):
        """A literal NUL or other control byte in a template renders into the
        page and turns the file binary to every text tool that touches it."""
        for p in sorted(TEMPLATE_DIR.iterdir()):
            if not p.is_file():
                continue
            with self.subTest(template=p.name):
                text = p.read_text(encoding="utf-8")
                bad = {c for c in text if ord(c) < 32 and c not in "\n\r\t"}
                self.assertFalse(bad, f"{p.name} has control characters: "
                                      f"{sorted(hex(ord(c)) for c in bad)}")


if __name__ == "__main__":
    unittest.main()
