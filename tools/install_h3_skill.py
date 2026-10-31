"""Install MiniMax's official h3-prompt-writing skill into .claude/skills/ at a pinned commit.

The upstream repo has no license file, so the skill is fetched rather than vendored.
Bump COMMIT deliberately after reviewing upstream changes.
Run: python tools/install_h3_skill.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO = "MiniMax-AI/MiniMax-H3"
COMMIT = "a107547fa669c509b8e6363fe18378d46ab3066c"
FILES = [
    "SKILL.md",
    "references/base-en.txt",
    "references/ref-en.txt",
    "agents/openai.yaml",
]
DEST = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "h3-prompt-writing"


def main() -> int:
    for rel in FILES:
        url = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/skills/h3-prompt-writing/{rel}"
        dest = DEST / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as r:
            dest.write_bytes(r.read())
        print(f"{dest.relative_to(DEST.parents[2])}  <- {url}")
    (DEST / "SOURCE.txt").write_text(f"https://github.com/{REPO}/tree/{COMMIT}/skills/h3-prompt-writing\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
