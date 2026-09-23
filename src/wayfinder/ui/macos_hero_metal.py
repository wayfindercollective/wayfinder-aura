"""Metal compute renderer for the macOS in-app hero waveform.

This mirrors ``hero_render.render_hero_wave`` mathematically. The existing PIL
renderer remains the cross-platform reference and fallback; on macOS this
renderer writes the same visual directly into a CAMetalLayer drawable so the
CPU only submits a small uniform block per frame.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from pathlib import Path


_METAL_FRAMEWORK = "/System/Library/Frameworks/Metal.framework/Metal"
_PIXEL_FORMAT_BGRA8_UNORM = 80

_SHADER = r"""
#include <metal_stdlib>
using namespace metal;

struct HeroUniforms {
    float4 dims;    // width, height, time, audio level
    float4 params;  // morph, stroke scale, unused, unused
    float4 color;   // state RGB, 1
    float4 bg;      // background RGB, 1
};

float clampf(float value, float lo, float hi) {
    return clamp(value, lo, hi);
}

float soft_limit(float dy, float height, float stroke_scale) {
    float max_stroke = round(6.0 * stroke_scale);
    float glow_extra = round(2.0 * stroke_scale);
    float max_stroke_radius = (max_stroke + glow_extra * 2.0) * 0.5;
    float a_max = max(4.0, height * 0.5 - max_stroke_radius - 3.0);
    float knee = a_max * 0.7;
    float soft_range = a_max - knee;
    float magnitude = abs(dy);
    if (magnitude <= knee) return dy;
    float limited = knee + soft_range * tanh((magnitude - knee) / soft_range);
    return copysign(limited, dy);
}

float wave_y(float x, float width, float height, float t, float amp,
             float freq, float phase, float stroke_scale) {
    float u = 640.0 * x / max(width - 1.0, 1.0);
    float f = freq * 0.32;
    float dy = amp * sin(f * u + t + phase);
    dy += amp * 0.4 * sin(f * 2.3 * u + t * 1.6 + phase);
    dy += amp * 0.2 * sin(f * 3.7 * u + t * 2.1 + phase * 0.5);
    return clampf(height * 0.5 + soft_limit(dy, height, stroke_scale), 1.0, height - 1.0);
}

float highlight_y(float x, float width, float height, float t, float amp,
                  float stroke_scale) {
    float u = 640.0 * x / max(width - 1.0, 1.0);
    float dy = amp * sin(0.13 * 0.32 * u + t * 1.4);
    dy += amp * 0.5 * sin(0.26 * 0.32 * u + t * 2.0 + 0.8);
    return clampf(height * 0.5 + soft_limit(dy, height, stroke_scale), 1.0, height - 1.0);
}

float segment_distance(float2 p, float2 a, float2 b) {
    float2 ab = b - a;
    float denom = max(dot(ab, ab), 0.0001);
    float along = clamp(dot(p - a, ab) / denom, 0.0, 1.0);
    return length(p - (a + along * ab));
}

float wave_distance(float2 p, float width, float height, float t, float amp,
                    float freq, float phase, float stroke_scale, int count) {
    float step = width / float(count - 1);
    int center = int(floor(p.x / max(step, 0.001)));
    float best = 1.0e6;
    for (int offset = -1; offset <= 1; ++offset) {
        int index = clamp(center + offset, 0, count - 2);
        float x0 = step * float(index);
        float x1 = step * float(index + 1);
        float2 a = float2(x0, wave_y(x0, width, height, t, amp, freq, phase, stroke_scale));
        float2 b = float2(x1, wave_y(x1, width, height, t, amp, freq, phase, stroke_scale));
        best = min(best, segment_distance(p, a, b));
    }
    return best;
}

