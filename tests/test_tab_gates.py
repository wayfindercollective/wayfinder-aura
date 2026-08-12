"""Which tabs a licence gates, in one place.

_switch_tab hardcoded the style/tone_system pairing inline, so nothing else
could tell a locked tab from a broken one — the live smoke test asserted the
style tab switches and failed on every unlicensed build, which reads as a
regression rather than a working gate.
"""

from wayfinder.ui.tab_gates import TAB_FEATURES, feature_for_tab, locked_tabs


def test_style_tab_is_gated_on_the_tone_system_feature():
    assert TAB_FEATURES["style"] == "tone_system"
    assert feature_for_tab("style") == "tone_system"


def test_ungated_tabs_have_no_feature():
    for tab in ("dictate", "settings", "history"):
        assert feature_for_tab(tab) is None


def test_locked_tabs_lists_only_what_the_gate_denies():
    assert locked_tabs(lambda _f: False) == ["style"]
    assert locked_tabs(lambda _f: True) == []


def test_locked_tabs_treats_a_raising_gate_as_locked():
    """A broken FeatureGate must not silently unlock premium tabs."""

    def boom(_feature):
        raise RuntimeError("licence backend down")

    assert locked_tabs(boom) == ["style"]


def test_locked_tabs_is_sorted_for_a_stable_breadcrumb():
    assert locked_tabs(lambda _f: False) == sorted(locked_tabs(lambda _f: False))
