"""In-place app updates (core/app_installer.py) and the Install Update button.

The installer replaces the app the user runs, so the rules that keep that safe
are pinned here: only this repo's release downloads, only an app signed by the
same team and accepted by Gatekeeper, the swap only after Aura has exited, and
the old copy restored if the swap fails.
"""

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.core import app_installer
from wayfinder.core.app_installer import Outcome

REPO = Path(__file__).resolve().parent.parent
URL = app_installer.DOWNLOAD_PREFIX + "v1.2.0-beta.2/Wayfinder_Aura-1.2.0-beta.2-macOS-arm64.dmg"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX helper scripts")


def _response(chunks, length=None):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.raise_for_status.return_value = None
    resp.headers = {"Content-Length": str(length if length is not None else sum(map(len, chunks)))}
    resp.iter_content.return_value = chunks
    return resp


class TestDownload:
    def test_only_this_repos_release_assets(self, tmp_path):
        for bad in ("https://evil.example/x.dmg",
                    "https://github.com/someone/else/releases/download/v1/x.dmg",
                    app_installer.DOWNLOAD_PREFIX + "v1/../../x;rm.dmg"):
            with pytest.raises(ValueError):
                app_installer.download(bad, tmp_path)

    def test_streams_with_progress_and_lands_atomically(self, tmp_path):
        seen = []
        with patch("requests.get", return_value=_response([b"ab", b"cd"])):
            path = app_installer.download(URL, tmp_path, seen.append)
        assert path.read_bytes() == b"abcd"
        assert seen == [0.5, 1.0]
        assert not list(tmp_path.glob("*.part"))

    def test_a_short_download_is_refused(self, tmp_path):
        with patch("requests.get", return_value=_response([b"ab"], length=10)):
            with pytest.raises(IOError):
                app_installer.download(URL, tmp_path)
        assert not (tmp_path / URL.rsplit("/", 1)[1]).exists()

    def test_an_oversized_download_is_refused(self, tmp_path):
        with patch("requests.get", return_value=_response([], length=app_installer.MAX_DOWNLOAD_BYTES + 1)):
            with pytest.raises(ValueError):
                app_installer.download(URL, tmp_path)


class TestInstallMode:
    def _mac(self, monkeypatch, bundle: Path):
        monkeypatch.setattr(app_installer.sys, "platform", "darwin")
        monkeypatch.setattr(app_installer, "_frozen", lambda: True)
        monkeypatch.setattr(app_installer.sys, "executable",
                            str(bundle / "Contents" / "MacOS" / "Wayfinder Aura"))

    def test_mac_app_in_a_writable_folder(self, monkeypatch, tmp_path):
        bundle = tmp_path / "Wayfinder Aura.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        self._mac(monkeypatch, bundle)
        assert app_installer.install_mode() == "mac"

    def test_translocated_mac_app_falls_back(self, monkeypatch, tmp_path):
        bundle = tmp_path / "AppTranslocation" / "X" / "d" / "Wayfinder Aura.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        self._mac(monkeypatch, bundle)
        assert app_installer.install_mode() is None

    def test_unwritable_mac_folder_falls_back(self, monkeypatch, tmp_path):
        bundle = tmp_path / "Wayfinder Aura.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        self._mac(monkeypatch, bundle)
        monkeypatch.setattr(app_installer.os, "access", lambda path, mode: False)
        assert app_installer.install_mode() is None

    def test_source_runs_never_self_update(self, monkeypatch):
        monkeypatch.setattr(app_installer, "_frozen", lambda: False)
        for name in ("darwin", "win32"):
            monkeypatch.setattr(app_installer.sys, "platform", name)
            assert app_installer.install_mode() is None

    def test_windows_installer(self, monkeypatch):
        monkeypatch.setattr(app_installer.sys, "platform", "win32")
        monkeypatch.setattr(app_installer, "_frozen", lambda: True)
        assert app_installer.install_mode() == "windows"

    def test_appimage_but_not_flatpak(self, monkeypatch, tmp_path):
        image = tmp_path / "Wayfinder_Aura-1.2.0-x86_64.AppImage"
        image.write_bytes(b"x")
        monkeypatch.setattr(app_installer.sys, "platform", "linux")
        monkeypatch.setattr(app_installer, "appimage_path", lambda: image)
        assert app_installer.install_mode() == "appimage"
        monkeypatch.setattr(app_installer, "appimage_path", lambda: None)
        assert app_installer.install_mode() is None

    def test_prepare_update_without_a_mode_is_a_quiet_fallback(self, monkeypatch):
        monkeypatch.setattr(app_installer, "install_mode", lambda: None)
        outcome = app_installer.prepare_update(URL)
        assert not outcome.ready and outcome.message == "" and outcome.finish is None

    def test_prepare_update_never_raises(self, monkeypatch):
        monkeypatch.setattr(app_installer, "install_mode", lambda: "mac")
        monkeypatch.setattr(app_installer, "_prepare_mac",
                            lambda url, progress: (_ for _ in ()).throw(OSError("disk full")))
        outcome = app_installer.prepare_update(URL)
        assert not outcome.ready and "disk full" in outcome.message


