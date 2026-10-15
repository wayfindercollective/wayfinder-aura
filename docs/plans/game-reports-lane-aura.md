# Lane brief: game reports, Aura client

You are building the client half of game reports in Wayfinder Aura. Read these
in full first:

1. `docs/plans/game-reports-design-2026-10-15.md` (revision 3): the design.
   Section numbers below refer to it.
2. `CLAUDE.md` (the licence STOP section and rules 1-11), `AGENTS.md`,
   `docs/PLATFORM-DEVELOPMENT.md` (seams; Branches and merges) and
   `docs/CI.md`.
3. The server contract: Wayfinder OS
   `docs/plans/aura-game-reports-server-2026-10-15.md`.

## Order (do not reorder)

- **Part A, first, as a small PR:** `game_key()` and
  `scripts/export_game_catalog.py`, with their tests. It changes nothing a user
  sees and sends nothing. The server lane needs it to generate its catalog.
- **Server first, behind a flag:** the server ships to production with intake
  closed. Its labels document then says `"accepting": false`.
- **Part B, the feature PR, merges only after the server is on production.**
  Until intake opens, Part B must show no new-report UI. Build against mocks and
  the dev deployment (`fortunate-lemur-237`) using the URL overrides. Peter
  opens intake when the Beta ships; that is not part of this lane.

## Branch and PR

- For each part: a worktree and a branch from the latest `origin/main` (not the
  stale local `main`). Use `feat/game-catalog-export` for Part A and
  `feat/game-reports` for Part B.
- Open a pull request to `main`. Each merges only after `Quality`,
  `Platform smoke (macOS)` and `Windows tests` pass. Never push to `main`, never
  put `[skip ci]` on a head commit, never tag a release.
- Leave workflows and runner policy alone. If you must touch them, read
  `docs/CI.md` and run `python scripts/ci/check-runner-policy.py` first.

## Test discipline: red, then green, on real code paths

For each behaviour, write the test first against the real module or method:
the real `_inject_into_game_chat()` with platform helpers faked, the real queue
and state file in a temp dir, the real HTTP code with `requests` mocked at the
transport, and the real visibility hook. Watch it fail for the right reason,
then implement until it passes. Never re-implement or mock the function under
test. Keep UI construction tests marked `ui`, and put every decision in pure
functions that the unmarked suite covers.

## Hard rules

- **Licence integrity.** Nothing may touch licensing. `game_reports.py` and
  `game_labels.py` must not import `wayfinder.license` or `wayfinder.models_cdn`,
  read `license.json`, or send the plan. The feature is identical on Free and
  Ultra. Add the test that enforces it.
- **Privacy.** Send exactly the fields in section 5 and nothing else: never
  window titles, unlisted games, play times, the crash `install_id()`, the
  `machineId` or the licence key. Nothing is sent before **Send**.
- **Never in a game.** No `CTkToplevel` (rule 2), no notification, tray item or
  sound. The visibility hook never calls `focus_force()`, `lift()`,
  `deiconify()` or `activateIgnoringOtherApps_`, and does nothing while the game
  is frontmost.
- No network on the Tk thread: background threads, 10-second timeouts, never
  raising into the UI. No repeating timer under 100 ms (rule 1), only one-shot
  `after()` calls.
- Use design tokens and `wayfinder.ui.icons` (rule 11) and standard CTk widgets
  (rule 8).
- `DEFAULT_CONFIG` lives only in `config.py` (rule 3). New code goes in
  `src/wayfinder/` (rule 4); `wayfinder_main.py` gets thin hooks only. Nothing
  is forked per OS.

## Part A: catalog export

- `src/wayfinder/core/game_reports.py` (new) starts with `game_key(name)`. It
  converts NFKD to ASCII, lower-cases, drops `'` and `’`, turns every other run
  of non-alphanumerics into `-`, trims, and caps at 64 characters.
- `scripts/export_game_catalog.py` prints a deterministic `gameCatalog.ts`
  with a header naming the Aura SHA. It holds:
  - `AURA_GAME_CATALOG`: sorted `{key, name}` pairs from the union of the
    `macos_game_chat`, `windows_game_chat` and `linux_game_chat` `game_list()`
    names;
  - `AURA_RELEASE_LINES`: the sorted unique `major.minor` of every `vX.Y.Z` or
    `vX.Y.Z-pre` tag (`git tag`, after a fetch), ignoring other tags such as
    `macos-preview-*` and `archive/*`;
  - `RELEASE_LINES_VERSION`: a short hash of that list.

  It exits nonzero on a duplicate key or a key with two names.
- Tests: every key valid and unique; the profile games' keys pinned; the export
  deterministic and covering every entry; the conflict failures; line
  extraction from a fixture tag list.

## Part B: the feature

### 1. `src/wayfinder/core/game_reports.py`

