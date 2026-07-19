"""
Analyse every finished bot game with Stockfish, automatically, as they arrive.

The manual workflow was: run the bot for a day, stop it, then run
`engine.sf_review` over the whole record directory. That wastes the bot's own
idle time -- between games the machine is doing nothing -- and it means the
analysis only exists when someone is around to start it.

This daemon closes that gap. It watches the record directory, and whenever a
game finishes *and the bot is not currently playing*, it grades that one game
with Stockfish and appends the result to a JSON Lines file outside the repo.
Over a multi-day unattended run the analysis therefore keeps pace with the
games, and the data is waiting rather than needing hours of catch-up.

Two design points matter for an unattended run:

- **It never competes with a live game.** A game in progress means lichess-bot
  has our engine spawned as a subprocess; the watcher waits for that to clear
  before starting Stockfish, and Stockfish runs at low priority besides. The
  whole reason `sf_review` carries a "do not run while the bot is playing"
  warning is that CPU contention degrades the games being recorded.
- **It is resumable and idempotent.** Which games are done is derived from the
  output file itself, so a crash, a reboot, or a second copy of the process
  costs at most one repeated game. There is no separate state file to fall out
  of sync.

What it stores, per game: every move's centipawn loss, accuracy, and the
seconds actually spent on it (Lichess writes `%clk` into the PGN, so the clock
is free), plus the headers. That is deliberately raw -- ACPL, the
inaccuracy/mistake/blunder ladder, accuracy%, per-phase breakdowns and
"did we spend time where we went wrong" are all recoverable from it later
without re-running a single search.

    PYTHONPATH=. uv run --no-project python -m engine.sf_watch
    ... -m engine.sf_watch --once          # drain the backlog and exit
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from engine import pgn
from engine.chess_engine import GameState
from engine.sf_review import (
    BLUNDER, DEFAULT_RECORDS, INACCURACY, MAX_CPL, MISTAKE, OUR_NAME,
    SF_HASH_MB, SF_THREADS, find_stockfish, move_accuracy, white_pov,
    win_percent,
)
from engine.uci_client import EngineClientError, UciEngineClient

# Deliberately outside the repo: this is generated data about a running
# deployment, not source, and it grows without bound.
DEFAULT_OUTPUT = os.path.expanduser(
    '~/.local/share/pycheckmate/game_analysis.jsonl')
DEFAULT_LOG = os.path.expanduser('~/.local/share/pycheckmate/sf_watch.log')

# Depth 14 rather than sf_review's 16: this runs opportunistically between
# games, and finishing a game's analysis before the next one starts matters
# more here than the last increment of precision. Validated against Lichess's
# own published numbers on two games (10.9/46.9 vs 13/49, 17.6/5.2 vs 20/6).
DEFAULT_DEPTH = 14

POLL_SECONDS = 30

# A live game means lichess-bot has spawned our engine. Matching the module
# path is specific enough not to catch this watcher or an editor.
ENGINE_PROCESS_PATTERN = 'engine.uci'

# Wait this long after the bot goes idle before starting: a new game usually
# follows quickly, and being half-way through a Stockfish search when it does
# is exactly what we are avoiding.
IDLE_GRACE_SECONDS = 20

CLOCK_PATTERN = re.compile(r'\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]')


def log(message: str, log_path: str) -> None:
    """
    Append one timestamped line to the watcher's log and to stdout.

    Parameters
    ----------
    message : str
        Text to record.
    log_path : str
        File to append to.

    Returns
    -------
    None
    """
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}'
    print(line, flush=True)
    with open(log_path, 'a', encoding='utf-8') as handle:
        handle.write(line + '\n')


def bot_is_playing() -> bool:
    """
    Report whether lichess-bot currently has a game in progress.

    Returns
    -------
    bool
        True when an engine subprocess is alive, which is the cheapest
        reliable proxy for "a game is being played right now".
    """
    try:
        found = subprocess.run(['pgrep', '-f', ENGINE_PROCESS_PATTERN],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        # Cannot tell -- assume busy. Delaying analysis is harmless; running
        # it during a live game is not.
        return True
    # pgrep matches this process too when it was started with the module path,
    # so discount our own pid.
    pids = {line for line in found.stdout.split() if line != str(os.getpid())}
    return bool(pids)


def already_done(output_path: str) -> set[str]:
    """
    Read back which games have already been analysed.

    Deriving this from the output file rather than a side-car state file is
    what makes the watcher safely restartable: there is nothing that can
    disagree with the data.

    Parameters
    ----------
    output_path : str
        Path to the JSON Lines output.

    Returns
    -------
    set of str
        Basenames of games already present.
    """
    done: set[str] = set()
    if not os.path.exists(output_path):
        return done
    with open(output_path, encoding='utf-8') as handle:
        for line in handle:
            try:
                done.add(json.loads(line)['file'])
            except (json.JSONDecodeError, KeyError):
                continue  # A torn last line from a kill; ignore it.
    return done


def parse_clocks(text: str) -> list[float]:
    """
    Extract the clock reading after each move from a Lichess PGN.

    Parameters
    ----------
    text : str
        Full PGN text.

    Returns
    -------
    list of float
        Seconds remaining after each half-move, in order.
    """
    return [int(h) * 3600 + int(m) * 60 + float(s)
            for h, m, s in CLOCK_PATTERN.findall(text)]


def seconds_spent(clocks: list[float], index: int, increment: float) -> float:
    """
    Work out how long one move actually took.

    A clock reading is what remained *after* the move, so the time spent is
    the drop from the same player's previous reading, with the increment
    (which was added on completion) taken back off.

    Parameters
    ----------
    clocks : list of float
        Per-half-move clock readings.
    index : int
        Half-move index.
    increment : float
        Increment in seconds.

    Returns
    -------
    float
        Seconds spent, or -1.0 when it cannot be determined (the first two
        half-moves have no previous reading for that player).
    """
    if index < 2 or index >= len(clocks):
        return -1.0
    return max(0.0, clocks[index - 2] - clocks[index] + increment)


def analyse(path: str, engine: UciEngineClient, depth: int) -> dict | None:
    """
    Grade one finished game and return everything worth keeping about it.

    Parameters
    ----------
    path : str
        PGN file to analyse.
    engine : UciEngineClient
        A ready Stockfish client.
    depth : int
        Fixed search depth per position.

    Returns
    -------
    dict or None
        A record ready to serialise, or None when the file is unparseable or
        the bot did not play in it.
    """
    text = open(path, encoding='utf-8', errors='replace').read()

    def header(name: str) -> str:
        found = re.search(rf'\[{name} "([^"]*)"', text)
        return found.group(1) if found else ''

    white, black = header('White'), header('Black')
    if OUR_NAME not in (white, black):
        return None
    ours_white = white == OUR_NAME

    try:
        _fen, sans = pgn.parse_pgn(text)
    except pgn.PgnError:
        return None

    gs = GameState()
    uci_moves: list[str] = []
    movers: list[bool] = []
    for san in sans:
        try:
            move = pgn.san_to_move(gs, san)
        except pgn.PgnError:
            break
        movers.append(gs.white_to_move)
        uci_moves.append(move.get_uci_notation())
        gs.make_move(move, annotate=False)
    if len(uci_moves) < 2:
        return None

    time_control = header('TimeControl')
    increment = 0.0
    if '+' in time_control:
        try:
            increment = float(time_control.split('+')[1])
        except ValueError:
            increment = 0.0
    clocks = parse_clocks(text)

    # Each position is evaluated once and reused as the "after" of one move
    # and the "before" of the next.
    evals: list[int] = []
    for i in range(len(uci_moves) + 1):
        _best, score, is_mate = engine.analyse(uci_moves[:i], depth)
        evals.append(white_pov(score, is_mate, i % 2 == 0))

    moves = []
    for i, by_white in enumerate(movers):
        swing = evals[i] - evals[i + 1] if by_white else evals[i + 1] - evals[i]
        loss = min(max(swing, 0), MAX_CPL)
        # Win% is always from the point of view of whoever just moved.
        before = win_percent(evals[i] if by_white else -evals[i])
        after = win_percent(evals[i + 1] if by_white else -evals[i + 1])
        moves.append({
            'ply': i,
            'move_number': i // 2 + 1,
            'uci': uci_moves[i],
            'ours': by_white == ours_white,
            'cpl': loss,
            'accuracy': round(move_accuracy(before, after), 2),
            'eval_before': evals[i],
            'eval_after': evals[i + 1],
            'seconds': round(seconds_spent(clocks, i, increment), 2),
        })

    ours = [m for m in moves if m['ours']]
    theirs = [m for m in moves if not m['ours']]

    def summarise(group: list[dict]) -> dict:
        if not group:
            return {}
        losses = [m['cpl'] for m in group]
        return {
            'moves': len(group),
            'acpl': round(sum(losses) / len(losses), 2),
            'accuracy': round(sum(m['accuracy'] for m in group) / len(group), 2),
            'inaccuracies': sum(1 for c in losses if INACCURACY <= c < MISTAKE),
            'mistakes': sum(1 for c in losses if MISTAKE <= c < BLUNDER),
            'blunders': sum(1 for c in losses if c >= BLUNDER),
        }

    return {
        'file': os.path.basename(path),
        'analysed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'mtime': os.path.getmtime(path),
        'depth': depth,
        'site': header('Site'),
        'white': white,
        'black': black,
        'our_colour': 'w' if ours_white else 'b',
        'opponent': black if ours_white else white,
        'our_elo': header('WhiteElo' if ours_white else 'BlackElo'),
        'opponent_elo': header('BlackElo' if ours_white else 'WhiteElo'),
        'result': header('Result'),
        'time_control': time_control,
        'termination': header('Termination'),
        'opening': header('Opening'),
        'us': summarise(ours),
        'them': summarise(theirs),
        'moves': moves,
    }


def main() -> None:
    """
    Watch the record directory and analyse finished games as they appear.

    Returns
    -------
    None
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', default=DEFAULT_RECORDS)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--log', default=DEFAULT_LOG)
    parser.add_argument('--depth', type=int, default=DEFAULT_DEPTH)
    parser.add_argument('--poll', type=int, default=POLL_SECONDS)
    parser.add_argument('--once', action='store_true',
                        help='analyse the backlog and exit, ignoring the bot')
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    os.makedirs(os.path.dirname(args.log) or '.', exist_ok=True)

    log(f'watching {args.records} -> {args.output} (depth {args.depth})',
        args.log)

    engine: UciEngineClient | None = None
    try:
        while True:
            done = already_done(args.output)
            pending = [p for p in sorted(
                          (os.path.join(args.records, f)
                           for f in os.listdir(args.records)
                           if f.endswith('.pgn')),
                          key=os.path.getmtime)
                       if os.path.basename(p) not in done]

            if pending and (args.once or not bot_is_playing()):
                if not args.once:
                    # Let a new game claim the CPU first if one is starting.
                    time.sleep(IDLE_GRACE_SECONDS)
                    if bot_is_playing():
                        continue

                if engine is None:
                    engine = UciEngineClient([find_stockfish()])
                    engine.set_option('Threads', SF_THREADS)
                    engine.set_option('Hash', SF_HASH_MB)

                path = pending[0]
                try:
                    record = analyse(path, engine, args.depth)
                except EngineClientError as exc:
                    # Stockfish died; drop the client so the next pass
                    # respawns it, and try this game again then.
                    log(f'engine error on {os.path.basename(path)}: {exc}',
                        args.log)
                    engine = None
                    continue
                except Exception as exc:  # noqa: BLE001 - must not stop the watch
                    log(f'skipping {os.path.basename(path)}: {exc!r}', args.log)
                    record = {'file': os.path.basename(path),
                              'error': repr(exc)}

                if record is None:
                    # Not our game, or unparseable. Record that so it is not
                    # retried on every pass forever.
                    record = {'file': os.path.basename(path), 'skipped': True}

                with open(args.output, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(record) + '\n')

                summary = record.get('us') or {}
                log(f'{record["file"][:44]:<44} '
                    f'acpl {summary.get("acpl", "-")} '
                    f'acc {summary.get("accuracy", "-")} '
                    f'blunders {summary.get("blunders", "-")}', args.log)
                continue

            if args.once:
                log('backlog drained', args.log)
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log('stopped', args.log)
    finally:
        if engine is not None:
            engine.close()


if __name__ == '__main__':
    main()
