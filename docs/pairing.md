# Extension pairing

How the capture extension gets a server address and a credential without anybody
copying a token.

The flow is one button in the extension's side panel. A token still exists — it
is still what `/api/upload` authenticates with — but it is minted, transported
and stored without ever being displayed, and there is no field to fill in
anywhere.

The panel is the only place any of this happens. It is the only surface that can
see which tab is open, navigate it, and inject into it, so it is the only one
that can turn "connect this browser" into a single press regardless of where the
user starts. `/account` reports and revokes; it does not connect.

## Why not `externally_connectable`

The textbook Chrome answer is `externally_connectable`, which lets a listed web
origin call `chrome.runtime.sendMessage(extensionId, …)` directly. It does not
fit here:

- **It is manifest-only.** Unlike content scripts, there is no
  `registerExternallyConnectable`. The server address is user-configured, so
  there is no origin to write into the manifest at build time — the same problem
  `announce.js` already solves by being registered at runtime. A wildcard is not
  an escape: `externally_connectable.matches` rejects patterns without at least
  a second-level domain, so `<all_urls>` and `*://*/*` are both refused.
- **It needs a stable extension ID.** The recorder is now distributed through
  the Chrome Web Store, which provides one; the earlier unpacked-ZIP
  distribution did not.

With the store listing, the hosted case (a fixed domain) could add
`externally_connectable` as a shortcut. Self-hosted deployments would still
need what is specified here, so this is the general mechanism either way.

## The shape

**The extension always initiates.** That is the most important thing here, and
everything else follows from it.

The obvious design is a Connect button on `/account`. It cannot be the primary
path: `announce.js` is registered only for the origin already saved in
`chrome.storage`, so on a clean install the extension has nothing running on the
app page — and a clean install is precisely when pairing is needed. A page-side
button would work only for people who are already connected. Two paths where one
silently cannot work when it matters is worse than one path.

So: the side panel asks, the page answers.

```
  side panel                    Captcher tab (session cookie)
  ──────────                    ───────────────────────────────────
  [Connect this browser]
      │ GET /api/extension/ping   is a Captcher server here?
      │ executeScript(connect.js) activeTab, on the user's gesture
      │                    ─────► connect.js
      │                              │ postMessage
      │                              │  {source:"captcher-extension",
      │                              │   type:"captcher:pair-request"}
      │                              ▼
      │                           pair.js (loaded by the toolbar,
      │                              │     so ANY app page answers)
      │                              │ POST /api/extension/pair
      │                              │   same-origin, cookie-authed
      │                              │ ← { code, expires_in }
      │                              │ postMessage
      │                              │  {source:"captcher-app",
      │                              │   type:"captcher:pair-offer", code}
      │                              ▼
      │                           connect.js
      │                              │ chrome.runtime.sendMessage
      │                              ▼
      │                           background.js
      │                              │ POST <origin>/api/extension/claim
      │                              │ ← { token, email, device_id }
      │                              │ chrome.storage.local.set(
      │                              │   { server: { url, token } })
      │ ◄───────────────────────────  pair-status { ok, email }
      ▼
  recording surface
```

The privileged call is made by the page, same-origin, with the cookie the
browser already sends. The extension only ever calls a *public* endpoint with a
short-lived code. Nothing needs CORS, which matters because the server has none
(no `do_OPTIONS`, no `Access-Control-*`) — extension-to-server `POST` works only
because MV3 extension pages are exempt for hosts in `host_permissions`, exactly
as `/api/upload` already relies on.

The server URL is not transported at all. It is `sender.origin`, which Chrome
sets and a page cannot spoof. There is no field for a user to get wrong.

### Why `pair.js` is on the toolbar

Every app page carries the toolbar, so every app page can answer a
pair request. Putting the listener on `/account` only would mean the panel had
to send people to a particular page before it could do anything, which is the
kind of instruction this design exists to delete.

### The panel decides everything else

The connect surface is a readout of one step at a time and no fields, with a
button only when there is something for a person to press. What it says follows
`connectState`, recomputed whenever the active tab changes or navigates:

| State | Meaning | Shown |
|---|---|---|
| `checking` | the active tab has not been identified yet | *Checking this tab…* |
| `open` | the active tab is not a Captcher server | "Open your Captcher app in this tab", and an **Open Captcher** button |
| `working` | navigating or claiming | *Connecting…* |
| `ready` | the tab *is* Captcher, but the pairing failed | the error, and **Try again** |
| `done` | connected | **Connected** as *who* to *where*; the surface moves to recording after a short hold |

**A Captcher tab pairs the moment the panel recognises it**, once per page
load, with nothing pressed. Only a tab on the configured server address (see
`extension/server-address.js`; set in the panel's settings, default
`http://127.0.0.1:8080`) is ever asked — the panel does not probe the other sites
somebody browses. The "once" is the panel's `autoConnected` memory (`tabId url`):
without it a tab that refused would be re-asked on every switch back to it, six
seconds of *Connecting…* ending in the same error. Reloading that tab, pressing **Try again**, or
reopening Connection settings on purpose tries again.

