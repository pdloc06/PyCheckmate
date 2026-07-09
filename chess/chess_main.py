"""
Main driver for the chess program.

This module handles user input (mouse clicks and keyboard events) and
displays the current GameState object using pygame. It manages the game
loop, graphics rendering, and move animations.
"""
import pygame as pg
from chess import chess_engine

WIDTH = HEIGHT = 512
DIMENSION = 8 # Dimensions of a chess board are 8x8
SQ_SIZE = WIDTH // DIMENSION
MAX_FPS = 20
ANIMATION_FPS = 60
IMAGES = {} # Storing chess pieces' images
board_colors = [pg.Color('white'), pg.Color('grey')]

def main() -> None:
    """
    Initialize pygame, handle user input, and update graphics.

    This function sets up the game loop, captures mouse and keyboard events
    to execute moves, undo moves, or reset the game, and continuously redraws
    the updated game state.
    """
    pg.init()

    screen = pg.display.set_mode((WIDTH, HEIGHT))
    clock = pg.time.Clock()
    screen.fill(pg.Color('white'))

    gs = chess_engine.GameState()
    valid_moves = gs.get_valid_moves()
    move_made = False  # Flag variable, preventing overrun of the gs.get_valid_moves() function
    move_unmake = False  # Flag variable for reversing the animation for unmade a move
    move_to_unmake = None

    load_images()  # Only load images once, before entering the while loop

    running = True
    game_over = False

    sq_selected = None  # No square is selected initially. Tuple: (row, col)
    player_clicks = []  # Keep track of the player clicks. List of two tuples: [(row, col), (row1, col1)]

    while running:
        for e in pg.event.get():
            if e.type == pg.QUIT:
                running = False
            # Mouse handle
            elif e.type == pg.MOUSEBUTTONDOWN:
                if not game_over:
                    location = pg.mouse.get_pos()  # (x, y) location of the mouse
                    col = location[0] // SQ_SIZE
                    row = location[1] // SQ_SIZE

                    if sq_selected == (row, col):  # User clicked the same square twice
                        sq_selected = None  # Deselect and clear player clicks
                        player_clicks = []
                    else:
                        sq_selected = (row, col)
                        print(sq_selected)
                        player_clicks.append(sq_selected)  # Store the square player selected

                    if len(player_clicks) == 2:
                        for move in valid_moves:
                            if (
                                move.start_row == player_clicks[0][0]
                                and move.start_col == player_clicks[0][1]
                                and move.end_row == player_clicks[1][0]
                                and move.end_col == player_clicks[1][1]
                            ):
                                gs.make_move(move)
                                print(move.get_chess_notation())
                                move_made = True
                                sq_selected = None  # Deselect and clear player clicks
                                player_clicks = []
                                break
                        if not move_made:
                            player_clicks = [sq_selected]
            # Key handle
            elif e.type == pg.KEYDOWN:
                if e.key == pg.K_z and (e.mod & (pg.KMOD_META | pg.KMOD_CTRL)):  # CMD/CTRL + Z to undo last move
                    if len(gs.move_log) != 0:
                        move_to_unmake = gs.move_log[-1]  # Store move information for animate unmaking the move
                        gs.unmake_move()
                        move_made = True  # Considering unmake_move() equals to make a (reverse) move
                        move_unmake = True  # Reverse move flag
                if e.key == pg.K_r and (e.mod & (pg.KMOD_META | pg.KMOD_CTRL)):  # CMD/CTRL + R to restart game
                    # Reset everything
                    gs = chess_engine.GameState()
                    valid_moves = gs.get_valid_moves()
                    move_made = False
                    move_unmake = False
                    move_to_unmake = None
                    sq_selected = None
                    player_clicks = []

        if move_made:  # Regenerate the valid moves after the move is made
            if move_unmake:
                animate_move(move_to_unmake, screen, gs.board, clock, move_unmake=move_unmake)
            else:
                animate_move(gs.move_log[-1], screen, gs.board, clock)
            valid_moves = gs.get_valid_moves()
            # Reset flags and temp variable for move
            move_made = False
            move_unmake = False
            move_to_unmake = []

        draw_game_state(screen, gs, valid_moves, sq_selected)

        if gs.is_checkmate:
            game_over = True
            if gs.white_to_move:
                white_wins = False
                winning_animation(screen, gs, white_wins)
            else:
                white_wins = True
                winning_animation(screen, gs, white_wins)
        elif gs.is_stalemate:
            game_over = True
            stalemate_animation(screen, gs)

        clock.tick(MAX_FPS)
        pg.display.flip()

