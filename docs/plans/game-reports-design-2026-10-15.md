# Game reports: how well Aura works in each game

Status: design, revision 3 (after Sol 6.1 xhigh reviews r1, REVISE 8, and
r2, REVISE 2).
Approved in shape by Peter on 2026-10-15. Not built.
Server half: Wayfinder OS `docs/plans/aura-game-reports-server-2026-10-15.md`.
Build briefs: `docs/plans/game-reports-lane-aura.md` (this repo) and Wayfinder OS
`docs/plans/aura-game-reports-lane-server.md`.

All Aura paths below were read on `origin/main` at `d7f4d49` (2026-10-12). The
local `main` checkout on the Mac Studio was 182 commits behind; branch from
`origin/main`.

## 1. Summary

Players can tell Aura how it worked in a game. Peter's own test sessions and
those anonymous player reports set each game's label in the Games tab.

- After a few dictations into a supported game, Aura asks once, inside its own
  window, when the window is next visible to the player: "How did Aura work in
  World of Warcraft?" with **Works**, **Some issues** and **Doesn't work**, and
  an optional short note. Any game on the server's catalog can also be reported
  from the Games tab.
- Nothing leaves the computer until the player presses **Send**. A report
  carries the game's name, the Aura version, the operating system and its
  major version, the answer, the note if typed, a random report ID used only
  for game reports, and a counter. Players can see and withdraw what they
  sent, even while intake is closed or offline.
- The labels form a ladder: **Untested**, **Playable**, **Works with
  (community)** and **Verified**. Verified means Peter tested it himself;
  marketing says "Works with", and says "Verified" only for those games.
  Community promotion needs 3 or more anonymous reporters on the current Aura
  version line, with no open issue reports, counted from complete per-version
  totals. Only trusted release lines (lines with an Aura release tag) count.
  Problems reported after an update drop a game one level.
- Labels are per operating system, because Gamer mode differs per system.
- Notes are read only on Wayfinder's signed-in review page. The weekly Slack
  digest carries counts and label changes, never notes, so withdrawal and the
  90-day note erasure reach every copy.
- The server lives in Wayfinder OS beside the existing Aura feedback and crash
  endpoints, with a server-owned catalog of game keys. It ships first with
  intake closed. The client ships next and offers new reports only while a
  fresh labels document says intake is open.

## 2. What exists today

| Concern | Where |
| --- | --- |
| Games list, labels, notes | `src/wayfinder/core/macos_game_chat.py`: `STATUS_LABELS` (Verified, Playable, Untested, Not recommended), `STATUS_MEANINGS`, `GameProfile.tested`, `profile_entry()`, `INFO_ONLY`, `game_list()`, `search_games()`. Windows and Linux reuse them in `windows_game_chat.py` and `linux_game_chat.py` (Linux adds Dark Age of Camelot). Researched entries are generated into `src/wayfinder/core/game_list_data.py` by `scripts/build_game_list.py`; all are `untested`. No profile sets `tested=True`, so no game shows Verified today. |
| Which game is in front | macOS `macos_game_chat.frontmost_app()` (NSWorkspace bundle id and name); Windows `windows_game_chat._foreground()` (exe name and title); Linux `linux_game_chat._front_window()` (`_NET_ACTIVE_WINDOW`, `WM_CLASS`, title, gamescope `STEAM_GAME`). Matching: `match_profile()` and `unlisted_game_reason()`; the backend is chosen by `_game_chat_module()` in `wayfinder_main.py`. |
| When detection runs | Only at dictation time: `WayfinderApp._gamer_profile_now()` and `WayfinderApp._inject_into_game_chat()`, on the injection worker thread. No background polling. |
| Games tab UI | `WayfinderApp._create_games_tab()` (legend, search, segmented filter, 40-row cap) and `_create_game_chat_rows()` (Gamer mode and Send game messages toggles). |
| Settings | `src/wayfinder/config.py` `DEFAULT_CONFIG`; `SETTING_TOOLTIPS` in `wayfinder_main.py`; Settings > System holds Updates and Send crash reports. |
| Dictate-tab banner | `_show_app_update_banner()` / `_hide_app_update_banner()` with `_dictate_banner_anchor`: an inline `CTkFrame`, the pattern this feature reuses. |
| Window lifecycle | `WayfinderApp.__init__` maps the root during a cold launch and schedules startup work with `after()` (model and app update checks, then `_start_crash_report_pass()` at 8 s); `--minimized` and `start_minimized` hide it again. `_show_window()` is reached only from the tray and a second launch (it deiconifies and raises). Native restoration (Dock click, taskbar, window manager) goes through neither. |
| Tray | macOS: pystray in `setup_tray()`. Linux: a Qt StatusNotifier in the overlay subprocess. Windows: its own. |
| Network calls | Crash reports `src/wayfinder/core/crash_reports.py` (`CRASH_API_URL`, env override, queue, `install_id()`, 14-day expiry); feedback `src/wayfinder/core/feedback.py`; both POST to `https://fine-shrimp-886.convex.site/api/aura/*`. Licence `src/wayfinder/license.py` talks to a separate deployment, `shiny-goshawk-432.convex.site`. Model catalog `src/wayfinder/model_catalog.py`. TLS via `src/wayfinder/tls.py`. |
| Privacy notice | `PRIVACY.md`: "No analytics, no telemetry" and an 8-item network list. Crash reports are the only diagnostic data today. |
| Server side | Wayfinder OS `convex/auraDesktop/{crashReports,feedback,httpHelpers}.ts`, tables in `convex/schema/schemaAuraDesktop.ts`, routes in `convex/http.ts`. Downloads are Next.js routes, `src/app/aura/download/{mac,linux,windows}/route.ts`. |

