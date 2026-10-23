"""The collapsed Advanced section of Settings: where rows are built, which groups show."""

import types

from wayfinder.ui.advanced_section import ADVANCED_GROUPS, advanced_slot, groups_to_show


class _Frame:
    def __init__(self, exists=True):
        self._exists = exists

    def winfo_exists(self):
        if isinstance(self._exists, Exception):
            raise self._exists
        return self._exists


def test_group_order_is_stable_and_unique():
    keys = [key for key, _ in ADVANCED_GROUPS]
    assert keys == ["audio", "processing", "system", "overlay", "benchmark"]
    assert len(set(keys)) == len(keys)


def test_every_group_has_a_heading_for_its_collapsible_header():
    # The builder uppercases the heading for each section's header row.
    assert all(isinstance(title, str) and title.strip() for _key, title in ADVANCED_GROUPS)


def test_slot_uses_the_advanced_content_frame_when_built():
    content, fallback = _Frame(), _Frame()
    app = types.SimpleNamespace(_advanced_groups={"processing": (_Frame(), content)})
    assert advanced_slot(app, "processing", fallback) is content


def test_slot_falls_back_when_the_section_was_never_built():
    fallback = _Frame()
    assert advanced_slot(types.SimpleNamespace(), "processing", fallback) is fallback
    assert advanced_slot(types.SimpleNamespace(_advanced_groups={}), "audio", fallback) is fallback


def test_slot_falls_back_for_an_unknown_group():
    fallback = _Frame()
    app = types.SimpleNamespace(_advanced_groups={"audio": (_Frame(), _Frame())})
    assert advanced_slot(app, "nope", fallback) is fallback


def test_slot_falls_back_when_the_frame_was_destroyed_or_broken():
    fallback = _Frame()
    destroyed = types.SimpleNamespace(_advanced_groups={"audio": (_Frame(), _Frame(exists=False))})
    broken = types.SimpleNamespace(
        _advanced_groups={"audio": (_Frame(), _Frame(exists=RuntimeError("tk gone")))}
    )
    assert advanced_slot(destroyed, "audio", fallback) is fallback
    assert advanced_slot(broken, "audio", fallback) is fallback


def test_only_groups_with_packed_rows_show_in_display_order():
    assert groups_to_show({"overlay": True, "audio": True, "system": False}) == ["audio", "overlay"]


def test_remote_mode_hides_processing_and_benchmark_groups():
    packed = {"audio": True, "processing": False, "system": True, "overlay": True, "benchmark": False}
    assert groups_to_show(packed) == ["audio", "system", "overlay"]


def test_nothing_packed_shows_nothing():
    assert groups_to_show({}) == []


def test_mode_settings_rebuild_clears_advanced_processing_rows():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    source = (repo / "wayfinder_main.py").read_text(encoding="utf-8")
    body = source.split("def _build_mode_settings(self, mode: str) -> None:", 1)[1].split(
        "\n    def ", 1
    )[0]
    assert 'advanced_slot(self, "processing", None)' in body
    assert "for widget in _advanced_processing.winfo_children():" in body


