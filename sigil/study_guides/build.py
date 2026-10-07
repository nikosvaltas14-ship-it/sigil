"""LaTeX -> PDF for the study guides: xelatex, twice, with a repair loop.

Design rules:
* **xelatex is an explicit path.** MiKTeX is installed per-user and is not on
  PATH, so the caller passes the resolved binary (settings.xelatex); nothing
  here searches for it beyond a PATH lookup of what it was given.
* **A build never waits for a person.** stdin is closed, the interaction mode
  is nonstopmode with -halt-on-error, and the window is hidden. MiKTeX's
  on-the-fly package installer is switched on explicitly, so a missing package
  is fetched rather than turned into an "install?" dialog that nobody under
  pythonw will ever click (that dialog would hang the build until the
  watchdog, then fail anyway).
* **Success means a fresh PDF.** The previous attempt's guide.pdf is removed
  first, so a stale file from a good earlier run can never pass for this one.
* **Everything stays in the working directory** for debugging: guide.tex,
  guide.log, and each failed attempt's .tex/.log as guide.attemptN.*. The
  auxiliary files of an earlier attempt (.aux/.toc/.out/...) are removed
  first: a halted pass leaves them truncated, and the next first pass would
  choke on them and burn a repair turn on an error the LaTeX does not have.
* **The LaTeX is untrusted.** The model wrote it from Moodle material, so it
  gets no shell escape and no file access outside the working directory: a
  document naming an absolute or '..' path in a file command, or using
  \\write18 / \\openin / \\openout, is refused before xelatex runs (the refusal
  goes to the repair turn like a build error). A textual check cannot see
  through every catcode trick, so it is a guard, not a sandbox.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import child_env
from ..proc import kill_tree as _kill_tree

log = logging.getLogger(__name__)

TEX_NAME = "guide.tex"
# xelatex runs twice: the second pass resolves references and the tcolorbox /
# tikz positions the first one only recorded.
XELATEX_PASSES = 2
# The spec's "last ~60 lines of the log" that a repair turn gets to see.
LOG_TAIL_LINES = 60
# Per-pass ceiling. A guide builds in well under a minute; the first build on a
# fresh MiKTeX also refreshes font caches, which is why this is generous.
DEFAULT_TIMEOUT = 600
# At most this many repair turns before the course is marked failed (spec 4.6).
DEFAULT_MAX_REPAIRS = 2
_XELATEX_FLAGS = ("-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                  "-recorder")      # guide.fls: every file TeX opened (files_read_outside)
# No shell escape, spelled per distribution (MiKTeX does not know TeX Live's).
_MIKTEX_NO_SHELL = "-disable-write18"
_TEXLIVE_NO_SHELL = "-no-shell-escape"
# TeX Live: read/write files only under the working directory ("paranoid").
# MiKTeX ignores these; the path check in `unsafe_latex` is what covers MiKTeX.
_TEX_ENV = {"openin_any": "p", "openout_any": "p"}
# Files a previous (possibly halted) attempt may have left behind.
_STALE_SUFFIXES = (".pdf", ".aux", ".toc", ".out", ".lof", ".lot", ".log", ".xdv", ".fls")

# Primitives a study guide never needs and that reach outside the document.
# \csname, \catcode and \scantokens can spell any other command without naming
# it; the rest read or write files under names `_FILE_COMMANDS` does not know.
_FORBIDDEN = re.compile(
    r"\\(?:write18|ShellEscape|openin|openout|directlua|latelua|csname|catcode|scantokens"
    r"|read|readline|newread|newwrite|@@input|XeTeXpdffile|XeTeXpicfile|pdffiledump"
    r"|pgfplotstableread|csvautotabular|csvautolongtable|csvreader|DTLloaddb|DTLloadrawdb"
    r"|markdownInput|filecontents|verbatimwrite|embedfile|attachfile|textattachfile"
    r"|includemedia"
    # Raw driver specials reach xdvipdfmx, which reads files and writes PDF
    # objects on its own; and the commands that name a command indirectly.
    r"|special|ExplSyntaxOn|ProvidesExplPackage|ProvidesExplFile|ProvidesExplClass"
    r"|UseName|ExpandArgs|@nameuse|@namedef|@ifundefined|csuse|csdef|csgdef|csedef"
    r"|csxdef|cslet|letcs|csletcs|ifcsname|DocumentMetadata|lowercase|uppercase"
    # PDF forms carry JavaScript actions.
    r"|PushButton|TextField|CheckBox|ChoiceMenu|Submit|Reset)(?![A-Za-z@])")
_FORM_ENV = re.compile(r"\\begin\s*\{\s*Form\s*\}")
# \usepackage / \documentclass naming a path instead of a plain package name.
_PACKAGE_PATH = re.compile(r"\\(?:usepackage|RequirePackage|documentclass|LoadClass)"
                           r"\s*(?:\[[^\]]*\]\s*)?\{[^}]*[/\\.:~][^}]*\}")
# PDF links that launch a program or open a local file when clicked.
_LAUNCH_LINK = re.compile(r"\\(?:href|url)\s*(?:\[[^\]]*\]\s*)?\{\s*(run|file|launch|javascript)"
                          r"\s*:([^}]*)\}", re.I)
# The only launch link allowed: the weekly review's "../<course>/<guide>.pdf",
# which the code (not the model) supplies.
_GUIDE_LINK = re.compile(r"^\.\./(?:[^/\\:.][^/\\:]*/)*[^/\\:]+\.pdf$", re.I)
# Commands that take file names in their brace arguments.
_FILE_COMMANDS = re.compile(
    r"\\(input|include|InputIfFileExists|IfFileExists|verbatiminput|VerbatimInput"
    r"|lstinputlisting|inputminted|includegraphics|includepdf|includestandalone|import"
    r"|subimport|subfile|bibliography|addbibresource)(?![A-Za-z@])\*?\s*(?:\[[^\]]*\]\s*)*"
    r"((?:\{[^{}]*\}\s*){1,2})")
# A file command whose argument opens with a second brace: `{{../x}}` hides the
# name from `_BRACE_ARG`, and no real guide needs it.
_NESTED_FILE_ARG = re.compile(
    r"\\(input|include|InputIfFileExists|IfFileExists|verbatiminput|VerbatimInput"
    r"|lstinputlisting|inputminted|includegraphics|includepdf|includestandalone|import"
    r"|subimport|subfile|bibliography|addbibresource)(?![A-Za-z@])\*?\s*(?:\[[^\]]*\]\s*)*"
    r"\{\s*\{")
# The TeX primitive form: \input file (no braces).
_BARE_INPUT = re.compile(r"\\input\s+([^\s{}\\]+)")
_BRACE_ARG = re.compile(r"\{([^{}]*)\}")
# Absolute (/x, \x, ~/x, C:x) or containing a '..' component.
_UNSAFE_PATH = re.compile(r"^\s*(?:[/\\~]|[A-Za-z]:)|(?:^|[/\\])\s*\.\.\s*(?:[/\\]|$)")
# MiKTeX-only flag (TeX Live's xelatex does not know it): install missing
# packages silently instead of asking.
_MIKTEX_INSTALLER_FLAG = "-enable-installer"


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    pdf_path: Path | None
    log_tail: str


def compile_tex(latex: str, workdir: Path, xelatex: str,
                timeout: int = DEFAULT_TIMEOUT) -> BuildResult:
    """Write guide.tex into `workdir` and build it with xelatex (two passes)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    binary = _resolve(xelatex)
    if not binary:
        return BuildResult(False, None, f"xelatex not found: {xelatex!r} "
                                        "(set study_guides_xelatex)")
    tex = workdir / TEX_NAME
    pdf = tex.with_suffix(".pdf")
    tex.write_text(latex, encoding="utf-8")
    _remove_stale(tex)
    problem = unsafe_latex(latex)
    if problem:
        log.warning("study guides: refused to compile %s: %s", tex, problem)
        return BuildResult(False, None, f"refused before compiling: {problem}. "
                                        "Remove it; the guide must be self-contained.")
    cmd = [binary, *_XELATEX_FLAGS, *_distribution_flags(binary), TEX_NAME]
    started = time.monotonic()
    for n in range(1, XELATEX_PASSES + 1):
        code, output = _run_pass(cmd, workdir, timeout)
        written = files_written_unexpected(tex.with_suffix(".fls"), workdir)
        if written:
            for name in written:
                stray = Path(name)
                if stray.is_file() and _inside(stray.resolve(), workdir.resolve()):
                    stray.unlink(missing_ok=True)
            pdf.unlink(missing_ok=True)
            log.warning("study guides: xelatex wrote %d unexpected file(s); build refused",
                        len(written))
            return BuildResult(False, None, "refused: the document wrote a file other than "
                                            f"its own output ({Path(written[0]).name})")
        outside = files_read_outside(tex.with_suffix(".fls"), workdir, binary)
        if outside:
            pdf.unlink(missing_ok=True)
            log.warning("study guides: xelatex read %d file(s) outside the build folder "
                        "and the TeX tree; build refused", len(outside))
            return BuildResult(False, None, "refused: the document read a file outside "
                                            f"its folder ({Path(outside[0]).name})")
        if code != 0:
            tail = _log_tail(tex.with_suffix(".log"), output)
            log.warning("study guides: xelatex pass %d failed (exit %s) in %s", n, code, workdir)
            return BuildResult(False, None, tail)
    if not pdf.is_file():
        return BuildResult(False, None, _log_tail(tex.with_suffix(".log"), "no PDF produced"))
    from .pdf_scan import pdf_problems
    problems = pdf_problems(pdf)
    if problems:
        pdf.unlink(missing_ok=True)
        log.warning("study guides: built PDF refused: %s", "; ".join(problems[:5]))
        return BuildResult(False, None, f"refused after building: {problems[0]}. The guide "
                                        "must be a plain document with no active content.")
    log.info("study guides: built %s in %.0fs", pdf, time.monotonic() - started)
    tail = _log_tail(tex.with_suffix(".log"), "")
    # The log and recorder list hold local paths (with the user name), and the
    # review model reads this folder: they are not needed once the PDF exists.
    for suffix in (".log", ".fls"):
        tex.with_suffix(suffix).unlink(missing_ok=True)
    return BuildResult(True, pdf, tail)