def draw_game_state(
    screen: pg.Surface,
    gs: chess_engine.GameState,
    valid_moves: list[chess_engine.Move],
    sq_selected: tuple[int, int] | None
) -> None:
    """
    Render all graphics for the current game state.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    gs : GameState
        The current state of the game containing board information.
    valid_moves : list of Move
        The list of currently valid moves for highlighting.
    sq_selected : tuple of int or None
        The currently selected square coordinates as (row, col), or None if
        no square is currently selected.
    """
    draw_board(screen)
    highlight_last_move(screen, gs)
    highlight_current_square(screen, gs, valid_moves, sq_selected)
    draw_pieces(screen, gs.board)

def highlight_current_square(
    screen: pg.Surface,
    gs: chess_engine.GameState,
    valid_moves: list[chess_engine.Move],
    sq_selected: tuple[int, int] | None
) -> None:
    """
    Highlight the currently selected square and possible destinations.

    Highlights the selected square in yellow and adds a transparent
    green circle in the middle of each valid destination square.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    gs : GameState
        The current state of the game containing board information.
    valid_moves : list of Move
        The list of currently valid moves for the selected piece.
    sq_selected : tuple of int or None
        The currently selected square coordinates as (row, col), or None if
        no square is currently selected.
    """
    if sq_selected is not None:
        row, col = sq_selected
        if gs.board[row][col][0] == gs.friendly_color:  # Check if sq_selected contains a piece that can be moved
            # Highlight selected square
            current_sq = pg.Surface((SQ_SIZE, SQ_SIZE))
            current_sq.set_alpha(100)  # Transparency value: 0 --> 255 (max, solid)
            current_sq.fill(pg.Color('yellow'))
            screen.blit(current_sq, (col * SQ_SIZE, row * SQ_SIZE))

            # Gray circles for possible move
            for move in valid_moves:
                if move.start_row == row and move.start_col == col:
                    movable_indicator = pg.Surface((SQ_SIZE, SQ_SIZE), pg.SRCALPHA)
                    movable_indicator.fill((0, 0, 0, 0))  # Fill with transparent color
                    x_center = SQ_SIZE // 2
                    y_center = SQ_SIZE // 2
                    radius = SQ_SIZE // 6
                    transparent_green = (100, 180, 120, 175)  # (R, G, B, Alpha)
                    pg.draw.circle(movable_indicator, transparent_green, (x_center, y_center), radius)
                    screen.blit(movable_indicator, (move.end_col * SQ_SIZE, move.end_row * SQ_SIZE))

def highlight_last_move(screen: pg.Surface, gs: chess_engine.GameState) -> None:
    """
    Highlight the starting and ending squares of the last move.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    gs : GameState
        The current state of the game containing the move log.
    """
    if len(gs.move_log) > 0:  # Check if there are any moves in the log
        last_move = gs.move_log[-1]
        # Create a yellow surface with transparency
        highlight_sq = pg.Surface((SQ_SIZE, SQ_SIZE))
        highlight_sq.set_alpha(100)
        highlight_sq.fill(pg.Color('yellow'))
        # Highlight the starting square of the move (start_row, start_col)
        start_x = last_move.start_col * SQ_SIZE
        start_y = last_move.start_row * SQ_SIZE
        screen.blit(highlight_sq, (start_x, start_y))
        # Highlight the ending square of the move (end_row, end_col)
        end_x = last_move.end_col * SQ_SIZE
        end_y = last_move.end_row * SQ_SIZE
        screen.blit(highlight_sq, (end_x, end_y))

def draw_board(screen: pg.Surface) -> None:
    """
    Draw the checkered squares on the board.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    """
    global board_colors
    for row in range(DIMENSION):
        for col in range(DIMENSION):
            color = board_colors[((row + col) % 2)]
            pg.draw.rect(screen, color, pg.Rect(col * SQ_SIZE, row * SQ_SIZE, SQ_SIZE, SQ_SIZE))

