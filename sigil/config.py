"""Configuration for the Sigil study assistant.

Two sources, kept apart on purpose:

* **Secrets live in the OS credential store** (Windows Credential Manager),
  see `secrets_store`. They are never read from or written to a file: not
  config.json, not .env.
* **Everything else** (courses, timetable, semester dates, models, paths) lives
  in config.json, created from config.example.json on first run. `.env` may
  hold non-secret settings such as SIGIL_DATA_DIR. Both are gitignored.

Runtime data (caches, logs, built PDFs) goes to `DATA_DIR`:
`SIGIL_DATA_DIR` if set, else a per-user folder outside the project
(see `_default_data_dir`), created with owner-only permissions.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from dotenv import dotenv_values

from .secrets_store import SECRET_NAMES, get_secret

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
ENV_PATH = ROOT / ".env"



def _load_env_file() -> None:
    """Non-secret settings from .env into os.environ (never overriding the real
    environment). A secret found there is ignored: secrets belong in the
    credential store, not in a file."""
    for key, value in dotenv_values(ENV_PATH).items():
        if key.upper() in SECRET_NAMES:
            log.warning(".env contains %s; ignored. Store it with "
                        "`python -m sigil.study_guides set-secret %s` and delete the line.",
                        key, key.upper())
            continue
        if value is not None:
            os.environ.setdefault(key, value)


_load_env_file()



def _default_data_dir() -> Path:
    """Per-user, outside the project folder and outside synced folders:
    %LOCALAPPDATA%/sigil-study on Windows, ~/.local/share/sigil-study elsewhere."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "sigil-study"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "sigil-study"


DATA_DIR = Path(os.environ.get("SIGIL_DATA_DIR") or _default_data_dir()).expanduser()

# config key -> secret name. These keys are answered from the credential store
# only; a value for them in config.json is ignored and never saved.
SECRET_ENV = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}

# Every secret this project reads from the environment. Child processes (the
# claude CLI, xelatex, git) get an environment without them: course material
# that reaches the model is untrusted, so the model's process must not hold them.
SECRET_ENV_NAMES = frozenset(SECRET_NAMES)


# What a child process may inherit: an allowlist, so a variable added to .env or
# the shell later is not handed to the model's process by default.
_CHILD_ENV_NAMES = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "USERNAME", "USER", "LOGNAME", "COMPUTERNAME", "OS", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "LANG", "LANGUAGE", "TZ", "TERM", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
})
_CHILD_ENV_PREFIXES = ("LC_", "MIKTEX", "TEXMF", "TEXINPUTS")


def child_env(extra: dict | None = None, allow_prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """An allowlisted copy of os.environ (never a secret), plus `extra`.

    `allow_prefixes` admits more names for one caller, e.g. the claude CLI's
    own CLAUDE_*/ANTHROPIC_* settings when it is authenticated through them."""
    prefixes = _CHILD_ENV_PREFIXES + tuple(allow_prefixes)
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in SECRET_ENV_NAMES
           and (k.upper() in _CHILD_ENV_NAMES or k.upper().startswith(prefixes))}
    env.update(extra or {})
    return env


