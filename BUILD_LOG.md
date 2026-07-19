# PyCheckmate — Build Log

A running record of how this engine was built: what was added, what broke, what
was measured, and what got thrown away. Kept incrementally, not reconstructed at
the end.

**Status legend:** ✅ shipped · ❌ measured and reverted · ⏳ in progress

---

## Project Overview

**What it is.** A chess engine written from scratch in Python, with three faces:
a Pygame desktop game, a chess.com-style post-game review screen, and a UCI
adapter that runs the same engine as a bot on Lichess.

**Stack.**

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.14 | Learning project — clarity over raw speed |
| Engine core | **Pure stdlib, zero dependencies** | Lets the engine run under PyPy |
| Runtime accel | PyPy 3.11 subprocess | JIT gives a large speedup on the hot search loop; auto-detected, never required |
| GUI | pygame-ce | Desktop game + review screen |
| Tooling | uv, pytest, mypy (strict) | Reproducible env; 124 tests and a clean type check are the gates on every change |
| Deployment | lichess-bot bridge, macOS | Engine speaks UCI, so a standard bridge hosts it |

**Goal.** Learn search and evaluation by building them rather than reading about
them — and then prove the result is real by making it play rated games against
strangers. A secondary goal became just as important: learn to **measure**
engine changes honestly, because most of the interesting failures in this
project were measurement failures, not coding failures.

**Scale.** ~10,900 lines total; ~5,700 in `engine/`, of which `move_finder.py`
(search, 1,651 lines) and `chess_engine.py` (rules, 1,642 lines) are the core.

---

## Architecture & Algorithms

### Board representation
Board is `list[list[int]]` of small integer piece codes (`0` empty, `1-6` white,
`7-12` black). It began as `'wP'`/`'--'` strings; migrating to ints removed
string comparison from the hot loops. String codes survive only at the FEN,
SAN/UCI and GUI-image boundaries.

**Two parallel move pipelines**, which is the central design decision:

- **UI path** — `get_valid_moves()` returns rich `Move` objects; `make_move()`
  maintains move log, state log, repetition counts, full Zobrist recompute.
- **AI path** — `get_valid_moves(for_ai=True)` returns bare 5-tuples;
  `make_ai_move()` skips all logging, updates Zobrist incrementally, and returns
  a 4-tuple undo package.

The hot loop cannot afford the bookkeeping the UI needs, and the UI cannot work
without it. Keeping them separate — and converting between them at exactly one
seam (`Move.from_ai_tuple`) — is what makes both fast and correct.

### Search (`engine/move_finder.py`)
Built up incrementally, each piece measured before it was kept:

| Technique | Purpose |
|---|---|
| Negamax + alpha-beta | The base search |
| Iterative deepening | Anytime search — gives a usable move whenever the clock stops |
| Transposition table (Zobrist-keyed) | Reuse work across transpositions; depth-preferred replacement with aging |
| Quiescence search | Fixes the horizon effect — never evaluate mid-capture |
| MVV-LVA + killers + history | Move ordering; alpha-beta's benefit depends almost entirely on trying good moves first |
| Static exchange evaluation (SEE) | Prune losing captures in quiescence instead of searching them |
| Null-move pruning | Skip a turn; if still winning, the position is not worth full depth |
| PVS (null-window scouts) | Assume the first move is best, verify siblings cheaply |
| Late move reductions | Search unpromising late moves shallower, re-search if one beats alpha |
| Aspiration windows | Start each iteration in a narrow window around the last score |
| Check extension | Never stop the search inside a forced sequence |
| Futility / reverse-futility pruning | Skip nodes that cannot reach alpha |

### Evaluation
Material + piece-square tables, **tapered** between middlegame and endgame by
material phase (a king belongs in the corner at move 20 and in the centre at
move 60 — one static table cannot say both). Plus mobility, rook activity and
pawn structure terms. Always computed from White's perspective, memoized by
Zobrist key.

### Time management (`engine/uci.py`)
Soft/hard two-bound design: `time_limit` is the target, `hard_limit` funds a
panic extension when the score collapses. The budget divides the remaining clock
by an **estimate of moves still to play**, with two emergency tiers below 25s
and 10s. See the 2026-07-19 entries — this is the part with the most interesting
measurement story.

