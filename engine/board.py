"""
Board state: the position, the moves that change it, and attack detection.

This module owns everything that describes *where the pieces are* and how that
changes — the board array, the piece-tracking sets, castling and en-passant
rights, the Zobrist key, and the two make/unmake pipelines. It also owns attack
detection (`check_pins_checks`, `is_square_attacked`), because `make_move`
needs it to annotate check for SAN and because pins and checks are properties
of the position rather than of any move list.

*Generating* moves lives next door in `engine.movegen`, which imports from here.
The dependency runs one way and must stay that way: nothing in this module may
import `movegen`.

The board is an 8x8 list of small ints. An empty square is 0, White pieces are
1-6 and Black 7-12, so `0 < piece < 7` tests color and `PIECE_TYPE[piece]`
recovers a color-independent 1-6 type index. Row 0 is rank 8, so Black's home
rank comes first. The legacy 'wP'/'--' string codes survive only at the FEN,
SAN/UCI and GUI boundaries, via `CODE_TO_INT` / `INT_TO_CODE`.

Every position is additionally identified by an incrementally updated Zobrist
key (`zobrist_key`), so search code gets transposition tables and repetition
detection with O(1) hashing per move.
"""
import random
from dataclasses import dataclass

# Shared movement geometry
ORTHOGONAL_DIRECTIONS: tuple[tuple[int, int], ...] = ((-1, 0), (1, 0), (0, -1), (0, 1))
DIAGONAL_DIRECTIONS: tuple[tuple[int, int], ...] = ((-1, -1), (1, 1), (1, -1), (-1, 1))
ALL_DIRECTIONS: tuple[tuple[int, int], ...] = (
    (-1, 0), (0, -1), (1, 0), (0, 1),
    (-1, -1), (-1, 1), (1, -1), (1, 1)
)
KNIGHT_DELTAS: tuple[tuple[int, int], ...] = (
    (-2, -1), (-2, 1), (-1, -2), (-1, 2),
    (1, -2), (1, 2), (2, -1), (2, 1)
)

# Lightweight AI move-type codes used in 5-element move tuples
# 0=Normal, 1=Castle, 2=En Passant, 3=Promo(Q), 4=Promo(R), 5=Promo(B), 6=Promo(N)
AI_PROMO_PIECES: dict[int, str] = {3: 'Q', 4: 'R', 5: 'B', 6: 'N'}
AI_PROMO_CODES: dict[str, int] = {'Q': 3, 'R': 4, 'B': 5, 'N': 6}

# Integer board encoding. An empty square is 0; White pieces are 1-6 and Black
# 7-12, so `0 < piece < 7` tests colour and `PIECE_TYPE[piece]` recovers a
# colour-independent 1-6 type index (used to key the PST and the move dispatch).
# Switching the board from 'wP'/'--' strings to these ints removes string
# indexing and comparison from every square test in the hot move-gen/eval loops.
EMPTY = 0
WP, WN, WB, WR, WQ, WK = 1, 2, 3, 4, 5, 6
BP, BN, BB, BR, BQ, BK = 7, 8, 9, 10, 11, 12
PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING = 1, 2, 3, 4, 5, 6
PIECE_TYPE: tuple[int, ...] = (
    0, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING,
    PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING,
)

# Boundary conversions: FEN, SAN/UCI notation, and the GUI still speak the old
# two-character codes, so convert only at those edges.
CODE_TO_INT: dict[str, int] = {
    '--': EMPTY,
    'wP': WP, 'wN': WN, 'wB': WB, 'wR': WR, 'wQ': WQ, 'wK': WK,
    'bP': BP, 'bN': BN, 'bB': BB, 'bR': BR, 'bQ': BQ, 'bK': BK,
}
INT_TO_CODE: dict[int, str] = {value: code for code, value in CODE_TO_INT.items()}

# Promotion move-type code (3-6) -> piece-type int; make_ai_move adds a colour
# offset (0 for White, 6 for Black) to land on the concrete piece int.
AI_PROMO_TYPE: dict[int, int] = {3: QUEEN, 4: ROOK, 5: BISHOP, 6: KNIGHT}

# Zobrist hashing tables, keyed by the integer piece code (1-12).
_zobrist_rng = random.Random(20260716)
ZOBRIST_PIECES: dict[int, list[list[int]]] = {
    piece: [[_zobrist_rng.getrandbits(64) for _ in range(8)] for _ in range(8)]
    for piece in range(1, 13)
}
ZOBRIST_SIDE: int = _zobrist_rng.getrandbits(64)
ZOBRIST_CASTLING: list[int] = [_zobrist_rng.getrandbits(64) for _ in range(16)]
ZOBRIST_EP_FILE: list[int] = [_zobrist_rng.getrandbits(64) for _ in range(8)]


@dataclass
class CastleRights:
    """
    Data wrapper for storing castling privileges at a specific game state.

    Attributes
    ----------
    white_king_side : bool
        True if White can castle king-side.
    white_queen_side : bool
        True if White can castle queen-side.
    black_king_side : bool
        True if Black can castle king-side.
    black_queen_side : bool
        True if Black can castle queen-side.
    """
    white_king_side: bool
    white_queen_side: bool
    black_king_side: bool
    black_queen_side: bool