def build_with_repair(latex: str, workdir: Path, xelatex: str,
                      repair_fn: Callable[[str, str], str],
                      max_repairs: int = DEFAULT_MAX_REPAIRS) -> tuple[BuildResult, str]:
    """Build; on failure hand (latex, log_tail) to `repair_fn` for a fixed full
    document and try again, at most `max_repairs` times.

    Returns the last BuildResult and the LaTeX it was built from. A repair_fn
    that raises ends the loop with the last failed result — the exception is
    logged, since the course is about to be marked failed for it.
    """
    workdir = Path(workdir)
    result = compile_tex(latex, workdir, xelatex)
    for attempt in range(1, max_repairs + 1):
        if result.ok:
            break
        _keep_failed_attempt(workdir, attempt)
        log.info("study guides: build failed, repair %d/%d", attempt, max_repairs)
        try:
            latex = repair_fn(latex, result.log_tail)
        except Exception as exc:  # noqa: BLE001 — any repair failure ends the loop
            log.warning("study guides: repair %d failed: %s", attempt, exc)
            return result, latex
        result = compile_tex(latex, workdir, xelatex)
    if not result.ok:
        log.warning("study guides: build still failing after %d repairs", max_repairs)
    return result, latex


def _resolve(xelatex: str) -> str:
    if not xelatex:
        return ""
    if Path(xelatex).is_file():
        return str(xelatex)
    return shutil.which(xelatex) or ""


