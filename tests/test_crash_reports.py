"""Crash and error reports (core/crash_reports.py).

Pinned: consent (Beta on, Stable opt-in, explicit choice wins), what a report
may contain (scrubbed, matching the backend contract), dedupe and caps, native
crash pickup on the next launch (a real segfault in a child process, on every
OS), macOS crash-log summaries, and the send/keep/drop rules.
"""

import json
import re
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.core import crash_reports as cr

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setitem(cr._state, "dir", tmp_path / "reports")
    monkeypatch.setitem(cr._state, "app_version", "1.2.0-beta.3")
    monkeypatch.setitem(cr._state, "native_file", None)
    monkeypatch.setitem(cr._state, "config_getter", lambda: {})
    monkeypatch.setattr(cr, "_send_soon", lambda: None)  # never touch the network


def _queued():
    queue = cr.reports_dir() / "pending"
    return [json.loads(p.read_text()) for p in sorted(queue.glob("*.json"))] if queue.exists() else []


def _raise(exc):
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - the point of the helper
        return type(caught), caught, caught.__traceback__


# The backend's contract (Wayfinder-OS /api/aura/crash): anything else is a 400.
def assert_matches_contract(report):
    hex_ = lambda n: re.compile(rf"^[0-9a-f]{{{n}}}$")  # noqa: E731
    assert hex_(32).match(report["reportId"]) and hex_(32).match(report["installId"])
    assert hex_(16).match(report["signature"])
    assert report["kind"] in ("crash", "error")
    assert report["channel"] in ("stable", "beta")
    limits = {"title": 200, "where": 60, "appVersion": 40, "os": 40, "osVersion": 80,
              "arch": 20, "package": 20, "python": 20, "trace": 16000}
    for key, limit in limits.items():
        assert isinstance(report[key], str) and len(report[key]) <= limit, key
    assert isinstance(report["uptimeSeconds"], int) and report["uptimeSeconds"] >= 0
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", report["occurredAt"])
    assert set(report) == {"reportId", "installId", "kind", "signature", "title", "where",
                           "appVersion", "channel", "os", "osVersion", "arch", "package",
                           "python", "uptimeSeconds", "occurredAt", "trace"}


class TestConsent:
    @pytest.mark.parametrize("version,config,expected", [
        ("1.2.0-beta.3", {}, True),                       # beta build: on
        ("1.2.0", {}, False),                             # stable build: off
        ("1.2.0", {"update_channel": "beta"}, True),      # stable install on Beta: on
        ("1.2.0-beta.3", {"update_channel": "stable"}, False),
        ("1.2.0-beta.3", {"crash_reports": "off"}, False),  # explicit choice wins
        ("1.2.0", {"crash_reports": "on"}, True),
        ("dev", {}, False),                               # source checkout: never
    ])
    def test_beta_on_stable_opt_in(self, version, config, expected):
        assert cr.reports_enabled(config, version) is expected

    def test_disabled_records_nothing(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "off"})
        assert cr.record_exception(*_raise(ValueError("x")), where="main") is False
        assert _queued() == []


