# squad-rcon-cli

Interactive RCON console for **QA-testing Squad RCON updates**.

Connect to a test server, type any RCON command, see the raw reply. It never
hard-codes the command set, so new or changed commands work without a code
change. That is the point when you QA a stream of RCON updates.

Standalone: Python 3.11+ stdlib only. No venv, nothing to install.

## Usage

```bash
# Interactive session (REPL)
python3 squad_rcon_cli.py --host <host> --port <port> --password <pw>

# One-shot: run a single command and exit (good for checklists / capturing output)
python3 squad_rcon_cli.py --host <host> --port <port> --password <pw> --command "ListPlayers"
```

- In the REPL, type a command and press Enter.
- `quit`, `exit`, or Ctrl-D to leave.
- Server push messages (chat, admin camera, kick notices) print as they
  arrive, tagged `[push]`.

## Logging

For QA evidence you usually want a record of what was sent and what came back.

- **`--log FILE`** appends a timestamped NDJSON transcript.
  - One `{"ts", "dir", "data"}` per line.
  - `dir` is `sent`, `recv`, `push` or `error`.
  - Good for attaching to a bug report or diffing across runs.

  ```bash
  python3 squad_rcon_cli.py --host <h> --port <p> --password <pw> --log run.ndjson
  ```

- **`--debug-bytes`** hex-dumps the raw wire traffic to stderr
  (`[bytes ->]` / `[bytes <-]`).
  - Only needed when an update touches the protocol framing itself (packet
    types, multi-packet behavior).
  - The dump includes the auth packet, which contains your password.

  ```bash
  python3 squad_rcon_cli.py ... --debug-bytes 2> wire.log
  ```

To capture the decoded session without flags, run the REPL inside
`script -q session.log`. It records stdout and stderr.

## Command reference

The server is the authoritative source, so nothing is bundled here.

- **`ListCommands 1`** lists every command with its description.
  - Plain `ListCommands` lists names and arguments only.
  - Its header says to use `ListCommands false` for details. That is wrong
    (10.6): `ListCommands 1` is the form that works.
- **`ListPermittedCommands`** needs a connected in-game player, so it returns
  nothing useful over a bare RCON login.

Quoting is the most common mistake. Squad is strict about which arguments are
quoted, and this tool passes your text through unchanged:

- `AdminKick "<NameOrSteamId>" <KickReason>` (id quoted, reason bare)
- `AdminBan "<NameOrEOSId>" "<BanLength>" <BanReason>` (id and length quoted, reason bare)
- `AdminWarn "<NameOrEOSId>" <WarnReason>` (id quoted, reason bare)
- `AdminBroadcast <Message>` (no quotes)

## How to QA properly

