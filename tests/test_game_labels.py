"""Community game labels: the daily labels document, intake permission and
which label a Games-tab row shows (design docs/plans/game-reports-design-2026-10-15.md,
sections 3.4, 4.3, 4.4 and 5.3). requests is faked at the transport.
"""

from __future__ import annotations

import pytest

from tests.game_report_fixtures import Transport, game, http_date, labels_doc, line
from wayfinder.core import game_labels as gl
from wayfinder.core import macos_game_chat as gc
from wayfinder.core.macos_game_chat import GameEntry

T = 1_760_000_000.0          # a fixed "now" (seconds)
HOUR, DAY = 3600, 86400


@pytest.fixture
def labels(tmp_path, monkeypatch):
    monkeypatch.setattr(gl, "get_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(gl, "_is_packaged", lambda: True)
    return gl


@pytest.fixture
def transport(monkeypatch):
    return Transport(monkeypatch)


def fetch(labels, transport, doc, *, at=T, date=None, generated=None, status=200):
    """One refresh at local time ``at`` answered with ``doc``."""
    if isinstance(doc, dict) and "generatedAt" in doc:
        doc = dict(doc, generatedAt=int((generated if generated is not None else at) * 1000))
    headers = {} if date is False else {"Date": http_date(date if date is not None else at)}
    transport.answer((status, doc, headers))
    return labels.refresh(now=at)


# --- permission lifetime (section 4.4) ----------------------------------------------------

def test_permission_lasts_24_hours_after_a_fresh_fetch(labels, transport):
    assert fetch(labels, transport, labels_doc()) is True
    assert labels.intake_open(T + 1) is True
    assert labels.intake_open(T + DAY - 1) is True
    assert labels.intake_open(T + DAY + 1) is False


def test_a_failed_refresh_never_extends_permission(labels, transport):
    fetch(labels, transport, labels_doc())
    transport.answer(ConnectionError("offline"))
    assert labels.refresh(now=T + DAY + 1) is False
    assert labels.intake_open(T + DAY + 2) is False
    transport.answer((503, {"ok": False}, {"Date": http_date(T + 2 * DAY + 2)}))
    assert labels.refresh(now=T + 2 * DAY + 2) is False
    assert labels.intake_open(T + 2 * DAY + 3) is False


def test_a_clock_moved_back_counts_as_expired(labels, transport):
    fetch(labels, transport, labels_doc())
    assert labels.intake_open(T - 60) is False


def test_a_stale_document_grants_nothing_and_keeps_the_old_cache(labels, transport):
    old = labels_doc(reportable=("albion-online",))
    fetch(labels, transport, old)
    stale = labels_doc(reportable=("world-of-warcraft",))
    later = T + DAY + 10
    assert fetch(labels, transport, stale, at=later, generated=later - HOUR - 1) is False
    assert labels.intake_open(later + 1) is False
    assert labels.reportable() == frozenset({"albion-online"})


def test_without_a_date_header_the_local_clock_judges_freshness(labels, transport):
    assert fetch(labels, transport, labels_doc(), date=False) is True
    assert labels.intake_open(T + 1) is True
    assert fetch(labels, transport, labels_doc(), at=T + DAY + 5, date=False,
                 generated=T + DAY + 5 - 2 * HOUR) is False


def test_closed_intake_offers_nothing(labels, transport):
    fetch(labels, transport, labels_doc(accepting=False))
    assert labels.intake_open(T + 1) is False
    assert labels.reportable() == frozenset({"world-of-warcraft"})


# --- fetching -----------------------------------------------------------------------------

def test_at_most_one_fetch_a_day_and_it_carries_nothing(labels, transport):
    fetch(labels, transport, labels_doc())
    assert labels.refresh(now=T + HOUR) is False        # not due: no request
    assert len(transport.gets()) == 1
    request = transport.gets()[0]
    assert request.url == gl.GAME_LABELS_URL and request.body is None
    assert request.timeout == 10
    assert not any(h.lower() in ("cookie", "authorization") for h in request.headers)
    fetch(labels, transport, labels_doc(), at=T + DAY + 1)
    assert len(transport.gets()) == 2


def test_a_source_checkout_fetches_only_with_an_override(labels, transport, monkeypatch):
    monkeypatch.setattr(gl, "_is_packaged", lambda: False)
    monkeypatch.delenv("WAYFINDER_AURA_GAME_LABELS_URL", raising=False)
    assert labels.refresh(now=T) is False
    assert transport.sent == []
    monkeypatch.setenv("WAYFINDER_AURA_GAME_LABELS_URL", "https://dev.example/aura/game-labels")
    monkeypatch.setattr(gl, "GAME_LABELS_URL", "https://dev.example/aura/game-labels")
    assert fetch(labels, transport, labels_doc(), at=T + 1) is True
    assert transport.gets()[0].url == "https://dev.example/aura/game-labels"


def test_the_default_url_and_override(monkeypatch):
    import importlib
    monkeypatch.delenv("WAYFINDER_AURA_GAME_LABELS_URL", raising=False)
    assert importlib.reload(gl).GAME_LABELS_URL == "https://wayfindercollective.io/aura/game-labels"
    monkeypatch.setenv("WAYFINDER_AURA_GAME_LABELS_URL", "https://x.test/labels")
    assert importlib.reload(gl).GAME_LABELS_URL == "https://x.test/labels"
    monkeypatch.delenv("WAYFINDER_AURA_GAME_LABELS_URL")
    importlib.reload(gl)


def test_a_corrupt_cache_is_ignored(labels, tmp_path):
    (tmp_path / "game-labels.json").write_text("{not json", encoding="utf-8")
    assert labels.intake_open(T) is False
    assert labels.labels_doc(T) is None


# --- validation ---------------------------------------------------------------------------

GOOD_GAME = game("world-of-warcraft", "macos", [line("1.2", "works", verified=True, works=2)])


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(v=2),
    lambda d: d.update(accepting="yes"),
    lambda d: d.update(generatedAt=True),
    lambda d: d.update(capped=None),
    lambda d: d.update(reportable="world-of-warcraft"),
    lambda d: d.update(reportable=["World Of Warcraft"]),
    lambda d: d.update(games={}),
    lambda d: d["games"][0].update(os="amiga"),
    lambda d: d["games"][0].update(key="Not A Key"),
    lambda d: d["games"][0].update(complete="true"),
    lambda d: d["games"][0]["lines"][0].update(level="great"),
    lambda d: d["games"][0]["lines"][0].update(line="1.2.3"),
    lambda d: d["games"][0]["lines"][0].update(sourceLine=None),
    lambda d: d["games"][0]["lines"][0].update(works=-1),
    lambda d: d["games"][0]["lines"][0].update(works=True),
    lambda d: d["games"][0]["lines"][0].pop("carried"),
    lambda d: d["games"][0].update(lines="1.2"),
])
def test_validation_rejects_unknown_versions_levels_and_shapes(mutate):
    import copy
    doc = labels_doc(games=[GOOD_GAME])
    assert gl.validate_labels(copy.deepcopy(doc)) is not None
    bad = copy.deepcopy(doc)
    mutate(bad)
    assert gl.validate_labels(bad) is None


