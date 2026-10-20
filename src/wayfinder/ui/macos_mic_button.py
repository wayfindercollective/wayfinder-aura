"""Retina presentation of the shared mic pixels over the existing Tk hit target.

Aqua PhotoImages are one pixel per point even on a Retina screen. A static
CALayer keeps the renderer's backing-resolution pixels instead. The existing
recording loop supplies pulse frames; this adapter has no animation timer.
"""

from __future__ import annotations

import sys

from wayfinder.ui.mic_button import render_mic_button


class MacOSMicButton:
    def __init__(self, owner, canvas, ns_window, layer):
        self.owner = owner
        self.canvas = canvas
        self.ns_window = ns_window
        self.layer = layer
        self._appearance = None
        self._cache = {}
        self._key = None
        self._geometry = None
        self._hidden = False
        self._ready = False
        self._closed = False
        self._layout_job = None
        self._bindings = []
        ancestors = []
        widget = canvas
        while widget is not None:
            ancestors.append(widget)
            if widget is owner:
                break
            widget = getattr(widget, "master", None)
        self._ancestors = tuple(ancestors)

    @classmethod
    def try_create(cls, owner, canvas):
        if sys.platform != "darwin":
            return None
        result = None
        try:
            from AppKit import NSApplication
            from Quartz import CALayer, kCAGravityResize

            if not canvas.winfo_ismapped() or min(canvas.winfo_width(), canvas.winfo_height()) <= 1:
                return None
            window = next((w for w in NSApplication.sharedApplication().windows()
                           if str(w.title() or "") == str(owner.title())), None)
            if window is None or window.contentView().layer() is None:
                return None
            layer = CALayer.layer()
            layer.setName_("WayfinderMicButton")
            layer.setContentsGravity_(kCAGravityResize)
            layer.setMasksToBounds_(True)
            layer.setOpaque_(True)
            layer.setZPosition_(1000.0)
            layer.setHidden_(True)
            result = cls(owner, canvas, window, layer)
            window.contentView().layer().addSublayer_(layer)
            for sequence in ("<Configure>", "<Map>", "<Unmap>", "<Destroy>"):
                binding = owner.bind(sequence, result._on_layout, add="+")
                result._bindings.append((sequence, binding))
            return result
        except Exception:
            if result is not None:
                result.close()
            return None

    def _on_layout(self, event) -> None:
        if self._closed or event.widget not in self._ancestors:
            return
        if event.type == "17" or str(event.type) == "Destroy":
            self.close()
        elif self._layout_job is None:
            self._layout_job = self.owner.after_idle(self._sync_layout)

    def _sync_layout(self) -> None:
        self._layout_job = None
        if not self._closed:
            self.update_geometry()

    @staticmethod
    def _contents(image):
        from Foundation import NSData
        from Quartz import (
            CGColorSpaceCreateWithName,
            CGDataProviderCreateWithCFData,
            CGImageCreate,
            kCGBitmapByteOrder32Big,
            kCGColorSpaceSRGB,
            kCGImageAlphaPremultipliedLast,
            kCGRenderingIntentDefault,
        )

        rgba = image.convert("RGBA")  # Shared renderer is opaque; alpha is 255.
        raw = rgba.tobytes("raw", "RGBA")
        data = NSData.dataWithBytes_length_(raw, len(raw))
        contents = CGImageCreate(
            rgba.width, rgba.height, 8, 32, rgba.width * 4,
            CGColorSpaceCreateWithName(kCGColorSpaceSRGB),
            kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big,
            CGDataProviderCreateWithCFData(data), None, False,
            kCGRenderingIntentDefault,
        )
        if contents is None:
            raise RuntimeError("Could not create the mic button image")
        return data, contents

    def render(self, color, *, card, ink, light, active=False, pulse=None, hover=False, hidden=False) -> bool:
        appearance = (color, card, ink, light, active, pulse, hover)
        if self._ready and appearance == self._appearance and bool(hidden) == self._hidden:
            return True  # Repeated quantized pulse: no Tk/Quartz work at all.
        self._hidden = bool(hidden)
        self._appearance = appearance
        return self.update_geometry()

    def update_geometry(self) -> bool:
        if self._closed or self._appearance is None:
            return False
        try:
            from Quartz import CATransaction, CGRectMake

            if not self.canvas.winfo_viewable():
                self._ready = False
                self._show(False)
                return True
            width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
            if min(width, height) <= 1:
                return False
            backing = float(self.ns_window.backingScaleFactor() or 1.0)
            pixels = round(max(width, height) * backing)
            key = (pixels, *self._appearance)
            if key not in self._cache:
                color, card, ink, light, active, pulse, hover = self._appearance
                image = render_mic_button(
                    pixels, color, card=card, ink=ink, light=light,
                    active=active, pulse=pulse, hover=hover,
                )
                if len(self._cache) >= 32:
                    self._cache.clear()
                self._cache[key] = self._contents(image)
            x = self.canvas.winfo_rootx() - self.owner.winfo_rootx()
            top = self.canvas.winfo_rooty() - self.owner.winfo_rooty()
            y = self.ns_window.contentView().bounds().size.height - top - height
            geometry = (x, y, width, height, backing)
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            if geometry != self._geometry:
                self.layer.setFrame_(CGRectMake(x, y, width, height))
                self.layer.setContentsScale_(backing)
                self._geometry = geometry
            if key != self._key:
                self.layer.setContents_(self._cache[key][1])
                self._key = key
            self.layer.setHidden_(self._hidden)
            CATransaction.commit()
            self._ready = True
            return True
        except Exception:
            self._ready = False
            return False

    def _show(self, visible) -> None:
        try:
            from Quartz import CATransaction

            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self.layer.setHidden_(not visible)
            CATransaction.commit()
        except Exception:
            pass

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = bool(hidden)
        if hidden:
            self._show(False)
        else:
            self.update_geometry()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._layout_job is not None:
                self.owner.after_cancel(self._layout_job)
            for sequence, binding in self._bindings:
                self.owner.unbind(sequence, binding)
        except Exception:
            pass  # Tk can already be destroying the window.
        try:
            self.layer.removeFromSuperlayer()
        except Exception:
            pass
        self._cache.clear()
