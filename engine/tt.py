"""
Transposition table: the shape of a stored search result.

The table itself is a plain dict, deliberately. A dedicated class would put a
Python method call on the hottest path in the engine — the probe runs at every
node — and buys nothing a dict does not already do. What actually needed a home
is the *contract*: what an entry contains and what its flag means, which was
previously implicit in the code that read it.

An entry is `(depth, flag, score, best_move, generation)`:

- `depth`      how deep the search that produced `score` was. A shallower
               entry cannot answer a deeper question, so probes compare this.
- `flag`       how to read `score`, one of the three constants below.
- `score`      the value found. **Mate scores are never stored** — they are
               relative to the root, so a mate-in-3 entry reused at another
               depth would claim the wrong distance.
- `best_move`  the move that caused the cutoff, used for move ordering even
               when the score itself is unusable.
- `generation` which search wrote the entry, so the replacement policy can
               prefer evicting results left over from earlier moves.

Callers may keep one table across searches — `engine.uci` holds a game-long
one — because Zobrist keys identify positions absolutely.
"""
from engine.movegen import MoveTuple

# Type alias for the transposition table: zobrist_key -> (depth, flag, score,
# best_move, generation). Callers may hold one of these across searches (the
# UCI adapter keeps a game-long table) and pass it to `find_best_move`. The
# `generation` field stamps which search wrote the entry so the replacement
# policy can evict entries left over from earlier moves (see `_negamax`).
TTable = dict[int, tuple[int, int, int, MoveTuple | None, int]]

# Transposition table bound flags
TT_EXACT, TT_LOWER, TT_UPPER = 0, 1, 2
