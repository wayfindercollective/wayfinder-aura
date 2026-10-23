"""Wheel ownership is based on available movement, not keyboard focus."""

import pytest

from wayfinder.ui.text_scroll import text_wheel_at_edge


@pytest.mark.parametrize("view,delta,expected", [
    ((0, 1), 120, True), ((0, 1), -120, True),
    ((0, 1), 1, True), ((0, 1), -1, True),
    ((0, 0.2), 120, True), ((0, 0.2), -120, False),
    ((0.8, 1), 120, False), ((0.8, 1), -120, True),
    ((0.3, 0.5), 120, False), ((0.3, 0.5), -120, False),
    ((0, 1), 0, False),
])
def test_only_an_edge_in_the_requested_direction_hands_off(view, delta, expected):
    assert text_wheel_at_edge(view, delta=delta) is expected


@pytest.mark.parametrize("num", [4, 5])
def test_old_x11_button_wheels_do_not_need_a_delta(num):
    assert text_wheel_at_edge((0, 1), num=num)
    assert not text_wheel_at_edge((0.2, 0.8), num=num)


@pytest.mark.parametrize("delta", [120, -120, 1, -1])
def test_shift_wheel_is_never_treated_as_vertical_page_scrolling(delta):
    assert not text_wheel_at_edge((0, 1), delta=delta, state=1)
