# Recording a walkthrough

## Before you record

- **Use sample data.** The recorder saves the pages you visit as you see
  them. Redaction (below) catches passwords and card fields, not ordinary text,
  so record in a demo account or a test environment.
- **Pick one short task.** "Log a call on a deal" makes a better walkthrough
  than "everything about the CRM". A recording can have up to 40 steps and 25
  different pages; record a longer process in parts.
- **Start where a learner starts.** The first captured screen is the first
  thing a learner sees.

## Recording

1. Open the application you want to teach in Chrome, and open the
   **Captcher Recorder** side panel from the toolbar.
2. Give the walkthrough a title and press **Start recording**.
3. Do the task at a normal pace. Every click, choice and typed value is
   recorded, and each new page is captured as you reach it. The page freezes
   briefly while it is captured; wait for it.
4. Press **Finish & submit**. Keep the panel open until the upload finishes:
   closing it loses the recording.

The recording appears on Captcher's **Walkthroughs** page as a draft in the
authoring queue. **Author with AI** turns it into a walkthrough; you can edit
every step afterwards, by hand or by asking the AI assist.

## What is captured

Each page is saved as a single HTML file with its styles, images and fonts,
plus a screenshot, and the actions you took on it. The recorder captures only
the tab you started in and tabs opened from it, and only while recording.

**Redaction.** Password fields, one-time-code and card fields, fields named like
passwords, PINs or card codes, and hidden inputs are blanked before a page is
captured, and the screenshot is taken after that. Content inside frames from
other sites cannot be blanked. Redaction does not find secrets in ordinary
text, URLs or images, which is why sample data matters. The recorder's
**Privacy and capture limits** page has the full detail.

**Where it goes.** Only to the Captcher server set in the recorder's settings
(by default the one on your own computer). The extension has no analytics and
contacts nothing else.

## When a page will not capture

The recorder stops rather than capture something it could not check: a page
still loading, an embedded frame it cannot reach, or a field it could not
verify was blanked. The panel says which. Wait for the page to settle and try
the step again, or start that part of the task in a new recording.