1. **Point it at a non-prod test server.** Never QA against a live server.
   An extra connection over `MaxConnectionsFromSameHost` evicts the oldest
   session from your host, which can be the production admin tool (see
   [Connection / session behavior](#connection--session-behavior)).
2. **For each update**, run the command and compare the raw reply against the
   expected output. Cover edge cases: bad args, missing player, empty result,
   unicode names.
3. **For connection-limit / multi-client behavior**, use a dedicated
   multi-connection tester. This tool holds a single connection.
4. **For regression**, save known-good replies for the stable commands (acks,
   errors, "not defined") and re-check them. `ListPlayers` and `ListSquads`
   change every run, so check their structure instead of exact text.

## RCON gotchas (learned from real Squad servers)

This tool shows raw server output and does not parse it, so the parsing traps
below do not affect it. A QA tester still needs them to tell a real regression
from Squad's normal behavior.

Facts marked **(10.6)** were checked on Squad v10.6.0.685998.3133 against
recordings of a server with up to 100 players.

### Changed in Squad 10.6

| Area | Before 10.6 | Now (10.6) |
|---|---|---|
| `ShowServerInfo` player and queue counts | Cached server-browser data, 20-30 s old (earlier note) | Current: matches `ListPlayers` within 1 player in 97% of samples |
| `ShowServerInfo` `TeamOne_s` / `TeamTwo_s` | Map-prefixed names | Faction setup names (`USA_LO_CombinedArms`); empty, then missing, during a map change |
| `ShowServerInfo` `NextLayer_s` | Used by SquadJS core until 2026-10 | Not reliable; use `ShowNextMap` |
| `ListPlayers` lines | No party or vehicle fields | `Party ID` after `Team ID`, `Vehicle` after `Role` |
| `ListSquads` team lines | `Team ID: N (<name>)` | Ends in ` - Tickets: N` |
| Player display names | No leading spaces | Can start with 1-3 spaces (game bug) |

Details for each row are in the sections below.

### Wire protocol

The tool handles all of this internally. It is listed so you know what is
normal.

- **Squad RCON is UE4 RCON, not Valve Source RCON.**
  - Server push messages use packet type `0x01`.
  - Replies to your commands use type `0x00`.
- **Multi-packet replies.** A command reply can span several packets.
  - The client marks the end by sending an empty sentinel command with its own
    id.
  - The reply is complete when the empty reply to the sentinel comes back.
- **Each empty end packet is followed by a 7-byte blob,
  `00 01 00 00 00 00 00`.**
  - Identify the blob by its exact bytes, never by size alone. Short push
    messages exist.
  - The blob can arrive in a later TCP read than the packet before it. An
    empty packet cannot be classified until the next 7 bytes are buffered.
  - If a client consumes the empty packet too early, the blob stays at the
    head of the buffer and reads as a garbage size field. Framing is then
    wrong for the rest of the connection: the session stops receiving replies
    while writes still succeed (seen live).
  - This tool waits for those 7 bytes before it consumes an empty packet.
- **Packets split in the middle of a line.**
  - Join the packet bodies with nothing between them, then split into lines.
  - In the live captures, 17 data packets of `ListPlayers` replies ended
    mid-line.
  - A client that joins with a separator (SquadJS used `,` until 2026-10)
    corrupts the line at each packet boundary.
- **Request ids are echoed exactly.**
  - Data packets carry the id of the command.
  - The two empty packets carry the id of the empty sentinel command.
  - Verified with plain 32-bit ids. Route replies by id; do not compute the
    owner of a reply from an offset.
- **Large replies span multiple TCP reads.** Buffer until a full packet
  decodes (8 KB read chunks).
- **The 14..4096 packet-size limit applies only to packets the server
  receives.**
  - The server splits its own reply chunks at about 4096 *characters*. With
    multibyte UTF-8 player names the size field is larger in bytes (4149
    observed live).
  - A client that caps inbound packets at 4096 drops every large
    `ListPlayers` reply.
  - Real chunks stay under about 16 KB (4096 characters at 4 bytes each). A
    garbage size field from misframed text reads as hundreds of millions. A
    64 KB cap separates the two.

### Connection / session behavior

- **`MaxConnectionsFromSameHost` (RCON.cfg) is a per-host sliding window
  (FIFO).**
  - A new connection over the limit evicts the *oldest* session from that
    host, one at a time until it fits. The new connection succeeds.
  - It does not drop all sessions, and it does not reject the new one.
  - Example with limit 2: conn 1 ok, conn 2 ok, conn 3 ok + drops conn 1,
    conn 4 ok + drops conn 2.
  - Clients that reconnect automatically (SquadJS) turn this into a reconnect
    loop: the evicted session reconnects and evicts the next oldest. This is
    the real reason to run a single shared RCON session.
- **A wrong password gets no reply (10.6).**
  - The server sends no packet and closes the connection about 250 ms after
    the auth packet.
  - It does not send an `AUTH_RESPONSE` with id `-1` (Valve Source RCON
    does).
  - Nothing is written to `LogRCONServer`, and a correct login right after
    works (no lockout).
  - This tool reports the close during login as `Auth failed`.
- **Concurrent commands are answered in send order (10.6).**
  - The server never interleaves the packets of two replies, so several
    commands can be in flight on one connection.
  - Verified live: 40 commands in one write, with multi-packet `ListPlayers`
    replies, all answered in order.
  - This tool matches replies by id, so it does not depend on the order.
- **Reply times are short, so a timeout is safe.**
  - 134,461 recorded replies, about 100 players: p99 83 ms, p99.9 234 ms,
    maximum 2.6 s (`AdminWarn`), none over 3 s.
  - A 10 s command timeout never fires on a healthy session.
  - Without a timeout, one missing reply leaves the command waiting forever.
    A client that pairs replies by order then gives every later reply to the
    wrong command.
- **RCON refuses connections while the game server restarts.**
  - Expect `ECONNREFUSED` for about 10 s, for example at a scheduled daily
    restart.
  - A reconnecting client must treat a failed attempt as "try again later",
    not as a fatal error.

### `ShowServerInfo`

- **Counts are current (10.6).**
  - `PlayerCount_I` was compared with the `ListPlayers` count taken within
    1 s: equal in 78% of 1,434 samples, within 1 in 97%. The small difference
    is players still joining.
  - In a 1 s poll, `PublicQueue_I` went 0, 1, 0 within 9 s.
  - An earlier note (before 10.6) described a cached server-browser blob
    20-30 s old. That no longer matches.
  - Still confirm a kick or team change with `ListPlayers` / `ListSquads`,
    because they show which player changed.
- **Map change sequence (10.6).**
  1. `MapName_s` switches when loading starts.
  2. `TeamOne_s` / `TeamTwo_s` become `""`.
  3. They disappear from the reply for about 18 s.
  4. They return with the new faction setup names (`USA_LO_CombinedArms`).
- **`NextLayer_s` is not reliable (10.6).**
  - It is a display name (`Al Basrah Seed v1`), not a layer id.
  - It can keep the old next layer while `ShowNextMap` says
    `Next map is not defined`.
  - Some layers omit it. Use `ShowNextMap`.
- **Field types are mixed.**
  - `_I` keys are quoted strings: `"PlayerCount_I": "42"`.
  - `MaxPlayers` and `MatchTimeout_d` are numbers.
  - `_b` keys are booleans.

### `ShowNextMap` / `ShowCurrentMap`

- **No next map:** `Next map is not defined`, not an empty reply.
- **Next map set:** `Next level is <map>, layer is <layer>, factions <f1> <f2>`.
- **`ShowCurrentMap`** has the same shape: `Current level is ...`.
- **Faction tokens** like `RGF+Support` are single space-delimited tokens.
  The unit part is optional (`factions PLANMC ADF`).

### `ListPlayers` / `ListSquads`

- **10.6 added fields to `ListPlayers`.**
  - `| Party ID: ...` after the team.
  - `| Vehicle: <name> (<seat>)` or `| Vehicle: N/A` after the role, for
    example `Vehicle: BAF_LPPV (Driver)`.
  - Parsers written for older versions match none of these lines, because
    `Party ID` sits between `Team ID` and `Squad ID`.
- **10.6 `ListSquads` team lines end in ` - Tickets: N`**, for example
  `Team ID: 1 (3rd Division Battle Group) - Tickets: 119`.
- **`N/A`** shows up for squad or team when a player is unassigned.
- **Squad IDs can have gaps.** The gaps are real, not a bug.
- **No trailing comma on the wire.**
  - Older notes say role fields can end in `,`.
  - 270 player lines in the raw captures, none ends in a comma.
  - The comma came from SquadJS joining packets with `,` (see
    [Wire protocol](#wire-protocol)).

### Player names

- **Names are freeform UTF-8.** They can contain pipes (`|`) and other
  characters used as delimiters. Do not assume a name is ASCII or pipe-free.
- **Names can contain spaces anywhere.** A captured name was `B V B`, so
  "take the token after `Player`" is wrong.
- **Display names can start with spaces (10.6, game bug).**
  - The game joins the clan tag and the name with a space, also when the tag
    is empty. Untagged players get a leading space; some names get two or
    three (`Remote admin has warned player   THE CON.`).
  - Seen in `ListPlayers`, `ListParties` and warn replies.
  - `ListSquads` creator names do not have it.
  - Do not trim names in one place only. Lookups that compare names across
    commands then stop matching.

### Push messages and player IDs

Squad uses three different wrappers for player IDs, with inconsistent case:

| Message | ID wrapper |
|---|---|
| Chat (`[ChatAll]`, `[ChatTeam]`, ...) | `[Online IDs:EOS: ... steam: ...]` |
| Admin camera, possessed | `[Online Ids:EOS: ... steam: ...]` (lowercase `ds`) |
| Admin camera, unpossessed | `[Online IDs:EOS: ... steam: ...]` |
| Kick (`Kicked player N.`) | `[Online IDs= EOS: ... steam: ...]` |
| Squad created | `(Online IDs: EOS: ... steam: ...)` |

If a new or changed message flips the case or the delimiter, parsers break.

**Messages with names only, no IDs:**

- `Remote admin has warned player <name>. Message was "..."`
- `Remote admin disbanded squad N on team N, named "..."`
- `[ChatAdmin] ASQKillDeathRuleset : Player <attacker> Team Killed Player <victim>`

They confirm that an action happened. They are not a source of stable
identity: names are not unique and can change.

**Team kills: RCON push vs log.**

- The RCON push is simpler to detect than the log path, which joins
  `Wound()` / `Die()` `LogSquadTrace` lines.
- But the log `Die()` line carries the attacker's EOS ID, and the push does
  not. Moving TK detection to RCON loses that ID.
- For QA: RCON shows that a TK happened. Confirm identity-dependent behavior
  another way.

### Ack / error strings (assertion targets)

**Admin command replies (10.6).** These commands answer with a confirmation,
not `Success`:

| Command | Reply |
|---|---|
| `AdminWarn` | `Remote admin has warned player <name>. Message was "<text>"` |
| `AdminKick` | `<name> was kicked: <reason>`, then `Kicked player N. [Online IDs= ...] <name>` |
| `AdminBroadcast` | `Message broadcasted <<text>>` |
| `AdminDisbandSquad` | `Remote admin disbanded squad N on team N, named "..."` |
| `AdminReloadServerConfig` | `Reloading server config...` |

**Other known replies:**

- `Success`: generic admin-command ack (not seen for the commands above on
  10.6)
- `ERROR: Invalid player id`
- `ERROR: Unable to find player with name or id (<id>)`
- `Could not find player <id>`
- `Error: Indexed Squad not found`
- `Next map is not defined`: `ShowNextMap` when unset (also seen when map
  voting is enabled)

### Client pitfalls seen in real clients

Each of these was found in a client used in production (SquadJS, or an RCON
module used in place of its own). A new client should test for them:

- A wrong password resolved as a successful login, so the client never
  reconnected.
- One `error` listener added to the socket per pending command. This leaks
  listeners under load (`MaxListenersExceededWarning`).
- Commands sent without awaiting the result. Any connection drop became an
  unhandled rejection and ended the process.
- A failed reconnect attempt that was not caught, which also ended the
  process.
- Reply routing by `id - 2`. On Squad 10.6 this gives each reply to the
  command sent two earlier.

## Self-test

```bash
python3 test_squad_rcon_cli.py
```

Checks the packet codec without a server: round-trip, unicode, follow-response
blob, partial and multi-packet buffering, oversized reply chunks, garbage size
fields.

## License

MIT, see [LICENSE](LICENSE).