class TestMacVerification:
    @staticmethod
    def _fake_run(identities, strict_ok=True, gatekeeper_ok=True):
        def run(args, timeout=120):
            if args[1] == "-dv":
                ident, team = identities[args[-1]]
                return types.SimpleNamespace(
                    returncode=0, stdout="",
                    stderr=f"Identifier={ident}\nTeamIdentifier={team}\n")
            if args[1] == "--verify":
                return types.SimpleNamespace(returncode=0 if strict_ok else 1, stdout="", stderr="")
            if args[0].endswith("spctl"):
                return types.SimpleNamespace(returncode=0 if gatekeeper_ok else 3, stdout="", stderr="")
            raise AssertionError(args)
        return run

    ID, TEAM = "io.wayfindercollective.WayfinderAura", "5JJQ8L5HHD"

    NEW, OLD = str(Path("/new")), str(Path("/old"))  # "\\new" on Windows

    def test_same_team_notarized_update_passes(self, monkeypatch):
        monkeypatch.setattr(app_installer, "_run", self._fake_run(
            {self.NEW: (self.ID, self.TEAM), self.OLD: (self.ID, self.TEAM)}))
        assert app_installer.verify_mac_app(Path("/new"), Path("/old")) is None

    @pytest.mark.parametrize("new,old,strict,gk,reason", [
        ((ID, "OTHERTEAM1"), (ID, TEAM), True, True, "someone else"),
        (("com.evil.app", TEAM), (ID, TEAM), True, True, "someone else"),
        ((ID, TEAM), (ID, TEAM), False, True, "signature"),
        ((ID, TEAM), (ID, TEAM), True, False, "notarized"),
        ((ID, TEAM), (ID, "not set"), True, True, "not Developer ID signed"),
    ])
    def test_anything_else_is_refused(self, monkeypatch, new, old, strict, gk, reason):
        monkeypatch.setattr(app_installer, "_run", self._fake_run(
            {self.NEW: new, self.OLD: old}, strict_ok=strict, gatekeeper_ok=gk))
        assert reason in app_installer.verify_mac_app(Path("/new"), Path("/old"))


def _exited_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@posix_only
class TestSwapHelpers:
    def _bundle(self, path: Path, marker: str) -> Path:
        (path / "Contents").mkdir(parents=True)
        (path / "Contents" / "version").write_text(marker)
        return path

    def test_mac_swap_replaces_and_opens_the_new_copy(self, tmp_path):
        installed = self._bundle(tmp_path / "Wayfinder Aura.app", "old")
        staged = self._bundle(tmp_path / ".Wayfinder Aura.app.update", "new")
        opened = tmp_path / "opened"
        opener = tmp_path / "open.sh"
        opener.write_text(f'#!/bin/sh\necho "$1" > "{opened}"\n')
        opener.chmod(0o755)
        subprocess.run(["/bin/sh", "-c", app_installer._mac_helper_script(), "t",
                        str(_exited_pid()), str(installed), str(staged),
                        str(tmp_path / ".previous"), str(opener), str(tmp_path / "failed")],
                       check=True, timeout=20)
        assert (installed / "Contents" / "version").read_text() == "new"
        assert not staged.exists() and not (tmp_path / ".previous").exists()
        assert opened.read_text().strip() == str(installed)

    def test_mac_swap_failure_keeps_the_old_copy(self, tmp_path):
        installed = self._bundle(tmp_path / "Wayfinder Aura.app", "old")
        missing_stage = tmp_path / ".Wayfinder Aura.app.update"  # never staged
        subprocess.run(["/bin/sh", "-c", app_installer._mac_helper_script(), "t",
                        str(_exited_pid()), str(installed), str(missing_stage),
                        str(tmp_path / ".previous"), "true", str(tmp_path / "failed")],
                       check=True, timeout=20)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists()  # next launch hands over the download

    def test_mac_swap_waits_for_aura_to_exit(self, tmp_path):
        installed = self._bundle(tmp_path / "Wayfinder Aura.app", "old")
        staged = self._bundle(tmp_path / ".Wayfinder Aura.app.update", "new")
        aura = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        helper = subprocess.Popen(["/bin/sh", "-c", app_installer._mac_helper_script(), "t",
                                   str(aura.pid), str(installed), str(staged),
                                   str(tmp_path / ".previous"), "true", str(tmp_path / "failed")])
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                helper.wait(timeout=1)
            assert (installed / "Contents" / "version").read_text() == "old"
        finally:
            aura.kill()
            aura.wait()
        helper.wait(timeout=10)
        assert (installed / "Contents" / "version").read_text() == "new"

    def test_appimage_swap_keeps_the_path_and_runs_it(self, tmp_path):
        target = tmp_path / "Wayfinder_Aura-1.1.8-x86_64.AppImage"
        target.write_text("old")
        ran = tmp_path / "ran"
        staged = tmp_path / ".update"
        staged.write_text(f'#!/bin/sh\necho new > "{ran}"\n')
        staged.chmod(0o755)
        subprocess.run(["/bin/sh", "-c", app_installer._appimage_helper_script(), "t",
                        str(_exited_pid()), str(target), str(staged), str(tmp_path / "failed")],
                       check=True, timeout=20)
        assert ran.read_text().strip() == "new"
        assert not staged.exists() and os.access(target, os.X_OK)


