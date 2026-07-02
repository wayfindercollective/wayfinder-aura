"""Cross-surface brand-palette parity (Phase 5, design pass).

The overlay (``src/wayfinder/ui/overlay.py``) runs as a bare-script subprocess
and cannot cleanly import ``theme.py``, so its color constants are DUPLICATED
by design. These tests source-parse the overlay (and the two tray renderers)
and assert the duplicated hues stay in lock-step with the app design tokens in
``src/wayfinder/ui/theme.py``. If a token changes and the overlay/tray copy is
not updated, this file fails — that is the sync mechanism.

Everything here is a text/AST parse (no PyQt6, no Tk) so it runs headless.
"""
import re
from pathlib import Path

from wayfinder.ui.theme import COLORS

REPO = Path(__file__).resolve().parent.parent
OVERLAY = REPO / "src" / "wayfinder" / "ui" / "overlay.py"
TRAY_QT = REPO / "src" / "wayfinder" / "ui" / "tray_icon.py"
MAIN = REPO / "wayfinder_main.py"

HEX_RE = re.compile(r'#[0-9A-Fa-f]{6}')


def _block(text: str, start_marker: str) -> str:
    """Return the source of a top-level dict literal starting at ``start_marker``.

    Grabs from the marker line to the matching closing brace at column 0
    (``}`` at the start of a line) — good enough for the flat, hand-formatted
    palette dicts in overlay.py.
    """
    idx = text.index(start_marker)
    tail = text[idx:]
    end = tail.index("\n}\n")
    return tail[: end + 2]


def _hexes(block: str) -> set[str]:
    return {h.upper() for h in HEX_RE.findall(block)}


# =============================================================================
# STYLE_PALETTES — each badge hue is an exact app token
# =============================================================================

# minimal→text_secondary, professional→state_ready, casual→state_typing,
# dev→accent_yellow, personal→accent.
STYLE_TOKEN_KEYS = ["text_secondary", "state_ready", "state_typing",
                    "accent_yellow", "accent"]


def test_style_palette_hexes_are_exactly_the_five_tokens():
    src = OVERLAY.read_text()
    found = _hexes(_block(src, "STYLE_PALETTES = {"))
    expected = {COLORS[k].upper() for k in STYLE_TOKEN_KEYS}
    # The documented five brand hues, and nothing else.
    assert expected == {"#8B8B8F", "#7B8BD9", "#5DD4A8", "#E5AC2A", "#A78BFA"}
    assert found == expected, (
        f"overlay STYLE_PALETTES hues {found} != app tokens {expected}"
    )


def test_style_palette_per_key_mapping():
    """Assert each named style maps to its intended token, not just set-equality."""
    src = OVERLAY.read_text()
    block = _block(src, "STYLE_PALETTES = {")
    mapping = {
        "minimal": COLORS["text_secondary"],
        "professional": COLORS["state_ready"],
        "casual": COLORS["state_typing"],
        "dev": COLORS["accent_yellow"],
        "personal": COLORS["accent"],
    }
    for name, token in mapping.items():
        # e.g.  "minimal": StyleColors( ... color="#8B8B8F" ... )
        entry = re.search(
            rf'"{name}":\s*StyleColors\((.*?)\)', block, re.DOTALL
        )
        assert entry, f"style '{name}' not found in STYLE_PALETTES"
        colors = [c.upper() for c in HEX_RE.findall(entry.group(1))]
        assert colors == [token.upper()], (
            f"style '{name}' color {colors} != token {token}"
        )


# =============================================================================
# STATE_PALETTES — LISTENING/PROCESSING on tokens, READY on documented blends
# =============================================================================

