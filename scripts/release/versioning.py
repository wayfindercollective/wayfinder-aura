"""Version bookkeeping shared by the Beta workflow and promote.py.

A release is identified by its tag (``v1.2.0-beta.3``, ``v1.2.0``). Every file
that embeds the version must agree with the tag: Release Readiness refuses a
tag that does not match pyproject.toml, the DMG and AppImage are named from
it, and the in-app updater only offers a download whose name carries it. So a
release is cut as one small commit on top of the main commit being shipped
("release: 1.2.0-beta.3") that stamps the version everywhere, and the tag
points at that commit. main itself only carries the next planned version.

Standard library only: the Beta job runs this before any dependency install.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]

# (file, pattern whose group 1 is the version). Each must match exactly once.
VERSION_FILES = (
    ("pyproject.toml", r'^version = "([^"]+)"'),
    ("src/wayfinder/__init__.py", r'^__version__ = "([^"]+)"'),
    ("scripts/build-appimage.sh", r'^VERSION="([^"]+)"'),
    ("packaging/windows/installer.iss", r'^  #define MyAppVersion "([^"]+)"'),
)
METAINFO = "flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml"
CHANGELOG = "CHANGELOG.md"

# What a complete release carries ({v}: the tag without its "v"). The Release
# workflow uploads the Linux files; the Mac release watcher (or publish-macos,
# once signing secrets exist) attaches the DMG. Windows stays internal.
WORKFLOW_ASSETS = (
    "Wayfinder_Aura-{v}-x86_64.AppImage",
    "Wayfinder_Aura-{v}-x86_64.AppImage.zsync",
    "io.wayfindercollective.WayfinderAura.flatpak",
)
MAC_ASSET = "Wayfinder_Aura-{v}-macOS-arm64.dmg"


def release_assets(tag: str, mac: bool = True) -> set[str]:
    """Asset names release ``tag`` must carry (without the DMG: mac=False)."""
    version = tag[1:] if tag.startswith("v") else tag
    return {name.format(v=version) for name in WORKFLOW_ASSETS + ((MAC_ASSET,) if mac else ())}


def missing_assets(release: dict | None, names: set[str]) -> list[str]:
    """Which of ``names`` the release lacks as uploaded assets (all of them
    when there is no published release)."""
    if not release or release.get("draft"):
        return sorted(names)
    have = {a.get("name") for a in release.get("assets") or [] if a.get("state") == "uploaded"}
    return sorted(names - have)

_TAG_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def parse(tag: str):
    """("v1.2.0-beta.3") -> ((1, 2, 0), ("beta", "3")); stable -> (core, None)."""
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    core = (int(match[1]), int(match[2]), int(match[3]))
    return core, (tuple(match[4].split(".")) if match[4] else None)


def beta_number(tag: str):
    """N for a ``vX.Y.Z-beta.N`` tag, else None."""
    parsed = parse(tag)
    if not parsed or not parsed[1] or len(parsed[1]) != 2:
        return None
    kind, number = parsed[1]
    return int(number) if kind == "beta" and number.isdigit() else None


def core_str(core) -> str:
    return ".".join(str(part) for part in core)


def latest_stable(tags):
    stables = [p[0] for p in map(parse, tags) if p and p[1] is None]
    return max(stables) if stables else None


def next_beta(main_version: str, tags) -> str:
    """The next beta version for main.

    main's version names the next stable release. Once that version has
    shipped (main still says 1.2.0 after v1.2.0), betas move on to the next
    patch by themselves, so nobody has to remember a bump after each release.
    """
    parsed = parse(main_version)
    if parsed is None:
        raise ValueError(f"unreadable main version {main_version!r}")
    base = parsed[0]
    stable = latest_stable(tags)
    if stable is not None and base <= stable:
        base = (stable[0], stable[1], stable[2] + 1)
    numbers = [
        n for tag in tags
        if (n := beta_number(tag)) is not None and parse(tag)[0] == base
    ]
    return f"{core_str(base)}-beta.{max(numbers, default=0) + 1}"


def read_version(root: Path = ROOT) -> str:
    text = (root / VERSION_FILES[0][0]).read_text(encoding="utf-8")
    match = re.search(VERSION_FILES[0][1], text, re.MULTILINE)
    if not match:
        raise ValueError("no version in pyproject.toml")
    return match[1]


def set_version(version: str, root: Path = ROOT) -> list[Path]:
    """Write ``version`` into every file that embeds it."""
    if parse(version) is None:
        raise ValueError(f"not a release version: {version!r}")
    changed = []
    for relative, pattern in VERSION_FILES:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        matches = list(re.finditer(pattern, text, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"{relative}: expected one version line, found {len(matches)}")
        start, end = matches[0].span(1)
        path.write_text(text[:start] + version + text[end:], encoding="utf-8")
        changed.append(path)
    return changed


# --- CHANGELOG ---------------------------------------------------------------

_SECTION_RE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def unreleased_body(text: str) -> str:
    """Everything between ``## [Unreleased]`` and the next ``## [`` heading."""
    match = re.search(r"^## \[Unreleased\][^\n]*\n", text, re.MULTILINE)
    if not match:
        raise ValueError("CHANGELOG has no ## [Unreleased] section")
    following = _SECTION_RE.search(text, match.end())
    end = following.start() if following else len(text)
    return text[match.end():end]


