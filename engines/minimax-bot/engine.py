"""A small but real UCI engine: alpha-beta + iterative deepening + simple eval.

Good starting point if you write your engine in Python.
"""
import sys
import time

import chess

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
# Tiny centralisation bonus for knights/bishops/pawns.
CENTER = [0, 1, 2, 3, 3, 2, 1, 0]
MATE = 100_000


class Timeout(Exception):
    pass


def evaluate(board: chess.Board) -> int:
    """Score from the side to move's point of view."""
    score = 0
    for sq, piece in board.piece_map().items():
        v = VALUES[piece.piece_type]
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP, chess.PAWN):
            v += 5 * (CENTER[chess.square_file(sq)] + CENTER[chess.square_rank(sq)])
        score += v if piece.color == chess.WHITE else -v
    return score if board.turn == chess.WHITE else -score


def ordered(board):
    # Captures first (MVV-LVA-ish), then the rest.
    def key(m):
        if board.is_capture(m):
            victim = board.piece_at(m.to_square)
            return -(VALUES[victim.piece_type] if victim else 100)
        return 0
    return sorted(board.legal_moves, key=key)


class Searcher:
    def __init__(self, deadline):
        self.deadline = deadline
        self.nodes = 0

    def negamax(self, board, depth, alpha, beta, ply):
        self.nodes += 1
        if self.nodes % 1024 == 0 and time.monotonic() > self.deadline:
            raise Timeout
        if board.is_checkmate():
            return -MATE + ply
        if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_draw():
            return 0
        if depth == 0:
            return self.quiesce(board, alpha, beta)
        best = -MATE
        for m in ordered(board):
            board.push(m)
            score = -self.negamax(board, depth - 1, -beta, -alpha, ply + 1)
            board.pop()
            if score > best:
                best = score
            alpha = max(alpha, score)
            if alpha >= beta:
                break
        return best

    def quiesce(self, board, alpha, beta):
        self.nodes += 1
        if self.nodes % 1024 == 0 and time.monotonic() > self.deadline:
            raise Timeout
        stand = evaluate(board)
        if stand >= beta:
            return stand
        alpha = max(alpha, stand)
        for m in ordered(board):
            if not board.is_capture(m):
                continue
            board.push(m)
            score = -self.quiesce(board, -beta, -alpha)
            board.pop()
            if score >= beta:
                return score
            alpha = max(alpha, score)
        return alpha


def search(board, budget_s):
    deadline = time.monotonic() + budget_s
    s = Searcher(deadline)
    moves = ordered(board)
    best_move = moves[0]
    depth = 1
    while True:
        try:
            best_score, current = -MATE - 1, None
            # Search the previous best move first.
            for m in [best_move] + [x for x in moves if x != best_move]:
                board.push(m)
                score = -s.negamax(board, depth - 1, -MATE, MATE, 1)
                board.pop()
                if score > best_score:
                    best_score, current = score, m
            best_move = current
            print(f"info depth {depth} score cp {best_score} nodes {s.nodes} pv {best_move.uci()}", flush=True)
        except Timeout:
            # board may have pushed moves left; caller passes a copy.
            break
        depth += 1
        if depth > 30 or time.monotonic() > deadline - budget_s * 0.5:
            break
    return best_move


def main():
    board = chess.Board()
    for line in sys.stdin:
        cmd = line.split()
        if not cmd:
            continue
        if cmd[0] == "uci":
            print("id name MinimaxBot\nid author scacchackathon\nuciok", flush=True)
        elif cmd[0] == "isready":
            print("readyok", flush=True)
        elif cmd[0] == "ucinewgame":
            board = chess.Board()
        elif cmd[0] == "position":
            if cmd[1] == "startpos":
                board = chess.Board()
                rest = cmd[2:]
            else:
                board = chess.Board(" ".join(cmd[2:8]))
                rest = cmd[8:]
            if rest and rest[0] == "moves":
                for m in rest[1:]:
                    board.push_uci(m)
        elif cmd[0] == "go":
            args = dict(zip(cmd[1::2], cmd[2::2]))
            mine = "wtime" if board.turn == chess.WHITE else "btime"
            inc = "winc" if board.turn == chess.WHITE else "binc"
            left = int(args.get(mine, 10000)) / 1000
            increment = int(args.get(inc, 0)) / 1000
            if "movetime" in args:
                budget = int(args["movetime"]) / 1000 * 0.9
            else:
                budget = max(0.01, left / 30 + increment * 0.8)
            move = search(board.copy(), budget)
            print(f"bestmove {move.uci()}", flush=True)
        elif cmd[0] == "quit":
            break


if __name__ == "__main__":
    main()