class Move:
    """
    Representation of a single chess move.

    Stores source and destination squares, move type constraints, and provides
    functions to convert movements into standard algebraic chess notation and
    UCI coordinate notation.
    """
    NORMAL = 'normal'
    EN_PASSANT = 'en_passant'
    CASTLE = 'castle'
    PROMOTION = 'promotion'

    # Translation dictionaries for chess notation
    ROWS_TO_RANKS = {0: '8', 1: '7', 2: '6', 3: '5', 4: '4', 5: '3', 6: '2', 7: '1'}
    COLS_TO_FILES = {0: 'a', 1: 'b', 2: 'c', 3: 'd', 4: 'e', 5: 'f', 6: 'g', 7: 'h'}
    RANKS_TO_ROWS = {rank: row for row, rank in ROWS_TO_RANKS.items()}
    FILES_TO_COLS = {file: col for col, file in COLS_TO_FILES.items()}

    def __init__(
        self,
        start_sq: tuple[int, int],
        end_sq: tuple[int, int],
        board: list[list[int]],
        move_type: str = 'normal',
        promotion_piece: str = 'Q'
    ) -> None:
        """
        Initialize a Move object with its state and properties.

        Parameters
        ----------
        start_sq : tuple of int
            The starting coordinate (row, col) of the move.
        end_sq : tuple of int
            The destination coordinate (row, col) of the move.
        board : list of list of str
            The board array to extract piece information.
        move_type : str, optional
            The special classification of the move. Default is 'normal'.
        promotion_piece : str, optional
            The piece selected if a pawn promotes. Default is 'Q'.
        """
        self.start_row = start_sq[0]
        self.start_col = start_sq[1]
        self.end_row = end_sq[0]
        self.end_col = end_sq[1]

        self.piece_moved = board[self.start_row][self.start_col]
        self.piece_captured = board[self.end_row][self.end_col]

        self.move_type = move_type
        self.promotion_piece = promotion_piece

        if self.move_type == self.EN_PASSANT:
            # The captured piece in en passant is always the opposite color pawn
            self.piece_captured = BP if self.piece_moved == WP else WP

        # Stores ambiguity notation context if evaluated during UI rendering
        self.disambiguation: str = ''

        self.is_check: bool = False
        self.is_checkmate: bool = False

    @classmethod
    def normal(cls, start_sq: tuple[int, int], end_sq: tuple[int, int], board: list[list[int]]) -> 'Move':
        """Construct a standard normal move."""
        return cls(start_sq, end_sq, board, move_type=cls.NORMAL)

    @classmethod
    def en_passant(cls, start_sq: tuple[int, int], end_sq: tuple[int, int], board: list[list[int]]) -> 'Move':
        """Construct an en-passant capture move."""
        return cls(start_sq, end_sq, board, move_type=cls.EN_PASSANT)

    @classmethod
    def castle(cls, start_sq: tuple[int, int], end_sq: tuple[int, int], board: list[list[int]]) -> 'Move':
        """Construct a castling move."""
        return cls(start_sq, end_sq, board, move_type=cls.CASTLE)

    @classmethod
    def promotion(
            cls,
            start_sq: tuple[int, int],
            end_sq: tuple[int, int],
            board: list[list[int]],
            promotion_piece: str = 'Q'
    ) -> 'Move':
        """Construct a pawn promotion move."""
        return cls(
            start_sq,
            end_sq,
            board,
            move_type=cls.PROMOTION,
            promotion_piece=promotion_piece,
        )

    @classmethod
    def from_ai_tuple(cls, move_tuple: tuple[int, int, int, int, int], board: list[list[int]]) -> 'Move':
        """
        Rebuild a full Move object from a lightweight AI move tuple.

        This is the bridge between the AI search layer (which works on
        5-element tuples for speed) and the UI layer (which needs full Move
        objects for animation, notation, and undo support).

        Parameters
        ----------
        move_tuple : tuple of int
            Format: (start_row, start_col, end_row, end_col, move_type)
            Types: 0=Normal, 1=Castle, 2=En Passant, 3=Promo(Q), 4=R, 5=B, 6=N
        board : list of list of str
            The board array *before* the move is executed.

        Returns
        -------
        Move
            The equivalent fully-featured Move object.
        """
        start_row, start_col, end_row, end_col, move_type = move_tuple
        start_sq, end_sq = (start_row, start_col), (end_row, end_col)

        if move_type == 1:
            return cls.castle(start_sq, end_sq, board)
        if move_type == 2:
            return cls.en_passant(start_sq, end_sq, board)
        if move_type >= 3:
            return cls.promotion(start_sq, end_sq, board, promotion_piece=AI_PROMO_PIECES[move_type])
        return cls.normal(start_sq, end_sq, board)

    def to_ai_tuple(self) -> tuple[int, int, int, int, int]:
        """
        Convert this Move into the lightweight 5-element AI tuple format.

        Returns
        -------
        tuple of int
            Format: (start_row, start_col, end_row, end_col, move_type).
        """
        if self.move_type == self.CASTLE:
            code = 1
        elif self.move_type == self.EN_PASSANT:
            code = 2
        elif self.move_type == self.PROMOTION:
            code = AI_PROMO_CODES[self.promotion_piece]
        else:
            code = 0
        return self.start_row, self.start_col, self.end_row, self.end_col, code

    def __eq__(self, other: object) -> bool:
        """Determine equality between this move and another object."""
        if isinstance(other, Move):
            return (
                    self.start_row == other.start_row
                    and self.start_col == other.start_col
                    and self.end_row == other.end_row
                    and self.end_col == other.end_col
                    and self.move_type == other.move_type
                    and self.promotion_piece == other.promotion_piece
            )
        return False

    @property
    def is_pawn_promotion(self) -> bool:
        """Check if the move involves a pawn reaching the furthest rank and promoting."""
        return self.move_type == self.PROMOTION

    @property
    def is_enpassant_move(self) -> bool:
        """Check if the move is a special en-passant diagonal pawn capture."""
        return self.move_type == self.EN_PASSANT

    @property
    def is_castle_move(self) -> bool:
        """Check if the move is a castling maneuver involving both the king and a rook."""
        return self.move_type == self.CASTLE

    def get_chess_notation(self) -> str:
        """
        Construct the algebraic chess notation string for the move.

        Returns
        -------
        str
            The algebraic notation representing the move executed.
        """
        if self.is_castle_move:
            notation = 'O-O' if self.end_col > self.start_col else 'O-O-O'
        else:
            notation = ''

            # Non-pawn piece moves prefix the notation with their letter (N, B, R, Q, K)
            if PIECE_TYPE[self.piece_moved] != PAWN:
                notation = INT_TO_CODE[self.piece_moved][1]
                if self.disambiguation:
                    notation += self.disambiguation

            # Append capture indicator
            if self.piece_captured != EMPTY:
                if PIECE_TYPE[self.piece_moved] == PAWN:
                    notation += self.COLS_TO_FILES[self.start_col]
                notation += 'x'

            # Append standard destination suffix
            notation += self._get_file_rank(self.end_row, self.end_col)

            if self.is_pawn_promotion:
                notation += '=' + self.promotion_piece

        # Append check or checkmate symbols (evaluated post-move)
        if self.is_checkmate:
            notation += '#'
        elif self.is_check:
            notation += '+'

        return notation

    def get_uci_notation(self) -> str:
        """
        Construct the UCI coordinate notation for the move (e.g., 'e2e4', 'e7e8q').

        Returns
        -------
        str
            The UCI string of the move, including a lowercase promotion suffix.
        """
        uci = self._get_file_rank(self.start_row, self.start_col) + self._get_file_rank(self.end_row, self.end_col)
        if self.is_pawn_promotion:
            uci += self.promotion_piece.lower()
        return uci

    def _get_file_rank(self, row: int, col: int) -> str:
        """Convert matrix coordinates to standard board notations (e.g., 'e4')."""
        return self.COLS_TO_FILES[col] + self.ROWS_TO_RANKS[row]