## 3. Labels and thresholds

### 3.1 The ladder

| Rank | In-app label | Meaning shown in the legend | Marketing may say |
| --- | --- | --- | --- |
| 3 | **Verified** | "We tested Aura in this game ourselves." | "Works with" or "Verified" |
| 3 | **Works with (community)** | "3 or more anonymous reporters say it works on this version, and none report a problem." | "Works with (community reports)". Never "Verified". |
| 2 | **Playable** | "Works, maybe with a step from you, or with small issues." | Nothing |
| 1 | **Untested** | "Not tried yet, or not enough reports." | Nothing |
| none | **Not recommended** | Unchanged: the game's rules or chat make it a bad idea. | Nothing |

Verified and Works with (community) share a rank with different sources;
Verified is the stronger claim. Not recommended stays in code, and reports
never change it. "Reporters" are anonymous report IDs, not verified people;
copy says so wherever a count is shown.

### 3.2 Units

- **Per game, per operating system** (`macos`, `windows`, `linux`).
- **Per Aura version line**, `major.minor`: `1.2.0`, `1.2.1` and
  `1.2.1-beta.2` are all line `1.2`. Lines compare numerically (`1.10 > 1.9`).
- **Per reporter:** one report ID counts once per game, system and line, with
  its newest answer.
- **Only catalog games:** the server accepts reports only for game keys in its
  own catalog, with the exact canonical name (section 5.5).
- **Only trusted release lines count.** The catalog also lists every line
  that has an Aura release tag (section 5.5). Reports on the one next minor
  line, or the next major's `.0`, are kept in **quarantine**: stored, but
  never folded, published or counted against any budget, until a regenerated
  list makes that line trusted. Any other version is refused. A fabricated
  line such as `900.0` can therefore never push a real line out of the six
  published ones.

### 3.3 The rule

