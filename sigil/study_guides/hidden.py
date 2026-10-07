"""Courses no longer being taken, dropped from every study source.

Config key `study_mode_hidden_courses` lists them. A name matches as a
case- and accent-insensitive substring of a course's name, so "Φυσική" also
catches "001 Φυσική - 2025/2". Pure: no I/O, so every reader can share it.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable

CONFIG_KEY = "study_mode_hidden_courses"


def fold(text: str) -> str:
    """Lower-case, accents removed: "ΦΥΣΙΚΗ" == "Φυσική"."""
    plain = "".join(c for c in unicodedata.normalize("NFD", str(text))
                    if not unicodedata.combining(c))
    return plain.casefold()


def needles(cfg) -> tuple[str, ...]:
    """The hidden-course names from `cfg`, cleaned, casefolded, blanks dropped."""
    raw = cfg.get(CONFIG_KEY) if hasattr(cfg, "get") else None
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(fold(s.strip()) for s in raw if isinstance(s, str) and s.strip())


def is_hidden(hidden: Iterable[str], *course_names: str | None) -> bool:
    """True if any of `course_names` contains any `hidden` needle (already folded)."""
    folded = [fold(n) for n in course_names if n]
    return any(h in name for h in hidden for name in folded)
