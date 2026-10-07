"""Today's lecture material: pick the files, fetch them once, turn them into text.

A course page holds the whole semester's slides, notes, exercise sheets and
past papers. This module narrows that to what today's topic needs and hands
the prompt builder one `Material` (spec §4.4).

Design rules:

* **The schedule's own links win.** Files in the module/section ids the parsed
  schedule tied to today are taken as they are. Only without those does the
  selector fall back to matching the topic — a lecture number first («Διάλεξη
  3» against `L03.pdf`), then shared keywords — and only a genuinely unclear
  match costs a helper-model call.
* **Download once.** Every file is cached under `CACHE_DIR`, keyed by
  `fileurl` + `timemodified`: an unchanged file is never fetched twice, and a
  professor re-uploading a file (new `timemodified`) is fetched again.
* **Scanned PDFs are not text.** A PDF with almost no extractable characters
  per page goes to `Material.attachments`, for the generator's Read tool, which
  reads the page images the way the API's `document` blocks would.
* **Past papers are separate.** Files that look like past exams never count as
  today's material; their text is extracted once (a `.txt` beside the cached
  file), and only the chunks relevant to today's topic go into the prompt.
* **Trim by relevance, and say so.** Material over the budget keeps its most
  topic-relevant pages, in reading order; every cut is written to
  `Material.notes` so the run log shows what the model never saw.
* **A bad file is a note, not a crash.** A download or extraction failure is
  logged and noted, and the rest of the day's material still goes through.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..fileio import atomic_write_text, read_text_locked
from .models import CACHE_DIR, Course, FileRef, Material, ScheduleEntry

if TYPE_CHECKING:
    from .moodle import MoodleClient
    from .state import JsonCache

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).with_name("prompts")
FILE_PICK_PROMPT_FILE = PROMPTS_DIR / "file_pick.txt"

# How much extracted lecture text one guide gets. Opus reads far more than
# this, but a whole textbook drowns the day's lecture; ~150k characters is a
# long lecture's slides plus its notes with room to spare.
MAX_MATERIAL_CHARS = 150_000

# How much past-exam text rides along. Enough for a handful of relevant
# problems; past papers are context for "exam level", not the lecture itself.
MAX_PAST_EXAM_CHARS = 30_000

# Below this many extractable characters per page, a PDF is a scan. A slide
# with a title and three bullets already has ~150; a scanned page has 0-20.
SCANNED_CHARS_PER_PAGE = 60

# How many past-exam chunks the helper model is shown when keywords find none,
# and how much of each: the call chooses among previews, it does not read them.
MAX_EXAM_CHUNKS_FOR_MODEL = 40
EXAM_PREVIEW_CHARS = 300

# How many candidate files the file-picking model is shown at most, and how
# many it may pick: one lecture is slides + notes + an exercise sheet, rarely more.
MAX_FILES_FOR_MODEL = 120
MAX_PICKED_FILES = 6

# A keyword match is "clear" when the best file shares at least this many
# topic keywords and beats the runner-up; anything weaker asks the model.
CLEAR_KEYWORD_SCORE = 2

# Keyword stems are this long, so Greek inflection («κύκλωμα», «κυκλώματα»,
# «κυκλωμάτων») still matches. Shorter tokens are ignored as noise.
STEM_CHARS = 6
MIN_TOKEN_CHARS = 4

# Extraction page markers. The trimmer splits on them, so they must be what
# extract_text writes.
_PAGE_MARK = "[σελ. {n}]"
_SLIDE_MARK = "[διαφάνεια {n}]"
_CHUNK_SPLIT = re.compile(r"(?m)^(?=\[(?:σελ\.|διαφάνεια) \d+\]$)")

_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".tex", ".c", ".h", ".cpp", ".py", ".java")
_HTML_SUFFIXES = (".html", ".htm")

# Matched against the accent-stripped, lower-cased name with _ . - turned into
# spaces. Greeklish forms are here because professors name files that way
# ("c_sos_themata_2025.pdf"); «πρόοδος» is a midterm, which counts. "exam" is
# a whole word so "examples.pdf" is not a past paper.
#
# Both patterns need exam/schedule CONTEXT, not a bare stem: «Ειδικά θέματα
# δένδρων» is a lecture, «Εξέταση ευστάθειας» is a method, and
# «Προγραμματισμός σε C» is half of Data Structures. So «θέματα» counts only
# next to «παλαιά», «εξετάσεων», an exam period or a year; «εξετάσεις» and
# «πρόοδος» only as whole words; «πρόγραμμα» only as a whole word with a
# schedule word beside it, or as the whole name.
_EXAM_PERIOD = (r"(?:ιανουαρ|φεβρουαρ|ιουν|ιουλ|σεπτεμβρ|εξεταστικ|"
                r"januar|februar|june|july|septemb|\d{4}\b|\d{1,2}\s+\d{2,4}\b)")
_PAST_EXAM_PATTERN = re.compile(
    r"παλαια\s+θεματα|θεματα\s+(?:εξετασ|προοδ|" + _EXAM_PERIOD + r")"
    r"|\bεξετασεισ?\b|\bεξετασεων\b|\bεξεταστικη\b|\bπροοδο[σι]?\b|\bπροοδου\b"
    r"|^\s*(?:θεματα|themata)\s*(?:pdf|docx?|zip)?\s*$"
    r"|palia\s+themata|palaia\s+themata|themata\s+(?:exetas|proodo|" + _EXAM_PERIOD + r")"
    r"|\bexetaseis\b|\bexetaseon\b|\bproodos\b|\bexams?\b|past\s+papers?|midterm")
_SCHEDULE_PATTERN = re.compile(
    r"\bπρογραμμα\s+(?:μαθηματ|διαλεξε|θεωρια|φροντιστηρ|εργαστηρ|εξαμην|σπουδ|υλησ)"
    r"|\bωρολογιο\s+προγραμμα\b|χρονοπρογραμμα|χρονοδιαγραμμα|^\s*προγραμμα\s*(?:\d{4}\s*)?"
    r"(?:pdf|docx?|xlsx?|odt)?\s*$|\bsyllabus\b|\bschedule\b|\bprogramma\s+mathim")

# Text without page markers (a DOCX, a text file) is cut into pieces about
# this long, at line breaks, so the trimmer still has something to rank.
PLAIN_CHUNK_CHARS = 4_000

# A lecture/chapter/week number in a topic or a file name. The label must
# start a word, so "approach 2" is not chapter 2.
_LABELLED_NUMBER = re.compile(
    r"(?<![^\W\d_])(?:διαλεξη|μαθημα|κεφαλαιο|ενοτητα|εβδομαδα|μερος|lecture|lect|lec|"
    r"chapter|chap|ch|week|unit|part|topic)\s*[._#-]?\s*0*(\d{1,2})(?!\d)")
# A number that opens a file name ("03 - Δίοδοι.pdf", "3_intro.pdf") or an
# L-number ("L03.pdf").
_LEADING_NUMBER = re.compile(r"^\s*0*(\d{1,2})[a-z]?(?=[\s._-])")   # "01b-enosi_pn.pdf"
_L_NUMBER = re.compile(r"(?<![a-z0-9])l0*(\d{1,2})(?!\d)")

# Words that say nothing about a topic, accent-stripped and lower-cased.
_STOPWORDS = frozenset({
    "και", "για", "στην", "στον", "στις", "στους", "στα", "απο", "προς", "μετα",
    "οπως", "ειναι", "αυτο", "αυτη", "μαθημα", "διαλεξη", "διαλεξεις", "κεφαλαιο",
    "ενοτητα", "εβδομαδα", "μερος", "ασκησεις", "ασκηση", "σημειωσεις", "εισαγωγη",
    "συνεχεια", "the", "and", "for", "with", "from", "into", "lecture", "chapter",
    "notes", "slides", "part", "week", "introduction", "intro", "pdf", "pptx",
    "docx",
})

AskJson = Callable[[str, dict, str], Any]

FILE_PICK_SCHEMA: dict = {
    "type": "object",
    "properties": {"module_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["module_ids"],
}
EXAM_PICK_SCHEMA: dict = {
    "type": "object",
    "properties": {"chunks": {"type": "array", "items": {"type": "integer"}}},
    "required": ["chunks"],
}

_FALLBACK_FILE_PICK_PROMPT = """\
Today's lecture topic: {topic}