class TestLeftovers:
    def test_unused_staging_and_backups_are_removed_at_startup(self, monkeypatch, tmp_path):
        bundle = tmp_path / "Wayfinder Aura.app"
        (bundle / "Contents").mkdir(parents=True)
        for name in (".Wayfinder Aura.app.update", ".Wayfinder Aura.app.previous"):
            (tmp_path / name / "Contents").mkdir(parents=True)
        monkeypatch.setattr(app_installer, "mac_bundle_path", lambda: bundle)
        app_installer.cleanup_stale_staging()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Wayfinder Aura.app"]

    def test_a_failed_swap_hands_over_the_download_for_one_launch(self, monkeypatch, tmp_path):
        marker = tmp_path / "update-install-failed"
        marker.write_text("")
        monkeypatch.setattr(app_installer, "_failure_marker", lambda: marker)
        monkeypatch.setattr(app_installer, "mac_bundle_path", lambda: None)
        monkeypatch.setattr(app_installer, "_in_place_failed", False)
        assert app_installer.cleanup_stale_staging() is True
        assert not marker.exists()
        monkeypatch.setattr(app_installer.sys, "platform", "win32")
        monkeypatch.setattr(app_installer, "_frozen", lambda: True)
        assert app_installer.install_mode() is None


class TestWindows:
    def test_silent_install_relaunches(self):
        assert "/VERYSILENT" in app_installer.SILENT_INSTALL_ARGS
        assert "/relaunch=1" in app_installer.SILENT_INSTALL_ARGS
        iss = (REPO / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
        assert "Check: RelaunchAfterSilentUpdate" in iss
        assert "ExpandConstant('{param:relaunch|0}') = '1'" in iss
        assert "PrivilegesRequired=lowest" in iss  # no UAC prompt for the update


class TestInstallButton:
    @pytest.fixture
    def wm(self):
        import wayfinder_main
        return wayfinder_main

    def test_install_update_replaces_get_update_when_possible(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "_update_installs_in_place", lambda: True)
        started = []
        app = types.SimpleNamespace(
            _app_update_info={"download_url": URL},
            _install_app_update=started.append,
            _open_url=lambda url: pytest.fail("opened the browser"))
        wm.WayfinderApp._open_app_update_page(app)
        assert started == [{"download_url": URL}]

    def test_without_in_place_install_it_still_downloads(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "_update_installs_in_place", lambda: False)
        monkeypatch.setattr(wm, "IS_MACOS", True)
        opened = []
        app = types.SimpleNamespace(_app_update_info={"download_url": URL},
                                    _open_url=opened.append)
        wm.WayfinderApp._open_app_update_page(app)
        assert opened == [URL]

    def _prepared(self, wm, state):
        calls = []
        app = types.SimpleNamespace(
            app_state=state, _app_update_installing=True,
            log=lambda m: calls.append(("log", m)),
            quit_app=lambda: calls.append(("quit",)),
            _show_app_update_status=lambda t: calls.append(("status", t)),
            _open_url=lambda u: calls.append(("open", u)))
        app._finish_app_update = lambda outcome: wm.WayfinderApp._finish_app_update(app, outcome)
        return app, calls

    def test_ready_update_restarts_when_idle(self, wm):
        app, calls = self._prepared(wm, wm.AppState.IDLE)
        finished = []
        wm.WayfinderApp._on_app_update_prepared(
            app, {}, Outcome(ready=True, finish=lambda: finished.append(True)))
        assert finished == [True] and ("quit",) in calls

    def test_ready_update_never_interrupts_a_dictation(self, wm):
        app, calls = self._prepared(wm, wm.AppState.RECORDING)
        outcome = Outcome(ready=True, finish=lambda: pytest.fail("finished mid-dictation"))
        wm.WayfinderApp._on_app_update_prepared(app, {}, outcome)
        assert app._pending_app_update is outcome and ("quit",) not in calls
        app.app_state = wm.AppState.IDLE
        finished = []
        outcome.finish = lambda: finished.append(True)
        wm.WayfinderApp._finish_pending_app_update(app)
        assert finished == [True] and ("quit",) in calls

    def test_failed_install_hands_over_the_download(self, wm, tmp_path):
        app, calls = self._prepared(wm, wm.AppState.IDLE)
        dmg = tmp_path / "x.dmg"
        wm.WayfinderApp._on_app_update_prepared(
            app, {"download_url": URL},
            Outcome(message="Not installed: nope.", fallback_path=str(dmg)))
        assert ("open", dmg.as_uri()) in calls
        assert ("quit",) not in calls

    def test_idle_transition_finishes_a_pending_update(self, wm):
        src = Path(wm.__file__).read_text(encoding="utf-8")
        body = src.split("    def update_state(self, new_state: AppState):", 1)[1]
        body = body.split("\n    def ", 1)[0]
        assert "self.after(0, self._finish_pending_app_update)" in body
