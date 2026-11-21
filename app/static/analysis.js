// In-browser Stockfish 19 analysis for the game viewer. The engine runs in a Web Worker on the
// viewer's own machine, so it costs the tournament server nothing.
// Engine: stockfish.js lite single-threaded build (GPLv3), https://github.com/nmrugg/stockfish.js
const SF_WORKER = "/static/vendor/stockfish-19-lite-single.js";
const SF_DEPTH = 22;   // stop searching a position at this depth (a few seconds on a laptop)
const SF_LINES = 2;    // MultiPV: best move + runner-up

let _Chess = null;
async function loadChess() {
  if (!_Chess) _Chess = (await import("/static/vendor/chess.js")).Chess;
  return _Chess;
}

// Talks UCI to the worker. Only one search runs at a time: moving to another position sends
// "stop", waits for "bestmove", then starts the new one. Finished positions are cached.
class Analyser {
  constructor(onUpdate, onStatus) {
    this.onUpdate = onUpdate;   // (lines, fen, final)
    this.onStatus = onStatus;   // (text)
    this.worker = null;
    this.ready = false;
    this.target = null;         // position we want analysed
    this.current = null;        // position of the running search
    this.stopping = false;      // "stop" sent, waiting for "bestmove"
    this.lines = [];
    this.cache = new Map();
  }

  start() {
    if (this.worker) return;
    this.onStatus("loading Stockfish…");
    try {
      this.worker = new Worker(SF_WORKER);
    } catch (e) {
      this.onStatus("couldn't start Stockfish in this browser");
      return;
    }
    this.worker.onerror = () => this.onStatus("couldn't load Stockfish");
    this.worker.onmessage = (ev) => this._line(String(ev.data));
    this._send("uci");
  }

  stop() {
    if (this.worker) this.worker.terminate();
    this.worker = null; this.ready = false; this.current = null; this.stopping = false;
  }

  analyse(fen) {
    this.target = fen;
    const cached = this.cache.get(fen);
    if (cached) {
      this.onUpdate(cached, fen, true);
      this._halt();
      return;
    }
    if (!this.worker || !this.ready) return;
    if (this.current === fen && !this.stopping) return;
    if (this.current !== null) { this._halt(); return; }
    this._go();
  }

  // Nothing to analyse (e.g. checkmate on the board).
  idle() { this.target = null; this._halt(); }

  _halt() {
    if (this.current !== null && !this.stopping) { this.stopping = true; this._send("stop"); }
  }

  _send(cmd) { this.worker.postMessage(cmd); }

  _go() {
    this.current = this.target; this.lines = []; this.stopping = false;
    this._send("position fen " + this.current);
    this._send(`go depth ${SF_DEPTH}`);
  }

  _line(s) {
    if (s === "uciok") {
      this._send(`setoption name MultiPV value ${SF_LINES}`);
      this._send("setoption name Hash value 32");
      this._send("isready");
    } else if (s === "readyok") {
      this.ready = true;
      this.onStatus("");
      if (this.target && !this.cache.has(this.target)) this._go();
    } else if (s.startsWith("bestmove")) {
      const finished = this.current, wasStopped = this.stopping;
      this.current = null; this.stopping = false;
      if (!wasStopped && finished === this.target) {
        this.cache.set(finished, this.lines);
        this.onUpdate(this.lines, finished, true);
      } else if (this.target && !this.cache.has(this.target)) {
        this._go();
      }
    } else if (s.startsWith("info ") && !this.stopping && this.current) {
      const info = parseInfo(s, this.current);
      if (!info) return;
      this.lines[info.multipv - 1] = info;
      this.onUpdate(this.lines, this.current, false);
    }
  }
}

// "info depth 18 ... multipv 1 score cp 34 ... pv e2e4 e7e5" -> {depth, multipv, score, pv}
// Stockfish scores from the side to move; we return them from White's view.
function parseInfo(s, fen) {
  const t = s.split(" ");
  let depth = 0, multipv = 1, score = null, bound = false, pv = null;
  for (let i = 1; i < t.length; i++) {
    if (t[i] === "depth") depth = +t[++i];
    else if (t[i] === "multipv") multipv = +t[++i];
    else if (t[i] === "score") {
      const kind = t[++i], v = +t[++i];
      score = kind === "mate" ? { mate: v } : { cp: v };
      if (t[i + 1] === "lowerbound" || t[i + 1] === "upperbound") bound = true;
    } else if (t[i] === "pv") { pv = t.slice(i + 1); break; }
  }
  if (!score || bound || !pv || !pv.length) return null;
  if (fen.split(" ")[1] === "b") score = score.mate !== undefined ? { mate: -score.mate } : { cp: -score.cp };
  return { depth, multipv, score, pv };
}

function fmtScore(sc) {
  if (!sc) return "";
  if (sc.mate !== undefined) return sc.mate > 0 ? `#${sc.mate}` : `#-${-sc.mate}`;
  const v = sc.cp / 100;
  return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1);
}

// White's share of the eval bar, 0..100.
function scorePct(sc) {
  if (!sc) return 50;
  if (sc.mate !== undefined) return sc.mate > 0 ? 100 : 0;
  return 50 + 50 * (2 / (1 + Math.exp(-sc.cp / 250)) - 1);
}

// UCI principal variation -> "12. Nf3 d5 13. c4 …" (at most maxPlies moves).
function pvToSan(Chess, fen, uci, maxPlies = 12) {
  const board = new Chess(fen);
  const parts = fen.split(" ");
  let black = parts[1] === "b", n = parseInt(parts[5] || "1");
  const out = [];
  for (let i = 0; i < Math.min(uci.length, maxPlies); i++) {
    const u = uci[i];
    let m = null;
    try { m = board.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] }); } catch (e) { break; }
    if (!m) break;
    if (!black) out.push(`<span class="no">${n}.</span>`);
    else if (i === 0) out.push(`<span class="no">${n}…</span>`);
    out.push(m.san);
    if (black) n++;
    black = !black;
  }
  return out.join(" ");
}

// Engine arrows on an SVG laid over the board (viewBox 0 0 8 8, one unit per square).
function drawArrows(svg, arrows, flipped) {
  const centre = (sq) => {
    const f = "abcdefgh".indexOf(sq[0]), r = +sq[1] - 1;
    return flipped ? [7 - f + 0.5, r + 0.5] : [f + 0.5, 7 - r + 0.5];
  };
  let html = "";
  for (const a of arrows) {
    if (!a.uci || a.uci.length < 4) continue;
    const [x1, y1] = centre(a.uci.slice(0, 2)), [x2, y2] = centre(a.uci.slice(2, 4));
    const len = Math.hypot(x2 - x1, y2 - y1);
    if (!len) continue;
    const ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    const head = 0.42, hw = 0.25;                 // arrow head length / half-width, in squares
    const bx = x2 - ux * head, by = y2 - uy * head;
    const f = (v) => v.toFixed(3);
    html += `<g class="arrow ${a.cls}">` +
      `<line x1="${f(x1)}" y1="${f(y1)}" x2="${f(bx)}" y2="${f(by)}"/>` +
      `<polygon points="${f(x2)},${f(y2)} ${f(bx - uy * hw)},${f(by + ux * hw)} ${f(bx + uy * hw)},${f(by - ux * hw)}"/></g>`;
  }
  svg.innerHTML = html;
}