Course: {course_name}

Below are the files on the course page, one per line, as
module_id | section name | filename. Pick at most {max_files}. Return the module ids of the files that
hold the material for today's topic (lecture slides, notes, exercise sheets for
it). Leave out files for other topics, past exam papers and schedules. If none
fit, return an empty list. JSON only: {{"module_ids": [..]}}

{files}
"""

_EXAM_PICK_PROMPT = """\
Today's lecture topic: {topic}

Below are numbered excerpts from past exam papers of this course. Return the
numbers of the excerpts whose problems test today's topic. If none do, return
an empty list. JSON only: {{"chunks": [..]}}

{chunks}
"""


# --------------------------------------------------------------------------
# Names

def _fold(text: str) -> str:
    """Lower-case, accents stripped, final sigma folded: a matching key only."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("ς", "σ")


def is_past_exam_name(name: str) -> bool:
    """True for «Θέματα», «Εξετάσεις», «Παλαιά θέματα», "exam", greeklish forms."""
    return bool(_PAST_EXAM_PATTERN.search(_name_key(name)))


def is_schedule_name(name: str) -> bool:
    """True for «Πρόγραμμα», «Χρονοδιάγραμμα», "syllabus", "schedule"."""
    return bool(_SCHEDULE_PATTERN.search(_name_key(name)))


