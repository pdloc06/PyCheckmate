"""
The headless chess engine package: rules, search, and UCI plumbing.

Nothing in here imports pygame, so the whole package runs under any bare
Python interpreter — including PyPy, where the JIT makes the search about
twice as fast (see uci_client.py). The GUI lives in the sibling `gui`
package and talks to this one through GameState/Move or, when PyPy is
available, over the UCI protocol via a subprocess.

Modules
-------
board      : board state, make/unmake, attack detection, FEN, Zobrist keys
movegen    : legal and capture generation, as free functions over a GameState
eval       : static evaluation — material, piece-square tables, positional terms
search     : negamax with alpha-beta, TT, quiescence, ordering, time management
tt         : transposition table entry layout and flags
pgn        : PGN/SAN import
analysis   : chess.com-style game review
uci        : UCI protocol adapter (run with `python -m engine.uci`)
uci_client : host-side client that spawns `engine.uci` as a subprocess

The dependencies run strictly one way, and that is worth preserving:

    board <- movegen <- eval <- search

`board` imports none of the others, so a cycle can only appear by adding an
import that points backwards along that chain.

Measurement and operations tooling lives in `engine.tools`, which may import
freely from here but is never imported *by* the engine.
"""
