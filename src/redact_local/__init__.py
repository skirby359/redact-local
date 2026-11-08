"""redact-local: de-identify matter documents on this machine before an AI reads them.

Nothing heavy is imported here. Modules that pull in Presidio, spaCy, PyMuPDF or
RapidOCR install the network guard first (see netguard.py).
"""

import functools
import subprocess
from pathlib import Path

__version__ = "0.2.0"


@functools.lru_cache(maxsize=1)
def build() -> str:
    """The version, plus the git commit when running from a checkout: "0.2.0 (b62ff95)".

    An editable install runs whatever is in the working tree, so the version number alone
    can't tell two runs apart. "+changes" means src/ or config/ differed from that commit.
    Any git problem (no git, not a checkout) just leaves the version on its own.
    """
    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".git").exists():
        return __version__
    kw = dict(capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], **kw)
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "src", "config"], **kw)
    except (OSError, subprocess.SubprocessError):
        return __version__
    commit = head.stdout.strip()
    if head.returncode or not commit:
        return __version__
    return f"{__version__} ({commit}{'+changes' if dirty.returncode == 0 and dirty.stdout.strip() else ''})"
