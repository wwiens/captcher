#!/usr/bin/env python3
"""
digest.py — compact page digests for the AI authoring pass.

A captured Salesforce page prepares to ~200K chars of markup, of which the
model needs almost none: authoring feeds on the *visible text* (42K chars
across a whole 13-page capture) and on *targeting handles* for the handful of
interactive elements a step can act on. This module parses a captured page
(stdlib html.parser) and renders it as a
digest: a visible-text outline plus an inventory of interactive elements, each
carrying a server-generated selector that has been VERIFIED unique against the
parse tree before the model ever sees it. The model picks elements by ref
("e17"); author.resolve_refs() substitutes the verified target.

The matcher implements exactly the selector grammar the generator emits —
compound simple selectors (tag, #id, .class, [attr], [attr="value"],
:nth-of-type(n)) joined by descendant and child combinators, plus the
target-level `text` / `text_exact` narrowing — with the same semantics as the
player: bridge.js resolves `document.querySelectorAll(t.selector)` and filters
by whitespace-normalised textContent (containment for `text`, equality for
`text_exact`). Anything outside that grammar makes match() return None
("cannot evaluate"), never a guess: validate() must not report false verdicts
on selectors the model or an old walkthrough wrote in fuller CSS.
"""
import itertools
import re
from html.parser import HTMLParser

OUTLINE_MAX_CHARS = 8_000       # per-page visible-text outline budget
MAX_ELEMENTS_PER_PAGE = 400     # inventory cap; dialog contents keep priority
SRC_EXCERPT_CHARS = 260         # start-tag excerpt (capture_edits anchor);
                                # class/style elided, handlers clipped — see
                                # _src_excerpt
NAME_CHARS = 60                 # accessible-name cap in inventory lines
TEXT_EXACT_MAX = 80             # longest text we anchor a target on


class DigestError(Exception):
    """A page this module could not digest; callers fall back to raw HTML."""


# ------------------------------------------------------------------- classes
#
# Build-generated (hashed) class and id tokens. They change on the vendor's
# next deploy, so they are what selector generation must prefer NOT to lean on
# — though on a frozen snapshot even a generated id resolves exactly, which is
# why the ladder still uses one as a late resort. Authored tokens
# (`fusion-leftrail--item`, `slds-button`) must survive; the probes in
# tests/test_author_prune.py pin both directions for classes.
# (These lived in author.py; author.py re-exports them so its tests hold.)

_CLS_B64 = re.compile(r"^[A-Za-z0-9+/_-]{16,}={1,2}$")            # Ywjq71DjN…yEHfxIw==
_CLS_SC_NAMED = re.compile(r"^[A-Za-z]+__[A-Za-z]+-[0-9a-z]{6,}-\d+$")  # Header__Wrap-paluxf-0
_CLS_SC_TAIL = re.compile(r"-sc-[0-9a-z]{6,}-\d+$")               # Avatar__Span-sc-19y5ooy-1
_CLS_HEX_TAIL = re.compile(r"-[0-9a-f]{6,10}$")                   # Typography-body-2-e793b1d
_CLS_MOD_TAIL = re.compile(r"--[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*$")     # qbss-foo--2dtZ-qcz
_CLS_EMOTION = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])[A-Za-z]{6,7}$")     # ladnjH gpULFc
# A trailing 5-6 char segment that is not a plain word: txp-capability-…-qNmcA.
# The negative lookaheads keep real trailing words (…-Inner-Secondary).
_CLS_RAND_TAIL = re.compile(
    r"-(?![A-Z][a-z]+$)(?![a-z]+$)(?![A-Z]+$)[A-Za-z0-9+_]{5,6}$")


def is_generated_class(tok: str) -> bool:
    """True for a build-generated (hashed) class token."""
    return bool(_CLS_B64.match(tok) or _CLS_SC_NAMED.match(tok)
                or _CLS_SC_TAIL.search(tok) or _CLS_HEX_TAIL.search(tok)
                or _CLS_MOD_TAIL.search(tok) or _CLS_EMOTION.match(tok)
                or _CLS_RAND_TAIL.search(tok))


_ID_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_ID_HEX_TAIL = re.compile(r"[0-9a-f]{6,}$", re.I)


def is_generated_id(tok: str) -> bool:
    """True for a framework-generated id (Aura `695:0`, React `:r1:`,
    `ember123`, uuids, numbered instance ids). These stay usable on a frozen
    snapshot — the ladder ranks them low for re-capture friendliness, not
    because they would miss."""
    if not tok:
        return True
    if ":" in tok or tok.isdigit():
        return True
    if re.match(r"^ember\d+$", tok):
        return True
    if _ID_UUID.search(tok):
        return True
    if re.search(r"[-_]\d+$", tok):                      # input-39, brandBand_2
        return True
    tail = _ID_HEX_TAIL.search(tok)
    if tail and re.search(r"\d", tail.group(0)):         # deadbeef1, a0b1c2
        return True
    return False


