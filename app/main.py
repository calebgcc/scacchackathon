import time
import hashlib
import html
import hmac
import io
import json
import re
import secrets
import shutil
import tarfile
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import chess
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import builder, db, ratings, runner
from .config import ADMIN_PASSWORD, BOOK_PATH, BUILD_TIMEOUT_S, MAX_UPLOAD_MB, PUBLIC_URL, SECRET_KEY

BASE = Path(__file__).parent
ROOT = BASE.parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ADMIN_PASSWORD:
        print("WARNING: ADMIN_PASSWORD is not set - the admin panel is disabled", flush=True)
    db.conn()
    await runner.recover_after_restart()
    await runner.cleanup_containers("scacc.smoke")
    builder.resume_pending()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY or secrets.token_hex(16),
                   session_cookie="scacc_admin", max_age=7 * 24 * 3600, same_site="lax")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def fmt_time(ts):
    """Clock time (the event is one day); older than today also shows the date."""
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d %b %H:%M")


def fmt_dur(a, b):
    """'45s', '3m 05s', '1h 02m'."""
    if not a:
        return ""
    s = int((b or db.now()) - a)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def fmt_size(n):
    if not n:
        return ""
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


# fastchess termination -> words for the "Problems" column
TERMINATION_LABELS = {"time_forfeit": "lost on time", "illegal_move": "illegal move", "disconnect": "crashed",
                      "stall": "stopped responding", "interrupt": "interrupted"}


def termination_label(term: str) -> str:
    return TERMINATION_LABELS.get(term, term.replace("_", " "))


templates.env.filters["t"] = fmt_time
templates.env.filters["size"] = fmt_size
templates.env.filters["term"] = termination_label
templates.env.globals["dur"] = fmt_dur
templates.env.globals["asset_v"] = str(int(time.time()))  # cache-buster: changes on every restart


def base_url(request: Request) -> str:
    return (db.get_setting("public_url") or PUBLIC_URL or str(request.base_url)).rstrip("/")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("is_admin", is_admin(request))
    ctx.setdefault("state", public_state())
    ctx.setdefault("event_name", db.get_setting("event_name"))
    return templates.TemplateResponse(request, name, ctx)


# --- auth -------------------------------------------------------------------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "scc_" + secrets.token_urlsafe(24)


def is_admin(request: Request) -> bool:
    return bool(ADMIN_PASSWORD) and request.session.get("admin") is True


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(303, headers={"Location": "/admin/login"})


