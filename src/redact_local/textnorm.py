"""Text normalization shared by extraction, names.txt and the keep lists.

Applied to document text before detection and to list entries when they're
read, so the two sides compare in the same form.
"""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"'})
_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])-[ \t]*\n[ \t]*(?=[^\W\d_])")


def normalize_entry(text: str) -> str:
    """For a list entry: NFKC, no zero-width characters, straight quotes."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).translate(_QUOTES)


def normalize_text(text: str) -> str:
    """Fold the variants that let a name slip past matching.

    NFKC (ligatures, fullwidth letters), soft hyphens and zero-width characters
    removed, curly apostrophes and quotes made straight, line endings made \\n,
    and words hyphenated across a line break rejoined ("Faker-\\nton").
    """
    text = normalize_entry(text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _HYPHEN_BREAK.sub("", text)