def bullet_blocks(body: str) -> list[str]:
    """Top-level ``- `` bullets with their wrapped continuation lines."""
    blocks, current = [], None
    for line in body.splitlines(keepends=True):
        if line.startswith("- "):
            if current is not None:
                blocks.append(current)
            current = line
        elif current is not None and line.startswith("  ") and line.strip():
            current += line
        else:
            if current is not None:
                blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def bullet_titles(body: str) -> list[str]:
    """The bold lead of each bullet ("**Faster startup.** ..." -> "Faster startup.")."""
    titles = []
    for block in bullet_blocks(body):
        flat = " ".join(block[2:].split())
        bold = re.match(r"\*\*(.+?)\*\*", flat)
        titles.append(bold[1] if bold else flat)
    return titles


def _tidy(body: str) -> str:
    """Drop ``###`` headings left with no bullets, and runs of blank lines."""
    out = []
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("### "):
            rest = "".join(lines[index + 1:])
            nxt = re.search(r"^(### |- )", rest, re.MULTILINE)
            if not nxt or nxt[1] == "### ":
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip("\n")


def promote_changelog(text: str, version: str, released_body: str, when: date) -> str:
    """Move ``released_body`` (the Unreleased notes of the commit being
    released) under ``## [version] — date``, leaving anything newer under
    Unreleased. Bullets are matched verbatim, so notes added to main after
    the beta was cut stay unreleased."""
    body = unreleased_body(text)
    remaining = body
    for block in bullet_blocks(released_body):
        remaining = remaining.replace(block, "", 1)
    lead = released_body.split("\n### ", 1)[0].strip()
    if lead and not lead.startswith("- "):
        remaining = remaining.replace(lead, "", 1)
    head, tail = text.split(body, 1) if body else (text, "")
    new_unreleased = _tidy(remaining)
    released = _tidy(released_body)
    section = f"## [{version}] — {when.isoformat()}\n\n{released}\n\n"
    return (
        head.rstrip("\n") + "\n\n"
        + (new_unreleased + "\n\n" if new_unreleased else "")
        + section
        + tail.lstrip("\n")
    )


# --- AppStream metainfo --------------------------------------------------------

def add_metainfo_release(
    version: str, when: date, intro: str, items, *, development: bool, root: Path = ROOT
) -> None:
    """Insert ``<release>`` at the top of <releases> (newest first)."""
    path = root / METAINFO
    text = path.read_text(encoding="utf-8")
    if f'<release version="{version}"' in text:
        return
    kind = ' type="development"' if development else ""
    lines = [f'    <release version="{version}" date="{when.isoformat()}"{kind}>',
             "      <description>",
             f"        <p>{escape(intro)}</p>"]
    items = [item for item in items if item]
    if items:
        lines.append("        <ul>")
        lines += [f"          <li>{escape(item)}</li>" for item in items]
        lines.append("        </ul>")
    lines += ["      </description>", "    </release>"]
    marker = "  <releases>\n"
    if marker not in text:
        raise ValueError("metainfo has no <releases> block")
    path.write_text(text.replace(marker, marker + "\n".join(lines) + "\n", 1), encoding="utf-8")