The server keeps **complete** per-line totals: for each game, system and line,
the number of open reports by answer (W works, S some issues, D doesn't work),
updated in the same transaction as every report write. Hidden reports (Peter
marked them resolved, not Aura's fault or spam) are not open. The fold reads
the trusted lines only, by direct lookup, and walks them oldest to newest. The
server version also records provenance (server design section 5):

```
rank, verified = UNTESTED, false
for each line L, oldest first:
    if Peter recorded a test session on L (newest one counts):
        rank = WORKS if works, PLAYABLE if some issues, UNTESTED if doesn't work
        verified = (result == works)
        continue          # his test decides L; later reports on L only reach the digest
    evidence = WORKS    if W >= 3 and S + D == 0 and the open works answers span 24 hours or more
               PLAYABLE if W + S >= 1 and D < W + S
               UNTESTED otherwise
    carried  = max(UNTESTED, rank - 1) if S + D > 0 else rank
    if evidence > carried: rank, verified = evidence, false
    else:                  rank, verified = carried, verified and carried == WORKS
    publish (L, rank, verified, W, S, D)
```

The policy in words:

- **Untested, then Playable:** one anonymous reporter saying Works or Some
  issues, with fewer saying Doesn't work, makes a game Playable.
- **Works with (community):** 3 or more reporters on the line say Works, none
  report a problem on that line, and their answers did not all arrive within
  24 hours (a cheap guard against one person minting IDs).
- **Carry forward:** a label earned on `1.2` stays on `1.3` until someone on
  `1.3` reports a problem. An update alone never wipes a label.
- **Problems after an update drop the carried label one level.** That line's
  own reports can still hold it at Playable, because "Some issues" means it
  works with a catch:

  | Before (line 1.2) | Reports on 1.3 | After |
  | --- | --- | --- |
  | Verified | 1 Some issues | Playable (and **re-test** in the digest) |
  | Works with (community) | 1 Some issues | Playable |
  | Playable | 1 Some issues | Playable (the new line's own evidence holds it) |
  | Playable | 1 Doesn't work | Untested |
  | Playable | 1 Works, 1 Doesn't work | Untested (a tie is not support) |
  | Untested | 1 Some issues | Playable |

  On the same line, a single Some issues report also takes Works with
  (community) down to Playable, because promotion needs no open issues. When
  Peter hides a report, the totals change and the label is recomputed.
- **Verified** holds on Peter's line. Issues on a newer line drop it like
  any other label, and the digest marks the game **re-test**.
- **Incomplete evidence never promotes.** Totals are exact per line. Only if a
  game ever had more than 200 version lines, or Peter more than 200 sessions,
  would the server read fewer. It then folds only the lines it read, starting
  from Untested. The fold never ranks higher from a lower start, so the result
  can only be lower than the truth. It also publishes `complete: false`, and Aura
  shows "Based on recent versions only."

Thresholds live in one server constant block (`MIN_COMMUNITY_REPORTERS = 3`,
`COMMUNITY_SPAN_MS = 24h`). The cases above are in the table-driven tests.

### 3.4 Which label the app shows

The server publishes, for each game and system, the folded state after each of
the newest six **trusted** lines (section 5.3). Each state carries its
provenance: `sourceLine` (the line whose evidence or test session set the
level) and `carried` (true when that source is older than the line). The app:

1. shows **Not recommended** whenever its own code says so;
2. otherwise takes the newest published line **not newer** than the app's own
   line, and shows Verified, Works with (community), Playable or Untested from
   it;
3. if no published line is that old (the game only has reports from newer
   versions, or the app's line has aged out of the six), shows the label from
   code with no evidence caption. Nothing from a newer version leaks onto an
   older app;
4. with no usable labels document at all, shows the label from code, as
   today.

So a player on Stable `1.2` never sees a verdict that only `1.3` testers gave,
and a player on `1.3` sees `1.3`'s state.

Peter's verdicts are recorded on the server, not by setting
`GameProfile.tested`. The flag stays as it is and only matters as the code
baseline.

## 4. Client experience

Peter's rules: unobtrusive, simple, no option-heavy screens, never steal focus
mid-game, never block input.

### 4.1 When Aura asks

- **Counting.** When `_inject_into_game_chat()` delivers a dictation through
  `send_to_chat()` into a supported game (a matched profile that is not Not
  recommended), Aura adds one to that game's local count. Not recommended games
  are not counted (on the Mac their branch hands off to the normal paste, and
  Aura shouldn't nudge players to report a game it advises against). Unlisted
  games are never counted: their name comes from a window class or title,
  which can carry a character or server name.
- **Eligible:** the count is 3 or more; the game was never asked about on this
  computer; **Ask how games went** is on; the game's key is in the server's
  `reportable` list; and intake permission is fresh (section 4.4).
- **The visibility hook.** One method, `_on_main_window_visible()`, decides,
  and it is called from every way the window becomes visible to the player:
  - **cold launch**: scheduled with `after()` from the end of
    `WayfinderApp.__init__`, beside the existing startup work; it does
    nothing when the launch was minimized (`--minimized`, `start_minimized`);
  - **tray and second launch**: `_show_window()`;
  - **native restoration**: a root-window `<Map>` binding (deiconify or
    restore on Windows and Linux) and a root-window `<FocusIn>` binding (the Mac
    Dock, Cmd-Tab, clicking the window), filtered to the root and coalesced
    with one pending `after(250)` so a burst of events runs it once;
  - **an asynchronous labels fetch completing**: the fetch callback re-runs it
    through the Tk event queue.

  Each run rechecks everything at that moment: the window is viewable and not
  natively hidden, the app is idle (not recording or processing), the game in
  question is **not** frontmost, no card is already showing, and eligibility
  holds. A late callback that lands after the player went back to the game does
  nothing; the next visibility event asks again.
- **Never:** the hook never calls `focus_force()`, `lift()`, `deiconify()` or
  `activateIgnoringOtherApps_`; it only packs a frame into a window the player
  is already looking at. No notification, no `CTkToplevel`, no tray item, no
  sound, no overlay change. On the Steam Deck in Game Mode the window is not
  visible, so the card waits for Desktop Mode.
- **At most once per game.** Closing the card counts as asked. The Games tab
  always lets a player report again.

Tray item, rejected for v1: the tray lives in three places (pystray on the
Mac, the overlay subprocess's Qt tray on Linux, its own on Windows). A
once-per-game question is not worth a menu item in all three.

### 4.2 The card

Inline on the Dictate tab, under any update banner (the
`_dictate_banner_anchor` pattern), in Aura's tokens:

```
How did Aura work in World of Warcraft?          [x]
( Works )  ( Some issues )  ( Doesn't work )
Don't ask again
```

The pills use the overlay's state colours: mint for Works (the PASTING
colour), gold for Some issues (PROCESSING) and rose for Doesn't work
(RECORDING). `[x]` is the Lucide `x` from `wayfinder.ui.icons`.

After a pill is pressed:

```
How did Aura work in World of Warcraft?          [x]
( Works )  ( Some issues )  ( Doesn't work )          <- chosen pill stays lit
[ Add a note (optional)                              ]
Sends only: World of Warcraft, Aura 1.2.1, macOS 15, your answer and note.
                                       [ Cancel ]  [ Send ]
```

- Placeholder for Works: "Anything worth knowing? No names or contact
  details, please." For the others: "What went wrong? For example: chat didn't
  open, or the text didn't paste."
- 280 characters, with a live count past 240. A link shows "Links can't be
  sent" inline and disables Send.
- Send: "Thanks. It helps other players pick games. You can see or withdraw it
  in Games." The card closes after 4 seconds (one `after()`).
- Offline: "Saved. Aura sends it when you're online."
- Don't ask again: sets `game_report_prompts` to off and says "Aura won't ask.
  You can still report a game from the Games tab."

### 4.3 The Games tab

- **Labels** from section 3.4, with one short evidence caption in muted text,
  built from the provenance so it stays true after the source line leaves the
  six:
  - Verified: "Tested by Wayfinder on Aura {sourceLine}."
  - Community, not carried: "{works} anonymous reporters say it works on Aura
    {line}."
  - Community, carried: "Works with (community), confirmed on Aura
    {sourceLine}."
  - After a drop: "Problems reported on Aura {sourceLine}."

  When the document is `capped`, or a game's `complete` is false, the caption
  adds "Based on recent versions only."
- **Legend** with the new label and meanings (section 3.1). The filter uses
  short names: All, Verified, Community, Playable, Untested, Not recommended.
- **New reports** (only while intake permission is fresh and the game is in
  `reportable`): a hint under the legend, "Played one of these? Click it to
  tell us how Aura worked.", and clicking a row expands the card under it. There
  are no per-row buttons.
- **Your answer:** a reported row shows "You said: Works".
- **Your reports, always available.** One quiet line at the bottom of the
  GAMES tile: "Your reports: 2. See or withdraw." It expands inline into what
  this computer sent: game, answer, note, version, date and status (Sent,
  Waiting to send, Not confirmed, Couldn't send), with **Withdraw** per game
  and **Withdraw all**. This list and withdrawal work while intake is closed,
  while the labels fetch fails and while offline (withdrawals then wait). The
  intake switch only governs new reports.
- **The switch:** one toggle in the Gamer mode tile, **Ask how games went**
  (`game_report_prompts`, on by default). Tooltip: "After you've used Aura in a
  game a few times, Aura asks once how it went, in its own window, never over
  the game. Nothing is sent unless you press Send."

### 4.4 Intake permission has a short life

`accepting: true` lets Aura offer new reports for **24 hours after the last
successful fetch**, and only if that document was fresh when fetched: its
server `generatedAt` is no more than 1 hour older than the response's HTTP
`Date` (or the local clock when `Date` is missing). A failed refresh never
extends it, and a local fetch time in the future (clock moved back) counts as
expired. The server lets the CDN serve a copy at most 20 minutes old (section
5.3). So closing intake removes report UI from every app within about a day
(25 hours at most), even if refreshes fail. Cached labels for display are a
separate matter: the last good document is used for up to 30 days, then the
code labels.

## 5. Data sent

Three requests from `src/wayfinder/core/game_reports.py` and
`game_labels.py`, on background threads, with the existing TLS setup. Released
(PyInstaller) builds only, like crash reports; a source checkout sends only
when a URL override is set for testing.

### 5.1 Submit a report

`POST https://fine-shrimp-886.convex.site/api/aura/game-report`
(override `WAYFINDER_AURA_GAME_REPORT_URL`), `Content-Type: application/json`:

```json
{
  "reportId": "9f3c...32 lowercase hex, new for each Send",
  "reporterId": "41ab...32 lowercase hex, this computer's report ID",
  "seq": 7,
  "gameKey": "world-of-warcraft",
  "gameName": "World of Warcraft",
  "answer": "works",
  "note": "Chat opened and sent fine in a raid.",
  "appVersion": "1.2.1",
  "os": "macos",
  "osVersion": "15"
}
```

- `gameKey`: the list name folded to lower-case ASCII; apostrophes dropped;
  every other run of non-alphanumerics becomes `-`; at most 64 characters
  (`Garry's Mod` -> `garrys-mod`). `gameName`: exactly as Aura's list spells it.
  The server accepts the pair only if it matches its catalog.
- `answer`: `works`, `some_issues` or `doesnt_work`. `note`: omitted when
  empty.
- `os` and coarse `osVersion`: macOS major (`15.4.1` -> `15`); Windows `10` or
  `11` (build 22000 or later is 11); Linux os-release `ID` plus the major of
  `VERSION_ID` (`steamos 3`, `bazzite 42`, `arch`). Never the kernel,
  hostname, CPU, display server or package type.
- `seq`: a per-computer counter, increased for every Send and never reset
  (not even by Withdraw all). The server uses it to keep the newest answer and to
  reject anything older than a withdrawal. It is used instead of a timestamp,
  which would say when the player played.
- `reportId` makes retries idempotent.
- `appVersion` must be on a trusted line or the one pending next line (section
  3.2); otherwise the server answers 400.

### 5.2 Withdraw

`POST https://fine-shrimp-886.convex.site/api/aura/game-report/withdraw`
(override `WAYFINDER_AURA_GAME_WITHDRAW_URL`):

```json
{ "reporterId": "41ab...", "gameKey": "world-of-warcraft", "throughSeq": 9 }
```

- **`throughSeq` is fixed at the moment the player presses Withdraw.** It is
  the counter's value then, so it covers every submission made so far. It is
  stored on disk with the pending withdrawal, and **every retry resends that
  same value**, never the current counter. A report sent after the press has a
  higher counter and is never touched by that withdrawal.
- Leave out `gameKey` to withdraw everything.
- The server keeps a **withdrawal barrier** for 30 days: a short record (the ID
  fingerprint, the game key or "all", and `throughSeq`, no answer or note). It
  makes the server refuse any later-arriving submission for that game with
  `seq <= throughSeq`, so a delayed or retried copy can't bring a withdrawn
  report back.
- The server deletes **only rows with `seq <= throughSeq`** on every page and
  continuation, using that request's own value. So a replayed withdrawal, after
  a new report at a higher counter was accepted, deletes nothing.
- A later report with a higher counter is accepted normally.
- The answer is `{"ok": true, "withdrawn": 2}`. Repeating it is safe, and it
  works while intake is closed.

### 5.3 Fetch labels

`GET https://wayfindercollective.io/aura/game-labels` (override
`WAYFINDER_AURA_GAME_LABELS_URL`), no body, no cookies, no ID. It runs at most
once a day, when the Games tab opens or the visibility hook finds the
permission expired with a game otherwise eligible. Cached in the cache dir as
`game-labels.json` with its local fetch time and response `Date`.

```json
{
  "v": 1,
  "accepting": true,
  "generatedAt": 1760548800000,
  "capped": false,
  "reportable": ["albion-online", "world-of-warcraft"],
  "games": [
    { "key": "world-of-warcraft", "os": "macos", "complete": true,
      "lines": [
        { "line": "1.2", "level": "works", "verified": true,
          "sourceLine": "1.2", "carried": false,
          "works": 2, "someIssues": 0, "doesntWork": 0 },
        { "line": "1.3", "level": "playable", "verified": false,
          "sourceLine": "1.3", "carried": false,
          "works": 1, "someIssues": 1, "doesntWork": 0 } ] }
  ]
}
```

`reportable` is the server catalog's keys. Status rows exist only for catalog
games, and only trusted lines appear, so the feed is bounded by the catalog
and the release list (section 5.5). The app rejects a
document whose `v`, levels or shapes it doesn't know, and keeps the last good
cache. The server serves it with `s-maxage=600, stale-while-revalidate=600`.

### 5.4 Never sent

Audio, dictated or transcribed text, the clipboard, window titles, other apps
or games, unlisted games, play times, settings, licence key, plan, machine ID,
the crash-report install ID, API keys, email, hostname, user name. The server
sees the address of any request (section 8).

### 5.5 The game catalog and release lines (server-owned)

The server only accepts reports for games and version lines it knows. Both lists
live in `convex/auraDesktop/gameCatalog.ts` in Wayfinder OS, reviewed in a PR,
and are generated by this repo's `scripts/export_game_catalog.py`:

- **Games:** the union of the macOS, Windows and Linux `game_list()` names,
  through `game_key()`. The script fails on a duplicate key or a key with two
  names.
- **Release lines:** every `major.minor` with at least one `vX.Y.Z` or
  `vX.Y.Z-pre` tag in this repository (`git tag`, after a fetch). Other tags
  (`macos-preview-*`, `archive/*`) are ignored. Tagging a release is Peter's own
  act, so a tagged line is trusted.

When an Aura release adds games or opens a new line, one Wayfinder OS PR
regenerates the file. Until it lands, new games are not in `reportable`, so
Aura offers no report for them. Reports on the new line wait in quarantine on
the server and join the labels within a day of the PR's deploy. Invented keys
and versions are refused before anything is written, so they can never take
room in the labels feed.

## 6. Identifying a distinct reporter without tracking

- On the first Send, Aura makes `reporterId = secrets.token_hex(16)` and stores
  it in `game-reports.json` beside `config.json` (`get_config_dir()`, mode
  `0600`, written atomically). It survives cache cleaning and updates.
- It is **not** the crash-report `install_id()`, so a game report can't be
  joined to a crash report, and it can be reset on its own. It is **not** the
  licence key or the activation `machineId` (`license.py`): free users have
  neither, and either would tie a game opinion to a purchase or a machine.
  Linking to the licence would resist spam better, but identifying players is
  not worth it. Section 8 handles spam.
- The server stores only `SHA-256(reporterId)` and compares hashes. With 128
  random bits, the hash can't be reversed or guessed, so no pepper or new
  secret is needed.
- The raw ID is the player's proof of ownership for withdrawal; only their
  computer has it. **Withdraw all** replaces it after the server confirms.
  Deleting the settings folder loses it; PRIVACY.md says to withdraw first,
  and retention ends everything after two years anyway.

### 6.1 Local report states (and why withdrawal asks the server)

| State | Meaning | On Withdraw |
| --- | --- | --- |
| Queued | Never attempted: no request has started | Deleted locally; no network |
| Sending / Not confirmed | A request started (marked on disk **before** it starts), with no definite answer: a timeout, a dropped connection, 429, 404, 503 or 5xx. The server may have it. | Server withdrawal |
| Sent | 200 | Server withdrawal |
| Couldn't send | 400, 413 or 415: the server wrote nothing for this submission | Deleted locally, but if any other submission for that game was ever attempted, server withdrawal |
| Withdrawing | Waiting for the server to confirm; holds the `throughSeq` fixed at the press | Retried at launch **with that same `throughSeq`**; removed only on 200 |

Rule: **if any request for that game ever started from this computer, Withdraw
goes to the server.** Over-withdrawing is harmless (it is idempotent). Before
sending the withdrawal, Aura removes that game's entries from the send queue
under the state lock, so it stops retrying them. A request already in flight
is caught by the server's barrier, because its `seq` is not above
`throughSeq`. A submission answered with `{"withdrawn": true}` is dropped
silently. A new report for the same game made after the press (a higher
counter) is sent normally, and a retried withdrawal cannot delete it.

## 7. Privacy text

### 7.1 New PRIVACY.md section (after "Crash reports")

> ## Game reports (only when you press Send)
>
> Aura can ask how it worked in a game, so players can see which games it
> works with. Nothing is sent unless you press **Send** on a report.
>
> - **When Aura asks.** After you have dictated into a supported game a few
>   times, Aura may ask once, inside its own window when you next have it open:
>   "How did Aura work in World of Warcraft?" It never asks over a game, never
>   takes focus and never asks twice about the same game. You can report a game
>   from the Games tab, and turn the question off there (**Ask how games
>   went**) or with **Don't ask again**.
> - **What a report holds.** The game's name as Aura's list spells it, the
>   Aura version, your operating system and its major version (for example
>   macOS 15, Windows 11, or a Linux distribution such as SteamOS 3), your
>   answer (Works, Some issues or Doesn't work) and the short note if you typed
>   one. It also holds a random report ID made on your computer for game
>   reports only, and a counter, so several reports from one computer count
>   once, the newest answer wins and you can withdraw them. That ID is not your
>   crash-report ID, licence key or machine ID, and Wayfinder stores only a
>   one-way fingerprint of it.
> - **What it never holds.** Audio, dictated text, window titles, other apps
>   or games, your settings, licence key, plan or API keys, or your email
>   address. The note is yours to write: please leave out names and contact
>   details. Links are not accepted.
> - **Where it goes.** Wayfinder's own backend, which runs on Convex. Reports
>   are counted per game, operating system and Aura version line (for example
>   1.3) to set the labels in the Games tab, so a label such as "Works with
>   (community)" means three or more anonymous report IDs, not verified people.
>   Notes are read only by the Wayfinder team on a signed-in review page. They
>   are never shown publicly or posted anywhere else. A weekly summary with
>   counts and label changes, never notes or report IDs, goes to the team's
>   Slack.
> - **Your internet address.** Like any web request, the server sees your IP
>   address. It is used only to limit spam, is kept for about two hours in
>   rate-limit counters and is never stored with a report.
> - **Seeing and withdrawing.** Games > Your reports lists every report this
>   computer sent, and works even when reporting is paused. **Withdraw**
>   deletes that game's reports from Wayfinder's servers. **Withdraw all**
>   deletes them all and makes a new report ID. So that a delayed copy can't
>   bring a withdrawn report back, the server keeps a short record (the ID's
>   fingerprint and the counter, no answer or note) for 30 days. If you delete
>   Aura's settings folder, Aura can no longer show which reports were yours,
>   so withdraw them first.
> - **How long it is kept.** Notes are erased after 90 days. Reports are
>   deleted after 2 years, or as soon as you withdraw them.
> - **Offline.** A report waits on your computer until Aura can send it, for
>   up to 14 days.
> - **On your computer.** To ask only once, Aura counts dictations into
>   supported games and remembers which games it asked about. It keeps that,
>   your report ID, the counter and a copy of what you sent in
>   `game-reports.json` beside `config.json`, readable only by your user
>   account (`0600`).

### 7.2 Other PRIVACY.md edits

- Rename "Window detection (Linux)" to **Game detection** and cover all three
  systems: "Gamer mode (on by default) checks which app is in front each time
  you dictate: on the Mac its bundle ID and name, on Windows its program file
  name and window title, on Linux the X11 window's class, title and
  `STEAM_GAME` property. This stays on your computer. The only traces are an
  activity-log line naming the game and, for supported games, the count
  described under Game reports."
- Under "No analytics, no telemetry", add "Game reports are not telemetry:
  Aura sends one only when you press Send." Then add two network items:
  9. **Game reports, only when you press Send on one**, as described above.
     Withdrawing sends your report ID so the server can delete them.
  10. **Community game labels, at most once a day,** when you open the Games
      tab or Aura is about to ask about a game. The request carries nothing
      about you.

### 7.3 Website copy (Wayfinder OS, same day as the client Beta)

`src/app/aura/_lib/content.ts` (the `leaves` list, which is the website's
complete list of what connects to the internet, and the two network FAQ
answers) and `src/app/llms.txt/route.ts` gain two matching clauses:

- "Game reports, only when you press Send: the game, Aura and system versions,
  your answer and an optional note, with a random report ID you can withdraw";
- "A daily download of community game labels, which carries nothing about
  you".

Wherever the site mentions the community label, it says "3 or more anonymous
reporters", never "players" as if verified. No em dashes;
`npm run validate:marketing-copy` passes.

## 8. Abuse and spam limits

Client:

- One report per Send, at most 5 Sends a day (counted locally), and a new
  answer for a game replaces one still queued.
- The note: 280 characters, control characters stripped, links refused.
- Reports are offered only for catalog games (`reportable`).

Server (details in the server design):

- Public, rate-limited, no CORS, JSON only, 8 KB body cap, strict validation
  before any database work (the `httpHelpers.ts` pattern), and the catalog
  check, so invented keys never create a row.
- Atomic limits written in the same transaction as the report: per reporter
  hash (20 an hour), per ingress address (30 an hour) and global (600 an hour);
  withdrawal has its own per-address and global limits. Windows stay at an
  hour or less, because the shared limiter cleanup deletes older records.
- One row per reporter, game, system and line, so one ID never counts twice;
  complete per-line totals; the 24-hour spread; the weekly digest naming every
  promotion; and **Hide** on the review page.
- Notes are never public and never leave the signed-in review page. Agents that
  list reports through Catalog V2 see only whether a note exists.
- The intake switch (closed by default) stops intake at once on the server,
  and the 24-hour permission lifetime removes report UI from every app within
  a day.

Rejected: storing a network fingerprint of the IP to spot one person with many
IDs. The privacy cost is too high for a games list.

## 9. Retention

| Data | Kept |
| --- | --- |
| Report note | 90 days, then erased; the answer stays. It exists only in the database: no Slack copy, no Catalog V2 copy, never logged. |
| Report (answer, game, versions, system, ID fingerprint, counter) | 2 years, or until withdrawn |
| Per-line totals | Change with the reports they count |
| Withdrawal barrier (fingerprint, game key or all, counter) | 30 days, longer than the app's 14-day send window |
| Label change log | 2 years |
| Weekly Slack digest | Slack's own retention; it holds only counts, game names and label changes |
| Peter's test sessions | Until Peter deletes them |
| IP in rate-limit counters | About 1 to 2 hours (hourly cleanup cron) |
| Local `game-reports.json` | Until withdrawn or deleted; sent entries older than 2 years are pruned |
| Local queue | 14 days, then "Not confirmed" (if attempted) or dropped (if never attempted) |
| Labels cache | Display up to 30 days; intake permission 24 hours |

## 10. Weekly digest

Every Monday the server posts one Slack message to the Aura channel (the
webhook crash reports already use) and exposes the same content as the Catalog
V2 read `platform.aura_games.digest.get`, so Fox Grid can file it to Peter's
Inbox. It holds counts, game names and label changes only, **never notes or
report IDs**, and links to the signed-in `/admin/aura-games` page for detail:

1. Intake state, and reports this week by answer and system.
2. Every label change with its reason ("Valheim, Windows: Playable -> Works
   with (community), 3 anonymous reporters on 1.2"). Dropped Verified games are
   marked **re-test**.
3. Games with new issue reports, as counts ("World of Warcraft, macOS: 2 Some
   issues on 1.3"), each linking to the page.
4. Test sessions recorded this week (game, system, line, result).

A quiet week still gets one line, so a missing post means something broke.

## 11. Failure and offline behaviour

| Situation | Client | Server |
| --- | --- | --- |
| Offline at Send | Queued (never attempted); "Saved. Aura sends it when you're online."; retried at launch and after the next successful labels fetch | n/a |
| Timeout or dropped connection mid-request | "Not confirmed"; retried with the same `reportId` and `seq` | Possibly stored; a retry is a duplicate |
| 200 | Sent, or dropped silently if `withdrawn: true` | Stored, duplicate, stale or behind a barrier |
| 400 / 413 / 415 | "Couldn't send"; not retried | Nothing written |
| 429, 503 (not accepting), 404, 5xx | Kept and retried for up to 14 days | Nothing written |
| Labels fetch fails | Permission expires 24 hours after the last good fetch; labels shown from cache (up to 30 days) or code; **Your reports and Withdraw still work** | Next route answers 503 `no-store`; the CDN serves at most a 20-minute-old copy |
| Labels document invalid or stale (`Date - generatedAt` over 1 hour) | Ignored for permission; old cache kept for display | n/a |
| Withdraw, nothing ever attempted for that game | Deleted locally | n/a |
| Withdraw otherwise | "Withdrawing"; retried at launch; removed on 200 | Barrier written first, rows deleted, idempotent, works while closed |
| Delayed submission after a withdrawal | n/a | Refused by the barrier, nothing written |
| Withdrawal replayed after a newer report | Resends its fixed `throughSeq` | Deletes only `seq <= throughSeq`; the newer report stays |
| App on a pending line (first beta of a new minor, before the catalog PR) | Sends normally; the labels shown come from the newest trusted line not newer than its own | Stored in quarantine; joins the labels within a day of the catalog PR's deploy |
| Unknown version (not trusted, not pending) | "Couldn't send" (400) | Nothing written |
| Labels callback after the player returned to the game | No card; the next visibility event rechecks | n/a |
| Corrupt `game-reports.json` | Renamed `.bad`, start fresh, logged once; never crash | n/a |
| Slack webhook missing | n/a | Logged, skipped |

Every network call runs off the Tk thread, never raises into the UI, and times
out after 10 seconds.

## 12. Licence integrity

Nothing here touches licensing. `game_reports.py` and `game_labels.py` must
not import `wayfinder.license` or `wayfinder.models_cdn`, read `license.json`,
or send the plan. The feature works the same on Free and Ultra. A test parses
both modules' imports, and another fixes the exact payload keys.

## 13. Tests (red first, then green, on the real code paths)

Aura (`pytest`; only the UI construction test is marked `ui`):

- Payload keys exactly the allowed set; no licence, machine ID, crash
  install ID or plan; the import ban.
- Reporter ID: 32 hex characters, persistent, different from
  `crash_reports.install_id()`, file `0600`; `seq` never decreases, including
  across Withdraw all.
- The export's release lines come from `vX.Y.Z` and `vX.Y.Z-pre` tags only,
  with other tags ignored, and the list is sorted and unique (a fixture tag
  list).
- `game_key()` pinning and uniqueness across the three `game_list()`s;
  `scripts/export_game_catalog.py` output is deterministic, covers every
  entry, and fails on a duplicate key or a key with two names.
- Coarse OS for the Mac, Windows (10 and 11 by build) and os-release fixtures.
- `_inject_into_game_chat()` counts a supported game on success, and never
  counts a Not recommended game, an unlisted one or an aborted send (driven
  through the real method with the platform helpers faked).
- Visibility hook: the cold-launch `after()` call reaches
  `_on_main_window_visible()` and does nothing when the launch was minimized;
  `_show_window()`, root `<Map>` and root `<FocusIn>` reach it once per burst; a
  labels callback re-runs it; it never calls focus or raise methods.
- Eligibility at the moment of each run: below 3, already asked, switch off,
  key not reportable, permission expired, game frontmost, app busy. Also **the
  delayed callback**: the fetch completes after the game is frontmost again,
  and no card appears.
- Permission lifetime: 24 hours after the fetch; never extended by a failed
  refresh; a clock moved back counts as expired; `Date - generatedAt` over 1
  hour gives no permission.
- **The fixed boundary:** press Withdraw at counter 9, Send a new report (10),
  then let the pending withdrawal retry. It resends 9, and report 10 is kept
  locally and sent normally.
- States and withdrawal (section 6.1): a never-attempted report is deleted
  locally; one marked Sending (a crash simulated mid-request), Not confirmed,
  Sent or Couldn't send with an earlier attempt goes to the server with
  `throughSeq` equal to the counter; the queue stops retrying that game; offline
  stays Withdrawing; reset only after the server confirms; `withdrawn: true`
  drops silently.
- Your reports and Withdraw are available with intake closed and with the
  labels fetch failing.
- Captions from provenance: Verified names `sourceLine`; a carried line says
  "confirmed on Aura {sourceLine}", even when that line is not among the six.
- Labels: daily cache, schema rejection, line selection (an app older than
  every published line falls back to code, an app between lines takes the
  older one, a newer app takes the newest), merge order, `capped` and
  `complete` captions.
- Note: length, links refused, control characters stripped.
- `PRIVACY.md` names "Game reports" and "Community game labels".
- `tests/test_design_ratchet.py` passes untouched.

Server: see the server design, section 12 (contract, catalog, barrier, the
fixed withdrawal boundary with interleaved retries and continuations, the
six-fabricated-lines regression and quarantine, totals invariant, fold table
including the demotion cases and provenance, projection, digest without notes,
retention, the probe helper and V2).

## 14. Rollout

1. **Aura Part A (small PR, first):** `game_key()` and
   `scripts/export_game_catalog.py` (games and release lines) with their tests.
   No user-visible change, no network.
2. **Server (Wayfinder OS):** the catalog generated from Part A, tables,
   endpoints, totals, fold, barrier, labels route, review page, V2 operations,
   digest and retention. Intake is **closed** when no settings row exists, so
   the deploy changes nothing for anyone.
3. **Try it on dev.** With intake opened on the dev deployment, run a source
   build with the three URL overrides pointed at dev.
4. **Aura Part B (the feature PR), after the server is on production.**
   Released in the next Beta. Until intake opens, the labels document says
   `accepting: false` and no new-report UI appears.
5. **Open intake** (Peter, review page or `platform.aura_games.intake.set`)
   when the Beta is published; merge the website copy (section 7.3) the same
   day.
6. **Watch two digests**, then ship in Stable. If spam or a bug appears, close
   intake: the server refuses at once and every app hides new-report UI within
   about a day, with no app update. Your reports and withdrawal keep working.
7. **Ongoing.** When Aura tags the first release of a new line, or adds games,
   regenerate the server catalog in one Wayfinder OS PR. Until it lands,
   reports on that line wait in quarantine.

## 15. Decisions, and the alternatives turned down

- **Live labels fetch instead of baking labels into releases:** demotions
  reach players without waiting for a release, and the same document carries
  the off switch and the catalog list. The cost is one listed, anonymous,
  daily GET.
- **A server-owned catalog, generated from Aura's lists and tags:** it bounds
  the feed and refuses invented games and versions. Release lines come from
  tags rather than live GitHub calls, so they are reviewed, deterministic and
  need no new outbound dependency. The cost is one regeneration PR when Aura adds
  games.
- **Complete per-line totals instead of reading recent rows:** labels can't be
  promoted on partial evidence.
- **Peter's verdicts on the server, not in `GameProfile.tested`:** one place
  computes the ladder and the digest can say "re-test".
- **Labels per system and per version line**, with no fallback to newer lines.
- **Notes only on the signed-in page:** every copy is reachable by withdrawal
  and erasure.
- **No tray item, no notification:** see 4.1.
- **Only catalog games:** names typed by players or read from windows are a
  privacy and moderation problem. A "Suggest a game" form can come later.

## 16. Decisions on the open questions (root, 2026-10-15; Peter may change them)

1. **The question is on by default.** The card is local, appears once per game
   in Aura's own window, and nothing is sent until Send is pressed. That is how
   AU4 A's "off until the user acts" is met.
2. **The digest goes to the existing Aura Slack channel**, with counts and
   label changes only, never notes, linking to the signed-in
   `/admin/aura-games` page, where notes are visible.
3. **Retention:** notes are erased after 90 days, reports deleted after 2
   years.