def player_from_request(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-token", "").strip()
    if not token:
        raise HTTPException(401, "missing token: send 'Authorization: Bearer <your token>'")
    player = db.q1("SELECT * FROM players WHERE token_hash=?", hash_token(token))
    if player is None or not player["active"]:
        raise HTTPException(401, "invalid token")
    return player


# --- shared state -------------------------------------------------------------

def latest_round():
    return db.q1("SELECT * FROM rounds ORDER BY id DESC LIMIT 1")


def round_progress(r) -> dict:
    done = db.q1("SELECT COUNT(*) c FROM games WHERE round_id=? AND status='done'", r["id"])["c"]
    live = db.q1("SELECT COUNT(*) c FROM games WHERE round_id=? AND status='live'", r["id"])["c"]
    return {"id": r["id"], "status": r["status"], "done": done, "live": live, "total": r["total_games"]}


def public_state() -> dict:
    r = latest_round()
    running = r is not None and r["status"] == "running"
    sub = db.q1("SELECT MAX(id) m, COUNT(*) c FROM submissions")
    busy = db.q1("SELECT COUNT(*) c FROM submissions WHERE status IN ('queued','building','testing')")["c"]
    return {
        "submissions_open": bool(db.get_setting("submissions_open")),
        "round": round_progress(r) if r else None,
        "running": running,
        "builds_in_progress": busy,
        "sig": f"{db.get_setting('submissions_open')}-{r['id'] if r else 0}-{r['status'] if r else ''}-"
               f"{round_progress(r)['done'] if r else 0}-{sub['m']}-{busy}",
    }


def player_overview():
    return db.q("""
        SELECT p.id, p.name, p.active,
          (SELECT version FROM submissions WHERE player_id=p.id AND status='ready' ORDER BY version DESC LIMIT 1) AS ready_version,
          (SELECT version FROM submissions WHERE player_id=p.id ORDER BY version DESC LIMIT 1) AS last_version,
          (SELECT status FROM submissions WHERE player_id=p.id ORDER BY version DESC LIMIT 1) AS last_status,
          (SELECT created_at FROM submissions WHERE player_id=p.id ORDER BY version DESC LIMIT 1) AS last_at,
          (SELECT COUNT(*) FROM submissions WHERE player_id=p.id) AS n_subs
        FROM players p WHERE p.active=1 ORDER BY p.name""")


def history():
    """Rank of each player in every finished round."""
    rounds = db.q("SELECT id FROM rounds WHERE status IN ('done','running') ORDER BY id")
    table = {}
    for r in rounds:
        rows, _ = ratings.standings(r["id"])
        for row in rows:
            table.setdefault(row["name"], {})[r["id"]] = row
    return [r["id"] for r in rounds], table


# --- public pages -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    r = db.q1("SELECT * FROM rounds WHERE status IN ('running','done') ORDER BY id DESC LIMIT 1")
    rows, cross = (ratings.standings(r["id"]) if r else ([], {}))
    live = db.q("""SELECT g.*, w.name wn, b.name bn FROM games g JOIN players w ON w.id=g.white_id
                   JOIN players b ON b.id=g.black_id WHERE g.round_id=? AND g.status='live' ORDER BY g.game_no""",
                r["id"]) if r and r["status"] == "running" else []
    round_ids, hist = history()
    return render(request, "index.html", round=r, rows=rows, cross=cross, live=live,
                  players=player_overview(), round_ids=round_ids, hist=hist,
                  progress=round_progress(r) if r else None)


@app.get("/rounds", response_class=HTMLResponse)
async def rounds_page(request: Request):
    rounds = db.q("SELECT * FROM rounds ORDER BY id DESC")
    info = []
    for r in rounds:
        rows, _ = ratings.standings(r["id"])
        info.append({"r": r, "winner": rows[0] if rows and rows[0]["games"] else None,
                     "n": len(rows), "progress": round_progress(r)})
    return render(request, "rounds.html", rounds=info)


@app.get("/rounds/{round_id}", response_class=HTMLResponse)
async def round_page(request: Request, round_id: int):
    r = db.q1("SELECT * FROM rounds WHERE id=?", round_id)
    if r is None:
        raise HTTPException(404)
    rows, cross = ratings.standings(round_id)
    games = db.q("""SELECT g.id, g.game_no, g.status, g.result, g.reason, g.termination,
                           json_array_length(g.moves) nply, w.name wn, b.name bn
                    FROM games g JOIN players w ON w.id=g.white_id JOIN players b ON b.id=g.black_id
                    WHERE g.round_id=? ORDER BY g.game_no""", round_id)
    reasons = sorted({end_label(g) for g in games if end_label(g)})
    return render(request, "round.html", round=r, rows=rows, cross=cross, games=games, reasons=reasons,
                  end_label=end_label, progress=round_progress(r), config=json.loads(r["config"]))


def end_label(g) -> str:
    """Short, filterable description of how a game ended."""
    if g["status"] == "live":
        return "in progress"
    if g["status"] == "aborted":
        return "aborted"
    reason = (g["reason"] or "").lower()
    term = (g["termination"] or "").lower()
    # order matters: "stalemate" and "mating material" both contain "mate"
    if "stalemate" in reason:
        return "stalemate"
    if "material" in reason:
        return "insufficient material"
    if reason.endswith(" mates") or "checkmate" in reason:
        return "checkmate"
    if "time" in reason or "time" in term:
        return "time forfeit"
    if "illegal" in reason or "illegal" in term:
        return "illegal move"
    if "disconnect" in reason or "crash" in reason or "stall" in term or "abandon" in term:
        return "crash / disconnect"
    if "adjudicat" in reason or term == "adjudication":
        return "adjudication"
    if "repetition" in reason:
        return "repetition"
    if "stalemate" in reason:
        return "stalemate"
    if "50" in reason or "fifty" in reason:
        return "50-move rule"
    if "material" in reason:
        return "insufficient material"
    return g["reason"] or term or ""


def load_game(game_id: int):
    g = db.q1("""SELECT g.*, w.name wn, b.name bn, r.config FROM games g JOIN players w ON w.id=g.white_id
                 JOIN players b ON b.id=g.black_id JOIN rounds r ON r.id=g.round_id WHERE g.id=?""", game_id)
    if g is None:
        raise HTTPException(404)
    return g


def replay(g):
    board = chess.Board() if g["start_fen"] in ("startpos", "") else chess.Board(g["start_fen"])
    fens, sans, moves = [board.fen()], [], []
    for uci in json.loads(g["moves"]):
        try:
            mv = chess.Move.from_uci(uci)
            sans.append(board.san(mv))
            board.push(mv)
        except (ValueError, AssertionError):
            break
        moves.append(uci)
        fens.append(board.fen())
    return board, fens, sans, moves


def pgn_text(g, board=None) -> str:
    """The PGN fastchess wrote, or one generated from the moves (e.g. while the round is running)."""
    if g["pgn"]:
        return g["pgn"]
    import chess.pgn
    if board is None:
        board = replay(g)[0]
    game = chess.pgn.Game.from_board(board)
    cfg = json.loads(g["config"])
    game.headers.update({"Event": f"{cfg.get('event_name', 'Scacchackathon')} round {g['round_id']}",
                         "Site": "?", "Round": str(g["game_no"]), "White": g["wn"], "Black": g["bn"],
                         "Result": g["result"] or "*", "TimeControl": cfg.get("tc", "?")})
    if g["reason"]:
        game.headers["Termination"] = g["reason"]
    return str(game) + "\n"


@app.get("/games/{game_id}", response_class=HTMLResponse)
async def game_page(request: Request, game_id: int):
    g = load_game(game_id)
    nav = db.q("SELECT id FROM games WHERE round_id=? ORDER BY game_no", g["round_id"])
    ids = [x["id"] for x in nav]
    i = ids.index(game_id)
    return render(request, "game.html", g=g, prev_id=ids[i - 1] if i > 0 else None,
                  next_id=ids[i + 1] if i + 1 < len(ids) else None)


def game_clocks(g, fens, evals):
    """Per position: [white clock, black clock] in ms, and per ply the mover's thinking time (None for book moves).

    fastchess reports both clocks after every move (stored as wt/bt). Games recorded before we stored them get
    clocks rebuilt from the time control: each side starts with base + increment, and every move costs its
    thinking time and adds the increment, which is exactly how fastchess runs the clock.
    """
    try:
        parts = tc_parts(json.loads(g["config"] or "{}").get("tc", ""))
    except ValueError:
        parts = None
    if not parts:
        return None, [None] * (len(fens) - 1)
    _, base, inc = parts
    clock = {"w": round((base + inc) * 1000), "b": round((base + inc) * 1000)}
    clocks, think = [[clock["w"], clock["b"]]], []
    book = g["book_plies"]
    for p in range(1, len(fens)):
        side = "w" if fens[p - 1].split(" ")[1] == "w" else "b"
        e = evals[p - book - 1] if p > book and p - book - 1 < len(evals) else None
        if e is None:
            think.append(None)
        else:
            ms = e.get("ms")
            think.append(ms)
            if "wt" in e and "bt" in e:
                clock = {"w": e["wt"], "b": e["bt"]}
            elif ms is not None:
                clock[side] = clock[side] - ms + round(inc * 1000)
        clocks.append([clock["w"], clock["b"]])
    return clocks, think


@app.get("/api/games/{game_id}")
async def game_json(game_id: int):
    g = load_game(game_id)
    board, fens, sans, moves = replay(g)
    evals = json.loads(g["evals"])
    clocks, think = game_clocks(g, fens, evals)
    return {"id": g["id"], "round_id": g["round_id"], "game_no": g["game_no"], "white": g["wn"],
            "black": g["bn"], "status": g["status"], "result": g["result"], "reason": g["reason"],
            "termination": g["termination"], "book_plies": g["book_plies"],
            "moves": moves, "san": sans, "fens": fens, "evals": evals,
            "clocks": clocks, "think_ms": think, "pgn": pgn_text(g, board)}


@app.get("/games/{game_id}/pgn")
async def game_pgn(game_id: int):
    return PlainTextResponse(pgn_text(load_game(game_id)))


@app.get("/rounds/{round_id}/pgn")
async def round_pgn(round_id: int):
    p = runner.round_dir(round_id) / "games.pgn"
    if p.exists():
        return PlainTextResponse(p.read_text(errors="replace"))
    ids = db.q("SELECT id FROM games WHERE round_id=? ORDER BY game_no", round_id)
    if not ids:
        raise HTTPException(404)
    return PlainTextResponse("\n".join(pgn_text(load_game(x["id"])) for x in ids))


@app.get("/api/state")
async def api_state():
    return public_state()


def tc_parts(tc: str):
    """'40/1:00+0.5' -> (moves or None, base seconds, increment seconds); None if unparseable."""
    m = re.fullmatch(r"(?:(\d+)/)?(\d+(?:\.\d+)?)(?::(\d+))?(?:\+(\d+(?:\.\d+)?))?", tc.strip())
    if not m:
        return None
    moves, base, secs, inc = m.groups()
    return moves, (float(base) * 60 + float(secs) if secs else float(base)), float(inc or 0)


def tc_human(tc: str) -> str:
    """'10+0.1' -> '10 seconds per game, plus 0.1 seconds per move'; also handles '40/60' and '1:00+1'."""
    parts = tc_parts(tc)
    if not parts:
        return tc
    moves, total, inc = parts
    def amount(x: float, unit: str) -> str:
        return f"{x:g} {unit}" + ("" if x == 1 else "s")
    base_txt = amount(total / 60, "minute") if total >= 120 and total % 60 == 0 else amount(total, "second")
    out = f"{base_txt} for {'every ' + moves + ' moves' if moves else 'the whole game'}"
    if inc > 0:
        out += f", plus {amount(inc, 'second')} added after each move"
    return out


@app.get("/help")
async def help_redirect():
    return RedirectResponse("/docs", 301)


@app.get("/docs", response_class=HTMLResponse)
async def docs_page(request: Request):
    settings = db.all_settings()
    n = len(runner.candidates())
    pairs = n * (n - 1) // 2
    parts = tc_parts(settings["tc"])
    # a typical "time left / 30 + 80% of the increment" budget for the first move, in ms
    budget_ms = round(parts[1] * 1000 / 30 + parts[2] * 800) if parts else None
    return render(request, "docs.html", url=base_url(request), settings=settings,
                  concurrency=runner.effective_concurrency(settings), tc_text=tc_human(settings["tc"]),
                  n_engines=n, n_pairs=pairs, n_games=pairs * int(settings["rounds"]) * 2,
                  draw_adj=runner.DRAW_ADJ, resign_adj=runner.RESIGN_ADJ, startup_s=runner.STARTUP_MS // 1000,
                  max_upload_mb=MAX_UPLOAD_MB, build_timeout_min=BUILD_TIMEOUT_S // 60,
                  book_name=Path(BOOK_PATH).stem, budget_ms=budget_ms,
                  base_ms=round(parts[1] * 1000) if parts else None, inc_ms=round(parts[2] * 1000) if parts else None)


@app.get("/submit.sh")
async def submit_script(request: Request):
    text = (ROOT / "client" / "submit.sh").read_text().replace("__SCACC_URL__", base_url(request))
    return PlainTextResponse(text, media_type="text/x-shellscript")


@app.get("/examples/{name}.tar.gz")
async def example_tarball(name: str):
    src = ROOT / "engines" / name
    if name not in ("random-bot", "minimax-bot") or not src.is_dir():
        raise HTTPException(404)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        t.add(src, arcname=name, filter=lambda ti: None if "__pycache__" in ti.name else ti)
    return Response(buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})


