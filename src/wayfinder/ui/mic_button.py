"""The hero mic button, drawn as a lit glass disc (pure PIL, no Tk).

Design (80-unit canvas, disc radius 24 units):

- **Glow**: a soft state-colour halo behind the disc, the same light as the
  hero ribbon. Brighter on hover; while recording it breathes with the pulse.
- **Disc**: idle, a dark glass disc tinted by the state colour, lighter at the
  top; active (recording / processing / typing), filled with the state colour,
  again lighter at the top.
- **Rim**: a thin state-colour edge, bright at the top and fading toward the
  bottom, with a faint inner highlight across the upper arc.
- **Glyph**: a filled microphone (capsule, cradle, stem, base) drawn as vector
  shapes, light on the idle disc and dark on a filled one; recording shows a
  rounded stop square instead.

Everything crisp is drawn 4x supersampled and LANCZOS-downscaled; the glow is
blurred at final size (cheap). Returns an RGB image pre-composited over the
card colour, so the caller's cache and Tk PhotoImage path stay unchanged.
"""
from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

DESIGN = 80.0     # design units across the canvas
DISC_R = 24.0     # disc radius in design units
SS = 4            # supersampling factor for crisp edges


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a, b, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _vertical(size: int, top, bottom) -> Image.Image:
    """An RGB square shading from ``top`` to ``bottom``."""
    ramp = Image.linear_gradient("L").resize((size, size))
    return Image.composite(Image.new("RGB", (size, size), bottom),
                           Image.new("RGB", (size, size), top), ramp)


def _disc_mask(size: int, cx: float, cy: float, r: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return mask


def _glow(phys: int, color, strength: float, radius_units: float) -> Image.Image:
    """Soft halo as an L mask at final size (blur is cheap here)."""
    k = phys / DESIGN
    c = phys / 2.0
    mask = Image.new("L", (phys, phys), 0)
    r = radius_units * k
    ImageDraw.Draw(mask).ellipse([c - r, c - r, c + r, c + r],
                                 fill=int(255 * max(0.0, min(1.0, strength))))
    return mask.filter(ImageFilter.GaussianBlur(max(1.0, 6.5 * k)))


def _draw_mic_glyph(draw: ImageDraw.ImageDraw, cx: float, cy: float, k: float, fill) -> None:
    """Filled microphone: capsule, U cradle, stem and base (design units * k)."""
    w = max(1, round(2.4 * k))               # stroke of cradle/stem/base
    cap_w, cap_h = 8.0 * k, 14.0 * k
    top = cy - 11.0 * k
    draw.rounded_rectangle([cx - cap_w / 2, top, cx + cap_w / 2, top + cap_h],
                           radius=cap_w / 2, fill=fill)
    cr = 7.6 * k                              # cradle radius
    ccy = top + cap_h - 4.4 * k               # cradle centre (hugs the capsule's lower half)
    draw.arc([cx - cr, ccy - cr, cx + cr, ccy + cr], start=0, end=180, fill=fill, width=w)
    stem_top = ccy + cr - w / 2
    stem_bot = cy + 12.0 * k
    draw.rounded_rectangle([cx - w / 2, stem_top, cx + w / 2, stem_bot], radius=w / 2, fill=fill)
    base_w = 5.2 * k
    draw.rounded_rectangle([cx - base_w, stem_bot - w / 2, cx + base_w, stem_bot + w / 2],
                           radius=w / 2, fill=fill)


def render_mic_button(phys: int, color: str, *, card: str, ink: str, light: str,
                      active: bool = False, pulse: float | None = None,
                      hover: bool = False) -> Image.Image:
    """The mic button as an RGB ``phys`` x ``phys`` image over ``card``.

    ``color``: state colour. ``ink``: the dark glyph/stop colour on a filled
    disc. ``light``: the glyph colour on the idle disc. ``pulse`` (recording)
    breathes in [0.8, 1.0]; ``active`` fills the disc (processing/typing).
    """
    phys = max(16, int(phys))
    state = _rgb(color)
    card_rgb, ink_rgb, light_rgb = _rgb(card), _rgb(ink), _rgb(light)
    recording = pulse is not None
    filled = recording or active
    expand = min(max(((pulse or 0.9) - 0.8) / 0.2, 0.0), 1.0)

    # 1. Glow at final size, composited onto the card.
    if recording:
        strength, radius = 0.62 - 0.30 * expand, DISC_R + 3.0 + 6.0 * expand
    elif filled:
        strength, radius = 0.55, DISC_R + 3.0
    else:
        strength, radius = (0.42 if hover else 0.28), DISC_R + 2.0
    base = Image.new("RGB", (phys, phys), card_rgb)
    base = Image.composite(Image.new("RGB", (phys, phys), state), base,
                           _glow(phys, state, strength, radius))

    # 2. Disc, rim and glyph supersampled.
    S = phys * SS
    k = S / DESIGN
    c = S / 2.0
    r = DISC_R * k
    big = base.resize((S, S), Image.BILINEAR)
    disc = _disc_mask(S, c, c, r)
    if filled:
        fill = _vertical(S, _mix(state, (255, 255, 255), 0.22), _mix(state, (0, 0, 0), 0.10))
    else:
        tint = 0.30 if hover else 0.22
        fill = _vertical(S, _mix(card_rgb, state, tint), _mix(card_rgb, (0, 0, 0), 0.28))
    big.paste(fill, (0, 0), disc)

    # Rim: bright at the top, fading down (a ring mask shaded by a vertical ramp).
    rim_w = max(1.0, 1.4 * k)
    ring = _disc_mask(S, c, c, r)
    ring = ImageChops.subtract(ring, _disc_mask(S, c, c, r - rim_w))
    rim_top = _mix(state, (255, 255, 255), 0.35 if filled else 0.10)
    rim_bottom = _mix(state, card_rgb, 0.10 if filled else 0.55)
    big.paste(_vertical(S, rim_top, rim_bottom), (0, 0), ring)

    # Faint inner highlight across the upper arc (glass).
    hl = Image.new("L", (S, S), 0)
    hr = r - rim_w * 1.6
    ImageDraw.Draw(hl).arc([c - hr, c - hr, c + hr, c + hr], start=205, end=335,
                           fill=int(255 * (0.20 if filled else 0.12)),
                           width=max(1, round(1.2 * k)))
    big = Image.composite(Image.new("RGB", (S, S), (255, 255, 255)), big, hl)

    draw = ImageDraw.Draw(big)
    if recording:
        sq = 8.5 * k
        draw.rounded_rectangle([c - sq, c - sq, c + sq, c + sq],
                               radius=max(1, round(3.2 * k)), fill=ink_rgb)
    else:
        _draw_mic_glyph(draw, c, c, k, ink_rgb if filled else light_rgb)

    return big.resize((phys, phys), Image.LANCZOS)
