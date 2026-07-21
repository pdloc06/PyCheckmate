"""
Test suite for the game-review analysis module: win-percentage conversion,
eval formatting, move classification thresholds, the sacrifice heuristic,
opening-book lookups, and the background whole-game analyser.
"""
import time

from engine import analysis
from engine.board import GameState
from engine.eval import CHECKMATE_SCORE
from engine.search import search_position
from engine.movegen import generate_legal


def _eval(score_white: int, best=None, legal: int = 20) -> analysis.PositionEval:
    """Build a PositionEval fixture without running a search."""
    return analysis.PositionEval(score_white, best, legal)


# --- Win percentage and formatting ---
def test_win_percent_is_symmetric_around_equality():
    """Verify the logistic conversion is centered and monotonic."""
    assert analysis.win_percent(0) == 50.0
    assert analysis.win_percent(200) > 60.0
    assert abs(analysis.win_percent(150) + analysis.win_percent(-150) - 100.0) < 1e-9


def test_win_percent_saturates_on_mate_scores():
    """Verify forced-mate scores map to certain win/loss."""
    assert analysis.win_percent(CHECKMATE_SCORE - 5) == 100.0
    assert analysis.win_percent(-(CHECKMATE_SCORE - 5)) == 0.0


def test_format_eval_renders_cp_and_mate():
    """Verify the eval-bar label formats for all score kinds."""
    assert analysis.format_eval(130) == '+1.3'
    assert analysis.format_eval(-40) == '-0.4'
    assert analysis.format_eval(CHECKMATE_SCORE - 9) == 'M5'   # Mate in 9 plies
    assert analysis.format_eval(-(CHECKMATE_SCORE - 6)) == '-M3'
    assert analysis.format_eval(CHECKMATE_SCORE) == '#'


# --- Classification ladder ---
def test_classify_engine_move_is_best():
    """Verify playing the engine's own move earns the Best tag."""
    move = (6, 4, 4, 4, 0)
    tag = analysis.classify_move(
        move, _eval(30, best=move), _eval(30), True, in_book=False, sacrifice=False
    )
    assert tag == analysis.BEST


def test_classify_win_percent_ladder():
    """Verify increasing win% losses walk down the quality ladder."""
    move = (6, 4, 4, 4, 0)
    other = (6, 0, 4, 0, 0)

    def tag_for(cp_after: int) -> str:
        return analysis.classify_move(
            move, _eval(0, best=other), _eval(cp_after), True,
            in_book=False, sacrifice=False,
        )

    assert tag_for(-35) == analysis.EXCELLENT    # ~3.2 win% lost
    assert tag_for(-70) == analysis.GOOD         # ~6.4 win% lost
    assert tag_for(-100) == analysis.INACCURACY  # ~9.1 win% lost
    assert tag_for(-180) == analysis.MISTAKE     # ~16 win% lost
    assert tag_for(-700) == analysis.BLUNDER     # ~39 win% lost


def test_classify_book_and_forced_take_priority():
    """Verify book positions and only-moves bypass the ladder."""
    move = (6, 4, 4, 4, 0)
    assert analysis.classify_move(
        move, _eval(0, best=move), _eval(0), True, in_book=True, sacrifice=False
    ) == analysis.BOOK
    assert analysis.classify_move(
        move, _eval(0, best=move, legal=1), _eval(0), True, in_book=False, sacrifice=False
    ) == analysis.FORCED


def test_classify_missed_mate_is_a_miss():
    """Verify throwing away a forced mate earns the Miss tag."""
    move = (6, 4, 4, 4, 0)
    mate_score = CHECKMATE_SCORE - 3
    tag = analysis.classify_move(
        move, _eval(mate_score, best=(0, 0, 1, 1, 0)), _eval(400), True,
        in_book=False, sacrifice=False,
    )
    assert tag == analysis.MISS


def test_classify_best_sacrifice_is_brilliant():
    """Verify a sound sacrifice that is also the best move gets Brilliant."""
    move = (4, 2, 3, 3, 0)
    tag = analysis.classify_move(
        move, _eval(50, best=move), _eval(60), True, in_book=False, sacrifice=True
    )
    assert tag == analysis.BRILLIANT


def test_classify_only_good_move_is_great():
    """Verify a unique saving move gets Great when alternatives collapse."""
    move = (4, 2, 3, 3, 0)
    tag = analysis.classify_move(
        move, _eval(0, best=move), _eval(0), True,
        in_book=False, sacrifice=False, second_best_score_white=-400,
    )
    assert tag == analysis.GREAT


