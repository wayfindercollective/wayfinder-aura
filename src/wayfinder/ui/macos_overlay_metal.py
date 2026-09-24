"""Native Metal waveform layer for the macOS floating status pill."""

from __future__ import annotations

import ctypes
import sys

from wayfinder.ui.macos_hero_metal import _native_library_candidates


# The Qt renderer clamps each waveform centerline to its layout rectangle, but
# its 10px-wide glow stroke is painted outside that rectangle and is clipped only
# by the pill. Give Metal the same four logical pixels between the wave box and
# the pill edge so peaks retain the old soft overflow instead of ending in a hard
# horizontal cut.
WAVE_BLEED = 4.0


def _expanded_wave_frame(
    x: float,
    top: float,
    width: float,
    height: float,
    content_height: float,
    bleed: float = WAVE_BLEED,
) -> tuple[float, float, int, int]:
    """Convert a Qt top-left wave box to an expanded Core Animation frame."""
    expanded_width = max(1, int(round(width + bleed * 2.0)))
    expanded_height = max(1, int(round(height + bleed * 2.0)))
    native_x = float(x) - bleed
    native_y = float(content_height) - float(top) - float(height) - bleed
    return native_x, native_y, expanded_width, expanded_height


_SHADER = r"""
#include <metal_stdlib>
using namespace metal;

struct OverlayUniforms {
    float4 dims;    // logical width, height, wave time, breath time
    float4 params;  // audio level, backing scale, unused, unused
    float4 color;
};

float segment_distance(float2 p, float2 a, float2 b) {
    float2 ab = b - a;
    float along = clamp(dot(p - a, ab) / max(dot(ab, ab), 0.0001), 0.0, 1.0);
    return length(p - (a + along * ab));
}

float wave_y(float x, float center, float amp, float freq, float phase,
             float t, float height) {
    float y = center + amp * sin(freq * x + t + phase);
    y += amp * 0.4 * sin(freq * 2.3 * x + t * 1.6 + phase);
    // 2.0 (was 2.1): every term then completes whole cycles in the idle
    // Core Animation loop (overlay_renderer.m), so hand-offs keep phase.
    y += amp * 0.2 * sin(freq * 3.7 * x + t * 2.0 + phase * 0.5);
    return clamp(y, 0.0, height);
}

float highlight_y(float x, float center, float amp, float t, float height) {
    float y = center + amp * sin(0.13 * x + t * 1.4);
    y += amp * 0.5 * sin(0.26 * x + t * 2.0 + 0.8);
    return clamp(y, 0.0, height);
}

float path_distance(float2 p, float width, float center, float amp,
                    float freq, float phase, float t, float height,
                    bool highlight) {
    float step = 2.0;
    int center_index = int(floor(p.x / step));
    float best = 1.0e6;
    for (int offset = -1; offset <= 1; ++offset) {
        int index = clamp(center_index + offset, 0, max(0, int(ceil(width / step)) - 1));
        float x0 = min(width, step * float(index));
        float x1 = min(width, step * float(index + 1));
        float y0 = highlight
            ? highlight_y(x0, center, amp, t, height)
            : wave_y(x0, center, amp, freq, phase, t, height);
        float y1 = highlight
            ? highlight_y(x1, center, amp, t, height)
            : wave_y(x1, center, amp, freq, phase, t, height);
        best = min(best, segment_distance(p, float2(x0, y0), float2(x1, y1)));
    }
    return best;
}

float coverage(float distance, float width, float backingScale) {
    float aa = 0.5 / max(backingScale, 1.0);
    return 1.0 - smoothstep(max(0.0, width * 0.5 - aa), width * 0.5 + aa, distance);
}

float over_alpha(float below, float above) {
    return above + below * (1.0 - above);
}

kernel void overlay_wave(texture2d<float, access::write> output [[texture(0)]],
                         constant OverlayUniforms& u [[buffer(0)]],
                         uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= output.get_width() || gid.y >= output.get_height()) return;
    float bleed = max(0.0, u.params.z);
    float width = max(1.0, u.dims.x - bleed * 2.0);
    float height = max(1.0, u.dims.y - bleed * 2.0);
    float t = u.dims.z;
    float breathTime = u.dims.w;
    float level = clamp(u.params.x, 0.0, 1.0);
    float scale = max(u.params.y, 1.0);
    float2 p = float2(float(gid.x) / scale, float(gid.y) / scale) - bleed;
    float center = height * 0.5;
    float maxAmp = height * 0.4;
    float baseBreath = 0.15 + 0.12 * (0.5 + 0.5 * sin(breathTime));
    float amplitudeFactor = min(1.0, baseBreath + pow(level, 0.6) * 12.0);
    float amp = maxAmp * amplitudeFactor;
    float fadeZone = min(12.0, width * 0.25);
    float edge = fadeZone > 0.0
        ? clamp(min(p.x, width - p.x) / fadeZone, 0.0, 1.0)
        : 1.0;

    float freqs[4] = {0.07, 0.11, 0.16, 0.22};
    float phases[4] = {0.0, 1.0, 2.2, 0.7};
    float alphas[4] = {0.15, 0.25, 0.40, 0.55};
    float thickness[4] = {6.0, 5.0, 4.0, 3.0};
    float alpha = 0.0;
    for (int i = 0; i < 4; ++i) {
        float distance = path_distance(
            p, width, center, amp, freqs[i], phases[i], t, height, false
        );
        float glow = coverage(distance, thickness[i] + 4.0, scale)
            * alphas[i] * 0.3 * edge;
        alpha = over_alpha(alpha, glow);
        float core = coverage(distance, thickness[i], scale) * alphas[i] * edge;
        alpha = over_alpha(alpha, core);
    }
    float hiDistance = path_distance(
        p, width, center, amp, 0.0, 0.0, t, height, true
    );
    alpha = over_alpha(alpha, coverage(hiDistance, 6.0, scale) * 0.4 * edge);
    alpha = over_alpha(alpha, coverage(hiDistance, 2.0, scale) * edge);

    // Core Animation composites premultiplied BGRA correctly over Qt's static chrome.
    output.write(float4(u.color.rgb * alpha, alpha), gid);
}
"""


