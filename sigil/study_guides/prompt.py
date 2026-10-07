"""The words the study-guide pipeline sends to Claude, assembled from prompts/.

Design rules:
* **The prompt texts live in `prompts/*.txt`, not in code.** The guide prompts
  (system, add-ons, user template) are the spec's section 7 copied verbatim, so
  a wording change is an edit to a text file that anyone can diff against the
  spec. This module only picks which files apply and fills in the blanks.
* **Templates use `str.format` named fields.** Only the template is parsed for
  fields; the values dropped into it (lecture text full of LaTeX braces) are
  never re-parsed. `verify.txt` and `repair.txt` contain literal LaTeX braces,
  so they are instructions that get the document *appended*, never formatted.
* **Nothing here talks to the network or the CLI.** Building a prompt must be
  cheap and deterministic so `run --dry-run` can print exactly what would be
  sent.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from .models import LAB, LECTURE, TUTORIAL, Course, Material

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).with_name("prompts")

# Add-ons a course may list in `addons`, each mapping to prompts/addon_<name>.txt.
KNOWN_ADDONS = ("circuits", "code", "math")
# The exam-format value that switches on the MCQ add-on automatically.
MCQ_EXAM_FORMAT = "mcq"
# What `exam_format` holds before anyone knows how the course is examined.
UNKNOWN_EXAM_FORMAT = "unknown"

# The user template's "Τύπος σημερινής ώρας" line, per session type.
_SESSION_LABELS = {
    LECTURE: "lecture (Θ)",
    TUTORIAL: "tutorial (Α)",
    LAB: "lab",
}
# The user template's "Προέλευση θέματος" line (spec 7.3 wording).
_ORIGIN_SCHEDULE = "from_schedule"
_ORIGIN_INFERRED = "inferred"

# Stand-ins for empty blocks, in the language of the template around them.
_NO_PREVIOUS = "καμία (πρώτη διάλεξη ή άγνωστες)"
_NO_EXAM_FORMAT = "άγνωστη"
_NO_PAST_EXAMS = "κανένα"
_NO_MATERIAL = "(δεν βρέθηκε υλικό στο e-learning για το σημερινό θέμα)"
_ATTACHMENTS_LINE = "Συνημμένα αρχεία (διάβασέ τα με το Read tool): {names}"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """The text of prompts/<name>.txt. Raises FileNotFoundError if missing —
    a missing prompt is an install fault, not something to paper over."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def build_system(course: Course) -> str:
    """System prompt for one course: section 7.1 plus the course's add-ons,
    plus the MCQ add-on when the course is known to be examined by MCQ."""
    parts = [load_prompt("system").rstrip()]
    for addon in course.addons:
        if addon not in KNOWN_ADDONS:
            log.warning("study guides: course %s lists unknown add-on %r — ignored",
                        course.key, addon)
            continue
        parts.append(load_prompt(f"addon_{addon}").rstrip())
    if (course.exam_format or "").strip().lower() == MCQ_EXAM_FORMAT:
        parts.append(load_prompt("addon_mcq").rstrip())
    return "\n\n".join(parts) + "\n"


def build_user(course: Course, day_str: str, topic: str, topic_inferred: bool,
               session_type: str, previous_topics: list[str],
               material: Material) -> str:
    """The user message (section 7.3) for one course-day.

    Scanned PDFs in `material.attachments` are named at the top of the
    <material> block; the caller must stage those files in the CLI's working
    directory (generate.generate_guide's `attachments_dir`) so Read finds them.
    """
    return load_prompt("user_template").format(
        course_name=course.name_gr,
        date=day_str,
        scheduled_topic=topic,
        topic_origin=_ORIGIN_INFERRED if topic_inferred else _ORIGIN_SCHEDULE,
        session_type=_SESSION_LABELS.get(session_type, session_type or "lecture (Θ)"),
        previous_topics=_previous_block(previous_topics),
        exam_format=_exam_format_text(course.exam_format),
        material=_material_block(material),
        past_exams=(material.past_exams or "").strip() or _NO_PAST_EXAMS,
    )


def _previous_block(previous_topics: list[str]) -> str:
    topics = [t.strip() for t in previous_topics if t and t.strip()]
    return "\n".join(f"- {t}" for t in topics) if topics else _NO_PREVIOUS


def _exam_format_text(exam_format: str) -> str:
    value = (exam_format or "").strip()
    return _NO_EXAM_FORMAT if not value or value.lower() == UNKNOWN_EXAM_FORMAT else value


def _material_block(material: Material) -> str:
    body = (material.text or "").strip()
    if not material.attachments:
        return body or _NO_MATERIAL
    names = ", ".join(Path(p).name for p in material.attachments)
    line = _ATTACHMENTS_LINE.format(names=names)
    # Scanned PDFs alone are real material, so no "nothing found" note then.
    return f"{line}\n\n{body}" if body else line