### Supporting modules
`analysis.py` — chess.com-style move grading (blunder/mistake/brilliant ladder
via win% loss). `pgn.py` — SAN import by *matching* against legal moves rather
than re-implementing rules. `uci.py`/`uci_client.py` — the engine-as-a-process
pair. `bench.py`, `abtest.py`, `selfplay.py`, `tm_replay.py`, `tm_allocate.py` —
the measurement toolkit, which grew to five modules for good reason.

---

## Timeline & Milestones

### Phase 1 — Rules engine (2026-07-01 → 07-10)
- **07-01** First commit. Board state, piece rendering, `GameState`.
- **07-04→07-06** Move generation, then legality: check detection, pin handling,
  checkmate/stalemate.
- **07-07** Castling and pawn promotion.
- **07-08** En passant, including the hidden-horizontal-pin case (capturing en
  passant removes *two* pawns from a rank, which can expose a king sideways).
- **07-10** 50-move rule, threefold repetition, GUI extracted into `gui/`.

### Phase 2 — Search engine (2026-07-16 → 07-17)
- **07-16** Zobrist hashing and the first real search (`move_finder.py`).
  Alpha-beta, move ordering, iterative deepening.
- **07-16** UCI adapter written — engine becomes hostable as a bot.
- **07-17** ✅ Perft bug found and fixed (see Challenges). Perft suite added.
- **07-17** PyPy UCI subprocess hosting; engine confirmed dependency-free.
- **07-17** Game review screen: eval bar, move grading, variations, FEN/PGN import.

### Phase 3 — Strength tuning (2026-07-18)
Ten measured stages (A–J), each committed separately so it could be reverted alone.
- ✅ Quiescence, persistent TT, extra eval terms.
- ✅ Integer piece codes. Depth-5 search **1.35s → 0.28s**.
- ✅ Eval cache by Zobrist key. Depth-6 **35% faster under PyPy**, 8% CPython.
- ✅ PVS, quiescence TT + delta pruning, log-scaled LMR, tapered eval, mobility.
- ❌ **Late move pruning — reverted.** −60 Elo, and −24 after fixing a broken test.
- ❌ **Countermove + IIR — reverted.** Never measured on its own; unproven code goes.
- **07-18** Deployed to Lichess. Self-play smoke test added.

### Phase 4 — Time management (2026-07-19)
- ✅ **Node-count methodology adopted.** Score-neutral changes verified by exact
  node equality instead of overnight matches.
- ✅ **Ray tables precomputed. 10.4% faster overall**, `_mobility` 43% faster,
  proven safe by identical node counts. Verified in ~30 seconds.
- ✅ **`tm_replay.py`** — grades the bot's 16 real online games to find where it
  actually loses.
- ✅ **Time budget reshaped** (moves-to-go + overhead reserve). Middlegame
  thinking time **+46%**.
- ❌ **Best-move stability — measured worse (1.11x vs 1.34x), reverted.**
- ⏳ **400-game clock-mode gate running.** Started 2026-07-19.

---

## Challenges & Solutions

### 1. The two-node perft discrepancy
**Problem.** Move generation looked correct and passed every hand-written test.

**Diagnosis.** Perft — count leaf nodes at depth N and compare to published
values. Depth 5 produced **4,865,607 nodes instead of the canonical 4,865,609**.
Two nodes wrong out of 4.8 million: a bug no amount of playing would ever
surface, and no eyeballing would ever find.

**Root cause.** `_is_square_attacked` let sliding attack rays pass *through* an
adjacent enemy pawn whenever that pawn didn't itself attack the probed square.
So a queen could "attack" a square straight through her own blocking pawn,
illegally restricting the enemy king's moves.

**Fix.** One blocking condition, plus perft(4), Kiwipete perft(1–3), and a
direct regression test. **Lesson: a test that produces one exact number beats
any number of tests that produce "looks right."**

### 2. Profiling contradicted the obvious hypothesis
**Problem.** Search was too slow. Everyone "knows" attack detection dominates a
chess engine.

**Diagnosis.** `cProfile` at depth 5 said the hotspots were `evaluate()` and
move generation — **not** `_is_square_attacked`, which is where I'd have spent
the day. A later depth-6 profile put `evaluate` at 31% of runtime, and
`_mobility` alone at 14% (121,789 calls).

