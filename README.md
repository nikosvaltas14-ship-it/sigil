# Sigil — study assistant

Sigil reads my courses on ΑΠΘ e-learning (Moodle), writes a Greek study guide
for every class I had, and reminds me on Telegram before tests, finals and
deadlines. It is the study part of Sigil, my personal assistant.

## What it does today

- **Moodle access without storing a password.** Signs in through ΑΠΘ's single
  sign-on in a small browser window (or takes a token) and keeps only the
  Moodle web-service token.
- **Course discovery.** Finds the semester's enrolled courses and maps them to
  the courses in your config.
- **A study guide for each day's classes, once a day.** From 20:30 (config
  `study_guides_run_time`) it builds the guides for the classes that have ended
  and still have no guide, one pass per day. For each one it:
  - reads the course page's schedule (or the official course outline) to find
    the day's topic;
  - picks that lecture's files and extracts their text (PDF, PowerPoint, Word,
    scanned PDFs);
  - has Claude write a Greek LaTeX guide (theory from zero, worked examples,
    exercises with full solutions, common mistakes) with Sonnet at medium
    effort;
  - runs a second pass that checks the maths, and a final review by Opus at low
    effort that approves or corrects the guide;
  - compiles it with XeLaTeX and files the PDF in an Obsidian vault folder per
    course.
- **A hard weekly budget.** The guides may take at most 50% of the weekly
  Claude limit (`study_guides_weekly_budget_pct`), paced at an equal share per
  day of the limit's 7-day window: on day N the weekly usage, counting
  everything on the account, may be at most 50% x N/7. Unused days carry over.
  Before each guide the weekly usage is read from the Claude Code CLI and the
  guide only starts if its expected cost (measured from earlier guides, 4% until
  then) still fits under that day's allowance. If the usage cannot be read, or
  the 5-hour limit is used up, nothing is built and the next hourly run
  tries again. Telegram is told once a day when the guides are paused.
- **Timetable aware.** Uses a weekly timetable, holidays and semester dates.
  Lab dates can come from an iCloud calendar.
- **Handwritten notes (optional).** Adds new pages from GoodNotes PDF backups
  to the next guide of that course.
- **Deadlines and exams.** Reads assignments and quizzes from the Moodle
  timeline, and tests/finals from the Moodle calendar, course announcements and
  the department's exam timetable.
- **Telegram reminders.** Sends reminders at configurable lead times (e.g. 7
  days, 3 days, 1 day, 3 hours before), with quiet hours. Outgoing messages
  only: there is no bot command interface.
- **Weekly review.** A Saturday PDF summary of the week's classes, guides and
  deadlines.

## Tech stack

Python 3.12 · httpx · PyMuPDF, python-pptx, python-docx · Claude Code CLI
(Opus/Sonnet) · XeLaTeX (MiKTeX or TeX Live) · PySide6 QtWebEngine (SSO login
only) · caldav · Telegram Bot API · pytest

Developed and tested on Windows 11.

## Setup

```bash
git clone https://github.com/<you>/sigil.git
cd sigil
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-sso.txt   # or requirements.txt without the SSO browser
```

You also need:

- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and
  logged in (`claude` on PATH). Guides are generated through it.
- XeLaTeX ([MiKTeX](https://miktex.org/) on Windows, TeX Live elsewhere) with
  the DejaVu fonts.

Configure:

```bash
copy config.example.json config.json          # macOS/Linux: cp
python -m sigil.study_guides set-secret TELEGRAM_BOT_TOKEN   # typed hidden
python -m sigil.study_guides set-secret TELEGRAM_CHAT_ID
python -m sigil.study_guides secrets                         # what is set (never values)
```

- **Secrets are never stored in a file.** They live in the OS credential store
  (Windows Credential Manager, macOS Keychain, Linux Secret Service), encrypted
  with your login. `login` and `calendar-login` put the Moodle token and the
  iCloud app-specific password there too. Treat the Moodle token like your
  student password: it can do anything the Moodle app can.
- `config.json`: your courses, timetable, semester dates and the vault folder
  for the PDFs (`vault_path`). The example timetable is made up; replace it.
- `.env` (optional, see `.env.example`) holds non-secret settings only.
- Runtime data (caches, logs, built PDFs) goes to `%LOCALAPPDATA%\sigil-study`
  on Windows or `~/.local/share/sigil-study` elsewhere, owner-only.

Then:

```bash
python -m sigil.study_guides login --browser   # sign in once (ΑΠΘ SSO)
python -m sigil.study_guides discover          # map Moodle courses to config keys
python -m sigil.study_guides run --dry-run     # see today's topic, files and prompt
python -m sigil.study_guides run               # build today's guides
```

## Running it automatically

Three commands are meant to run on a schedule:

| Command | When | What |
|---|---|---|
| `catchup` | hourly | one pass a day (from `study_guides_run_time`): guides for finished classes that have none yet, within the weekly budget (at most `study_guides_max_guides_per_day`, default 6) |
| `remind` | every 30 min | due Telegram reminders |
| `weekly` | Saturdays | the weekly review PDF |

Windows Task Scheduler example (run from the project folder):

```bat
schtasks /Create /TN "Sigil catchup" /SC HOURLY /TR "\"%CD%\.venv\Scripts\python.exe\" -m sigil.study_guides catchup"
```

On macOS/Linux, use cron with the same commands.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests run fully offline against synthetic Moodle responses and a fake
Claude CLI.

## Security

See [SECURITY.md](SECURITY.md). In short: secrets only in the OS credential
store (never a file), untrusted course material never reaches a shell, the
LaTeX the model writes is checked against an allowlist, every built PDF is
scanned before use, and there are no listening ports.

## License

MIT, see [LICENSE](LICENSE). Dependencies keep their own licences; note that
PyMuPDF is AGPL-3.0.