class TestScrubbing:
    def test_home_user_names_emails_and_dictated_text_are_removed(self):
        home = str(Path.home())
        text = (f'File "{home}/app/x.py", line 3\n'
                'File "/Users/someone/y.py" File "/home/pat/z.py" '
                r'File "C:\Users\Pat\w.py" '
                "mail me@example.com "
                "msg 'this is a long sentence the user actually dictated into the app' "
                "ok 'short'")
        out = cr.scrub(text)
        assert home not in out and "~/app/x.py" in out
        assert "someone" not in out and "/home/pat" not in out and "Pat" not in out
        assert "me@example.com" not in out and "<email>" in out
        assert "actually dictated" not in out
        assert "'short'" in out

    def test_scrubbing_stays_fast_on_huge_word_runs(self):
        import time
        start = time.perf_counter()
        out = cr.scrub("x" * 200_000 + " a.b@example.com " + "y" * 200_000)
        assert time.perf_counter() - start < 0.5  # it runs on the Tk thread
        assert "a.b@example.com" not in out

    def test_long_exception_messages_are_capped(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        cr.record_exception(*_raise(RuntimeError("x" * 5000)), where="main")
        (report,) = _queued()
        assert "x" * (cr.MAX_MESSAGE_CHARS + 1) not in report["trace"]


class TestRecording:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})

    def test_report_matches_the_backend_contract(self):
        assert cr.record_exception(*_raise(KeyError("k")), where="tk-callback")
        (report,) = _queued()
        assert_matches_contract(report)
        assert report["kind"] == "error" and report["where"] == "tk-callback"
        assert report["title"].startswith("KeyError in test_crash_reports.py:")
        assert report["appVersion"] == "1.2.0-beta.3" and report["channel"] == "beta"

    @pytest.mark.parametrize("char", ["a", "é", "語", "🎙"])
    def test_the_sent_body_always_fits_the_endpoints_64_kb(self, char):
        cr.record_exception(*_raise(RuntimeError("x")), where="main")
        (report,) = _queued()
        report["trace"] = char * 40000
        fitted = cr.build_report("error", "main", "t", char * 40000, ["a.py:f"], "E", {})
        assert len(json.dumps(fitted).encode()) <= cr.MAX_BODY_BYTES < 64 * 1024
        assert len(fitted["trace"]) <= 16000
        assert_matches_contract(fitted)

    def test_same_bug_once_a_day_and_a_daily_cap(self, monkeypatch):
        for _ in range(3):
            cr.record_exception(*_raise(ValueError("same")), where="main")
        assert len(_queued()) == 1
        monkeypatch.setattr(cr, "DAILY_CAP", 2)
        cr.record_exception(*_raise(TypeError("a")), where="main")
        cr.record_exception(*_raise(OSError("b")), where="main")
        assert len(_queued()) == 2

    def test_quitting_is_not_an_error(self):
        assert cr.record_exception(*_raise(KeyboardInterrupt()), where="main") is False
        assert cr.record_exception(*_raise(SystemExit(0)), where="main") is False

    def test_install_id_is_random_and_stable(self):
        first = cr.install_id()
        assert re.fullmatch(r"[0-9a-f]{32}", first) and cr.install_id() == first


class TestHooks:
    def test_install_chains_the_python_hooks(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        seen = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append("previous"))
        monkeypatch.setattr(threading, "excepthook", lambda args: seen.append("previous-thread"))
        monkeypatch.setattr(cr.faulthandler, "enable", lambda **kw: None)
        cr.install("1.2.0-beta.3", lambda: {"crash_reports": "on"})
        sys.excepthook(*_raise(ValueError("boom")))
        worker = threading.Thread(target=lambda: 1 / 0, name="transcribe")
        worker.start()
        worker.join()
        assert seen == ["previous", "previous-thread"]
        kinds = sorted((r["kind"], r["where"]) for r in _queued())
        assert kinds == [("crash", "main"), ("error", "thread:transcribe")]
        cr.mark_clean_exit()

    def test_clean_quit_leaves_no_native_file(self, monkeypatch):
        monkeypatch.setattr(cr.faulthandler, "enable", lambda **kw: None)
        monkeypatch.setattr(sys, "excepthook", sys.excepthook)
        monkeypatch.setattr(threading, "excepthook", threading.excepthook)
        cr.install("1.2.0-beta.3")
        native = cr._state["native_file"]
        native.write_text("Windows fatal exception: code 0xe06d7363 (survived)")
        cr.mark_clean_exit()
        assert not native.exists()


CRASHER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from wayfinder.core import crash_reports as cr
    cr._state["dir"] = __import__("pathlib").Path(sys.argv[2])
    cr.install("1.2.0-beta.3", lambda: {"crash_reports": "on"})
    if sys.platform == "win32":  # no Windows Error Reporting dialog in CI
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0002)
    def native_part():
        import faulthandler
        faulthandler._sigsegv()
    native_part()
