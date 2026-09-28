"""The hero mic button: a quiet hairline chip that only takes colour when
something is happening (pure PIL, no Tk).

Design (80-unit canvas, disc radius 24 units):

- **Ready**: a raised chip (card + 5% white) with a hairline rim, like the rim
  on every pane, a faint glass highlight across the upper arc, and the Lucide
  ``mic`` as a 2 px line glyph, the same icon language as the sidebar.
  Hover: the rim takes the state colour and the glyph goes white.
- **Recording**: a solid state-colour disc with a white rounded stop square;
  a thin ring outside it expands and fades with the pulse (radar, not glow).
- **Processing / typing**: the chip tinted by the state colour, with a state
  hairline and glyph.

No blurs, gradients or halos: colour and motion carry the state. Drawn 4x
supersampled and LANCZOS-downscaled; returns an RGB image pre-composited over
the card colour, so the caller's cache and Tk PhotoImage path stay unchanged.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

DESIGN = 80.0     # design units across the canvas
DISC_R = 24.0     # disc radius in design units
GLYPH = 22.0      # Lucide glyph box in design units
SS = 4            # supersampling factor for crisp edges

_WHITE = (255, 255, 255)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a, b, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _stroke(draw: ImageDraw.ImageDraw, p, q, width: float, fill) -> None:
    """A line with round caps (PIL lines are butt-capped)."""
    draw.line([p, q], fill=fill, width=int(round(width)))
    r = width / 2
    for x, y in (p, q):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _lucide_mic(draw: ImageDraw.ImageDraw, cx: float, cy: float, box: float, fill) -> None:
    """Lucide ``mic`` on its 24-unit grid, 2-unit stroke, scaled to ``box``:
    capsule 9..15 x 2..15 (r 3), cradle of radius 7 about (12, 12), stem 19..22."""
    u = box / 24.0
    w = 2.0 * u
    ox, oy = cx - 12 * u, cy - 12 * u

    def pt(x, y):
        return ox + x * u, oy + y * u

    draw.rounded_rectangle([*pt(9, 2), *pt(15, 15)], radius=3 * u, outline=fill,
                           width=int(round(w)))
    _stroke(draw, pt(5, 10), pt(5, 12), w, fill)
    _stroke(draw, pt(19, 10), pt(19, 12), w, fill)
    draw.arc([*pt(5, 5), *pt(19, 19)], start=0, end=180, fill=fill, width=int(round(w)))
    _stroke(draw, pt(12, 19), pt(12, 22), w, fill)


def render_mic_button(phys: int, color: str, *, card: str, ink: str, light: str,
                      active: bool = False, pulse: float | None = None,
                      hover: bool = False) -> Image.Image:
    """The mic button as an RGB ``phys`` x ``phys`` image over ``card``.

    ``color``: state colour. ``light``: the ready glyph colour. ``ink`` is kept
    for the caller's signature (the stop square is white on the state colour).
    ``pulse`` (recording) breathes in [0.8, 1.0]; ``active`` tints the chip
    (processing / typing).
    """
    del ink
    phys = max(16, int(phys))
    S = phys * SS
    k = S / DESIGN
    c = S / 2.0
    r = DISC_R * k
    state, card_rgb, light_rgb = _rgb(color), _rgb(card), _rgb(light)
    img = Image.new("RGB", (S, S), card_rgb)
    draw = ImageDraw.Draw(img)
    hair = max(1.0, 1.25 * k)

    if pulse is not None:
        expand = min(max((pulse - 0.8) / 0.2, 0.0), 1.0)
        rr = r + (3.0 + 8.0 * expand) * k
        draw.ellipse([c - rr, c - rr, c + rr, c + rr],
                     outline=_mix(card_rgb, state, 0.12 + 0.55 * (1.0 - expand)),
                     width=int(round(1.5 * k)))
        draw.ellipse([c - r, c - r, c + r, c + r], fill=state)
        sq = 6.5 * k
        draw.rounded_rectangle([c - sq, c - sq, c + sq, c + sq], radius=2.2 * k, fill=_WHITE)
    elif active:
        draw.ellipse([c - r, c - r, c + r, c + r], fill=_mix(card_rgb, state, 0.16),
                     outline=_mix(card_rgb, state, 0.85), width=int(round(hair)))
        _lucide_mic(draw, c, c, GLYPH * k, state)
    else:
        chip = _mix(card_rgb, _WHITE, 0.08 if hover else 0.05)
        rim = _mix(card_rgb, state, 0.75) if hover else _mix(card_rgb, _WHITE, 0.16)
        draw.ellipse([c - r, c - r, c + r, c + r], fill=chip, outline=rim, width=int(round(hair)))
        hr = r - hair * 1.5
        draw.arc([c - hr, c - hr, c + hr, c + hr], start=210, end=330,
                 fill=_mix(chip, _WHITE, 0.10), width=max(1, int(round(k))))
        _lucide_mic(draw, c, c, GLYPH * k, _WHITE if hover else light_rgb)

    return img.resize((phys, phys), Image.LANCZOS)
