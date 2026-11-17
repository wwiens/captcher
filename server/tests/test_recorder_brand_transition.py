"""The app's recorder instructions must name the recorder's real labels.

Until recorder 0.3.0 the extension was still called "Shadow Capture Recorder",
and these pages carried line-scoped transitional copy saying so. 0.3.0 renamed
it "Captcher Recorder" (Brand Guide v1, 2026-09-25), so the instructions now
use the new labels and the transitional notice is gone. The second test reads
the recorder source, so the two cannot drift apart again.
"""
import json
import os
from pathlib import Path
import re

import pytest

from captcher import paths

ROOT = Path(__file__).resolve().parents[1] / "captcher"

RECORDER_NAME = "Captcher Recorder"
OPEN_ACTION = "Open Captcher"

INSTRUCTION_PAGES = ("extension.html", "account.html", "getting-started.html", "_extension-panel.html")


@pytest.mark.parametrize("name", INSTRUCTION_PAGES)
def test_instructions_name_the_captcher_recorder(name):
    text = (ROOT / "templates" / name).read_text(encoding="utf-8")
    assert "Shadow Capture" not in text, name
    assert "the unchanged extension" not in text, name
    assert f"<strong>{RECORDER_NAME}</strong>" in text or name == "_extension-panel.html", name
    for nonexistent_label in ("captcher side panel", "captcher icon"):
        assert nonexistent_label not in text, (name, nonexistent_label)
    if "<title>" in text:
        assert re.search(r"<title>[^<]* — Captcher</title>", text)


def test_instruction_labels_match_recorder_source():
    extension = paths.extension_checkout(ROOT, os.environ.get("CAPTCHER_EXTENSION_DIR", ""))
    if not (extension / "manifest.json").is_file():
        message = f"Recorder source missing: {extension}"
        if os.environ.get("CAPTCHER_REQUIRE_EXTENSION", "").strip() not in ("", "0", "false"):
            pytest.fail(message)
        pytest.skip(message)
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == RECORDER_NAME
    panel = (extension / "sidepanel/panel.html").read_text(encoding="utf-8")
    script = (extension / "sidepanel/panel.js").read_text(encoding="utf-8")
    assert re.search(rf'<button\b[^>]*id="connect-action"[^>]*>{OPEN_ACTION}</button>', panel)
    assert f'action = "{OPEN_ACTION}";' in script
    instructions = (ROOT / "templates/extension.html").read_text(encoding="utf-8")
    assert f'<strong>{manifest["name"]}</strong>' in instructions
    assert f"<strong>{OPEN_ACTION}</strong>" in instructions
