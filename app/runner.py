"""Runs one evaluation round with fastchess and ingests its event stream."""
import asyncio
import json
import os
import random
import re
import signal
from pathlib import Path

from . import db
from .builder import engine_run_args
from .config import BOOK_PATH, DATA_DIR, DOCKER, FASTCHESS

_task: asyncio.Task | None = None
_proc: asyncio.subprocess.Process | None = None
_cancel_requested = False


# Adjudication thresholds (used when the "adjudicate" setting is on; also shown on the docs page).
DRAW_ADJ = {"movenumber": 40, "movecount": 8, "score": 10}
RESIGN_ADJ = {"movecount": 3, "score": 600}
STARTUP_MS = 30000   # time an engine gets to answer "uci" / "isready" when it starts


def round_dir(round_id: int) -> Path:
    return Path(DATA_DIR) / "rounds" / str(round_id)


def is_running() -> bool:
    return _task is not None and not _task.done()


def effective_concurrency(settings: dict) -> int:
    c = int(settings.get("concurrency") or 0)
    if c <= 0:
        c = max(1, (os.cpu_count() or 2) // 2)
    return c


def candidates():
    """Latest ready submission of every active player."""
    return db.q("""
        SELECT p.id AS player_id, p.name, s.id AS submission_id, s.version, s.image
        FROM players p JOIN submissions s ON s.player_id = p.id
        WHERE p.active = 1 AND s.status = 'ready'
          AND s.version = (SELECT MAX(version) FROM submissions s2
                           WHERE s2.player_id = p.id AND s2.status = 'ready')
        ORDER BY p.name""")


def build_command(round_id: int, entries, settings: dict, seed: int) -> list[str]:
    d = round_dir(round_id)
    args = [
        FASTCHESS,
        "-event", f"{settings['event_name']} round {round_id}",
        "-tournament", "roundrobin",
        "-rounds", str(settings["rounds"]), "-games", "2",
        "-concurrency", str(effective_concurrency(settings)), "-force-concurrency",
        "-openings", f"file={BOOK_PATH}", "format=pgn", "order=random",
        "-srand", str(seed),
        "-each", f"tc={settings['tc']}", f"timemargin={settings['timemargin']}",
        "-pgnout", f"file={d / 'games.pgn'}", "timeleft=true",
        "-events", f"file={d / 'events.ndjson'}",
        "-recover",
        "-startup-ms", str(STARTUP_MS),
        "-autosaveinterval", "0",
        "-config", f"outname={d / 'config.json'}",
        "-ratinginterval", "0",
    ]
    if int(settings.get("maxmoves") or 0) > 0:
        args += ["-maxmoves", str(settings["maxmoves"])]
    if settings.get("adjudicate"):
        args += ["-draw", *(f"{k}={v}" for k, v in DRAW_ADJ.items()),
                 # twosided: both engines must agree, so one buggy eval can't resign a game
                 "-resign", *(f"{k}={v}" for k, v in RESIGN_ADJ.items()), "twosided=true"]
    for e in entries:
        run = engine_run_args(e["image"], f"scacc.round={round_id}",
                              settings["engine_cpus"], settings["engine_memory"])
        args += ["-engine", f"cmd={DOCKER}", f"args={' '.join(run)}", f"name={e['name']}"]
    return args


def start_round() -> int:
    global _task, _cancel_requested
    if is_running():
        raise RuntimeError("a round is already running")
    entries = candidates()
    if len(entries) < 2:
        raise RuntimeError("need at least two players with a ready engine")
    settings = db.all_settings()
    db.set_setting("submissions_open", False)
    seed = random.randint(1, 2**31 - 1)
    n = len(entries)
    total = n * (n - 1) // 2 * int(settings["rounds"]) * 2
    cfg = dict(settings, seed=seed, concurrency_effective=effective_concurrency(settings))
    rid = db.ex("INSERT INTO rounds(created_at, started_at, status, config, total_games) VALUES(?,?,?,?,?)",
                db.now(), db.now(), "running", json.dumps(cfg), total)
    for e in entries:
        db.ex("INSERT INTO round_entries(round_id, player_id, submission_id) VALUES(?,?,?)",
              rid, e["player_id"], e["submission_id"])
    round_dir(rid).mkdir(parents=True, exist_ok=True)
    _cancel_requested = False
    _task = asyncio.get_running_loop().create_task(_run(rid, entries, settings, seed))
    return rid


async def cancel_round() -> None:
    global _cancel_requested
    if not is_running() or _proc is None:
        return
    _cancel_requested = True
    try:
        os.killpg(_proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(asyncio.shield(_proc.wait()), 10)
    except asyncio.TimeoutError:
        try:
            os.killpg(_proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def cleanup_containers(label: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        DOCKER, "ps", "-aq", "--filter", f"label={label}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    ids = out.decode().split()
    if ids:
        rm = await asyncio.create_subprocess_exec(
            DOCKER, "rm", "-f", *ids, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await rm.wait()


class EventIngester:
    """Tails events.ndjson and mirrors games into the db."""

    def __init__(self, round_id: int, name_to_pid: dict[str, int]):
        self.round_id = round_id
        self.path = round_dir(round_id) / "events.ndjson"
        self.offset = 0
        self.buf = b""
        self.names = name_to_pid
        self.games: dict[int, dict] = {}   # game_no -> in-memory state
        self.dirty: set[int] = set()
        self.finished_reason: str | None = None

    def poll(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        if not data:
            return
        self.offset += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for raw in lines:
            if raw.strip():
                try:
                    self.handle(json.loads(raw))
                except (ValueError, KeyError) as e:
                    print(f"[round {self.round_id}] bad event: {e}: {raw[:200]!r}", flush=True)
        self.flush()

    def handle(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "game_started":
            no = ev["game"]
            g = {"moves": list(ev.get("opening") or []), "book": len(ev.get("opening") or []), "evals": []}
            self.games[no] = g
            db.ex("""INSERT INTO games(round_id, game_no, white_id, black_id, status, start_fen, book_plies,
                                       moves, started_at)
                     VALUES(?,?,?,?, 'live', ?,?,?,?)
                     ON CONFLICT(round_id, game_no) DO UPDATE SET status='live', moves=excluded.moves,
                        book_plies=excluded.book_plies, evals='[]', result='*'""",
                  self.round_id, no, self.names[ev["white"]], self.names[ev["black"]],
                  ev.get("start_fen") or "startpos", g["book"], json.dumps(g["moves"]), db.now())
        elif kind == "move":
            g = self.games.get(ev["game"])
            if g is None or not ev.get("legal", True):
                return
            g["moves"].append(ev["move"])
            sc = ev.get("score") or {}
            # cp/mate/d: the mover's own report; ms: how long it thought; wt/bt: both clocks after the move
            g["evals"].append({k: v for k, v in (("cp", sc.get("cp")), ("mate", sc.get("mate")),
                                                  ("d", ev.get("depth")), ("ms", ev.get("time_ms")),
                                                  ("wt", ev.get("wtime")), ("bt", ev.get("btime")))
                               if v is not None})
            self.dirty.add(ev["game"])
        elif kind in ("game_finished", "game_aborted"):
            no = ev["game"]
            g = self.games.get(no, {"moves": [], "evals": []})
            status = "done" if kind == "game_finished" else "aborted"
            db.ex("""UPDATE games SET status=?, result=?, reason=?, termination=?, moves=?, evals=?, finished_at=?
                     WHERE round_id=? AND game_no=?""",
                  status, ev.get("result", "*"), ev.get("reason", ""), ev.get("termination", ""),
                  json.dumps(g["moves"]), json.dumps(g["evals"]), db.now(), self.round_id, no)
            self.dirty.discard(no)
            self.games.pop(no, None)
        elif kind == "tournament_finished":
            self.finished_reason = ev.get("reason", "")

    def flush(self) -> None:
        for no in self.dirty:
            g = self.games.get(no)
            if g:
                db.ex("UPDATE games SET moves=?, evals=? WHERE round_id=? AND game_no=?",
                      json.dumps(g["moves"]), json.dumps(g["evals"]), self.round_id, no)
        self.dirty.clear()


def attach_pgns(round_id: int) -> None:
    path = round_dir(round_id) / "games.pgn"
    if not path.exists():
        return
    text = path.read_text(errors="replace")
    for chunk in re.split(r"\n(?=\[Event )", text):
        m = re.search(r'\[GameId "(\d+)"\]', chunk)
        if m:
            db.ex("UPDATE games SET pgn=? WHERE round_id=? AND game_no=?",
                  chunk.strip() + "\n", round_id, int(m.group(1)))


async def _run(round_id: int, entries, settings: dict, seed: int) -> None:
    global _proc
    d = round_dir(round_id)
    cmd = build_command(round_id, entries, settings, seed)
    (d / "command.txt").write_text(" ".join(cmd) + "\n")
    ingester = EventIngester(round_id, {e["name"]: e["player_id"] for e in entries})
    status, note = "failed", ""
    try:
        with open(d / "fastchess.log", "wb") as logf:
            _proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(d), stdout=logf, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            while _proc.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(_proc.wait()), 1.0)
                except asyncio.TimeoutError:
                    pass
                ingester.poll()
        ingester.poll()
        rc = _proc.returncode
        if _cancel_requested:
            status, note = "cancelled", "cancelled by admin"
        elif rc == 0 and ingester.finished_reason in ("completed", None, ""):
            status = "done"
        else:
            note = f"fastchess exited with code {rc} ({ingester.finished_reason or 'no finish event'})"
    except asyncio.CancelledError:
        # the app is shutting down (deploy / restart) while the round runs
        note = "server stopped during round"
        raise
    except Exception as e:  # noqa: BLE001
        note = f"{type(e).__name__}: {e}"
    finally:
        _proc = None
        db.ex("UPDATE games SET status='aborted' WHERE round_id=? AND status='live'", round_id)
        attach_pgns(round_id)
        await cleanup_containers(f"scacc.round={round_id}")
        db.ex("UPDATE rounds SET status=?, finished_at=?, note=? WHERE id=?", status, db.now(), note, round_id)
        print(f"[round {round_id}] finished: {status} {note}", flush=True)


async def recover_after_restart() -> None:
    for r in db.q("SELECT id FROM rounds WHERE status='running'"):
        db.ex("UPDATE rounds SET status='failed', finished_at=?, note='server restarted during round' WHERE id=?",
              db.now(), r["id"])
        db.ex("UPDATE games SET status='aborted' WHERE round_id=? AND status='live'", r["id"])
        attach_pgns(r["id"])
    await cleanup_containers("scacc.round")