def test_classify_black_perspective():
    """Verify the ladder flips sign correctly for Black's moves."""
    move = (1, 4, 3, 4, 0)
    # White-perspective score jumps +700 after Black's move: Black blundered
    tag = analysis.classify_move(
        move, _eval(0, best=(0, 0, 1, 1, 0)), _eval(700), False,
        in_book=False, sacrifice=False,
    )
    assert tag == analysis.BLUNDER


# --- Sacrifice heuristic ---
def test_sacrifice_detects_hanging_queen_offer():
    """Verify moving a queen onto an attacked, undefended square counts."""
    gs = GameState.from_fen('6k1/5pp1/8/3r4/8/8/5PP1/3Q2K1 w - - 0 1')
    # Qxd5 captures a rook — not a sacrifice (gains material)
    assert not analysis.is_sacrifice(gs, (7, 3, 3, 3, 0))
    # Qd4 hangs the queen to the rook for nothing
    assert analysis.is_sacrifice(gs, (7, 3, 4, 3, 0))


def test_sacrifice_ignores_pawn_moves(gs):
    """Verify pawn offers never count as (piece) sacrifices."""
    assert not analysis.is_sacrifice(gs, (6, 4, 4, 4, 0))  # e4


def test_sacrifice_state_is_restored():
    """Verify the make/unmake probe leaves the position untouched."""
    gs = GameState.from_fen('6k1/5pp1/8/3r4/8/8/5PP1/3Q2K1 w - - 0 1')
    key_before = gs.zobrist_key
    board_before = [row[:] for row in gs.board]
    analysis.is_sacrifice(gs, (7, 3, 4, 3, 0))
    assert gs.zobrist_key == key_before
    assert gs.board == board_before


# --- Opening book ---
def test_book_recognizes_theory_and_rejects_novelty(gs):
    """Verify a mainline position is book and a random one is not."""
    italian = GameState.from_fen(
        'r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3'
    )
    assert analysis.is_book_position(italian)

    novelty = GameState.from_fen(
        'rnbqkbnr/pppppppp/8/8/7P/8/PPPPPPP1/RNBQKBNR b KQkq - 0 1'
    )  # 1. h4 is not in anyone's book
    assert not analysis.is_book_position(novelty)


# --- Position evaluation & accuracy ---
def test_evaluate_position_reports_white_perspective():
    """Verify scores come back White-positive regardless of side to move."""
    # White is a queen up in both positions
    white_to_move = GameState.from_fen('6k1/8/8/8/8/8/8/Q5K1 w - - 0 1')
    black_to_move = GameState.from_fen('6k1/8/8/8/8/8/8/Q5K1 b - - 0 1')
    eval_w = analysis.evaluate_position(white_to_move, 2, 5.0)
    eval_b = analysis.evaluate_position(black_to_move, 2, 5.0)
    assert eval_w.score_white > 300
    assert eval_b.score_white > 300


def test_search_position_scores_terminal_positions():
    """Verify the score interface handles mate and stalemate roots."""
    mated = GameState.from_fen('rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3')
    generate_legal(mated, for_ai=True)
    move, score = search_position(mated, max_depth=2, time_limit=5.0)
    assert move is None and score == -CHECKMATE_SCORE


def test_accuracy_from_drops_bounds():
    """Verify the accuracy curve behaves at its extremes."""
    assert analysis.accuracy_from_drops([]) == 100.0
    assert analysis.accuracy_from_drops([0.0, 0.0]) > 99.0
    assert analysis.accuracy_from_drops([50.0]) < 15.0


# --- Whole-game analyser ---
def test_game_analysis_worker_completes_and_tags():
    """Verify the background analyser fills every eval and tag slot."""
    start = GameState().to_fen()
    moves = [(6, 4, 4, 4, 0), (1, 4, 3, 4, 0)]  # 1. e4 e5
    worker = analysis.GameAnalysis(start, moves, max_depth=2, time_limit=1.0)
    try:
        deadline = time.time() + 30
        while not worker.done and time.time() < deadline:
            time.sleep(0.05)
        assert worker.done
        assert all(entry is not None for entry in worker.evals)
        assert all(tag is not None for tag in worker.tags)
        # 1. e4 e5 is as book as it gets
        assert worker.tags[0] == analysis.BOOK
        assert worker.accuracy(white=True) is not None
    finally:
        worker.stop()