**Fix.** Optimized what the profiler pointed at. The `_mobility` cost turned out
not to be its logic but its *bookkeeping*: ray-walking re-tested
`0 <= r < 8 and 0 <= c < 8` at every single step. Board geometry is fixed, so
every ray is enumerable once at import. Result: **43% faster in isolation, 10.4%
overall.**

### 3. A night of measurement that produced nothing
**Problem.** Search stages F–J ran overnight self-play matches. Net result:
**~0 Elo, most of it unresolvable noise.** A whole program of machine time
bought no knowledge.

**Diagnosis.** The instrument was wrong for the question. A 100-game match
resolves nothing finer than **±70 Elo**; 400 games gets to **±35**. Real search
improvements are worth 10–30 Elo. A 100-game verdict on a 20-Elo change is
*noise wearing a number*.

**Fix — the most valuable thing learned in this project.** Match the instrument
to the change:

- **Score-neutral changes** (faster eval, cheaper movegen): use **node count**.
  It is exactly deterministic. Identical node totals prove the search made every
  same decision, so the change *cannot* have altered play. This turns an
  overnight match into a 30-second check — and it's *stronger* evidence, because
  it's a proof rather than a statistic.
- **Behaviour changes**: self-play, 400 games minimum, never two concurrently.

The catch: node determinism only holds because the bench seeds the root RNG. The
root shuffle changes how much the search prunes, so unseeded counts don't
reproduce and the method silently stops working. That's now guarded by
`test_node_count_is_reproducible_for_a_seeded_search`.

### 4. A benchmark that couldn't see improvements
**Problem.** Self-play used a depth cap of 6 as a "safety net," assuming the
clock always stopped the search first.

**Diagnosis.** Measured it: at the 0.2s budget, **depth 6 completed naturally in
4 of 7 realistic positions**. The cap was binding, not the clock.

**Why it mattered.** A faster engine in a cap-bound position has *nowhere to
spend the speed*. Every speed optimization would measure as 0 Elo no matter how
much it really helped. The measuring instrument had a hard ceiling and was
silently reporting it as a result.

**Fix.** `DEPTH` raised to 12 — above what the budget can reach, leaving the
clock as the only binding constraint.

### 5. The engine was playing a faster time control than it was given
**Problem.** Bot was blundering in positions where it had time to spare.

**Diagnosis.** Built `tm_replay.py` to replay all 16 real online games, parse
`%clk` annotations for actual time spent, and grade every move. Two findings:

- It finished **sixty-move games with 28–48% of its clock unspent.**
- **73% of its blunders/mistakes/missed wins fell in moves 21–40** — 8% and 7%
  error rates, against 1% in moves 1–20 where the opening book answers anyway.

**Root cause.** `clock_move_budget` divided the remaining clock by a constant 30.
Dividing by a constant **decays geometrically**, so the clock is never actually
spent — it just asymptotes. Time was being hoarded forever and left on the table
at the end.

**Fix.** Divide by an *estimate of moves remaining* instead. Middlegame budget:

| Time control | Moves 21–40, before | After | Change |
|---|---|---|---|
| 5+0 | 3.7s | 5.4s | **+46%** |
| 10+2 | 9.3s | 12.9s | **+39%** |

Opening damped (5+0 move 1: 10.0s → 5.7s), unspent clock after a 45-move game
cut from ~25% to 18%.

**Near-miss worth recording.** The first draft engaged its safety guard below
60s — which in a 5+0 game means hoarding from about move 35, *precisely inside
the band where the errors happen*. A simulation caught it flagging at move 77.
The shipped version uses 25s/10s tiers and survives a marathon identically to
the old rule (both reach move ~124).

### 6. The heuristic that measured backwards — twice
**Problem.** The headline feature: "know when to think longer." Standard idea —
if the root best move keeps changing between iterations, the position is sharp,
so extend.

**Diagnosis, attempt 1.** Measured a uniform 1.01x. The mechanism **wasn't
firing at all**: I counted every best-move change, but depths 1–3 always flip,
so the signal was drowned. Fixed with a decaying accumulator.