# ---------------------------------------------------------------------- tree

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
# Content that never reaches the digest. script/style arrive as CDATA in
# html.parser (no nested tags fire); template/noscript contents do parse, so
# a skip depth suppresses them.
_SKIP = {"script", "style", "template", "noscript"}
# tag → set of sibling tags whose opening implies this one's end tag. SingleFile
# serialises a live DOM so end tags are normally present; this is belt and
# braces for hand-written fixture HTML and the odd omission.
_IMPLIES_END = {
    "li": {"li"},
    "p": {"p", "div", "ul", "ol", "table", "section", "article", "aside",
          "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "form",
          "fieldset", "blockquote", "pre"},
    "option": {"option", "optgroup"},
    "optgroup": {"optgroup"},
    "tr": {"tr"},
    "td": {"td", "th", "tr"},
    "th": {"td", "th", "tr"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "thead": {"tbody", "tfoot"},
    "tbody": {"tbody", "tfoot"},
}

_WS_RE = re.compile(r"\s+")


def norm(s: str) -> str:
    """bridge.js norm(): collapse whitespace, trim. The matcher's text keys
    must judge exactly the way the player will."""
    return _WS_RE.sub(" ", s or "").strip()


class Node:
    __slots__ = ("tag", "attrs", "parent", "children", "src", "frame_tree",
                 "_norm", "_nth")

    def __init__(self, tag, attrs, parent, src=None):
        self.tag = tag              # lowercased by html.parser
        self.attrs = attrs          # dict, first occurrence wins
        self.parent = parent
        self.children = []          # Nodes and text strings, document order
        self.src = src              # raw start-tag text from the file
        self.frame_tree = None      # iframe with captured srcdoc: its own Tree
        self._norm = None
        self._nth = None

    def norm_text(self) -> str:
        """Whitespace-normalised textContent, cached."""
        if self._norm is None:
            self._norm = norm("".join(self._raw_text()))
        return self._norm

    def _raw_text(self):
        for c in self.children:
            if isinstance(c, str):
                yield c
            else:
                yield from c._raw_text()

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def element_children(self):
        return [c for c in self.children if isinstance(c, Node)]

    def nth_of_type(self) -> int:
        """1-based position among same-tag element siblings (CSS semantics).
        Cached — a 3000-row table would otherwise make every check quadratic."""
        if self._nth is not None:
            return self._nth
        if self.parent is None:
            self._nth = 1
            return 1
        counts = {}
        for sib in self.parent.children:
            if isinstance(sib, Node):
                counts[sib.tag] = counts.get(sib.tag, 0) + 1
                sib._nth = counts[sib.tag]
        return self._nth


class Tree:
    """Parsed page plus the indexes the matcher leans on."""

    def __init__(self, root):
        self.root = root
        self.nodes = []             # every element, document order
        self.by_id = {}             # id -> [Node] (duplicates happen in the wild)
        self.by_tag = {}            # tag -> [Node]
        stack = [root]
        while stack:
            node = stack.pop()
            for c in reversed(node.element_children()):
                stack.append(c)
            if node is root:
                continue
            self.nodes.append(node)
            self.by_tag.setdefault(node.tag, []).append(node)
            i = node.attrs.get("id")
            if i:
                self.by_id.setdefault(i, []).append(node)
        self.order = {id(n): i for i, n in enumerate(self.nodes)}


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node(None, {}, None)
        self.stack = [self.root]
        self.skip = 0

    def _attrs(self, attrs):
        d = {}
        for k, v in attrs:
            if k not in d:
                d[k] = "" if v is None else v
        return d

    def handle_starttag(self, tag, attrs):
        # Skip depth counts EVERY _SKIP start tag, even while already
        # skipping: script/style content is CDATA (no nested tags fire), but a
        # <template> inside a <noscript> does, and its </template> must not
        # end the noscript skip early.
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        top = self.stack[-1]
        while top.tag and tag in _IMPLIES_END.get(top.tag, ()):
            self.stack.pop()
            top = self.stack[-1]
        node = Node(tag, self._attrs(attrs), top, src=self.get_starttag_text())
        top.children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag in _SKIP:
            return
        top = self.stack[-1]
        top.children.append(Node(tag, self._attrs(attrs), top,
                                 src=self.get_starttag_text()))

    def handle_endtag(self, tag):
        if self.skip:
            if tag in _SKIP:
                self.skip -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # stray end tag: ignore, exactly as a browser would

    def handle_data(self, data):
        if self.skip or not data:
            return
        # Whitespace-only nodes are still text to the browser, and textContent
        # concatenates them: that is what puts a space between two <td>s
        # written on their own lines. Dropping them made norm_text() disagree
        # with bridge.js norm(textContent) for every container whose children
        # are laid out one per line — a table row, a list item, a card — so a
        # row reading "Fall sale Draft All subscribers" in the player was
        # "Fall saleDraftAll subscribers" here. An authored text_exact then
        # matched in match(), passed the verify pass, and could never match at
        # play time. Collapsed to a single space rather than kept verbatim so
        # the tree does not carry a copy of the file's indentation; norm()
        # would collapse it to exactly this anyway.
        self.stack[-1].children.append(data if data.strip() else " ")


# Captured same-origin iframes arrive inlined as a `srcdoc` attribute (that is
# how the capture engine serializes them). Each parses into its own Tree hung
# off the iframe's Node — a separate document, exactly as the browser treats
# it, so selectors never leak across the boundary. Depth-capped: srcdoc can
# nest, and each level multiplies the escaping.
MAX_FRAME_DEPTH = 3


def parse_page(html: str, _depth: int = 0) -> Tree:
    """Parse a captured page. Raises DigestError on anything that does not
    yield a usable element tree — the caller's cue to fall back to raw HTML."""
    try:
        b = _Builder()
        b.feed(html)
        b.close()
    except Exception as e:  # html.parser is lenient; anything it raises is exotic
        raise DigestError(f"page failed to parse: {e}")
    tree = Tree(b.root)
    if not tree.nodes:
        raise DigestError("page parsed to an empty element tree")
    if _depth < MAX_FRAME_DEPTH:
        for node in tree.by_tag.get("iframe", []):
            doc = node.attrs.get("srcdoc")
            if not doc or not doc.strip():
                continue
            try:
                node.frame_tree = parse_page(doc, _depth + 1)
            except DigestError:
                pass                # unparseable frame: outer page still digests
    return tree


# ------------------------------------------------------------------- matcher

_SEL_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<child>>)
  | (?P<tag>[a-zA-Z][a-zA-Z0-9-]*)
  | \#(?P<id>[-\w]+)
  | \.(?P<cls>[-\w]+)
  | \[(?P<aname>[a-zA-Z][\w-]*)
      (?:=(?P<aval>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s\]"']+))?\]
  | :nth-of-type\((?P<nth>\d+)\)
""", re.X)


def _unquote(v):
    if v and v[0] in "\"'" and v[-1] == v[0] and len(v) >= 2:
        body = v[1:-1]
        return re.sub(r"\\(.)", r"\1", body)
    return v


def _parse_selector(sel: str):
    """→ (compounds, combinators) or None when out of grammar.
    combinators[i] sits between compounds[i] and compounds[i+1]."""
    sel = sel.strip()
    compounds, combinators = [], []
    cur = None
    pending = None          # combinator seen since last compound
    pos, n = 0, len(sel)
    while pos < n:
        m = _SEL_TOKEN.match(sel, pos)
        if not m:
            return None
        pos = m.end()
        if m.group("ws"):
            if cur is not None:
                pending = pending or " "
            continue
        if m.group("child"):
            if cur is None:
                return None
            pending = ">"
            continue
        if cur is None or pending is not None:
            if cur is not None:
                combinators.append(pending)
            cur = {"tag": None, "id": None, "classes": [], "attrs": [], "nth": None}
            compounds.append(cur)
            pending = None
        if m.group("tag"):
            if cur["tag"] is not None:     # "div p" without space is impossible;
                return None                # a second tag in one compound is not CSS
            cur["tag"] = m.group("tag").lower()
        elif m.group("id"):
            cur["id"] = m.group("id")
        elif m.group("cls"):
            cur["classes"].append(m.group("cls"))
        elif m.group("aname"):
            cur["attrs"].append((m.group("aname").lower(), _unquote(m.group("aval"))
                                 if m.group("aval") is not None else None))
        elif m.group("nth"):
            cur["nth"] = int(m.group("nth"))
    if not compounds:
        return None
    return compounds, combinators


def _match_compound(node: Node, c) -> bool:
    if c["tag"] is not None and node.tag != c["tag"]:
        return False
    if c["id"] is not None and node.attrs.get("id") != c["id"]:
        return False
    for cls in c["classes"]:
        if cls not in node.classes():
            return False
    for name, val in c["attrs"]:
        if name == "class" and val is not None:
            # [class="…"] compares the full attribute, which the generator
            # never emits; keep exact-value semantics anyway
            if node.attrs.get(name) != val:
                return False
            continue
        if name not in node.attrs:
            return False
        if val is not None and node.attrs.get(name) != val:
            return False
    if c["nth"] is not None and node.nth_of_type() != c["nth"]:
        return False
    return True


def _chain_matches(node: Node, compounds, combinators, i) -> bool:
    """compounds[i+1] already matched at `node`; satisfy compounds[0..i]."""
    if i < 0:
        return True
    comb = combinators[i]
    if comb == ">":
        p = node.parent
        return (p is not None and p.tag is not None
                and _match_compound(p, compounds[i])
                and _chain_matches(p, compounds, combinators, i - 1))
    anc = node.parent
    while anc is not None and anc.tag is not None:
        if _match_compound(anc, compounds[i]) and \
                _chain_matches(anc, compounds, combinators, i - 1):
            return True
        anc = anc.parent
    return False


def _compiled(tree: Tree, target: dict):
    """→ (candidate Nodes, compounds, combinators) or None if out of grammar."""
    sel = (target or {}).get("selector")
    if not sel:
        return None
    parsed = _parse_selector(sel)
    if parsed is None:
        return None
    compounds, combinators = parsed
    last = compounds[-1]
    if last["id"] is not None:
        candidates = tree.by_id.get(last["id"], [])
    elif last["tag"] is not None:
        candidates = tree.by_tag.get(last["tag"], [])
    else:
        # a bare [attr=…] compound: candidate narrowing by attribute id is the
        # common case (the [id="…"] form the generator emits for exotic ids)
        for name, val in last["attrs"]:
            if name == "id" and val is not None:
                candidates = tree.by_id.get(val, [])
                break
        else:
            candidates = tree.nodes
    return candidates, compounds, combinators


def _iter_matches(tree: Tree, target: dict, compiled):
    candidates, compounds, combinators = compiled
    last = compounds[-1]
    text_exact = (target or {}).get("text_exact")
    text = (target or {}).get("text")
    for n in candidates:
        if not _match_compound(n, last):
            continue
        if not _chain_matches(n, compounds, combinators, len(compounds) - 2):
            continue
        if text_exact:
            if n.norm_text() != text_exact:
                continue
        elif text:
            if text not in n.norm_text():
                continue
        yield n


def match(tree: Tree, target: dict):
    """Resolve a walkthrough target against the tree, player-faithfully.

    Returns the matched Nodes in document order, or None when the selector
    uses CSS outside the generated grammar (= cannot evaluate, no verdict).

    A target with `frames` descends first: each hop is resolved with
    querySelector semantics (FIRST match, no text filter — exactly what the
    bridge does) and the walk continues in that iframe's own tree. A hop that
    lands on a frame whose content was not captured is a definite miss ([]),
    the same empty document the player would search."""
    for fsel in (target or {}).get("frames") or []:
        hits = match(tree, {"selector": fsel})
        if hits is None:
            return None                 # hop out of grammar: no verdict
        node = hits[0] if hits else None
        if node is None:
            return []
        if node.frame_tree is None:
            return []                   # frame not captured: empty document
        tree = node.frame_tree
    if (target or {}).get("frames"):
        target = {k: v for k, v in target.items() if k != "frames"}
    compiled = _compiled(tree, target)
    if compiled is None:
        return None
    return list(_iter_matches(tree, target, compiled))


# -------------------------------------------------------- selector generation

_CSS_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


# `^data-sc-` is Captcher's own capture stamps (data-sc-scroll: a scroll
# offset like "191 0" — short, unique-ish, and meaningless as a handle).
# Same pattern as recorder.js BOOKKEEPING_DATA; change them together.
_BOOKKEEPING_DATA = re.compile(r"^data-sc-|react|v-|vue|ng-|key$|index|hash|uid|guid|-id$|reactid")


def _local_candidates(node: Node):
    """Ordered per-element handles, best first (ladder rungs 1-4)."""
    out = []
    a = node.attrs
    id_ = a.get("id")
    if id_ and not is_generated_id(id_):
        out.append("#" + id_ if _CSS_IDENT.match(id_) else f"[id={_quote(id_)}]")
    for name in ("name", "data-testid", "data-id"):
        if a.get(name):
            out.append(f"{node.tag}[{name}={_quote(a[name])}]")
    # Other authored data-* attributes (data-stage="Proposal", data-status=
    # "open") name a lane or a column the way an id names a control. Short
    # values only — a sentence-long data-empty message is a label, not a
    # handle — and never the framework bookkeeping kinds.
    for name, val in a.items():
        if (name.startswith("data-") and name not in ("data-testid", "data-id")
                and val and len(val) <= 24 and not _BOOKKEEPING_DATA.search(name)
                and not is_generated_class(val)):
            out.append(f"{node.tag}[{name}={_quote(val)}]")
    for name in ("aria-label", "placeholder"):
        if a.get(name):
            out.append(f"{node.tag}[{name}={_quote(a[name])}]")
    authored = [c for c in node.classes() if not is_generated_class(c)][:2]
    if authored:
        out.append(node.tag + "".join("." + c for c in authored
                                      if _CSS_IDENT.match(c)))
    out.append(node.tag)
    return out


def _unique(tree: Tree, node: Node, target: dict) -> bool:
    """True when target matches exactly `node` and nothing else. Stops at the
    second hit — the ladder probes many losing candidates on a big page, and
    'matches 3000 times' must cost two hits, not three thousand."""
    compiled = _compiled(tree, target)
    if compiled is None:
        return False
    found, others = False, 0
    for n in _iter_matches(tree, target, compiled):
        if n is node:
            found = True
        else:
            others += 1
            if others:
                return False
    return found


_MAX_SCOPE_ANCESTORS = 15


def target_for(tree: Tree, node: Node) -> dict:
    """The verified-unique target for an element — the ladder.

    Every returned target has been resolved against the tree and matched
    exactly this node; there is no unverified branch.
    """
    locals_ = _local_candidates(node)

    for sel in locals_:                                      # rungs 1-4
        if _unique(tree, node, {"selector": sel}):
            return {"selector": sel}

    anc, hops = node.parent, 0                               # rung 5: scoping
    while anc is not None and anc.tag is not None and hops < _MAX_SCOPE_ANCESTORS:
        for asel in _local_candidates(anc)[:-1]:             # never bare-tag scopes
            if not _unique(tree, anc, {"selector": asel}):
                continue
            for lsel in locals_:
                t = {"selector": f"{asel} {lsel}"}
                if _unique(tree, node, t):
                    return t
            break                                            # unique anchor found; deeper scoping won't help
        anc, hops = anc.parent, hops + 1

    text = node.norm_text()                                  # rung 6: text anchor
    if text and len(text) <= TEXT_EXACT_MAX:
        for lsel in locals_:
            t = {"selector": lsel, "text_exact": text}
            if _unique(tree, node, t):
                return t

    id_ = node.attrs.get("id")                               # rung 7: generated id
    if id_ and is_generated_id(id_):
        t = {"selector": f"[id={_quote(id_)}]"}
        if _unique(tree, node, t):
            return t

    # rung 8: child chain with :nth-of-type from the nearest unique ancestor
    segs, cur = [], node
    while cur is not None and cur.tag is not None:
        segs.append(f"{cur.tag}:nth-of-type({cur.nth_of_type()})")
        anchor = None
        p = cur.parent
        if p is not None and p.tag is not None:
            for asel in _local_candidates(p)[:-1]:
                if _unique(tree, p, {"selector": asel}):
                    anchor = asel
                    break
        if anchor or p is None or p.tag is None:
            sel = (f"{anchor} > " if anchor else "") + " > ".join(reversed(segs))
            t = {"selector": sel}
            if _unique(tree, node, t):
                return t
        cur = p
    # Structurally the full-path chain identifies exactly one node; reaching
    # here means duplicated trees the parser folded — surface it honestly.
    raise DigestError("no unique selector derivable for element")


# ------------------------------------------------------------------ digest


_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK = {"address", "article", "aside", "blockquote", "body", "caption",
          "dd", "details", "dialog", "div", "dl", "dt", "fieldset",
          "figcaption", "figure", "footer", "form", "header", "hr", "html",
          "li", "main", "nav", "ol", "p", "pre", "section", "table",
          "tbody", "tfoot", "thead", "tr", "ul"}
_INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea", "summary"}
_INTERACTIVE_ROLES = {"button", "link", "tab", "menuitem", "menuitemcheckbox",
                      "menuitemradio", "option", "checkbox", "radio",
                      "combobox", "switch", "textbox", "searchbox"}


def _is_hidden(node: Node) -> bool:
    """Only the `hidden` attribute counts — deliberately NOT aria-hidden.

    aria-hidden is an accessibility annotation, and real pages abuse it:
    Salesforce Lightning wraps its record-edit modal in
    <div aria-hidden="true" class="uiModal--…"> while the modal is the very
    thing on screen. Honouring it dropped every form field from the digest and
    the authoring pass wrote the form entry out of the walkthrough. `hidden`
    is different: browsers actually apply display:none to it, so an element
    under one genuinely cannot be clicked in the player."""
    return "hidden" in node.attrs


def _in_dialog(node: Node) -> bool:
    cur = node
    while cur is not None and cur.tag is not None:
        if cur.tag == "dialog" or cur.attrs.get("role") in ("dialog", "alertdialog") \
                or cur.attrs.get("aria-modal") == "true":
            return True
        cur = cur.parent
    return False


def _label_index(tree: Tree):
    """input id → associated <label> text, plus node → wrapping label text."""
    by_for, wrapping = {}, {}
    for lab in tree.by_tag.get("label", []):
        text = lab.norm_text()
        if not text:
            continue
        if lab.attrs.get("for"):
            by_for.setdefault(lab.attrs["for"], text)
        else:
            stack = list(lab.element_children())
            while stack:
                n = stack.pop()
                if n.tag in ("input", "select", "textarea"):
                    wrapping.setdefault(id(n), text)
                stack.extend(n.element_children())
    return by_for, wrapping


def _accessible_name(node: Node, tree: Tree, by_for, wrapping) -> str:
    a = node.attrs
    if a.get("aria-label"):
        return a["aria-label"]
    if a.get("aria-labelledby"):
        parts = []
        for ref in a["aria-labelledby"].split():
            for n in tree.by_id.get(ref, [])[:1]:
                parts.append(n.norm_text())
        if any(parts):
            return " ".join(p for p in parts if p)
    if node.tag in ("input", "select", "textarea"):
        if a.get("id") and a["id"] in by_for:
            return by_for[a["id"]]
        if id(node) in wrapping:
            return wrapping[id(node)]
    text = node.norm_text()
    if text:
        return text
    for name in ("placeholder", "title", "alt", "value"):
        if a.get(name):
            return a[name]
    return ""


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


# Inline event-handler attributes: onclick, oninput, onchange … — the page's
# wiring. `^on[a-z]+$` and not a prefix test, so a data attribute that merely
# starts with "on" (onboarding-step=…) is not mistaken for one.
_EVENT_ATTR = re.compile(r"^on[a-z]+$")
# One attribute span in a raw start tag, quoting styles as captured pages
# actually write them (double, single, none).
_ATTR_SPAN = re.compile(
    r"""\s+(?P<name>[^\s=/>'"]+)(?:\s*=\s*(?P<val>"[^"]*"|'[^']*'|[^\s>]+))?""")
HANDLER_EXCERPT_CHARS = 110     # per inline-handler body, clipped individually


def _src_excerpt(src: str) -> str:
    """The src excerpt, budget spent on signal.

    The raw start tag used to be clipped from the front, and on framework
    pages the class attribute alone (300+ chars of Tailwind) ate the whole
    budget — an oninput at offset 486 of a 1,207-char tag was simply absent
    from the digest, so the model could not know the page was reactive
    (FIELD-NOTES material: the partial-payment capture, 2026-08-14).

    Drops class/style spans (each replaced by a visible " …"), clips each
    inline-handler body individually rather than losing everything after the
    first long one, and keeps every other attribute verbatim. Elisions are
    marked because the excerpt is a capture_edits anchor: a find string must
    be a contiguous run copied from between the … marks."""
    m = re.match(r"<[a-zA-Z][^\s/>]*", src)
    if not m:
        return src
    out = [m.group(0)]
    for a in _ATTR_SPAN.finditer(src, m.end()):
        name = a.group("name").lower()
        if name in ("class", "style"):
            if out[-1] != " …":
                out.append(" …")
            continue
        chunk = a.group(0)
        val = a.group("val")
        if _EVENT_ATTR.match(name) and val and len(val) > HANDLER_EXCERPT_CHARS:
            q = val[0] if val[0] in "\"'" else ""
            body = val[1:-1] if q else val
            chunk = (f' {a.group("name")}={q}'
                     f'{body[:HANDLER_EXCERPT_CHARS - 1]}…{q}')
        out.append(chunk)
    out.append("/>" if src.rstrip().endswith("/>") else ">")
    return "".join(out)


def _interactive_nodes(tree: Tree):
    picked = []
    for node in tree.nodes:
        if _is_hidden(node):
            continue
        cur = node.parent
        hidden = False
        while cur is not None and cur.tag is not None:
            if _is_hidden(cur):
                hidden = True
                break
            cur = cur.parent
        if hidden:
            continue
        tag, a = node.tag, node.attrs
        if tag == "input" and a.get("type") == "hidden":
            continue
        if tag == "option":
            continue                       # rendered on its <select> line
        if tag in _INTERACTIVE_TAGS or a.get("role") in _INTERACTIVE_ROLES:
            picked.append(node)
        elif tag == "tr" and any(c.tag == "td" for c in node.element_children()):
            picked.append(node)
        elif _is_draggable(node):
            picked.append(node)
    if len(picked) > MAX_ELEMENTS_PER_PAGE:
        in_dialog = [n for n in picked if _in_dialog(n)]
        rest = [n for n in picked if not _in_dialog(n)]
        picked = (in_dialog + rest)[:MAX_ELEMENTS_PER_PAGE]
        picked.sort(key=lambda n: tree.order[id(n)])
    return picked


def _is_draggable(node: Node) -> bool:
    """An element the page lets the user pick up. Only an explicit
    draggable="true" counts: links and images are draggable by default in
    every browser, and that default is never a product affordance."""
    return (node.attrs.get("draggable") or "").strip().lower() == "true"


MAX_DROPZONES_PER_PAGE = 40
_DROP_ATTRS = ("ondrop", "ondragover", "ondragenter", "aria-dropeffect")


def _lane_signature(node: Node):
    return (node.tag, tuple(sorted(c for c in node.classes()
                                   if not is_generated_class(c))))


def _dropzone_nodes(tree: Tree, draggables, taken):
    """Where a draggable can be released — the other half of a drag step.

    Two sources. Explicit: an element wired for drops (inline drop handlers,
    aria-dropeffect). Structural: the LANES a draggable lives in — walk up
    from each draggable to the nearest ancestor that has same-shaped siblings
    (same tag and authored classes: the columns of a board, the lists of a
    sorter) and take every sibling, the source lane included, because a drag
    back to where it started is a legitimate step too. `taken` are nodes
    already inventoried as interactive; a zone that is also a control keeps
    its interactive line."""
    zones, seen = [], set()

    def add(n):
        if id(n) in seen or id(n) in taken or _is_hidden(n):
            return
        seen.add(id(n))
        zones.append(n)

    for node in tree.nodes:
        if any(node.attrs.get(k) for k in _DROP_ATTRS) \
                or any(k.startswith("data-drop") for k in node.attrs):
            add(node)
    for d in draggables:
        lane, hops = d.parent, 0
        while lane is not None and lane.tag is not None and hops < 4:
            parent = lane.parent
            if parent is None or parent.tag is None:
                break
            sig = _lane_signature(lane)
            peers = [s for s in parent.element_children()
                     if _lane_signature(s) == sig and not _is_draggable(s)]
            if len(peers) >= 2:
                for peer in peers:
                    add(peer)
                break
            lane, hops = parent, hops + 1
    zones.sort(key=lambda n: tree.order[id(n)])
    return zones[:MAX_DROPZONES_PER_PAGE]


def _zone_name(node: Node) -> str:
    """A drop zone is named by its heading when it has one — "Negotiation",
    not the text of every card inside it."""
    stack = list(reversed(node.element_children()))
    while stack:
        n = stack.pop()
        if n.tag in _HEADINGS or n.attrs.get("role") == "heading":
            text = n.norm_text()
            if text:
                return text
        stack.extend(reversed(n.element_children()))
    return node.norm_text()


# Non-interactive elements worth naming: the values a page DISPLAYS. A running
# total, a status pill, a validation message — the things edit instructions
# are about at least as often as the controls. They are inventoried read-only:
# an anchor for capture_edits, never a step target (nothing can click a total).
_DISPLAY_ROLES = {"status", "alert", "timer", "progressbar"}
MAX_DISPLAY_PER_PAGE = 120


def _display_nodes(tree: Tree, taken):
    """Named display elements: carrying data-testid, aria-live, a display
    role, or being an <output> — and not already in the interactive set."""
    picked = []
    for node in tree.nodes:
        if id(node) in taken or _is_hidden(node):
            continue
        cur, hidden = node.parent, False
        while cur is not None and cur.tag is not None:
            if _is_hidden(cur):
                hidden = True
                break
            cur = cur.parent
        if hidden:
            continue
        a = node.attrs
        if (a.get("data-testid") or a.get("aria-live")
                or a.get("role") in _DISPLAY_ROLES or node.tag == "output"):
            picked.append(node)
    return picked[:MAX_DISPLAY_PER_PAGE]


def _display_line(ref, node: Node, tree: Tree, by_for, wrapping) -> str:
    a = node.attrs
    parts = [f"{ref} {node.tag}"]
    name = norm(_accessible_name(node, tree, by_for, wrapping))
    if name:
        parts[0] += f' "{_clip(name, NAME_CHARS)}"'
    for attr in ("data-testid", "id", "role", "aria-live"):
        if a.get(attr) and a[attr] != name:
            parts.append(f"{attr}={_clip(a[attr], 40)}")
    events = sorted(k for k in a if _EVENT_ATTR.match(k))
    if events:
        parts.append("reactive: " + ",".join(events))
    if node.src:
        parts.append(f"src: {_clip(norm(_src_excerpt(node.src)), SRC_EXCERPT_CHARS)}")
    return "  " + " | ".join(parts)


def _outline(tree: Tree) -> str:
    lines, seen = [], set()
    buf = []

    def flush():
        text = norm(" ".join(buf))
        buf.clear()
        if text and text not in seen:
            seen.add(text)
            lines.append("  " + text)

    def walk(node: Node):
        for c in node.children:
            if isinstance(c, str):
                buf.append(c)
                continue
            if _is_hidden(c) or c.tag == "select":
                continue                    # options live on the inventory line
            if c.tag in _HEADINGS or c.attrs.get("role") == "heading":
                flush()
                text = c.norm_text()
                if text and text not in seen:
                    seen.add(text)
                    level = _HEADINGS.get(c.tag) or c.attrs.get("aria-level") or "?"
                    lines.append(f"  [h{level}] {text}")
                continue
            if c.tag in _BLOCK:
                flush()
                walk(c)
                flush()
            else:
                walk(c)

    walk(tree.root)
    flush()
    out = "\n".join(lines)
    if len(out) > OUTLINE_MAX_CHARS:
        head = out[:int(OUTLINE_MAX_CHARS * 0.65)]
        tail = out[-int(OUTLINE_MAX_CHARS * 0.3):]
        head = head[:head.rfind("\n")]
        tail = tail[tail.find("\n") + 1:]
        out = head + "\n  … (outline elided) …\n" + tail
    return out


def _inventory_line(ref, node: Node, tree: Tree, by_for, wrapping, target,
                    kind=None) -> str:
    a = node.attrs
    if kind is None:
        kind = node.tag
        if node.tag == "input":
            kind = f"input({a.get('type', 'text')})"
        elif node.tag == "tr":
            kind = "row"
        elif _is_draggable(node) and node.tag not in _INTERACTIVE_TAGS:
            kind = "draggable"
    parts = [f"{ref} {kind}"]
    name = (norm(_zone_name(node)) if kind == "dropzone"
            else norm(_accessible_name(node, tree, by_for, wrapping)))
    if node.tag == "tr":
        cells = [c.norm_text() for c in node.element_children() if c.tag in ("td", "th")]
        name = cells[0] if cells and cells[0] else name
    if name:
        parts[0] += f' "{_clip(name, NAME_CHARS)}"'
    for attr in ("name", "placeholder", "data-testid", "role"):
        if a.get(attr) and a[attr] != name:
            parts.append(f"{attr}={_clip(a[attr], 40)}")
    if node.tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "#")):
        parts.append(f"href={_clip(a['href'].split('?')[0], 60)}")
    if node.tag in ("input", "textarea") and a.get("type") not in ("checkbox", "radio",
                                                                   "submit", "button"):
        parts.append(f'value="{_clip(a.get("value", ""), 40)}"')
    if node.tag == "select":
        opts, sel_text = [], None
        for o in node.element_children():
            for opt in ([o] if o.tag == "option" else
                        [c for c in o.element_children() if c.tag == "option"]):
                text = opt.norm_text()
                val = opt.attrs.get("value")
                opts.append(f'"{_clip(text, 40)}"'
                            + (f" (value={val!r})" if val is not None and val != text
                               else ""))
                if "selected" in opt.attrs:
                    sel_text = text
        shown = opts[:12] + ([f"…+{len(opts) - 12} more"] if len(opts) > 12 else [])
        parts.append("options: " + ", ".join(shown))
        if sel_text is not None:
            parts.append(f'selected "{_clip(sel_text, 40)}"')
    # frames render as "hop >>> hop >>> selector" for the reader's benefit
    # only — the model answers with refs, and resolve_refs() substitutes the
    # structured target, so the arrow notation never needs parsing anywhere.
    sel = " >>> ".join(list(target.get("frames") or []) + [target["selector"]])
    if "text_exact" in target:
        sel += f' + text_exact "{target["text_exact"]}"'
    parts.append(f"sel: {sel}")
    # The page's own wiring, surfaced: an element with inline handlers reacts
    # during playback (the player dispatches real input/change events), so an
    # edit instruction about "the total updating" may already be implemented.
    events = sorted(k for k in a if _EVENT_ATTR.match(k))
    if events:
        parts.append("reactive: " + ",".join(events))
    if node.src:
        parts.append(f"src: {_clip(norm(_src_excerpt(node.src)), SRC_EXCERPT_CHARS)}")
    return "  " + " | ".join(parts)


def _frame_contexts(tree: Tree):
    """(document tree, frames-hop prefix) for this page: the page itself,
    then every captured iframe document reachable from it. Each hop is a
    verified-unique selector for the iframe element in ITS parent document —
    the exact descent bridge.js and match() perform. A frame that cannot be
    uniquely named (or only with a text anchor, which querySelector hops do
    not support) is skipped along with its content: better absent from the
    inventory than present with an unreplayable route."""
    out = [(tree, ())]
    i = 0
    while i < len(out):
        t, prefix = out[i]
        i += 1
        for node in t.by_tag.get("iframe", []):
            if node.frame_tree is None:
                continue
            hidden, cur = _is_hidden(node), node.parent
            while not hidden and cur is not None and cur.tag is not None:
                hidden = _is_hidden(cur)
                cur = cur.parent
            if hidden:
                continue
            try:
                hop = target_for(t, node)
            except DigestError:
                continue
            if "text_exact" in hop or "text" in hop:
                continue
            out.append((node.frame_tree, prefix + (hop["selector"],)))
    return out


def page_digest(rel: str, tree: Tree, aliases=(), ref_counter=None):
    """Render one page's digest block.

    Returns (text_block, ref_entries) where ref_entries is
    [{"ref", "page", "target", "tag", "name"}] — everything resolve_refs()
    needs to substitute a model-picked ref for its verified target. Captured
    iframe documents are inventoried too, their targets carrying the frames
    hop list (see _frame_contexts).
    """
    if ref_counter is None:
        ref_counter = itertools.count(1)
    title = ""
    for t in tree.by_tag.get("title", [])[:1]:
        title = t.norm_text()

    header = [f"=== PAGE {rel} ==="]
    if title:
        header.append(f"title: {title}")
    if aliases:
        header.append("same page as (interchangeable `page` values): "
                      + ", ".join(aliases))

    contexts = _frame_contexts(tree)
    entries, inv_lines, zone_lines, disp_lines = [], [], [], []
    outline = _outline(tree)
    for ftree, fprefix in contexts:
        by_for, wrapping = _label_index(ftree)
        if fprefix:
            outline += (f"\n  [content of frame "
                        f"{' >>> '.join(fprefix)}]\n" + _outline(ftree))
        picked = _interactive_nodes(ftree)
        for node in picked:
            try:
                target = target_for(ftree, node)
            except DigestError:
                continue                    # untargetable element: omit, never guess
            if fprefix:
                target["frames"] = list(fprefix)
            ref = f"e{next(ref_counter)}"
            inv_lines.append(_inventory_line(ref, node, ftree, by_for, wrapping, target))
            entries.append({"ref": ref, "page": rel, "target": target,
                            "tag": node.tag, "node": node,
                            "name": norm(_accessible_name(node, ftree, by_for, wrapping))})
        # Drop zones share the "e" prefix (they ARE step-addressable, as a
        # drag's drop_target) but carry a flag so resolve_refs can refuse one
        # used as the thing to click, type into or pick up.
        draggables = [n for n in picked if _is_draggable(n)]
        for node in _dropzone_nodes(ftree, draggables, {id(n) for n in picked}):
            try:
                target = target_for(ftree, node)
            except DigestError:
                continue
            if fprefix:
                target["frames"] = list(fprefix)
            ref = f"e{next(ref_counter)}"
            zone_lines.append(_inventory_line(ref, node, ftree, by_for, wrapping,
                                              target, kind="dropzone"))
            entries.append({"ref": ref, "page": rel, "target": target,
                            "tag": node.tag, "node": node, "dropzone": True,
                            "name": norm(_zone_name(node))})
        # Display elements ride the same ref counter under a "d" prefix so a
        # ref names exactly one element regardless of kind; resolve_refs
        # rejects a "d" ref used as a step target.
        for node in _display_nodes(ftree, {id(n) for n in picked}):
            ref = f"d{next(ref_counter)}"
            disp_lines.append(_display_line(ref, node, ftree, by_for, wrapping))
            entries.append({"ref": ref, "page": rel, "target": None,
                            "tag": node.tag, "node": node, "display": True,
                            "name": norm(_accessible_name(node, ftree, by_for, wrapping))})

    block = "\n".join(header) + "\n\nVISIBLE TEXT OUTLINE:\n" + outline \
        + "\n\nINTERACTIVE ELEMENTS (pick step targets by ref):\n" \
        + ("\n".join(inv_lines) if inv_lines else "  (none found)")
    if zone_lines:
        block += ("\n\nDROP ZONES (where a `draggable` element can be released — "
                  "valid ONLY as a drag step's drop_target, never as target):\n"
                  + "\n".join(zone_lines))
    if disp_lines:
        block += ("\n\nDISPLAY ELEMENTS (read-only: what the page shows — "
                  "anchors for capture_edits; steps can NEVER target these):\n"
                  + "\n".join(disp_lines))
    return block, entries
