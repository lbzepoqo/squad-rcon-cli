# squad-rcon-cli

Interactive RCON console for **QA-testing Squad RCON updates**.

Connect to a test server, type any RCON command, see the raw reply. It never
hard-codes the command set, so it works for new or changed commands without any
code change — which is the point when QA'ing a stream of incoming RCON updates.

Standalone: Python 3.11+ stdlib only. No venv, nothing to install.

## Usage

```bash
# Interactive session (REPL)
python3 squad_rcon_cli.py --host <host> --port <port> --password <pw>

# One-shot: run a single command and exit (good for checklists / capturing output)
python3 squad_rcon_cli.py --host <host> --port <port> --password <pw> --command "ListPlayers"
```

In the REPL: type a command, press Enter. `quit`, `exit`, or Ctrl-D to leave.
Unsolicited server push messages (chat, admin-cam, kick/ban notices) print as
they arrive, tagged `[push]`.

## Logging

For QA evidence you usually want a record of what was sent and what came back.

- **`--log FILE`** appends a timestamped NDJSON transcript: one
  `{"ts", "dir", "data"}` per line, where `dir` is `sent` / `recv` / `push` /
  `error`. Good for attaching to a bug report or diffing across runs.

  ```bash
  python3 squad_rcon_cli.py --host <h> --port <p> --password <pw> --log run.ndjson
  ```

- **`--debug-bytes`** hex-dumps the raw wire traffic to stderr (`[bytes ->]` /
  `[bytes <-]`). Only needed when an update touches the protocol *framing*
  itself (packet types, multi-packet behavior). Note: the dump includes the
  auth packet, which contains your password.

  ```bash
  python3 squad_rcon_cli.py ... --debug-bytes 2> wire.log
  ```

If you just want the decoded session captured with no flags, wrap the whole REPL
in the shell's own `script -q session.log` — it records stdout and stderr.

## Command reference

For the list of commands the server supports, type `ListCommands` in the REPL
against the real server. (`ListPermittedCommands` needs a connected in-game
player, so it returns nothing useful over a bare RCON login.) The server is the
authoritative source, so there's nothing to bundle and keep in sync.

Quoting is the #1 footgun: Squad is strict about which arguments are quoted.

- `AdminKick "<NameOrSteamId>" <KickReason>` (id quoted, reason bare)
- `AdminBan "<NameOrEOSId>" "<BanLength>" <BanReason>` (id and length quoted, reason bare)
- `AdminWarn "<NameOrEOSId>" <WarnReason>` (id quoted, reason bare)
- `AdminBroadcast <Message>` (no quotes)

This tool passes your text through unchanged, so the quoting is on you.

## How to QA properly

1. **Point it at a non-prod test server.** Never QA against a live server —
   some RCON behavior (connection limits) drops *all* sessions for that host.
2. **For each update**, run the command and compare the raw reply against the
   expected output. Cover edge cases: bad args, missing/non-existent player,
   empty result, unicode names.
3. **For the connection-limit / multi-client behavior** (multiple admin tools
   from one host fighting for RCON slots), use a dedicated multi-connection
   tester; this tool holds a single connection.
4. **For regression** as features stabilize, save known-good command/response
   pairs for the *stable* replies (acks, errors, "not defined") and re-check
   them. Volatile replies (ListPlayers/ListSquads) change every run, so
   parse-check their structure instead of comparing exact text.

## RCON gotchas (learned from real Squad servers)

This tool shows **raw** server output and does not parse it, so it is immune to
the parsing traps below, but a QA tester needs to know them to tell a real
regression from Squad's normal weirdness.

### Wire protocol (handled internally — listed so you know what's normal)

- **Squad RCON is UE4 RCON, not Valve Source RCON.** Unsolicited server push
  messages use packet type `0x01`; replies to your command use type `0x00`.
- **Empty end-of-response packets carry a 7-byte follow-response blob.** Must be
  consumed or the byte stream desyncs.
- **Multi-packet responses.** A command reply can span several packets; the end
  is signalled by sending an empty sentinel packet with the same id and reading
  until the empty reply comes back.