**Open Captcher** navigates the active tab in place — the panel is docked
to this window, and a new tab would put the thing being set up where the panel is
not looking — waits for the load, and the arrival pairs like any other. It navigates to
the configured server address. A
self-hosted address is not discoverable any other way, which is also why the
note always names the address before you press it.

Origins are pinged through a cache. The active tab changes constantly while
somebody works, and re-probing a known origin on each of those would be noise on
someone else's server. The entry is dropped when that tab finishes loading
again, since a reload may have changed what the page can answer.

## Wire contract

Five names, all load-bearing across the two repos, all failing by silence:

| Name | Direction | Payload |
|---|---|---|
| `captcher:pair-request` | extension → page | — ("mint me a code") |
| `captcher:pair-offer` | page → extension | `{ code }` or `{ error }` |
| `source: "captcher-extension"` | on every request | the discriminator the page checks |
| `source: "captcher-app"` | on every offer | the discriminator `connect.js` checks |
| `{"captcher": true}` | `/api/extension/ping` body | how the panel recognises a server |

They travel over `window.postMessage` and are therefore visible to the page —
which is why the payload is a one-time code and never a token.

**The offer carries `error` when there is no code**, and that is not a detail: a
page that cannot mint a code (a server still waiting for its access token at
`/unlock`) and a page with no listener at all are indistinguishable from the
extension's side, and only one of them has a fix the user can act on. So the page
passes the server's own message back rather than going quiet and leaving the panel to report a timeout.

Both ends validate:

- **The page** accepts only `event.source === window`, `event.origin ===
  location.origin`, and `source: "captcher-extension"`. Without the first
  two, an iframe — an ad, an embedded widget, an embedded walkthrough — could ask
  the page to mint a pairing code and then read the answer off the bus.
- **`connect.js`** accepts only `event.source === window` and
  `source: "captcher-app"`, and reads nothing but `code` and `error`.
- Both post to `location.origin`, never `"*"`. The payload is a credential.

`announce.js` takes no part in any of this and must stay that way — it is a
one-way presence flag, and its own two names
(`data-captcherCapture`, `captcher:capture-present`) are a separate
contract with `templates/_extension-panel.html`.

## Endpoints

### `POST /api/extension/pair` — session only

Mints a one-time pairing code for the server's owner. **Requires a browser
session, not a bearer token**, for the same reason `/api/token` does: a leaked
capture token must not be able to mint credentials for itself.

```json
→ (no body)
← 201 { "code": "cap_pair_…", "expires_in": 120 }
```

The code is 32 bytes of `secrets.token_urlsafe`, held in memory only
(`_PAIRING_CODES` in `auth.py`), never written to a table. A restart between
offer and claim invalidates it, which costs one re-click and means a crash cannot
leave a redeemable credential on disk.

### `POST /api/extension/claim` — public

Redeems a code for a durable per-device token. Public because the extension has
no session; the code *is* the authentication, and it is single-use and
short-lived.

```json
→ { "code": "cap_pair_…", "label": "Chrome on macOS" }
← 201 { "ok": true, "token": "cap_…", "email": "…", "device_id": "…" }
← 400 { "error": "That pairing code has expired or was already used. …" }
```

`email` stays the wire key, and it is read off the member row that redeemed
rather than being what the code resolved to — the panel shows "connected as
<address>".

The 400's second sentence currently reads *"Click Connect again on your Account
page."*, which names a control that deliberately does not exist. Fix the string,
not the design.

The code is consumed under `_PAIRING_LOCK` **before** the token is minted, so two
simultaneous claims cannot both succeed, and a failed mint still spends the code
— the safe direction, since a failed pair is one more click and a replayable code
is a credential. `label` is truncated to 64 characters and rendered escaped on
`/account`; it is attacker-controlled in the sense that whoever holds a valid
code chose it, which is the same person about to own the device row.

Failures are deliberately uniform — expired, already-claimed and never-existed
all return the same message. Distinguishing them tells a code-guesser which half
of the space to search, and no legitimate user behaves differently in the three
cases.

### `GET /api/extension/ping` — public

```json
← 200 { "captcher": true, "pairing": 1 }
```

How the panel knows whether the active tab is the configured Captcher server —
which drives the whole connect surface, so this is called on tab changes, not
just on a button press. It is only ever asked of the configured address. It returns no member data and is safe to call unauthenticated: it
says only "a Captcher server answers here", which the extension is about to
find out anyway by posting to it.

`pairing` is the protocol version (`server.PAIRING_API_VERSION`), advertised so a
panel newer or older than its server can say so rather than failing shapeless.
Bump it only for a change the extension has to notice.

### `DELETE /api/extension/devices/<device_id>` — session only

Revokes one device token. Filtered on the member as well as the device id, so
knowing another member's device id is not enough to revoke it. `/account` lists
devices and each row has a Revoke button.

## Per-device tokens