**Diagnosis, attempt 2.** Now it fired — *backwards*, at 0.64x. I traced it
per-iteration and found the fault was in **my test labels, not the code**. I had
hand-picked Kiwipete as "hard" and a rook endgame as "easy." The engine picks
the same move at every depth in Kiwipete (calm) and flaps all search long in the
endgame (unstable). **My labels were exactly inverted.**

**Resolution.** Rebuilt the instrument (`tm_allocate.py`) on *real blunders from
real games* — the only labels not contaminated by my guesses. Against those:

| Rule | Critical/routine time ratio |
|---|---|
| Existing panic rule (binary score-drop) | **1.34x** |
| + continuous response + stability signal | 1.11x |
| + continuous response only | 1.36x (a wash) |

**The planned feature was worse than the crude rule it was meant to replace.**
Reverted; only the negative result is committed, as a comment where the next
person will look.

**Why stability backfires here:** in quiet endgames the root best move flaps
between moves of *identical* score, so it reads as maximally unstable exactly
where there's least to think about.

**The part that matters most:** a self-play match would have called this whole
change "neutral" after a full night, with **no way to distinguish "didn't work"
from "never fired."** Both failure modes were caught in minutes by an instrument
that observed the mechanism directly.

### 7. Production incident — HTTP 429 lockout
**Problem.** Bot appeared online but accepted challenges without ever playing.
Error log full of `RateLimitedError`.

**Diagnosis.** Lichess rate-limits `/api/stream/event`. Restarting the bot
repeatedly while iterating on config tripped a 429 the bridge couldn't recover
from — it retried *faster* than the ~60s cooldown, so it could never escape. Made
worse by orphaned `multiprocessing` children from previous runs holding streams
open, invisible to a normal process check.

**Fix.** Sweep orphans explicitly (`ps aux | grep "lichess-bot/.venv"`), then one
clean start and wait in silence. Operational rule adopted: **batch all config
edits, then exactly one restart cycle.**

---

## Performance Benchmarks

Measured with `engine.bench` (4 positions: opening / middlegame / tactical /
endgame), seeded RNG, best-of-5 back-to-back runs.

| Date | Change | Metric | Before | After | Gain |
|---|---|---|---|---|---|
| 07-18 | Integer piece codes + search cuts | Depth-5 search | 1.35s | 0.28s | **~4.8x** |
| 07-18 | Zobrist eval cache (PyPy) | Depth-6 search | — | — | **35% faster** |
| 07-18 | Zobrist eval cache (CPython) | Depth-6 search | — | — | 8% faster |
| 07-19 | Precomputed ray tables | Bench total (best-of-5) | 3.471s | 3.111s | **10.4%** |
| 07-19 | Precomputed ray tables | `_mobility` isolated (56k calls) | 0.053s | 0.030s | **43%** |

**Safety proof for the ray-table change:** both versions visited **113,218 nodes
— exactly**. Identical node counts across all four positions mean every search
decision was unchanged, so the optimization provably cannot have altered play.
Sample ranges were disjoint (3.471–3.499 vs 3.111–3.210), so the timing result
is real and not the ~29% run-to-run noise this machine shows.

**Time allocation** (`tm_replay` over 16 real games):

| Metric | Before | After |
|---|---|---|
| Moves 21–40 budget, 5+0 | 3.7s | 5.4s (+46%) |
| Moves 21–40 budget, 10+2 | 9.3s | 12.9s (+39%) |
| Clock unspent after 45-move game | ~25% | 18% |
| Opening move 1 budget, 5+0 | 10.0s | 5.7s (damped) |

_Not yet measured: nodes/second, and depth-reached-in-fixed-time over the project's
history. Both would strengthen this table; neither was recorded early enough to
reconstruct honestly._

---

## Testing & Validation

**124 tests, plus strict mypy across 30 source files.** Both gate every change.

