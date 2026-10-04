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

    def _script(self, path: Path, body: str) -> Path:
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    TOKEN = "0123456789abcdef0123456789abcdef"

    def _mac(self, tmp_path, opener, pid=None, stage=True, wait=3):
        installed = self._bundle(tmp_path / "Wayfinder Aura.app", "old")
        staged = tmp_path / ".Wayfinder Aura.app.update"
        if stage:
            self._bundle(staged, "new")
            (staged / "Contents" / "MacOS").mkdir()
            self._script(staged / "Contents" / "MacOS" / "aura", "sleep 30")
        args = ["/bin/sh", "-c", app_installer._mac_helper_script(), "t",
                str(pid or _exited_pid()), str(installed), str(staged),
                str(tmp_path / ".previous"), str(opener), str(tmp_path / "failed"),
                str(tmp_path / "launched"), str(wait), self.TOKEN]
        return installed, staged, args

    # A stand-in for `open --env AURA_UPDATE_TOKEN=... <app>`: starts the
    # app's binary and, like confirm_launch, writes "<token> <pid>".
    def _opener(self, tmp_path, confirm=True, token=None):
        body = f'echo "$*" >> "{tmp_path}/opened"\n'
        if confirm:
            body += ('[ "$1" = --env ] || exit 0\n'
                     '"$3/Contents/MacOS/aura" & echo $! >> "' + str(tmp_path) + '/pids"\n'
                     f'echo "{token or "${2#*=}"} $!" > "{tmp_path}/launched"\n')
        return self._script(tmp_path / "open.sh", body)

    def _reap(self, tmp_path):
        for pid in (tmp_path / "pids").read_text().split() if (tmp_path / "pids").exists() else []:
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass

    def test_mac_swap_keeps_the_new_copy_once_it_confirms(self, tmp_path):
        installed, staged, args = self._mac(tmp_path, self._opener(tmp_path))
        try:
            subprocess.run(args, check=True, timeout=30)
        finally:
            self._reap(tmp_path)
        assert (installed / "Contents" / "version").read_text() == "new"
        assert not staged.exists() and not (tmp_path / ".previous").exists()
        assert not (tmp_path / "failed").exists()
        assert (tmp_path / "opened").read_text().splitlines()[0] == (
            f"--env AURA_UPDATE_TOKEN={self.TOKEN} {installed}")

    def test_mac_new_copy_that_never_confirms_is_rolled_back(self, tmp_path):
        installed, staged, args = self._mac(tmp_path, self._opener(tmp_path, confirm=False), wait=2)
        subprocess.run(args, check=True, timeout=30)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert not staged.exists() and not (tmp_path / ".previous").exists()
        assert (tmp_path / "failed").exists()
        assert (tmp_path / "opened").read_text().splitlines()[1] == str(installed)

    def test_mac_a_confirmation_without_this_updates_token_is_ignored(self, tmp_path):
        # The re-check's case: the candidate hangs while some other Aura (or
        # an older marker) confirms. The backup must survive and come back.
        opener = self._opener(tmp_path, token="f" * 32)
        installed, staged, args = self._mac(tmp_path, opener, wait=2)
        try:
            subprocess.run(args, check=True, timeout=30)
        finally:
            self._reap(tmp_path)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists() and not (tmp_path / ".previous").exists()

    def test_mac_a_confirmation_from_a_process_outside_the_bundle_is_ignored(self, tmp_path):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        opener = self._script(tmp_path / "open.sh",
                              f'echo "{self.TOKEN} {other.pid}" > "{tmp_path}/launched"')
        installed, _, args = self._mac(tmp_path, opener, wait=2)
        try:
            subprocess.run(args, check=True, timeout=30)
        finally:
            other.kill()
            other.wait()
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists()

    def test_mac_a_confirmation_from_a_dead_process_is_ignored(self, tmp_path):
        opener = self._script(tmp_path / "open.sh",
                              f'echo "{self.TOKEN} {_exited_pid()}" > "{tmp_path}/launched"')
        installed, _, args = self._mac(tmp_path, opener, wait=2)
        subprocess.run(args, check=True, timeout=30)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists()

    def test_mac_failing_opener_is_rolled_back(self, tmp_path):
        installed, _, args = self._mac(tmp_path, "/usr/bin/false", wait=2)
        subprocess.run(args, timeout=30)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists()

    def test_mac_refused_swap_keeps_the_old_copy(self, tmp_path):
        installed, _, args = self._mac(tmp_path, "true", stage=False)
        subprocess.run(args, check=True, timeout=30)
        assert (installed / "Contents" / "version").read_text() == "old"
        assert (tmp_path / "failed").exists()

    def test_mac_swap_waits_for_aura_to_exit(self, tmp_path):
        aura = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        installed, _, args = self._mac(tmp_path, self._opener(tmp_path), pid=aura.pid, wait=10)
        helper = subprocess.Popen(args)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                helper.wait(timeout=1)
            assert (installed / "Contents" / "version").read_text() == "old"
        finally:
            aura.kill()
            aura.wait()
        try:
            helper.wait(timeout=20)
        finally:
            self._reap(tmp_path)
        assert (installed / "Contents" / "version").read_text() == "new"

    def test_an_aura_that_never_exits_is_left_alone(self, tmp_path):
        aura = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            installed, staged, args = self._mac(tmp_path, self._opener(tmp_path), pid=aura.pid, wait=1)
            assert subprocess.run(args, timeout=30).returncode == 1
        finally:
            aura.kill()
            aura.wait()
        assert (installed / "Contents" / "version").read_text() == "old"
        assert not staged.exists() and (tmp_path / "failed").exists()
        assert not (tmp_path / "opened").exists()

    def _appimage(self, tmp_path, staged_body, wait=3):
        target = self._script(tmp_path / "Wayfinder_Aura-1.1.8-x86_64.AppImage",
                              f'echo old >> "{tmp_path}/ran"')
        staged = self._script(tmp_path / ".update", staged_body)
        args = ["/bin/sh", "-c", app_installer._appimage_helper_script(), "t",
                str(_exited_pid()), str(target), str(staged), str(tmp_path / "failed"),
                str(tmp_path / "launched"), str(tmp_path / ".previous"), str(wait),
                self.TOKEN]
        return target, staged, args

    def test_appimage_swap_keeps_the_path_once_the_new_one_confirms(self, tmp_path):
        target, staged, args = self._appimage(
            tmp_path, f'echo new >> "{tmp_path}/ran"; '
                      f'echo "$AURA_UPDATE_TOKEN $$" > "{tmp_path}/launched"; sleep 3')
        subprocess.run(args, check=True, timeout=30)
        assert (tmp_path / "ran").read_text().split() == ["new"]
        assert not staged.exists() and not (tmp_path / ".previous").exists()
        assert "launched" in target.read_text() and os.access(target, os.X_OK)

    def test_appimage_confirmed_by_another_process_is_rolled_back(self, tmp_path):
        # The re-check's Linux probe: the new AppImage hangs and an unrelated
        # Aura writes the marker. Without this update's token it is ignored.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            target, _, args = self._appimage(
                tmp_path, f'echo new >> "{tmp_path}/ran"; '
                          f'echo "{"f" * 32} {other.pid}" > "{tmp_path}/launched"; sleep 30', wait=2)
            subprocess.run(args, check=True, timeout=30)
        finally:
            other.kill()
            other.wait()
        assert 'echo old' in target.read_text()
        assert (tmp_path / "failed").exists() and not (tmp_path / ".previous").exists()

    def test_appimage_that_cannot_run_is_rolled_back(self, tmp_path):
        # The review's case: accepted magic, but the file exits 126.
        target, _, args = self._appimage(tmp_path, "exit 126")
        subprocess.run(args, check=True, timeout=30)
        assert (tmp_path / "ran").read_text().split() == ["old"]
        assert 'echo old' in target.read_text()
        assert (tmp_path / "failed").exists() and not (tmp_path / ".previous").exists()