def _name_key(name: str) -> str:
    return re.sub(r"[_.\-]+", " ", _fold(name or ""))


def _keywords(text: str) -> set[str]:
    """Topic keyword stems: folded words of 4+ letters, minus stopwords."""
    words = re.findall(r"[^\W\d_]+", _fold(text))
    return {w[:STEM_CHARS] for w in words
            if len(w) >= MIN_TOKEN_CHARS and w not in _STOPWORDS}


def _numbers(text: str, *, filename: bool = False) -> set[int]:
    """Lecture/chapter numbers written in `text`; file names also count a
    leading number or an L-number."""
    folded = _fold(text)
    found = {int(n) for n in _LABELLED_NUMBER.findall(folded)}
    if filename:
        found |= {int(n) for n in _LEADING_NUMBER.findall(folded)}
        found |= {int(n) for n in _L_NUMBER.findall(folded)}
    return found


# --------------------------------------------------------------------------
# Selection

def select_files(entry: ScheduleEntry | None, topic: str, files: list[FileRef],
                 ask_json: AskJson | None = None, model: str = "sonnet", *,
                 course_name: str = "") -> list[FileRef]:
    """Today's files, in course-page order.

    Order of preference (spec §4.4): the schedule's module ids, then its
    section ids, then a lecture-number match, then a clear keyword match, then
    the helper model (when `ask_json` is given), then the best keyword guess.
    Past exams and schedule files are never selected here. `course_name` only
    fills the helper-model prompt.
    """
    named = _by_filenames(entry, files)   # named outright: no name filter applies
    if named:                              # («…Πρόγραμμα Σπουδών).pdf» is notes)
        return named
    candidates = [f for f in files
                  if not is_past_exam_name(f.filename) and not is_schedule_name(f.filename)]
    if not candidates:
        return []
    for picker in (_by_module_ids, _by_section_ids):
        picked = picker(entry, candidates)
        if picked:
            return picked
    by_number = _by_lecture_number(topic, candidates)
    if by_number:
        log.info("files for %r matched by lecture number: %d", topic, len(by_number))
        return by_number
    scored = _keyword_scores(topic, candidates)
    if _is_clear(scored):
        return _with_score(scored, scored[0][0])
    if ask_json is not None:
        picked = _ask_model(topic, candidates, ask_json, model, course_name)
        if picked is not None:
            return picked
    best = scored[0][0] if scored else 0
    if best <= 0:
        log.warning("no file matches topic %r", topic)
        return []
    return _with_score(scored, best)