class MacOSOverlayMetalLayer:
    """ctypes bridge to the native 15 fps transparent Metal layer."""

    def __init__(self, library, handle, ns_window):
        self.library = library
        self.handle = handle
        self.ns_window = ns_window
        self._frame = None

    @classmethod
    def try_create(cls, title: str):
        if sys.platform != "darwin":
            return None
        try:
            import objc
            from AppKit import NSApplication

            ns_window = next(
                (
                    window
                    for window in NSApplication.sharedApplication().windows()
                    if str(window.title() or "") == title
                ),
                None,
            )
            if ns_window is None:
                return None
            view = ns_window.contentView()
            if view.layer() is None:
                view.setWantsLayer_(True)
            parent = view.layer()
            if parent is None:
                return None
            path = next((p for p in _native_library_candidates() if p.is_file()), None)
            if path is None:
                return None
            library = ctypes.CDLL(str(path))
            library.wf_overlay_create.argtypes = [
                ctypes.c_void_p, ctypes.c_double, ctypes.c_char_p
            ]
            library.wf_overlay_create.restype = ctypes.c_void_p
            library.wf_overlay_set_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_int,
                ctypes.c_int,
            ]
            library.wf_overlay_set_state.argtypes = [
                ctypes.c_void_p,
                ctypes.c_float,
                ctypes.c_float,
                ctypes.c_float,
                ctypes.c_float,
            ]
            library.wf_overlay_set_hidden.argtypes = [ctypes.c_void_p, ctypes.c_int]
            try:
                library.wf_overlay_set_idle.argtypes = [ctypes.c_void_p, ctypes.c_int]
            except AttributeError:
                pass  # an older renderer dylib: the wave simply never settles
            library.wf_overlay_destroy.argtypes = [ctypes.c_void_p]
            handle = library.wf_overlay_create(
                ctypes.c_void_p(objc.pyobjc_id(parent)),
                float(ns_window.backingScaleFactor()),
                _SHADER.encode("utf-8"),
            )
            return cls(library, handle, ns_window) if handle else None
        except Exception as exc:
            print(f"overlay: native Metal waveform unavailable ({exc})", flush=True)
            return None

    def update(self, rect, color, audio_level: float, *, hidden: bool = False) -> None:
        if not self.handle:
            return
        content_height = float(self.ns_window.contentView().bounds().size.height)
        x, y, width, height = _expanded_wave_frame(
            float(rect.x()),
            float(rect.y()),
            float(rect.width()),
            float(rect.height()),
            content_height,
        )
        frame = (round(x, 3), round(y, 3), width, height)
        if frame != self._frame:
            self.library.wf_overlay_set_frame(
                self.handle, x, y, width, height
            )
            self._frame = frame
        self.library.wf_overlay_set_state(
            self.handle,
            float(audio_level),
            float(color.redF()),
            float(color.greenF()),
            float(color.blueF()),
        )
        self.library.wf_overlay_set_hidden(self.handle, int(bool(hidden)))

    def set_hidden(self, hidden: bool) -> None:
        if self.handle:
            self.library.wf_overlay_set_hidden(self.handle, int(bool(hidden)))

    def set_idle(self, idle: bool) -> None:
        """READY with nothing happening: the native wave may settle (see .m)."""
        if self.handle and hasattr(self.library, "wf_overlay_set_idle"):
            self.library.wf_overlay_set_idle(self.handle, int(bool(idle)))

    def close(self) -> None:
        if self.handle:
            self.library.wf_overlay_destroy(self.handle)
            self.handle = None
