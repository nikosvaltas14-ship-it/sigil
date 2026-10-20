"""The weekly-limit budget, the once-a-day pass and the model/effort choices."""
import json
from datetime import date, datetime
from types import SimpleNamespace

from sigil import config as config_mod
from sigil.study_guides import generate, jobs, usage
from sigil.study_guides.models import OK
from sigil.study_guides.settings import load_settings

WEEK_END = 2_000_000_000                 # a window that resets at this epoch second
WEEK_START = WEEK_END - usage.WEEK_SECONDS


def _event(week, session=0.1, status="allowed", reset=WEEK_END):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": session, "resetsAt": reset - 1},
                           "seven_day": {"utilization": week, "resetsAt": reset}}}})


# ---- reading the CLI's usage ------------------------------------------------

def test_parse_usage_reads_both_windows_from_the_last_event():
    out = "\n".join(['{"type":"assistant"}', _event(0.2), _event(0.25, session=0.4)])
    got = usage.parse_usage(out)
    assert (got.week, got.session, got.week_resets_at, got.rejected) == (
        0.25, 0.4, WEEK_END, False)


def test_parse_usage_marks_a_refused_call():
    assert usage.parse_usage(_event(0.5, session=1.01, status="rejected")).rejected


def test_parse_usage_without_an_event_is_none():
    assert usage.parse_usage('{"type":"result"}\nnot json') is None


# ---- the allowance ----------------------------------------------------------

def test_allowance_grows_a_seventh_of_the_share_per_day_and_stops_at_the_share():
    day = usage.DAY_SECONDS
    assert usage.allowance(WEEK_END, WEEK_START + 60, 0.5) == 0.5 / 7
    assert abs(usage.allowance(WEEK_END, WEEK_START + 3 * day + 5, 0.5) - 0.5 * 4 / 7) < 1e-9
    assert usage.allowance(WEEK_END, WEEK_END - 1, 0.5) == 0.5
    assert usage.allowance(WEEK_END, WEEK_END + 99 * day, 0.5) == 0.5


def test_decide_allows_under_the_days_allowance_and_blocks_over_it():
    now = WEEK_START + 3600                              # day 1: allowance 7.14%
    ok = usage.decide(usage.Usage(0.02, WEEK_END, 0.1), share=0.5, estimate=0.04, now_ts=now)
    assert ok.allowed
    over = usage.decide(usage.Usage(0.05, WEEK_END, 0.1), share=0.5, estimate=0.04, now_ts=now)
    assert not over.allowed and not over.retry_later


def test_unspent_days_carry_over():
    day4 = WEEK_START + 3 * usage.DAY_SECONDS + 3600     # allowance 28.6%
    assert usage.decide(usage.Usage(0.2, WEEK_END, 0.0), share=0.5, estimate=0.04,
                        now_ts=day4).allowed


def test_decide_never_passes_the_weekly_share():
    last = WEEK_END - 60
    assert not usage.decide(usage.Usage(0.48, WEEK_END, 0.0), share=0.5, estimate=0.04,
                            now_ts=last).allowed


def test_decide_fails_closed_when_usage_is_unknown_and_retries_later():
    for reading in (None, usage.Usage(None, None, None)):
        d = usage.decide(reading, share=0.5, estimate=0.04, now_ts=WEEK_START)
        assert not d.allowed and d.retry_later


def test_decide_waits_out_the_five_hour_limit():
    d = usage.decide(usage.Usage(0.0, WEEK_END, 1.0, rejected=True), share=0.5,
                     estimate=0.01, now_ts=WEEK_START + 10)
    assert not d.allowed and d.retry_later


def test_cost_estimate_is_the_largest_recent_guide(tmp_path):
    path = tmp_path / "cost.json"
    assert usage.estimate_cost(0.04, path) == 0.04
    usage.record_cost(usage.Usage(0.10, WEEK_END), usage.Usage(0.13, WEEK_END), path)
    usage.record_cost(usage.Usage(0.13, WEEK_END), usage.Usage(0.19, WEEK_END), path)
    assert abs(usage.estimate_cost(0.04, path) - 0.06) < 1e-9
    # a window rollover or an unchanged reading says nothing about the cost
    usage.record_cost(usage.Usage(0.9, WEEK_END), usage.Usage(0.0, WEEK_END + 99), path)
    usage.record_cost(usage.Usage(0.2, WEEK_END), usage.Usage(0.2, WEEK_END), path)
    assert len(json.loads(path.read_text())) == 2


# ---- models and effort ------------------------------------------------------

def test_defaults_sonnet_medium_writes_and_opus_low_reviews():
    s = load_settings(config_mod.Config({}))
    assert (s.model, s.effort) == ("sonnet", "medium")
    assert (s.review_model, s.review_effort) == ("opus", "low")
    assert s.weekly_budget == 0.5


def test_bad_effort_falls_back_to_the_default():
    s = load_settings(config_mod.Config({"study_guides_effort": "turbo"}))
    assert s.effort == "medium"


def test_effort_reaches_the_cli():
    cmd = generate._command("claude", "sonnet", "Read", (), None, None, True, "medium")
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert "--effort" not in generate._command("claude", "sonnet", "", (), None, None)


