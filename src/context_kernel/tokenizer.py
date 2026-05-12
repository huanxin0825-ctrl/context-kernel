from __future__ import annotations

import json
import re
from typing import Any


_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(value: Any) -> int:
    """Small deterministic token estimate for budgeting and comparisons."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)

    ascii_words = _ASCII_WORD.findall(value)
    cjk_chars = _CJK_CHAR.findall(value)
    punctuation = re.findall(r"[^\sA-Za-z0-9_\u4e00-\u9fff]", value)

    return max(1, len(ascii_words) + len(cjk_chars) + max(1, len(punctuation) // 4))