def test_a_rejected_document_keeps_the_last_good_cache(labels, transport):
    fetch(labels, transport, labels_doc(games=[GOOD_GAME]))
    bad = labels_doc(games=[game("world-of-warcraft", "macos", [line("1.2", "great")])])
    assert fetch(labels, transport, bad, at=T + DAY + 1) is False
    assert labels.labels_doc(T + DAY + 2)["games"][0]["lines"][0]["level"] == "works"


def test_cached_labels_display_for_30_days(labels, transport):
    fetch(labels, transport, labels_doc(games=[GOOD_GAME]))
    entry = GameEntry("World of Warcraft", gc.UNTESTED, "note")
    assert labels.display_status(entry, "macos", "1.2", now=T + 29 * DAY)[0] == gc.VERIFIED
    assert labels.display_status(entry, "macos", "1.2", now=T + 31 * DAY) == (gc.UNTESTED, "")


# --- which label a row shows (section 3.4) -------------------------------------------------

WOW = GameEntry("World of Warcraft", gc.UNTESTED, "note")


def _doc(*lines_, os_="macos", complete=True, capped=False, key="world-of-warcraft"):
    return labels_doc(games=[game(key, os_, lines_, complete=complete)], capped=capped)


def test_an_app_older_than_every_published_line_falls_back_to_code():
    doc = _doc(line("1.3", "works", verified=True, works=1))
    assert gl.display_status(WOW, "macos", "1.2", doc=doc) == (gc.UNTESTED, "")


