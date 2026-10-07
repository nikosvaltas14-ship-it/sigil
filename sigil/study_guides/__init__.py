"""Daily study guides: tonight's lectures, from Moodle to a PDF in the vault.

For each course the student had today the pipeline reads the professor's posted
schedule on ΑΠΘ e-learning, picks that day's material, has Claude (the `claude`
CLI on the subscription) write a Greek LaTeX study guide, compiles it with
xelatex and files the PDF under `03 Resources/University/<Course>/`.

Entry points: `run` (the nightly pipeline, also `python -m sigil.study_guides
run`), `has_class_today` (a cheap gate), `summary_text` (the
one-line-per-course report), `discover` (first-time Moodle course mapping) and
`load_settings`.

Note: importing this package rebinds its `run` attribute to the *function*,
so `from sigil.study_guides import run` gives the function, not the module.
Reach the other names through the re-exports below (or
`from sigil.study_guides.run import ...`), never as `run.<name>`.
"""
from .run import discover, has_class_today, run, summary_text
from .settings import load_settings

__all__ = ["run", "has_class_today", "summary_text", "discover", "load_settings"]
