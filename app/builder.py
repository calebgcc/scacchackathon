"""Turns an uploaded archive into a tested Docker image.

Pipeline per submission: extract -> docker build -> UCI smoke test -> ready | failed.
"""
import asyncio
import shutil
import tarfile
import zipfile
from pathlib import Path

import chess

from . import db
from .config import BUILD_TIMEOUT_S, BUILD_WORKERS, DATA_DIR, DOCKER

MAX_EXTRACTED = 300 * 1024 * 1024
MAX_FILES = 20000
LOG_LIMIT = 200_000  # characters kept in the db per submission

_sem: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()


def submissions_dir() -> Path:
    p = Path(DATA_DIR) / "submissions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def image_name(player_name: str, version: int) -> str:
    return f"scacc-engine/{player_name}:v{version}"


def engine_run_args(image: str, label: str, cpus: str, memory: str) -> list[str]:
    """`docker run` arguments shared by the smoke test and tournament games."""
    return ["run", "-i", "--rm", "--init", "--network", "none",
            f"--cpus={cpus}", f"--memory={memory}", f"--memory-swap={memory}",
            "--pids-limit", "256", "--label", label, image]


class Log:
    """Accumulates build output and periodically writes it to the db."""

    def __init__(self, sub_id: int):
        self.sub_id = sub_id
        self.text = ""

    def add(self, s: str) -> None:
        self.text += s
        if len(self.text) > LOG_LIMIT:
            self.text = "[... log truncated ...]\n" + self.text[-LOG_LIMIT:]

    def line(self, s: str) -> None:
        self.add(s.rstrip("\n") + "\n")

    def flush(self) -> None:
        db.ex("UPDATE submissions SET log=? WHERE id=?", self.text, self.sub_id)


def set_status(sub_id: int, status: str, **extra) -> None:
    cols = ", ".join(f"{k}=?" for k in extra)
    sql = f"UPDATE submissions SET status=?{', ' + cols if cols else ''} WHERE id=?"
    db.ex(sql, status, *extra.values(), sub_id)


# --- extraction ------------------------------------------------------------

def extract(archive: Path, dest: Path) -> Path:
    """Extract archive safely; return the directory that contains the Dockerfile."""
    dest.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                count += 1
                total += info.file_size
                if count > MAX_FILES or total > MAX_EXTRACTED:
                    raise ValueError("archive too large")
                target = (dest / info.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise ValueError(f"unsafe path in archive: {info.filename}")
            z.extractall(dest)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            members = t.getmembers()
            for m in members:
                count += 1
                total += max(m.size, 0)
                if count > MAX_FILES or total > MAX_EXTRACTED:
                    raise ValueError("archive too large")
            t.extractall(dest, filter="data")
    else:
        raise ValueError("upload is not a .tar.gz / .tgz / .tar / .zip archive")

    if (dest / "Dockerfile").is_file():
        return dest
    # Accept archives that wrap everything in one top-level folder.
    entries = [p for p in dest.iterdir() if p.name not in ("__MACOSX",) and not p.name.startswith("._")]
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / "Dockerfile").is_file():
        return entries[0]
    found = sorted(str(p.relative_to(dest)) for p in dest.rglob("Dockerfile"))
    hint = f" (found: {', '.join(found[:3])})" if found else ""
    raise ValueError("no Dockerfile at the top level of the archive" + hint)


# --- subprocess helpers ----------------------------------------------------

async def run_logged(args: list[str], log: Log, timeout: float) -> int:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def pump():
        last_flush = 0.0
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            log.add(chunk.decode(errors="replace"))
            t = asyncio.get_running_loop().time()
            if t - last_flush > 1:
                log.flush()
                last_flush = t

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.line(f"\n!! timed out after {timeout:.0f}s")
        return -1
    finally:
        log.flush()
    return proc.returncode


# --- UCI smoke test --------------------------------------------------------

SMOKE_POSITIONS = [
    # (position command, description)
    ("position startpos", "start position, white to move"),
    ("position startpos moves e2e4 e7e5 g1f3", "after 1.e4 e5 2.Nf3, black to move"),
    ("position fen r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
     "FEN position (white has mate in one)"),
    ("position fen 8/P5k1/8/8/8/8/6K1/8 w - - 0 1 moves g2f3 g7f7", "FEN + moves, promotion available"),
]


def board_for(cmd: str) -> chess.Board:
    tokens = cmd.split()
    if tokens[1] == "startpos":
        board, rest = chess.Board(), tokens[2:]
    else:
        board, rest = chess.Board(" ".join(tokens[2:8])), tokens[8:]
    if rest and rest[0] == "moves":
        for m in rest[1:]:
            board.push_uci(m)
    return board


