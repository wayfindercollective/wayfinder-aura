"""Flatpak release-manifest helper tests."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "flatpak" / "prepare-release-manifest.py"
MANIFEST = REPO / "flatpak" / "io.wayfindercollective.WayfinderAura.yml"


def _project_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _release_tag() -> str:
    return f"v{_project_version()}"


def _load_script():
    spec = importlib.util.spec_from_file_location("prepare_release_manifest", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_release_manifest_replaces_local_app_source():
    helper = _load_script()
    commit = "a" * 40
    tag = _release_tag()

    rendered = helper.render_release_manifest(MANIFEST.read_text(encoding="utf-8"), tag, commit)

    assert "      - type: dir\n        path: .." not in rendered
    assert f"url: {helper.REPO_URL}" in rendered
    assert f"tag: {tag}" in rendered
    assert f"commit: {commit}" in rendered
    assert "- python-deps.json" in rendered


def test_cli_writes_release_manifest_when_dirty_is_explicitly_allowed(
    monkeypatch, tmp_path, capsys
):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    commit = "b" * 40
    tag = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: True)
    monkeypatch.setattr(helper, "_dev_license_defaults", lambda _repo: [])

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", commit,
            "--manifest", str(manifest),
            "--output", str(output),
            "--allow-dirty",
        ]
    )

    assert rc == 0
    rendered = output.read_text(encoding="utf-8")
    copied_deps = output.parent / "python-deps.json"
    assert "      - type: dir\n        path: .." not in rendered
    assert f"url: {helper.REPO_URL}" in rendered
    assert f"tag: {tag}" in rendered
    assert f"commit: {commit}" in rendered
    assert f"tag={tag} commit={commit}" in capsys.readouterr().out
    assert copied_deps.exists()
    assert copied_deps.read_text(encoding="utf-8") == (REPO / "flatpak" / "python-deps.json").read_text(
        encoding="utf-8"
    )


def test_cli_rejects_overwriting_local_manifest(monkeypatch, capsys):
    helper = _load_script()
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: False)
    tag = _release_tag()

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", "1" * 40,
            "--output", str(MANIFEST),
        ]
    )

    assert rc == 2
    assert "output must not overwrite the local-build manifest" in capsys.readouterr().err


def test_cli_rejects_dev_license_defaults_without_override(monkeypatch, tmp_path, capsys):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    tag = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: False)
    monkeypatch.setattr(
        helper,
        "_dev_license_defaults",
        lambda _repo: ["LICENSE_API_URL", "LICENSE_PUBLIC_KEY_HEX"],
    )

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", "e" * 40,
            "--manifest", str(manifest),
            "--output", str(output),
        ]
    )

    assert rc == 2
    assert not output.exists()
    err = capsys.readouterr().err
    assert "license defaults still point at the dev backend" in err
    assert "LICENSE_API_URL, LICENSE_PUBLIC_KEY_HEX" in err


def test_cli_allows_dev_license_defaults_for_local_dry_runs(monkeypatch, tmp_path):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    tag = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: False)
    monkeypatch.setattr(helper, "_dev_license_defaults", lambda _repo: ["LICENSE_API_URL"])

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", "f" * 40,
            "--manifest", str(manifest),
            "--output", str(output),
            "--allow-dev-license",
        ]
    )

    assert rc == 0
    assert "commit: " + "f" * 40 in output.read_text(encoding="utf-8")


def test_cli_rejects_dirty_tree_without_override(monkeypatch, tmp_path, capsys):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    tag = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: True)

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", "c" * 40,
            "--manifest", str(manifest),
            "--output", str(output),
        ]
    )

    assert rc == 2
    assert not output.exists()
    assert "worktree is dirty" in capsys.readouterr().err


def test_cli_rejects_tag_that_does_not_match_pyproject(monkeypatch, tmp_path, capsys):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    expected = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: False)
    monkeypatch.setattr(helper, "_dev_license_defaults", lambda _repo: [])

    rc = helper.main(
        [
            "--tag", "v9.9.9",
            "--commit", "d" * 40,
            "--manifest", str(manifest),
            "--output", str(output),
        ]
    )

    assert rc == 1
    assert not output.exists()
    assert f"expected {expected}" in capsys.readouterr().err


def test_cli_rejects_short_commit(monkeypatch, tmp_path, capsys):
    helper = _load_script()
    manifest = tmp_path / "local.yml"
    output = tmp_path / "release.yml"
    tag = _release_tag()
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(helper, "_working_tree_dirty", lambda _repo: False)
    monkeypatch.setattr(helper, "_dev_license_defaults", lambda _repo: [])

    rc = helper.main(
        [
            "--tag", tag,
            "--commit", "abc123",
            "--manifest", str(manifest),
            "--output", str(output),
        ]
    )

    assert rc == 1
    assert not output.exists()
    assert "full 40-character sha1" in capsys.readouterr().err