def test_guide_and_review_calls_use_their_own_model_and_effort(monkeypatch, tmp_path):
    calls = []

    def fake(prompt, **kw):
        calls.append((kw["model"], kw["effort"]))
        return generate.CLIResult(text="APPROVED\\documentclass x\\end{document}", cost_usd=None)

    monkeypatch.setattr(generate, "run_claude", fake)
    s = load_settings(config_mod.Config({}))
    try:
        generate.generate_guide("sys", "user", attachments_dir=tmp_path, settings=s)
    except generate.ClaudeCLIError:
        pass
    try:
        generate.review_guide("\\documentclass x\\end{document}", pdf_path=tmp_path / "a.pdf",
                              workdir=tmp_path, settings=s)
    except generate.ClaudeCLIError:
        pass
    assert calls == [("sonnet", "medium"), ("opus", "low")]


# ---- the once-a-day pass ----------------------------------------------------

def _pass_env(monkeypatch, tmp_path, reading, n_items=2):
    monkeypatch.setattr(usage, "_latest", None)
    monkeypatch.setattr(jobs, "BUDGET_PATH", tmp_path / "budget.json")
    monkeypatch.setattr(jobs, "PASS_PATH", tmp_path / "pass.json")
    monkeypatch.setattr(usage, "COST_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(jobs, "load_settings", lambda cfg: SimpleNamespace(
        enabled=True, run_time="20:30", weekly_budget=0.5, guide_cost=0.04))
    monkeypatch.setattr(jobs.semester, "notice_due", lambda s, d: False)
    monkeypatch.setattr(jobs.icloud_calendar, "refresh_if_stale", lambda now: False)
    items = [SimpleNamespace(day=date(2026, 10, 20), course_key=f"c{i}", last_reason="")
             for i in range(n_items)]
    monkeypatch.setattr(jobs.catchup, "due", lambda s, now, lb: items)
    monkeypatch.setattr(usage, "read_usage", lambda s, d: reading)
    sent, ran = [], []
    monkeypatch.setattr(jobs.notify, "send_telegram", lambda cfg, text: sent.append(text) or True)

    def fake_run(cfg, day, course, out):
        ran.append(course)
        return [SimpleNamespace(status=OK, extra={}, reason="", course_key=course)]

    monkeypatch.setattr(jobs, "run", fake_run)
    import sigil.study_guides as sg
    monkeypatch.setattr(sg, "summary_text", lambda results, day: "ok")
    return sent, ran


def _at(hour, minute=0):
    return datetime(2026, 10, 20, hour, minute)


def _reading(week, day_no=3, **kw):
    """A reading taken during `day_no` (half way through it) of the weekly window."""
    reset = _at(20, 30).timestamp() + (7 - (day_no - 1) - 0.5) * usage.DAY_SECONDS
    return usage.Usage(week, int(reset), kw.get("session", 0.0), kw.get("rejected", False))


def test_nothing_is_built_before_the_daily_run_time(monkeypatch, tmp_path):
    sent, ran = _pass_env(monkeypatch, tmp_path, _reading(0.0))
    assert "daily pass runs from 20:30" in jobs.run_catchup(config_mod.Config({}), now=_at(20, 0))
    assert ran == []


def test_the_pass_runs_once_a_day(monkeypatch, tmp_path):
    sent, ran = _pass_env(monkeypatch, tmp_path, _reading(0.0))
    cfg = config_mod.Config({})
    jobs.run_catchup(cfg, now=_at(20, 30))
    assert ran == ["c0", "c1"]
    assert "already ran" in jobs.run_catchup(cfg, now=_at(21, 30))
    assert ran == ["c0", "c1"]


def test_over_the_days_allowance_builds_nothing_and_tells_once(monkeypatch, tmp_path):
    sent, ran = _pass_env(monkeypatch, tmp_path, _reading(0.30))   # allowance 21.4%
    cfg = config_mod.Config({})
    jobs.run_catchup(cfg, now=_at(20, 30))
    jobs.run_catchup(cfg, now=_at(21, 30))
    assert ran == []
    assert len(sent) == 1 and "paused" in sent[0]


def test_the_budget_is_checked_before_every_guide(monkeypatch, tmp_path):
    # 14% used, allowance 21.4%, a guide costs ~4%: one guide fits, a second does not
    sent, ran = _pass_env(monkeypatch, tmp_path, _reading(0.14), n_items=3)

    def fake_run(cfg, day, course, out):
        ran.append(course)
        usage.note(_reading(0.14 + 0.05 * len(ran)))
        return [SimpleNamespace(status=OK, extra={}, reason="", course_key=course)]

    monkeypatch.setattr(jobs, "run", fake_run)
    jobs.run_catchup(config_mod.Config({}), now=_at(20, 30))
    assert ran == ["c0"]


def test_unreadable_usage_builds_nothing_and_is_retried_next_hour(monkeypatch, tmp_path):
    sent, ran = _pass_env(monkeypatch, tmp_path, None)
    cfg = config_mod.Config({})
    jobs.run_catchup(cfg, now=_at(20, 30))
    assert ran == []
    monkeypatch.setattr(usage, "read_usage", lambda s, d: _reading(0.0))
    jobs.run_catchup(cfg, now=_at(21, 30))
    assert ran == ["c0", "c1"]
