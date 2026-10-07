# Security

## Secrets never go in this repository

Tokens, passwords and chat ids live only in the OS credential store (Windows
Credential Manager, macOS Keychain, Linux Secret Service), never in a file:
not in the repo, not in `.env` (secrets found there are ignored), not in the
data folder. `.env`, `config.json` and `data/` are gitignored anyway, and a
[gitleaks](https://github.com/gitleaks/gitleaks) pre-commit hook blocks commits
that contain anything that looks like a secret:

```
pip install pre-commit
pre-commit install
```

If a secret is ever committed by mistake, treat it as leaked: revoke it at the
issuing service first (for a Telegram bot: BotFather `/revoke`), then remove it
from the history.

## Design notes

- Sigil only **sends** Telegram messages to one configured chat. It runs no bot
  command handler, web server, webhook or open port.
- Course material and web pages are treated as untrusted. The model that writes
  guides gets no shell, may read only the files staged for it, runs in the
  CLI's safe mode (no user instructions, plugins, hooks or MCP servers), and
  has an allowlisted environment that holds no secret. The LaTeX it writes
  must use only allowlisted commands, environments and packages, none of
  which read or write files, and xelatex's own record of opened and written
  files is checked after every pass. Every built PDF is then checked before
  it is used: no JavaScript, embedded files, forms or
  automatic actions, no launch links except to sibling guide PDFs, https-only
  web links, no stray streams, and none of the known secret values in its
  bytes or text.
- Every request, redirect hops included, is https: Moodle calls only to the
  one configured Moodle host, and page/timetable fetches only to `auth.gr`
  hosts on port 443. Downloads are size- and time-capped, Office files are
  checked by their real decompressed size, and HTML is parsed in linear time.
- Guide generation is capped per day, so hostile material cannot run up
  unbounded model usage.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab →
"Report a vulnerability") instead of a public issue. You should get a reply
within a week.