def unsafe_latex(latex: str) -> str:
    """Why this document must not be compiled, or "" when it may be."""
    from .latex_allowlist import allowlist_problem
    problem = allowlist_problem(latex)
    if problem:
        return problem
    forbidden = _FORBIDDEN.search(latex)
    if forbidden:
        return f"{forbidden.group(0)} is not allowed"
    if _FORM_ENV.search(latex):
        return "a PDF form is not allowed"
    nested = _NESTED_FILE_ARG.search(latex)
    if nested:
        return f"\\{nested.group(1)} with a nested-brace file name is not allowed"
    package = _PACKAGE_PATH.search(latex)
    if package:
        return f"{package.group(0)!r} loads a file by path"
    for link in _LAUNCH_LINK.finditer(latex):
        if link.group(1).lower() != "run" or not _GUIDE_LINK.match(link.group(2).strip()):
            return f"the link {link.group(0)[:80]!r} would launch a program or open a local file"
    for match in _FILE_COMMANDS.finditer(latex):
        for arg in _BRACE_ARG.findall(match.group(2)):
            if any(_UNSAFE_PATH.search(part) for part in arg.split(",")):
                return (f"\\{match.group(1)} names {arg.strip()!r}, "
                        f"outside the working directory")
    for match in _BARE_INPUT.finditer(latex):
        if _UNSAFE_PATH.search(match.group(1)):
            return f"\\input names {match.group(1)!r}, outside the working directory"
    return ""