DEFAULTS = {
    # Obsidian vault the guides are filed into ("" = keep PDFs in DATA_DIR only).
    "vault_path": "",
    "allow_vault_writes": True,
    # Optional folder of Claude Cowork projects that hold hand-made study guides.
    "projects_root": "",
    # Path or name of the Claude Code CLI ("" = find `claude` on PATH).
    "agent_claude_cmd": "",
    "study_guides_enabled": True,
    "study_guides_catchup_days": 3,
    "study_guides_semester_start": "",
    "study_guides_semester_end": "",
    # "YYYY-MM-DD" days and "YYYY-MM-DD..YYYY-MM-DD" ranges without classes.
    "study_guides_no_class_dates": [],
    "study_guides_skip_labs": False,
    "study_guides_infer_topic": True,
    "study_guides_model": "opus",
    "study_guides_helper_model": "sonnet",
    "study_guides_verify_pass": True,
    "study_guides_review_pass": True,
    "study_guides_review_model": "opus",
    "study_guides_timeout_sec": 1800,
    # Most guide attempts per day that may reach Claude (cost and abuse cap).
    "study_guides_max_guides_per_day": 6,
    "study_guides_moodle_url": "https://elearning.auth.gr",
    "study_guides_vault_root": "03 Resources/University",
    "study_guides_xelatex": "",
    # Handwritten-notes backup folder: "auto", a path, or "" (off).
    "study_guides_goodnotes_dir": "",
    "study_guides_goodnotes_folders": {},
    # Read lab dates from an iCloud calendar (needs `calendar-login`).
    "study_guides_calendar_labs": False,
    "study_guides_calendar_aliases": {},
    # Courses taught by two lecturers on different days (see syllabus.py).
    "study_guides_strands": {},
    # [{"key", "name_gr", "name_en", "vault_folder", "moodle_course_id",
    #   "addons", "exam_format"}] — see config.example.json.
    "study_guides_courses": [],
    # [{"weekday": "TU", "start": "09:00", "end": "11:00", "course": "<key>",
    #   "type": "Θ" | "Α" | "lab"}]
    "study_guides_timetable": [],
    "study_guides_finished_courses": [],
    "study_mode_hidden_courses": [],
    "study_deadlines_horizon_days": 21,
    "study_deadlines_lookback_days": 7,
    "study_weekly_enabled": True,
    "study_weekly_model": "opus",
    "study_weekly_send_pdf": True,
    "study_cowork_projects": [],
    "study_cowork_auto": False,
    "study_cowork_ignore": [],
    # Approximate exam period; correct it when the official calendar is out.
    "study_exam_period_start": "",
    "study_exam_period_end": "",
    "study_remind_enabled": True,
    "study_remind_assignment_leads": [168, 72, 24, 3],
    "study_remind_quiz_leads": [72, 24, 3],
    "study_remind_test_leads": [168, 48, 24],
    "study_remind_final_leads": [504, 168, 72, 24],
    "study_remind_quiet": "23:00-08:00",
}


class Config:
    def __init__(self, data: dict):
        self._data = {k: v for k, v in data.items() if k not in SECRET_ENV}

    def get(self, name, default=None):
        if name in SECRET_ENV:
            return get_secret(SECRET_ENV[name]) or default
        return self._data.get(name, default)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in SECRET_ENV:
            return self.get(name, "")
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def set(self, name, value):
        if name in SECRET_ENV:
            raise ValueError(f"{name} is a secret: run `python -m sigil.study_guides "
                             f"set-secret {SECRET_ENV[name]}` instead")
        self._data[name] = value

    @property
    def vault(self) -> Path:
        return Path(self._data.get("vault_path") or "")

    def save(self) -> None:
        """Write config.json atomically (temp file + replace). Secrets never go in."""
        if getattr(self, "_read_only", False):
            log.error("config save skipped: config.json failed to load at startup")
            return
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

    def as_dict(self) -> dict:
        return dict(self._data)


def load_config() -> Config:
    from .fileio import restrict_to_owner
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Every start, not only on creation: an existing folder or a hand-made
    # .env may still carry inherited permissions.
    for path in (DATA_DIR, ENV_PATH):
        if path.exists() and not restrict_to_owner(path):
            log.warning("could not restrict %s to your account", path.name)
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False),
                                   encoding="utf-8")

    # A failed read must never turn into a saved reset: retry (antivirus can hold
    # the file briefly), and if it still fails run on defaults with save() off.
    data = dict(DEFAULTS)
    error = None
    for attempt in range(5):
        try:
            data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            error = None
            break
        except Exception as exc:  # noqa: BLE001
            error = exc
            time.sleep(0.2 * (attempt + 1))
    leaked = [k for k in SECRET_ENV if data.get(k)]
    if leaked:
        log.warning("config.json contains %s; ignored — secrets are read from the credential store only",
                    ", ".join(leaked))
    cfg = Config(data)
    if error is not None:
        cfg._read_only = True
        log.error("config.json could not be read (%s): running on defaults and NOT "
                  "saving any setting this session", type(error).__name__)
    return cfg