def draw_pieces(screen: pg.Surface, board: list[list[str]]) -> None:
    """
    Draw the chess pieces on the board using the current game state board.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    board : list of list of str
        The 2D array representing the current board arrangement.
    """
    for row in range(DIMENSION):
        for col in range(DIMENSION):
            piece = board[row][col]
            if piece != '--':  # Not empty square
                screen.blit(IMAGES[piece], pg.Rect(col * SQ_SIZE, row * SQ_SIZE, SQ_SIZE, SQ_SIZE))

def load_images(pieces_type: str = 'standard') -> None:
    """
    Initialize a global dictionary of images and load piece assets.

    Parameters
    ----------
    pieces_type : str, optional
        The type/folder name of the piece graphics to load. Default is 'standard'.
    """
    # PLAN: Add switch pieces' type feature
    pieces = ['bB', 'bK', 'bN', 'bP', 'bQ', 'bR', 'wB', 'wK', 'wN', 'wP', 'wQ', 'wR']
    for piece in pieces:
        IMAGES[piece] = pg.transform.smoothscale(
            pg.image.load('pieces/' + pieces_type + '/' + piece + '.png'),
            (SQ_SIZE, SQ_SIZE),
        )

def animate_move(
    move: chess_engine.Move,
    screen: pg.Surface,
    board: list[list[str]],
    clock: pg.time.Clock,
    move_unmake: bool = False
) -> None:
    """
    Animate a move on the board from its starting square to its ending square.

    Parameters
    ----------
    move : Move
        The move to animate.
    screen : pygame.Surface
        The main display surface to draw on.
    board : list of list of str
        The current 2D board state array.
    clock : pygame.time.Clock
        The game clock used to manage frame rates.
    move_unmake : bool, optional
        Flag indicating if the animation is reversing an unmade move. Default is False.
    """
    global board_colors
    # Locate the starting and ending squares based on the move_unmake flag
    if move_unmake:
        anim_start_row, anim_start_col = move.end_row, move.end_col
        anim_end_row, anim_end_col = move.start_row, move.start_col
        erase_row, erase_col = move.start_row, move.start_col
    else:
        anim_start_row, anim_start_col = move.start_row, move.start_col
        anim_end_row, anim_end_col = move.end_row, move.end_col
        erase_row, erase_col = move.end_row, move.end_col

    # Calculate distance
    row_distance = anim_end_row - anim_start_row
    col_distance = anim_end_col - anim_start_col

    frames_per_square = 5  # PLAN: Add feature to adjust animation speed
    frame_count = (abs(row_distance) + abs(col_distance)) * frames_per_square

    if frame_count == 0:
        return

    for frame in range(frame_count + 1):
        row = anim_start_row + row_distance * frame / frame_count
        col = anim_start_col + col_distance * frame / frame_count
        draw_board(screen)
        draw_pieces(screen, board)

        # Erase the piece moved from its ending square temporarily
        color = board_colors[(erase_row + erase_col) % 2]
        erase_square = pg.Rect(erase_col * SQ_SIZE, erase_row * SQ_SIZE, SQ_SIZE, SQ_SIZE)
        pg.draw.rect(screen, color, erase_square)

        # Redraw the captured piece if this is a normal move (not an undo)
        # For undo, the draw_pieces function has already redrawn the captured piece
        if not move_unmake and move.piece_captured != '--':
            if move.is_enpassant_move:
                # The captured pawn in en passant is on the same row the moving piece started from
                enpassant_row = move.start_row
                enpassant_col = move.end_col
                enpassant_square = pg.Rect(enpassant_col * SQ_SIZE, enpassant_row * SQ_SIZE, SQ_SIZE, SQ_SIZE)
                screen.blit(IMAGES[move.piece_captured], enpassant_square)
            else:
                screen.blit(IMAGES[move.piece_captured], erase_square)

        # Draw the piece being moved
        screen.blit(IMAGES[move.piece_moved], pg.Rect(col * SQ_SIZE, row * SQ_SIZE, SQ_SIZE, SQ_SIZE))
        pg.display.flip()
        clock.tick(ANIMATION_FPS)

