"""The sample configuration (config.example.json over the built-in defaults)."""
import json
from pathlib import Path

from sigil.config import DEFAULTS

_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"
SAMPLE = {**DEFAULTS, **{k: v for k, v in json.loads(_EXAMPLE.read_text(encoding="utf-8")).items()
                         if not k.startswith("_")}}
