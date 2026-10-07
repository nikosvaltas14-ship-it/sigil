"""Finding and stopping external CLIs (the `claude` CLI, xelatex)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _cli_candidates(name: str) -> list[str]:
    """Places a CLI might live on Windows besides PATH (npm global, native installer)."""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [str(home / ".local" / "bin" / f"{name}.exe"),
            str(appdata / "npm" / f"{name}.cmd")]


def find_cli(name: str, override: str = "") -> str:
    """Full path to the CLI, or '' when it isn't installed."""
    if override:
        return override if (shutil.which(override) or Path(override).is_file()) else ""
    hit = shutil.which(name)
    if hit:
        return hit
    for cand in _cli_candidates(name):
        if Path(cand).is_file():
            return cand
    return ""


def kill_tree(proc) -> None:
    """Kill a process and everything it started.

    npm CLIs are `.cmd` shims launched through `cmd /c`, so `proc` is the
    cmd.exe wrapper: `proc.kill()` alone would leave node running. `taskkill
    /T` takes the whole tree down on Windows."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()          # the wrapper too, and the only step off Windows
    except OSError:
        pass
