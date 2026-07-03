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
# (a) Font-literal whitelist (Phase 2: type-scale sweep complete)
# ---------------------------------------------------------------------------
# Pragmatic single-line regex: matches `font=(...., <int>` where the size arg
# is an integer literal (not `self.font_sizes[...]`). Because `[^)]*` stops at
# the first ')', it CANNOT see font tuples that span multiple physical lines
# (`font=(\n   fam, 13\n)`), and it will not match token lookups like
# `font=(self.font_body[0], self.font_sizes["small"])` (no bare int after a
# comma).
#
# Phase 2 swept ~190 hardcoded font sizes onto the 6-token ramp
# (self.font_sizes). Every REMAINING literal must carry one of exactly two
# sanctioned trailing comments, or the sweep has regressed:
#   * "# optical glyph size"           — single-glyph buttons (× − + ⟲ ←) and
#                                        hero value numerals, sized for optical
#                                        balance rather than the text ramp.
#   * "# module-scope: no font_sizes access" — font sites in module-level
#                                        helper classes (ToolTip, ConfettiOverlay,
#                                        CompatibilityBanner, ModeSelector,
#                                        FloatingIndicator) that live OUTSIDE
#                                        WayfinderApp and thus have no
#                                        self.font_sizes to reference.
FONT_LITERAL_RE = re.compile(r"font=\([^)]*,\s*[0-9]+")
SANCTIONED_FONT_COMMENTS = ("# optical glyph size", "# module-scope: no font_sizes access")
# Post-sweep count is the whitelist size. It may only shrink (delete a glyph /
# rewrite a module-scope class), never grow — a new bare literal fails below.
FONT_WHITELIST_MAX = 22


def _line_of(offset: int) -> str:
    """Return the full physical line containing the given source offset."""
    start = MAIN_SRC.rfind("\n", 0, offset) + 1
    end = MAIN_SRC.find("\n", offset)
    return MAIN_SRC[start: end if end != -1 else len(MAIN_SRC)]


def test_font_literals_are_whitelisted():
    unsanctioned = []
    total = 0
    for m in FONT_LITERAL_RE.finditer(MAIN_SRC):
        total += 1
        line = _line_of(m.start())
        if not any(c in line for c in SANCTIONED_FONT_COMMENTS):
            lineno = MAIN_SRC.count("\n", 0, m.start()) + 1
            unsanctioned.append((lineno, line.strip()))
    assert not unsanctioned, (
        "hardcoded font-size literal(s) without a sanctioned whitelist comment "
        f"({SANCTIONED_FONT_COMMENTS}); use self.font_sizes[...] tokens instead:\n"
        + "\n".join(f"  {ln}: {txt}" for ln, txt in unsanctioned)
    )
    assert total <= FONT_WHITELIST_MAX, (
        f"font-literal whitelist grew to {total} (max {FONT_WHITELIST_MAX}); the "
        "whitelist may only shrink. New styled text must use self.font_sizes[...]."
    )


# ---------------------------------------------------------------------------
# (b) Radius-literal whitelist (Phase 3: radius sweep complete)
# ---------------------------------------------------------------------------
# Phase 3 swept 77 hardcoded corner_radius values onto the RADIUS tokens
# (xs=6/sm=8/md=12/lg=24/xl=28) for one corner language: 6→xs, 8→sm, 12→md
# (pixel-identical), 10→md (the deliberate +2px unification), one-off 5→xs and
# 24→lg. Every REMAINING literal must carry one of exactly three sanctioned
# trailing comments, or the sweep has regressed:
#   * "# pill: height/2, intentional off-token"     — CTkSwitch pills whose
#                                                     radius is switch_height/2.
#   * "# mic meter: intentional"                    — the calibration level
#                                                     meter bg + fill bar (tiny
#                                                     off-ramp radii by design).
#   * "# module-scope squircle: no matching token"  — one-off squircle radii on
#                                                     module-level helper classes
#                                                     (ConfettiOverlay — Phase 11
#                                                     rewrites it; FloatingIndicator
#                                                     inset) with no ramp value.
RADIUS_LITERAL_RE = re.compile(r"corner_radius=[0-9]+")
SANCTIONED_RADIUS_COMMENTS = (
    "# pill: height/2, intentional off-token",
    "# mic meter: intentional",
    "# module-scope squircle: no matching token",
)
RADIUS_WHITELIST_MAX = 7  # may only shrink, never grow


