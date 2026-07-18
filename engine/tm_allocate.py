"""What would the new time manager spend on the positions that actually mattered?

`engine.tm_replay` establishes which of the bot's real moves were the critical
ones, by grading its 16 online games. This takes that labelling and asks the
question the time-management work exists to answer: given the same nominal
budget everywhere, does the adaptive search *choose* to spend longer on the
positions where the bot went wrong than on the ones it was always going to
get right?

Hand-picked "hard" and "easy" positions proved useless for this -- a position
that is tactically rich to a human can be one where the engine picks the same
move at every depth, and a dead rook endgame can flap between equal-scoring
moves all search long. Real blunders in real games are the only labels not
contaminated by my guesses.

    uv run --no-project python -m engine.tm_allocate [soft] [hard]
"""
import sys
import time
from pathlib import Path

from engine import analysis, move_finder
from engine.chess_engine import GameState
from engine.tm_replay import CRITICAL, DEFAULT_DIR, analyse_game


def main() -> None:
    """Grade every game, then time an adaptive search on each of our moves."""
    soft = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    hard = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    label_seconds = 0.3

    files = sorted(Path(DEFAULT_DIR).glob('*.pgn'))
    print(f'{len(files)} games; labelling at {label_seconds}s, '
          f'then timing each position at soft={soft}s hard={hard}s\n')

    # Re-derive the labels, and rebuild the position each record refers to.
    records: list[dict] = []
    for path in files:
        got = analyse_game(path, label_seconds)
        if got:
            records.extend(got)
    print(f'{len(records)} of the bot\'s moves labelled')

    # Replaying to recover FENs is cheap next to searching, so redo it here
    # rather than widening tm_replay's record format.
    from engine import pgn
    fens: dict[tuple[str, int], str] = {}
    for path in files:
        text = path.read_text()
        _fen, sans = pgn.parse_pgn(text)
        gs = GameState()
        for ply, san in enumerate(sans):
            fens[(path.stem, ply)] = gs.to_fen()
            try:
                gs.make_move(pgn.san_to_move(gs, san), annotate=False)
            except pgn.PgnError:
                break

    timed: list[tuple[str, float]] = []
    for i, rec in enumerate(records):
        fen = fens.get((rec['game'], rec['ply']))
        if fen is None:
            continue
        move_finder._EVAL_CACHE.clear()
        gs = GameState.from_fen(fen)
        start = time.perf_counter()
        move_finder.find_best_move(gs, max_depth=64, time_limit=soft, hard_limit=hard)
        timed.append((rec['grade'], time.perf_counter() - start))
        if (i + 1) % 100 == 0:
            print(f'  ...{i + 1}/{len(records)}')

    critical = [t for g, t in timed if g in CRITICAL]
    routine = [t for g, t in timed if g not in CRITICAL and g != analysis.BOOK]

    print(f'\n=== adaptive allocation vs real outcomes (soft {soft}s) ===')
    c = sum(critical) / len(critical)
    r = sum(routine) / len(routine)
    print(f'critical positions (blunder/mistake/missed win): {c:5.2f}s  (n={len(critical)})')
    print(f'routine positions                              : {r:5.2f}s  (n={len(routine)})')
    print(f'ratio                                          : {c / r:5.2f}x')
    print('\n>1.00x means the search spends longer where the games were lost.')


if __name__ == '__main__':
    main()