def _by_filenames(entry: ScheduleEntry | None, files: list[FileRef]) -> list[FileRef]:
    """Files named by a syllabus strand. One Moodle folder holds a whole
    lecturer's notes under a single module id, so ids cannot pick one chapter."""
    if entry is None or not entry.filenames:
        return []
    wanted = {_fold(n) for n in entry.filenames}
    return [f for f in files if _fold(f.filename) in wanted]


def _by_module_ids(entry: ScheduleEntry | None, files: list[FileRef]) -> list[FileRef]:
    if entry is None or not entry.module_ids:
        return []
    wanted = set(entry.module_ids)
    return [f for f in files if f.module_id in wanted]


def _by_section_ids(entry: ScheduleEntry | None, files: list[FileRef]) -> list[FileRef]:
    if entry is None or not entry.section_ids:
        return []
    wanted = set(entry.section_ids)
    return [f for f in files if f.section_id in wanted]


def _by_lecture_number(topic: str, files: list[FileRef]) -> list[FileRef]:
    """Files whose name (or, failing that, section) carries the topic's number."""
    numbers = _numbers(topic)
    if not numbers:
        return []
    by_name = [f for f in files if _numbers(f.filename, filename=True) & numbers]
    if by_name:
        return by_name
    return [f for f in files if _numbers(f.section_name) & numbers]


def _keyword_scores(topic: str, files: list[FileRef]) -> list[tuple[int, int, FileRef]]:
    """(score, page-order index, file), best first. Score = shared keyword stems."""
    wanted = _keywords(topic)
    scored = []
    for index, ref in enumerate(files):
        have = _keywords(f"{ref.filename} {ref.section_name}")
        scored.append((len(wanted & have), index, ref))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored


def _is_clear(scored: list[tuple[int, int, FileRef]]) -> bool:
    if not scored or scored[0][0] < CLEAR_KEYWORD_SCORE:
        return False
    return len(scored) == 1 or scored[0][0] > scored[1][0]


def _with_score(scored: list[tuple[int, int, FileRef]], score: int) -> list[FileRef]:
    """Every file on the top score, back in course-page order."""
    top = sorted((t for t in scored if t[0] == score), key=lambda t: t[1])
    return [ref for _, _, ref in top]


def _ask_model(topic: str, files: list[FileRef], ask_json: AskJson,
               model: str, course_name: str) -> list[FileRef] | None:
    """The helper model's pick, or None when the call or its answer failed."""
    shown = files[:MAX_FILES_FOR_MODEL]
    if len(files) > len(shown):
        log.warning("file pick: showing the model %d of %d files", len(shown), len(files))
    listing = "\n".join(f"{f.module_id} | {f.section_name} | {f.filename}" for f in shown)
    prompt = _fill(_load_prompt(FILE_PICK_PROMPT_FILE, _FALLBACK_FILE_PICK_PROMPT),
                   {"topic": topic, "files": listing, "course_name": course_name or "-",
                    "max_files": str(MAX_PICKED_FILES)})
    try:
        answer = ask_json(prompt, FILE_PICK_SCHEMA, model)
    except Exception as exc:  # noqa: BLE001 — any CLI failure falls back to keywords
        log.warning("file pick model call failed (%s); using keyword match", exc)
        return None
    ids = _int_list(answer, "module_ids")
    if ids is None:
        log.warning("file pick answer unusable (%r); using keyword match", answer)
        return None
    wanted = set(ids)
    picked = [f for f in files if f.module_id in wanted]
    log.info("file pick model chose %d file(s) for %r", len(picked), topic)
    return picked


