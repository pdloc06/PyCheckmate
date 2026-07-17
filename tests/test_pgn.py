"""
Test suite for the PGN/SAN import module: SAN token resolution against the
legal-move generator, movetext cleanup (comments, variations, NAGs), FEN
detection, and full-game replay.
"""
import pytest

from engine import pgn
from engine.chess_engine import GameState


# --- FEN detection ---
def test_looks_like_fen_accepts_standard_fen():
    """Verify a full 6-field FEN is recognized as a FEN."""
    assert pgn.looks_like_fen('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')


def test_looks_like_fen_rejects_pgn_movetext():
    """Verify PGN movetext is not mistaken for a FEN."""
    assert not pgn.looks_like_fen('1. e4 e5 2. Nf3 Nc6 3. Bb5 a6')


# --- SAN resolution ---
def test_san_resolves_simple_pawn_and_knight_moves(gs):
    """Verify plain SAN tokens map to the expected squares."""
    e4 = pgn.san_to_move(gs, 'e4')
    assert e4.get_uci_notation() == 'e2e4'

    nf3 = pgn.san_to_move(gs, 'Nf3')
    assert nf3.get_uci_notation() == 'g1f3'


def test_san_resolves_captures_castling_and_checks():
    """Verify decorated tokens (captures, castles, check marks) resolve."""
    gs = GameState.from_fen('r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 0 1')
    capture = pgn.san_to_move(gs, 'Nxe5')
    assert capture.get_uci_notation() == 'f3e5'

    castle = pgn.san_to_move(gs, 'O-O')
    assert castle.is_castle_move and castle.get_uci_notation() == 'e1g1'


def test_san_resolves_disambiguation():
    """Verify file/rank disambiguation picks the right piece."""
    # Two white rooks on a1 and h1 can both reach d1 (king off the back rank)
    gs = GameState.from_fen('3k4/8/8/8/8/4K3/8/R6R w - - 0 1')
    assert pgn.san_to_move(gs, 'Rad1').get_uci_notation() == 'a1d1'
    assert pgn.san_to_move(gs, 'Rhd1').get_uci_notation() == 'h1d1'
    with pytest.raises(pgn.PgnError):
        pgn.san_to_move(gs, 'Rd1')  # Ambiguous without the hint


def test_san_resolves_promotion_piece():
    """Verify the promotion letter selects the matching Move object."""
    gs = GameState.from_fen('8/4P1k1/8/8/8/8/8/4K3 w - - 0 1')
    queen = pgn.san_to_move(gs, 'e8=Q')
    knight = pgn.san_to_move(gs, 'e8=N+')
    assert queen.promotion_piece == 'Q'
    assert knight.promotion_piece == 'N'


def test_san_rejects_illegal_move(gs):
    """Verify an unreachable square raises a PgnError."""
    with pytest.raises(pgn.PgnError):
        pgn.san_to_move(gs, 'Ke2')  # King is boxed in at the start


# --- Movetext parsing ---
def test_parse_pgn_strips_decorations():
    """Verify comments, variations, NAGs, and results are all removed."""
    text = (
        '[Event "Test"]\n'
        '[Result "1-0"]\n'
        '\n'
        '1. e4 {best by test} e5 (1... c5 2. Nf3) 2. Nf3 $1 Nc6 1-0'
    )
    start_fen, tokens = pgn.parse_pgn(text)
    assert start_fen is None
    assert tokens == ['e4', 'e5', 'Nf3', 'Nc6']


def test_parse_pgn_reads_fen_header():
    """Verify a [FEN "..."] header is surfaced as the starting position."""
    fen = '6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1'
    start_fen, tokens = pgn.parse_pgn(f'[FEN "{fen}"]\n\n1. Ra8# 1-0')
    assert start_fen == fen
    assert tokens == ['Ra8#']


# --- Whole-game replay ---
def test_game_from_pgn_replays_scholars_mate():
    """Verify a full PGN replays to the expected final position."""
    text = '1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0'
    gs = pgn.game_from_pgn(text)
    assert len(gs.move_log) == 7
    assert gs.is_checkmate
    # The mating move should carry the '#' suffix in its notation
    assert gs.move_log[-1].get_chess_notation().endswith('#')


def test_game_from_pgn_rejects_garbage():
    """Verify non-chess text raises a PgnError instead of crashing."""
    with pytest.raises(pgn.PgnError):
        pgn.game_from_pgn('hello world, this is not chess')