def test_between_lines_the_older_line_wins_and_a_newer_app_takes_the_newest():
    doc = _doc(line("1.2", "works", verified=True),
               line("1.4", "playable", some=1))
    assert gl.display_status(WOW, "macos", "1.3", doc=doc)[0] == gc.VERIFIED
    assert gl.display_status(WOW, "macos", "1.2", doc=doc)[0] == gc.VERIFIED
    assert gl.display_status(WOW, "macos", "1.4", doc=doc)[0] == gc.PLAYABLE
    assert gl.display_status(WOW, "macos", "1.10", doc=doc)[0] == gc.PLAYABLE  # 1.10 > 1.4


def test_labels_are_per_operating_system():
    doc = _doc(line("1.2", "works", verified=True), os_="windows")
    assert gl.display_status(WOW, "macos", "1.2", doc=doc) == (gc.UNTESTED, "")
    assert gl.display_status(WOW, "windows", "1.2", doc=doc)[0] == gc.VERIFIED


def test_merge_order_not_recommended_from_code_then_published_then_code():
    caution = GameEntry("RuneScape", gc.NOT_RECOMMENDED, "note")
    doc = labels_doc(games=[game("runescape", "macos", [line("1.2", "works", verified=True)]),
                            game("world-of-warcraft", "macos", [line("1.2", "untested", doesnt=1)])])
    assert gl.display_status(caution, "macos", "1.2", doc=doc) == (gc.NOT_RECOMMENDED, "")
    verified_in_code = GameEntry("World of Warcraft", gc.VERIFIED, "note")
    assert gl.display_status(verified_in_code, "macos", "1.2", doc=doc)[0] == gc.UNTESTED
    assert gl.display_status(verified_in_code, "macos", "1.2", doc=None) == (gc.VERIFIED, "")
    assert gl.display_status(verified_in_code, "macos", None, doc=doc) == (gc.VERIFIED, "")


@pytest.mark.parametrize("state,status,caption", [
    (line("1.2", "works", verified=True),
     gc.VERIFIED, "Tested by Wayfinder on Aura 1.2."),
    (line("1.3", "works", verified=True, source="1.1", carried=True),
     gc.VERIFIED, "Tested by Wayfinder on Aura 1.1."),
    (line("1.3", "works", works=4),
     gc.COMMUNITY, "4 anonymous reporters say it works on Aura 1.3."),
    (line("1.3", "works", source="1.2", carried=True, works=1),
     gc.COMMUNITY, "Works with (community), confirmed on Aura 1.2."),
    (line("1.3", "playable", some=1, works=1),
     gc.PLAYABLE, "Problems reported on Aura 1.3."),
    (line("1.3", "untested", doesnt=1),
     gc.UNTESTED, "Problems reported on Aura 1.3."),
    (line("1.3", "playable", works=1),
     gc.PLAYABLE, ""),
])
def test_captions_come_from_provenance(state, status, caption):
    assert gl.display_status(WOW, "macos", "1.3", doc=_doc(state)) == (status, caption)