def _remove_stale(tex: Path) -> None:
    """Delete the previous attempt's PDF and auxiliary files (never guide.tex)."""
    for suffix in _STALE_SUFFIXES:
        stale = tex.with_suffix(suffix)
        try:
            stale.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("study guides: could not remove stale %s: %s", stale.name, exc)


def _distribution_flags(binary: str) -> tuple[str, ...]:
    if "miktex" in binary.lower():
        return (_MIKTEX_NO_SHELL, _MIKTEX_INSTALLER_FLAG)
    return (_TEXLIVE_NO_SHELL,)


def _run_pass(cmd: list[str], workdir: Path, timeout: int) -> tuple[int | None, str]:
    """One xelatex pass: (exit code, console output). A timeout kills the whole
    process tree (MiKTeX starts helpers) and reads as a failure."""
    # Console output is discarded (guide.log has it all) so a runaway \typeout
    # loop cannot fill this process's memory; the log itself is size-capped.
    log_path = workdir / "guide.log"
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(workdir), env=child_env(_TEX_ENV),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return None, f"could not start xelatex: {exc}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            return proc.wait(timeout=_POLL_SEC), ""
        except subprocess.TimeoutExpired:
            pass
        try:
            too_big = log_path.stat().st_size > MAX_TEX_LOG_BYTES
        except OSError:
            too_big = False
        if too_big or time.monotonic() > deadline:
            _kill_tree(proc)
            proc.wait()
            if too_big:
                return None, f"xelatex log passed {MAX_TEX_LOG_BYTES // (1024 * 1024)} MB; stopped"
            return None, f"xelatex timed out after {timeout}s"