async def smoke_test(image: str, log: Log, cpus: str, memory: str) -> tuple[bool, str | None]:
    args = engine_run_args(image, "scacc.smoke=1", cpus, memory)
    log.line(f"$ docker {' '.join(args)}")
    proc = await asyncio.create_subprocess_exec(
        DOCKER, *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    engine_name = None

    async def send(s: str):
        log.line(f"> {s}")
        proc.stdin.write((s + "\n").encode())
        await proc.stdin.drain()

    async def expect(prefix: str, timeout: float) -> str:
        nonlocal engine_name
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            msg = (f"no '{prefix}' line within {timeout:.0f}s - does the engine flush stdout after every line?"
                   if prefix != "bestmove" else f"no 'bestmove' within {timeout:.0f}s (clock was 10s)")
            if remaining <= 0:
                raise TimeoutError(msg)
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(msg) from None
            if not raw:
                raise EOFError("engine exited (stdout closed)")
            line = raw.decode(errors="replace").strip()
            if line:
                log.line(f"< {line}")
            if line.startswith("id name "):
                engine_name = line[8:].strip()[:60]
            if line.split(" ")[0] == prefix:
                return line

    ok = False
    try:
        await send("uci")
        await expect("uciok", 30)
        await send("isready")
        await expect("readyok", 10)
        for cmd, desc in SMOKE_POSITIONS:
            log.line(f"# test: {desc}")
            await send("ucinewgame")
            await send("isready")
            await expect("readyok", 10)
            await send(cmd)
            await send("go wtime 10000 btime 10000 winc 100 binc 100")
            line = await expect("bestmove", 6)
            parts = line.split()
            if len(parts) < 2:
                raise ValueError("bestmove without a move")
            board = board_for(cmd)
            try:
                move = chess.Move.from_uci(parts[1])
            except ValueError:
                raise ValueError(f"'{parts[1]}' is not a UCI move (expected e.g. e2e4, e7e8q)")
            if move not in board.legal_moves:
                raise ValueError(f"illegal move {parts[1]} in position: {board.fen()}")
            log.line(f"# ok: {board.san(move)}")
        await send("quit")
        ok = True
    except (TimeoutError, asyncio.TimeoutError, EOFError, ValueError, BrokenPipeError, ConnectionResetError) as e:
        log.line(f"!! smoke test failed: {e}")
    finally:
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        if err:
            log.line("--- engine stderr ---")
            log.add(err[-5000:] + "\n")
        log.flush()
    return ok, engine_name


# --- pipeline ----------------------------------------------------------------

async def process(sub_id: int) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(BUILD_WORKERS)
    row = db.q1("SELECT s.*, p.name AS player FROM submissions s JOIN players p ON p.id=s.player_id WHERE s.id=?", sub_id)
    log = Log(sub_id)
    log.text = row["log"] or ""
    base = submissions_dir() / str(sub_id)
    archive = base / "upload"
    async with _sem:
        try:
            set_status(sub_id, "building")
            log.line(f"== submission #{sub_id}: {row['player']} v{row['version']}")
            src_root = base / "src"
            if src_root.exists():
                shutil.rmtree(src_root)
            ctx = extract(archive, src_root)
            log.line(f"== extracted, building image from {ctx.relative_to(base)}")
            image = image_name(row["player"], row["version"])
            rc = await run_logged(
                [DOCKER, "build", "--progress=plain", "-t", image,
                 "--label", "scacc.engine=1", "--label", f"scacc.player={row['player']}", str(ctx)],
                log, BUILD_TIMEOUT_S)
            if rc != 0:
                log.line(f"!! docker build failed (exit {rc})")
                set_status(sub_id, "failed", finished_at=db.now())
                return
            set_status(sub_id, "testing", image=image)
            log.line("== build ok, running UCI smoke test")
            ok, name = await smoke_test(image, log, db.get_setting("engine_cpus"), db.get_setting("engine_memory"))
            if ok:
                log.line("== READY: this version will play in the next round")
                set_status(sub_id, "ready", finished_at=db.now(), engine_name=name)
            else:
                set_status(sub_id, "failed", finished_at=db.now(), engine_name=name)
        except Exception as e:  # noqa: BLE001 - report anything to the player
            log.line(f"!! {type(e).__name__}: {e}")
            set_status(sub_id, "failed", finished_at=db.now())
        finally:
            log.flush()
            shutil.rmtree(base / "src", ignore_errors=True)


def enqueue(sub_id: int) -> None:
    task = asyncio.get_running_loop().create_task(process(sub_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def resume_pending() -> None:
    """After a restart, re-run anything that was mid-pipeline."""
    for row in db.q("SELECT id FROM submissions WHERE status IN ('queued','building','testing') ORDER BY id"):
        db.ex("UPDATE submissions SET status='queued', log=log || ? WHERE id=?",
              "\n== server restarted, retrying\n", row["id"])
        enqueue(row["id"])