def _int_list(answer: Any, key: str) -> list[int] | None:
    """A list of ints from `{key: [...]}`, a bare list, or JSON text of either."""
    if isinstance(answer, str):
        try:
            answer = json.loads(_strip_fence(answer))
        except json.JSONDecodeError:
            return None
    if isinstance(answer, dict):
        answer = answer.get(key)
    if not isinstance(answer, list):
        return None
    out = []
    for item in answer:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _strip_fence(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def _load_prompt(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("prompt %s unreadable (%s); using the built-in one", path.name, exc)
        return fallback


def _fill(template: str, fields: dict[str, str]) -> str:
    """Plain `{name}` replacement (templates carry literal JSON braces); a
    str.format-style template's doubled braces are unescaped first."""
    if "{{" in template or "}}" in template:
        template = template.replace("{{", "{").replace("}}", "}")
    for key, value in fields.items():
        template = template.replace("{" + key + "}", value)
    return template


# --------------------------------------------------------------------------
# Download

def ensure_downloaded(client: "MoodleClient", ref: FileRef, cache: "JsonCache") -> Path:
    """The local copy of `ref`, downloading it only when it is new or changed.

    Raises whatever `client.download` raises (a `MoodleError`, an OSError);
    `build_material` turns that into a note.
    """
    key = _cache_key(ref)
    record = cache.get(key)
    if isinstance(record, dict):
        cached = Path(str(record.get("path", "")))
        if cached.is_file() and _size_ok(cached, ref):
            return cached
        log.info("cached copy of %s is missing or incomplete; fetching again", ref.filename)
    dest = CACHE_DIR / _cache_name(ref, key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    path = Path(client.download(ref.fileurl, dest))
    cache.put(key, {"path": str(path), "filename": ref.filename,
                    "size": path.stat().st_size})
    log.info("downloaded %s (%d bytes)", ref.filename, path.stat().st_size)
    return path


def _cache_key(ref: FileRef) -> str:
    return f"{ref.fileurl}|{ref.timemodified}"


def _size_ok(path: Path, ref: FileRef) -> bool:
    """Moodle's `filesize` is exact; 0 means it did not say."""
    return ref.filesize <= 0 or path.stat().st_size == ref.filesize


def _cache_name(ref: FileRef, key: str) -> str:
    """`<module>_<hash>_<name>`: unique per version, still recognisable."""
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    stem, suffix = Path(ref.filename).stem, Path(ref.filename).suffix
    safe_stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", stem).strip(" .")[:60] or "file"
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:10]
    return f"{ref.module_id}_{digest}_{safe_stem}{safe_suffix}"


# --------------------------------------------------------------------------
# Extraction

def extract_text(path: Path) -> str:
    """Plain text of a PDF, PPTX, DOCX, text or HTML file; "" if unsupported
    or unreadable (logged). PDF pages and slides are prefixed with markers
    (`[σελ. N]`, `[διαφάνεια N]`) the trimmer relies on."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf_text(path)
        if suffix in (".pptx", ".docx"):
            problem = _zip_bomb(path)
            if problem:
                log.warning("skipped %s: %s", path.name, problem)
                return ""
            return _pptx_text(path) if suffix == ".pptx" else _docx_text(path)
        if suffix in _TEXT_SUFFIXES:
            return _plain_text(path)
        if suffix in _HTML_SUFFIXES:
            from .moodle import html_to_text
            return html_to_text(_plain_text(path))
    except Exception as exc:  # noqa: BLE001 — corrupt files raise anything
        log.warning("could not extract text from %s: %s: %s",
                    path.name, type(exc).__name__, exc)
        return ""
    log.info("no text extractor for %s (%s); skipped", path.name, suffix or "no suffix")
    return ""


# Office files are zips; a tiny one can expand to gigabytes of XML. Lecture
# decks with images stay far below these.
MAX_PDF_PAGES = 400                 # a course file longer than this is a textbook
MAX_OFFICE_UNCOMPRESSED = 200 * 1024 * 1024
MAX_OFFICE_XML_MEMBER = 20 * 1024 * 1024
MAX_OFFICE_MEMBERS = 5000
_ZIP_CHUNK = 1024 * 1024


def _zip_bomb(path: Path) -> str:
    """Why this .docx/.pptx must not be parsed, or "" when it may be."""
    import zipfile
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_OFFICE_MEMBERS:
            return f"has {len(infos)} parts"
        declared = sum(i.file_size for i in infos)
        if declared > MAX_OFFICE_UNCOMPRESSED:
            return f"expands to {declared // (1024 * 1024)} MB"
        # The sizes in the zip headers can lie: count what the XML parts really
        # decompress to, stopping as soon as a cap is passed.
        total = 0
        for info in infos:
            if not info.filename.lower().endswith((".xml", ".rels")):
                continue
            size = 0
            with zf.open(info) as member:
                while chunk := member.read(_ZIP_CHUNK):
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_OFFICE_XML_MEMBER:
                        return f"{info.filename} expands past {MAX_OFFICE_XML_MEMBER // (1024 * 1024)} MB"
                    if total > MAX_OFFICE_UNCOMPRESSED:
                        return f"expands past {MAX_OFFICE_UNCOMPRESSED // (1024 * 1024)} MB"
    return ""


def _pdf_text(path: Path) -> str:
    import pymupdf
    parts = []
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, start=1):
            if number > MAX_PDF_PAGES:
                log.warning("%s: only the first %d pages read", path.name, MAX_PDF_PAGES)
                break
            text = page.get_text("text").strip()
            if text:
                parts.append(f"{_PAGE_MARK.format(n=number)}\n{text}")
    return "\n\n".join(parts)


def _pptx_text(path: Path) -> str:
    from pptx import Presentation
    parts = []
    for number, slide in enumerate(Presentation(str(path)).slides, start=1):
        lines = [line for shape in slide.shapes for line in _shape_lines(shape)]
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                lines.append(f"Σημειώσεις: {notes.text.strip()}")
        if lines:
            parts.append(f"{_SLIDE_MARK.format(n=number)}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _shape_lines(shape: Any) -> list[str]:
    """Text of one slide shape, recursing into groups and reading tables."""
    if hasattr(shape, "shapes"):   # a group shape
        return [line for inner in shape.shapes for line in _shape_lines(inner)]
    if getattr(shape, "has_table", False):
        return [" | ".join(cell.text.strip() for cell in row.cells)
                for row in shape.table.rows]
    if getattr(shape, "has_text_frame", False):
        text = shape.text_frame.text.strip()
        return [text] if text else []
    return []


def _docx_text(path: Path) -> str:
    import docx
    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(lines)


def _plain_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1253"):   # cp1253: Greek Windows files
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_scanned_pdf(path: Path) -> bool:
    """True when a PDF has too few extractable characters per page to be text."""
    try:
        import pymupdf
        with pymupdf.open(Path(path)) as doc:
            pages = doc.page_count
            if pages == 0:
                return False
            pages = min(pages, MAX_PDF_PAGES)
            chars = sum(len(page.get_text("text").strip())
                        for _, page in zip(range(pages), doc))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not inspect %s for scanning: %s", Path(path).name, exc)
        return False
    return chars / pages < SCANNED_CHARS_PER_PAGE


# --------------------------------------------------------------------------
# Trimming

def _chunks(text: str) -> list[str]:
    """Split extracted text at its page/slide markers, or, when it has none,
    into ~PLAIN_CHUNK_CHARS pieces at line breaks."""
    marked = [c for c in _CHUNK_SPLIT.split(text) if c.strip()]
    if len(marked) > 1 or len(text) <= PLAIN_CHUNK_CHARS:
        return marked or [text]
    pieces, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > PLAIN_CHUNK_CHARS:
            pieces.append(current)
            current = ""
        current += line
    if current.strip():
        pieces.append(current)
    return pieces


def _relevant_chunks(chunks: list[str], topic: str, budget: int) -> tuple[list[int], int]:
    """Indices of the chunks to keep (reading order) within `budget` chars,
    most topic-relevant first; and the number of chunks dropped."""
    wanted = _keywords(topic)
    ranked = sorted(range(len(chunks)),
                    key=lambda i: (-len(wanted & _keywords(chunks[i])), i))
    kept, used = [], 0
    for index in ranked:
        size = len(chunks[index])
        if used + size > budget:
            continue
        kept.append(index)
        used += size
    return sorted(kept), len(chunks) - len(kept)


def _trim(docs: list[tuple[str, str]], topic: str,
          max_chars: int) -> tuple[list[tuple[str, str]], list[str]]:
    """Fit (label, text) documents into `max_chars`, sharing the budget evenly
    across documents that exceed their share. Returns documents and notes."""
    total = sum(len(text) for _, text in docs)
    if total <= max_chars:
        return docs, []
    small = [len(t) for _, t in docs if len(t) <= max_chars // len(docs)]
    big_count = len(docs) - len(small)
    share = (max_chars - sum(small)) // max(big_count, 1)
    out, notes = [], []
    for label, text in docs:
        if len(text) <= max_chars // len(docs):
            out.append((label, text))
            continue
        chunks = _chunks(text)
        kept, dropped = _relevant_chunks(chunks, topic, share)
        out.append((label, "\n\n".join(chunks[i] for i in kept)))
        note = (f"trimmed {label}: kept {len(kept)} of {len(chunks)} pages "
                f"most relevant to the topic ({dropped} dropped, budget {share} chars)")
        notes.append(note)
        log.info(note)
    return out, notes


# --------------------------------------------------------------------------
# Past exams

def _exam_text(path: Path) -> str:
    """Extracted text of a past paper, cached in a `.txt` beside the download."""
    sidecar = path.with_name(path.name + ".txt")
    if sidecar.is_file():
        try:
            return read_text_locked(sidecar)
        except OSError as exc:
            log.warning("past-exam text cache %s unreadable (%s); re-extracting",
                        sidecar.name, exc)
    text = extract_text(path)
    try:
        atomic_write_text(sidecar, text)
    except OSError as exc:
        log.warning("could not cache past-exam text for %s: %s", path.name, exc)
    return text


def _relevant_exam_text(texts: list[tuple[str, str]], topic: str,
                        ask_json: AskJson | None, model: str) -> tuple[str, list[str]]:
    """The past-exam chunks that test today's topic, with their source names."""
    chunks = [(name, chunk) for name, text in texts for chunk in _chunks(text)]
    if not chunks:
        return "", []
    wanted = _keywords(topic)
    hits = [i for i, (_, chunk) in enumerate(chunks) if wanted & _keywords(chunk)]
    notes: list[str] = []
    if not hits and ask_json is not None:
        hits = _ask_exam_chunks(topic, chunks, ask_json, model)
        notes.append(f"past exams: chunks chosen by {model}")
    if not hits:
        return "", ["past exams: nothing relevant to the topic"]
    wanted_sorted = sorted(hits, key=lambda i: (-len(wanted & _keywords(chunks[i][1])), i))
    out, used = [], 0
    for index in wanted_sorted:
        name, chunk = chunks[index]
        block = f"--- {name} ---\n{chunk}"
        if used + len(block) > MAX_PAST_EXAM_CHARS:
            notes.append(f"past exams: capped at {MAX_PAST_EXAM_CHARS} chars")
            break
        out.append((index, block))
        used += len(block)
    return "\n\n".join(b for _, b in sorted(out)), notes


def _ask_exam_chunks(topic: str, chunks: list[tuple[str, str]], ask_json: AskJson,
                     model: str) -> list[int]:
    shown = chunks[:MAX_EXAM_CHUNKS_FOR_MODEL]
    listing = "\n\n".join(f"[{i}] ({name}) {' '.join(chunk.split())[:EXAM_PREVIEW_CHARS]}"
                          for i, (name, chunk) in enumerate(shown))
    prompt = _fill(_EXAM_PICK_PROMPT, {"topic": topic, "chunks": listing})
    try:
        ids = _int_list(ask_json(prompt, EXAM_PICK_SCHEMA, model), "chunks")
    except Exception as exc:  # noqa: BLE001 — past exams are optional context
        log.warning("past-exam pick model call failed: %s", exc)
        return []
    return [i for i in ids or [] if 0 <= i < len(shown)]


def _past_exams(files: list[FileRef], topic: str, client: "MoodleClient",
                cache: "JsonCache", ask_json: AskJson | None,
                model: str) -> tuple[str, list[str]]:
    exams = [f for f in files if is_past_exam_name(f.filename)]
    texts, notes = [], []
    for ref in exams:
        try:
            path = ensure_downloaded(client, ref, cache)
        except Exception as exc:  # noqa: BLE001 — one bad paper must not stop the rest
            notes.append(f"past exam {ref.filename}: download failed ({exc})")
            log.warning("past exam %s: download failed: %s", ref.filename, exc)
            continue
        if path.suffix.lower() == ".pdf" and is_scanned_pdf(path):
            notes.append(f"past exam {ref.filename}: scanned, skipped")
            continue
        text = _exam_text(path)
        if text.strip():
            texts.append((ref.filename, text))
    text, pick_notes = _relevant_exam_text(texts, topic, ask_json, model) if texts else ("", [])
    return text, notes + pick_notes


# --------------------------------------------------------------------------
# The day's material

def build_material(course: Course, entry: ScheduleEntry | None, topic: str,
                   files: list[FileRef], client: "MoodleClient", cache: "JsonCache",
                   ask_json: AskJson | None = None, model: str = "sonnet",
                   max_chars: int = MAX_MATERIAL_CHARS) -> Material:
    """Select, download and extract today's material for one course.

    Scanned PDFs are returned as `attachments` (cache paths; the caller stages
    them into the generator's working directory). Never raises for a single
    bad file — that becomes a note.
    """
    selected = select_files(entry, topic, files, ask_json=ask_json, model=model,
                            course_name=course.name_gr)
    notes: list[str] = []
    if not selected:
        notes.append(f"{course.key}: no material file matched the topic")
    docs, attachments, used = _collect(selected, client, cache, notes)
    docs, trim_notes = _trim(docs, topic, max_chars)
    notes.extend(trim_notes)
    past, exam_notes = _past_exams(files, topic, client, cache, ask_json, model)
    notes.extend(exam_notes)
    text = "\n\n".join(f"=== {label} ===\n{body}" for label, body in docs if body.strip())
    for note in notes:
        log.info("%s: %s", course.key, note)
    return Material(text=text, files_used=tuple(used), attachments=tuple(attachments),
                    past_exams=past, notes=tuple(notes))


def _collect(selected: Iterable[FileRef], client: "MoodleClient", cache: "JsonCache",
             notes: list[str]) -> tuple[list[tuple[str, str]], list[Path], list[str]]:
    """(label, text) per text file, scanned-PDF paths, and filenames used."""
    docs: list[tuple[str, str]] = []
    attachments: list[Path] = []
    used: list[str] = []
    for ref in selected:
        try:
            path = ensure_downloaded(client, ref, cache)
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the rest
            notes.append(f"{ref.filename}: download failed ({exc})")
            log.warning("%s: download failed: %s", ref.filename, exc)
            continue
        if path.suffix.lower() == ".pdf" and is_scanned_pdf(path):
            attachments.append(path)
            used.append(ref.filename)
            notes.append(f"{ref.filename}: scanned PDF, attached for the Read tool")
            continue
        text = extract_text(path)
        if not text.strip():
            notes.append(f"{ref.filename}: no extractable text")
            continue
        label = f"{ref.filename} ({ref.section_name})" if ref.section_name else ref.filename
        docs.append((label, text))
        used.append(ref.filename)
    return docs, attachments, used
