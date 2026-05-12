from __future__ import annotations

import re


_TERM = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "or",
    "the",
    "to",
    "use",
    "when",
    "with",
    "baseline",
    "document",
    "documentation",
    "file",
    "files",
    "increase",
    "increases",
    "unrelated",
    "request",
    "task",
}


def terms(text: str) -> set[str]:
    return {
        term
        for match in _TERM.finditer(text)
        if (term := match.group(0).lower().strip()) and term not in _STOPWORDS
    }


def matched_terms(left: str, right: str) -> list[str]:
    return sorted(terms(left).intersection(terms(right)))
