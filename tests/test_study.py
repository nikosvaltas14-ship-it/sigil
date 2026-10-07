"""Offline tests for the study pipeline.

Moodle is an httpx.MockTransport over the synthetic fixtures in
sigil/study_guides/fixtures/, the claude CLI is a fake, and every path the
pipeline writes is redirected into a temporary directory. Nothing here touches
the network, Telegram, iCloud or a real account. Only `study_pipeline` runs a
real xelatex, and skips that part when xelatex is not installed.
"""
# ruff: noqa
import pytest


# ---- study guides (sigil/study_guides) --------------------------------
# All offline: Moodle is httpx.MockTransport over the recorded-shape
# fixtures in sigil/study_guides/fixtures/, the claude CLI is a fake, and
# every path the pipeline writes (runs.json, caches, lock, build dir, the
# vault) is redirected into a TemporaryDirectory. Only the last pipeline
# check runs something real: xelatex, skipped when it is not installed.

_SG_BASE = "https://elearning.auth.gr"
_SG_TOKEN = "0123456789abcdef0123456789abcdef"  # fake, for tests  gitleaks:allow  pragma: allowlist secret
_SG_TOPIC = "Θεωρήματα Thevenin και Norton"

def _sg_approve_all(latex, **_k):
    """A review gate that approves whatever it is shown."""
    return True, latex

def _sg_cfg(tmp, **over):
    """A config dict for the pipeline: circuits2 on Tuesdays, temp vault."""
    from pathlib import Path
    vault = Path(tmp) / "vault"
    (vault / "03 Resources" / "University").mkdir(parents=True, exist_ok=True)
    cfg = {
        "vault_path": str(vault), "allow_vault_writes": True,
        "study_guides_semester_start": "2026-09-28",
        "study_guides_semester_end": "2027-01-14",
        "study_guides_calendar_labs": False,   # never the real iCloud cache
        "study_guides_courses": [{"key": "circuits2", "moodle_course_id": 18431}],
        "study_guides_timetable": [{"weekday": "TU", "start": "09:00",
                                    "end": "11:00", "course": "circuits2",
                                    "type": "Θ"}],
    }
    cfg.update(over)
    return cfg

def _sg_sandbox(tmp):
    """Point every path run.py/material.py write to into `tmp`."""
    import contextlib
    import functools
    import importlib
    from pathlib import Path
    from unittest import mock
    from sigil.study_guides import material, settings, state
    run_mod = importlib.import_module("sigil.study_guides.run")
    tmp = Path(tmp)
    stack = contextlib.ExitStack()
    for name, value in (
            ("WORK_DIR", tmp), ("BUILD_DIR", tmp / "build"),
            ("DISCOVER_DUMP_DIR", tmp / "discover"),
            ("RunState", lambda: state.RunState(tmp / "runs.json")),
            ("JsonCache", lambda p: state.JsonCache(tmp / Path(p).name)),
            ("run_lock", lambda: state.run_lock(tmp / "run.lock")),
            ("load_settings", functools.partial(
                settings.load_settings, course_ids_path=tmp / "course_ids.json"))):
        stack.enter_context(mock.patch.object(run_mod, name, value))
    stack.enter_context(mock.patch.object(material, "CACHE_DIR", tmp / "cache"))
    return stack

def _sg_pdf_bytes():
    """A small real PDF with enough extractable text not to look scanned."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(12):
        page.insert_text((50, 60 + 20 * i),
                         f"Thevenin Norton equivalent circuit, line {i}: "
                         f"open-circuit voltage and short-circuit current.")
    data = doc.tobytes()
    doc.close()
    return data

def _sg_client(calls=None, token=_SG_TOKEN):
    """A real MoodleClient whose transport serves the fixtures; the site
    only accepts `_SG_TOKEN`, so any other `token` is rejected."""
    import json
    from pathlib import Path
    from urllib.parse import parse_qs
    import httpx
    from sigil.study_guides import moodle
    fixtures = Path("sigil/study_guides/fixtures")
    pdf = _sg_pdf_bytes()

    def fx(name):
        return json.loads((fixtures / name).read_text(encoding="utf-8"))

    def handler(request):
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/webservice/rest/server.php":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("wstoken") != _SG_TOKEN:
                return httpx.Response(200, json=fx("error_invalidtoken.json"))
            fn = form["wsfunction"]
            if fn == "core_webservice_get_site_info":
                return httpx.Response(200, json=fx("site_info.json"))
            if fn == "core_enrol_get_users_courses":
                return httpx.Response(200, json=fx("user_courses.json"))
            if fn == "core_course_get_contents":
                return httpx.Response(
                    200, json=fx(f"course_contents_{form['courseid']}.json"))
        if path.startswith("/webservice/pluginfile.php/"):
            if request.url.params.get("token") != _SG_TOKEN:
                return httpx.Response(200, json=fx("error_invalidtoken.json"))
            return httpx.Response(200, content=pdf,
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    return moodle.MoodleClient(_SG_BASE, token, delay=0, backoff=0,
                               transport=httpx.MockTransport(handler),
                               token_source=lambda: None)

def _sg_ask_json(prompt, schema, model):
    """The helper model: a schedule for the circuits page, nothing else."""
    if "entries" in (schema.get("properties") or {}):
        return {"entries": [
            {"date": "2026-10-06", "session_type": "Θ", "topic": "Ανάλυση κόμβων",
             "section_ids": [95003], "module_ids": [801020]},
            {"date": "2026-10-13", "session_type": "Θ", "topic": _SG_TOPIC,
             "section_ids": [95004], "module_ids": [801030]},
            {"date": "2026-10-15", "session_type": "Α",
             "topic": "Ασκήσεις σε Thevenin/Norton",
             "section_ids": [95004], "module_ids": []},
            {"date": "2026-10-20", "session_type": "Θ",
             "topic": "Μεταβατική απόκριση κυκλωμάτων RC και RL",
             "section_ids": [95005], "module_ids": [801040]}]}
    return {}

def study_calendar_labs_check():
    """Labs come from the calendar cache when it covers the day: no lab the
    timetable lists but the calendar lacks, a lab only the calendar has, the
    alias and accent-folded title matching, and a fall-back to the timetable
    outside the downloaded window or without a cache."""
    import json
    import tempfile
    from datetime import date
    from pathlib import Path
    from sigil.study_guides import icloud_calendar as ic
    from sigil.study_guides.settings import load_settings
    from sigil.study_guides.timetable import todays_sessions

    def event(title, start, end, all_day=False):
        return {"title": title, "location": "", "start": start, "end": end,
                "calendar": "t", "all_day": all_day}

    with tempfile.TemporaryDirectory() as tmp:
        cfg = _sg_cfg(
            tmp, study_guides_calendar_labs=True,
            study_guides_courses=[{"key": "circuits2", "moodle_course_id": 1},
                                  {"key": "electronics1", "moodle_course_id": 2}],
            study_guides_calendar_aliases={"Ηλεκτρονικών Κυκλωμάτων 2": "circuits2"},
            study_guides_timetable=[
                {"weekday": "MO", "start": "16:00", "end": "20:00",
                 "course": "circuits2", "type": "lab"},
                {"weekday": "MO", "start": "09:00", "end": "11:00",
                 "course": "circuits2", "type": "Θ"}])
        s = load_settings(cfg, course_ids_path=Path(tmp) / "none.json")
        saved = (ic.CACHE_PATH, dict(ic._memo))
        ic.CACHE_PATH = Path(tmp) / "calendar_cache.json"
        ic._memo.update(mtime=None, data=None)
        try:
            mon5, mon12 = date(2026, 10, 5), date(2026, 10, 12)
            if [x.type for x in todays_sessions(s, mon5)] != ["Θ", "lab"]:
                raise RuntimeError("no cache must keep the timetable's labs")
            events = [
                event("Εργαστήριο Ηλεκτρονικών Κυκλωμάτων 2 - Άσκηση 1",
                      "2026-10-12T18:00", "2026-10-12T19:30"),
                event("ΗΛΕΚΤΡΟΝΙΚΗ Ι (Εργαστήριο) - Τμήμα 04 Ομάδα 12",
                      "2026-10-16T15:00", "2026-10-16T18:00"),
                event("Ηλεκτρονική Ι (Θ)", "2026-10-13T09:00", "2026-10-13T11:00"),
                event("Εργαστήριο", "2026-10-14T09:00", "2026-10-14T10:00"),
                event("Εργαστήριο Ηλεκτρονικών Κυκλωμάτων 2", "2026-10-15",
                      "2026-10-16", all_day=True)]
            (Path(tmp) / "calendar_cache.json").write_text(json.dumps(
                {"fetched": "2026-10-07T12:00:00", "from": "2026-09-23", "to": "2026-12-06",
                 "events": events}), encoding="utf-8")
            ic._memo.update(mtime=None, data=None)
            if [x.type for x in todays_sessions(s, mon5)] != ["Θ"]:
                raise RuntimeError("a lab the calendar lacks must be dropped")
            lab = [x for x in todays_sessions(s, mon12) if x.type == "lab"]
            if [(x.course_key, x.start, x.end) for x in lab] != [("circuits2", "18:00", "19:30")]:
                raise RuntimeError(f"calendar lab not used: {lab}")
            fri = todays_sessions(s, date(2026, 10, 16))
            if [(x.course_key, x.start) for x in fri] != [("electronics1", "15:00")]:
                raise RuntimeError(f"folded title / calendar-only lab: {fri}")
            if todays_sessions(s, date(2026, 10, 14)) or todays_sessions(s, date(2026, 10, 15)):
                raise RuntimeError("a title with no course, or an all-day event, made a lab")
            if not any(x.type == "lab" for x in todays_sessions(s, date(2026, 12, 14))):
                raise RuntimeError("outside the window the timetable's labs must apply")
        finally:
            ic.CACHE_PATH = saved[0]
            ic._memo.update(saved[1])
    return "calendar labs replace the timetable's; fall-back and title matching ok"

def study_timetable_check():
    """Weekday mapping, the semester window, no_class_dates, labs and
    tutorials — and an empty timetable is a quiet 'no', not an error."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from sigil.study_guides.models import Session
    from sigil.study_guides.settings import load_settings
    from sigil.study_guides.timetable import (has_class, no_class_reason,
                                              session_decision, todays_sessions)
    with tempfile.TemporaryDirectory() as tmp:
        ids = Path(tmp) / "none.json"
        cfg = _sg_cfg(tmp, study_guides_no_class_dates=["2026-12-22..2027-01-06"],
                      study_guides_timetable=[
                          {"weekday": "TU", "start": "11:00", "end": "13:00",
                           "course": "circuits2", "type": "Α"},
                          {"weekday": "TU", "start": "09:00", "end": "11:00",
                           "course": "circuits2", "type": "Θ"},
                          {"weekday": "WE", "start": "15:00", "end": "17:00",
                           "course": "circuits2", "type": "lab"},
                          {"weekday": "XX", "start": "09:00", "end": "10:00",
                           "course": "circuits2", "type": "Θ"}])
        s = load_settings(cfg, course_ids_path=ids)
        tue = todays_sessions(s, date(2026, 10, 13))
        if [x.start for x in tue] != ["09:00", "11:00"]:
            raise RuntimeError(f"Tuesday slots wrong or unsorted: {tue}")
        for label, day in (("Saturday", date(2026, 10, 17)),
                           ("before the semester", date(2026, 9, 22)),
                           ("after the semester", date(2027, 1, 19)),
                           ("the Christmas break", date(2026, 12, 29))):
            if todays_sessions(s, day) or not no_class_reason(s, day):
                raise RuntimeError(f"{label} counted as a class day")
        if s.skip_labs:
            raise RuntimeError("labs must get guides by default (skip_labs False)")
        lab = Session("circuits2", "WE", "15:00", "17:00", "lab")
        tut = Session("circuits2", "TU", "11:00", "13:00", "Α")
        if not session_decision(lab, s, False)[0]:
            raise RuntimeError("a lab was skipped with skip_labs off")
        s_nolab = load_settings({**cfg, "study_guides_skip_labs": True},
                                course_ids_path=ids)
        if session_decision(lab, s_nolab, True)[0]:
            raise RuntimeError("a lab got a guide with skip_labs on")
        if session_decision(tut, s, False)[0] or not session_decision(tut, s, True)[0]:
            raise RuntimeError("a tutorial must need scheduled content")
        empty = load_settings({**cfg, "study_guides_timetable": []}, course_ids_path=ids)
        if has_class(empty, date(2026, 10, 13)):
            raise RuntimeError("an empty timetable reported a class")
        if "timetable" not in no_class_reason(empty, date(2026, 10, 13)):
            raise RuntimeError("an empty timetable did not say so")
    return "weekdays, semester window, breaks, labs on, tutorials need content, empty timetable"