- **Large responses span multiple TCP reads.** Buffer until a full packet
  decodes (8 KB read chunks).

### Response content quirks (what to eyeball when verifying an update)

- **`Ids` vs `IDs` — Squad is inconsistent** (confirmed in live captures).
  Possessed admin-camera push uses mixed-case `Online Ids:`; unpossessed uses
  `Online IDs:`. Both appear in the same session. If a new/changed command flips
  the case, downstream parsers break.
- **`:` vs `=` delimiter — also inconsistent.** Chat pushes use
  `[Online IDs:...]`; kick/ban pushes use `[Online IDs=...]`.
- **Player names are freeform UTF-8** and can contain pipes (`|`) and the same
  characters used as delimiters. Don't assume a name is ASCII or pipe-free.
- **Trailing comma** appears on some role fields.
- **`N/A`** shows up for squad/team when a player is unassigned.
- **ShowNextMap returns the literal string `not defined`** (not an empty reply)
  when no next map is set.
- **Faction tokens like `RGF+Support`** are single whitespace-delimited tokens.
- **Squad ID gaps**: squad numbering can be non-contiguous; gaps are real, not a
  bug.

### Connection / session behavior

- **`MaxConnectionsFromSameHost` (RCON.cfg) is a per-host fixed-size sliding
  window (FIFO).** A new connection over the limit evicts the *oldest* session
  from that host (one at a time, until it fits) and itself succeeds — it does
  NOT drop all sessions, nor does it reject the new one. Example with limit 2:
  conn 1 ok, conn 2 ok, conn 3 ok + drops conn 1, conn 4 ok + drops conn 2.
  Clients that auto-reconnect (SquadJS) turn this into a reconnect storm: the
  evicted oldest reconnects, which evicts the next oldest, and so on — the real
  reason to run a single shared RCON session. Test with a dedicated
  multi-connection tester (this tool holds a single connection).
- **Failed auth replies with packet id `-1`.**
- **Commands must be serialized.** Concurrent commands race because replies are
  matched by id; send one at a time.

### Team kill / combat events: RCON push vs log parsing

A team kill surfaces on RCON as a single push line:

```
[ChatAdmin] ASQKillDeathRuleset : Player <attacker> Team Killed Player <victim>
```

Detection-wise this is simpler than the log path (which stitches together
`Wound()` / `Die()` `LogSquadTrace` lines). But the push carries **names only,
no EOS/steam IDs** — unlike the chat pushes beside it, which do include
`[Online IDs:...]`. So it does not solve identity:

- Names are non-unique and mutable; you still need a name -> live-state resolver
  to get a stable ID.
- The log `Die()` line actually carries the *attacker's* EOS ID, which the RCON
  push drops. Moving TK to RCON would lose the one ID the log gave you.
- The names themselves resist parsing: a captured victim name was `B V B`
  (embedded spaces), so "take the token after `Player`" is wrong.

Takeaway for QA: RCON is fine for *observing* that a TK happened; it is not a
source of player identity. Confirm identity-dependent behavior another way.

### Observable-but-name-only events (good for QA, useless for keying by ID)

Many admin side-effects show up on RCON as confirmation pushes that carry the
player **name only**:

- `Remote admin has warned player <name>. Message was "..."`
- `<name> was kicked: <reason>`
- `Remote admin disbanded squad N on team N, named "..."`

Useful to confirm an action fired; not a source of stable identity.

### Ack / error strings (assertion targets)

Known success/failure replies worth asserting against:

- `Success` — generic admin-command ack
- `ERROR: Invalid player id`
- `ERROR: Unable to find player with name or id (<id>)`
- `Could not find player <id>`
- `Error: Indexed Squad not found`
- `Next map is not defined` — ShowNextMap when unset (also seen when map
  voting is enabled)

## Self-test

```bash
python3 test_squad_rcon_cli.py
```

Checks the packet codec (round-trip, unicode, follow-response blob, partial and
multi-packet buffering) — no server needed.

## License

MIT — see [LICENSE](LICENSE).