def _blend(frac: float) -> str:
    """round(bg + (state_ready - bg) * frac), matching the overlay comment."""
    sr = tuple(int(COLORS["state_ready"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    bg = (0x0D, 0x11, 0x17)  # app bg_base #0D1117
    return "#%02X%02X%02X" % tuple(round(bg[i] + (sr[i] - bg[i]) * frac) for i in range(3))


# READY hues are exact state_ready→bg blends, luminance-matched to the previous
# idle colors (border_top 29%, glow 20%, wave 55%; border_bottom is the deep
# GitHub-dark surface #161B22 shared with the old idle palette).
READY_BLENDS = {
    "border_top": _blend(0.29),     # #2D344F
    "glow": _blend(0.20),           # #23293E
    "wave": _blend(0.55),           # #4A5482
    "border_bottom": "#161B22",
}


def test_ready_blend_helper_matches_chosen_hexes():
    assert READY_BLENDS["border_top"].upper() == "#2D344F"
    assert READY_BLENDS["glow"].upper() == "#23293E"
    assert READY_BLENDS["wave"].upper() == "#4A5482"


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chan = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in chan)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_ready_luminance_stays_within_15pct_of_old_idle():
    """Idle must stay quiet: new READY hues keep luminance close to the old ones."""
    old = {"border_top": "#2F3450", "glow": "#232840", "wave": "#4A5578"}
    for field, old_hex in old.items():
        new_hex = READY_BLENDS[field]
        lo, ln = _relative_luminance(old_hex), _relative_luminance(new_hex)
        delta = abs(ln - lo) / lo
        assert delta <= 0.15, (
            f"READY.{field} luminance moved {delta:.1%} ({old_hex}->{new_hex}); "
            "idle overlay must stay within 15% of the old darkness"
        )


def test_ready_palette_matches_documented_blends():
    src = OVERLAY.read_text()
    block = _block(src, "STATE_PALETTES = {")
    ready = re.search(
        r'OverlayState\.READY:\s*StateColors\((.*?)\),\s*OverlayState\.LISTENING',
        block, re.DOTALL,
    )
    assert ready, "READY entry not found in STATE_PALETTES"
    body = ready.group(1)
    for field, expected in READY_BLENDS.items():
        m = re.search(rf'{field}="(#[0-9A-Fa-f]{{6}})"', body)
        assert m, f"READY.{field} not found"
        assert m.group(1).upper() == expected.upper(), (
            f"READY.{field} = {m.group(1)} != documented blend {expected}"
        )


def test_listening_processing_use_app_state_tokens():
    src = OVERLAY.read_text()
    block = _block(src, "STATE_PALETTES = {")
    # LISTENING glow/wave == app state_recording; PROCESSING glow/wave == state_processing.
    listening = re.search(
        r'OverlayState\.LISTENING:\s*StateColors\((.*?)\),\s*OverlayState\.PROCESSING',
        block, re.DOTALL,
    ).group(1)
    processing = re.search(
        r'OverlayState\.PROCESSING:\s*StateColors\((.*?)\),\s*\}',
        block, re.DOTALL,
    ).group(1)
    rec = COLORS["state_recording"].upper()
    proc = COLORS["state_processing"].upper()
    assert re.search(r'glow="([^"]+)"', listening).group(1).upper() == rec
    assert re.search(r'wave="([^"]+)"', listening).group(1).upper() == rec
    assert re.search(r'glow="([^"]+)"', processing).group(1).upper() == proc
    assert re.search(r'wave="([^"]+)"', processing).group(1).upper() == proc


def test_all_state_palette_hexes_are_tokens_or_documented():
    """Every hex in STATE_PALETTES is an app token or a documented blend/shadow."""
    src = OVERLAY.read_text()
    found = _hexes(_block(src, "STATE_PALETTES = {"))
    allowed = {
        # app tokens (recording rose + processing gold family)
        COLORS["state_recording"].upper(),
        COLORS["state_processing"].upper(),
        "#E8A0A8", "#2D1520",  # rose highlight/shadow (LISTENING)
        "#E8C86A", "#2D2208",  # gold highlight/bronze shadow (PROCESSING)
        # READY documented blends + shared deep surface
        READY_BLENDS["border_top"].upper(),
        READY_BLENDS["glow"].upper(),
        READY_BLENDS["wave"].upper(),
        "#161B22",
        # source tokens named in the READY derivation comment
        COLORS["state_ready"].upper(),   # #7B8BD9
        "#0D1117",                       # app bg_base
    }
    assert found <= allowed, f"unexpected STATE_PALETTES hexes: {found - allowed}"


# =============================================================================
# Tray idle color — brand violet in BOTH renderers, matching app accent
# =============================================================================

VIOLET_RGB = (167, 139, 250)  # == #A78BFA == app accent


def test_app_accent_is_violet_rgb():
    accent = COLORS["accent"].lstrip("#")
    assert tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4)) == VIOLET_RGB


def test_tray_idle_rgb_parity_both_files():
    # Qt tray: _COLORS["idle"] = QColor(167, 139, 250)
    qt_src = TRAY_QT.read_text()
    qt = re.search(r'"idle":\s*QColor\((\d+),\s*(\d+),\s*(\d+)\)', qt_src)
    assert qt, "idle QColor not found in tray_icon.py"
    qt_rgb = tuple(int(g) for g in qt.groups())

    # pystray tray: IDLE branch  glyph_color = (167, 139, 250, 255)
    main_src = MAIN.read_text()
    main = re.search(r'#\s*IDLE.*?glyph_color\s*=\s*\((\d+),\s*(\d+),\s*(\d+),\s*255\)',
                     main_src, re.DOTALL)
    assert main, "idle glyph_color tuple not found in wayfinder_main.py"
    main_rgb = tuple(int(g) for g in main.groups())

    assert qt_rgb == VIOLET_RGB, f"tray_icon.py idle {qt_rgb} != {VIOLET_RGB}"
    assert main_rgb == VIOLET_RGB, f"wayfinder_main.py idle {main_rgb} != {VIOLET_RGB}"
    assert qt_rgb == main_rgb


def test_no_stale_blue_idle_rgb_remains():
    """The old brand-blue idle (70,130,220) must be gone from both tray renderers."""
    for path in (TRAY_QT, MAIN):
        assert "70, 130, 220" not in path.read_text(), (
            f"stale idle blue (70,130,220) still present in {path.name}"
        )