- **URLs**, with environment overrides as `crash_reports.py` has:
  `GAME_REPORT_URL` (`WAYFINDER_AURA_GAME_REPORT_URL`) and `GAME_WITHDRAW_URL`
  (`WAYFINDER_AURA_GAME_WITHDRAW_URL`). Released builds only; a source
  checkout sends only when an override is set.
- **`game-reports.json`** in `get_config_dir()`: `0600`, atomic writes, a lock.
  - It holds `reporterId` (`secrets.token_hex(16)`, made at the first Send), the
    counter `seq` (never decreases, not even on Withdraw all), per-game `uses`,
    `asked` and `attempted`, the local reports with their state (section 6.1)
    and the daily Send count.
  - A corrupt file is renamed `.bad`, logged once, and never crashes the app.
- **`coarse_os()`:** the macOS major; Windows `10`/`11` by build (22000 or
  later is 11); Linux `ID` plus the `VERSION_ID` major, lower-case.
- **`note_game_use(name)`:** thread-safe and fast.
- **`eligible_prompt(frontmost_name, busy, now)`:** section 4.1 eligibility,
  which includes the key being in `reportable` and fresh permission (section
  4.4).
- **Sending:**
  - `submit()` writes `Queued`.
  - The sender marks the entry `Sending` **on disk before** the request starts.
  - Results follow section 11 (`Not confirmed` keeps the same `reportId` and
    `seq`; `withdrawn: true` drops silently).
  - A newer answer replaces an entry still `Queued`.
  - At most 5 Sends a day; 14-day expiry.
- **`withdraw(key=None)`:**
  - Under the lock, remove that game's queue entries.
  - If any request for it ever started, POST `{reporterId, gameKey,
    throughSeq}`. Otherwise delete locally.
  - **`throughSeq` is fixed at the press:** it is the counter then, stored on
    disk with the `Withdrawing` entry, and every retry resends that same value,
    never the current counter. A report sent after the press is never touched.
  - Keep the entry `Withdrawing` until a 200.
  - `withdraw_all()` resets the ID only after the server confirms.
- **Notes:** 280 characters, control characters stripped, links refused, blank
  omitted.

### 2. `src/wayfinder/core/game_labels.py`

- **`GAME_LABELS_URL`** (`WAYFINDER_AURA_GAME_LABELS_URL`, default
  `https://wayfindercollective.io/aura/game-labels`). At most one fetch a day,
  in the background. Cache `game-labels.json` in `get_cache_dir()`, with the
  local fetch time and the response `Date`.
- **`validate_labels(doc)`** rejects an unknown `v`, level or shape and keeps
  the last good cache. Display uses it for up to 30 days.
- **`intake_open(now)`** (section 4.4) is true only within 24 hours of a
  successful fetch whose `generatedAt` was no more than 1 hour older than its
  `Date`. Failed refreshes never extend it, and a fetch time in the future
  counts as expired. `reportable()` comes from the same document.
- **`display_status(entry, os_family, app_line)`** (section 3.4): Not
  recommended from code wins; otherwise the newest published line **not
  newer** than `app_line`; if there is none, the code label with no caption.
  It returns the caption built from the line's `sourceLine` and `carried`
  provenance (section 4.3): "Tested by Wayfinder on Aura {sourceLine}.",
  "confirmed on Aura {sourceLine}" when carried, and "anonymous reporters"
  wording. It adds "Based on recent versions only." for `capped` or
  `complete: false`.

### 3. Labels (`src/wayfinder/core/macos_game_chat.py`)

Add `COMMUNITY` ("Works with (community)") to `STATUS_LABELS` and
`STATUS_MEANINGS`, and use the section 3.1 meanings. Add `FILTER_LABELS` with
the short names. The Windows and Linux modules re-export them. Do not change
`GameProfile.tested` or any profile.

### 4. UI (`src/wayfinder/ui/game_report_card.py`)

- The inline card (section 4.2), reused under a Games-tab row.
- The "Your reports" panel with Withdraw and Withdraw all. **It is always
  available**, independent of `intake_open()` and of the labels fetch.
- Copy strings in pure, tested functions.

### 5. Hooks in `wayfinder_main.py` (thin)

- **`_inject_into_game_chat()`:** after `send_to_chat()` returns for a
  supported profile, call `note_game_use(profile.name)`. Never call it for
  `profile.caution`, unlisted games or `GameChatAborted`.
- **New `_on_main_window_visible()`** (section 4.1). It is called from:
  - an `after()` at the end of `__init__` (a no-op for minimized launches);
  - `_show_window()`;
  - root `<Map>` and root `<FocusIn>` bindings, filtered to the root and
    coalesced with one pending `after(250)`;
  - the labels-fetch callback, through the event queue.

  At each run, recheck everything: viewable and not natively hidden, idle,
  game not frontmost, no card showing, `eligible_prompt()`. If permission has
  expired, start a background fetch. The card packs under any update banner.
