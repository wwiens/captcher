# Sharing a walkthrough

## Download it

On a walkthrough's card, choose **More options → Download…**, pick a brand
profile and a format:

| Format | Use it for |
|---|---|
| **Single HTML file** | Sending to someone, or attaching to a ticket or a wiki page. It opens in any browser. |
| **Folder (.zip)** | Hosting on your own website: an `index.html` with its assets beside it. |
| **SCORM 2004 / SCORM 1.2** | Uploading to a learning management system. Upload the .zip as it is; completion is reported to the LMS. |

Every download plays without a Captcher server and keeps working after you
delete the walkthrough, so this is the way to share with people who cannot reach
your Captcher. The folder and SCORM downloads are fully offline. A single HTML
file is too, except that a custom brand logo is loaded from its URL.

## Embed it in your site

**More options → Embed…** publishes a public copy of the walkthrough and gives
you a snippet that adds a Help button and modal to any page:

```html
<script src="http://127.0.0.1:8080/static/embed.js" defer
        data-sc-src="http://127.0.0.1:8080/embed/WALKTHROUGH-ID"
        data-sc-title="How to log a call"></script>
```

**An embed only works for people who can reach your Captcher server.** On a
server that listens only on your own computer (the default), that is you. To
embed for other people, either:

- run Captcher where they can reach it, behind HTTPS and with an access token
  (see "Sharing on a network" in the README), or
- download the **Folder (.zip)**, put it on your own site, and point
  `data-sc-src` at its `index.html`. `embed.js` works with any URL an iframe can
  load. Host `embed.js` itself beside it (it is `server/captcher/static/embed.js`
  in the repository).

Turning embedding off for a walkthrough makes its `/embed/` URL answer 404
again.

### Snippet options

| Attribute | Meaning |
|---|---|
| `data-sc-src` | Required. The walkthrough's `/embed/` URL, or any hosted export. |
| `data-sc-title` | Accessible label and tooltip. Default "Help". |
| `data-sc-button` | `none` hides the floating button; use your own trigger. |
| `data-sc-position` | `bottom-right` (default) or `bottom-left`. |
| `data-sc-color` | The button's fill colour. |
| `data-sc-width`, `data-sc-height` | A fixed modal size in pixels. |
| `data-sc-max-width` | The widest the modal may grow. |
| `data-sc-backdrop` | `0` to skip dimming the page behind the modal. |

Any element with a `data-captcher-help` attribute opens the modal when
clicked, and `window.CaptcherHelp.open()`, `.close()` and `.toggle()` do the
same from script.

## Brand it

**Brand profile** sets the colours, fonts, logo and name an exported or
embedded walkthrough shows. Choose the profile in the Download or Embed dialog.
Logos are referenced by URL; the folder and SCORM downloads copy them in.