class TestLeftovers:
    def _leftovers(self, monkeypatch, tmp_path):
        bundle = tmp_path / "Wayfinder Aura.app"
        (bundle / "Contents").mkdir(parents=True)
        for name in (".Wayfinder Aura.app.update", ".Wayfinder Aura.app.previous"):
            (tmp_path / name / "Contents").mkdir(parents=True)
        monkeypatch.setattr(app_installer, "mac_bundle_path", lambda: bundle)
        monkeypatch.setattr(app_installer, "_failure_marker", lambda: tmp_path / "no-marker")

    def test_stale_staging_and_backups_are_removed_at_startup(self, monkeypatch, tmp_path):
        self._leftovers(monkeypatch, tmp_path)
        monkeypatch.setattr(app_installer, "STALE_AFTER_S", -1)
        app_installer.cleanup_stale_staging()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Wayfinder Aura.app"]

    def test_a_fresh_backup_a_helper_may_need_is_kept(self, monkeypatch, tmp_path):
        self._leftovers(monkeypatch, tmp_path)
        app_installer.cleanup_stale_staging()
        assert (tmp_path / ".Wayfinder Aura.app.previous").exists()

    def test_the_new_version_confirms_its_launch_before_cleanup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_installer, "_launch_marker", lambda: tmp_path / "launched")
        monkeypatch.setattr(app_installer, "_update_token", "")
        monkeypatch.setenv(app_installer.TOKEN_ENV, "ab" * 16)
        app_installer.confirm_launch()
        assert (tmp_path / "launched").read_text() == f"{'ab' * 16} {os.getpid()}\n"
        assert app_installer.TOKEN_ENV not in os.environ  # children never inherit it
        src = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")
        body = src.split("def startup_pass() -> None:", 1)[1].split("\n\n", 1)[0]
        assert body.index("confirm_launch()") < body.index("cleanup_stale_staging()")

    def test_the_token_leaves_the_environment_at_startup(self, monkeypatch, tmp_path):
        # Taken before any child process starts, so none inherits it; the
        # confirmation later still has it.
        monkeypatch.setattr(app_installer, "_launch_marker", lambda: tmp_path / "launched")
        monkeypatch.setattr(app_installer, "_update_token", "")
        monkeypatch.setenv(app_installer.TOKEN_ENV, "cd" * 16)
        app_installer.take_update_token()
        assert app_installer.TOKEN_ENV not in os.environ
        app_installer.confirm_launch()
        assert (tmp_path / "launched").read_text().split()[0] == "cd" * 16
        assert app_installer._update_token == ""
        src = (REPO / "main.py").read_text(encoding="utf-8")
        body = src.split("def main():", 1)[1]
        assert body.index("take_update_token()") < body.index("_dispatch_cli_control_verb()")

    def test_a_launch_the_helper_did_not_start_confirms_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_installer, "_launch_marker", lambda: tmp_path / "launched")
        monkeypatch.setattr(app_installer, "_update_token", "")
        monkeypatch.delenv(app_installer.TOKEN_ENV, raising=False)
        app_installer.confirm_launch()
        monkeypatch.setenv(app_installer.TOKEN_ENV, "not-a-token")
        app_installer.confirm_launch()
        assert not (tmp_path / "launched").exists()

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
        # The review's case: another recording starts before the deferred
        # IDLE callback runs. The staged update must still be there afterwards.
        wm.WayfinderApp._finish_pending_app_update(app)
        assert app._pending_app_update is outcome
        app.app_state = wm.AppState.IDLE
        finished = []
        outcome.finish = lambda: finished.append(True)
        wm.WayfinderApp._finish_pending_app_update(app)
        assert finished == [True] and ("quit",) in calls
        assert app._pending_app_update is None

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