A member holds a **list** of tokens, one row in `capture_tokens` per paired
browser. It was once one token per member, and minting replaced it: pairing a
second browser silently revoked the first, with no error anywhere — the older
extension just started getting 401s, whose message sent its owner to regenerate,
breaking the other browser in turn. With a one-click pairing flow that is no
longer an edge case, because pairing is now cheap enough to do casually.

```
capture_tokens
  id            random, public — what Revoke names
  member_id     uuid, cascades from members
  token_hash    sha256 of the plaintext; the token itself is never stored
  label         chosen by whoever redeemed the code, capped at 64 chars
  created_at
  last_used_at
```

- **The digest is a unique indexed column.** `find_member_by_token` is one probe
  where the previous file-backed mirror forced a scan of every account's every
  device. There is no constant-time comparison and none is needed: the lookup key
  is a SHA-256 of 32 random bytes, so there is no low-entropy secret for a timing
  signal to leak. A plain digest is also enough — a high-entropy random token
  does not need a slow KDF.
- **A revoked or deleted account's token stops working on its next use**, because
  `find_member_by_token` re-checks `has_beta_access`.
- **`last_used_at` is a diagnostic, not an audit record** ("which of these three
  is my laptop?"). Uploads are the hot path, so it is written at most once an
  hour per token, rate-limited in memory rather than by reading the stored
  timestamp first — which would double the queries on the upload path to maintain
  a diagnostic.
- **The `cap_` token prefix is the discriminator** `find_member_by_token` rejects
  non-tokens on. It is deliberately not brand-derived: every issued token carries
  it, so changing it 401s every paired extension at once.

### CLI

```bash
captcher token [LABEL]       # add a device, labelled CLI unless named
captcher devices             # list device rows
captcher revoke <id>         # drop one
```

`token` *adds* a device rather than replacing one, so existing browsers keep
working.

## Failure modes, and what the user sees

Every one of these ends somewhere the user can act, because the panel is the only
surface involved and it can always say what it is waiting for.

| What happens | Where it is caught | What is shown |
|---|---|---|
| Active tab is not Captcher | `/api/extension/ping` fails | "Open your Captcher app in this tab", and a button naming the address |
| Server not running | `ping` fails after navigating | "…did not load. Is the server running?" |
| Server needs its access token (bound beyond loopback) and this browser has not entered it | `/api/extension/pair` → 401, carried back in the offer's `error` | The server's own words, pointing at `/unlock` |
| Page cannot be injected (`chrome://`, Web Store, PDF) | `executeScript` throws | "Could not reach this page (…)." |
| Code expired (slow claim, server restart) | `/api/extension/claim` → 400 | "That pairing code has expired or was already used." |
| Claim succeeds, storage write fails | `background.js` catch | Reported as a failure; nothing stored, code already burned, **Try again** fixes it |
| Claim reports success but storage is empty | `finishConnect` re-reads storage | Reported as a failure rather than moving to a surface where nothing works |
| Two windows connect at once | Two codes, two claims, two device rows | Both work; `/account` shows two devices, each revocable |

**There is no manual fallback, and that is deliberate.** A URL field and a token
field beside a Connect button is three ways to do one thing, two of them worse,
and their presence is what made the old surface feel like configuration. `POST
/api/token` still exists for scripts and `captcher token` still mints one,
so a genuinely stuck deployment is not locked out — it just is not offered a
second path in the UI.

## The names both ends have to agree on

Five, and every one of them fails by silence. A renamed message is not heard, a
renamed marker is not read, a renamed probe key is not recognised — and nothing
on either side raises, so the symptom is always the same: pairing appears to do
nothing, or the install prompt never goes away.

| Name | Where it is spoken |
|---|---|
| `captcher:pair-request` | extension → page |
| `captcher:pair-offer` | page → extension |
| `source: "captcher-extension"` / `"captcher-app"` | on both messages |
| `data-captcher-capture`, `captcher:capture-present` | `announce.js` → `_extension-panel.html` |
| `/api/extension/ping` → `{"captcher": true}` | server → `sidepanel/panel.js` |

They were renamed in separate passes once, which is exactly how the two halves
came to disagree. `tests/test_pairing.py`, `test_pairing_handshake.py` and
`test_connect_surface.py` compare the server and extension folders rather than pinning
either side's literal, so the half that falls behind is the half that fails.

Change both ends in one commit. Renaming one end is invisible: the request is
never heard, or the answer never recognised, and neither side raises anything.

## Tests

Three, because the three things that can break here break in different ways:

- **`test_pairing.py`** — the Python half (codes, device tokens, who may call
  what) plus a contract check that reads the extension's files and compares
  message names.
- **`test_pairing_handshake.py`** — the real JS files run under node against a
  fake `postMessage` bus. The names can match while the conversation still does
  not happen, and that failure is silent.
- **`test_connect_surface.py`** — `panel.js` loaded whole into a fake DOM. Pins
  the state machine: what the readout says, when it navigates, that a Captcher
  tab pairs once and only once, and that a failure leaves a usable
  surface behind.

All three **skip when the extension repo is not checked out beside this one**, so
a green run proves nothing on its own. Check that they ran.
