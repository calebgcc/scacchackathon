"""Standings, cross tables and Elo estimates for a round."""
import math
from collections import defaultdict

from . import db

# Terminations that mean the loser did something wrong (vs. losing over the board).
CLEAN_TERMINATIONS = {"normal", "adjudication", ""}


def round_games(round_id: int, finished_only: bool = True):
    sql = "SELECT * FROM games WHERE round_id=?" + (" AND status='done'" if finished_only else "")
    return db.q(sql + " ORDER BY game_no", round_id)


def bradley_terry(players: list[int], results: list[tuple[int, int, float]]) -> dict[int, tuple[float, float]]:
    """Return {player: (elo, ±95%)} anchored to mean 0.

    Draws count as half a win each. A virtual draw against a 0-rated opponent
    acts as a weak prior, so 100% or 0% scores stay finite.
    """
    if not players:
        return {}
    wins = defaultdict(float)
    n = defaultdict(float)  # games between pair (frozenset)
    for w, b, s in results:
        wins[w] += s
        wins[b] += 1 - s
        n[(w, b)] += 1
        n[(b, w)] += 1
    gamma = {p: 1.0 for p in players}
    for _ in range(300):
        new = {}
        for i in players:
            denom = 1.0 / (gamma[i] + 1.0)  # prior: one game vs virtual gamma=1
            for j in players:
                if j != i and n[(i, j)]:
                    denom += n[(i, j)] / (gamma[i] + gamma[j])
            new[i] = (wins[i] + 0.5) / denom
        # normalise geometric mean to 1
        g = math.exp(sum(math.log(v) for v in new.values()) / len(new))
        gamma = {p: v / g for p, v in new.items()}
    out = {}
    k = 400 / math.log(10)
    for i in players:
        info = 0.0
        for j in players:
            if j != i and n[(i, j)]:
                p = gamma[i] / (gamma[i] + gamma[j])
                info += n[(i, j)] * p * (1 - p)
        err = 1.96 * k / math.sqrt(info) if info > 0 else float("inf")
        out[i] = (k * math.log(gamma[i]), err)
    return out


def standings(round_id: int):
    """Return (rows sorted by rank, crosstable dict, players dict)."""
    entries = db.q("""SELECT e.player_id, e.submission_id, p.name, s.version, s.engine_name
                      FROM round_entries e JOIN players p ON p.id=e.player_id
                      JOIN submissions s ON s.id=e.submission_id WHERE e.round_id=?""", round_id)
    pids = [e["player_id"] for e in entries]
    rows = {e["player_id"]: {
        "player_id": e["player_id"], "name": e["name"], "version": e["version"],
        "engine_name": e["engine_name"], "points": 0.0, "games": 0, "w": 0, "d": 0, "l": 0,
        "problems": defaultdict(int), "sb": 0.0,
    } for e in entries}
    cross = defaultdict(lambda: [0.0, 0])  # (a, b) -> [points of a vs b, games]
    results = []
    for g in round_games(round_id):
        w, b = g["white_id"], g["black_id"]
        if w not in rows or b not in rows:
            continue
        s = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}.get(g["result"])
        if s is None:
            continue
        results.append((w, b, s))
        for pid, score in ((w, s), (b, 1 - s)):
            r = rows[pid]
            r["games"] += 1
            r["points"] += score
            r["w" if score == 1 else "d" if score == 0.5 else "l"] += 1
        cross[(w, b)][0] += s
        cross[(w, b)][1] += 1
        cross[(b, w)][0] += 1 - s
        cross[(b, w)][1] += 1
        term = (g["termination"] or "").lower()
        if s != 0.5 and term not in CLEAN_TERMINATIONS:
            loser = b if s == 1 else w
            rows[loser]["problems"][term] += 1
    elo = bradley_terry(pids, results)
    for pid, r in rows.items():
        r["elo"], r["elo_err"] = elo.get(pid, (0.0, float("inf")))
        r["score_pct"] = 100 * r["points"] / r["games"] if r["games"] else 0.0
        # Sonneborn-Berger tiebreak: sum of opponents' points weighted by score against them
        r["sb"] = sum(cross[(pid, o)][0] / max(cross[(pid, o)][1], 1) * rows[o]["points"]
                      for o in rows if o != pid and cross[(pid, o)][1])
        r["problems"] = dict(r["problems"])
    # Points, then Sonneborn-Berger, then Elo. Engines equal on all three share a rank ("1, 1, 3"); the name
    # only fixes the display order.
    tie_key = lambda r: (-r["points"], -round(r["sb"], 6), -round(r["elo"], 3))
    ordered = sorted(rows.values(), key=lambda r: (*tie_key(r), r["name"]))
    for i, r in enumerate(ordered, 1):
        r["rank"] = ordered[i - 2]["rank"] if i > 1 and tie_key(r) == tie_key(ordered[i - 2]) else i
    return ordered, dict(cross)