# --- player API ------------------------------------------------------------

@app.post("/api/submit")
async def api_submit(request: Request, engine: UploadFile = File(...)):
    player = player_from_request(request)
    if not db.get_setting("submissions_open"):
        raise HTTPException(403, "submissions are closed right now - wait for the organizer to open them")
    busy = db.q1("SELECT id FROM submissions WHERE player_id=? AND status IN ('queued','building','testing')",
                 player["id"])
    if busy:
        raise HTTPException(409, f"submission #{busy['id']} is still building - wait for it to finish")
    version = (db.q1("SELECT MAX(version) v FROM submissions WHERE player_id=?", player["id"])["v"] or 0) + 1
    sub_id = db.ex("INSERT INTO submissions(player_id, version, created_at, status) VALUES(?,?,?, 'queued')",
                   player["id"], version, db.now())
    base = builder.submissions_dir() / str(sub_id)
    base.mkdir(parents=True, exist_ok=True)
    size, limit = 0, MAX_UPLOAD_MB * 1024 * 1024
    with open(base / "upload", "wb") as f:
        while chunk := await engine.read(1 << 20):
            size += len(chunk)
            if size > limit:
                f.close()
                shutil.rmtree(base, ignore_errors=True)
                db.ex("DELETE FROM submissions WHERE id=?", sub_id)
                raise HTTPException(413, f"upload larger than {MAX_UPLOAD_MB} MB - leave out build outputs/.git")
            f.write(chunk)
    db.ex("UPDATE submissions SET size_bytes=? WHERE id=?", size, sub_id)
    builder.enqueue(sub_id)
    return {"id": sub_id, "player": player["name"], "version": version, "status": "queued",
            "log_url": f"{base_url(request)}/api/submissions/{sub_id}/log"}