def study_schedule_check():
    """The helper model's schedule JSON is validated, never trusted."""
    from datetime import date
    from sigil.study_guides.schedule_parser import (ScheduleError, entry_for,
                                                    infer_next, validate_schedule)
    start, end = date(2026, 10, 1), date(2027, 1, 14)
    raw = ('Here it is:\n```json\n{"entries": ['
           '{"date": "2026-10-13", "session_type": "Θ", "topic": "Thevenin", "extra": 1},'
           '{"date": "2026-10-15", "session_type": "A", "topic": "Ασκήσεις"},'
           '{"date": "13/10/2026", "session_type": "Θ", "topic": "bad date"},'
           '{"date": "2026-02-30", "session_type": "Θ", "topic": "no such day"},'
           '{"date": "2026-03-10", "session_type": "Θ", "topic": "out of semester"},'
           '{"date": "2026-10-20", "session_type": "seminar", "topic": "bad type"},'
           '{"date": "2026-10-22", "session_type": null, "topic": "  "},'
           '{"date": "2026-10-27", "session_type": null, "topic": "Laplace"}'
           ']}\n```')
    entries = validate_schedule(raw, start, end)
    if [e.topic for e in entries] != ["Thevenin", "Ασκήσεις", "Laplace"]:
        raise RuntimeError(f"wrong entries kept: {entries}")
    if entries[1].session_type != "Α":
        raise RuntimeError("a Latin 'A' was not read as the tutorial type")
    if entry_for(entries, "2026-10-13", "Θ").topic != "Thevenin":
        raise RuntimeError("entry_for missed the exact match")
    if entry_for(entries, "2026-10-13", "Α") is not None:
        raise RuntimeError("entry_for matched a different session type")
    if entry_for(entries, "2026-10-27", "Θ").topic != "Laplace":
        raise RuntimeError("an untyped entry did not match")
    nxt = infer_next(entries, "2026-10-29", ["thevenin", "Ασκήσεις"])
    if nxt is None or nxt.topic != "Laplace":
        raise RuntimeError(f"infer_next picked {nxt}")
    # Past-dated topics count as taught even with no guide in runs.json.
    sem = validate_schedule([{"date": d, "topic": t} for d, t in (
        ("2026-10-01", "T1"), ("2026-10-08", "T2"), ("2026-10-22", "T4"))], start, end)
    nxt = infer_next(sem, "2026-10-15", [])
    if nxt is None or nxt.topic != "T4":
        raise RuntimeError(f"infer_next went back to a past topic: {nxt}")
    # A week-level entry (dated to the Monday) covers the rest of its week.
    weekly = validate_schedule([{"date": "2026-10-12", "session_type": None,
                                 "topic": "Εβδομάδα 3"}], start, end)
    if (entry_for(weekly, "2026-10-14", "Α") or entry_for(weekly, "2026-10-14", "Θ")) is None:
        raise RuntimeError("a week-level entry did not cover a Wednesday class")
    if entry_for(weekly, "2026-10-19", "Θ") is not None:
        raise RuntimeError("a week-level entry leaked into the next week")
    if validate_schedule([{"date": "2026-10-13", "topic": "x"}], start, end)[0].topic != "x":
        raise RuntimeError("a bare list was refused")
    for bad in ("not json", "[]", '{"entries": [{"date": "x", "topic": "y"}]}'):
        try:
            validate_schedule(bad, start, end)
        except ScheduleError:
            continue
        raise RuntimeError(f"{bad!r} did not raise ScheduleError")
    return "fenced JSON, bad dates/types/topics dropped, extra keys ignored, lookups"


def study_names_latex_check():
    """File-name classifiers need context; model LaTeX cannot reach outside."""
    from sigil.study_guides.build import unsafe_latex
    from sigil.study_guides.material import is_past_exam_name, is_schedule_name
    for name in ("Προγραμματισμός σε C.pdf", "Εισαγωγή στον προγραμματισμό.pdf"):
        if is_schedule_name(name):
            raise RuntimeError(f"{name!r} taken for a schedule")
    for name in ("Ειδικά θέματα δένδρων.pdf", "Εξέταση ευστάθειας.pdf", "examples.pdf"):
        if is_past_exam_name(name):
            raise RuntimeError(f"{name!r} taken for a past paper")
    if not (is_schedule_name("Πρόγραμμα μαθημάτων.pdf") and is_schedule_name("syllabus.pdf")):
        raise RuntimeError("a real schedule file was missed")
    for name in ("Παλαιά θέματα.pdf", "c_sos_themata_2025.pdf", "Εξετάσεις Ιουνίου.pdf"):
        if not is_past_exam_name(name):
            raise RuntimeError(f"{name!r} not taken for a past paper")
    for bad in (r"\input{../../moodle_token.txt}", r"\verbatiminput{C:/x.txt}",
                r"\immediate\write18{dir}", r"\openin1=x"):
        if not unsafe_latex(bad):
            raise RuntimeError(f"{bad!r} was allowed")
    if unsafe_latex(r"\textbf{Θεώρημα} $\frac{a}{b}$ \url{https://a/../b}"):
        raise RuntimeError("safe LaTeX refused")
    # Guides are self-contained: even harmless-looking file commands are refused.
    for bad in (r"\input{part}", r"\includegraphics{scan.pdf}"):
        if not unsafe_latex(bad):
            raise RuntimeError(f"{bad!r} was allowed")
    return "names need exam/schedule context; outside paths and shell refused"


