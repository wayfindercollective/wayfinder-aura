"""Native button presentation; no recording, key events, or config writes."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wayfinder.ui.macos_mic_button import MacOSMicButton


def test_native_mic_is_not_created_on_other_platforms(monkeypatch):
    import wayfinder.ui.macos_mic_button as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert MacOSMicButton.try_create(object(), object()) is None


def test_layout_events_are_filtered_coalesced_and_cancelled_on_close():
    owner = SimpleNamespace(after_idle=Mock(return_value="job"), after_cancel=Mock(), unbind=Mock())
    parent = SimpleNamespace(master=owner)
    canvas = SimpleNamespace(master=parent)
    layer = Mock()
    native = MacOSMicButton(owner, canvas, None, layer)
    native.update_geometry = Mock()
    native._bindings = [("<Configure>", "binding")]
    native._on_layout(SimpleNamespace(widget=object(), type="Configure"))
    owner.after_idle.assert_not_called()
    for widget in (canvas, parent, owner):
        native._on_layout(SimpleNamespace(widget=widget, type="Configure"))
    owner.after_idle.assert_called_once()
    owner.after_idle.call_args.args[0]()
    native.update_geometry.assert_called_once()
    native._on_layout(SimpleNamespace(widget=canvas, type="Configure"))
    native.close()
    owner.after_cancel.assert_called_once_with("job")
    owner.unbind.assert_called_once_with("<Configure>", "binding")
    layer.removeFromSuperlayer.assert_called_once()
    native._on_layout(SimpleNamespace(widget=owner, type="Configure"))
    assert owner.after_idle.call_count == 2


@pytest.mark.ui
@pytest.mark.skipif(sys.platform != "darwin", reason="Native Aqua image presentation")
def test_native_mic_is_retina_sized_cached_and_follows_layout(monkeypatch):
    import tkinter as tk

    import customtkinter as ctk
    from Quartz import CGImageGetHeight, CGImageGetWidth

    import wayfinder.ui.macos_mic_button as mod

    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    root.title("Aura Retina mic QA")
    root.geometry("320x280+3000+3000")
    parent = ctk.CTkFrame(root)
    parent.pack(fill="x", pady=20)
    canvas = tk.Canvas(parent, width=80, height=80, bg="#151B25", highlightthickness=0)
    canvas.pack()
    renderer = Mock(wraps=mod.render_mic_button)
    monkeypatch.setattr(mod, "render_mic_button", renderer)
    native = None
    try:
        root.update()
        native = MacOSMicButton.try_create(root, canvas)
        assert native is not None
        options = {"card": "#151B25", "ink": "#0B0F15", "light": "#E8E8E8"}
        assert native.render("#5B8FD4", **options)
        backing = native.ns_window.backingScaleFactor()
        contents = native.layer.contents()
        assert CGImageGetWidth(contents) == round(canvas.winfo_width() * backing)
        assert CGImageGetHeight(contents) == round(canvas.winfo_height() * backing)
        assert native.layer.frame().size.width == canvas.winfo_width()
        assert native.layer.contentsScale() == backing
        assert not native.layer.isHidden()
        target = os.environ.get("AURA_UI_SNAPSHOTS")
        if target:
            from PIL import Image

            data, contents = native._cache[native._key]
            image = Image.frombytes("RGBA", (CGImageGetWidth(contents), CGImageGetHeight(contents)), bytes(data))
            image.save(Path(target) / "mic_retina.png")
        sync = Mock(wraps=native.update_geometry)
        monkeypatch.setattr(native, "update_geometry", sync)
        assert native.render("#5B8FD4", **options)
        renderer.assert_called_once()
        sync.assert_not_called()  # An unchanged frame adds no Tcl/Quartz traffic.
        assert native.render("#5B8FD4", hover=True, **options)
        assert renderer.call_count == 2
        for pulse in (0.8, 0.9, 1.0, 0.9, 0.8):
            assert native.render("#E8707F", pulse=pulse, **options)
        assert renderer.call_count == 5  # Repeated pulse frames are cache hits.

        before_size = (canvas.winfo_width(), canvas.winfo_height())
        root.geometry("320x420+3000+3000")
        parent.pack_configure(pady=(60, 0))
        root.update()
        frame = native.layer.frame()
        actual_top = native.ns_window.contentView().bounds().size.height - frame.origin.y - frame.size.height
        assert actual_top == pytest.approx(canvas.winfo_rooty() - root.winfo_rooty(), abs=1)
        assert (canvas.winfo_width(), canvas.winfo_height()) == before_size
        assert renderer.call_count == 5

        native.set_hidden(True)  # A covering Ultra/dropdown panel owns the pixels.
        root.geometry("360x440+3000+3000")
        root.update()
        assert native.layer.isHidden()
        native.set_hidden(False)
        assert not native.layer.isHidden()
        parent.pack_forget()
        root.update()
        assert native.layer.isHidden()
        parent.pack(fill="x")
        root.update()
        assert not native.layer.isHidden()
        # A display/backing-scale change re-renders, not stretches, the pixels.
        real_window = native.ns_window
        native.ns_window = SimpleNamespace(
            contentView=real_window.contentView, backingScaleFactor=lambda: 1.0,
        )
        assert native.update_geometry()
        assert CGImageGetWidth(native.layer.contents()) == canvas.winfo_width()
        native.ns_window = real_window
        assert native.update_geometry()
        assert CGImageGetWidth(native.layer.contents()) == round(canvas.winfo_width() * backing)
        native.close()
        assert native.layer.superlayer() is None
    finally:
        if native is not None:
            native.close()
        root.destroy()