def own_submission(request: Request, sub_id: int):
    player = player_from_request(request)
    s = db.q1("SELECT * FROM submissions WHERE id=? AND player_id=?", sub_id, player["id"])
    if s is None:
        raise HTTPException(404, "no such submission for this token")
    return s


@app.get("/api/submissions/{sub_id}")
async def api_submission(request: Request, sub_id: int):
    s = own_submission(request, sub_id)
    return {"id": s["id"], "version": s["version"], "status": s["status"], "engine_name": s["engine_name"],
            "log": s["log"]}


@app.get("/api/submissions/{sub_id}/log")
async def api_submission_log(request: Request, sub_id: int, offset: int = 0):
    s = own_submission(request, sub_id)
    log = s["log"] or ""
    return PlainTextResponse(log[offset:], headers={"X-Status": s["status"], "X-Next-Offset": str(len(log))})


@app.get("/api/me")
async def api_me(request: Request):
    player = player_from_request(request)
    subs = db.q("SELECT id, version, status, created_at FROM submissions WHERE player_id=? ORDER BY id DESC",
                player["id"])
    return {"name": player["name"], "submissions_open": bool(db.get_setting("submissions_open")),
            "submissions": [dict(s) for s in subs]}


# --- admin -------------------------------------------------------------------

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = ""):
    return render(request, "admin_login.html", error=error, configured=bool(ADMIN_PASSWORD))


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        request.session["admin"] = True
        return RedirectResponse("/admin", 303)
    return RedirectResponse("/admin/login?error=1", 303)