- **`_create_games_tab()`:**
  - `display_status()` and captions on every row, plus "You said: ...";
  - the legend and short filters;
  - the hint and row click, both only while `intake_open()` and the key is in
    `reportable()`;
  - the always-available "Your reports" line;
  - keep the 40-row cap.
- **`_create_game_chat_rows()`:** the **Ask how games went** toggle, with its
  tooltip in `SETTING_TOOLTIPS`.
- **`_start_crash_report_pass()`:** also call `send_pending()` and retry
  pending withdrawals (independent of the crash switch).

### 6. Config and docs

- `config.py`: `"game_report_prompts": True` (root decision: on by default).
- `PRIVACY.md`: section 7.1 word for word, the renamed Game detection section,
  the telemetry sentence, and network items 9 and 10.
- `CHANGELOG.md` `[Unreleased]`: one player-facing line.
- Do not edit the website; its copy ships from Wayfinder OS with the Beta.

## Tests to write first (each must fail before its code exists)

In `tests/test_game_reports.py` and `tests/test_game_labels.py`, plus
`tests/test_release_metadata.py`:

1. **Payload and imports:** exact payload keys; no licence, machine ID, crash
   install ID or plan; the AST import ban.
2. **Reporter ID and counter:** the ID is 32 hex, persistent and different from
   `crash_reports.install_id()`; the file is `0600` (not checked on Windows);
   corrupt-file recovery; `seq` never decreases, including across Withdraw all.
3. **Coarse OS:** macOS `15.4.1`, Windows builds 19045 and 22631, and
   os-release fixtures (SteamOS, Bazzite, Ubuntu, Arch without `VERSION_ID`).
4. **Counting through the real `_inject_into_game_chat()`:** a supported game
   counts; a Not recommended game, an unlisted game and an aborted send do not.
5. **The visibility hook:**
   - each entry point (cold-launch `after()`, `_show_window()`, `<Map>`,
     `<FocusIn>`, the fetch callback) reaches it, coalesced per burst;
   - a minimized launch shows nothing;
   - it never calls a focus or raise method;
   - **a delayed fetch callback that lands after the game is frontmost again
     shows no card.**
6. **Eligibility:** below 3; asked; switch off; not `reportable`; permission
   expired; frontmost; busy; most-recent first.
7. **Permission lifetime:**
   - expires 24 hours after a fetch;
   - a failed refresh does not extend it;
   - a clock moved back counts as expired;
   - `Date - generatedAt` over 1 hour grants nothing.
8. **States and withdrawal:**
   - a never-attempted report is deleted locally with no network;
   - Sending (a crash simulated mid-request), Not confirmed, Sent, and
     Couldn't send after an earlier attempt each POST with `throughSeq` equal to
     the counter at the press;
   - **the fixed boundary:** press Withdraw at 9, send a new report (10), then
     let the withdrawal retry. It resends 9, and report 10 stays and is sent;
   - the queue stops retrying that game;
   - offline stays Withdrawing; reset happens only on 200;
   - `withdrawn: true` drops silently.
9. **Always available:** Your reports and Withdraw work with intake closed and
   with the labels fetch failing.
10. **Labels:**
    - schema rejection keeps the old cache; 30-day display expiry;
    - **an app older than every published line falls back to code**; between
      lines it takes the older one; a newer app takes the newest;
    - merge order; the `capped` and `complete` captions;
    - provenance captions, including a carried source no longer among the six.
11. **Notes:** length, links refused, control characters stripped.
12. **Docs:** `PRIVACY.md` names "Game reports" and "Community game labels".
13. **Design ratchet:** `tests/test_design_ratchet.py` passes untouched.

## Gate

Before every push of a branch, run the `Quality` job's commands locally:

```bash
ruff check --select F821,F823,F722,E9 .
python scripts/verify_structure.py
python -m pytest tests/ -q --tb=short -m "not ui and not slow and not network and not perf"
```

Then the PR's required checks (`Quality`, `Platform smoke (macOS)`,
`Windows tests`) must pass on the owner's self-hosted runners. An offline runner
means wait, not pass.

## Manual end-to-end (Part B, before asking for merge)

With the server on dev and intake opened there, run a source build with the
three overrides pointed at dev (`https://fortunate-lemur-237.convex.site/...`
and the dev Vercel `/aura/game-labels`).

1. Make 3 game dictations, quit, and cold-launch: the card appears without
   taking focus.
2. Send a report. Withdraw it, then Withdraw all.
3. Close intake on dev and check that Your reports still works.
4. Confirm nothing appears while the game is in front.

Record the results in the PR.

## Report back

Both PR links, the gate output, the platforms you exercised by hand, and any
deviation from the design, with the reason.
