"""Simplest possible UCI engine: plays a random legal move.

Use it as a template: replace `choose_move` with your own search.
"""
import random
import sys

import chess


def choose_move(board: chess.Board, wtime: int, btime: int, winc: int, binc: int) -> chess.Move:
    # Prefer captures, otherwise random. Replace with your search!
    moves = list(board.legal_moves)
    captures = [m for m in moves if board.is_capture(m)]
    return random.choice(captures or moves)


def parse_go(tokens):
    vals = {"wtime": 10000, "btime": 10000, "winc": 0, "binc": 0}
    for i, t in enumerate(tokens):
        if t in vals and i + 1 < len(tokens):
            vals[t] = int(tokens[i + 1])
    return vals


def main():
    board = chess.Board()
    for line in sys.stdin:
        cmd = line.split()
        if not cmd:
            continue
        if cmd[0] == "uci":
            print("id name RandomBot\nid author scacchackathon\nuciok", flush=True)
        elif cmd[0] == "isready":
            print("readyok", flush=True)
        elif cmd[0] == "ucinewgame":
            board = chess.Board()
        elif cmd[0] == "position":
            if cmd[1] == "startpos":
                board = chess.Board()
                rest = cmd[2:]
            else:  # position fen <6 fields> [moves ...]
                board = chess.Board(" ".join(cmd[2:8]))
                rest = cmd[8:]
            if rest and rest[0] == "moves":
                for m in rest[1:]:
                    board.push_uci(m)
        elif cmd[0] == "go":
            t = parse_go(cmd)
            move = choose_move(board, t["wtime"], t["btime"], t["winc"], t["binc"])
            print(f"bestmove {move.uci()}", flush=True)
        elif cmd[0] == "quit":
            break


if __name__ == "__main__":
    main()
