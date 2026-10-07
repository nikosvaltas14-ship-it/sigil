"""Every test run writes its runtime data into a throwaway folder, never into
the real per-user data folder (set before `sigil.config` is imported)."""
import os
import tempfile

os.environ["SIGIL_DATA_DIR"] = tempfile.mkdtemp(prefix="sigil-tests-")
for _name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MOODLE_TOKEN",
              "ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD"):
    os.environ[_name] = ""      # set (empty) so a real .env is never loaded into a test

import keyring  # noqa: E402
import pytest  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """A credential store that lives in memory: tests never touch the real one."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.data:
            raise PasswordDeleteError(username)
        del self.data[(service, username)]


_STORE = MemoryKeyring()
keyring.set_keyring(_STORE)


@pytest.fixture(autouse=True)
def _empty_secret_store():
    _STORE.data.clear()
    yield _STORE
    _STORE.data.clear()