""")


class TestNativeCrash:
    def test_a_real_segfault_is_reported_on_the_next_launch(self, tmp_path, monkeypatch):
        reports = cr.reports_dir()
        result = subprocess.run([sys.executable, "-c", CRASHER, str(SRC), str(reports)],
                                capture_output=True, timeout=60)
        assert result.returncode != 0  # it really crashed
        crash_files = list(reports.glob("native-*.log"))
        assert len(crash_files) == 1 and crash_files[0].stat().st_size > 0

        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        assert cr.collect_native_crashes() == 1
        (report,) = _queued()
        assert_matches_contract(report)
        assert report["kind"] == "crash" and report["where"] == "native"
        assert "native_part" in report["trace"]
        assert not list(reports.glob("native-*.log"))

    def test_empty_files_from_earlier_launches_are_just_removed(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        cr.reports_dir().mkdir(parents=True)
        (cr.reports_dir() / "native-111.log").write_text("")
        assert cr.collect_native_crashes() == 0
        assert not list(cr.reports_dir().glob("native-*.log")) and _queued() == []


IPS_HEADER = {"app_name": "Wayfinder Aura", "app_version": "1.2.0-beta.3", "bug_type": "309"}
IPS_BODY = {
    "osVersion": {"train": "macOS 27.0"},
    "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
    "termination": {"indicator": "Abort trap: 6"},
    "faultingThread": 1,
    "usedImages": [{"name": "libsystem_kernel.dylib"}, {"name": "Python"}],
    "threads": [{"frames": []}, {"frames": [
        {"imageIndex": 0, "symbol": "__pthread_kill"},
        {"imageIndex": 1, "symbol": "PyEval_RestoreThread"}]}],
    "procPath": "/Users/someone/Applications/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
}


class TestMacCrashLogs:
    def _write(self, folder: Path, name: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(IPS_HEADER) + "\n" + json.dumps(IPS_BODY))

    def test_summary_names_the_exception_and_frames(self, tmp_path):
        self._write(tmp_path, "Wayfinder Aura-2026-10-03-120000.ips")
        text = cr.summarize_ips(tmp_path / "Wayfinder Aura-2026-10-03-120000.ips")
        assert "EXC_CRASH (SIGABRT)" in text and "PyEval_RestoreThread" in text
        assert "someone" not in text

    def test_old_logs_are_history_new_ones_are_reported(self, tmp_path, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        logs = tmp_path / "DiagnosticReports"
        self._write(logs, "Wayfinder Aura-2026-09-01-000000.ips")
        assert cr.collect_macos_crash_logs(logs) == 0  # first run: already there
        self._write(logs, "Wayfinder Aura-2026-10-03-120000.ips")
        assert cr.collect_macos_crash_logs(logs) == 1
        assert cr.collect_macos_crash_logs(logs) == 0  # not twice
        (report,) = _queued()
        assert_matches_contract(report)
        assert report["where"] == "macos-crash-log"


class TestSending:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})

    def _queue(self, n=1):
        for i in range(n):
            cr.record_exception(*_raise(ValueError(f"bug {i}")) if i == 0 else
                                _raise(type(f"E{i}", (Exception,), {})("x")), where="main")

    @pytest.mark.parametrize("status,kept,sent", [
        (200, 0, 1), (400, 0, 0), (413, 0, 0), (404, 1, 0), (429, 1, 0), (503, 1, 0)])
    def test_status_codes(self, status, kept, sent):
        self._queue()
        with patch("requests.post", return_value=MagicMock(status_code=status)) as post:
            assert cr.send_pending() == sent
        assert post.call_args.args[0] == cr.CRASH_API_URL
        assert len(_queued()) == kept

    def test_offline_keeps_everything(self):
        self._queue(2)
        with patch("requests.post", side_effect=OSError("offline")):
            assert cr.send_pending() == 0
        assert len(_queued()) == 2

    def test_turning_reports_off_deletes_the_queue(self, monkeypatch):
        self._queue()
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "off"})
        with patch("requests.post", side_effect=AssertionError("sent while off")):
            cr.send_pending()
        assert _queued() == []


class TestAppWiring:
    @pytest.fixture
    def src(self):
        return (Path(__file__).resolve().parent.parent / "wayfinder_main.py").read_text(encoding="utf-8")

    def test_tk_callback_errors_are_reported(self, src):
        body = src.split("    def report_callback_exception(self, exc, val, tb):", 1)[1]
        body = body.split("\n    def ", 1)[0]
        assert "super().report_callback_exception(exc, val, tb)" in body
        assert 'record_exception(exc, val, tb, where="tk-callback")' in body

    def test_quit_marks_a_clean_exit_first(self, src):
        body = src.split("    def quit_app(self, icon=None, item=None):", 1)[1].split("\n    def ", 1)[0]
        assert body.index("crash_reports.mark_clean_exit()") < body.index("self.stop_event.set()")

    def test_main_installs_the_reporter_in_the_gui_process(self):
        main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        body = main.split("def main():", 1)[1]
        assert body.index("_signal_existing_instance()") < body.index("crash_reports.install(")

    def test_settings_switch_and_default(self, src):
        from wayfinder.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["crash_reports"] == ""
        assert '"Send crash reports"' in src

    def test_toggle_saves_the_choice_and_off_clears_the_queue(self, monkeypatch):
        import wayfinder_main as wm
        monkeypatch.setitem(cr._state, "config_getter", lambda: {"crash_reports": "on"})
        cr.record_exception(*_raise(ValueError("x")), where="main")
        saved = []
        monkeypatch.setattr(wm, "save_config", lambda cfg: saved.append(dict(cfg)))
        app = types.SimpleNamespace(
            config={}, log=lambda m: None,
            _crash_reports_var=types.SimpleNamespace(get=lambda: False))
        wm.WayfinderApp._on_crash_reports_toggled(app)
        assert app.config["crash_reports"] == "off" and saved
        assert _queued() == []
