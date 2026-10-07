"""Read-only client for ΑΠΘ e-learning (Moodle) over its web-service REST API.

The official mobile app works with a student account, so the `moodle_mobile_app`
service is enabled, and everything this feature needs is available through it
without scraping a single HTML page:

  core_webservice_get_site_info   -> userid, and proof the token still works
  core_enrol_get_users_courses    -> the enrolled courses and their ids
  core_course_get_contents        -> sections (name, HTML summary) and modules,
                                     whose `contents[]` carry the file URLs
  <fileurl>?token=...             -> the file itself (webservice/pluginfile.php)

Design rules:

* **Read only.** Only the three functions above and file downloads. Nothing
  here submits, posts or changes anything on the site.
* **The password is never kept.** `interactive_login` asks for it with getpass,
  trades it once for a token at login/token.php and drops it. Only the token is
  stored, in the OS credential store (`secrets_store`), never in a file.
* **The token never reaches a log or an exception.** REST calls send it in a
  POST body; downloads need it in the query string, so every URL this module
  logs goes through `_redact`, a filter scrubs httpx's own "HTTP Request: ..."
  lines, and a download refuses any URL on a host other than the site's —
  a file link pointing elsewhere must not receive the token.
* **Moodle reports failure inside a 200.** A web-service error is a JSON body
  with `exception`/`errorcode`/`message`, and token.php answers `{"error": ...}`;
  both are checked explicitly rather than trusting the status code.
* **An invalid token is re-read once, then fails loudly.** Without a stored
  password there is nothing to refresh *with*, so the "refresh" is a re-read of
  the token file (a `login` run in another window may have replaced it). If
  that does not help, `InvalidToken` says to run `login` again.
* **Polite and patient.** A fixed delay between requests, and retries with
  exponential backoff on network errors, 429 and 5xx — never on 4xx.
"""
from __future__ import annotations

import base64
import binascii
import getpass
import logging
import os
import re
import secrets
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import httpx

from .models import FileRef

log = logging.getLogger(__name__)

DEFAULT_SERVICE = "moodle_mobile_app"
TOKEN_NAME = "MOODLE_TOKEN"    # its name in the credential store
REST_PATH = "/webservice/rest/server.php"
TOKEN_ENDPOINT = "/login/token.php"
USER_AGENT = "Sigil-study-guides/1.0 (personal, read-only)"

# Moodle's errorcode for a token that is unknown, expired or revoked.
INVALID_TOKEN_CODE = "invalidtoken"
# Statuses worth another try: rate limiting and server-side trouble.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_BACKOFF_SEC = 1.0         # first retry waits this, then doubles
MAX_BACKOFF_SEC = 30.0            # never wait longer than this between tries
LOGIN_TIMEOUT_SEC = 30.0
DOWNLOAD_CHUNK = 64 * 1024
# A lecture PDF is a few MB; a 200 MB "file" is a video or a mistake, and the
# material stage could not use it anyway.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
# Only resources and folders hold lecture material. Page/book modules also list
# an index.html in contents[], but their text reaches the schedule parser
# through the module description instead.
FILE_MODULES = frozenset({"resource", "folder"})
# A Moodle token is 32 hex chars; accept a little wider in case a site differs,
# but never whitespace or punctuation (that is a paste accident).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")
_SECRET_QS_RE = re.compile(r"((?:ws|private)?token=)[^&\s'\"<>]+", re.IGNORECASE)
# The Telegram Bot API puts its token in the URL path: .../bot<id>:<secret>/...
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_LAUNCH_RE = re.compile(r"^[a-z][a-z0-9+.-]*://token=(?P<blob>\S+)$", re.IGNORECASE)

EXIT_LOGIN_FAILED = 1             # network / site trouble
EXIT_LOGIN_REJECTED = 2           # the site refused the credentials


class MoodleError(RuntimeError):
    """Anything that went wrong talking to Moodle. Messages are token-free."""


class InvalidToken(MoodleError):
    """The stored token is unknown, expired or revoked: run `login` again."""


class LoginRejected(MoodleError):
    """login/token.php refused the credentials (or the site wants SSO)."""


# --------------------------------------------------------------------- redaction

def _redact(text: object) -> str:
    """`text` with every token=/wstoken=/privatetoken= value, and a Telegram
    bot token in a URL path, replaced by ***."""
    return _BOT_TOKEN_RE.sub("bot***", _SECRET_QS_RE.sub(r"\1***", str(text)))


