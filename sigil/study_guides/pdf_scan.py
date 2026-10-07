"""Checks on a built guide PDF before it may be filed or sent anywhere.

The LaTeX is written by a model that reads untrusted course material, and the
second build stage (xdvipdfmx) can read files and add raw PDF objects through
specials that xelatex's own file record never lists. Filtering the LaTeX
source cannot be complete, so the finished PDF itself is checked:

* **No active content.** No JavaScript, embedded files, forms, media, or
  automatic actions; launch actions only to the weekly review's
  "../<course>/<guide>.pdf" links; web links only over https.
* **No stray streams.** Every stream must hang off the page tree (contents,
  fonts, images, forms) or be the document metadata. A file pulled in with
  `pdf:fstream` and parked anywhere else is refused.
* **No secret.** The PDF's bytes and every decoded stream are searched for the
  secrets this process knows (environment and stored token files).

`pdf_problems` returns why the PDF must not be used, or [] when it may.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MIN_SECRET_CHARS = 8

_REF = re.compile(r"(\d+)\s+0\s+R")
_PARENT = re.compile(r"/Parent\s+\d+\s+0\s+R")
_ACTIVE = re.compile(r"/(JavaScript|JS|GoToR|GoToE|SubmitForm|ImportData|EmbeddedFiles?"
                     r"|RichMedia\w*|AA|AcroForm|Movie|Sound|Rendition|XFA|FileAttachment"
                     r"|Win|Mac|Unix)(?![A-Za-z0-9])")
_LAUNCH = re.compile(r"/S\s*/Launch(?![A-Za-z0-9])")
_LAUNCH_FILE = re.compile(r"/F\s*\(((?:[^()\\]|\\.)*)\)")
_URI = re.compile(r"/URI\s*\(((?:[^()\\]|\\.)*)\)")
_OPEN_ACTION_DICT = re.compile(r"/OpenAction\s*<<(.*?)>>", re.S)
_STRUCTURAL_TYPES = re.compile(r"/Type\s*/(ObjStm|XRef|Metadata)(?![A-Za-z0-9])")
# The weekly review's links to guide PDFs, written by the code, not the model.
_GUIDE_LINK = re.compile(r"^\.\./(?:[^/\\:.][^/\\:]*/)*[^/\\:]+\.pdf$", re.I)


def known_secrets() -> list[str]:
    """Every secret value this process can see (environment and credential
    store). Short values are skipped: too many false matches, and too short to
    be a credential."""
    from pathlib import Path
    from ..secrets_store import all_values
    home = str(Path.home())          # local paths name the user; a guide never needs one
    values = [*all_values(), home, home.replace("\\", "/")]
    return sorted({v for v in values if len(v) >= MIN_SECRET_CHARS})


def _pdf_string(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def pdf_problems(pdf: Path, secrets: list[str] | None = None) -> list[str]:
    import pymupdf
    secrets = known_secrets() if secrets is None else secrets
    problems: list[str] = []
    try:
        data = Path(pdf).read_bytes()
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — an unreadable PDF is not filed
        return [f"the PDF cannot be read ({type(exc).__name__})"]
    with doc:
        count = doc.xref_length()
        reachable = _page_tree(doc, count)
        needles = [(s, [s.encode("utf-8"), s.encode("utf-16-be")]) for s in secrets]
        if any(n in data for _, forms in needles for n in forms):
            problems.append("the PDF contains a secret value")
        # Typeset text is stored as glyph codes, so search the extracted text too
        # (with spaces and hyphenation removed, as a line break can split a token).
        page_text = "".join(page.get_text("text") for page in doc)
        squashed = re.sub(r"[\s­-]+", "", page_text)
        if any(s in page_text or re.sub(r"[\s-]+", "", s) in squashed for s, _ in needles):
            problems.append("the PDF text shows a secret value")
        for xref in range(1, count):
            try:
                text = doc.xref_object(xref, compressed=True)
            except Exception:  # noqa: BLE001 — free or broken entries
                continue
            problems += _object_problems(xref, text)
            if not doc.xref_is_stream(xref):
                continue
            try:
                stream = doc.xref_stream(xref) or b""
            except Exception:  # noqa: BLE001 — images in filters PyMuPDF won't decode
                stream = b""
            # pgf leaves unused form XObjects (transparency groups) around; those
            # are drawing code. A file pulled in with pdf:fstream is not.
            if (xref not in reachable and not _STRUCTURAL_TYPES.search(text)
                    and not (_FORM_XOBJECT.search(text) and is_drawing_code(stream))):
                problems.append(f"object {xref} is a stream outside the page tree")
            if any(n in stream for _, forms in needles for n in forms):
                problems.append(f"object {xref} contains a secret value")
    return sorted(set(problems))


def _page_tree(doc, count: int) -> set[int]:
    """Objects reachable from the pages (not through /Parent)."""
    seen: set[int] = set()
    stack = [doc[i].xref for i in range(doc.page_count)]
    while stack:
        xref = stack.pop()
        if xref in seen or not 0 < xref < count:
            continue
        seen.add(xref)
        try:
            text = doc.xref_object(xref, compressed=True)
        except Exception:  # noqa: BLE001
            continue
        stack += [int(m) for m in _REF.findall(_PARENT.sub("", text))]
    return seen


_FORM_XOBJECT = re.compile(r"/Subtype\s*/Form(?![A-Za-z0-9])")
# The PDF content-stream operators (ISO 32000 Annex A), minus inline images.
_OPERATORS = frozenset(
    "b B b* B* BDC BMC BT BX c cm CS cs d d0 d1 Do DP EMC ET EX f F f* G g gs h i j J K "
    "k l m M MP n q Q re RG rg ri s S SC sc SCN scn sh T* Tc Td TD Tf Tj TJ TL Tm Tr Ts "
    "Tw Tz v w W W* y ' \"".split())
_CONTENT_TOKEN = re.compile(
    rb"\s+|%[^\r\n]*"                                   # whitespace, comments
    rb"|[+-]?(?:\d+\.?\d*|\.\d+)(?![^\s/<>\[\]()%])"     # numbers
    rb"|/[^\s/<>\[\]()%{}]*"                            # names
    rb"|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\)"      # strings (one nesting level)
    rb"|<[0-9A-Fa-f\s]*>|<<|>>|\[|\]"                   # hex strings, dicts, arrays
    rb"|(?P<op>[A-Za-z'\"][A-Za-z0-9*]*)")             # operators
MAX_DRAWING_CODE_BYTES = 256 * 1024


def is_drawing_code(stream: bytes) -> bool:
    """Does `stream` tokenize entirely into PDF drawing operators and operands?"""
    if len(stream) > MAX_DRAWING_CODE_BYTES:
        return False
    pos = 0
    while pos < len(stream):
        m = _CONTENT_TOKEN.match(stream, pos)
        if not m or m.end() == pos:
            return False
        op = m.group("op")
        if op is not None and op.decode("latin-1") not in _OPERATORS:
            return False
        pos = m.end()
    return True


_IMAGE = re.compile(r"/Subtype\s*/Image(?![A-Za-z0-9])")


def _object_problems(xref: int, text: str) -> list[str]:
    out = []
    # Guides are drawn with TikZ and never include an image; an image object
    # can only come from a file read into the PDF behind the checks' back.
    if _IMAGE.search(text):
        out.append(f"object {xref} embeds an image")
    active = _ACTIVE.search(text)
    if active:
        out.append(f"object {xref} has /{active.group(1)} (active content)")
    if _LAUNCH.search(text):
        target = _LAUNCH_FILE.search(text)
        if not target or not _GUIDE_LINK.match(_pdf_string(target.group(1))):
            out.append(f"object {xref} launches a file or program")
    for uri in _URI.findall(text):
        if not _pdf_string(uri).lower().startswith("https://"):
            out.append(f"object {xref} links to a non-https address")
    for action in _OPEN_ACTION_DICT.findall(text):
        if not re.search(r"/S\s*/GoTo(?![A-Za-z])", action):
            out.append(f"object {xref} runs an action when opened")
    return out
