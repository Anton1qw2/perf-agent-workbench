"""Editable literal keywords for routing chat messages to documentation."""

import re
from pathlib import Path

TRIGGER_PATH = Path(__file__).with_name("triggers.txt")


def matches_documentation_trigger(message: str, path: Path | None = None) -> bool:
    """Reload UTF-8 keywords each turn; a missing or unreadable file disables routing."""
    try:
        content = (path if path is not None else TRIGGER_PATH).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return False
    for line in content.splitlines():
        keyword = line.strip()
        if keyword and not keyword.startswith("#"):
            if re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", message, re.I):
                return True
    return False