class _RedactTokens(logging.Filter):
    """Scrubs token query parameters from httpx's request log lines, which
    print the full URL at INFO — and a download URL carries the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact(a) if isinstance(a, (str, httpx.URL)) else a
                for a in record.args)
        return True


def _install_log_redaction() -> None:
    httpx_log = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactTokens) for f in httpx_log.filters):
        httpx_log.addFilter(_RedactTokens())


_install_log_redaction()


# ------------------------------------------------------------------ token store

def _normalize_base(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc:
        # The password and the token must never travel in clear text.
        raise MoodleError(f"Moodle URL must be https://..., got {base_url!r}")
    return base


def _valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


def save_token(token: str) -> None:
    """Store the web-service token in the OS credential store (never a file)."""
    from ..secrets_store import set_secret
    token = (token or "").strip()
    if not _valid_token(token):
        raise ValueError("that does not look like a Moodle token "
                         "(expected 16-128 letters/digits)")
    set_secret(TOKEN_NAME, token)
    log.info("moodle: token saved to the credential store")


def load_token() -> str | None:
    """The token from the environment or the credential store; None when there
    is none (or what is stored is not a token)."""
    from ..secrets_store import get_secret
    token = get_secret(TOKEN_NAME)
    if not token:
        return None
    if not _valid_token(token):
        log.warning("moodle: the stored %s is not a token; ignoring it", TOKEN_NAME)
        return None
    return token


def fetch_token(base_url: str, username: str, password: str,
                service: str = DEFAULT_SERVICE,
                transport: httpx.BaseTransport | None = None) -> str:
    """Trade username+password for a web-service token at login/token.php.

    Raises LoginRejected when the site answers with `{"error": ...}` or with
    something that is not JSON at all (an SSO redirect or login page), and
    MoodleError for network or server trouble. The password is only ever put
    in the POST body of this one request.
    """
    base = _normalize_base(base_url)
    url = base + TOKEN_ENDPOINT
    form = {"username": username, "password": password, "service": service}
    try:
        with httpx.Client(transport=transport, timeout=LOGIN_TIMEOUT_SEC,
                          headers={"User-Agent": USER_AGENT}) as http:
            resp = http.post(url, data=form)
    except httpx.HTTPError as exc:
        raise MoodleError(f"could not reach {url}: {type(exc).__name__}") from None
    if resp.is_redirect:
        raise LoginRejected(f"{url} redirected to "
                            f"{_redact(resp.headers.get('location', '?'))} — "
                            "the site probably requires SSO for this login")
    if resp.status_code >= 500:
        raise MoodleError(f"{url} answered HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise LoginRejected(f"{url} did not answer with JSON (HTTP "
                            f"{resp.status_code}) — the site probably requires SSO") from None
    if not isinstance(payload, dict):
        raise LoginRejected(f"{url} answered with unexpected JSON")
    token = payload.get("token")
    if isinstance(token, str) and _valid_token(token):
        return token
    code = payload.get("errorcode") or "error"
    message = payload.get("error") or payload.get("message") or "no token in the reply"
    raise LoginRejected(f"{code}: {message}")


# ------------------------------------------------------------------ the client

def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Moodle REST form encoding: lists/dicts become name[0][key]=value."""
    flat: dict[str, str] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            flat.update(_flatten(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            flat[name] = "1" if value else "0"
        elif value is not None:
            flat[name] = str(value)
    return flat


def _ws_error(payload: Any) -> tuple[str, str] | None:
    """(errorcode, message) when `payload` is a Moodle exception, else None."""
    if isinstance(payload, dict) and "exception" in payload:
        return (str(payload.get("errorcode") or "unknown"),
                str(payload.get("message") or payload.get("exception")))
    if isinstance(payload, dict) and "errorcode" in payload and "error" in payload:
        # webservice/pluginfile.php uses token.php's {"error", "errorcode"} shape.
        return str(payload["errorcode"]), str(payload["error"])
    return None


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "")
    # ASCII digits only: str.isdigit() also accepts "²", which float() rejects.
    return float(value) if value.isascii() and value.isdigit() else None


class MoodleClient:
    """One Moodle site, one token. Use as a context manager, or call close().

    `retries` is how many extra attempts a request gets after the first one
    fails with a network error, 429 or 5xx. `token_source` is what an
    `invalidtoken` answer re-reads (the token file by default; tests pass a
    stub). `backoff` is the first retry delay in seconds.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 delay: float = 0.5, retries: int = 3,
                 transport: httpx.BaseTransport | None = None, *,
                 backoff: float = DEFAULT_BACKOFF_SEC,
                 token_source: Callable[[], str | None] | None = load_token) -> None:
        if not _valid_token(token):
            raise InvalidToken("no valid Moodle token — run "
                               "`python -m sigil.study_guides login` first")
        self.base_url = _normalize_base(base_url)
        self._host = urlsplit(self.base_url).netloc.lower()
        self._token = token
        self._delay = max(0.0, delay)
        self._retries = max(0, retries)
        self._backoff = max(0.0, backoff)
        self._token_source = token_source
        self._last_request = 0.0
        self._sleep = time.sleep
        self._http = httpx.Client(transport=transport, timeout=timeout,
                                  headers={"User-Agent": USER_AGENT},
                                  follow_redirects=False, max_redirects=5,
                                  event_hooks={"request": [self._same_site_only]})

    def _same_site_only(self, request: httpx.Request) -> None:
        """Every request, redirect hops included, goes to the Moodle site over
        https; a redirect elsewhere (SSRF, a downgrade) is refused."""
        if request.url.scheme != "https" or request.url.netloc.decode().lower() != self._host:
            raise MoodleError(f"refusing a request outside the site: {_redact(request.url)}")

    def __enter__(self) -> "MoodleClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- web-service calls ------------------------------------------------

    def call(self, wsfunction: str, **params: Any) -> Any:
        """Call one web-service function and return its decoded JSON."""
        for attempt in range(2):
            payload = self._post_ws(wsfunction, params)
            error = _ws_error(payload)
            if error is None:
                return payload
            code, message = error
            if code != INVALID_TOKEN_CODE:
                raise MoodleError(f"{wsfunction}: {code}: {self._scrub(message)}")
            if attempt == 0 and self._reload_token():
                log.warning("moodle: %s said invalidtoken; retrying once with "
                            "the token re-read from disk", wsfunction)
                continue
            raise InvalidToken(f"{wsfunction}: the Moodle token was rejected "
                               f"({self._scrub(message)}) — run "
                               "`python -m sigil.study_guides login` again")
        raise AssertionError("unreachable")

    def site_info(self) -> dict:
        info = self.call("core_webservice_get_site_info")
        if not isinstance(info, dict) or not isinstance(info.get("userid"), int):
            raise MoodleError("core_webservice_get_site_info: no userid in the reply")
        return info

    def user_courses(self, userid: int) -> list[dict]:
        courses = self.call("core_enrol_get_users_courses", userid=int(userid))
        if not isinstance(courses, list):
            raise MoodleError("core_enrol_get_users_courses: expected a list")
        return [c for c in courses if isinstance(c, dict) and "id" in c]

    def course_contents(self, courseid: int) -> list[dict]:
        sections = self.call("core_course_get_contents", courseid=int(courseid))
        if not isinstance(sections, list):
            raise MoodleError(f"core_course_get_contents({courseid}): expected a list")
        return [s for s in sections if isinstance(s, dict)]

    # -- file download ----------------------------------------------------

    def download(self, fileurl: str, dest: Path) -> Path:
        """Fetch one course file to `dest` (atomically, via a .part file)."""
        url = self._file_url(fileurl)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        resp = self._send("GET", url, stream=True, follow_redirects=True)
        try:
            self._check_status(resp, url)
            self._reject_error_body(resp, dest.name)
            size = self._stream_to(resp, part)
        except BaseException:
            _unlink_quietly(part)
            raise
        finally:
            resp.close()
        os.replace(part, dest)
        log.info("moodle: downloaded %s (%d bytes)", dest.name, size)
        return dest

    def _file_url(self, fileurl: str) -> httpx.URL:
        parts = urlsplit(fileurl or "")
        if parts.scheme != "https" or parts.netloc.lower() != self._host:
            # Never hand the token to a host that is not the Moodle site.
            raise MoodleError(f"refusing to download from outside the site: "
                              f"{_redact(fileurl)}")
        url = httpx.URL(fileurl)
        path = url.path
        if "/pluginfile.php/" in path and "/webservice/pluginfile.php/" not in path:
            # The token only works on the web-service variant of pluginfile.
            url = url.copy_with(path=path.replace("/pluginfile.php/",
                                                  "/webservice/pluginfile.php/", 1))
        params = [(k, v) for k, v in url.params.multi_items() if k.lower() != "token"]
        return url.copy_with(params=params).copy_merge_params({"token": self._token})

    def _reject_error_body(self, resp: httpx.Response, filename: str) -> None:
        """Moodle answers a bad download with a JSON error or an HTML page."""
        ctype = resp.headers.get("content-type", "").lower()
        wanted = filename.lower()
        if "application/json" in ctype and not wanted.endswith(".json"):
            resp.read()
            try:
                error = _ws_error(resp.json())
            except ValueError:
                error = None
            if error and error[0] == INVALID_TOKEN_CODE:
                raise InvalidToken(f"download of {filename}: the Moodle token "
                                   "was rejected — run `login` again")
            raise MoodleError(f"download of {filename}: Moodle answered "
                              f"{error[0] + ': ' + self._scrub(error[1]) if error else 'JSON'}")
        if "text/html" in ctype and not wanted.endswith((".htm", ".html")):
            raise MoodleError(f"download of {filename}: got an HTML page instead "
                              "of the file (login page, or the file is not visible)")

    @staticmethod
    def _stream_to(resp: httpx.Response, part: Path) -> int:
        size = 0
        with open(part, "wb") as fh:
            for chunk in resp.iter_bytes(DOWNLOAD_CHUNK):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise MoodleError(f"{part.stem}: larger than "
                                      f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB, skipped")
                fh.write(chunk)
        return size

    # -- transport --------------------------------------------------------

    def _post_ws(self, wsfunction: str, params: dict[str, Any]) -> Any:
        url = self.base_url + REST_PATH
        form = {"wstoken": self._token, "wsfunction": wsfunction,
                "moodlewsrestformat": "json", **_flatten(params)}
        resp = self._send("POST", url, data=form)
        self._check_status(resp, url)
        try:
            return resp.json()
        except ValueError:
            # A maintenance page or a proxy error, not the web service.
            raise MoodleError(f"{wsfunction}: Moodle did not answer with JSON "
                              f"(HTTP {resp.status_code})") from None

    def _send(self, method: str, url: str | httpx.URL, *,
              data: dict | None = None, stream: bool = False,
              follow_redirects: bool = False) -> httpx.Response:
        """One request with throttling and retry/backoff. Returns the response
        of the first attempt that is not retryable; raises after the last."""
        attempts = self._retries + 1
        why = ""
        wait_hint: float | None = None
        for attempt in range(attempts):
            if attempt:
                wait = min(self._backoff * 2 ** (attempt - 1), MAX_BACKOFF_SEC)
                self._sleep(min(max(wait, wait_hint or 0.0), MAX_BACKOFF_SEC))
            self._throttle()
            try:
                request = self._http.build_request(method, url, data=data)
                resp = self._http.send(request, stream=stream,
                                       follow_redirects=follow_redirects)
            except httpx.TransportError as exc:
                why, wait_hint = f"{type(exc).__name__}: {self._scrub(exc)}", None
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                why, wait_hint = f"HTTP {resp.status_code}", _retry_after(resp)
                resp.close()
            log.warning("moodle: %s %s failed (%s), attempt %d/%d",
                        method, _redact(url), why, attempt + 1, attempts)
        raise MoodleError(f"{method} {_redact(url)} failed after {attempts} "
                          f"attempts ({why})")

    def _check_status(self, resp: httpx.Response, url: str | httpx.URL) -> None:
        if resp.is_success:
            return
        resp.close()
        if resp.status_code in (401, 403):
            raise MoodleError(f"{_redact(url)}: HTTP {resp.status_code} "
                              "(no access — is the token still valid?)")
        raise MoodleError(f"{_redact(url)}: HTTP {resp.status_code}")

    def _throttle(self) -> None:
        wait = self._last_request + self._delay - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def _reload_token(self) -> bool:
        """Re-read the token source; True when it now holds a different token."""
        if self._token_source is None:
            return False
        try:
            fresh = self._token_source()
        except Exception as exc:  # a broken source must not hide the real error
            log.warning("moodle: re-reading the token failed: %s", type(exc).__name__)
            return False
        if fresh and fresh != self._token and _valid_token(fresh):
            self._token = fresh
            return True
        return False

    def _scrub(self, text: object) -> str:
        """`text` with query-string tokens and the literal token removed."""
        return _redact(text).replace(self._token, "***")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("moodle: could not remove partial download %s: %s", path, exc)


# ------------------------------------------------------------ content helpers

def iter_files(contents: list[dict]) -> list[FileRef]:
    """Every downloadable file of the course, in page order, de-duplicated.

    Only `type == "file"` entries of resource/folder modules the user can see;
    URL modules point outside Moodle and hidden modules cannot be fetched.
    """
    refs: list[FileRef] = []
    seen: set[str] = set()
    for section in contents or []:
        if not isinstance(section, dict) or section.get("uservisible") is False:
            continue
        for module in section.get("modules") or []:
            if not _is_file_module(module):
                continue
            for item in module.get("contents") or []:
                ref = _file_ref(section, module, item)
                if ref is not None and ref.fileurl not in seen:
                    seen.add(ref.fileurl)
                    refs.append(ref)
    return refs


def _is_file_module(module: Any) -> bool:
    return (isinstance(module, dict)
            and module.get("modname") in FILE_MODULES
            and module.get("uservisible", True) is not False)


def _file_ref(section: dict, module: dict, item: Any) -> FileRef | None:
    if not isinstance(item, dict) or item.get("type") != "file":
        return None
    fileurl, filename = item.get("fileurl"), item.get("filename")
    if not fileurl or not filename:
        log.debug("moodle: module %s has a file entry without url/name", module.get("id"))
        return None
    try:
        return FileRef(
            module_id=int(module.get("id") or 0),
            section_id=int(section.get("id") or 0),
            section_name=str(section.get("name") or ""),
            filename=str(filename),
            fileurl=str(fileurl),
            timemodified=int(item.get("timemodified") or 0),
            mimetype=str(item.get("mimetype") or ""),
            filesize=int(item.get("filesize") or 0),
        )
    except (TypeError, ValueError) as exc:
        log.warning("moodle: skipping malformed file entry %r in module %s: %s",
                    filename, module.get("id"), exc)
        return None


# Tags that end a line of text, and those whose content is never text.
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "table", "ul", "ol", "section", "article",
    "blockquote", "pre", "hr", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6",
})
_SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "template"})
_CELL_TAGS = frozenset({"td", "th"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skipping += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _CELL_TAGS:
            self.parts.append(" | ")      # a schedule table keeps its columns
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


# Course pages and posts are untrusted; this bounds the work one can cause.
MAX_HTML_CHARS = 2_000_000


def _defuse_html(html: str) -> str:
    """`html` with comments dropped and every '<' that does not open a tag
    closed before the next '<' escaped, in one linear pass.

    Python's HTMLParser is quadratic on unclosed constructs ("<a <a <a ...",
    "<!--<!--..."): 60 KB of them takes minutes. After this pass every tag it
    sees is bounded, so parsing stays linear. Text extraction loses nothing.
    """
    out: list[str] = []
    i, n = 0, len(html)
    next_gt = -2                      # cached position of the next '>' (-1: none left)
    while i < n:
        j = html.find("<", i)
        if j < 0:
            out.append(html[i:])
            break
        out.append(html[i:j])
        if html.startswith("<!--", j):
            end = html.find("-->", j + 4)
            if end < 0:
                break                 # unclosed comment: the rest is comment
            i = end + 3
            continue
        if next_gt != -1 and next_gt <= j:
            next_gt = html.find(">", j + 1)
        following_lt = html.find("<", j + 1)
        if next_gt < 0 or (0 <= following_lt < next_gt):
            out.append("&lt;")
            i = j + 1
            continue
        out.append(html[j:next_gt + 1])
        i = next_gt + 1
    return "".join(out)


def html_to_text(html: str | None) -> str:
    """Readable plain text from a Moodle HTML summary/description.

    Lines follow block elements, list items get "- ", table cells are joined
    with " | ", entities are decoded and blank lines are dropped (a schedule
    reads one entry per line; paragraph spacing carries no meaning here).
    """
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(_defuse_html(str(html)[:MAX_HTML_CHARS]))
    parser.close()
    lines = []
    for raw in "".join(parser.parts).split("\n"):
        line = re.sub(r"[^\S\n]+", " ", raw).strip()
        line = re.sub(r"^\|\s*", "", line)
        if line:
            lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------- interactive login

def _launch_url(base: str, passport: str) -> str:
    return (f"{base}/admin/tool/mobile/launch.php?service={DEFAULT_SERVICE}"
            f"&passport={passport}&urlscheme=moodlemobile")


def _sso_help(base: str, passport: str) -> str:
    return "\n".join([
        "",
        "The site refused a username/password login. ΑΠΘ probably routes web",
        "logins through central SSO. Two ways to get a token without it:",
        "",
        " (a) Security keys page: log in on the website, open",
        f"       {base}/user/managetoken.php",
        "     and copy the key for 'Moodle mobile web service' (if listed).",
        "",
        " (b) Mobile-app SSO launch: while logged in on the website, open",
        f"       {_launch_url(base, passport)}",
        "     The browser then tries to open a 'moodlemobile://token=...' link",
        "     it cannot handle. Copy that whole link (from the address bar, the",
        "     'open app?' prompt or the dev-tools Network tab) and paste it here.",
        "",
    ])


def _parse_pasted_token(text: str) -> str | None:
    """A bare token, or the token inside a moodlemobile://token=<base64> link
    (base64 of "<signature>:::<token>[:::<privatetoken>]")."""
    text = (text or "").strip()
    if _valid_token(text):
        return text
    match = _LAUNCH_RE.match(text)
    if not match:
        return None
    blob = unquote(match.group("blob")).split("&", 1)[0]
    try:
        decoded = base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    pieces = decoded.split(":::")
    return pieces[1] if len(pieces) >= 2 and _valid_token(pieces[1]) else None


def _verify_and_save(base: str, token: str, transport: httpx.BaseTransport | None,
                     out: Callable[..., None]) -> None:
    with MoodleClient(base, token, transport=transport, token_source=None) as client:
        info = client.site_info()
    save_token(token)
    out(f"Logged in to {info.get('sitename', base)} as "
        f"{info.get('fullname', info.get('username', '?'))}. Token saved.")


def _manual_token(base: str, transport: httpx.BaseTransport | None,
                  secret_prompt: Callable[[str], str],
                  out: Callable[..., None]) -> None:
    out(_sso_help(base, secrets.token_hex(8)))
    pasted = secret_prompt("Paste the token or the moodlemobile:// link "
                           "(empty to give up): ")
    if not pasted.strip():
        out("No token entered. Nothing was saved.")
        raise SystemExit(EXIT_LOGIN_REJECTED)
    token = _parse_pasted_token(pasted)
    if token is None:
        out("That is neither a token nor a moodlemobile://token=... link.")
        raise SystemExit(EXIT_LOGIN_REJECTED)
    _verify_and_save(base, token, transport, out)


def _password_token(base: str, transport: httpx.BaseTransport | None,
                    prompt: Callable[[str], str],
                    secret_prompt: Callable[[str], str]) -> str:
    """Ask for the credentials and trade them for a token. The password lives
    only in this frame and the one POST body."""
    username = prompt(f"Moodle username for {base}: ").strip()
    password = secret_prompt("Password (not stored): ")
    try:
        return fetch_token(base, username, password, transport=transport)
    finally:
        del password


def masked_input(label: str) -> str:
    """Read a secret, echoing `*` per character.

    Plain getpass shows nothing at all while typing, which reads as "it isn't
    taking my password". On Windows consoles this echoes a
    star per key instead; anywhere else it falls back to getpass.
    """
    try:
        import msvcrt
    except ImportError:
        return getpass.getpass(label)
    print(label, end="", flush=True)
    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            print()
            return "".join(chars)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x08":
            if chars:
                chars.pop()
                print("\b \b", end="", flush=True)
        elif ch in ("\x00", "\xe0"):
            msvcrt.getwch()   # arrow/function key: swallow its second code
        else:
            chars.append(ch)
            print("*", end="", flush=True)


def interactive_login(base_url: str, *, prompt: Callable[[str], str] = input,
                      secret_prompt: Callable[[str], str] = masked_input,
                      out: Callable[..., None] = print,
                      transport: httpx.BaseTransport | None = None) -> None:
    """One-time login: ask for username/password, keep only the token.

    When token.php refuses (SSO), print the two fallbacks from the spec and
    accept a pasted token or launch link instead; if none is given, exit
    non-zero. Every token is checked with core_webservice_get_site_info before
    it is saved, so a typo never replaces a working token.
    """
    try:
        base = _normalize_base(base_url)
        try:
            token = _password_token(base, transport, prompt, secret_prompt)
        except LoginRejected as exc:
            log.warning("moodle: token.php rejected the login: %s", exc)
            out(f"Login rejected: {exc}")
            _manual_token(base, transport, secret_prompt, out)
            return
        _verify_and_save(base, token, transport, out)
    except InvalidToken as exc:
        log.error("moodle: the new token does not work: %s", exc)
        out(f"The token was not accepted: {exc}")
        raise SystemExit(EXIT_LOGIN_REJECTED) from None
    except MoodleError as exc:
        log.error("moodle: login failed: %s", exc)
        out(f"Login failed: {exc}")
        raise SystemExit(EXIT_LOGIN_FAILED) from None
