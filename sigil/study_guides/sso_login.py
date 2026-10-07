"""Moodle login through ΑΠΘ's single sign-on, in a small embedded browser.

ΑΠΘ refuses username/password at login/token.php (web logins go through the
central SSO), and its security-keys page lists no mobile-service key. So this
does what the official Moodle app does: open admin/tool/mobile/launch.php,
let you sign in on the real ΑΠΘ page, and catch the
`moodlemobile://token=<base64>` redirect Moodle answers with.

Design rules:

* **The password is typed into ΑΠΘ's own page, never into Sigil.** Only the
  token that comes back is kept, through the same `_verify_and_save` path as
  every other login (it must answer core_webservice_get_site_info first).
* **The token is read from the redirect header, not from the browser.** Qt
  parses `moodlemobile://token=<base64>` into an empty, invalid URL, so the
  link never survives the browser intact. Instead, whenever the browser tries
  to leave for that scheme (navigation request, interceptor, scheme handler,
  or the error page of a failed load), launch.php is replayed with httpx using
  the browser's Moodle session cookie, and the `Location` header is parsed.
* **The passport is checked.** The token blob is signed as
  md5(<site url> + <passport>); a blob whose signature does not match the
  passport this window sent is refused.
* **Nothing persists.** The browser profile is off-the-record, so no ΑΠΘ
  session cookie is left on disk.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
from urllib.parse import unquote

from . import moodle

log = logging.getLogger(__name__)

SCHEME = b"moodlemobile"
WINDOW_SIZE = (960, 760)
# How long the direct launch.php replay may take.
DIRECT_TIMEOUT_SEC = 15.0
# The token link as it appears inside a page body.
_LINK_IN_PAGE = re.compile(r"moodlemobile://token=[A-Za-z0-9+/=%]+")


def _check_blob(url: str, base: str, passport: str) -> str | None:
    """The token from a moodlemobile://token=... URL, if its signature matches."""
    blob = url.split("token=", 1)[-1] if "token=" in url else ""
    blob = unquote(blob).split("&", 1)[0].rstrip("/")
    try:
        decoded = base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    pieces = decoded.split(":::")
    if len(pieces) < 2:
        return None
    # Moodle signs with the site's wwwroot; accept it with or without a
    # trailing slash rather than guess which one this site stores.
    wanted = {hashlib.md5((root + passport).encode(), usedforsecurity=False).hexdigest()  # Moodle's scheme
              for root in (base, base.rstrip("/"), base.rstrip("/") + "/")}
    if pieces[0] not in wanted:
        log.warning("moodle sso: the token signature does not match this login's passport")
        return None
    return moodle._parse_pasted_token(pieces[1])


def browser_login(base_url: str, out=print) -> None:
    """Open the SSO login window; save the token it yields. Exits non-zero
    if the window is closed without one."""
    base = moodle._normalize_base(base_url)
    passport = secrets.token_hex(8)
    token = catch_token(moodle._launch_url(base, passport), base, passport, out=out)
    if not token:
        out("The login window was closed before Moodle returned a token. Nothing was saved.")
        raise SystemExit(moodle.EXIT_LOGIN_REJECTED)
    try:
        moodle._verify_and_save(base, token, None, out)
    except moodle.MoodleError as exc:
        log.error("moodle sso: the new token does not work: %s", exc)
        out(f"The token was not accepted: {exc}")
        raise SystemExit(moodle.EXIT_LOGIN_FAILED) from None