class GameState:
    """
    Stores all information about the current state of the game.

    Determines valid moves at the current state and maintains a log of
    made moves, castling rights, en-passant squares, and Zobrist keys.
    """

    def __init__(self) -> None:
        """Initialize the game state, placing pieces on their starting squares."""
        self.board: list[list[int]] = [
            [BR, BN, BB, BQ, BK, BB, BN, BR],
            [BP, BP, BP, BP, BP, BP, BP, BP],
            [EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY],
            [EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY],
            [EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY],
            [EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY],
            [WP, WP, WP, WP, WP, WP, WP, WP],
            [WR, WN, WB, WQ, WK, WB, WN, WR],
        ]
        self.white_to_move = True

        # Current and home Kings' locations
        self.white_king_location = (7, 4)
        self.black_king_location = (0, 4)
        self.WHITE_KING_HOME_SQUARE = (7, 4)
        self.BLACK_KING_HOME_SQUARE = (0, 4)

        # Track active piece coordinates to optimize move generation
        self.white_pieces: set[tuple[int, int]] = set()
        self.black_pieces: set[tuple[int, int]] = set()
        for row in range(8):
            for col in range(8):
                piece = self.board[row][col]
                if piece != EMPTY:
                    if piece < BP:
                        self.white_pieces.add((row, col))
                    else:
                        self.black_pieces.add((row, col))

        # Game state flags
        self.in_check: bool = False
        self.is_checkmate: bool = False
        self.is_stalemate: bool = False
        self.checks: list[tuple[int, int, int, int]] = []
        self.pins: dict[tuple[int, int], tuple[int, int]] = {}

        self.move_log: list[Move] = []

        # En-passant coordinates
        self.enpassant_possible: tuple[int, int] | None = None
        self.enpassant_possible_log: list[tuple[int, int] | None] = [self.enpassant_possible]

        # Castling rights mapping
        self.white_castle_king_side: bool = True
        self.white_castle_queen_side: bool = True
        self.black_castle_king_side: bool = True
        self.black_castle_queen_side: bool = True
        self.castle_rights_log: list[CastleRights] = [
            CastleRights(
                self.white_castle_king_side,
                self.white_castle_queen_side,
                self.black_castle_king_side,
                self.black_castle_queen_side
            )
        ]

        # Rule tracking logs
        self.halfmove_clock: int = 0
        self.halfmove_clock_log: list[int] = []
        self.state_counts: dict[tuple, int] = {}
        self.state_log: list[tuple] = []

        # Hash and store the absolute initial state configuration
        initial_state: tuple = self.get_board_state()
        self.state_counts[initial_state] = 1
        self.state_log.append(initial_state)

        # Zobrist hashing: incremental 64-bit key of the current position.
        # `zobrist_history` mirrors `state_log` for repetition-aware AI search.
        self.zobrist_key: int = self.compute_zobrist_key()
        self.zobrist_history: list[int] = [self.zobrist_key]

    @property
    def friendly_color(self) -> str:
        """Get the color character of the player whose turn it is."""
        return 'w' if self.white_to_move else 'b'

    @property
    def enemy_color(self) -> str:
        """Get the color character of the opposing player."""
        return 'b' if self.white_to_move else 'w'

    def compute_zobrist_key(self) -> int:
        """
        Compute the full Zobrist hash key of the current position from scratch.

        The key XORs together random 64-bit numbers for every piece placement,
        the side to move, the castling-rights combination, and the en-passant
        file. `make_ai_move()` maintains the same key incrementally, so this
        full scan is only needed at initialization or after arbitrary board
        edits (e.g., loading a FEN or building test fixtures).

        Returns
        -------
        int
            The 64-bit Zobrist key identifying this position.
        """
        key = 0
        board = self.board
        for row in range(8):
            for col in range(8):
                piece = board[row][col]
                if piece != EMPTY:
                    key ^= ZOBRIST_PIECES[piece][row][col]

        if not self.white_to_move:
            key ^= ZOBRIST_SIDE

        key ^= ZOBRIST_CASTLING[self._castle_rights_index()]

        if self.enpassant_possible is not None:
            key ^= ZOBRIST_EP_FILE[self.enpassant_possible[1]]

        return key

    def _castle_rights_index(self) -> int:
        """Pack the four castling-right booleans into a 0-15 table index."""
        return (
            (8 if self.white_castle_king_side else 0)
            | (4 if self.white_castle_queen_side else 0)
            | (2 if self.black_castle_king_side else 0)
            | (1 if self.black_castle_queen_side else 0)
        )

    def refresh_derived_state(self) -> None:
        """
        Recompute all caches derived from the raw board array.

        Call this after directly editing `board`, `white_to_move`, castling
        rights, or `enpassant_possible` (as test fixtures and FEN loading do)
        so the piece sets, king locations, Zobrist key, and repetition logs
        become consistent again.

        Returns
        -------
        None
        """
        self.white_pieces.clear()
        self.black_pieces.clear()
        for row in range(8):
            for col in range(8):
                piece = self.board[row][col]
                if piece != EMPTY:
                    if piece < BP:
                        self.white_pieces.add((row, col))
                        if piece == WK:
                            self.white_king_location = (row, col)
                    else:
                        self.black_pieces.add((row, col))
                        if piece == BK:
                            self.black_king_location = (row, col)

        initial_state = self.get_board_state()
        self.state_counts = {initial_state: 1}
        self.state_log = [initial_state]
        self.zobrist_key = self.compute_zobrist_key()
        self.zobrist_history = [self.zobrist_key]

    @classmethod
    def from_fen(cls, fen: str) -> 'GameState':
        """
        Build a GameState from a FEN (Forsyth-Edwards Notation) string.

        Parameters
        ----------
        fen : str
            A standard 6-field FEN string, e.g.
            'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'.

        Returns
        -------
        GameState
            A fully initialized game state matching the FEN position.

        Raises
        ------
        ValueError
            If the FEN string does not contain the required fields.
        """
        fields = fen.split()
        if len(fields) < 4:
            raise ValueError(f'Invalid FEN (expected at least 4 fields): {fen!r}')

        placement, side, castling, ep = fields[0], fields[1], fields[2], fields[3]
        halfmove = int(fields[4]) if len(fields) > 4 else 0

        gs = cls()
        gs.board = [[EMPTY] * 8 for _ in range(8)]
        for row_index, rank_str in enumerate(placement.split('/')):
            col = 0
            for char in rank_str:
                if char.isdigit():
                    col += int(char)
                else:
                    color = 'w' if char.isupper() else 'b'
                    gs.board[row_index][col] = CODE_TO_INT[color + char.upper()]
                    col += 1

        gs.white_to_move = side == 'w'
        gs.white_castle_king_side = 'K' in castling
        gs.white_castle_queen_side = 'Q' in castling
        gs.black_castle_king_side = 'k' in castling
        gs.black_castle_queen_side = 'q' in castling
        gs.castle_rights_log = [
            CastleRights(
                gs.white_castle_king_side,
                gs.white_castle_queen_side,
                gs.black_castle_king_side,
                gs.black_castle_queen_side
            )
        ]

        if ep != '-':
            gs.enpassant_possible = (Move.RANKS_TO_ROWS[ep[1]], Move.FILES_TO_COLS[ep[0]])
        else:
            gs.enpassant_possible = None
        gs.enpassant_possible_log = [gs.enpassant_possible]

        gs.halfmove_clock = halfmove
        gs.refresh_derived_state()
        return gs

    def to_fen(self) -> str:
        """
        Serialize the current position into a FEN string.

        Returns
        -------
        str
            The 6-field FEN string of the current position. The fullmove
            counter is derived from the move log length.
        """
        rank_strings = []
        for row in range(8):
            rank = ''
            empty_run = 0
            for col in range(8):
                piece = self.board[row][col]
                if piece == EMPTY:
                    empty_run += 1
                else:
                    if empty_run:
                        rank += str(empty_run)
                        empty_run = 0
                    code = INT_TO_CODE[piece]
                    rank += code[1] if piece < BP else code[1].lower()
            if empty_run:
                rank += str(empty_run)
            rank_strings.append(rank)

        castling = ''
        if self.white_castle_king_side: castling += 'K'
        if self.white_castle_queen_side: castling += 'Q'
        if self.black_castle_king_side: castling += 'k'
        if self.black_castle_queen_side: castling += 'q'
        castling = castling or '-'

        if self.enpassant_possible is not None:
            ep = Move.COLS_TO_FILES[self.enpassant_possible[1]] + Move.ROWS_TO_RANKS[self.enpassant_possible[0]]
        else:
            ep = '-'

        fullmove = len(self.move_log) // 2 + 1
        return (
            '/'.join(rank_strings)
            + f' {"w" if self.white_to_move else "b"} {castling} {ep} {self.halfmove_clock} {fullmove}'
        )

    def make_move(self, move: 'Move', annotate: bool = True) -> None:
        """
        Execute a chess move on the board and update the game state.

        Parameters
        ----------
        move : Move
            The Move object containing the details of the play to be executed.
        annotate : bool, optional
            If True, calculates check status for algebraic notation. Set to
            False during evaluation simulations to boost performance.
        """
        self.halfmove_clock_log.append(self.halfmove_clock)

        # Reset clock on pawn advances or active captures
        if PIECE_TYPE[move.piece_moved] == PAWN or move.piece_captured != EMPTY:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        self.enpassant_possible_log.append(self.enpassant_possible)
        self.board[move.start_row][move.start_col] = EMPTY
        self.board[move.end_row][move.end_col] = move.piece_moved
        self.move_log.append(move)
        self.white_to_move = not self.white_to_move

        # Maintain king location caches
        if move.piece_moved == WK:
            self.white_king_location = (move.end_row, move.end_col)
        elif move.piece_moved == BK:
            self.black_king_location = (move.end_row, move.end_col)

        self._update_castle_rights(move)

        if move.move_type == Move.PROMOTION:
            promoted_piece = move.promotion_piece if move.promotion_piece else 'Q'
            color = 'w' if move.piece_moved < BP else 'b'
            self.board[move.end_row][move.end_col] = CODE_TO_INT[color + promoted_piece]

        # Establish en-passant target square on double pawn moves
        if PIECE_TYPE[move.piece_moved] == PAWN and abs(move.start_row - move.end_row) == 2:
            self.enpassant_possible = ((move.start_row + move.end_row) // 2, move.end_col)
        else:
            self.enpassant_possible = None

        if move.move_type == Move.EN_PASSANT:
            self.board[move.start_row][move.end_col] = EMPTY

        # Reposition the rook during a castle move
        if move.move_type == Move.CASTLE:
            if move.end_col - move.start_col == 2:  # King side
                self.board[move.end_row][move.end_col - 1] = self.board[move.end_row][move.end_col + 1]
                self.board[move.end_row][move.end_col + 1] = EMPTY
            else:  # Queen side
                self.board[move.end_row][move.end_col + 1] = self.board[move.end_row][move.end_col - 2]
                self.board[move.end_row][move.end_col - 2] = EMPTY

        # Keep active piece sets updated
        _is_white_moved = not self.white_to_move
        friendly_pieces = self.white_pieces if _is_white_moved else self.black_pieces
        enemy_pieces = self.black_pieces if _is_white_moved else self.white_pieces

        friendly_pieces.remove((move.start_row, move.start_col))
        friendly_pieces.add((move.end_row, move.end_col))

        if move.piece_captured != EMPTY:
            if move.move_type == Move.EN_PASSANT:
                enemy_pieces.remove((move.start_row, move.end_col))
            else:
                enemy_pieces.remove((move.end_row, move.end_col))

        if move.move_type == Move.CASTLE:
            if move.end_col - move.start_col == 2:  # King side
                friendly_pieces.remove((move.end_row, move.end_col + 1))
                friendly_pieces.add((move.end_row, move.end_col - 1))
            else:  # Queen side
                friendly_pieces.remove((move.end_row, move.end_col - 2))
                friendly_pieces.add((move.end_row, move.end_col + 1))

        if annotate:
            in_check, _, _ = self.check_pins_checks()
            move.is_check = in_check

        # Log state for threefold repetition tracking
        current_state = self.get_board_state()
        self.state_log.append(current_state)
        self.state_counts[current_state] = self.state_counts.get(current_state, 0) + 1

        # UI moves are rare relative to search nodes, so a full Zobrist
        # recompute here is simpler than a second incremental update path
        self.zobrist_key = self.compute_zobrist_key()
        self.zobrist_history.append(self.zobrist_key)

    def unmake_move(self) -> None:
        """
        Undo the last move made in the game.

        Restores the board, turn, castling rights, and internal tracking sets
        to their exact state before the previous move was executed.
        """
        if not self.move_log:
            return

        # Revert layout frequency and clocks
        current_state = self.state_log.pop()
        self.state_counts[current_state] -= 1
        if self.state_counts[current_state] == 0:
            del self.state_counts[current_state]

        self.zobrist_history.pop()
        self.zobrist_key = self.zobrist_history[-1]

        self.halfmove_clock = self.halfmove_clock_log.pop()

        last_move = self.move_log.pop()
        self.board[last_move.start_row][last_move.start_col] = last_move.piece_moved
        self.board[last_move.end_row][last_move.end_col] = last_move.piece_captured
        self.white_to_move = not self.white_to_move

        if last_move.piece_moved == WK:
            self.white_king_location = (last_move.start_row, last_move.start_col)
        elif last_move.piece_moved == BK:
            self.black_king_location = (last_move.start_row, last_move.start_col)

        if last_move.move_type == Move.EN_PASSANT:
            self.board[last_move.end_row][last_move.end_col] = EMPTY
            self.board[last_move.start_row][last_move.end_col] = last_move.piece_captured

        self.enpassant_possible = self.enpassant_possible_log.pop()

        # Restore previous castling rights
        self.castle_rights_log.pop()
        castle_rights = self.castle_rights_log[-1]
        self.white_castle_king_side = castle_rights.white_king_side
        self.white_castle_queen_side = castle_rights.white_queen_side
        self.black_castle_king_side = castle_rights.black_king_side
        self.black_castle_queen_side = castle_rights.black_queen_side

        # Return rook to origin if castled
        if last_move.move_type == Move.CASTLE:
            if last_move.end_col - last_move.start_col == 2:  # King side
                self.board[last_move.end_row][last_move.end_col + 1] = self.board[last_move.end_row][last_move.end_col - 1]
                self.board[last_move.end_row][last_move.end_col - 1] = EMPTY
            else:  # Queen side
                self.board[last_move.end_row][last_move.end_col - 2] = self.board[last_move.end_row][last_move.end_col + 1]
                self.board[last_move.end_row][last_move.end_col + 1] = EMPTY

        # Restore pieces' tracking sets
        friendly_pieces = self.white_pieces if self.white_to_move else self.black_pieces
        enemy_pieces = self.black_pieces if self.white_to_move else self.white_pieces

        friendly_pieces.remove((last_move.end_row, last_move.end_col))
        friendly_pieces.add((last_move.start_row, last_move.start_col))

        if last_move.piece_captured != EMPTY:
            if last_move.move_type == Move.EN_PASSANT:
                enemy_pieces.add((last_move.start_row, last_move.end_col))
            else:
                enemy_pieces.add((last_move.end_row, last_move.end_col))

        if last_move.move_type == Move.CASTLE:
            if last_move.end_col - last_move.start_col == 2:  # King side
                friendly_pieces.remove((last_move.end_row, last_move.end_col - 1))
                friendly_pieces.add((last_move.end_row, last_move.end_col + 1))
            else:  # Queen side
                friendly_pieces.remove((last_move.end_row, last_move.end_col + 1))
                friendly_pieces.add((last_move.end_row, last_move.end_col - 2))

    def get_board_state(self) -> tuple:
        """
        Generate a unique, immutable representation of the current board state.

        Converts the 2D mutable board list into a nested tuple. Using tuples
        eliminates memory allocation overhead during the AI's deep tree search
        and functions reliably as a hashable dictionary key.

        Returns
        -------
        tuple
            (board_tuple, enpassant, wks, wqs, bks, bqs, white_to_move).
        """
        board_tuple = tuple(tuple(row) for row in self.board)
        return (
            board_tuple,
            self.enpassant_possible,
            self.white_castle_king_side,
            self.white_castle_queen_side,
            self.black_castle_king_side,
            self.black_castle_queen_side,
            self.white_to_move
        )

    def make_ai_move(
            self,
            move_tuple: tuple[int, int, int, int, int]
    ) -> tuple[int, tuple[int, int] | None, tuple[bool, bool, bool, bool], int, int]:
        """
        Execute a lightweight move specifically optimized for AI search trees.

        Maintains the board array, piece tracking sets, king locations,
        castling rights, en-passant square, the incremental Zobrist key, and
        `halfmove_clock`. It deliberately does NOT update `move_log` or
        `state_counts`; the search layer tracks repetitions via Zobrist keys.

        `halfmove_clock` used to be skipped here too, on the reasoning that
        the search only needed repetitions. That was wrong, and it cost real
        games: with no halfmove clock the search cannot see the 50-move rule,
        so in a won position with no progress move it happily scored +300
        while the game drifted to a draw — and since equal-scored root moves
        are shuffled, it did so by playing what looked like random moves. One
        int in the undo package buys the search the whole rule.

        Parameters
        ----------
        move_tuple : tuple of int
            Format: (start_row, start_col, end_row, end_col, move_type)
            Types: 0=Normal, 1=Castle, 2=En Passant, 3=Promo(Q), 4=R, 5=B, 6=N

        Returns
        -------
        tuple
            An undo package structured as (captured_piece, old_enpassant,
            old_castle_rights_tuple, old_zobrist_key, old_halfmove_clock).
        """
        start_row, start_col, end_row, end_col, move_type = move_tuple
        board = self.board

        piece_moved = board[start_row][start_col]
        captured_piece = board[end_row][end_col]

        old_castle_rights = (
            self.white_castle_king_side, self.white_castle_queen_side,
            self.black_castle_king_side, self.black_castle_queen_side
        )
        old_enpassant = self.enpassant_possible
        old_zobrist = self.zobrist_key
        old_halfmove_clock = self.halfmove_clock
        old_rights_index = self._castle_rights_index()

        # Same rule `make_move` applies: a pawn advance or a capture is
        # irreversible progress and restarts the count. Move types 3-6 are
        # promotions, which are pawn moves; type 2 is en passant, which is
        # both. Read from the board rather than trusting the type alone.
        if PIECE_TYPE[piece_moved] == PAWN or captured_piece != EMPTY:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock = old_halfmove_clock + 1

        is_white = piece_moved < BP
        friendly_pieces = self.white_pieces if is_white else self.black_pieces
        enemy_pieces = self.black_pieces if is_white else self.white_pieces

        board[start_row][start_col] = EMPTY
        board[end_row][end_col] = piece_moved
        friendly_pieces.remove((start_row, start_col))
        friendly_pieces.add((end_row, end_col))

        key = old_zobrist ^ ZOBRIST_PIECES[piece_moved][start_row][start_col]

        if piece_moved == WK:
            self.white_king_location = (end_row, end_col)
            self.white_castle_king_side = False
            self.white_castle_queen_side = False
        elif piece_moved == BK:
            self.black_king_location = (end_row, end_col)
            self.black_castle_king_side = False
            self.black_castle_queen_side = False

        if move_type == 1:  # Castle
            if end_col - start_col == 2:  # King side
                rook = board[end_row][end_col + 1]
                board[end_row][end_col - 1] = rook
                board[end_row][end_col + 1] = EMPTY
                friendly_pieces.remove((end_row, end_col + 1))
                friendly_pieces.add((end_row, end_col - 1))
                key ^= ZOBRIST_PIECES[rook][end_row][end_col + 1] ^ ZOBRIST_PIECES[rook][end_row][end_col - 1]
            else:  # Queen side
                rook = board[end_row][end_col - 2]
                board[end_row][end_col + 1] = rook
                board[end_row][end_col - 2] = EMPTY
                friendly_pieces.remove((end_row, end_col - 2))
                friendly_pieces.add((end_row, end_col + 1))
                key ^= ZOBRIST_PIECES[rook][end_row][end_col - 2] ^ ZOBRIST_PIECES[rook][end_row][end_col + 1]
            key ^= ZOBRIST_PIECES[piece_moved][end_row][end_col]

        elif move_type == 2:  # En Passant
            board[start_row][end_col] = EMPTY
            captured_piece = BP if is_white else WP
            enemy_pieces.remove((start_row, end_col))
            key ^= ZOBRIST_PIECES[captured_piece][start_row][end_col]
            key ^= ZOBRIST_PIECES[piece_moved][end_row][end_col]

        elif move_type >= 3:  # Promotions
            promoted = AI_PROMO_TYPE[move_type] + (0 if is_white else 6)
            board[end_row][end_col] = promoted
            if captured_piece != EMPTY:
                enemy_pieces.remove((end_row, end_col))
                key ^= ZOBRIST_PIECES[captured_piece][end_row][end_col]
            key ^= ZOBRIST_PIECES[promoted][end_row][end_col]

        else:  # Normal moves
            if captured_piece != EMPTY:
                enemy_pieces.remove((end_row, end_col))
                key ^= ZOBRIST_PIECES[captured_piece][end_row][end_col]
            key ^= ZOBRIST_PIECES[piece_moved][end_row][end_col]

        if PIECE_TYPE[piece_moved] == PAWN and abs(start_row - end_row) == 2:
            self.enpassant_possible = ((start_row + end_row) // 2, end_col)
        else:
            self.enpassant_possible = None

        if PIECE_TYPE[piece_moved] == ROOK:
            if start_row == 7:
                if start_col == 0: self.white_castle_queen_side = False
                elif start_col == 7: self.white_castle_king_side = False
            elif start_row == 0:
                if start_col == 0: self.black_castle_queen_side = False
                elif start_col == 7: self.black_castle_king_side = False

        if captured_piece != EMPTY and PIECE_TYPE[captured_piece] == ROOK:
            if end_row == 7:
                if end_col == 0: self.white_castle_queen_side = False
                elif end_col == 7: self.white_castle_king_side = False
            elif end_row == 0:
                if end_col == 0: self.black_castle_queen_side = False
                elif end_col == 7: self.black_castle_king_side = False

        self.white_to_move = not self.white_to_move

        # Finalize the incremental Zobrist key: side, castling delta, EP files
        key ^= ZOBRIST_SIDE
        new_rights_index = self._castle_rights_index()
        if new_rights_index != old_rights_index:
            key ^= ZOBRIST_CASTLING[old_rights_index] ^ ZOBRIST_CASTLING[new_rights_index]
        if old_enpassant is not None:
            key ^= ZOBRIST_EP_FILE[old_enpassant[1]]
        if self.enpassant_possible is not None:
            key ^= ZOBRIST_EP_FILE[self.enpassant_possible[1]]
        self.zobrist_key = key

        return (captured_piece, old_enpassant, old_castle_rights, old_zobrist,
                old_halfmove_clock)

    def unmake_ai_move(
            self,
            move_tuple: tuple[int, int, int, int, int],
            undo_package: tuple[int, tuple[int, int] | None,
                                tuple[bool, bool, bool, bool], int, int]
    ) -> None:
        """
        Reverse state changes made by `make_ai_move` directly using primitive data.

        Parameters
        ----------
        move_tuple : tuple of int
            The exact move tuple originally passed to `make_ai_move`.
        undo_package : tuple
            The package returned by the corresponding `make_ai_move` call.
        """
        start_row, start_col, end_row, end_col, move_type = move_tuple
        (captured_piece, old_enpassant, old_castle_rights, old_zobrist,
         old_halfmove_clock) = undo_package
        board = self.board

        self.halfmove_clock = old_halfmove_clock
        self.white_to_move = not self.white_to_move
        piece_moved = board[end_row][end_col]

        if move_type >= 3:  # Promotion: the piece that moved was a pawn
            piece_moved = WP if piece_moved < BP else BP

        is_white = piece_moved < BP
        friendly_pieces = self.white_pieces if is_white else self.black_pieces
        enemy_pieces = self.black_pieces if is_white else self.white_pieces

        board[start_row][start_col] = piece_moved
        friendly_pieces.remove((end_row, end_col))
        friendly_pieces.add((start_row, start_col))

        if move_type == 2:  # En Passant
            board[end_row][end_col] = EMPTY
            board[start_row][end_col] = captured_piece
            enemy_pieces.add((start_row, end_col))
        else:
            board[end_row][end_col] = captured_piece
            if captured_piece != EMPTY:
                enemy_pieces.add((end_row, end_col))

        if move_type == 1:  # Castle
            if end_col - start_col == 2:
                board[end_row][end_col + 1] = board[end_row][end_col - 1]
                board[end_row][end_col - 1] = EMPTY
                friendly_pieces.remove((end_row, end_col - 1))
                friendly_pieces.add((end_row, end_col + 1))
            else:
                board[end_row][end_col - 2] = board[end_row][end_col + 1]
                board[end_row][end_col + 1] = EMPTY
                friendly_pieces.remove((end_row, end_col + 1))
                friendly_pieces.add((end_row, end_col - 2))

        if piece_moved == WK:
            self.white_king_location = (start_row, start_col)
        elif piece_moved == BK:
            self.black_king_location = (start_row, start_col)

        self.enpassant_possible = old_enpassant
        (self.white_castle_king_side, self.white_castle_queen_side,
         self.black_castle_king_side, self.black_castle_queen_side) = old_castle_rights
        self.zobrist_key = old_zobrist

    def make_null_move(self) -> tuple[tuple[int, int] | None, int]:
        """
        Pass the turn without moving a piece (for null-move pruning).

        The side to move flips and the en-passant square clears, exactly as
        if the player "skipped" their turn. Only legal inside AI search.

        Returns
        -------
        tuple
            (old_enpassant, old_zobrist_key) to feed into `unmake_null_move`.
        """
        old_enpassant = self.enpassant_possible
        old_zobrist = self.zobrist_key

        key = old_zobrist ^ ZOBRIST_SIDE
        if old_enpassant is not None:
            key ^= ZOBRIST_EP_FILE[old_enpassant[1]]

        self.enpassant_possible = None
        self.white_to_move = not self.white_to_move
        self.zobrist_key = key
        return old_enpassant, old_zobrist

    def unmake_null_move(self, undo_package: tuple[tuple[int, int] | None, int]) -> None:
        """
        Reverse a `make_null_move` call.

        Parameters
        ----------
        undo_package : tuple
            The package returned by the corresponding `make_null_move` call.
        """
        old_enpassant, old_zobrist = undo_package
        self.white_to_move = not self.white_to_move
        self.enpassant_possible = old_enpassant
        self.zobrist_key = old_zobrist

    def check_pins_checks(self) -> tuple[
        bool,
        dict[tuple[int, int], tuple[int, int]],
        list[tuple[int, int, int, int]]
    ]:
        """
        Scan outward from the king to identify active checks and absolute pins.

        Returns
        -------
        tuple
            (in_check: bool, pins: dict[(row, col): (d_row, d_col)], checks: list[(row, col, d_row, d_col)])
        """
        pins: dict[tuple[int, int], tuple[int, int]] = {}
        checks: list[tuple[int, int, int, int]] = []
        in_check = False
        row, col = self.white_king_location if self.white_to_move else self.black_king_location
        board = self.board
        white = self.white_to_move
        # Friendly pieces occupy one 1-6/7-12 band, the enemy the other; empty
        # squares (0) fall in neither. `enemy_is_black` selects the pawn-attack
        # directions, and `enemy_knight` is the single enemy knight code.
        if white:
            friendly_lo, friendly_hi, enemy_lo, enemy_hi = WP, WK, BP, BK
            enemy_knight = BN
        else:
            friendly_lo, friendly_hi, enemy_lo, enemy_hi = BP, BK, WP, WK
            enemy_knight = WN
        enemy_is_black = white

        for i, d in enumerate(ALL_DIRECTIONS):
            possible_pins: tuple = ()
            for j in range(1, 8):
                end_row = row + d[0] * j
                end_col = col + d[1] * j
                if 0 <= end_row < 8 and 0 <= end_col < 8:
                    end_piece = board[end_row][end_col]

                    # Ignore moving phantom King to prevent false blocks
                    if friendly_lo <= end_piece <= friendly_hi and PIECE_TYPE[end_piece] != KING:
                        if len(possible_pins) == 0:
                            possible_pins = (end_row, end_col, d[0], d[1])
                        else:
                            break
                    elif enemy_lo <= end_piece <= enemy_hi:
                        enemy_piece_type = PIECE_TYPE[end_piece]

                        if (
                            (0 <= i <= 3 and enemy_piece_type == ROOK)
                            or (4 <= i <= 7 and enemy_piece_type == BISHOP)
                            or (
                                j == 1
                                and (
                                        (enemy_is_black and 4 <= i <= 5)
                                        or (not enemy_is_black and 6 <= i <= 7)
                                )
                                and enemy_piece_type == PAWN
                            )
                            or (enemy_piece_type == QUEEN)
                            or (j == 1 and enemy_piece_type == KING)
                        ):
                            if len(possible_pins) == 0:
                                in_check = True
                                checks.append((end_row, end_col, d[0], d[1]))
                                break
                            else:
                                pins[(possible_pins[0], possible_pins[1])] = (possible_pins[2], possible_pins[3])
                                break
                        else:
                            break
                else:
                    break

        # Check for Knight attacks (they bypass pins)
        for move in KNIGHT_DELTAS:
            end_row = row + move[0]
            end_col = col + move[1]
            if 0 <= end_row < 8 and 0 <= end_col < 8:
                if board[end_row][end_col] == enemy_knight:
                    in_check = True
                    checks.append((end_row, end_col, move[0], move[1]))
                    break

        return in_check, pins, checks

    def side_to_move_in_check(self) -> bool:
        """
        Test whether the side to move is currently in check.

        Unlike the cached ``in_check`` attribute — which is only refreshed by
        ``get_valid_moves()`` and is therefore stale straight after a
        ``make_ai_move()`` — this recomputes the answer on demand with a single
        attack scan. The search uses it right after making a move to ask "did
        that move give check?", the cheap way, without generating the reply
        moves it would need for the cached flag.

        Returns
        -------
        bool
            True when the side to move's king stands on an attacked square.
        """
        row, col = (self.white_king_location if self.white_to_move
                    else self.black_king_location)
        return self.is_square_attacked(row, col)

    def is_square_attacked(self, row: int, col: int) -> bool:
        """
        Determine if a specific square is under attack by any enemy piece.
        Optimized for generating legal king bounds quickly.
        """
        board = self.board
        white = self.white_to_move
        if white:
            friendly_lo, friendly_hi, enemy_lo, enemy_hi = WP, WK, BP, BK
            enemy_knight = BN
        else:
            friendly_lo, friendly_hi, enemy_lo, enemy_hi = BP, BK, WP, WK
            enemy_knight = WN
        enemy_is_black = white

        for i, d in enumerate(ALL_DIRECTIONS):
            for j in range(1, 8):
                end_row = row + d[0] * j
                end_col = col + d[1] * j
                if 0 <= end_row < 8 and 0 <= end_col < 8:
                    end_piece = board[end_row][end_col]

                    if friendly_lo <= end_piece <= friendly_hi and PIECE_TYPE[end_piece] != KING:
                        break
                    elif enemy_lo <= end_piece <= enemy_hi:
                        enemy_piece_type = PIECE_TYPE[end_piece]

                        if 0 <= i <= 3 and enemy_piece_type in (ROOK, QUEEN):
                            return True
                        elif 4 <= i <= 7 and enemy_piece_type in (BISHOP, QUEEN):
                            return True
                        elif j == 1 and enemy_piece_type == PAWN:
                            if enemy_is_black and 4 <= i <= 5: return True
                            elif not enemy_is_black and 6 <= i <= 7: return True
                            # A pawn that does not attack this square still
                            # blocks the ray for any slider behind it
                            break
                        elif j == 1 and enemy_piece_type == KING:
                            return True
                        else:
                            break
                else:
                    break

        for m in KNIGHT_DELTAS:
            end_row = row + m[0]
            end_col = col + m[1]
            if 0 <= end_row < 8 and 0 <= end_col < 8:
                if board[end_row][end_col] == enemy_knight:
                    return True

        return False

    def squares_safe_for_castle(self, squares: list[tuple[int, int]]) -> bool:
        """Check if castling squares are free from enemy attacks."""
        for square in squares:
            if self.is_square_attacked(square[0], square[1]):
                return False
        return True

    def _update_castle_rights(self, move: 'Move') -> None:
        """Update castling privileges after kings or rooks abandon initial squares."""
        if move.piece_moved == WK:
            self.white_castle_king_side = False
            self.white_castle_queen_side = False
        elif move.piece_moved == BK:
            self.black_castle_king_side = False
            self.black_castle_queen_side = False
        elif move.piece_moved == WR:
            if move.start_row == 7:
                if move.start_col == 0: self.white_castle_queen_side = False
                elif move.start_col == 7: self.white_castle_king_side = False
        elif move.piece_moved == BR:
            if move.start_row == 0:
                if move.start_col == 0: self.black_castle_queen_side = False
                elif move.start_col == 7: self.black_castle_king_side = False

        if move.piece_captured == WR:
            if move.end_row == 7:
                if move.end_col == 0: self.white_castle_queen_side = False
                elif move.end_col == 7: self.white_castle_king_side = False
        elif move.piece_captured == BR:
            if move.end_row == 0:
                if move.end_col == 0: self.black_castle_queen_side = False
                elif move.end_col == 7: self.black_castle_king_side = False

        self.castle_rights_log.append(
            CastleRights(
                self.white_castle_king_side,
                self.white_castle_queen_side,
                self.black_castle_king_side,
                self.black_castle_queen_side
            )
        )
