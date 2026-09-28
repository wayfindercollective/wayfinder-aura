#!/usr/bin/env python3
"""Build src/wayfinder/core/game_list_data.py from game-chat research results.

Input: JSON lists (one per research batch) of
  {name, steam_appid, mac, text_chat, open_key, limit, paste, rules_risk,
   verdict, note, aliases, sources}
where verdict is "include" | "not_recommended" | "exclude".

Only games with text chat that can be played on a Mac are kept:
  include          -> Untested  (nobody has tried Aura in them yet)
  not_recommended  -> Not recommended (paste doesn't work, or rules forbid it)
  exclude          -> left out (no text chat, or no Mac access); the Games
                      tab tells players some games aren't listed for that reason.
Games that already have a profile or a hand-written entry in
core/macos_game_chat.py are skipped (those take precedence).

    python3 scripts/build_game_list.py            # from scripts/data/game_chat_research.json
    python3 scripts/build_game_list.py RESULT.json [RESULT.json ...]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "wayfinder" / "core" / "game_list_data.py"
sys.path.insert(0, str(ROOT / "src"))

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def clean_note(text: str) -> str:
    text = " ".join(str(text or "").split())
    text = text.replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    text = _EMOJI.sub("", text).strip()
    if text and text[-1] not in ".)!?":
        text += "."
    return text


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"[®™]", "", name.lower())).strip()


# Fixes applied after research (keyed by the final name).
NAME_FIXES = {"Sid Meier’s Civilization VI": "Sid Meier's Civilization VI"}
NOTE_OVERRIDES = {
    "Stellaris": "Multiplayer chat opens with L or the Multiplayer Chat button; open it, "
                 "then dictate. Paste is not confirmed (check in game).",
}
ALIAS_ADDS = {
    "Sid Meier's Civilization VI": ("civ", "civ 6", "civ6", "civilization"),
    "Sid Meier's Civilization VII": ("civ", "civ 7", "civ7", "civilization"),
    "Divinity: Original Sin 2 - Definitive Edition": ("dos2", "divinity"),
}

_DLC_WORDS = re.compile(r"\b(Package|Pack|Bundle|Soundtrack|Expansion|Season Pass)\b", re.I)

# Steam sells DLCs as separate products; these fold into their base game.
_DLC_BASES = {"stellaris": "Stellaris", "factorio": "Factorio"}


def fold_dlc(rows: list[dict], known: set[str] = frozenset()) -> list[dict]:
    """Keep one row per game: "Stellaris: MegaCorp" -> "Stellaris", and drop
    "X - DLC name" rows (and "X: DLC" rows) whose base game is listed."""
    names = {re.sub(r"[®™]", "", str(r.get("name", ""))).strip().lower() for r in rows} | known
    out = []
    for r in rows:
        name = re.sub(r"[®™]", "", str(r.get("name", ""))).strip()
        prefix = name.split(":", 1)[0].strip()
        if prefix.lower() in _DLC_BASES:
            r = dict(r, name=_DLC_BASES[prefix.lower()])
        elif " - " in name:
            base = name.split(" - ", 1)[0].split(":", 1)[0].strip().lower()
            if any(n.startswith(base) for n in names if n != name.lower()):
                continue
        elif ":" in name and prefix.lower() in names and prefix.lower() != name.lower():
            continue
        out.append(r)
    return out


def build(rows: list[dict]) -> list[tuple]:
    from wayfinder.core import macos_game_chat as gc

    known = {p.name.lower() for p in gc.PROFILES} | {e.name.lower() for e in gc.INFO_ONLY}
    rows = fold_dlc(rows, known)

    taken = {key(p.name) for p in gc.PROFILES} | {key(e.name) for e in gc.INFO_ONLY}
    taken |= {key(a) for e in gc.INFO_ONLY for a in e.aliases}
    entries, seen = [], set()
    for row in rows:
        verdict = str(row.get("verdict", "")).strip().lower()
        if verdict not in ("include", "not_recommended"):
            continue
        name = re.sub(r"[®™]", "", str(row.get("name", ""))).strip()
        name = NAME_FIXES.get(name, name)
        k = key(name)
        if not name or k in taken or k in seen or _DLC_WORDS.search(name):
            continue
        if gc.match_profile(None, name) is not None:
            continue  # a profile already covers it (e.g. WoW Classic, Old School RuneScape)
        note = clean_note(NOTE_OVERRIDES.get(name) or row.get("note", ""))
        if not note:
            continue
        status = "untested" if verdict == "include" else "not_recommended"
        aliases = tuple(sorted({str(a).strip().lower() for a in list(row.get("aliases") or [])
                                + list(ALIAS_ADDS.get(name, ())) if str(a).strip()}))
        sources = tuple(str(s) for s in (row.get("sources") or [])[:2])
        entries.append((name, status, note, aliases, sources))
        seen.add(k)
    return sorted(entries, key=lambda e: e[0].lower())


def write(entries: list[tuple]) -> None:
    lines = [
        '"""Researched game-chat entries for the Games tab (generated; do not edit).',
        "",
        "Built by scripts/build_game_list.py from web research on popular Mac-playable",
        "multiplayer games (Steam top sellers with Mac builds, plus non-Steam Mac",
        "games). Every entry is Untested until someone tries Aura in the game.",
        "Each tuple: (name, status, note, aliases, sources).",
        '"""',
        "",
        "RESEARCHED_GAMES = (",
    ]
    for name, status, note, aliases, sources in entries:
        lines.append(f"    ({name!r}, {status!r},")
        lines.append(f"     {note!r},")
        lines.append(f"     {aliases!r}, {sources!r}),")
    lines.append(")")
    OUT.write_text("\n".join(lines) + "\n")


DEFAULT_RESEARCH = ROOT / "scripts" / "data" / "game_chat_research.json"


def main(paths: list[str]) -> int:
    rows = []
    for path in paths or [str(DEFAULT_RESEARCH)]:
        data = json.loads(Path(path).read_text())
        rows.extend(data["games"] if isinstance(data, dict) else data)
    entries = build(rows)
    write(entries)
    by = {}
    for e in entries:
        by[e[1]] = by.get(e[1], 0) + 1
    print(f"{len(rows)} researched rows -> {len(entries)} entries {by} -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
