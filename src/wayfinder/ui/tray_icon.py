"""Qt renderer for the Wayfinder navigation-arrow tray icon.

Mirrors the from-source pystray tray (``wayfinder_main.get_tray_icon``) so the Flatpak's
``QSystemTrayIcon`` shows the same indicator: a clean cursor/arrow — brand blue (app accent /
tray icon) when idle, gold while processing — and, while recording, the outline traced
stroke-by-stroke then filled red (the "drawing" animation the desktop/pystray tray has always
had). Geometry and colours are kept identical to the from-source version so the two installs
look the same.
"""
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPolygonF, QPen

# 30°-rotated symmetric cursor arrow on a 64x64 design canvas — identical points to
# wayfinder_main.get_tray_icon(). Glyph scaled to fill ~90% of the panel slot (the
# previous points sat in a ~32×41 box and looked tiny on HiDPI trays). Index order
# matters: the recording trace walks 5->0->1->...->5.
_ARROW = [(55, 3), (43, 61), (38, 44), (29, 47), (28, 38), (9, 41)]
_TRACE = [(5, 0), (0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]  # outline path, bottom-left -> tip -> back
_COLORS = {
    "recording": QColor(255, 77, 77),    # red
    "processing": QColor(229, 172, 42),   # gold
    "pasting": QColor(93, 212, 168),      # green
    "idle": QColor(70, 130, 220),         # logo blue — matches the taskbar/app icon (user's call)
}
# Design units; render at 2× for HiDPI so KDE/Qt trays stay sharp and read larger.
_DESIGN = 64
_DEFAULT_RENDER = 128


def make_arrow_icon(state: str, pulse_scale: float = 1.0, size: int = _DEFAULT_RENDER) -> QIcon:
    """Render the arrow for ``state``.

    For ``"recording"`` with ``pulse_scale < 0.85`` the outline is traced up to that progress
    (the "drawing" phase); at >= 0.85 (and for every other state) a solid filled arrow is drawn.
    Cycle ``pulse_scale`` 0->1 on a timer to animate.

    Default ``size`` is 128 (2× the 64 design grid) for HiDPI tray panels.
    """
    sc = size / float(_DESIGN)
    pts = [QPointF(x * sc, y * sc) for (x, y) in _ARROW]
    color = _COLORS.get(state, _COLORS["idle"])

    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    # Do NOT set devicePixelRatio here: on KDE StatusNotifier a 128px source without
    # DPR reads larger in the panel than a 64px (or 128@2x) glyph. HiDPI sharpness
    # comes from the denser bitmap; panel scales to slot size.
    p = QPainter(px)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if state == "recording" and pulse_scale < 0.85:
            pen = QPen(color, max(2.5, 5.0 * sc))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            to_draw = (pulse_scale / 0.85) * len(_TRACE)
            for i, (a, b) in enumerate(_TRACE):
                s, e = pts[a], pts[b]
                if i < int(to_draw):
                    p.drawLine(s, e)
                elif i < to_draw:
                    frac = to_draw - int(to_draw)
                    pe = QPointF(s.x() + (e.x() - s.x()) * frac, s.y() + (e.y() - s.y()) * frac)
                    p.drawLine(s, pe)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPolygon(QPolygonF(pts))
    finally:
        p.end()
    return QIcon(px)
