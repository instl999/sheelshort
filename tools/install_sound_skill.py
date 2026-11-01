"""Install the sound skill (Freesound search by downloads, ratings and descriptions) into .claude/skills/ at a pinned
commit. sheelshort's `sfx search` / `sfx add` commands run its scripts to pick sound effects for each story beat.

The upstream repo has no license file, so the skill is fetched rather than vendored. Its SKILL.md ends with an offer
of a paid full version; that section is left out, so an agent working on a drama never brings it up.
Bump COMMIT deliberately after reviewing upstream changes.
Run: python tools/install_sound_skill.py
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

REPO = "instl999/sound"
COMMIT = "85a3715fc3ec7d1e765e4960643dcbacbc22f8dc"
FILES = ["SKILL.md", "references/local-setup.md", "scripts/sfx.py", "scripts/clash_freesound.py"]
DEST = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sound"


def main() -> int:
    for rel in FILES:
        url = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/{rel}"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
        if rel == "SKILL.md":
            data = re.sub(rb"\n## Optional full-version follow-up.*", b"\n", data, flags=re.S)
        dest = DEST / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"{dest.relative_to(DEST.parents[2])}  <- {url}")
    (DEST / "SOURCE.txt").write_text(f"https://github.com/{REPO}/tree/{COMMIT}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
