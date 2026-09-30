"""Core Animation presentation surface for the macOS in-app hero waveform.

The shared renderer still produces the exact approved PIL pixels. On Aqua we
hand those pixels to a dedicated CALayer instead of mutating a Tk PhotoImage;
that lets WindowServer composite only the 64px hero strip rather than asking Tk
to rasterize the surrounding CustomTkinter window on every 30 fps frame.
"""

from __future__ import annotations

import sys


class MacOSHeroLayer:
    """A CALayer positioned over one Tk canvas in the app's content view."""

    def __init__(
        self,
        owner,
        canvas,
        ns_window,
        layer,
        color_space,
        metal_renderer=None,
        native_renderer=None,
    ):
        self.owner = owner
        self.canvas = canvas
        self.ns_window = ns_window
        self.layer = layer
        self.color_space = color_space
        self.metal_renderer = metal_renderer
        self.native_renderer = native_renderer
        self._last_contents = None
        self._last_data = None
        self._size = (0, 0)

    @classmethod
    def try_create(cls, owner, canvas):
        """Attach to the mapped Wayfinder Tk window, or return ``None``."""
        if sys.platform != "darwin":
            return None
        try:
            from AppKit import NSApplication
            from Quartz import CALayer, CGColorSpaceCreateWithName, kCAFilterLinear, kCAGravityResize, kCGColorSpaceSRGB

            owner.update_idletasks()
            if canvas.winfo_width() <= 1 or canvas.winfo_height() <= 1:
                return None

            title = str(owner.title())
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
            parent = ns_window.contentView().layer()
            if parent is None:
                return None

            from wayfinder.ui.macos_hero_metal import (
                MetalHeroRenderer,
                NativeMetalHeroRenderer,
            )

            backing_scale = float(ns_window.backingScaleFactor())
            native_renderer = NativeMetalHeroRenderer.try_create(
                parent, backing_scale
            )
            if native_renderer is not None:
                # Seed the native timer with the card it sits on. It otherwise
                # draws its compiled-in default until the first state push,
                # which the idle path may never send.
                try:
                    r16, g16, b16 = canvas.winfo_rgb(canvas.cget("bg"))
                    native_renderer.set_state(
                        active=False,
                        audio_level=0.0,
                        stroke_scale=1.0,
                        color_rgb=(91, 143, 212),
                        bg_rgb=(r16 >> 8, g16 >> 8, b16 >> 8),
                    )
                except Exception:
                    pass
            metal_renderer = None
            if native_renderer is not None:
                layer = None
            else:
                metal_renderer = MetalHeroRenderer.try_create(backing_scale)
                if metal_renderer is not None:
                    layer = metal_renderer.layer
                else:
                    layer = CALayer.layer()
                    layer.setName_("WayfinderHeroWaveform")
                    layer.setMasksToBounds_(True)
                    layer.setOpaque_(True)
                    layer.setContentsGravity_(kCAGravityResize)
                    layer.setMagnificationFilter_(kCAFilterLinear)
                    layer.setMinificationFilter_(kCAFilterLinear)
                    layer.setContentsScale_(backing_scale)
                    layer.setZPosition_(1000.0)
                parent.addSublayer_(layer)

            result = cls(
                owner,
                canvas,
                ns_window,
                layer,
                CGColorSpaceCreateWithName(kCGColorSpaceSRGB),
                metal_renderer,
                native_renderer,
            )
            result.update_geometry()
            return result
        except Exception:
            return None

    def update_geometry(self) -> bool:
        """Align the native layer with the Tk canvas in content-view points."""
        try:
            from Quartz import CATransaction, CGRectMake

            width = int(self.canvas.winfo_width())
            height = int(self.canvas.winfo_height())
            if width <= 1 or height <= 1:
                return False
            content_height = float(self.ns_window.contentView().bounds().size.height)
            x = float(self.canvas.winfo_rootx() - self.owner.winfo_rootx())
            top = float(self.canvas.winfo_rooty() - self.owner.winfo_rooty())
            y = content_height - top - height
            if self.native_renderer is not None:
                self.native_renderer.set_frame(x, y, width, height)
            else:
                CATransaction.begin()
                CATransaction.setDisableActions_(True)
                self.layer.setFrame_(CGRectMake(x, y, width, height))
                CATransaction.commit()
            self._size = (width, height)
            if self.metal_renderer is not None:
                self.metal_renderer.set_size(width, height)
            return True
        except Exception:
            return False

    def render_wave(
        self,
        *,
        width: int,
        height: int,
        time_value: float,
        audio_level: float,
        morph: float,
        active: bool,
        stroke_scale: float,
        color_rgb: tuple[int, int, int],
        bg_rgb: tuple[int, int, int],
    ) -> bool:
        """Render directly with Metal when its pipeline is available."""
        if self.native_renderer is not None:
            # Colours first: the Python idle loop hands every later frame to the
            # native timer, so an early geometry bail-out must not leave the
            # renderer on its compiled-in default background.
            self.native_renderer.set_state(
                active=active,
                audio_level=audio_level,
                stroke_scale=stroke_scale,
                color_rgb=color_rgb,
                bg_rgb=bg_rgb,
            )
            if (width, height) != self._size and not self.update_geometry():
                return False
            self.native_renderer.set_hidden(False)
            return True
        renderer = self.metal_renderer
        if renderer is None:
            return False
        if (width, height) != self._size and not self.update_geometry():
            return False
        rendered = renderer.render(
            width=width,
            height=height,
            time_value=time_value,
            audio_level=audio_level,
            morph=morph,
            stroke_scale=stroke_scale,
            color_rgb=color_rgb,
            bg_rgb=bg_rgb,
        )
        if rendered:
            self.set_hidden(False)
        return rendered

    def set_image(self, image) -> bool:
        """Present an exact RGB PIL frame through Core Animation."""
        try:
            from Foundation import NSData
            from Quartz import (
                CATransaction,
                CGDataProviderCreateWithCFData,
                CGImageCreate,
                kCGBitmapByteOrder32Big,
                kCGImageAlphaPremultipliedLast,
                kCGRenderingIntentDefault,
            )

            width, height = image.size
            if (width, height) != self._size and not self.update_geometry():
                return False
            rgba = image.convert("RGBA")
            raw = rgba.tobytes("raw", "RGBA")
            data = NSData.dataWithBytes_length_(raw, len(raw))
            provider = CGDataProviderCreateWithCFData(data)
            contents = CGImageCreate(
                width,
                height,
                8,
                32,
                width * 4,
                self.color_space,
                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big,
                provider,
                None,
                False,
                kCGRenderingIntentDefault,
            )
            if contents is None:
                return False
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self.layer.setContents_(contents)
            self.layer.setHidden_(False)
            CATransaction.commit()
            # Keep the backing objects explicitly alive until the next frame.
            self._last_data = data
            self._last_contents = contents
            return True
        except Exception:
            return False

    def set_hidden(self, hidden: bool) -> None:
        if self.native_renderer is not None:
            self.native_renderer.set_hidden(hidden)
            return
        try:
            from Quartz import CATransaction

            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self.layer.setHidden_(bool(hidden))
            CATransaction.commit()
        except Exception:
            pass

    def close(self) -> None:
        if self.native_renderer is not None:
            self.native_renderer.close()
        elif self.metal_renderer is not None:
            self.metal_renderer.close()
        else:
            try:
                self.layer.removeFromSuperlayer()
            except Exception:
                pass
        self._last_contents = None
        self._last_data = None
