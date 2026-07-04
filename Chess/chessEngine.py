"""
This class is responsible for:
- Storing all the information about the current state of the game.
- Determining the valid moves at the current state.
- Keeping the move log.
"""

class GameState:
    def __init__(self):
        # The board is a 8x8 2D list, each element of the list has 2 characters:
        # The 1st char indicates the color of the piece ('b' OR 'w')
        # The 2nd char indicates the type of the piece ('B', 'K', 'N', 'P', 'Q', and 'R')
        # '--' represents an empty square
        self.board = [
            ['bR', 'bN', 'bB', 'bQ', 'bK', 'bB', 'bN', 'bR'],
            ['bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP'],
            ['--', '--', '--', '--', '--', '--', '--', '--'],
            ['--', '--', '--', '--', '--', '--', '--', '--'],
            ['--', '--', '--', '--', '--', '--', '--', '--'],
            ['--', '--', '--', '--', '--', '--', '--', '--'],
            ['wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP'],
            ['wR', 'wN', 'wB', 'wQ', 'wK', 'wB', 'wN', 'wR']
        ]
        self.white_to_move = True
        self.move_log = []

    def make_move(self, move):
        self.board[move.start_row][move.start_col] = '--'
        self.board[move.end_row][move.end_col] = move.piece_moved
        self.move_log.append(move)
        self.white_to_move = not self.white_to_move

    def unmake_move(self):
        if len(self.move_log) != 0: # There are move to unmake
            last_move = self.move_log.pop()
            self.board[last_move.start_row][last_move.start_col] = last_move.piece_moved
            self.board[last_move.end_row][last_move.end_col] = last_move.piece_captured
            self.white_to_move = not self.white_to_move 

class Move:
    # Dictionary to translate rows and cols to ranks and files of chess notation
    rows_to_ranks = {
        0: '8', 1: '7', 2: '6', 3: '5',
        4: '4', 5: '3', 6: '2', 7: '1',
    }
    cols_to_files = {
        0: 'a', 1: 'b', 2: 'c', 3: 'd',
        4: 'e', 5: 'f', 6: 'g', 7: 'h',
    }

    def __init__(self, start_sq, end_sq, board):
        self.start_row = start_sq[0]
        self.start_col = start_sq[1]

        self.end_row = end_sq[0]
        self.end_col = end_sq[1]

        self.piece_moved = board[self.start_row][self.start_col]
        self.piece_captured = board[self.end_row][self.end_col]

    def get_file_rank(self, row, col):
        return self.cols_to_files[col] + self.rows_to_ranks[row]

    def get_chess_notation(self):
        notation = ''
        if self.piece_moved and self.piece_moved[1] != 'P':
            notation = self.piece_moved[1]
        else:
            notation = ''

        if self.piece_captured != '--':
            notation += 'x'

        # PLAN: Cover the checkmate with an '#' at the end of the notation

        # Standard chess notation:
        # 1.e4 e5 2.Qh5?! Nc6 3.Bc4 Nf6?? 4.Qxf7#
        return notation + self.get_file_rank(self.end_row, self.end_col)