| Method | What it proves | Result |
|---|---|---|
| **Perft** (depths 1–4: 20 / 400 / 8,902 / 197,281, plus Kiwipete) | Move generation is exactly correct | Byte-exact. Caught the 2-node ray bug at depth 5 |
| **Random-walk make/unmake** | `white_pieces`/`black_pieces` sets stay exact through thousands of random moves | Passing — guards the subtlest class of bug in the codebase |
| **Node-count reproducibility** | The measurement method itself still works | Passing — protects the seeded-RNG invariant |
| **Self-play smoke test** | Whole games hold together over the real UCI round-trip; referee rejects illegal moves | Passing |
| **A/B self-play matches** (`abtest.py`) | Strength changes, 400 games ≈ ±35 Elo | Used to reject LMP (−60/−24 Elo) |
| **Blunder replay** (`tm_replay.py`) | Where the engine actually loses, from real games | 73% of errors in moves 21–40 |
| **Allocation probe** (`tm_allocate.py`) | Whether a time rule spends more where games were lost | Rejected the stability heuristic (1.11x vs 1.34x) |
| **Live Lichess play** | Everything, against real opponents | Deployed and playing |

**Known limitations.**
- No opening book of its own (relies on the bridge's polyglot book).
- No endgame tablebases.
- Single-threaded search — no SMP.
- Evaluation has no king-safety term yet.
- The 400-game gate uses self-play games averaging 72 moves per side, while real
  online games average 45 — so it exercises the time schedule's tail harder than
  deployment does.
- Zero flagged games observed so far in the gate, meaning the *overspend
  protection* half of time management remains untested by self-play.

---

## Results & Current Status

**Deployed and playing rated games on Lichess** (blitz 5+0 and 10+2; bullet
disabled — move overhead is too tight to be safe there yet).

**Strength:** not yet self-calibrated. Task open to run a laddered match against
known-rating opposition to fix the engine's own Elo, which is also needed to set
sane matchmaking bounds.

> **📌 PLACEHOLDER — fill in manually once available**
> - Lichess bot username: `________`
> - Blitz rating: `________`
> - Bullet / Rapid rating: `________`
> - Games played / W-L-D: `________`
> - Profile link: `________`

**In flight (2026-07-19):** 400-game clock-mode self-play gate confirming the
time-budget rework didn't cost strength. Expected outcome is "no significant
difference," which is a pass — the change exists to stop wasting clock, and the
evidence it works is the replay instrument, not this match.

**What's left**
- King safety in the evaluation (largest known eval gap).
- Node-effort-based early stopping (designed, gated on the time work paying off).
- Self-calibrated Elo + matchmaking bounds.
- Opening book owned by the engine rather than the bridge.

**What "done" looks like.** The engine plays rated blitz unattended without
crashing, flagging, or hanging pieces to the horizon effect, at a stable rating
I can actually quote. Most of that is met; the rating number is the gap.

---

## Key Takeaways

1. **The hardest problem was measurement, not chess.** Writing alpha-beta is a
   weekend. Knowing whether your change *helped* is the actual discipline. I
   burned a full night of self-play on search stages F–J for ~0 Elo of
   unresolvable noise, because I was asking a ±70-Elo instrument about a 20-Elo
   change.

2. **Deterministic checks beat statistical ones whenever you can get them.**
   Node count is exact: identical totals *prove* a change didn't alter play. That
   replaced an overnight match with a 30-second check for the ray-table
   optimization — and it's stronger evidence, since it's a proof rather than a
   p-value. The lesson generalizes past chess: prefer the check that can only
   have one answer.

3. **My intuitions about my own code were wrong more often than they were
   right.** I expected attack detection to be the hotspot; it was evaluation. I
   expected best-move stability to identify hard positions; it identified quiet
   endgames flapping between equal moves. I hand-labelled two test positions and
   got both *backwards*. Every one of those was caught by measuring, and none of
   them would have been caught by reading the code.

4. **Negative results are worth committing.** Three features were built,
   measured, and deleted — LMP (−60 Elo), countermove+IIR (unproven), best-move
   stability (1.11x vs the crude rule's 1.34x). Each left behind a comment
   explaining *why* it failed. The stability comment is the most useful thing in
   that file: it stops the next person rebuilding it.

5. **The bug that mattered was invisible to playing the game.** Two nodes wrong
   out of 4.8 million — undetectable by watching games, unhittable by
   hand-written tests, found instantly by one number that had a known correct
   value. Since then, every subsystem got an exact-value test if one existed.

---

_Last updated: 2026-07-19 — 400-game time-management gate in progress._