def winning_animation(screen: pg.Surface, gs: chess_engine.GameState, white_wins: bool) -> None:
    """
    Render animations and badges for a checkmate state.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    gs : GameState
        The current state of the game containing king locations.
    white_wins : bool
        Flag indicating if white is the winner.
    """
    # Store 2 Kings' location based on boolean varial black_wins
    win_king_location = gs.white_king_location if white_wins else gs.black_king_location
    lose_king_location = gs.black_king_location if white_wins else gs.white_king_location

    # Draw red overlay for the losing King
    red_surface = pg.Surface((SQ_SIZE, SQ_SIZE), pg.SRCALPHA)
    red_surface.fill((255, 0, 0, 150))  # Red with 150 transparency
    screen.blit(red_surface, (lose_king_location[1] * SQ_SIZE, lose_king_location[0] * SQ_SIZE))

    # Draw green overlay for the winning King
    green_surface = pg.Surface((SQ_SIZE, SQ_SIZE), pg.SRCALPHA)
    green_surface.fill((100, 200, 100, 150))  # Green with transparency 150
    screen.blit(green_surface, (win_king_location[1] * SQ_SIZE, win_king_location[0] * SQ_SIZE))

    # Draw 'Winner' badge
    win_x = win_king_location[1] * SQ_SIZE + SQ_SIZE  # The badge is put in the top right corner of the square
    win_y = win_king_location[0] * SQ_SIZE
    draw_badge(screen, "Winner", pg.Color('white'), pg.Color('green'), win_x, win_y)

    # Draw 'Checkmate' badge
    lose_x = lose_king_location[1] * SQ_SIZE + SQ_SIZE
    lose_y = lose_king_location[0] * SQ_SIZE
    draw_badge(screen, "Checkmate", pg.Color('red'), pg.Color('white'), lose_x, lose_y)

def stalemate_animation(screen: pg.Surface, gs: chess_engine.GameState) -> None:
    """
    Render animations and badges for a stalemate (draw) state.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    gs : GameState
        The current state of the game containing king locations.
    """
    # Create a gray overlay
    gray_surface = pg.Surface((SQ_SIZE, SQ_SIZE), pg.SRCALPHA)
    gray_surface.fill((150, 150, 150, 150))  # Gray with transparency 150

    # Apply overlay for 2 Kings
    for king_location in [gs.white_king_location, gs.black_king_location]:
        # Add gray color
        screen.blit(gray_surface, (king_location[1] * SQ_SIZE, king_location[0] * SQ_SIZE))
        # Draw 'Draw' badge
        badge_x = king_location[1] * SQ_SIZE + SQ_SIZE
        badge_y = king_location[0] * SQ_SIZE
        draw_badge(screen, "Draw", pg.Color('white'), pg.Color('black'), badge_x, badge_y)

def draw_badge(
    screen: pg.Surface,
    text: str,
    bg_color: pg.Color,
    text_color: pg.Color,
    center_x: int,
    center_y: int
) -> None:
    """
    Render a text badge with rounded corners on the screen.

    Parameters
    ----------
    screen : pygame.Surface
        The main display surface to draw on.
    text : str
        The text to display on the badge.
    bg_color : pygame.Color
        The background color of the badge.
    text_color : pygame.Color
        The color of the text.
    center_x : int
        The x-coordinate for the center of the badge.
    center_y : int
        The y-coordinate for the center of the badge.
    """
    # Set up the font
    font = pg.font.SysFont('Helvetica', 14, bold=True)
    text_surface = font.render(text, True, text_color)
    text_rect = text_surface.get_rect()

    # Background size of the badge
    padding_x, padding_y = 12, 6
    badge_rect = pg.Rect(0, 0, text_rect.width + padding_x, text_rect.height + padding_y)
    badge_rect.center = (center_x, center_y)

    # Clamp the badge rectangle to the screen, make sure it always in the screen
    badge_rect.clamp_ip(screen.get_rect())

    # Rounded the background
    pg.draw.rect(screen, bg_color, badge_rect, border_radius=10)

    # Add text into the badge
    text_rect.center = badge_rect.center
    screen.blit(text_surface, text_rect)

if __name__ == '__main__':
    main()