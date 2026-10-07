"""Courses already passed: off the active list for good.

Config key `study_guides_finished_courses` holds their keys. `load_settings`
drops them, so the guides, the timetable, exam refreshes and reminders stop
seeing them. Nothing on disk is deleted: guides already filed stay.
"""
from __future__ import annotations

CONFIG_KEY = "study_guides_finished_courses"


def finished_keys(cfg) -> frozenset[str]:
    raw = cfg.get(CONFIG_KEY) if hasattr(cfg, "get") else None
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(str(k).strip().lower() for k in raw if str(k).strip())
