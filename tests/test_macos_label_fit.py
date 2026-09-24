"""macOS label fitting: stacked text never asks for more width than its card."""
from __future__ import annotations

from types import SimpleNamespace

from wayfinder.ui.macos_label_fit import MIN_WRAP, fit_label, fitted_wraplength

LONG = "Times your full setup on a 10-second clip: speech-to-text + cleanup, measured warmed up."


def test_designed_wrap_is_clamped_to_the_card():
    # 520 px designed for the Linux window, but the Mac card is 440 px wide.
    assert fitted_wraplength(LONG, 520, 440, 40) == 440 - 40 - 4


def test_designed_wrap_is_kept_when_the_card_is_wider():
    assert fitted_wraplength(LONG, 300, 900, 40) == 300


def test_long_unwrapped_status_line_gets_wrapped():
    assert fitted_wraplength(LONG, 0, 500, 0) == 496


def test_short_labels_and_single_words_are_left_alone():
    assert fitted_wraplength("Microphone", 0, 400, 0) is None
    assert fitted_wraplength("a" * 80, 0, 400, 0) is None  # no break points
    assert fitted_wraplength(LONG, 0, 10, 0) is None      # not laid out yet


def test_never_narrower_than_the_minimum():
    assert fitted_wraplength(LONG, 520, 100, 40) == MIN_WRAP


class _Label:
    def __init__(self, *, side="top", wrap=0, justify="center", anchor="w", width=400, padx=10):
        self.options = {"text": LONG, "wraplength": wrap, "justify": justify}
        self._info = {"side": side, "anchor": anchor, "padx": padx, "ipadx": 0}
        self.master = SimpleNamespace(winfo_width=lambda: width)

    def pack_info(self):
        return self._info

    def cget(self, key):
        return self.options[key]

    def configure(self, **kw):
        self.options.update(kw)


def test_stacked_label_is_fitted_and_left_justified():
    label = _Label(width=400, padx=10)
    fit_label(label)
    assert label.options["wraplength"] == 400 - 20 - 4
    assert label.options["justify"] == "left"


def test_labels_in_rows_are_left_alone():
    label = _Label(side="left", wrap=520)
    fit_label(label)
    assert label.options["wraplength"] == 520


def test_refit_follows_a_wider_card_back_up_to_the_design():
    label = _Label(wrap=520, width=300, padx=0)
    fit_label(label)
    assert label.options["wraplength"] == 296
    label.master = SimpleNamespace(winfo_width=lambda: 1000)
    fit_label(label)
    assert label.options["wraplength"] == 520  # designed value, not the first clamp
