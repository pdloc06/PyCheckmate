# Plan: Running PyCheckmate as a Lichess Bot

This is the roadmap for taking the engine in this repository online as a
`BOT`-flagged account on lichess.org. Steps 1–2 are already implemented in
code; the rest is operational work.

---

## How Lichess bots work (background)

Lichess exposes a **Bot API** (a subset of the Board API): your program logs
in with a personal API token, listens to an event stream over HTTPS
long-polling, accepts challenges, and posts moves in **UCI coordinate
notation** (`e2e4`, `e7e8q`). You never implement the protocol by hand in
practice — the official **`lichess-bot`** bridge (github.com/lichess-bot-devs/lichess-bot)
does all of it and simply talks to your engine over the **UCI protocol**
(stdin/stdout text commands).

So the integration chain is:

```
Lichess servers  <—HTTPS/JSON—>  lichess-bot (Python bridge)  <—UCI text—>  engine/uci.py  —>  engine/move_finder.py + engine/chess_engine.py
```

## Step 1 — Engine prerequisites (DONE in this repo)

The engine must be able to reconstruct any position the server describes and
speak UCI move notation:

- `GameState.from_fen()` / `to_fen()` — positions arrive as FEN (`engine/chess_engine.py`)
- `Move.get_uci_notation()` — moves leave as UCI strings (`engine/chess_engine.py`)
- `Move.from_ai_tuple()` — converts search output to full moves (`engine/chess_engine.py`)
- Zobrist keys + repetition awareness in search (`engine/move_finder.py`) so the bot
  doesn't blunder into (or miss) threefold repetition draws online

## Step 2 — UCI adapter (DONE: `engine/uci.py`)

`engine/uci.py` implements the minimal command set lichess-bot needs:
`uci`, `isready`, `ucinewgame`, `position startpos|fen ... moves ...`,
`go depth/movetime`, `quit`.

Verify locally:

```
$ python -m engine.uci
uci
position startpos moves e2e4 e7e5
go depth 4
bestmove g1f3
quit
```

## Step 3 — Create the bot account

1. Register a **fresh** Lichess account (an account that has ever played a
   rated human game cannot be converted to a bot).
2. Create a personal API token at lichess.org/account/oauth/token with the
   `bot:play` scope. Store it in an environment variable — never commit it.
3. Upgrade the account to a bot (one-time, irreversible):
   `curl -d '' https://lichess.org/api/bot/account/upgrade -H "Authorization: Bearer <TOKEN>"`

## Step 4 — Wire up lichess-bot

1. Clone `https://github.com/lichess-bot-devs/lichess-bot`, install its
   requirements in a separate venv.
2. In its `config.yml`:
   - `token`: your `bot:play` token (or use the env-var indirection).
   - `engine.dir`: path to this repo; `engine.name`: a small launcher script
     (see below); `engine.protocol: uci`.
   - `challenge`: start restrictive — accept only `casual`, variants
     `standard`, time controls `rapid`/`classical` while testing.
3. Launcher script (`engine.sh`, committed here as a template):
   `#!/bin/sh` + `cd /path/to/repo && exec uv run --no-project -p pypy3.11 python -m engine.uci`
   (lichess-bot expects an executable, not a Python module).
4. Run `python lichess-bot.py -v`, then challenge your bot from your own
   account and play a game.

## Step 5 — Time management (DONE: `parse_go_limits` in `engine/uci.py`)

`handle_go` now parses the clock fields (`wtime`, `btime`, `winc`, `binc`)
that Lichess sends on every `go`:

