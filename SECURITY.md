# Security

## Reporting a vulnerability

Please report security problems privately, through
[GitHub's private vulnerability reporting](https://github.com/wwiens/captcher/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and what an
attacker could do with it.

## What Captcher is built to protect

Captcher is a single-user tool that runs on your own computer. It has no
accounts: whoever can reach the server owns everything in it. So by default it:

- listens only on `127.0.0.1`, and refuses to start on any other address unless
  `CAPTCHER_ACCESS_TOKEN` is set (each browser then enters the token once);
- refuses requests whose `Host` header is not its own (DNS rebinding) and
  cross-site POST, PUT and DELETE requests (CSRF);
- accepts uploads only with a per-browser token, stored as a SHA-256 digest and
  revocable from the Settings page;
- pairs the recorder with exactly one configured server, over https unless that
  server is on the same computer.

Bugs in any of those are vulnerabilities. So is anything that lets a web page
reach the recorder's token, or makes the recorder send a capture anywhere but
its configured server.

## Known limitation

**Captured pages are served from Captcher's own origin.** The recorder removes
page scripts when it captures, and playback blocks network access from captured
pages, but the player does not yet isolate them in a separate origin or a
sandboxed frame. Record only applications you trust. Isolating captured pages
properly is planned; see the note at the `/capture/` route in
`server/captcher/server.py`.

Redaction in the recorder blanks password, one-time-code and card fields before
a page is captured, but cannot recognise every secret in ordinary text. Record
with sample data; see [docs/recording.md](docs/recording.md).