@app.post("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, msg: str = "", err: str = ""):
    require_admin(request)
    token_for, token = request.session.pop("new_token", None) or ("", "")
    players = db.q("""SELECT p.*, (SELECT COUNT(*) FROM submissions WHERE player_id=p.id) n_subs
                      FROM players p ORDER BY p.active DESC, p.name""")
    subs = db.q("""SELECT s.id, s.version, s.status, s.created_at, s.finished_at, s.engine_name, s.size_bytes,
                          p.name FROM submissions s JOIN players p ON p.id=s.player_id
                   ORDER BY s.id DESC LIMIT 100""")
    rounds = db.q("SELECT * FROM rounds ORDER BY id DESC")
    settings = db.all_settings()
    return render(request, "admin.html", players=players, subs=subs, rounds=rounds, settings=settings,
                  candidates=runner.candidates(), running=runner.is_running(),
                  concurrency=runner.effective_concurrency(settings),
                  progress={r["id"]: round_progress(r) for r in rounds},
                  new_token=token, token_for=token_for, msg=msg, err=err, url=base_url(request))


def back(msg: str = "", err: str = ""):
    params = {k: v for k, v in dict(msg=msg, err=err).items() if v}
    return RedirectResponse("/admin" + ("?" + urlencode(params) if params else ""), 303)


@app.post("/admin/submissions/open")
async def admin_open(request: Request):
    require_admin(request)
    if runner.is_running():
        return back(err="a round is running - wait for it to finish or cancel it first")
    db.set_setting("submissions_open", True)
    return back("submissions are open")


@app.post("/admin/submissions/close")
async def admin_close(request: Request):
    require_admin(request)
    db.set_setting("submissions_open", False)
    return back("submissions are closed")


@app.post("/admin/rounds/start")
async def admin_start_round(request: Request):
    require_admin(request)
    try:
        rid = runner.start_round()
    except RuntimeError as e:
        return back(err=str(e))
    return back(f"round {rid} started")


@app.post("/admin/rounds/cancel")
async def admin_cancel_round(request: Request):
    require_admin(request)
    await runner.cancel_round()
    return back("round cancelled")


@app.post("/admin/rounds/{round_id}/delete")
async def admin_delete_round(request: Request, round_id: int):
    require_admin(request)
    r = db.q1("SELECT status FROM rounds WHERE id=?", round_id)
    if r is None or r["status"] == "running":
        return back(err="cannot delete a running round")
    db.ex("DELETE FROM games WHERE round_id=?", round_id)
    db.ex("DELETE FROM round_entries WHERE round_id=?", round_id)
    db.ex("DELETE FROM rounds WHERE id=?", round_id)
    shutil.rmtree(runner.round_dir(round_id), ignore_errors=True)
    return back(f"round {round_id} deleted")


@app.get("/admin/rounds/{round_id}/log")
async def admin_round_log(request: Request, round_id: int):
    require_admin(request)
    d = runner.round_dir(round_id)
    parts = []
    for name in ("command.txt", "fastchess.log"):
        p = d / name
        if p.exists():
            text = p.read_text(errors="replace")
            parts.append(f"===== {name} =====\n{text[-300_000:]}")
    return PlainTextResponse("\n".join(parts) or "no log yet")


@app.post("/admin/players")
async def admin_add_player(request: Request, name: str = Form(...)):
    require_admin(request)
    name = name.strip().lower()
    if not NAME_RE.match(name):
        return back(err="name must be 1-24 chars: lowercase letters, digits, - or _")
    if db.q1("SELECT id FROM players WHERE name=?", name):
        return back(err=f"player '{name}' already exists")
    token = new_token()
    db.ex("INSERT INTO players(name, token_hash, created_at) VALUES(?,?,?)", name, hash_token(token), db.now())
    request.session["new_token"] = [name, token]
    return back(f"player {name} created")


@app.post("/admin/players/{player_id}/token")
async def admin_regen_token(request: Request, player_id: int):
    require_admin(request)
    p = db.q1("SELECT name FROM players WHERE id=?", player_id)
    if p is None:
        return back(err="no such player")
    token = new_token()
    db.ex("UPDATE players SET token_hash=? WHERE id=?", hash_token(token), player_id)
    request.session["new_token"] = [p["name"], token]
    return back(f"new token for {p['name']} (the old one no longer works)")


@app.post("/admin/players/{player_id}/active")
async def admin_toggle_player(request: Request, player_id: int, active: int = Form(...)):
    require_admin(request)
    db.ex("UPDATE players SET active=? WHERE id=?", 1 if active else 0, player_id)
    return back("player updated")


@app.post("/admin/submissions/{sub_id}/status")
async def admin_sub_status(request: Request, sub_id: int, action: str = Form(...)):
    require_admin(request)
    s = db.q1("SELECT * FROM submissions WHERE id=?", sub_id)
    if s is None:
        return back(err="no such submission")
    if action == "disable" and s["status"] == "ready":
        db.ex("UPDATE submissions SET status='disabled' WHERE id=?", sub_id)
    elif action == "enable" and s["status"] == "disabled":
        db.ex("UPDATE submissions SET status='ready' WHERE id=?", sub_id)
    elif action == "rebuild" and s["status"] in ("failed", "ready", "disabled"):
        if not (builder.submissions_dir() / str(sub_id) / "upload").exists():
            return back(err="upload not found on disk")
        db.ex("UPDATE submissions SET status='queued', log=? WHERE id=?", "== rebuild requested by admin\n", sub_id)
        builder.enqueue(sub_id)
    else:
        return back(err=f"cannot {action} a submission that is {s['status']}")
    return back(f"submission #{sub_id}: {action} ok")


@app.get("/admin/submissions/{sub_id}", response_class=HTMLResponse)
async def admin_submission(request: Request, sub_id: int):
    require_admin(request)
    s = db.q1("SELECT s.*, p.name FROM submissions s JOIN players p ON p.id=s.player_id WHERE s.id=?", sub_id)
    if s is None:
        raise HTTPException(404)
    return render(request, "submission.html", s=s)


@app.post("/admin/settings")
async def admin_settings(request: Request, tc: str = Form(...), rounds: int = Form(...), concurrency: int = Form(...),
                   timemargin: int = Form(...), engine_cpus: str = Form(...), engine_memory: str = Form(...),
                   maxmoves: int = Form(...), event_name: str = Form(...), adjudicate: str = Form(""),
                   public_url: str = Form("")):
    require_admin(request)
    public_url = public_url.strip().rstrip("/")
    if public_url and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", public_url):
        return back(err="public URL must look like http://1.2.3.4:8000 or https://chess.example.com")
    if not re.fullmatch(r"(\d+/)?\d+(\.\d+)?(:\d+)?(\+\d+(\.\d+)?)?", tc.strip()):
        return back(err="time control must look like 10+0.1 or 60+1 or 1:00+0.5")
    if not re.fullmatch(r"\d+(\.\d+)?", engine_cpus.strip()) or not re.fullmatch(r"\d+[kmg]", engine_memory.strip().lower()):
        return back(err="cpus must be a number (e.g. 1) and memory like 512m / 1g")
    if not 1 <= rounds <= 500:
        return back(err="openings per pairing must be 1-500")
    db.set_setting("tc", tc.strip())
    db.set_setting("rounds", rounds)
    db.set_setting("concurrency", max(0, concurrency))
    db.set_setting("timemargin", max(0, timemargin))
    db.set_setting("engine_cpus", engine_cpus.strip())
    db.set_setting("engine_memory", engine_memory.strip().lower())
    db.set_setting("maxmoves", max(0, maxmoves))
    db.set_setting("event_name", event_name.strip()[:60] or "Scacchackathon")
    db.set_setting("adjudicate", bool(adjudicate))
    db.set_setting("public_url", public_url)
    return back("settings saved (used from the next round)")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"error": exc.detail}, exc.status_code)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{html.escape(str(exc.detail))}</p><p><a href='/'>home</a></p>",
                        exc.status_code)