- budget per move = `remaining_ms / 30 + increment_ms * 0.8` (the side to
  move's clock), clamped to `[0.05s, 20s]`, passed as `time_limit` to
  `find_best_move`
- with a clock-derived budget the depth cap is lifted (`CLOCK_MAX_DEPTH`) so
  iterative deepening's timer (`SearchTimeout`) is what ends the search
- explicit `go depth N` / `go movetime MS` still take precedence
- covered by `tests/test_uci.py`

## Step 6 — Strength & robustness hardening

In rough order of value per effort:

1. **Opening book** (DONE, bridge-side config only): lichess-bot reads a
   polyglot `.bin` book — `engines/komodo.bin` from the donna_opening_books
   repo, enabled under `engine.polyglot` in `config.yml`. Zero engine work.
2. **Search speed**: Python is the ceiling. Cheap wins first: run under
   `pypy3` — DONE: `uv python install pypy3.11` provides the interpreter,
   `uv run --no-project -p pypy3.11 python -m engine.uci` hosts the engine under it, and
   the GUI auto-uses it through `engine/uci_client.py` (measured ~2x on `engine/bench.py`;
   the gain grows with longer time controls as the JIT stays warm). Point
   lichess-bot's engine command at the PyPy invocation above.
   Captures-only quiescence generator: DONE —
   `get_valid_moves(for_ai=True, captures_only=True)` never materializes
   quiet moves at quiescence nodes (the bulk of the tree), roughly halving
   search time to a fixed depth (`bench.py` depth 6: 4.9s → 2.4s CPython).
3. **Persistent transposition table** (DONE): `find_best_move(..., tt=...)`
   accepts a caller-held table; `engine/uci.py` keeps one per game in
   `transposition_table`, clears it on `ucinewgame`, and caps its size.
4. **Eval improvements** (DONE): passed-pawn bonus, king-safety pawn shield,
   and bishop pair added to `evaluate()`.
5. **Endgame draw handling** (DONE): `_insufficient_material` scores dead
   material (K vs K, lone minor, KNN vs K) as an exact draw; lichess-bot's
   `offer_draw` config hooks act on the resulting scores.

## Step 7 — Deployment (macOS / Apple Silicon)

The bot only needs outbound HTTPS, so any always-on box works (a Raspberry Pi,
a $5 VPS, a spare laptop). This project is deployed on a **MacBook Air (M2)**
that travels, so the recipe below is macOS-native and **manually controlled**:
a small `bot` control script plus **`caffeinate`** to keep the laptop awake
while it runs.

### The laptop-sleep problem

A laptop's real risk isn't crashes — lichess-bot auto-reconnects on network
drops — it's **sleep**. The screen saver, display sleep, and screen lock are
all harmless: background processes keep running and the connection stays up.
Only *full system sleep* suspends the process and drops the game (an
in-progress game will then flag or abort).

The fix is `caffeinate -s`, which asserts "don't system-sleep" **only while the
wrapped process runs**, and **only on AC power**. So: keep the Mac **plugged
in** (lid may stay open; the screen is free to sleep). Nothing persistent is
changed — unlike `sudo pmset -c disablesleep 1`, there is no global setting to
remember to undo. When the bot stops, the Mac sleeps normally again.

### Run it with the `bot` script

Deployment used to be a launchd LaunchAgent (`RunAtLoad` + `KeepAlive`), but
that design fought the way this bot is actually hosted — on a laptop that gets
carried around. Auto-start at login opened the event stream on whatever Wi-Fi
the laptop happened to join, and `KeepAlive` relaunched a crashing bot in a
tight loop, each relaunch re-opening the stream (see the rate-limit section
below for why that hurts). A bot on a portable machine wants **manual, explicit
control**, so launchd was retired in favor of a plain control script.

`bot` in this repo is that script — copy it into the lichess-bot clone (it
resolves all paths relative to its own location), `chmod +x` it, and optionally
symlink it onto your PATH:

```
cp bot /PATH/TO/lichess-bot/bot && chmod +x /PATH/TO/lichess-bot/bot
ln -sf /PATH/TO/lichess-bot/bot ~/.local/bin/bot
```

Four subcommands, pidfile-based (`bot.pid`), all logging to `bot.log`:

```
bot up       # start (wrapped in caffeinate -s), refuses if already running
bot down     # stop, then sweep every process from the bot's venv
bot status   # running/stopped, process count, recent rate-limit lines, log tail
bot log      # tail -f bot.log
```

`bot up` wraps the bot in `caffeinate -s` (the sleep fix above) via `nohup`, so
it survives closing the terminal. There is deliberately **no auto-restart**: a
crashed bot stays down until you say otherwise, which is exactly the behavior
that keeps the rate limiter happy. lichess-bot reconnects through ordinary
network drops on its own; process-level crashes are rare enough to handle by
hand. Stopping is the whole cleanup — `caffeinate` dies with the bot and the
Mac resumes normal sleep, no `pmset`/`sudo` settings to undo.

The critical part is the **sweep** in `bot down` (and defensively in `bot up`):
it doesn't just kill the main pid, it `pkill`s everything running the clone's
`.venv` python. Why that matters is the next section's war story.

### Auto-challenging other bots (matchmaking)

You don't hunt for opponents by hand — lichess-bot's built-in matchmaking does
it. Set `matchmaking.allow_matchmaking: true` in `config.yml` and the bridge
pulls the live online-bot list from Lichess's `/api/bot/online` API, filters by
rating/variant suitability, and (after being idle `challenge_timeout` minutes)
challenges one at random with a time control drawn from `challenge_initial_time`
/ `challenge_increment`. Set `challenge_mode` to `casual`/`rated`, tune
`opponent_rating_difference` for opponent strength, and set
`challenge.accept_bot: true` so bots can challenge back too.

### The rate-limit trap: orphaned children, not restarts

Lichess protects `/api/stream/event` (the connection the bot holds open to
receive challenges and game events) with an **anti-polling rate limit**: open
that stream too many times in a short window and Lichess returns 429s, and per
the [official API tips](https://lichess.org/page/api-tips) the only cure is to
*wait a full minute* before touching the API again. Symptoms of tripping it:
the bot shows online but "accepts challenges without playing" (the stream is
down exactly when a game needs its first move), and the log fills with
`RateLimitedError: /api/stream/event is rate-limited`.

The trap's real mechanism took a while to diagnose. Restart bursts were the
first suspect, but the actual culprit was **orphaned child processes**:
lichess-bot runs its event-stream watcher (`watch_control_stream`) as a
`multiprocessing.Process`. If the main process is killed without a clean
shutdown, that child survives, reparented to PID 1 — invisible unless you go
looking with `ps` — and its reconnect loop keeps re-opening `/api/stream/event`
*forever*. From Lichess's side the token never went quiet, so the 429 penalty
renewed no matter how long the human "waited". (The observed worst case: six
orphans quietly hammering the API for 16 hours while every fresh start of the
bot mysteriously hit 429s within seconds.)

That is why `bot down`'s sweep kills **every** process from the clone's
`.venv`, not just the pidfile pid — and why `bot up` runs the same sweep first
(then waits 60 s, honoring the official cooldown) if it finds strays.

Two smaller mitigations live in the lichess-bot clone as local patches:

- `watch_control_stream` catches `RateLimitedError` and sleeps out the *whole*
  remaining cooldown instead of retrying every second — one reconnect per
  cooldown instead of sixty tracebacks a minute in the log.
- `handle_challenge` applies exponential backoff (60 s → 600 s cap) to 429s on
  the challenge endpoint.

**Day-to-day rule:** config changes still deserve a single `bot down` /
`bot up` cycle, not a burst of them — every stream re-open counts against the
anti-polling window. If `bot status` shows rate-limit lines two minutes after
a start, run `bot down`, confirm with `bot status` that **zero** processes
remain (that's the lesson), wait a couple of minutes, and `bot up` once.

### Learn from the games

Log games (lichess-bot writes PGNs) and skim losses for blunders — feed
concrete positions back into `tests/` as regression FENs via
`GameState.from_fen`.

## Testing pipeline (before going online)

1. `pytest tests/ -q` — engine correctness (perft, zobrist, search sanity).
2. Self-play smoke: `uv run --no-project python -m engine.selfplay` — spawns two
   `engine.uci` processes, plays 20 full games refereed by a `GameState`, and
   exits non-zero on any illegal move or engine crash. Exercises the whole UCI +
   search + make/unmake stack over complete games (the integration coverage
   perft can't give).
3. `lichess-bot` in casual-only mode vs. your own human account.
4. Only then open the challenge gate to the public and rated games.