def _log_tail(log_path: Path, fallback: str) -> str:
    """The last LOG_TAIL_LINES lines of the xelatex log, or of `fallback`
    (console output / a reason) when no log was written. Reads only the end
    of the file: a runaway \typeout loop can make the log huge."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        text = fallback
    return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])


def _keep_failed_attempt(workdir: Path, attempt: int) -> None:
    """Copy guide.tex/.log aside before a repair overwrites them, into a sibling
    folder: the build folder is where the review model may read, and a log
    holds local paths."""
    keep = workdir.parent / f"{workdir.name}.attempts"
    for suffix in (".tex", ".log"):
        src = workdir / f"guide{suffix}"
        if not src.is_file():
            continue
        try:
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, keep / f"guide.attempt{attempt}{suffix}")
        except OSError as exc:
            log.warning("study guides: could not keep %s: %s", src.name, exc)


# ---- what xelatex actually opened ---------------------------------------------
# The regexes above are a first filter; model-written LaTeX can hide a file
# read from any regex. `-recorder` makes xelatex list every file it opened. A
# build that read anything outside its folder and the TeX/font trees, or wrote
# anything but guide.* in its folder, is refused.

LOG_TAIL_BYTES = 64 * 1024
MAX_TEX_LOG_BYTES = 50 * 1024 * 1024    # a real guide logs well under 1 MB
_POLL_SEC = 0.5
# The TeX trees, as the distribution itself reports them (MiKTeX and TeX Live).
_KPSE_VARS = ("TEXMFROOT", "TEXMFDIST", "TEXMFMAIN", "TEXMFLOCAL", "TEXMFVAR",
              "TEXMFCONFIG", "TEXMFSYSVAR", "TEXMFSYSCONFIG", "TEXMFHOME")
# What a guide build may write: guide.<ext> in its own folder, nothing that an
# agent, shell or editor would pick up as configuration or code.
_OUTPUT_NAME = re.compile(r"^guide\.[A-Za-z0-9]{1,8}$")
_OUTPUT_BAD_EXT = frozenset({"md", "json", "toml", "yaml", "yml", "py", "ps1", "bat", "cmd",
                             "exe", "js", "sh", "lnk", "dll", "tex", "sty", "cls", "cfg"})


def _kpse_paths(binary: str) -> list[Path]:
    exe = Path(binary)
    kpse = next((str(c) for c in (exe.with_name("kpsewhich.exe"), exe.with_name("kpsewhich"))
                 if c.is_file()), shutil.which("kpsewhich") or "")
    if not kpse:
        return []
    out: list[Path] = []
    for var in _KPSE_VARS:
        try:
            done = subprocess.run([kpse, f"-var-value={var}"], capture_output=True, text=True,
                                  timeout=30, env=child_env(),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        for part in re.split(r"[;,{}]|(?<!^[A-Za-z]):(?![\/])", done.stdout.strip()):
            part = part.strip().lstrip("!")
            if part and "$" not in part:
                out.append(Path(part).expanduser())
    return out


def _tex_roots(binary: str, workdir: Path) -> list[Path]:
    candidates = list(_kpse_paths(binary))
    for var in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):     # MiKTeX fallback
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "MiKTeX")
            if var == "LOCALAPPDATA":
                candidates.append(Path(base) / "Programs" / "MiKTeX")
                candidates.append(Path(base) / "Microsoft" / "Windows" / "Fonts")
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        candidates.append(Path(windir) / "Fonts")
    candidates += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path("/etc/fonts"),
                   Path("/Library/Fonts"), Path("/System/Library/Fonts"),
                   Path.home() / ".fonts", Path.home() / "Library" / "Fonts"]
    home = Path.home().resolve()
    roots: list[Path] = []
    for cand in candidates:
        try:
            root = cand.resolve()
        except OSError:
            continue
        # Never a drive root, the home folder itself, or anything holding the build.
        if root.parent == root or root == home or _inside(workdir, root):
            continue
        roots.append(root)
    return roots


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _recorded(fls: Path, kind: str) -> list[Path | str]:
    """Resolved paths of the INPUT or OUTPUT lines of a recorder file."""
    try:
        lines = fls.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    pwd = fls.parent
    out: list[Path | str] = []
    for line in lines:
        if line.startswith("PWD "):
            pwd = Path(line[4:].strip())
        elif line.startswith(kind + " "):
            raw = line[len(kind) + 1:].strip()
            try:
                out.append((pwd / raw).resolve())      # an absolute `raw` replaces pwd
            except (OSError, ValueError):
                out.append(raw)
    return out


def files_read_outside(fls: Path, workdir: Path, binary: str) -> list[str]:
    """INPUT files neither in `workdir` nor in a TeX/font tree."""
    workdir = Path(workdir).resolve()
    inputs = _recorded(fls, "INPUT")
    if not inputs:
        return []
    roots = _tex_roots(binary, workdir)
    return [str(p) for p in inputs
            if not isinstance(p, Path)
            or not (_inside(p, workdir) or any(_inside(p, r) for r in roots))]


def files_written_unexpected(fls: Path, workdir: Path) -> list[str]:
    """OUTPUT files other than guide.<ext> directly in `workdir`."""
    workdir = Path(workdir).resolve()
    bad = []
    for p in _recorded(fls, "OUTPUT"):
        ok = (isinstance(p, Path) and p.parent == workdir and _OUTPUT_NAME.match(p.name)
              and p.suffix.lstrip(".").lower() not in _OUTPUT_BAD_EXT)
        if not ok:
            bad.append(str(p))
    return bad