def test_radius_literals_are_whitelisted():
    unsanctioned = []
    total = 0
    for m in RADIUS_LITERAL_RE.finditer(MAIN_SRC):
        total += 1
        line = _line_of(m.start())
        if not any(c in line for c in SANCTIONED_RADIUS_COMMENTS):
            lineno = MAIN_SRC.count("\n", 0, m.start()) + 1
            unsanctioned.append((lineno, line.strip()))
    assert not unsanctioned, (
        "hardcoded corner_radius literal(s) without a sanctioned whitelist "
        f"comment ({SANCTIONED_RADIUS_COMMENTS}); use corner_radius=RADIUS[...] "
        "tokens instead:\n"
        + "\n".join(f"  {ln}: {txt}" for ln, txt in unsanctioned)
    )
    assert total <= RADIUS_WHITELIST_MAX, (
        f"radius-literal whitelist grew to {total} (max {RADIUS_WHITELIST_MAX}); "
        "the whitelist may only shrink. New shapes must use corner_radius=RADIUS[...]."
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
# faster than 100ms in the live app today. All four are contractual per
# CLAUDE.md rule 9 (hero 30fps=33ms idle / 15fps=66ms, tray/mic feedback).
# The old confetti _animate_emojis 16ms bounce was DELETED in Phase 11 (the
# ConfettiOverlay is now a static inline toast) — its whitelist entry is gone
# so the lint now enforces that it never returns. The test's job is to make any
# NEW sub-100ms self-rearming loop fail the suite.
GRANDFATHERED_SUB100_SELF_REARM = {
    "_animate_hero",        # hero waveform, 66ms = 15fps active (rule 9)
    "_animate_idle_breath",  # hero idle breath, 33ms = 30fps idle (rule 9)
    "_tray_pulse_step",     # tray recording pulse, 50ms (deliberate, CPU-validated)
    "_update_mic_test",     # calibration level meter, 50ms
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


# ---------------------------------------------------------------------------
# (f) No-popup-dialogs enforcement — CLAUDE.md rule 2
# ---------------------------------------------------------------------------
# Every settings/confirm UI must be an inline CTkFrame panel (via
# _show_inline_panel) or an in-place control, NOT a spawned Toplevel. The only
# two sanctioned Toplevels are the transient chrome that genuinely can't live
# inline: the ToolTip hover bubble and the FloatingIndicator overlay window.
# Any Toplevel instantiated ANYWHERE ELSE fails CI, and modal `.grab_set()` is
# banned outright (it was the source of the Wayland focus/sizing bugs rule 2
# was written to end).
TOPLEVEL_RE = re.compile(r"Toplevel\s*\(")
GRAB_SET_RE = re.compile(r"\.grab_set\s*\(")

# Classes whose bodies are allowed to instantiate a Toplevel.
TOPLEVEL_ALLOWED_CLASSES = {"ToolTip", "FloatingIndicator"}


def _allowed_toplevel_line_ranges(src: str):
    """Return [(start_line, end_line), ...] for each sanctioned class body."""
    tree = ast.parse(src)
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in TOPLEVEL_ALLOWED_CLASSES:
            ranges.append((node.lineno, node.end_lineno))
    return ranges


def test_toplevels_only_in_sanctioned_classes():
    ranges = _allowed_toplevel_line_ranges(MAIN_SRC)
    # Guard against the class names being renamed and the test silently passing.
    assert len(ranges) == len(TOPLEVEL_ALLOWED_CLASSES), (
        "expected to locate every sanctioned Toplevel class "
        f"{sorted(TOPLEVEL_ALLOWED_CLASSES)}; found {len(ranges)} class bodies"
    )

    offenders = []
    for m in TOPLEVEL_RE.finditer(MAIN_SRC):
        lineno = MAIN_SRC.count("\n", 0, m.start()) + 1
        if not any(start <= lineno <= end for start, end in ranges):
            offenders.append((lineno, _line_of(m.start()).strip()))

    assert not offenders, (
        "Toplevel/CTkToplevel instantiated outside ToolTip/FloatingIndicator "
        "(CLAUDE.md rule 2: no popup dialogs — use an inline _show_inline_panel "
        f"CTkFrame instead). Offenders (line, src): {offenders}"
    )


# The no-Toplevel rule extends to the ENTIRE src/wayfinder package (all new code,
# CLAUDE.md rule 4). Same allowance as wayfinder_main.py and NOTHING more: the
# only Toplevels permitted app-wide are the ToolTip hover bubble and the
# FloatingIndicator overlay (transient chrome that genuinely can't live inline).
# Any other Toplevel under src/** fails CI — which is what proves the old modal
# SetupWizard CTkToplevel is gone (July 2026, Item 2): its UI is now the in-window
# wayfinder.ui.setup_pane pane (mirroring wayfinder.ui.welcome), NOT a Toplevel.
SRC_PACKAGE = REPO / "src" / "wayfinder"


def test_no_toplevels_in_src_package_outside_sanctioned_classes():
    offenders = []
    for path in sorted(SRC_PACKAGE.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "Toplevel" not in src:
            continue
        ranges = _allowed_toplevel_line_ranges(src)  # ToolTip/FloatingIndicator bodies
        for m in TOPLEVEL_RE.finditer(src):
            lineno = src.count("\n", 0, m.start()) + 1
            if not any(start <= lineno <= end for start, end in ranges):
                line = src.splitlines()[lineno - 1].strip()
                offenders.append((str(path.relative_to(REPO)), lineno, line))

    assert not offenders, (
        "Toplevel/CTkToplevel instantiated in src/wayfinder/** outside the "
        "sanctioned ToolTip/FloatingIndicator hover/overlay chrome (CLAUDE.md "
        "rule 2: no popup dialogs — build a place()'d inline CTkFrame pane like "
        f"wayfinder.ui.setup_pane instead). Offenders (file, line, src): {offenders}"
    )


def test_no_grab_set_call_sites():
    hits = [
        (MAIN_SRC.count("\n", 0, m.start()) + 1, _line_of(m.start()).strip())
        for m in GRAB_SET_RE.finditer(MAIN_SRC)
    ]
    assert not hits, (
        "`.grab_set()` is banned (CLAUDE.md rule 2 — modal Toplevels caused the "
        f"Wayland focus/sizing bugs). Hits: {hits}"
    )


# ---------------------------------------------------------------------------
# (g) Mouse-wheel pipeline — Tk 9 compat (probe-verified 2026-07-02)
# ---------------------------------------------------------------------------
# Tk 9 on XWayland delivers ONE <MouseWheel> per notch with delta=±120 and no
# legacy Button-4/5. CTk 5.2.2's own Linux handler scrolls event.delta canvas
# "units" of 10% viewport each — 12 viewports per notch, i.e. instant
# top/bottom teleport. _enable_linux_mousewheel neuters that handler at the
# class level and installs the one correct pixel-based pipeline. These tests
# keep both halves of the fix from silently regressing.


def test_ctk_wheel_shim_present():
    assert "ctk.CTkScrollableFrame._mouse_wheel_all = lambda" in MAIN_SRC, (
        "the Tk9-compat shim neutering CTk's broken <MouseWheel> math is gone — "
        "without it every wheel notch teleports scrollable frames to top/bottom"
    )
    assert '"<MouseWheel>"' in MAIN_SRC, (
        "no root-level <MouseWheel> binding — Tk 9 delivers wheel input ONLY as "
        "<MouseWheel> (delta=±120); Button-4/5 alone leaves the app wheel-dead"
    )


def test_no_unbind_all_call_sites():
    hits = [
        (MAIN_SRC.count("\n", 0, m.start()) + 1, _line_of(m.start()).strip())
        for m in re.finditer(r"\.unbind_all\s*\(", MAIN_SRC)
    ]
    assert not hits, (
        "unbind_all() removes EVERY root-level binding for that event app-wide "
        "(the old _trap_scroll killed global wheel scrolling after one hover of "
        f"the models panel). Scope bindings per-widget instead. Hits: {hits}"
    )


# ---------------------------------------------------------------------------
# (h) In-window dropdown panel — no tk.Menu / tk_popup open list
# ---------------------------------------------------------------------------
# CustomTkinter opens a CTkOptionMenu's list as a native tkinter.Menu via
# tk_popup. That list can never be rounded/shadowed and on KDE/Wayland its
# stacking is WM-owned — the proven root cause of the mic-dropdown z-order bug.
# Item 1 (July 2026) replaced every open list with an in-window place()'d
# CTkFrame: InlineOptionMenu (a CTkOptionMenu subclass) overrides the ONLY
# method that calls tk_popup (_open_dropdown_menu) and routes it to
# WayfinderApp._open_dropdown_panel. These tests keep the tk_popup path from
# ever coming back.
#
# `CTkOptionMenu\s*\(` matches only INSTANTIATIONS: the subclass base reference
# `class InlineOptionMenu(ctk.CTkOptionMenu):` reads `CTkOptionMenu)` (no `(`),
# so it is not matched — exactly what we want (base ref allowed, construction
# banned).
CTK_OPTIONMENU_CTOR_RE = re.compile(r"CTkOptionMenu\s*\(")


def test_no_ctk_optionmenu_construction():
    hits = [
        (MAIN_SRC.count("\n", 0, m.start()) + 1, _line_of(m.start()).strip())
        for m in CTK_OPTIONMENU_CTOR_RE.finditer(MAIN_SRC)
    ]
    assert not hits, (
        "A stock CTkOptionMenu was constructed — its open list is a native "
        "tk.Menu (tk_popup) that can't be styled and mis-stacks on KDE/Wayland "
        "(the mic-dropdown z-order bug). Use InlineOptionMenu, whose open list is "
        f"the in-window _open_dropdown_panel. Hits: {hits}"
    )


def test_inline_dropdown_panel_wiring_present():
    # The routing subclass exists and overrides the tk_popup entry point...
    assert re.search(r"class InlineOptionMenu\(ctk\.CTkOptionMenu\)", MAIN_SRC), (
        "InlineOptionMenu subclass is gone — the closed control would fall back "
        "to CTk's native tk.Menu open list (the KDE z-order bug returns)."
    )
    # ...and it overrides _open_dropdown_menu (the sole tk_popup caller).
    assert "def _open_dropdown_menu(self):" in MAIN_SRC, (
        "InlineOptionMenu no longer overrides _open_dropdown_menu — CTk's native "
        "tk_popup path is reachable again."
    )
    # ...routing into the in-window panel renderer on the toplevel.
    assert "def _open_dropdown_panel(self, option_menu):" in MAIN_SRC, (
        "the in-window dropdown panel renderer _open_dropdown_panel is gone."
    )