def study_filing_check():
    """Windows-safe names, never overwrite, never leave the vault."""
    import tempfile
    from pathlib import Path
    from sigil.study_guides.filing import MAX_PATH_CHARS, file_pdf, safe_filename
    name = safe_filename("2026-10-13", "Electrical Circuits II",
                         'Κεφ. 3: Thévenin/Norton? a*b "c" <d> |e\x07')
    if any(ch in name for ch in ':/\\?*"<>|\x07'):
        raise RuntimeError(f"unsafe characters survived: {name!r}")
    if not name.startswith("2026-10-13 - Electrical Circuits II - Κεφ. 3") \
            or not name.endswith(".pdf"):
        raise RuntimeError(f"bad name shape: {name!r}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "University"
        root.mkdir()
        src = Path(tmp) / "guide.pdf"
        src.write_bytes(b"%PDF-1.4 first")
        first = file_pdf(src, root, "Electrical Circuits II", name)
        first.write_bytes(b"%PDF-1.4 edited in the vault")
        src.write_bytes(b"%PDF-1.4 second")
        second = file_pdf(src, root, "Electrical Circuits II", name)
        third = file_pdf(src, root, "Electrical Circuits II", name)
        if first.read_bytes() != b"%PDF-1.4 edited in the vault":
            raise RuntimeError("an existing vault PDF was overwritten")
        if not (second.name.endswith(" (2).pdf") and third.name.endswith(" (3).pdf")):
            raise RuntimeError(f"collision names wrong: {second.name}, {third.name}")
        for folder, fname in (("..", name), ("../outside", name),
                              (str(Path(tmp).resolve()), name),
                              ("Electrical Circuits II", "../x.pdf"),
                              ("Electrical Circuits II", "CON.pdf")):
            try:
                file_pdf(src, root, folder, fname)
            except ValueError:
                continue
            raise RuntimeError(f"traversal not refused: {folder!r} / {fname!r}")
        long_name = safe_filename("2026-10-13", "Electrical Circuits II", "λ" * 400)
        capped = file_pdf(src, root, "Electrical Circuits II", long_name)
        if len(str(capped)) > MAX_PATH_CHARS:
            raise RuntimeError(f"path not capped: {len(str(capped))} chars")
    return "sanitised Greek name, (2)/(3), no overwrite, traversal refused, path capped"


def study_state_check():
    """A success is skipped next time; a failure is not; corrupt JSON is
    empty, not a crash; two runs never hold the lock at once."""
    import tempfile
    from pathlib import Path
    from sigil.study_guides.models import FAILED, OK, CourseResult
    from sigil.study_guides.state import JsonCache, LockBusy, RunState, run_lock
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        st = RunState(tmp / "runs.json")
        st.record(CourseResult("2026-10-13", "circuits2", FAILED, "boom"))
        if st.already_succeeded("2026-10-13", "circuits2"):
            raise RuntimeError("a failed run counted as done")
        st.record(CourseResult("2026-10-13", "circuits2", OK, topic="Thevenin",
                               files_used=("a.pdf",)))
        if not st.already_succeeded("2026-10-13", "circuits2"):
            raise RuntimeError("a successful run was not remembered")
        latest = st.for_date("2026-10-13")
        if len(latest) != 1 or latest[0]["status"] != OK:
            raise RuntimeError(f"for_date did not give the latest record: {latest}")
        if st.previous_topics("circuits2", "2026-10-20") != ["Thevenin"]:
            raise RuntimeError("previous_topics wrong")
        (tmp / "runs.json").write_text("{not json", encoding="utf-8")
        if RunState(tmp / "runs.json").already_succeeded("2026-10-13", "circuits2"):
            raise RuntimeError("a corrupt runs.json was not read as empty")
        cache = JsonCache(tmp / "cache.json")
        (tmp / "cache.json").write_text("[[[", encoding="utf-8")
        if cache.get("k") is not None:
            raise RuntimeError("a corrupt cache returned data")
        cache.put("k", {"a": (1, 2)})
        if JsonCache(tmp / "cache.json").get("k") != {"a": [1, 2]}:
            raise RuntimeError("cache round trip failed")
        with run_lock(tmp / "run.lock"):
            try:
                with run_lock(tmp / "run.lock"):
                    raise RuntimeError("a second run took a held lock")
            except LockBusy:
                pass
        if (tmp / "run.lock").exists():
            raise RuntimeError("the lock file outlived the run")
    return "idempotency, latest-per-course, corrupt JSON empty, lock exclusive"


def study_semester_check():
    """The shipped timetable/holidays load, and the end-of-semester notice
    fires once per semester_end, never during the semester."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from tests.sample import SAMPLE as DEFAULTS
    from sigil.study_guides import semester
    from sigil.study_guides.settings import load_settings
    from sigil.study_guides.timetable import todays_sessions
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        s = load_settings({**DEFAULTS, "vault_path": str(tmp)},
                          course_ids_path=tmp / "ids.json")
        if len(s.timetable) != len(DEFAULTS["study_guides_timetable"]):
            raise RuntimeError("the shipped timetable lost rows on load: "
                               f"{len(s.timetable)}")
        tue = {x.course_key for x in todays_sessions(s, date(2026, 10, 6))}
        if tue != {"circuits2", "electronics1"}:
            raise RuntimeError(f"Tuesday 2026-10-06 sessions wrong: {tue}")
        for off in (date(2026, 10, 28), date(2026, 12, 29), date(2027, 1, 6)):
            if todays_sessions(s, off):
                raise RuntimeError(f"{off} is a holiday but has sessions")
        marker = tmp / "notice.json"
        if semester.notice_due(s, date(2027, 1, 14), marker):
            raise RuntimeError("the notice fired on the semester's last day")
        if not semester.notice_due(s, date(2027, 1, 15), marker):
            raise RuntimeError("the notice did not fire after the semester")
        semester.mark_sent(s, marker)
        if semester.notice_due(s, date(2027, 1, 16), marker):
            raise RuntimeError("the notice repeated for the same semester")
        page = '<a href="/x">Νέα</a><a href="/study/orologio.pdf">Ωρολόγιο Πρόγραμμα</a>'
        url = semester.find_timetable_url(fetch=lambda _u: page)
        if url != "https://ece.auth.gr/study/orologio.pdf":
            raise RuntimeError(f"timetable link not found: {url!r}")

        def offline(_url):
            raise OSError("offline")
        if semester.find_timetable_url(fetch=offline) != "":
            raise RuntimeError("an unreachable site did not fall back to ''")
        if "couldn't find" not in semester.notice_text(s):
            raise RuntimeError("the no-link notice text is wrong")
        # Auto-discovery takes only this semester's pages, never last
        # year's namesake, and respects the Ι/ΙΙ numeral.
        import importlib
        sg_run = importlib.import_module("sigil.study_guides.run")
        if sg_run.semester_tag(s) != "2026/1":
            raise RuntimeError(f"semester tag wrong: {sg_run.semester_tag(s)!r}")

        class _Enrolled:
            def site_info(self):
                return {"userid": 1}

            def user_courses(self, _uid):
                return [{"id": 800, "fullname": "017 Δομές Δεδομένων - 2025/1"},
                        {"id": 900, "fullname": "017 Δομές Δεδομένων - 2026/1"},
                        {"id": 902, "fullname": "Ηλεκτρονική ΙΙ - 2026/1"}]
        ctx = sg_run._make_ctx(s, "2026-10-06", True, False, _Enrolled(),
                               lambda *a: None, None, None, None, None, None,
                               lambda _line: None)
        ids = {c.key: c.moodle_course_id
               for c in sg_run._auto_discover(ctx).settings.courses}
        if ids["datastructures"] != 900 or ids["electronics1"] != 0:
            raise RuntimeError(f"auto-discover picked wrong pages: {ids}")
    return (f"{len(s.timetable)} timetable rows, holidays skipped, "
            "notice once after semester_end, link best effort")


def study_moodle_fixtures():
    """The REST client and parsers over recorded-shape responses; the
    token never appears in an error or in the download log line."""
    import logging
    import tempfile
    from pathlib import Path
    from sigil.study_guides.moodle import InvalidToken, iter_files
    from sigil.study_guides.schedule_parser import collect_schedule_text
    calls = []
    client = _sg_client(calls)
    info = client.site_info()
    courses = client.user_courses(info["userid"])
    if len(courses) < 5:
        raise RuntimeError(f"only {len(courses)} enrolled courses parsed")
    contents = client.course_contents(18431)
    files = iter_files(contents)
    names = {f.filename for f in files}
    if "KII_Dialexi_03_Thevenin_Norton.pdf" not in names:
        raise RuntimeError(f"lecture file missing from {sorted(names)}")
    text = collect_schedule_text(contents)
    if "Thevenin" not in text or "Εβδομάδα 3" not in text:
        raise RuntimeError("the schedule text lost the section summaries")
    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec.getMessage())
    watched = [logging.getLogger("sigil.study_guides"), logging.getLogger("httpx")]
    levels = [lg.level for lg in watched]
    for lg in watched:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)   # every line the download could log
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ref = next(f for f in files if f.filename.endswith("Thevenin_Norton.pdf"))
            path = client.download(ref.fileurl, Path(tmp) / "l3.pdf")
            if not path.read_bytes().startswith(b"%PDF"):
                raise RuntimeError("download did not write the PDF")
    finally:
        for lg, level in zip(watched, levels):
            lg.removeHandler(handler)
            lg.setLevel(level)
    if not records:
        raise RuntimeError("the download logged nothing, so the redaction went unchecked")
    if any(_SG_TOKEN in r for r in records):
        raise RuntimeError("the Moodle token reached a log line")
    client.close()
    wrong = _sg_client(token="f" * 32)
    try:
        wrong.site_info()
        raise RuntimeError("a rejected token did not raise InvalidToken")
    except InvalidToken as exc:
        if "f" * 32 in str(exc):
            raise RuntimeError("the token leaked into the error text")
    finally:
        wrong.close()
    return (f"site info, {len(courses)} courses, {len(files)} files, schedule text, "
            f"download, no token in logs, invalid token raised")


def study_dry_run():
    """`run --dry-run` over the fixtures: topic from the schedule, the
    right lecture file, both prompts printed — and nothing generated,
    filed or recorded."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from sigil.study_guides import run as sg_run
    from sigil.study_guides.models import SKIPPED

    def no_generate(*a, **k):
        raise RuntimeError("a dry run called the generator")

    with tempfile.TemporaryDirectory() as tmp:
        out = []
        client = _sg_client()
        try:
            with _sg_sandbox(tmp):
                results_ = sg_run(_sg_cfg(tmp), day=date(2026, 10, 13), dry_run=True,
                                  client=client, ask_json=_sg_ask_json,
                                  generate_fn=no_generate, out=out.append)
        finally:
            client.close()
        if len(results_) != 1 or results_[0].status != SKIPPED:
            raise RuntimeError(f"dry run result: {results_}")
        r = results_[0]
        if r.topic != _SG_TOPIC or r.topic_inferred:
            raise RuntimeError(f"dry run topic {r.topic!r} (inferred={r.topic_inferred})")
        if "KII_Dialexi_03_Thevenin_Norton.pdf" not in r.files_used:
            raise RuntimeError(f"wrong files: {r.files_used}")
        printed = "\n".join(out)
        if "--- user prompt ---" not in printed or _SG_TOPIC not in printed:
            raise RuntimeError("the assembled prompt was not printed")
        if (Path(tmp) / "runs.json").exists():
            raise RuntimeError("a dry run recorded state")
        if any((Path(tmp) / "vault").rglob("*.pdf")):
            raise RuntimeError("a dry run filed a PDF")
    return f"topic '{r.topic}', {len(r.files_used)} file(s), prompts printed, nothing written"


def study_pipeline():
    """The real run path end to end: fake Claude, REAL xelatex, a temp
    vault. Filed once, skipped on a re-run, ' (2)' on --force."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from sigil.study_guides import run as sg_run
    from sigil.study_guides.build import BuildResult
    from sigil.study_guides.generate import CLIResult
    from sigil.study_guides.models import OK, SKIPPED
    from sigil.study_guides.settings import load_settings

    with tempfile.TemporaryDirectory() as tmp:
        xelatex = load_settings(_sg_cfg(tmp), course_ids_path=Path(tmp) / "x").xelatex
        if not xelatex or not Path(xelatex).is_file():
            return "skipped: no xelatex installed (set study_guides_xelatex)"
        prompts, verified = [], []

        def fake_generate(system, user, *, attachments_dir, settings):
            prompts.append(user)
            doc = ("\\documentclass{article}\n\\usepackage{fontspec}\n"
                   "\\setmainfont{DejaVu Serif}\n\\usepackage{polyglossia}\n"
                   "\\setdefaultlanguage{greek}\n\\begin{document}\n"
                   f"{_SG_TOPIC}: $V_{{th}} = 5\\,V$.\n\\end{{document}}\n")
            return CLIResult(text=f"Εδώ είναι:\n{doc}\nτέλος", cost_usd=0.5, raw={})

        def fake_verify(latex, *, workdir, settings):
            verified.append(latex)
            return latex

        def no_repair(latex, tail, *, workdir, settings):
            raise RuntimeError(f"the tiny document needed a repair: {tail[-300:]}")

        def fake_build(latex, workdir, xelatex_, repair_fn, max_repairs=2):
            pdf = Path(workdir) / "guide.pdf"
            pdf.write_bytes(b"%PDF-1.4 forced rebuild")
            return BuildResult(True, pdf, ""), latex

        cfg = _sg_cfg(tmp)
        kw = dict(ask_json=_sg_ask_json, generate_fn=fake_generate,
                  verify_fn=fake_verify, review_fn=_sg_approve_all, repair_fn=no_repair, out=lambda _l: None)
        client = _sg_client()
        try:
            with _sg_sandbox(tmp):
                first = sg_run(cfg, day=date(2026, 10, 13), client=client, **kw)
                again = sg_run(cfg, day=date(2026, 10, 13), client=client, **kw)
                forced = sg_run(cfg, day=date(2026, 10, 13), force=True, client=client,
                                build_fn=fake_build, **kw)
        finally:
            client.close()
        r = first[0]
        if r.status != OK:
            raise RuntimeError(f"pipeline did not succeed: {r.status} — {r.reason}")
        pdf = Path(r.pdf_path)
        folder = Path(tmp) / "vault" / "03 Resources" / "University" / "Electrical Circuits II"
        if pdf.parent != folder or not pdf.read_bytes().startswith(b"%PDF"):
            raise RuntimeError(f"PDF not filed in the course folder: {pdf}")
        if not pdf.name.startswith("2026-10-13 - Electrical Circuits II - Θεωρήματα"):
            raise RuntimeError(f"unexpected file name {pdf.name}")
        if not prompts or _SG_TOPIC not in prompts[0] or "Thevenin Norton" not in prompts[0]:
            raise RuntimeError("the prompt lacked the topic or the lecture text")
        if not verified:
            raise RuntimeError("the verify pass was skipped")
        if again[0].status != SKIPPED or "already" not in again[0].reason:
            raise RuntimeError(f"a re-run was not skipped: {again[0]}")
        if forced[0].status != OK or not forced[0].pdf_path.endswith(" (2).pdf"):
            raise RuntimeError(f"--force did not file a second copy: {forced[0]}")
        if pdf.read_bytes() == b"%PDF-1.4 forced rebuild":
            raise RuntimeError("--force overwrote the first PDF")
    return f"compiled with xelatex and filed '{pdf.name}'; re-run skipped; --force -> (2)"


def study_review_gate():
    """The Opus gate: approve, correct-then-approve, never-approves and a
    review that cannot run all behave, and the last two keep the guide off
    the shelf (they raise, so run.py never files)."""
    from pathlib import Path
    from types import SimpleNamespace
    from sigil.study_guides import generate as gen
    import sys
    sg_run = sys.modules["sigil.study_guides.run"]
    from sigil.study_guides.build import BuildResult

    doc = r"\documentclass{article}\begin{document}" + "x" * 100 + r"\end{document}" + "\n"
    fixed = doc.replace("x", "y")
    for answer, want in ((gen.REVIEW_APPROVED, (True, doc)), (fixed, (False, fixed))):
        if gen._parse_review(doc, answer) != want:
            raise RuntimeError(f"review answer {answer[:20]!r} parsed wrong")
    for bad in ("looks fine to me", r"\documentclass{a}\begin{document}\end{document}"):
        try:
            gen._parse_review(doc, bad)
        except gen.ClaudeCLIError:
            continue
        raise RuntimeError(f"a bad review answer was accepted: {bad[:30]!r}")

    def gate(review_fn, **over):
        builds = []

        def build(latex, workdir, xelatex_, repair_fn, max_repairs=2):
            builds.append(latex)
            return BuildResult(True, Path(workdir) / "guide.pdf", ""), latex

        settings = SimpleNamespace(review_pass=True, xelatex="", **over)
        ctx = SimpleNamespace(settings=settings, day_str="2026-10-07", review_fn=review_fn,
                              build_fn=build, repair_fn=lambda *a, **k: "")
        built = BuildResult(True, Path("guide.pdf"), "")
        course = SimpleNamespace(key="circuits2")
        return sg_run._review_gate(ctx, course, doc, built, Path(".")), builds

    (_, note), builds = gate(lambda latex, **_k: (True, latex))
    if "round 1" not in note or builds:
        raise RuntimeError(f"an approved guide was rebuilt or mis-noted: {note!r} {builds}")
    answers = iter([(False, fixed), (True, fixed)])
    (_, note), builds = gate(lambda latex, **_k: next(answers))
    if "round 2" not in note or builds != [fixed]:
        raise RuntimeError(f"a correction was not rebuilt then approved: {note!r}")
    for name, fn in (("never approves", lambda latex, **_k: (False, fixed)),
                     ("cannot run", lambda latex, **_k: (_ for _ in ()).throw(RuntimeError("usage")))):
        try:
            gate(fn)
        except sg_run._Fail:
            continue
        raise RuntimeError(f"a review that {name} still let the guide through")
    ctx_off = SimpleNamespace(settings=SimpleNamespace(review_pass=False))
    if sg_run._review_gate(ctx_off, None, doc, "built", Path("."))[1] != "off":
        raise RuntimeError("review_pass off should skip the gate")




def study_catchup_check():
    """The hourly check: only courses whose classes are all over, no
    course with a guide, none past the generation-failure cap, oldest first."""
    import tempfile
    from datetime import date, datetime
    from pathlib import Path
    from sigil.study_guides import catchup, load_settings as sg_load_settings
    from sigil.study_guides.models import CourseResult
    from sigil.study_guides.state import RunState

    cfg = {"study_guides_semester_start": "2026-10-01",
           "study_guides_semester_end": "2027-01-14",
           "study_guides_no_class_dates": [],
           "study_guides_timetable": [
               {"weekday": "TU", "start": "09:00", "end": "11:00",
                "course": "electronics1", "type": "lab"},
               {"weekday": "TU", "start": "11:00", "end": "13:00",
                "course": "circuits2", "type": "Θ"},
               {"weekday": "TU", "start": "16:00", "end": "18:00",
                "course": "electronics1", "type": "Α"},
               {"weekday": "WE", "start": "09:00", "end": "11:00",
                "course": "electronics1", "type": "Θ"},
               {"weekday": "WE", "start": "15:00", "end": "17:00",
                "course": "emfield1", "type": "Θ"}]}
    settings = sg_load_settings(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        state = RunState(Path(tmp) / "runs.json")
        # Tuesday 14:00: circuits is over, electronics still has its tutorial.
        got = catchup.due(settings, datetime(2026, 10, 6, 14, 0), 0, state)
        if [(d.day.isoformat(), d.course_key) for d in got] != [
                ("2026-10-06", "circuits2")]:
            raise RuntimeError(f"Tuesday 14:00: {got}")
        # Wednesday 12:00: Tuesday's two, then Wednesday's electronics only.
        state.record(CourseResult("2026-10-06", "circuits2", "ok", topic="x"))
        for _ in range(catchup.MAX_GENERATION_ATTEMPTS):
            state.record(CourseResult("2026-10-06", "electronics1", "failed",
                                      reason="LaTeX build failed",
                                      extra={"generation_attempted": True}))
        state.record(CourseResult("2026-10-07", "electronics1", "skipped",
                                  reason="no lecture material"))
        got = catchup.due(settings, datetime(2026, 10, 7, 12, 0), 3, state)
        pairs = [(d.day.isoformat(), d.course_key, d.last_reason) for d in got]
        if pairs != [("2026-10-07", "electronics1", "no lecture material")]:
            raise RuntimeError(f"Wednesday 12:00: {pairs}")
        # Cheap failures never use up the cap.
        state2 = RunState(Path(tmp) / "runs2.json")
        for _ in range(5):
            state2.record(CourseResult("2026-10-06", "electronics1", "failed",
                                       reason="schedule_missing"))
        got = catchup.due(settings, datetime(2026, 10, 7, 8, 0), 1, state2)
        if [d.course_key for d in got] != ["electronics1", "circuits2"]:
            raise RuntimeError(f"cheap failures capped: {got}")
        # Grace period: 13:00 lecture end is not due at 13:10.
        if catchup.due(settings, datetime(2026, 10, 6, 13, 10), 0,
                       RunState(Path(tmp) / "runs3.json")):
            raise RuntimeError("due before the grace period ended")
    return "finished classes only, done/capped skipped, oldest first"


# ---- deadlines + the Saturday weekly review ------------------------------
# Same rules as the study-guides checks above: Moodle is an
# httpx.MockTransport over the synthetic fixtures, every file lands in a
# TemporaryDirectory, Telegram and the vault are stubs, and no widget is
# built on a real SigilWindow (it would open the camera and microphone).

def _sd_server(drop=(), offline=False):
    """A fake Moodle timeline: filters by timesortfrom/to, pages by
    aftereventid/limitnum, and serves the assign fixtures."""
    import json
    from pathlib import Path
    from urllib.parse import parse_qs
    import httpx
    fixtures = Path("sigil/study_guides/fixtures")
    events = json.loads((fixtures / "action_events.json").read_text(encoding="utf-8"))["events"]
    calls = []

    def fx(name):
        return json.loads((fixtures / name).read_text(encoding="utf-8"))

    def handler(request):
        if offline:
            raise httpx.ConnectError("no route", request=request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append(form)
        if form.get("wstoken") != _SG_TOKEN:
            return httpx.Response(200, json=fx("error_invalidtoken.json"))
        fn = form["wsfunction"]
        if fn == "core_calendar_get_action_events_by_timesort":
            lo, hi = int(form["timesortfrom"]), int(form.get("timesortto") or 2**40)
            after, limit = int(form.get("aftereventid") or 0), int(form["limitnum"])
            evs = sorted((e for e in events if lo <= e["timesort"] <= hi
                          and e["instance"] not in drop),
                         key=lambda e: (e["timesort"], e["id"]))
            if after:
                evs = evs[next(i for i, e in enumerate(evs) if e["id"] == after) + 1:]
            page = evs[:limit]
            return httpx.Response(200, json={
                "events": page, "firstid": page[0]["id"] if page else 0,
                "lastid": page[-1]["id"] if page else 0})
        if fn == "mod_assign_get_assignments":
            return httpx.Response(200, json=fx("assign_assignments.json"))
        if fn == "mod_assign_get_submission_status":
            return httpx.Response(200, json=fx("assign_submission_status.json"))
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls



def study_deadlines_check():
    """Timeline paging, the window, warningcode 1, the cache, the floors,
    the submitted? inference, offline and check_submission."""
    import functools
    import logging
    import tempfile
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from unittest import mock
    from sigil.study_guides import assignments as A
    from sigil.study_guides import moodle
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    transport, calls = _sd_server()
    tl = "core_calendar_get_action_events_by_timesort"
    warned = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warned.append(record.getMessage())

    grab = _Grab()
    logging.getLogger("sigil.study_guides").addHandler(grab)
    try:
        with moodle.MoodleClient(_SG_BASE, _SG_TOKEN, delay=0, backoff=0,
                                 transport=transport, token_source=lambda: None) as c:
            items = A.fetch_deadlines(c, now, 7, 21, course_keys={18431: "circuits2"},
                                      page_size=3)
    finally:
        logging.getLogger("sigil.study_guides").removeHandler(grab)
    pages = [f for f in calls if f["wsfunction"] == tl]
    if len(pages) != 2 or "aftereventid" not in pages[1]:
        raise RuntimeError(f"expected two timeline pages, got {len(pages)}")
    if any(f.get("limittononsuspendedevents") != "1" for f in pages):
        raise RuntimeError("limittononsuspendedevents was not sent")
    cmids = [d.cmid for d in items]
    if len(cmids) != 5 or any(d.due_ts < now.timestamp() - 8 * 86400 for d in items):
        raise RuntimeError(f"the window let the April item in, or lost one: {cmids}")
    if not any(d.kind == "quiz" for d in items):
        raise RuntimeError("quizzes were dropped; they count as deadlines")
    by = {d.cmid: d for d in items}
    if by[810120].assign_id != 30101 or by[810120].course_key != "circuits2":
        raise RuntimeError("assign id / course key enrichment missing")
    if warned:
        raise RuntimeError(f"warningcode 1 was logged: {warned}")

    client = functools.partial(moodle.MoodleClient, delay=0, backoff=0,
                               token_source=lambda: None)
    cfg = {"study_guides_courses": [{"key": "circuits2", "moodle_course_id": 18431}]}
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(moodle, "load_token", lambda: _SG_TOKEN), \
            mock.patch.object(moodle, "MoodleClient", client):
        cache = Path(tmp) / "deadlines.json"
        snap = A.refresh(cfg, force=True, now=now, path=cache, transport=transport)
        if snap.error or len(snap.items) != 5 or A.cached(cache).items != snap.items:
            raise RuntimeError(f"refresh/cached disagree: {snap.error}")
        before = len(calls)
        A.refresh(cfg, force=True, now=now + timedelta(seconds=30), path=cache,
                  transport=transport)
        if len(calls) != before:
            raise RuntimeError("the 60 s forced floor did not hold")
        gone, _ = _sd_server(drop={810120})
        snap = A.refresh(cfg, force=True, now=now + timedelta(minutes=2), path=cache,
                         transport=gone)
        done = [d for d in snap.done if d.cmid == 810120]
        if not done or done[0].status != "submitted?":
            raise RuntimeError("a vanished assignment was not inferred submitted?")
        # A quiz that left the timeline after it closed was missed, not done.
        quiz = A.Deadline(9, 990, "quiz", "close", 18431, "C2", None, "Quiz",
                          int((now - timedelta(hours=1)).timestamp()), False, True, "u")
        _, qdone = A.infer_done({"items": [quiz.to_json()], "seen": {"990": quiz.due_ts}},
                                [], now, 7)
        if qdone:
            raise RuntimeError("a quiz that closed unattempted was inferred submitted?")
        off, _ = _sd_server(offline=True)
        snap = A.refresh(cfg, force=True, now=now + timedelta(minutes=4), path=cache,
                         transport=off)
        if not snap.is_offline or not snap.items:
            raise RuntimeError(f"offline lost the old items: {snap.error}")
        status = A.check_submission(cfg, 30155, path=cache, transport=transport)
        if status != "submitted":
            raise RuntimeError(f"check_submission said {status!r}")
        with mock.patch.object(moodle, "load_token", lambda: None):
            if A.refresh(cfg, force=True, now=now + timedelta(hours=1),
                         path=cache).error != "no_token":
                raise RuntimeError("a missing token was not reported as no_token")
    return "2 pages, April excluded, quizzes kept, warningcode 1 quiet, floor, submitted?, offline, check"


def study_weekly_check():
    """week_bounds, an empty week skips without a CLI call, and the study map."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from sigil.study_guides import studymap, weekly
    from sigil.study_guides.settings import load_settings
    if weekly.week_bounds(date(2026, 10, 3)) != (date(2026, 9, 28), date(2026, 10, 3)):
        raise RuntimeError(f"week_bounds wrong: {weekly.week_bounds(date(2026, 10, 3))}")
    if weekly.week_bounds(date(2026, 10, 4))[0] != date(2026, 9, 28):
        raise RuntimeError("Sunday did not map back to its week")
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings(_sg_cfg(tmp), course_ids_path=Path(tmp) / "ids.json")
        called = []

        def never(*a, **k):
            called.append(1)
            raise AssertionError("an empty week must not call the CLI")

        empty = lambda s, c, m: weekly.WeekFacts(  # noqa: E731
            week_start=m, week_end=weekly.week_bounds(m)[1], week_number=1)
        res = weekly.run_weekly(settings, {}, date(2026, 10, 3),
                                state_path=Path(tmp) / "weekly.json",
                                lock_path=Path(tmp) / "weekly.lock",
                                work_dir=Path(tmp) / "weekly",
                                gather_fn=empty, generate_fn=never)
        if res.status != "skipped" or "nothing" not in res.reason or called:
            raise RuntimeError(f"an empty week was not skipped: {res}")
        if weekly.already_done(date(2026, 9, 28), path=Path(tmp) / "weekly.json"):
            raise RuntimeError("a skipped week counted as done")
        rows = studymap.build(settings, None, [], date(2026, 10, 12))
        if not any(r.kind == "class" for r in rows) or rows != sorted(rows, key=lambda r: r.when):
            raise RuntimeError("the study map has no class rows or is unsorted")
    return "week_bounds(10-03) = 09-28..10-03; empty week skipped, no CLI; map sorted"








def study_ids_on_enrol_check():
    """The reminders find the new semester's Moodle ids without a class day."""
    import importlib
    import tempfile
    from datetime import datetime
    from pathlib import Path
    from unittest import mock
    from sigil.study_guides import exams, settings as sg_settings
    run_mod = importlib.import_module("sigil.study_guides.run")

    class Client:
        def site_info(self):
            return {"userid": 1}

        def user_courses(self, uid):
            return [{"id": 501, "fullname": "Ηλεκτρικά Κυκλώματα ΙΙ - 2026/1",
                     "shortname": "C2"},
                    {"id": 77, "fullname": "Ηλεκτρικά Κυκλώματα ΙΙ - 2025/2",
                     "shortname": "OLD"}]

        def call(self, fn, **params):
            return {"events": []} if "calendar" in fn else {"forums": []}

    cfg = {"study_guides_semester_start": "2026-10-01",
           "study_guides_semester_end": "2027-01-14",
           "study_guides_courses": [{"key": "circuits2", "name_gr": "Ηλεκτρικά Κυκλώματα ΙΙ",
                                     "name_en": "Electric Circuits II",
                                     "moodle_course_id": 0}]}
    with tempfile.TemporaryDirectory() as tmp:
        ids = Path(tmp) / "course_ids.json"
        save = lambda m: sg_settings.save_course_ids(m, path=ids)  # noqa: E731
        real_load = sg_settings.load_settings
        # Not the real data/study_guides/course_ids.json: once enrolled
        # it already holds the id, and nothing would be left to discover.
        load = lambda c: real_load(c, course_ids_path=ids)  # noqa: E731
        with mock.patch.object(run_mod, "save_course_ids", save), \
                mock.patch.object(sg_settings, "load_settings", load):
            result = exams.refresh_exams(cfg, now=datetime(2026, 9, 28, 12),
                                         path=Path(tmp) / "exams.json",
                                         client=Client(), force=True)
        if "moodle-calendar" not in result.ran:
            raise RuntimeError(f"the calendar source did not run: {result.errors}")
        got = sg_settings.load_settings(cfg, course_ids_path=ids).courses[0].moodle_course_id
        if got != 501:
            raise RuntimeError(f"picked course id {got}, wanted the 2026/1 one (501)")
    return "ids found from the reminders refresh, last year's course ignored"


def study_counted_lecture_check():
    """A lecture the page's schedule does not list: counted + syllabus topic."""
    import importlib
    import tempfile
    from datetime import date
    from pathlib import Path
    from types import SimpleNamespace
    from sigil.study_guides import settings as sg_settings
    from sigil.study_guides.models import LAB, Material, ScheduleEntry
    from sigil.study_guides.syllabus import SYLLABUS, syllabus_topic
    from sigil.study_guides.timetable import lecture_number
    run_mod = importlib.import_module("sigil.study_guides.run")

    cfg = {"study_guides_semester_start": "2026-10-01",
           "study_guides_semester_end": "2027-01-14",
           "study_guides_no_class_dates": ["2026-10-28"],
           "study_guides_timetable": [
               {"weekday": "WE", "start": "15:00", "end": "17:00",
                "course": "emfield1", "type": "Θ"},
               {"weekday": "FR", "start": "09:00", "end": "11:00",
                "course": "emfield1", "type": "Θ"}]}
    with tempfile.TemporaryDirectory() as tmp:
        s = sg_settings.load_settings(cfg, course_ids_path=Path(tmp) / "ids.json")
    counts = [lecture_number(s, "emfield1", date(2026, 10, d)) for d in (1, 2, 7, 9)]
    if counts != [0, 1, 2, 3]:
        raise RuntimeError(f"lecture numbers {counts}")
    if lecture_number(s, "emfield1", date(2026, 10, 30)) != 8:   # 10-28 is a holiday
        raise RuntimeError("holiday counted as a lecture")
    topics = SYLLABUS["emfield1"]
    if syllabus_topic("emfield1", 1, 26) != topics[0] \
            or syllabus_topic("emfield1", 26, 26) != topics[-1] \
            or syllabus_topic("nope", 1, 26) or syllabus_topic("emfield1", 1, 0):
        raise RuntimeError("syllabus mapping off")

    class State:
        def __init__(self, topics=(), files=()):
            self.topics, self.files = list(topics), set(files)

        def previous_topics(self, key, day):
            return self.topics

        def files_used_before(self, key, day):
            return self.files

    def ctx(state, day="2026-10-07"):
        return SimpleNamespace(settings=s, day_str=day, state=state)

    course = SimpleNamespace(key="emfield1")
    labs_only = [ScheduleEntry(date="2026-10-12", session_type=LAB, topic="Άσκηση 1")]
    entry, counted = run_mod._infer(ctx(State()), course, labs_only, "Θ")
    if not counted or not entry.topic.startswith("Διάλεξη 2 — ") or "Άσκηση" in entry.topic:
        raise RuntimeError(f"a lecture took {entry.topic!r}")

    def skips(state, files, topic):
        plan = SimpleNamespace(topic=topic)
        try:
            run_mod._check_counted_material(ctx(state), course, plan,
                                            Material(text="x", files_used=tuple(files)))
        except run_mod._Skip:
            return True
        return False

    t2, t3 = f"Διάλεξη 2 — {topics[0]}", f"Διάλεξη 3 — {topics[1]}"
    if not skips(State(), [], t2):
        raise RuntimeError("built a guide with no material")
    if not skips(State(), [f"f{i}.pdf" for i in range(9)], t2):
        raise RuntimeError("built a guide on the whole course page")
    if skips(State([t2], ["P1.pdf"]), ["P1.pdf"], t3):
        raise RuntimeError("same PDF, new syllabus topic was skipped")
    if not skips(State([f"Διάλεξη 1 — {topics[0]}"], ["P1.pdf"]), ["P1.pdf"], t2):
        raise RuntimeError("repeat of a topic on the same files was built")
    if not skips(State(["Διάλεξη 1"], ["P1.pdf"]), ["P1.pdf"], "Διάλεξη 2"):
        raise RuntimeError("bare lecture on old files was built")
    # Two lecturers on two days: each weekday counts and maps on its own,
    # and the strand's named files win even over the schedule-name filter.
    from sigil.study_guides.material import select_files
    from sigil.study_guides.models import FileRef
    from sigil.study_guides.syllabus import strand_for
    synthetic = {"appliedmath1": {
        "MO": {"label": "Lecturer A", "topics": [["Topic A1", ["a1.pdf", "a1-ex.pdf"]],
                                                 ["Topic A2", ["a2.pdf"]]]},
        "TH": {"label": "Lecturer B", "topics": [["Topic B1", ["b1.pdf"]],
                                                 ["Topic B2", ["b2.pdf"]]]}}}
    am = sg_settings.load_settings({**cfg, "study_guides_strands": synthetic,
                                    "study_guides_timetable": [
        {"weekday": "MO", "start": "12:00", "end": "15:00",
         "course": "appliedmath1", "type": "Θ"},
        {"weekday": "TH", "start": "12:00", "end": "15:00",
         "course": "appliedmath1", "type": "Θ"}]},
        course_ids_path=Path(tempfile.gettempdir()) / "no-such-ids.json")
    am_ctx = SimpleNamespace(settings=am, day_str="2026-10-05", state=State())
    mon = run_mod._numbered_lecture(am_ctx, SimpleNamespace(key="appliedmath1"))
    thu = run_mod._numbered_lecture(
        SimpleNamespace(settings=am, day_str="2026-10-08", state=State()),
        SimpleNamespace(key="appliedmath1"))
    mo, th = am.strands["appliedmath1"]["MO"], am.strands["appliedmath1"]["TH"]
    if mon.topic != f"Διάλεξη 1 ({mo.label}) — {mo.topics[0][0]}" \
            or mon.filenames != mo.topics[0][1]:
        raise RuntimeError(f"Monday strand: {mon}")
    if not thu.topic.startswith(f"Διάλεξη 2 ({th.label})"):
        raise RuntimeError(f"Thursday strand: {thu.topic!r}")
    if strand_for("emfield1", "MO", am.strands) is not None or strand_for("appliedmath1", "MO"):
        raise RuntimeError("a one-lecturer course got a strand")
    notes = "Σημειώσεις (για Νέο και Παλαιό Πρόγραμμα Σπουδών).pdf"
    refs = [FileRef(module_id=1, section_id=1, section_name="Υλικό", filename=n,
                    fileurl=f"u/{i}", timemodified=0)
            for i, n in enumerate((notes, "other.pdf", "set.pdf"))]
    picked = select_files(ScheduleEntry(date="2026-10-05", session_type="Θ",
                                        topic="x", filenames=(notes, "set.pdf")),
                          "x", refs)
    if [f.filename for f in picked] != [notes, "set.pdf"]:
        raise RuntimeError(f"named files not picked: {picked}")
    return ("counted lectures, holidays, syllabus topics, no lab topic for a lecture, "
            "skips, per-weekday strands, named files")


def study_goodnotes_check():
    """GoodNotes notes: subject folders map to courses, only pages no guide
    used are attached, and they count as used only once a guide is filed."""
    import tempfile
    from datetime import date
    from pathlib import Path
    from unittest import mock
    import pymupdf
    import importlib
    from sigil.study_guides import goodnotes as gn
    sg_run = importlib.import_module("sigil.study_guides.run")
    from sigil.study_guides.build import BuildResult
    from sigil.study_guides.generate import CLIResult
    from sigil.study_guides.models import OK, Course, Material
    from sigil.study_guides.settings import load_settings

    def notebook(path, pages):
        path.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as doc:
            for _ in range(pages):
                doc.new_page().draw_line((50, 50), (300, 120))   # ink, no text
            doc.save(path)

    def course(key, gr, en):
        return Course(key, gr, en, en)

    circuits = course("circuits2", "Ηλεκτρικά Κυκλώματα ΙΙ", "Electric Circuits II")
    courses = (circuits, course("electronics1", "Ηλεκτρονική Ι", "Electronics I"),
               course("emfield1", "Ηλεκτρομαγνητικό Πεδίο Ι", "Electromagnetic Field I"))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "iCloudDrive" / "GoodNotes"
        book = root / "Κυκλώματα 2" / "Σημειώσεις.pdf"
        notebook(book, 3)
        notebook(root / "Ηλεκτρονική" / "a.pdf", 1)
        (root / "Random").mkdir()
        if gn._best_folder(course("am2", "Εφαρμοσμένα Μαθηματικά ΙΙ", "Applied Maths II"),
                           [root / "Εφαρμοσμένα Μαθηματικά Ι"]) is not None:
            raise RuntimeError("a Ι folder matched a ΙΙ course")
        found = gn.course_folders(root, courses)
        got = {k: v.name for k, v in found.items()}
        if got != {"circuits2": "Κυκλώματα 2", "electronics1": "Ηλεκτρονική"}:
            raise RuntimeError(f"folder matching: {got}")
        stack_gd = mock.patch.object(gn, "_google_drives", lambda: [])
        stack_gd.start()
        nested = tmp / "OneDrive" / "Apps" / "Goodnotes 6"
        nested.mkdir(parents=True)
        with mock.patch.object(gn, "CLOUD_DIRS", (tmp / "nowhere", tmp / "iCloudDrive")):
            if gn.find_root("auto") != root or gn.find_root("") is not None:
                raise RuntimeError("find_root: auto/off wrong")
        with mock.patch.object(gn, "CLOUD_DIRS", (tmp / "OneDrive", tmp / "iCloudDrive")):
            if gn.find_root("auto") != nested:
                raise RuntimeError("find_root missed OneDrive/Apps/Goodnotes 6")
        stack_gd.stop()
        kw = dict(ledger_path=tmp / "ledger.json", notes_dir=tmp / "notes")
        first = gn.collect(str(root), {}, circuits, "2026-10-13", **kw)
        again = gn.collect(str(root), {}, circuits, "2026-10-13", **kw)
        if not first or first.page_count != 3 or not again or again.page_count != 3:
            raise RuntimeError("unused pages must stay pending until mark_used")
        gn.mark_used(first, ledger_path=kw["ledger_path"])
        if gn.collect(str(root), {}, circuits, "2026-10-14", **kw) is not None:
            raise RuntimeError("used pages were offered again")
        notebook(book, 5)                         # two pages written since
        later = gn.collect(str(root), {}, circuits, "2026-10-15", **kw)
        if not later or later.page_count != 2 or "σελ. 4-5" not in later.label:
            raise RuntimeError(f"new pages: {later}")
        capped = gn._newest([(book, 0, 19), (book, 0, 19)], 30)
        if sum(b - a + 1 for _p, a, b in capped) != 30 or capped[0][1] != 10:
            raise RuntimeError(f"page cap keeps the newest: {capped}")
        m = gn.attach(Material(text="slides"), later)
        if m.attachments[-1] != later.pdf or "GoodNotes" not in m.text \
                or not m.text.endswith("slides"):
            raise RuntimeError("attach did not add the notes")

        # Through the real run: attached for the generator, used once filed.
        sandbox = tmp / "run"
        sandbox.mkdir()
        cfg = {**_sg_cfg(str(sandbox)), "study_guides_goodnotes_dir": str(root)}
        if load_settings(_sg_cfg(str(sandbox))).goodnotes_dir != "":
            raise RuntimeError("a config without the key must leave notes off")
        staged = []

        def fake_generate(system, user, *, attachments_dir, settings):
            staged.append((user, (Path(attachments_dir) / gn.ATTACHMENT_NAME).is_file()))
            return CLIResult(text=r"\documentclass{article}\begin{document}x"
                                  r"\end{document}", cost_usd=0.1, raw={})

        def fake_build(latex, workdir, xelatex_, repair_fn, max_repairs=2):
            pdf = Path(workdir) / "guide.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            return BuildResult(True, pdf, ""), latex

        ledger = sandbox / "gn.json"
        ledger.write_text("{}", encoding="utf-8")
        run_kw = dict(ask_json=_sg_ask_json, generate_fn=fake_generate,
                      verify_fn=lambda latex, **_k: latex, review_fn=_sg_approve_all, build_fn=fake_build,
                      repair_fn=lambda *a, **k: "", out=lambda _l: None)
        client = _sg_client()
        try:
            with _sg_sandbox(str(sandbox)), \
                    mock.patch.object(gn, "LEDGER_PATH", ledger), \
                    mock.patch.object(gn, "NOTES_DIR", sandbox / "gn"):
                sg_run.run(cfg, day=date(2026, 10, 13), dry_run=True, client=client,
                           **run_kw)
                if gn.read_ledger(ledger):
                    raise RuntimeError("a dry run marked notes as used")
                result = sg_run.run(cfg, day=date(2026, 10, 13), client=client,
                                    **run_kw)[0]
        finally:
            client.close()
        if result.status != OK:
            raise RuntimeError(f"run failed: {result.reason}")
        if not staged or not staged[0][1] or gn.ATTACHMENT_NAME not in staged[0][0]:
            raise RuntimeError("notes were not staged/named for the generator")
        if gn.read_ledger(ledger).get("circuits2", {}).get(
                "Κυκλώματα 2/Σημειώσεις.pdf") != 5:
            raise RuntimeError(f"ledger after a filed guide: {gn.read_ledger(ledger)}")
    return "folders matched; new pages only; cap keeps newest; used only after filing"




# ---- study reminders: tests, finals, submissions (study_guides/exams.py,
# exam_text.py, exam_sources.py, reminders.py). Temp paths and fakes only:
# no Moodle, no Telegram, no Claude, never the real data/ files.
def _sr_fixtures():
    import types as _t
    from datetime import datetime as _dt, timedelta as _td
    now = _dt(2026, 10, 20, 12, 0).astimezone()

    def deadline(n, hours, *, kind="assign", status="open", cutoff_h=None, due_ts=None):
        due = due_ts if due_ts is not None else int((now + _td(hours=hours)).timestamp())
        return _t.SimpleNamespace(
            cmid=n, event_id=n, kind=kind, course_key="circuits2", course_short="CIRC2",
            title=f"Άσκηση {n}", due_ts=due, url=f"https://elearning.auth.gr/mod/{n}",
            assign_id=n if kind == "assign" else None,
            cutoff_ts=(int((now + _td(hours=cutoff_h)).timestamp())
                       if cutoff_h is not None else None),
            actionable=True, status=status)
    return now, deadline

def study_reminders_plan_check():
    from datetime import timedelta as _td
    from sigil.study_guides import reminders as rm
    from sigil.study_guides.exams import Exam
    now, deadline = _sr_fixtures()
    names = {"circuits2": "Ηλεκτρικά Κυκλώματα ΙΙ"}
    d50 = deadline(1, 50)
    got = rm.plan(now, [d50], [], {"sent": {}, "seen": {"dl:1": d50.due_ts}},
                  course_names=names)
    if len(got) != 1 or got[0].lead_h != 72 or len(got[0].keys) != 2:
        raise RuntimeError(f"catch-up lead: {got}")
    state = rm.next_state({"sent": {}, "seen": {}}, now, got,
                          rm.items_from([d50], [], names))
    if rm.plan(now, [d50], [], state, course_names=names):
        raise RuntimeError("a sent lead fired again")
    later = now + _td(hours=30)          # 20h left: the 24h lead is new
    again = rm.plan(later, [d50], [], state, course_names=names)
    if [r.lead_h for r in again] != [24]:
        raise RuntimeError(f"the 24h lead did not fire: {again}")
    moved = deadline(1, 50 + 48)
    re_armed = rm.plan(now, [moved], [], state, course_names=names)
    if len(re_armed) != 1 or re_armed[0].reason != "lead" or re_armed[0].note != "moved":
        raise RuntimeError(f"a moved due date did not re-arm: {re_armed}")
    if rm.plan(now, [deadline(2, 2, status="submitted")], [], {"sent": {}, "seen": {}}):
        raise RuntimeError("a submitted assignment was reminded")
    late = deadline(3, -2, cutoff_h=24)
    over = rm.plan(now, [late], [], {"sent": {}, "seen": {}})
    if [r.reason for r in over] != ["overdue"]:
        raise RuntimeError(f"no overdue line: {over}")
    st2 = rm.next_state({"sent": {}, "seen": {}}, now, over, rm.items_from([late], [], {}))
    if rm.plan(now, [late], [], st2):
        raise RuntimeError("the overdue line repeated")
    if rm.plan(now, [deadline(4, -2, cutoff_h=-1)], [], {"sent": {}, "seen": {}}):
        raise RuntimeError("overdue after the cut-off")
    far = deadline(5, 24 * 10)
    if rm.plan(now, [far], [], None):
        raise RuntimeError("the first run was not silent")
    new = rm.plan(now, [far], [], {"sent": {}, "seen": {}})
    if [r.reason for r in new] != ["new"]:
        raise RuntimeError(f"no New: line: {new}")
    test = Exam(id="manual:x", kind="test", course_key="circuits2",
                course_name="Ηλεκτρικά Κυκλώματα ΙΙ", title="Πρόοδος",
                date=(now + _td(hours=30)).date().isoformat(),
                time=(now + _td(hours=30)).strftime("%H:%M"), confirmed=False)
    both = rm.plan(now, [d50], [test], {"sent": {}, "seen": {"dl:1": d50.due_ts,
                                                             "ex:manual:x": test.due_ts()}},
                   course_names=names)
    if [r.item.group for r in both] != ["exam", "submission"] or both[0].lead_h != 48:
        raise RuntimeError(f"exam first with its 48h lead: {both}")
    text = rm.format_message(both, now)
    for bit in ("Tests & finals", "Submissions", "possible test - check the course page",
                "Ηλεκτρικά Κυκλώματα ΙΙ", "in 2 days", "https://elearning.auth.gr/mod/1"):
        if bit not in text:
            raise RuntimeError(f"message lacks {bit!r}:\n{text}")
    if text.index("Tests & finals") > text.index("Submissions"):
        raise RuntimeError("submissions came before tests")
    quiet = [rm.in_quiet_hours(now.replace(hour=h, minute=m), "23:00-08:00")
             for h, m in ((23, 30), (7, 59), (8, 0), (12, 0))]
    if quiet != [True, True, False, False]:
        raise RuntimeError(f"quiet hours wrong: {quiet}")
    if rm.leads_from_cfg({"study_remind_test_leads": [24, "x", 72]})["test"] != (72, 24):
        raise RuntimeError("config leads not cleaned")
    return "catch-up, no repeat, re-arm on move, submitted/overdue/cut-off, first-run seed, format, quiet"


def study_reminders_run_check():
    import tempfile
    import types as _t
    from pathlib import Path as _P
    from unittest import mock as _mock
    from tests.sample import SAMPLE as DEFAULTS
    from sigil.study_guides import assignments, reminders as rm
    now, deadline = _sr_fixtures()
    cfg = dict(DEFAULTS)
    sent: list = []
    with tempfile.TemporaryDirectory() as tmp:
        state = _P(tmp) / "sent.json"
        exams_path = _P(tmp) / "exams.json"
        snap = _t.SimpleNamespace(items=[deadline(1, 20), deadline(2, 24 * 10)])
        with _mock.patch.object(assignments, "cached", lambda *a, **k: snap):
            kw = dict(refresh=False, state_path=state, exams_path=exams_path,
                      check=lambda c, a: "not submitted")
            first = rm.run_reminders(cfg, now=now, send=lambda c, t: sent.append(t) or True, **kw)
            if first != "sent 1" or "Άσκηση 1" not in sent[-1] or "Άσκηση 2" in sent[-1]:
                raise RuntimeError(f"first run: {first} {sent}")
            if rm.run_reminders(cfg, now=now, send=lambda c, t: sent.append(t) or True, **kw) != "nothing due":
                raise RuntimeError("the second run re-sent")
            snap.items.append(deadline(3, 24 * 9))
            bad = rm.run_reminders(cfg, now=now, send=lambda c, t: False, **kw)
            if bad != "failed: telegram":
                raise RuntimeError(f"telegram failure: {bad}")
            soon = rm.run_reminders(cfg, now=now, send=lambda c, t: sent.append(t) or True, **kw)
            if not soon.startswith("backing off"):
                raise RuntimeError(f"no back-off right after a failed send: {soon}")
            from datetime import timedelta as _td
            ok = rm.run_reminders(cfg, now=now + _td(minutes=30),
                                  send=lambda c, t: sent.append(t) or True, **kw)
            if ok != "sent 1" or "New: " not in sent[-1]:
                raise RuntimeError(f"a failed send was marked sent: {ok} {sent[-1]}")
            night = rm.run_reminders(cfg, now=now.replace(hour=23, minute=30),
                                     send=lambda c, t: sent.append(t) or True, **kw)
            if night != "quiet hours":
                raise RuntimeError(f"quiet hours: {night}")
            gone = rm.run_reminders(cfg, now=now.replace(year=2027, month=6),
                                    send=lambda c, t: True, **kw)
            if not gone.startswith("skipped"):
                raise RuntimeError(f"outside the semester: {gone}")
            snap.items.append(deadline(4, 2))
            drop = rm.run_reminders(cfg, now=now,
                                    send=lambda c, t: sent.append(t) or True,
                                    **{**kw, "check": lambda c, a: "submitted"})
            if drop not in ("nothing due",):
                raise RuntimeError(f"a submitted assignment was sent: {drop}")
        if not state.is_file():
            raise RuntimeError("no state file written")
    return "sends once, retries after a failed send, quiet hours, window, submission check"


def study_reminders_review_fixes_check():
    # Review round 1: time/chapter ranges read as dates, quiz duplicates,
    # postponed tests, shared quiz sids, poll-time check_submission, the
    # 4096-char Telegram limit and the no-back-off retry loop.
    import tempfile
    import types as _t
    from datetime import date as _d, timedelta as _td
    from pathlib import Path as _P
    from unittest import mock as _mock
    from tests.sample import SAMPLE as DEFAULTS
    from sigil.study_guides import assignments, exam_sources as es, exams as ex
    from sigil.study_guides import exam_text as tx, reminders as rm
    from sigil.study_guides.exams import Exam
    lo, hi = _d(2026, 9, 24), _d(2027, 2, 12)
    for text in ("Η πρόοδος θα γίνει την Πέμπτη, ώρα 10:15-11:45, ύλη κεφάλαια 1-6",
                 "ώρα 10:30-12:30", "ασκήσεις 1-12", "Κεφ. 3-5"):
        if tx.extract_dates(text, lo, hi):
            raise RuntimeError(f"a range read as a date: {text!r} -> {tx.extract_dates(text, lo, hi)}")
    if tx.single_date("Πρόοδος 12-11 ώρα 10:00", lo, hi) != (_d(2026, 11, 12), "10:00"):
        raise RuntimeError("dd-mm date lost")
    if not tx.is_quiz_only("Quiz 2") or tx.is_quiz_only("Πρόοδος και quiz"):
        raise RuntimeError("is_quiz_only wrong")
    # Postponement: post 560 moves post 555's test from 12/11 to 20/11.
    ctx = _t.SimpleNamespace(low=lo, high=hi)
    found = [("2026-11-20", None, "", "test")]
    move = es.move_record(ctx, "circuits2", "Η πρόοδος μεταφέρεται από 12/11 στις 20/11", found)
    if not move or "2026-11-12" not in move["mentioned"]:
        raise RuntimeError(f"move_record: {move}")
    if es.move_record(ctx, "circuits2", "Πρόοδος 20/11", found) is not None:
        raise RuntimeError("an ordinary post read as a postponement")

    def ann(did, day, key="circuits2"):
        return Exam(id=f"ann:{did}:{day}", kind="test", course_key=key, course_name="X",
                    title="Πρόοδος", date=day, time=None, source="announcement")
    rows = [ann(555, "2026-11-12"), ann(560, "2026-11-20"), ann(700, "2026-11-12", "logic")]
    kept = {r.id for r in ex.supersede(rows, {"560": move}, _d(2026, 10, 20))}
    if kept != {"ann:560:2026-11-20", "ann:700:2026-11-12"}:
        raise RuntimeError(f"supersede kept {kept}")
    two = rows + [ann(556, "2026-12-01")]
    vague = {**move, "mentioned": ["2026-11-20"]}
    if len(ex.supersede(two, {"560": vague}, _d(2026, 10, 20))) != len(two):
        raise RuntimeError("supersede guessed between two old tests")
    # One quiz, two timeline events (open + close) -> two sids, no "Moved:".
    now, deadline = _sr_fixtures()
    opens, closes = deadline(7, 30, kind="quiz"), deadline(8, 80, kind="quiz")
    opens.cmid = closes.cmid = 99
    st = rm.next_state({"sent": {}, "seen": {}}, now, [], rm.items_from([opens, closes], []))
    if any(r.reason == "moved" or r.note == "moved"
           for r in rm.plan(now, [opens, closes], [], st)):
        raise RuntimeError("a quiz's two events read as Moved:")
    # Chunking under the Telegram limit, every reminder in exactly one chunk.
    many = [deadline(100 + i, 24 * 5) for i in range(60)]
    for d in many:
        d.url = "https://elearning.auth.gr/mod/assign/view.php?id=" + "9" * 60
    planned = rm.plan(now, many, [], {"sent": {}, "seen": {}})
    chunks = rm.format_messages(planned, now)
    if len(chunks) < 2 or any(len(t) > rm.MAX_MESSAGE_CHARS for t, _ in chunks) \
            or sum(len(m) for _, m in chunks) != len(planned):
        raise RuntimeError(f"chunking: {[len(t) for t, _ in chunks]}")
    # Back-off doubles; a clean send clears it.
    f2 = rm.with_failure(rm.with_failure(None, now), now)
    if not rm.backing_off(f2, now + _td(minutes=45)) or rm.backing_off(f2, now + _td(minutes=61)):
        raise RuntimeError("back-off not doubling")
    if "failures" in rm.next_state(f2, now, [], []):
        raise RuntimeError("a clean run kept the failure count")
    # The poll never calls check_submission; a partial send keeps the rest unseen.
    cfg = dict(DEFAULTS)
    with tempfile.TemporaryDirectory() as tmp:
        kw = dict(refresh=False, state_path=_P(tmp) / "s.json", exams_path=_P(tmp) / "e.json")
        snap = _t.SimpleNamespace(items=[deadline(1, 2)])

        def boom(*a, **k):
            raise AssertionError("check_submission called from the poll")
        with _mock.patch.object(assignments, "cached", lambda *a, **k: snap), \
                _mock.patch.object(assignments, "check_submission", boom):
            if rm.run_reminders(cfg, now=now, send=lambda c, t: True, **kw) != "sent 1":
                raise RuntimeError("the 2h reminder did not go out without a check")
            snap.items = many
            calls: list = []
            part = rm.run_reminders(cfg, now=now + _td(hours=1),
                                    send=lambda c, t: calls.append(t) or len(calls) == 1, **kw)
            if not part.startswith("partly sent"):
                raise RuntimeError(f"partial send: {part}")
            rest = rm.run_reminders(cfg, now=now + _td(hours=2),
                                    send=lambda c, t: True, **kw)
            if not rest.startswith("sent"):
                raise RuntimeError(f"the unsent New: lines were lost: {rest}")
    return "ranges, quiz-only, postponement, quiz sids, chunking, back-off, no poll check"


def study_exams_store_check():
    import tempfile
    from pathlib import Path as _P
    from tests.sample import SAMPLE as DEFAULTS
    from sigil.study_guides import exams as ex
    from sigil.study_guides.exams import Exam
    cfg = dict(DEFAULTS)
    with tempfile.TemporaryDirectory() as tmp:
        path = _P(tmp) / "exams.json"
        mine = ex.add_manual("circuits", "2026-11-12", "10:00", "test", cfg=cfg, path=path)
        if mine.course_key != "circuits2" or mine.source != "manual":
            raise RuntimeError(f"course not matched: {mine}")
        try:
            ex.add_manual("x", "12 Nov", path=path)
            raise RuntimeError("a bad date was accepted")
        except ValueError:
            pass
        sched = Exam(id="sched:circuits2:2026-11-12", kind="test", course_key="circuits2",
                     course_name="", title="πιθανή πρόοδος", date="2026-11-12",
                     source="schedule", confirmed=False)
        cal = Exam(id="cal:9", kind="test", course_key="electronics1", course_name="",
                   title="Test", date="2026-11-20", source="moodle-calendar")
        rows = ex.replace_source(ex.stored_rows(ex.read_store(path)), "schedule", [sched])
        rows = ex.replace_source(rows, "moodle-calendar", [cal])
        if len(ex.dedupe(rows)) != 2 or ex.dedupe(rows)[0].source != "manual":
            raise RuntimeError(f"dedupe did not prefer the confirmed manual row: {ex.dedupe(rows)}")
        if not any(r.source == "manual" for r in ex.replace_source(rows, "schedule", [])):
            raise RuntimeError("a refresh removed a manual row")
        try:
            ex.replace_source(rows, "manual", [])
            raise RuntimeError("manual rows were replaceable")
        except ValueError:
            pass
        if Exam.from_json({"id": 3}) is not None or Exam.from_json("junk") is not None:
            raise RuntimeError("junk rows were accepted")
        if not ex.remove_manual(mine.id, path=path) or ex.load_exams(path):
            raise RuntimeError("remove_manual failed")
    return "manual add/remove, course match, replace_source, dedupe, junk rows"


def study_exams_text_check():
    from datetime import date as _d
    from sigil.study_guides import exam_text as tx
    lo, hi = _d(2026, 9, 24), _d(2027, 2, 12)
    cases = {
        "Η πρόοδος θα γίνει την Πέμπτη 12 Νοεμβρίου στις 10:00": (_d(2026, 11, 12), "10:00"),
        "Διαγώνισμα 15/12 ώρα 14:00": (_d(2026, 12, 15), "14:00"),
        "Test on 3/2/2027": (_d(2027, 2, 3), None),
        "Midterm November 20": (_d(2026, 11, 20), None),
    }
    for text, want in cases.items():
        if tx.single_date(text, lo, hi) != want:
            raise RuntimeError(f"{text!r} -> {tx.single_date(text, lo, hi)}")
    if tx.single_date("12/11 ή 19/11", lo, hi) is not None:
        raise RuntimeError("two dates read as one")
    for yes in ("ΠΡΟΟΔΟΣ", "διαγώνισμα", "τεστ", "Quiz 2", "midterm", "εξετάσεις"):
        if not tx.is_exam_text(yes):
            raise RuntimeError(f"missed {yes!r}")
    if tx.is_exam_text("Διάλεξη 3: Νόμοι Kirchhoff"):
        raise RuntimeError("a lecture read as a test")
    if tx.exam_kind("τελική εξέταση") != "final" or not tx.is_explicit("Πρόοδος 1"):
        raise RuntimeError("kind / explicit wrong")
    return "Greek months, dd/mm, year inference, ambiguity, keywords"


def study_exams_sources_check():
    import tempfile
    from dataclasses import replace as _rep
    from datetime import date as _d, datetime as _dt
    from pathlib import Path as _P
    from unittest import mock as _mock
    from tests.sample import SAMPLE as DEFAULTS
    from sigil.study_guides import exam_sources as es, exams as ex, settings as sg
    pages = {
        es.EXAM_TIMETABLE_PAGE: (
            '<a href="/wp-content/uploads/Πρόγραμμα-Χειμερινής-Εξεταστικής-Περιόδου-2026-27_v1.pdf">'
            'Πρόγραμμα Χειμερινής Εξεταστικής 2026-27</a>'
            '<a href="/wp-content/uploads/Πρόγραμμα-Εαρινής-Εξεταστικής-2025-26.pdf">εαρινή</a>'
            '<a href="https://evil.example.com/programma-exetastikis-xeimerini.pdf">x</a>'),
    }
    fetch = lambda url: pages.get(url, "")  # noqa: E731
    url = es.find_exam_timetable_url(fetch, academic_year="2026-27")
    if "2026-27_v1.pdf" not in url or "auth.gr" not in url:
        raise RuntimeError(f"timetable link: {url!r}")
    if es.find_exam_timetable_url(lambda u: "", academic_year="2026-27"):
        raise RuntimeError("found a link on an empty site")
    settings = sg.load_settings(dict(DEFAULTS))
    courses = [_rep(settings.courses[0], moodle_course_id=111)]
    settings = _rep(settings, courses=tuple(courses))

    class Client:
        calls: list = []

        def call(self, fn, **kw):
            self.calls.append(fn)
            if fn == "core_calendar_get_calendar_events":
                ts = int(_dt(2026, 11, 12, 10, 0).timestamp())
                return {"events": [
                    {"id": 1, "name": "Πρόοδος Κυκλωμάτων", "courseid": 111, "timestart": ts},
                    {"id": 2, "name": "Quiz 1", "modulename": "quiz", "courseid": 111,
                     "timestart": ts},
                    {"id": 3, "name": "Διάλεξη", "courseid": 111, "timestart": ts}]}
            if fn == "mod_forum_get_forums_by_courses":
                return []
            raise AssertionError(fn)

    with tempfile.TemporaryDirectory() as tmp, \
            _mock.patch.object(sg, "load_settings", lambda cfg: settings):
        path = _P(tmp) / "exams.json"
        ex.add_manual("x", "2026-12-01", path=path)
        res = ex.refresh_exams(dict(DEFAULTS), now=_dt(2026, 10, 20, 12, 0), path=path,
                               client=Client(), fetch=lambda u: "",
                               schedule_cache_path=_P(tmp) / "none.json")
        cal = [e for e in res.exams if e.source == "moodle-calendar"]
        if len(cal) != 1 or cal[0].date != "2026-11-12" or cal[0].time != "10:00":
            raise RuntimeError(f"calendar rows: {res.exams} errors={res.errors}")
        if not any(e.source == "manual" for e in ex.load_exams(path)):
            raise RuntimeError("the refresh dropped the manual row")
        again = ex.refresh_exams(dict(DEFAULTS), now=_dt(2026, 10, 20, 13, 0), path=path,
                                 client=Client(), schedule_cache_path=_P(tmp) / "none.json")
        if Client.calls.count("core_calendar_get_calendar_events") != 1:
            raise RuntimeError(f"the 3h Moodle floor was ignored: {Client.calls}")
        if len(again.exams) != 2:
            raise RuntimeError(f"rows lost between refreshes: {again.exams}")
    return "ece.auth.gr link filter, calendar source (module events skipped), 3h floor, manual kept"








# ---- pytest entry points ----


def test_timetable():
    study_timetable_check()


def test_calendar_labs():
    study_calendar_labs_check()


def test_schedule():
    study_schedule_check()


def test_names_latex():
    study_names_latex_check()


def test_filing():
    study_filing_check()


def test_state():
    study_state_check()


def test_semester():
    study_semester_check()


def test_moodle_fixtures():
    study_moodle_fixtures()


def test_dry_run():
    study_dry_run()


def test_pipeline():
    study_pipeline()


def test_review_gate():
    study_review_gate()


def test_catchup():
    study_catchup_check()


def test_deadlines():
    study_deadlines_check()


def test_weekly():
    study_weekly_check()


def test_ids_on_enrol():
    study_ids_on_enrol_check()


def test_counted_lecture():
    study_counted_lecture_check()


def test_goodnotes():
    study_goodnotes_check()




def test_reminders_plan():
    study_reminders_plan_check()


def test_reminders_run():
    study_reminders_run_check()


def test_reminders_review_fixes():
    study_reminders_review_fixes_check()


def test_exams_store():
    study_exams_store_check()


def test_exams_text():
    study_exams_text_check()


def test_exams_sources():
    study_exams_sources_check()