def test_a_carried_source_no_longer_among_the_six_still_names_its_line():
    lines_ = [line(f"1.{n}", "works", source="1.1", carried=True, works=0) for n in range(4, 10)]
    doc = _doc(*lines_)
    assert gl.display_status(WOW, "macos", "1.9", doc=doc) == (
        gc.COMMUNITY, "Works with (community), confirmed on Aura 1.1.")
    dropped = [line("1.4", "playable", some=2)] + [
        line(f"1.{n}", "playable", source="1.4", carried=True) for n in range(5, 10)]
    assert gl.display_status(WOW, "macos", "1.9", doc=_doc(*dropped)) == (
        gc.PLAYABLE, "Problems reported on Aura 1.4.")


def test_capped_or_incomplete_adds_recent_versions_only():
    state = line("1.2", "works", verified=True)
    assert gl.display_status(WOW, "macos", "1.2", doc=_doc(state, complete=False))[1] == (
        "Tested by Wayfinder on Aura 1.2. Based on recent versions only.")
    assert gl.display_status(WOW, "macos", "1.2", doc=_doc(state, capped=True))[1].endswith(
        "Based on recent versions only.")
    plain = line("1.2", "playable", works=1)
    assert gl.display_status(WOW, "macos", "1.2", doc=_doc(plain, capped=True)) == (
        gc.PLAYABLE, "Based on recent versions only.")


@pytest.mark.parametrize("version,line_", [
    ("1.2.0", "1.2"), ("1.2.1-beta.4", "1.2"), ("1.10.3", "1.10"), ("v2.0.0", "2.0"),
    ("unknown", None), ("", None),
])
def test_app_line(version, line_):
    assert gl.app_line(version) == line_


def test_a_redirect_is_never_followed(labels, transport):
    transport.answer((307, b"", {"Location": "https://elsewhere.example/labels"}))
    assert labels.refresh(now=T) is False
    assert len(transport.gets()) == 1



# --- Sol r1 #5: a demotion keeps its caption after its line leaves the six -----------------

@pytest.mark.parametrize("kind", ["issues", "broken"])
def test_a_demotion_caption_survives_its_line_leaving_the_published_six(kind):
    level = "playable" if kind == "issues" else "untested"
    lines_ = [line(f"1.{n}", level, source="1.4", carried=True, demoted=("1.4", kind))
              for n in range(5, 11)]
    doc = _doc(*lines_)
    assert "1.4" not in {s["line"] for s in doc["games"][0]["lines"]}   # really gone
    assert gl.display_status(WOW, "macos", "1.10", doc=doc) == (
        gc.PLAYABLE if kind == "issues" else gc.UNTESTED, "Problems reported on Aura 1.4.")


def test_a_demotion_on_the_line_itself_names_that_line():
    state = line("1.4", "playable", some=1, works=2, demoted=("1.4", "issues"))
    assert gl.display_status(WOW, "macos", "1.4", doc=_doc(state)) == (
        gc.PLAYABLE, "Problems reported on Aura 1.4.")


def test_no_demotion_and_no_problems_means_no_caption():
    lines_ = [line(f"1.{n}", "playable", source="1.4", carried=True) for n in range(5, 11)]
    assert gl.display_status(WOW, "macos", "1.10", doc=_doc(*lines_)) == (gc.PLAYABLE, "")


@pytest.mark.parametrize("demoted_by,valid", [
    (None, True),
    ({"line": "1.4", "kind": "issues"}, True),
    ({"line": "1.4", "kind": "broken"}, True),
    ({"line": "1.4", "kind": "meh"}, False),
    ({"line": "1.4.0", "kind": "issues"}, False),
    ({"kind": "issues"}, False),
    ("1.4", False),
    (True, False),
])
def test_demoted_by_has_exactly_the_agreed_shape(demoted_by, valid):
    doc = _doc(line("1.5", "playable", source="1.4", carried=True))
    doc["games"][0]["lines"][0]["demotedBy"] = demoted_by
    assert (gl.validate_labels(doc) is not None) is valid


def test_a_document_without_demoted_by_still_reads():
    doc = _doc(line("1.2", "works", verified=True))
    doc["games"][0]["lines"][0].pop("demotedBy")
    assert gl.validate_labels(doc) is not None