float highlight_distance(float2 p, float width, float height, float t, float amp,
                         float stroke_scale, int count) {
    float step = width / float(count - 1);
    int center = int(floor(p.x / max(step, 0.001)));
    float best = 1.0e6;
    for (int offset = -1; offset <= 1; ++offset) {
        int index = clamp(center + offset, 0, count - 2);
        float x0 = step * float(index);
        float x1 = step * float(index + 1);
        float2 a = float2(x0, highlight_y(x0, width, height, t, amp, stroke_scale));
        float2 b = float2(x1, highlight_y(x1, width, height, t, amp, stroke_scale));
        best = min(best, segment_distance(p, a, b));
    }
    return best;
}

float coverage(float distance, float width) {
    // ImageDraw's integer-width stroke has a hard interior. One pixel of edge
    // smoothing matches the display-scaled Tk reference on Retina panels.
    return 1.0 - smoothstep(max(0.0, width * 0.5 - 0.5), width * 0.5 + 0.5, distance);
}

kernel void hero_wave(texture2d<float, access::write> output [[texture(0)]],
                      constant HeroUniforms& u [[buffer(0)]],
                      uint2 gid [[thread_position_in_grid]]) {
    uint width_px = output.get_width();
    uint height_px = output.get_height();
    if (gid.x >= width_px || gid.y >= height_px) return;

    float width = u.dims.x;
    float height = u.dims.y;
    float t = u.dims.z;
    float level = clampf(u.dims.w, 0.0, 1.0);
    float morph = clampf(u.params.x, 0.0, 1.0);
    float stroke_scale = clampf(u.params.y, 0.7, 2.5);
    float2 point = float2(float(gid.x), float(gid.y));
    float3 state_color = u.color.rgb;
    float3 result = u.bg.rgb;

    // Exact reference amplitude and idle breath.
    float max_amp = height * 0.42;
    float breath = 0.26 + 0.09 * (0.5 + 0.5 * sin(t * 0.8));
    float amp = max_amp * min(0.80, breath + 0.20 * morph + pow(level, 0.6) * 0.62 * morph);
    float brightness = 0.55 + 0.45 * morph;
    float hi_brightness = 0.40 + 0.60 * morph;
    int point_count = morph < 0.3
        ? 109
        : clamp(int(round(width / 9.0)), 109, 240);

    float freqs[4] = {0.07, 0.11, 0.16, 0.22};
    float phases[4] = {0.0, 1.0, 2.2, 0.7};
    float alphas[4] = {0.15, 0.25, 0.40, 0.55};
    float thicknesses[4] = {6.0, 5.0, 4.0, 3.0};
    float distances[4];
    float glow = 0.0;
    float blur_radius = max(1.0, 2.0 * stroke_scale);
    float glow_extra = max(1.0, round(2.0 * stroke_scale));

    for (int i = 0; i < 4; ++i) {
        distances[i] = wave_distance(
            point, width, height, t, amp, freqs[i], phases[i], stroke_scale, point_count
        );
        float core_width = max(1.0, round(thicknesses[i] * stroke_scale));
        float glow_width = core_width + glow_extra * 2.0;
        float outside = max(0.0, distances[i] - glow_width * 0.5);
        float gaussian = exp(-0.5 * outside * outside / (blur_radius * blur_radius));
        glow = max(glow, alphas[i] * 0.3 * brightness * gaussian);
    }

    float hi_distance = highlight_distance(
        point, width, height, t, amp, stroke_scale, point_count
    );
    float hi_width = max(1.0, round(2.0 * stroke_scale));
    float hi_glow_width = max(2.0, round(4.0 * stroke_scale));
    float hi_outside = max(0.0, hi_distance - hi_glow_width * 0.5);
    float hi_gaussian = exp(-0.5 * hi_outside * hi_outside / (blur_radius * blur_radius));
    glow = max(glow, 0.4 * hi_brightness * hi_gaussian);
    if (gid.y < 3 || gid.y >= height_px - 2) glow = 0.0;

    float fade_zone = max(24.0, width * 0.06);
    float edge_fade = clampf(min(float(gid.x), width - 1.0 - float(gid.x)) / fade_zone, 0.0, 1.0);
    result = mix(result, state_color, glow * edge_fade);

    // Source-over core strokes in the same dim-to-bright order as PIL.
    for (int i = 0; i < 4; ++i) {
        float core_width = max(1.0, round(thicknesses[i] * stroke_scale));
        float alpha = alphas[i] * brightness * coverage(distances[i], core_width) * edge_fade;
        result = mix(result, state_color, alpha);
    }
    float highlight_alpha = 0.95 * hi_brightness * coverage(hi_distance, hi_width) * edge_fade;
    result = mix(result, state_color, highlight_alpha);

    // BGRA8Unorm drawable; Metal's float write handles channel packing.
    output.write(float4(result, 1.0), gid);
}
"""


def _default_device():
    try:
        import Metal

        return Metal.MTLCreateSystemDefaultDevice()
    except ImportError:
        pass

    import objc

    framework = ctypes.CDLL(_METAL_FRAMEWORK)
    framework.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
    pointer = framework.MTLCreateSystemDefaultDevice()
    if not pointer:
        return None
    return objc.objc_object(c_void_p=ctypes.c_void_p(pointer))


class MetalHeroRenderer:
    """Compute shader + CAMetalLayer owned by the main AppKit content view."""

    def __init__(self, device, layer, pipeline, command_queue):
        self.device = device
        self.layer = layer
        self.pipeline = pipeline
        self.command_queue = command_queue
        self._size = (0, 0)

    @classmethod
    def try_create(cls, backing_scale: float):
        try:
            from Quartz import CAMetalLayer, kCAFilterLinear, kCAGravityResize

            device = _default_device()
            if device is None:
                return None
            library = device.newLibraryWithSource_options_error_(_SHADER, None, None)
            if isinstance(library, tuple):
                library, error = library
                if library is None:
                    raise RuntimeError(str(error))
            function = library.newFunctionWithName_("hero_wave")
            pipeline = device.newComputePipelineStateWithFunction_error_(function, None)
            if isinstance(pipeline, tuple):
                pipeline, error = pipeline
                if pipeline is None:
                    raise RuntimeError(str(error))
            command_queue = device.newCommandQueue()
            if pipeline is None or command_queue is None:
                return None

            layer = CAMetalLayer.layer()
            layer.setName_("WayfinderHeroWaveformMetal")
            layer.setDevice_(device)
            layer.setPixelFormat_(_PIXEL_FORMAT_BGRA8_UNORM)
            # The compute kernel writes the drawable (needs ShaderWrite usage);
            # framebufferOnly=True renders solid magenta on M3.
            layer.setFramebufferOnly_(False)
            try:
                from Quartz import CGColorSpaceCreateWithName, kCGColorSpaceSRGB

                # Match Tk's sRGB card colour (see hero_renderer.m).
                layer.setColorspace_(CGColorSpaceCreateWithName(kCGColorSpaceSRGB))
            except Exception:
                pass
            layer.setDisplaySyncEnabled_(True)
            layer.setPresentsWithTransaction_(False)
            layer.setOpaque_(True)
            layer.setMasksToBounds_(True)
            layer.setContentsGravity_(kCAGravityResize)
            layer.setMagnificationFilter_(kCAFilterLinear)
            layer.setMinificationFilter_(kCAFilterLinear)
            layer.setContentsScale_(float(backing_scale))
            layer.setZPosition_(1000.0)
            return cls(device, layer, pipeline, command_queue)
        except Exception:
            return None

    def set_size(self, width: int, height: int) -> None:
        self._size = (int(width), int(height))
        self.layer.setDrawableSize_((float(width), float(height)))

    def render(
        self,
        *,
        width: int,
        height: int,
        time_value: float,
        audio_level: float,
        morph: float,
        stroke_scale: float,
        color_rgb: tuple[int, int, int],
        bg_rgb: tuple[int, int, int],
    ) -> bool:
        encoder = None
        try:
            if (width, height) != self._size:
                self.set_size(width, height)
            drawable = self.layer.nextDrawable()
            if drawable is None:
                return False
            values = (
                float(width), float(height), float(time_value), float(audio_level),
                float(morph), float(stroke_scale), 0.0, 0.0,
                *(channel / 255.0 for channel in color_rgb), 1.0,
                *(channel / 255.0 for channel in bg_rgb), 1.0,
            )
            packed = struct.pack("16f", *values)
            command_buffer = self.command_queue.commandBuffer()
            encoder = command_buffer.computeCommandEncoder()
            encoder.setComputePipelineState_(self.pipeline)
            encoder.setTexture_atIndex_(drawable.texture(), 0)
            encoder.setBytes_length_atIndex_(packed, 64, 0)
            encoder.dispatchThreads_threadsPerThreadgroup_(
                (int(width), int(height), 1),
                (16, 8, 1),
            )
            encoder.endEncoding()
            command_buffer.presentDrawable_(drawable)
            command_buffer.commit()
            return True
        except Exception as exc:
            if encoder is not None:
                try:
                    encoder.endEncoding()
                except Exception:
                    pass
            if not getattr(self, "_error_logged", False):
                print(f"[ui] Metal hero render failed: {exc}", flush=True)
                self._error_logged = True
            return False

    def close(self) -> None:
        try:
            self.layer.removeFromSuperlayer()
        except Exception:
            pass


def _native_library_candidates() -> list[Path]:
    candidates = []
    override = os.environ.get("WAYFINDER_HERO_RENDERER_LIB")
    if override:
        candidates.append(Path(override).expanduser())
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.extend(
            (
                bundle / "lib" / "libwayfinder_hero.dylib",
                bundle / "Frameworks" / "lib" / "libwayfinder_hero.dylib",
                Path(sys.executable).parents[1]
                / "Resources"
                / "lib"
                / "libwayfinder_hero.dylib",
            )
        )
    source_root = Path(__file__).resolve().parents[3]
    candidates.append(
        source_root / "build" / "macos-native" / "lib" / "libwayfinder_hero.dylib"
    )
    return candidates


class NativeMetalHeroRenderer:
    """ctypes bridge to the native 30 fps Metal scheduler."""

    def __init__(self, library, handle):
        self.library = library
        self.handle = handle

    @classmethod
    def try_create(cls, parent_layer, backing_scale: float):
        try:
            import objc

            path = next((path for path in _native_library_candidates() if path.is_file()), None)
            if path is None:
                return None
            library = ctypes.CDLL(str(path))
            library.wf_hero_create.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_char_p]
            library.wf_hero_create.restype = ctypes.c_void_p
            library.wf_hero_set_frame.argtypes = [
                ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_int, ctypes.c_int
            ]
            library.wf_hero_set_state.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                *([ctypes.c_float] * 8),
            ]
            library.wf_hero_set_hidden.argtypes = [ctypes.c_void_p, ctypes.c_int]
            library.wf_hero_destroy.argtypes = [ctypes.c_void_p]
            handle = library.wf_hero_create(
                ctypes.c_void_p(objc.pyobjc_id(parent_layer)),
                float(backing_scale),
                _SHADER.encode("utf-8"),
            )
            return cls(library, handle) if handle else None
        except Exception:
            return None

    def set_frame(self, x: float, y: float, width: int, height: int) -> None:
        self.library.wf_hero_set_frame(
            self.handle, float(x), float(y), int(width), int(height)
        )

    def set_state(
        self,
        *,
        active: bool,
        audio_level: float,
        stroke_scale: float,
        color_rgb: tuple[int, int, int],
        bg_rgb: tuple[int, int, int],
    ) -> None:
        values = [channel / 255.0 for channel in (*color_rgb, *bg_rgb)]
        self.library.wf_hero_set_state(
            self.handle,
            int(bool(active)),
            float(audio_level),
            *values,
            float(stroke_scale),
        )

    def set_hidden(self, hidden: bool) -> None:
        self.library.wf_hero_set_hidden(self.handle, int(bool(hidden)))

    def close(self) -> None:
        if self.handle:
            self.library.wf_hero_destroy(self.handle)
            self.handle = None
