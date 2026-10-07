"""Where the study assistant's secrets live: the OS credential store, never a file.

On Windows that is Credential Manager (encrypted with the user's Windows login,
DPAPI); on macOS the Keychain; on Linux the Secret Service. No token, chat id
or password is ever written to disk by this project, so no file-read bug or
hostile document can read one back.

Lookup order for a secret NAME:
  1. the real process environment (for CI or a one-off run; never from .env,
     which `config` refuses to load secrets from);
  2. the credential store, service "sigil-study", user NAME.

Set them once with `python -m sigil.study_guides set-secret NAME`.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SERVICE = "sigil-study"
SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MOODLE_TOKEN",
                "ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD")


class SecretStoreError(RuntimeError):
    """The credential store could not be read or written."""


def _check(name: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}; expected one of {', '.join(SECRET_NAMES)}")


def get_secret(name: str) -> str:
    """The secret's value, or "" when it is not set (or the store is unavailable)."""
    _check(name)
    env = os.environ.get(name, "").strip()
    if env:
        return env
    try:
        import keyring
        return (keyring.get_password(SERVICE, name) or "").strip()
    except Exception as exc:  # noqa: BLE001 — no backend, locked store, ...
        log.warning("credential store unavailable for %s (%s)", name, type(exc).__name__)
        return ""


def set_secret(name: str, value: str) -> None:
    _check(name)
    value = (value or "").strip()
    if not value:
        raise ValueError("empty value; use delete_secret to remove a secret")
    try:
        import keyring
        keyring.set_password(SERVICE, name, value)
    except Exception as exc:  # noqa: BLE001
        raise SecretStoreError(f"could not save {name} ({type(exc).__name__})") from exc


def delete_secret(name: str) -> bool:
    """Remove a stored secret; False when there was none."""
    _check(name)
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(SERVICE, name)
            return True
        except PasswordDeleteError:
            return False
    except ImportError as exc:
        raise SecretStoreError("the keyring package is not installed") from exc


def status() -> dict[str, str]:
    """Which secrets are set and where ("env", "store" or ""); never the values."""
    out = {}
    for name in SECRET_NAMES:
        if os.environ.get(name, "").strip():
            out[name] = "env"
        else:
            out[name] = "store" if get_secret(name) else ""
    return out


def all_values() -> list[str]:
    """Every secret value currently available (for leak checks only)."""
    return [v for v in (get_secret(n) for n in SECRET_NAMES) if v]
