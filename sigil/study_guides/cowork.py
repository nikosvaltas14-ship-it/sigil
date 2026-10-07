"""Study guides made outside Sigil, in Claude Cowork projects.

Some guides are written in a Cowork project (a folder under `projects_root`,
e.g. "<project>/Οδηγοί μελέτης/") rather than by the nightly job. This
module finds those folders and turns their PDFs into the same record shape
runs.json uses, so the study map and the
weekly review show them next to the generated ones with no other changes.

Read-only by design: it lists and stats files, never opens, copies, moves or
writes one. A project is linked when it is named in `study_cowork_projects`
or, with `study_cowork_auto`, when it holds a study-guides folder
(`GUIDE_DIR_NAMES`), so a new uni project links itself once it has guides.
"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

KEY_PREFIX = "cowork:"
GUIDE_DIR_NAMES = ("οδηγοι μελετης", "οδηγοι", "study guides", "study guide", "guides")
DEFAULT_ROOT = ""                    # config projects_root; "" = none
MAX_PDFS_PER_PROJECT = 200          # a runaway folder must not stall a run


@dataclass(frozen=True)
class Project:
    key: str                # "cowork:<folder name>"
    name: str               # display name
    folder: Path
    guides: Path


def _fold(text: str) -> str:
    stripped = "".join(ch for ch in unicodedata.normalize("NFD", text)
                       if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().replace("_", " ").split())


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _root(cfg) -> Path | None:
    text = str(cfg.get("projects_root") or DEFAULT_ROOT).strip()
    if not text:
        return None          # Path("") would be the current directory
    root = Path(text).expanduser()
    return root if root.is_dir() else None


def _guides_dir(folder: Path, hint: str = "") -> Path | None:
    """The study-guides subfolder of a project, or None."""
    try:
        subs = [p for p in folder.iterdir() if p.is_dir()]
    except OSError:
        return None
    wanted = ([_fold(hint)] if hint else []) + list(GUIDE_DIR_NAMES)
    for name in wanted:
        for sub in subs:
            if _fold(sub.name) == name:
                return sub
    return None


def linked_projects(cfg) -> list[Project]:
    """Explicit `study_cowork_projects` entries, then auto-detected ones."""
    root = _root(cfg)
    if root is None:
        return []
    found: dict[str, Project] = {}
    for entry in cfg.get("study_cowork_projects") or []:
        if not isinstance(entry, dict) or not str(entry.get("folder") or "").strip():
            continue
        folder = root / str(entry["folder"]).strip()
        if not folder.is_dir() or not _inside(root, folder):
            continue
        guides = _guides_dir(folder, str(entry.get("guides") or ""))
        if guides is not None:
            name = str(entry.get("name") or "").strip() or folder.name
            found[folder.name.casefold()] = Project(KEY_PREFIX + folder.name, name,
                                                    folder, guides)
    if cfg.get("study_cowork_auto", True):
        ignored = {str(n).strip().casefold() for n in cfg.get("study_cowork_ignore") or []}
        try:
            folders = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            folders = []
        for folder in folders:
            low = folder.name.casefold()
            if low in found or low in ignored or folder.name.startswith("."):
                continue
            guides = _guides_dir(folder)
            if guides is not None:
                found[low] = Project(KEY_PREFIX + folder.name, folder.name.title(),
                                     folder, guides)
    return list(found.values())


def guide_pdfs(project: Project) -> list[Path]:
    """The PDFs directly inside the project's guides folder, by name."""
    try:
        pdfs = sorted(p for p in project.guides.glob("*.pdf") if p.is_file())
    except OSError:
        return []
    return pdfs[:MAX_PDFS_PER_PROJECT]


def _title(pdf: Path) -> str:
    return " ".join(pdf.stem.replace("_", " ").split()) or pdf.stem


def records(cfg, start: date | None = None, end: date | None = None) -> list[dict]:
    """runs.json-shaped records for every linked guide PDF dated start..end.

    Never raises: an unreadable project is skipped and logged.
    """
    rows: list[dict] = []
    try:
        projects = linked_projects(cfg)
    except Exception:  # noqa: BLE001 — a listing is never worth an exception
        log.exception("study cowork: could not list the linked projects")
        return rows
    for project in projects:
        for pdf in guide_pdfs(project):
            try:
                made = datetime.fromtimestamp(pdf.stat().st_mtime)
            except OSError:
                continue
            if (start and made.date() < start) or (end and made.date() > end):
                continue
            rows.append({"date": made.date().isoformat(),
                         "recorded_at": made.isoformat(timespec="seconds"),
                         "course_key": project.key, "course_label": project.name,
                         "status": "ok", "topic": _title(pdf), "pdf_path": str(pdf),
                         "origin": "cowork"})
    return sorted(rows, key=lambda r: (r["date"], r["topic"]))


def weekly_rows(cfg, monday: date, saturday: date) -> list[dict]:
    """The week's linked guides in the weekly review's guide-row shape."""
    return [{"date": r["date"], "course_key": r["course_key"], "course": r["course_label"],
             "course_gr": r["course_label"], "status": "ok", "topic": r["topic"],
             "topic_inferred": False, "reason": "made in a Claude Cowork project",
             "pdf_name": Path(r["pdf_path"]).name, "pdf_exists": True}
            for r in records(cfg, monday, saturday)]
