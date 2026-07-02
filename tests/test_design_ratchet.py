"""Design-system ratchet & lint tests (Phase 1 of the premium polish pass).

These tests are SOURCE-PARSING ONLY. They must never import Tk,
customtkinter, or wayfinder_main — they read the .py files as text / AST so
they can run headlessly and cheaply, and so they keep working even while the
15k-line legacy module is mid-refactor.

Purpose: ratchet the number of hardcoded design values (font sizes, corner
radii) downward over the design pass, and lint against banned timer patterns
(CTkProgressBar `.start()` indeterminate loops, and NEW self-rearming
sub-100ms `self.after` loops). Later phases (2 = fonts, 3 = radii) will drive
the baselines down toward explicit whitelists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "wayfinder_main.py"
THEME = REPO / "src" / "wayfinder" / "ui" / "theme.py"

MAIN_SRC = MAIN.read_text(encoding="utf-8")
THEME_SRC = THEME.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_dict_literal(src: str, name: str) -> dict:
    """Return the value of a module-level `NAME = {...}` pure-literal dict.

    Uses ast + literal_eval, so it only works on dicts whose values are
    literals (RADIUS/SPACING are int-valued literals — this is exactly what we
    want to compare across the two token mirrors)."""
    tree = ast.parse(src)
    for node in tree.body:
        # plain `NAME = {...}` (wayfinder_main.py)
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
        # annotated `NAME: dict[str, int] = {...}` (theme.py)
        elif isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} dict literal not found in source")


# ---------------------------------------------------------------------------
# (a) Font-literal ratchet
# ---------------------------------------------------------------------------
# Pragmatic single-line regex: matches `font=(...., <int>` where the size arg
# is an integer literal (not `self.font_sizes[...]`). Because `[^)]*` stops at
# the first ')', it CANNOT see font tuples that span multiple physical lines
# (`font=(\n   fam, 13\n)`), and it will not match token lookups like
# `font=(self.font_body[0], self.font_sizes["small"])` (no bare int after a
# comma). Validated against the live file: ~188 sites — plausible vs. the
# plan's ~190 hardcoded-font estimate.
FONT_LITERAL_RE = re.compile(r"font=\([^)]*,\s*[0-9]+")
FONT_LITERAL_BASELINE = 188  # Phase 2 will drive this toward a commented whitelist.


def test_font_literal_ratchet():
    count = len(FONT_LITERAL_RE.findall(MAIN_SRC))
    assert count <= FONT_LITERAL_BASELINE, (
        f"hardcoded font-size literals rose to {count} (baseline "
        f"{FONT_LITERAL_BASELINE}); use self.font_sizes[...] tokens instead. "
        "Phase 2 drives this down to a whitelist — never up."
    )


# ---------------------------------------------------------------------------
# (b) Radius-literal ratchet
# ---------------------------------------------------------------------------
RADIUS_LITERAL_RE = re.compile(r"corner_radius=[0-9]+")
RADIUS_LITERAL_BASELINE = 77  # Phase 3 will drive this toward a commented whitelist.


def test_radius_literal_ratchet():
    count = len(RADIUS_LITERAL_RE.findall(MAIN_SRC))
    assert count <= RADIUS_LITERAL_BASELINE, (
        f"hardcoded corner_radius literals rose to {count} (baseline "
        f"{RADIUS_LITERAL_BASELINE}); use corner_radius=RADIUS[...] tokens. "
        "Phase 3 drives this down to a whitelist — never up."
    )


# ---------------------------------------------------------------------------
# (c) Progressbar `.start()` lint
# ---------------------------------------------------------------------------
# CustomTkinter's CTkProgressBar indeterminate `.start()` re-arms via an
# internal `self.after(20, ...)` loop (verified in CTk 5.2.2) — that violates
# CLAUDE.md rule 1 (no timers < 100ms). Ban `.start()` on any variable whose
# name contains "progress" (progress/progressbar/progress_bar/...). Thread
# `.start()` calls (progress_thread.start()) are intentionally NOT excluded:
# there are none in the file today, and if one were added we'd rather see it
# and tighten the pattern than let a real progressbar loop slip through.
PROGRESS_START_RE = re.compile(r"progress\w*\.start\(")


def test_no_progressbar_start_loop():
    hits = [
        (i + 1, ln.strip())
        for i, ln in enumerate(MAIN_SRC.splitlines())
        if PROGRESS_START_RE.search(ln)
    ]
    assert not hits, (
        "CTkProgressBar `.start()` indeterminate loop is banned (20ms internal "
        f"re-arm). Use mode='indeterminate' + a >=100ms `.step()` tick. Hits: {hits}"
    )


# ---------------------------------------------------------------------------
# (d) Repeating sub-100ms self-rearming timer lint
# ---------------------------------------------------------------------------
# A "self-rearming" timer is `self.after(<int>, self.<name>)` issued from
# INSIDE the method named <name> — i.e. the callback re-schedules itself. Only
# these true loops matter; one-shot `self.after(0, self.<other>)` thread
# marshaling (callback != enclosing method) is explicitly NOT flagged.
#
# The grandfathered whitelist below is every self-rearming loop that fires
# faster than 100ms in the live app today. Four are contractual per CLAUDE.md
# rule 9 (hero 30fps=33ms idle / 15fps=66ms, tray/mic feedback); one
# (_animate_emojis, 16ms confetti) is a known rule violation slated for
# deletion in Phase 11. The test's job is to make any NEW sub-100ms
# self-rearming loop fail the suite.
GRANDFATHERED_SUB100_SELF_REARM = {
    "_animate_hero",        # hero waveform, 66ms = 15fps active (rule 9)
    "_animate_idle_breath",  # hero idle breath, 33ms = 30fps idle (rule 9)
    "_tray_pulse_step",     # tray recording pulse, 50ms (deliberate, CPU-validated)
    "_update_mic_test",     # calibration level meter, 50ms
    "_animate_emojis",      # confetti easter egg, 16ms — Phase 11 deletes this
}


def _self_after_self_rearms(src: str):
    """Yield (method_name, interval) for every `self.after(<int>, self.<name>)`
    call issued from inside the method `<name>` (a true self-rearm)."""
    tree = ast.parse(src)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []
            self.found: list[tuple[str, int]] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "after"
                and isinstance(f.value, ast.Name)
                and f.value.id == "self"
                and len(node.args) >= 2
            ):
                interval_node, cb_node = node.args[0], node.args[1]
                if (
                    isinstance(interval_node, ast.Constant)
                    and isinstance(interval_node.value, int)
                    and interval_node.value < 100
                    and isinstance(cb_node, ast.Attribute)
                    and isinstance(cb_node.value, ast.Name)
                    and cb_node.value.id == "self"
                ):
                    cb = cb_node.attr
                    if self.stack and self.stack[-1] == cb:
                        self.found.append((cb, interval_node.value))
            self.generic_visit(node)

    v = Visitor()
    v.visit(tree)
    return v.found


def test_no_new_sub100_self_rearming_timers():
    rearms = _self_after_self_rearms(MAIN_SRC)
    offenders = {name for name, _ in rearms} - GRANDFATHERED_SUB100_SELF_REARM
    assert not offenders, (
        "NEW self-rearming sub-100ms `self.after` loop(s) detected: "
        f"{sorted(offenders)}. Rule 1 bans timers < 100ms; if this is a "
        "deliberate animation, justify it and add to the whitelist."
    )
    # Sanity: the grandfathered loops we documented still exist (guards against
    # the regex silently breaking and the test becoming a no-op).
    assert {name for name, _ in rearms}, "timer lint found zero self-rearms — regex likely broke"


# ---------------------------------------------------------------------------
# (e) Token-mirror parity — RADIUS & SPACING must match theme.py exactly
# ---------------------------------------------------------------------------
# COLORS/FONT_SIZES intentionally diverge between the two mirrors today, so
# only RADIUS and SPACING are checked here.

def test_radius_mirror_parity():
    main_radius = _extract_dict_literal(MAIN_SRC, "RADIUS")
    theme_radius = _extract_dict_literal(THEME_SRC, "RADIUS")
    assert main_radius == theme_radius, (
        "RADIUS token mirror drifted between wayfinder_main.py and theme.py:\n"
        f"  main : {main_radius}\n  theme: {theme_radius}"
    )


def test_spacing_mirror_parity():
    main_spacing = _extract_dict_literal(MAIN_SRC, "SPACING")
    theme_spacing = _extract_dict_literal(THEME_SRC, "SPACING")
    assert main_spacing == theme_spacing, (
        "SPACING token mirror drifted between wayfinder_main.py and theme.py:\n"
        f"  main : {main_spacing}\n  theme: {theme_spacing}"
    )