def catch_token(start_url: str, base: str, passport: str, *, out=print,
                show: bool = True, timeout_ms: int = 0) -> str | None:
    """Run the browser at `start_url` until a signed moodlemobile:// URL shows
    up (-> its token) or the window closes / `timeout_ms` passes (-> None)."""
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import (QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
                                         QWebEngineUrlRequestInterceptor, QWebEnginePage,
                                         QWebEngineProfile)

    # Must happen before the QApplication exists. No flags on purpose: a
    # LocalScheme may not be navigated to from web content, which is exactly
    # the https -> moodlemobile:// redirect this has to see (2026-09-25: the
    # first version showed Chromium's "might be temporarily down" page).
    if QWebEngineUrlScheme.schemeByName(SCHEME).name() != SCHEME:
        QWebEngineUrlScheme.registerScheme(QWebEngineUrlScheme(SCHEME))

    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    got: dict[str, str] = {}

    def capture(url: str, via: str = "") -> None:
        log.debug("moodle sso: %s saw a %s: URL", via, url.split(":", 1)[0])
        if got or not url.lower().startswith("moodlemobile:"):
            return
        token = _check_blob(url, base, passport)
        if token is None:
            out("Moodle answered, but the token could not be read.")
            return
        got["token"] = token
        QTimer.singleShot(0, view.close)
        QTimer.singleShot(0, app.quit)

    host = QUrl(base).host()
    cookies: dict[str, str] = {}

    def remember_cookie(cookie) -> None:
        domain = cookie.domain().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            cookies[bytes(cookie.name()).decode()] = bytes(cookie.value()).decode()

    def ask_directly(via: str) -> None:
        """Replay launch.php with the browser's Moodle session and read the
        redirect header ourselves. Qt turns `moodlemobile://token=<base64>`
        into an empty (invalid) QUrl — base64 in the authority part — so the
        link itself cannot be trusted to survive the browser."""
        if got or not any(n.lower().startswith("moodlesession") for n in cookies):
            return
        import httpx
        try:
            resp = httpx.get(start_url, cookies=cookies, follow_redirects=False,
                             timeout=DIRECT_TIMEOUT_SEC)
        except httpx.HTTPError as exc:
            log.info("moodle sso: direct launch request failed (%s)", type(exc).__name__)
            return
        location = resp.headers.get("location", "")
        if not location.lower().startswith("moodlemobile:"):
            # Moodle's iOS path answers 200 with a page that clicks the link.
            found = _LINK_IN_PAGE.search(resp.text or "")
            location = found.group(0) if found else location
        log.debug("moodle sso: %s direct request -> %s %s:", via, resp.status_code,
                  location.split(":", 1)[0])
        capture(location, "direct")

    class Handler(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):  # noqa: N802 — Qt override
            capture(job.requestUrl().toString(), "scheme handler")
            job.fail(job.Error.RequestAborted)
            QTimer.singleShot(0, lambda: ask_directly("scheme handler"))

    class Interceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):  # noqa: N802 — Qt override
            if info.requestUrl().scheme() == "moodlemobile":
                info.block(True)
                QTimer.singleShot(0, lambda: ask_directly("interceptor"))

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, is_main):  # noqa: N802
            if url.scheme() == "moodlemobile":
                capture(url.toString(), "navigation")
                QTimer.singleShot(0, lambda: ask_directly("navigation"))
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main)

    profile = QWebEngineProfile(app)          # no name = off the record
    profile.cookieStore().cookieAdded.connect(remember_cookie)
    handler, interceptor = Handler(app), Interceptor(app)
    profile.installUrlSchemeHandler(SCHEME, handler)
    profile.setUrlRequestInterceptor(interceptor)
    view = QWebEngineView()
    page = Page(profile, view)
    view.setPage(page)
    # Backstops: a failed load (Chromium's error page for the scheme) or any
    # URL change also tries, so no single hook has to fire.
    page.urlChanged.connect(lambda u: capture(u.toString(), "urlChanged"))
    page.loadFinished.connect(lambda ok: None if ok else ask_directly("failed load"))
    view.setWindowTitle("Sigil - Moodle login (ΑΠΘ)")
    view.resize(*WINDOW_SIZE)
    view.load(QUrl(start_url))
    if show:
        view.show()
        view.destroyed.connect(app.quit)
        app.setQuitOnLastWindowClosed(True)
    if timeout_ms:
        QTimer.singleShot(timeout_ms, app.quit)
    app.exec()
    return got.get("token")
