"""Install the redact-local skill for Claude Code.

Run it with the Python that has redact-local installed (your venv's python):

    <venv python> scripts/install_skill.py            # link (junction on Windows, symlink elsewhere)
    <venv python> scripts/install_skill.py --copy     # copy instead of linking

It writes skill/SKILL.md from skill/SKILL.template.md, filling in that Python's path and
the config folder, then makes ~/.claude/skills/redact-local point at the skill folder.
Rerun it after editing the template or moving the venv.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skill"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--copy", action="store_true", help="copy the skill instead of linking it")
    ap.add_argument("--skills-dir", type=Path, default=Path.home() / ".claude" / "skills")
    args = ap.parse_args()

    try:
        from redact_local.config import config_dir
    except ImportError:
        print("Run this with the Python where redact-local is installed (pip install -e .).", file=sys.stderr)
        return 1

    template = (SKILL_DIR / "SKILL.template.md").read_text(encoding="utf-8")
    text = template.replace("{{PYTHON}}", sys.executable).replace("{{CONFIG_DIR}}", str(config_dir()))
    (SKILL_DIR / "SKILL.md").write_text(text, encoding="utf-8")
    print(f"Wrote {SKILL_DIR / 'SKILL.md'}")

    target = args.skills_dir / "redact-local"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.resolve() == SKILL_DIR.resolve():
            print(f"{target} already points at {SKILL_DIR}")
            return 0
        if args.copy and target.is_dir():
            shutil.copy2(SKILL_DIR / "SKILL.md", target / "SKILL.md")
            print(f"Updated the copy in {target}")
            return 0
        print(f"{target} already exists and points elsewhere. Remove it first.", file=sys.stderr)
        return 1

    if args.copy:
        target.mkdir()
        shutil.copy2(SKILL_DIR / "SKILL.md", target / "SKILL.md")
    elif os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(SKILL_DIR)], check=True, capture_output=True)
    else:
        target.symlink_to(SKILL_DIR, target_is_directory=True)
    print(f"Installed the skill at {target}. Start a new Claude Code session to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
