"""Pure text helpers for tests & finals: keyword matching and date extraction.

No IO, no Moodle, no model: `exams.py` and `exam_sources.py` feed these the
names, topics and announcement bodies they fetch, and the tests call them
directly.

Matching rules (kept simple on purpose, see `is_exam_text`):

* Text is normalised first: lower-cased, accents stripped, final sigma folded
  to sigma. So "Πρόοδος", "ΠΡΟΟΔΟΣ" and "προοδος" all read the same.
* A keyword anywhere makes the text exam-related: πρόοδος/προόδου,
  διαγώνισμα, τεστ, test, quiz/κουίζ, εξέταση/εξετάσεις, ενδιάμεση, midterm,
  exam. There are no exclusions: "εξέταση εργαστηρίου" (a lab exam) is still a
  test worth a reminder, and a false "possible test" costs one Telegram line.
* `is_explicit` is the stricter set (πρόοδος, διαγώνισμα, midterm, ενδιάμεση
  εξέταση) that lets a course-schedule entry count as a confirmed test rather
  than a "possible test".
* `exam_kind` says "final" only for τελική/final/εξεταστική wording; anything
  else is a "test".
* `is_quiz_only`: the only keyword is quiz/κουίζ. Moodle quizzes are already
  timeline deadlines (kind "quiz", `assignments.py`), so the schedule and
  announcement sources skip such text instead of reminding the quiz twice. A
  paper quiz announced only by a news post is the accepted loss.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

# ---- normalisation ------------------------------------------------------------


def normalize(text: str) -> str:
    """Lower-case, strip accents/diaeresis, fold final sigma (ς -> σ)."""
    decomposed = unicodedata.normalize("NFD", str(text or "").lower())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).replace("ς", "σ")


# ---- keywords -------------------------------------------------------------------

_EXAM_RE = re.compile(
    r"\bπροοδ\w*|\bδιαγωνισμ\w*|\bτεστ\b|\btests?\b|\bquiz\w*|\bκουιζ\w*"
    r"|\bεξετασ\w*|\bενδιαμεσ\w*|\bmidterms?\b|\bexams?\b|\bexamination\w*")
_EXPLICIT_RE = re.compile(
    r"\bπροοδ\w*|\bδιαγωνισμ\w*|\bmidterms?\b|\bενδιαμεσ\w*\s+εξετασ\w*")
_FINAL_RE = re.compile(r"\bτελικ\w*|\bfinals?\b|\bεξεταστικ\w*")


def is_exam_text(text: str) -> bool:
    """Does `text` mention a test, quiz or exam (Greek or English)?"""
    return bool(_EXAM_RE.search(normalize(text)))


_QUIZ_RE = re.compile(r"\bquiz\w*|\bκουιζ\w*")


def is_quiz_only(text: str) -> bool:
    """Is quiz/κουίζ the only exam keyword (i.e. very likely a Moodle quiz)?"""
    norm = normalize(text)
    hits = [m.group(0) for m in _EXAM_RE.finditer(norm)]
    return bool(hits) and all(_QUIZ_RE.fullmatch(h) for h in hits)


def is_explicit(text: str) -> bool:
    """Is it unmistakably a test (πρόοδος, διαγώνισμα, midterm, ενδιάμεση εξέταση)?"""
    return bool(_EXPLICIT_RE.search(normalize(text)))


def exam_kind(text: str) -> str:
    """"final" for final-exam wording, else "test"."""
    return "final" if _FINAL_RE.search(normalize(text)) else "test"


# ---- dates ------------------------------------------------------------------------

# Greek month names by normalised prefix (genitive or nominative)...
_GREEK_MONTHS = (
    ("ιανουαρ", 1), ("φεβρουαρ", 2), ("μαρτ", 3), ("απριλ", 4), ("μαι", 5),
    ("ιουν", 6), ("ιουλ", 7), ("αυγουστ", 8), ("σεπτεμβρ", 9), ("οκτωβρ", 10),
    ("νοεμβρ", 11), ("δεκεμβρ", 12),
)
# ...and English ones as exact words (a prefix would read "decide 3" as December).
_ENGLISH_MONTHS = {name: i for i, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
    ("december", "dec")), start=1) for name in names}

# 15/11/2026, 15-11-26, 15.11.2026 (a year is required with dots: "10.30" is a time).
_NUMERIC_FULL = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?![\d/.-])")
# 15/11 or 15-11, no year. Never next to ":" (the "15-11" in "10:15-11:45" is
# a time range), and a hyphen pair right after a range noun ("κεφάλαια 1-10",
# "ασκήσεις 1-12", "pages 3-5") is a range, not a date: see _is_range.
_NUMERIC_SHORT = re.compile(r"(?<![\d/.:-])(\d{1,2})([/-])(\d{1,2})(?![\d/.:-])")
_RANGE_NOUN_BEFORE = re.compile(
    r"\b(?:κεφ\w*|ασκησ\w*|σελ\w*|ενοτητ\w*|διαλεξ\w*|θεμ\w*|ερωτησ\w*|προβλημ\w*"
    r"|παραγραφ\w*|slides?|chapters?|ch|exercises?|problems?|pages?|pp?|sections?"
    r"|lectures?|units?|questions?)\.?\s*(?:\d{1,2}\s*[,&]\s*|\d{1,2}\s+(?:και|and)\s+)*$")
RANGE_LOOKBEHIND_CHARS = 40
# "15 Νοεμβρίου", "15η Νοεμβρίου 2026", "15th November".
_DAY_MONTH = re.compile(r"(?<!\d)(\d{1,2})(?:ησ|η|th|st|nd|rd)?\s+([a-zα-ω]{3,})\.?(?:\s+(\d{4}))?")
# "November 15".
_MONTH_DAY = re.compile(r"\b([a-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?")
_TIME = re.compile(r"(?<![\d/.:-])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d/.-])")
TIME_LOOKAHEAD_CHARS = 60


def _month_from_word(word: str) -> int | None:
    if word in _ENGLISH_MONTHS:
        return _ENGLISH_MONTHS[word]
    for prefix, month in _GREEK_MONTHS:
        if word.startswith(prefix):
            return month
    return None


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    value = int(raw)
    return value + 2000 if value < 100 else value


def _resolve(day: int, month: int, year: int | None, low: date, high: date) -> date | None:
    """The date inside [low, high]; a year-less one must fit exactly one year."""
    years = [year] if year else sorted({low.year, high.year})
    hits = []
    for candidate in years:
        try:
            value = date(candidate, month, day)
        except ValueError:
            continue
        if low <= value <= high:
            hits.append(value)
    return hits[0] if len(hits) == 1 else None


def _is_range(norm: str, match: re.Match) -> bool:
    """Is a year-less "a-b" really a range of chapters/exercises/pages?"""
    if match.group(2) != "-":
        return False
    before = norm[max(0, match.start() - RANGE_LOOKBEHIND_CHARS):match.start()]
    return bool(_RANGE_NOUN_BEFORE.search(before))


def _time_after(text: str, end: int) -> str | None:
    match = _TIME.search(text[end:end + TIME_LOOKAHEAD_CHARS])
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def extract_dates(text: str, low: date, high: date) -> list[tuple[date, str | None]]:
    """Every (date, "HH:MM"|None) written in `text` that falls in [low, high].

    Understands dd/mm/yyyy, dd-mm-yy, dd.mm.yyyy, dd/mm, dd-mm, "15 Νοεμβρίου"
    (any Greek case, optional "η"/year), "15 November" and "November 15". A
    year-less date is kept only when exactly one year in the window fits it.
    A time is taken from just after the date ("15/11 ώρα 10:00"). Distinct
    dates, in order of appearance; the first time seen for a date wins.
    """
    norm = normalize(text)
    found: list[tuple[int, date, str | None]] = []
    taken: list[range] = []

    def add(match: re.Match, day: int, month: int, year: int | None) -> None:
        span = range(match.start(), match.end())
        if any(match.start() in r or (match.end() - 1) in r for r in taken):
            return
        value = _resolve(day, month, year, low, high) if 1 <= month <= 12 else None
        if value is not None:
            taken.append(span)
            found.append((match.start(), value, _time_after(norm, match.end())))

    for m in _NUMERIC_FULL.finditer(norm):
        add(m, int(m.group(1)), int(m.group(2)), _year(m.group(3)))
    for m in _DAY_MONTH.finditer(norm):
        month = _month_from_word(m.group(2))
        if month:
            add(m, int(m.group(1)), month, _year(m.group(3)))
    for m in _MONTH_DAY.finditer(norm):
        month = _month_from_word(m.group(1))
        if month:
            add(m, int(m.group(2)), month, _year(m.group(3)))
    for m in _NUMERIC_SHORT.finditer(norm):
        if not _is_range(norm, m):
            add(m, int(m.group(1)), int(m.group(3)), None)

    out: dict[date, str | None] = {}
    for _, value, clock in sorted(found, key=lambda f: f[0]):
        if value not in out:
            out[value] = clock
        elif out[value] is None and clock:
            out[value] = clock
    return list(out.items())


def single_date(text: str, low: date, high: date) -> tuple[date, str | None] | None:
    """The one date in `text`, or None when there is none or more than one."""
    dates = extract_dates(text, low, high)
    return dates[0] if len(dates) == 1 else None


__all__ = ["normalize", "is_exam_text", "is_quiz_only", "is_explicit", "exam_kind",
           "extract_dates", "single_date"]
