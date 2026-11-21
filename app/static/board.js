// Minimal FEN board renderer + game viewer. No dependencies.
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const FILES = "abcdefgh";

function renderBoard(el, fen, lastMove, flipped) {
  const rows = fen.split(" ")[0].split("/");
  const grid = [];
  for (const row of rows) {
    const r = [];
    for (const ch of row) {
      if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) r.push(null);
      else r.push(ch);
    }
    grid.push(r);
  }
  const hl = new Set();
  if (lastMove) { hl.add(lastMove.slice(0, 2)); hl.add(lastMove.slice(2, 4)); }
  let html = "";
  for (let i = 0; i < 8; i++) {
    for (let j = 0; j < 8; j++) {
      const rank = flipped ? i : 7 - i, file = flipped ? 7 - j : j;
      const p = grid[7 - rank][file];
      const name = FILES[file] + (rank + 1);
      const cls = ((rank + file) % 2 === 1 ? "l" : "d") + (hl.has(name) ? " hl" : "");
      let inner = "";
      if (p) inner += `<span class="pc ${p === p.toUpperCase() ? "w" : "b"}">${GLYPH[p.toLowerCase()]}︎</span>`;
      if (i === 7) inner += `<span class="coord f">${FILES[file]}</span>`;
      if (j === 0) inner += `<span class="coord r">${rank + 1}</span>`;
      html += `<div class="sq ${cls}">${inner}</div>`;
    }
  }
  el.innerHTML = html;
}

// Resolves true if the text reached the clipboard, false if the browser refused.
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
  }
  // http:// pages have no clipboard API: fall back to a hidden textarea.
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; } finally { ta.remove(); }
  return ok;
}

// Board colour themes: [id, label, light, dark]. "" = the default brown board.
const BOARD_THEMES = [
  ["", "Brown", "#f0d9b5", "#b58863"], ["sand", "Sand", "#ebe4d3", "#9c8a6b"], ["green", "Green", "#eeeed2", "#769656"],
  ["blue", "Blue", "#dee3e6", "#8ca2ad"], ["grey", "Grey", "#d9d9d9", "#8f8f8f"], ["purple", "Purple", "#e6e0f0", "#8877b7"],
];

function initThemePicker(el) {
  const current = () => { const t = document.documentElement.dataset.board || ""; return t === "brown" ? "" : t; };
  el.innerHTML = BOARD_THEMES.map(([id, label, l, d]) =>
    `<button type="button" data-theme="${id}" title="${label} board" aria-label="${label} board" style="--l:${l};--d:${d}"></button>`).join("");
  const mark = () => el.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.theme === current()));
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    const t = b.dataset.theme;
    if (t) document.documentElement.dataset.board = t; else delete document.documentElement.dataset.board;
    try { localStorage.setItem("scacc.board", t); } catch (e) { /* private mode */ }
    mark();
  });
  mark();
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function initViewer(gameId) {
  const boardEl = document.getElementById("board");
  const movesEl = document.getElementById("moves");
  const vbar = document.getElementById("vbar");
  const vfill = document.getElementById("vfill");
  const evalTxt = document.getElementById("evaltxt");
  const pgnEl = document.getElementById("pgntext");
  let data = null, ply = 0, flipped = false, follow = true, timer = null;

  function evalFor(p) {
    // evals are indexed from the first non-book ply; fastchess reports them from white's view
    if (!data || p <= data.book_plies) return null;
    const e = data.evals[p - data.book_plies - 1];
    if (!e) return null;
    if (e.mate !== undefined) return { mate: e.mate, d: e.d };
    if (e.cp !== undefined && Math.abs(e.cp) >= 20000) return { mate: e.cp > 0 ? "+" : "-", d: e.d };
    if (e.cp !== undefined) return { cp: e.cp, d: e.d };
    return null;
  }

  function players() {
    const score = { "1-0": ["1", "0"], "0-1": ["0", "1"], "1/2-1/2": ["½", "½"] }[data.result] || ["", ""];
    const line = (name, color, sc) =>
      `<span class="pl-name"><span class="dot ${color}"></span>${esc(name)}<span class="score">${sc}</span></span>` +
      (data.clocks ? `<span class="clock" data-side="${color}" title="time left on ${color === "w" ? "white" : "black"}'s clock"></span>` : "");
    const w = line(data.white, "w", score[0]), b = line(data.black, "b", score[1]);
    document.getElementById("pl-top").innerHTML = flipped ? w : b;
    document.getElementById("pl-bottom").innerHTML = flipped ? b : w;
  }

  // Clock as on chess.com: 0:08.4 (tenths under 20 s), 1:05 above.
  function fmtClock(ms) {
    const neg = ms < 0, t = Math.abs(ms);
    const m = Math.floor(t / 60000), sec = Math.floor(t / 1000) % 60;
    const body = t < 20000 ? `${m}:${String(sec).padStart(2, "0")}.${Math.floor(t / 100) % 10}` : `${m}:${String(sec).padStart(2, "0")}`;
    return (neg ? "−" : "") + body;
  }
  // Thinking time per move: <1ms, 64ms, 1.4s, 12s. fastchess counts whole milliseconds, so 0 means under 1 ms.
  function fmtThink(ms) {
    if (ms < 1) return "<1ms";
    if (ms < 1000) return ms + "ms";
    if (ms < 10000) return (ms / 1000).toFixed(1) + "s";
    return Math.round(ms / 1000) + "s";
  }

  function showClocks(elapsedMs = 0) {
    if (!data.clocks) return;
    const c = [...(data.clocks[ply] || data.clocks[data.clocks.length - 1])];
    if (elapsedMs) c[data.fens[ply].split(" ")[1] === "w" ? 0 : 1] -= elapsedMs;
    const toMove = data.fens[ply].split(" ")[1];
    const atEnd = ply === data.fens.length - 1 && data.status !== "live";
    // fastchess says who flagged ("Black loses on time (121ms overrun)"); that clock shows 0:00.0 in red
    const lost = atEnd && /^(white|black) loses on time/i.exec(data.reason || "");
    const flagged = lost ? (lost[1].toLowerCase() === "white" ? "w" : "b") : null;
    document.querySelectorAll(".clock").forEach(el => {
      const side = el.dataset.side, ms = side === "w" ? c[0] : c[1];
      el.textContent = fmtClock(side === flagged ? 0 : Math.max(ms, 0));
      el.classList.toggle("active", !atEnd && side === toMove);
      // "low on time": under 15% of the starting clock (at most 10 s)
      el.classList.toggle("low", side === flagged || ms < Math.min(10000, 0.15 * data.clocks[0][0]));
      el.classList.toggle("flag", side === flagged);
    });
  }

  function show(p) {
    ply = Math.max(0, Math.min(p, data.fens.length - 1));
    renderBoard(boardEl, data.fens[ply], ply > 0 ? data.moves[ply - 1] : null, flipped);
    movesEl.querySelectorAll(".mv").forEach(m => m.classList.toggle("on", +m.dataset.ply === ply));
    showClocks();
    const on = movesEl.querySelector(".mv.on");
    if (on) {
      const top = on.offsetTop - movesEl.offsetTop;
      if (top < movesEl.scrollTop || top > movesEl.scrollTop + movesEl.clientHeight - 30)
        movesEl.scrollTop = top - movesEl.clientHeight / 2;
    } else if (ply === 0) movesEl.scrollTop = 0;
    // Without Stockfish the bar shows the most recent score either player's engine reported
    // (fastchess reports them from White's view).
    let e = null;
    for (let p = ply; p > data.book_plies && !e; p--) e = evalFor(p);
    let pct = 50;
    if (e && e.mate !== undefined) pct = (e.mate === "+" || e.mate > 0) ? 100 : 0;
    else if (e) pct = 50 + 50 * (2 / (1 + Math.exp(-e.cp / 250)) - 1);
    evalTxt.innerHTML = engineEvalsLine();
    vbar.classList.toggle("flipped", flipped);
    if (sf.on) analyseShown();
    else { setBar(pct); drawArrows(arrowsEl, [], flipped); }
  }

  // The score the engine that played the shown move reported while choosing it
  // (an engine only reports on its own moves; fastchess gives it from White's view).
  function fmtEval(e) {
    if (e.mate !== undefined) return typeof e.mate === "number" ? `#${e.mate}` : (e.mate === "+" ? "white mates" : "black mates");
    return (e.cp > 0 ? "+" : "") + (e.cp / 100).toFixed(2);
  }
  function engineEvalsLine() {
    if (ply === 0) return "";
    if (ply <= data.book_plies) return "opening book move (not chosen by the engines)";
    const mover = data.fens[ply].split(" ")[1] === "b" ? data.white : data.black;
    const e = evalFor(ply);
    if (!e) return `<b>${esc(mover)}</b>'s engine gave no score for this move`;
    return `<b>${esc(mover)}</b>'s engine evaluated this move at <b>${fmtEval(e)}</b>` +
      ` <span class="muted">(${e.d ? `depth ${e.d}, ` : ""}white's view)</span>`;
  }

  function setBar(pct) { vfill.style.height = pct.toFixed(1) + "%"; }

  // ---- Stockfish analysis (see analysis.js) ----
  const arrowsEl = document.getElementById("arrows");
  const sfToggle = document.getElementById("sf-toggle");
  const sfScore = document.getElementById("sf-score");
  const sfInfo = document.getElementById("sf-info");
  const pvsEl = document.getElementById("pvs");
  const sf = { on: false, an: null, Chess: null, last: null, raf: 0 };

  function renderAnalysis(lines, fen, final) {
    if (!sf.on || fen !== data.fens[ply]) return;
    sf.last = { lines, fen, final };
    if (sf.raf) return;                       // at most one repaint per frame
    sf.raf = requestAnimationFrame(() => {
      sf.raf = 0;
      const { lines, fen, final } = sf.last;
      if (!sf.on || fen !== data.fens[ply]) return;
      const best = lines[0];
      if (!best) return;
      sfScore.textContent = fmtScore(best.score);
      sfInfo.textContent = `depth ${best.depth}${final ? "" : "…"}`;
      setBar(scorePct(best.score));
      pvsEl.innerHTML = lines.filter(Boolean).map((l, i) =>
        `<div class="pv${i ? " alt" : ""}"><b>${fmtScore(l.score)}</b> ${sf.Chess ? pvToSan(sf.Chess, fen, l.pv) : esc(l.pv.join(" "))}</div>`).join("");
      drawArrows(arrowsEl, lines.filter(Boolean).map((l, i) => ({ uci: l.pv[0], cls: i ? "alt" : "best" })).reverse(), flipped);
    });
  }

  async function analyseShown() {
    const fen = data.fens[ply];
    if (!sf.Chess) {
      try { sf.Chess = await loadChess(); } catch (e) { /* SAN falls back to UCI */ }
      if (!sf.on || fen !== data.fens[ply]) return;
    }
    if (sf.Chess) {
      const b = new sf.Chess(fen);
      if (b.isCheckmate() || b.isStalemate()) {
        sf.an.idle();
        const whiteWon = b.isCheckmate() && b.turn() === "b";
        sfScore.textContent = b.isStalemate() ? "½-½" : whiteWon ? "1-0" : "0-1";
        sfInfo.textContent = b.isStalemate() ? "stalemate" : "checkmate";
        setBar(b.isStalemate() ? 50 : whiteWon ? 100 : 0);
        pvsEl.innerHTML = ""; drawArrows(arrowsEl, [], flipped);
        return;
      }
    }
    if (sf.last && sf.last.fen === fen) renderAnalysis(sf.last.lines, fen, sf.last.final);
    else { pvsEl.innerHTML = ""; drawArrows(arrowsEl, [], flipped); }
    sf.an.analyse(fen);
  }

  function setAnalysis(on) {
    sf.on = on; sfToggle.checked = on;
    try { localStorage.setItem("scacc.analysis", on ? "1" : "0"); } catch (e) { /* private mode */ }
    pvsEl.hidden = !on;
    sfScore.textContent = "";
    if (on) {
      if (!sf.an) sf.an = new Analyser(renderAnalysis, (t) => {
        // status messages ("loading…", errors) must not wipe a "checkmate"/depth label
        if (t) sfInfo.textContent = t;
        else if (sfInfo.textContent.startsWith("loading")) sfInfo.textContent = "";
      });
      sf.an.start();
    } else {
      if (sf.an) { sf.an.stop(); sf.an = null; }
      sf.last = null; pvsEl.innerHTML = "";
      sfInfo.textContent = "analysis is off";
    }
    if (data) show(ply);
  }
  sfToggle.onchange = () => setAnalysis(sfToggle.checked);

  function renderMoves() {
    let html = "";
    const startBlack = data.fens[0].includes(" b ");
    let moveNo = parseInt(data.fens[0].split(" ")[5] || "1");
    // Each move shows how long its engine thought, with a bar scaled to the longest think of the game.
    const think = data.think_ms || [];
    const maxMs = Math.max(1, ...think.filter(x => x != null));
    const cell = (i) => {
      const book = i + 1 <= data.book_plies, ms = think[i];
      const t = book ? `<span class="t">book</span>`
        : ms != null ? `<span class="t" title="${ms < 1 ? "answered in under 1 ms" : `thought for ${ms} ms`}"><i style="width:${Math.max(2, 100 * ms / maxMs).toFixed(0)}%"></i>${fmtThink(ms)}</span>` : "";
      return `<span class="mv${book ? " book" : ""}" data-ply="${i + 1}"><span class="san">${esc(data.san[i])}</span>${t}</span>`;
    };
    let i = 0;
    if (startBlack && data.san.length) { html += `<span class="no">${moveNo}</span><span class="mv empty">…</span>${cell(0)}`; i = 1; moveNo++; }
    for (; i < data.san.length; i += 2, moveNo++) {
      html += `<span class="no">${moveNo}</span>${cell(i)}`;
      html += i + 1 < data.san.length ? cell(i + 1) : `<span class="mv empty"></span>`;
    }
    movesEl.innerHTML = html;
    movesEl.querySelectorAll(".mv[data-ply]").forEach(m => m.onclick = () => { follow = false; show(+m.dataset.ply); });
    const end = document.getElementById("endline");
    end.innerHTML = data.status === "live" ? '<span class="pill live">live</span>'
      : `<b>${esc(data.result)}</b> <span class="muted">${esc(data.reason || data.termination || data.status)}</span>`;
  }

  // Live games: the side to move's clock counts down between updates, starting when we first saw the move.
  let lastPlySeen = -1, lastPlyAt = 0;
  setInterval(() => {
    if (!data || data.status !== "live" || !data.clocks || ply !== data.fens.length - 1) return;
    showClocks(performance.now() - lastPlyAt);
  }, 100);

  async function load() {
    const d = await (await fetch(`/api/games/${gameId}`)).json();
    const atEnd = !data || ply === data.fens.length - 1;
    if (d.fens.length - 1 !== lastPlySeen) { lastPlySeen = d.fens.length - 1; lastPlyAt = performance.now(); }
    data = d;
    renderMoves();
    players();
    if (!pgnEl.hidden) pgnEl.value = d.pgn;
    show(follow && atEnd ? d.fens.length - 1 : ply);
    if (d.status === "live") setTimeout(load, 1500);
  }

  function step(n) { follow = false; show(ply + n); }
  const playBtn = document.getElementById("play");
  function togglePlay() {
    if (timer) { clearInterval(timer); timer = null; playBtn.textContent = "▶"; return; }
    if (ply >= data.fens.length - 1) show(0);
    playBtn.textContent = "❚❚";
    timer = setInterval(() => {
      if (ply >= data.fens.length - 1) { clearInterval(timer); timer = null; playBtn.textContent = "▶"; return; }
      show(ply + 1);
    }, 700);
  }
  document.getElementById("first").onclick = () => step(-9999);
  document.getElementById("prev").onclick = () => step(-1);
  document.getElementById("next").onclick = () => step(1);
  document.getElementById("last").onclick = () => { follow = true; show(9999); };
  document.getElementById("flip").onclick = () => { flipped = !flipped; players(); show(ply); };
  playBtn.onclick = togglePlay;
  document.getElementById("copypgn").onclick = async () => {
    const c = document.getElementById("copied");
    if (await copyText(data.pgn)) {
      c.textContent = "Copied ✓"; setTimeout(() => c.textContent = "", 2000);
    } else {
      // Browser refused clipboard access: show the PGN selected so Ctrl/Cmd+C works.
      pgnEl.hidden = false; pgnEl.value = data.pgn; pgnEl.focus(); pgnEl.select();
      document.getElementById("showpgn").textContent = "Hide PGN";
      c.textContent = "Press Ctrl/Cmd+C to copy";
    }
  };
  document.getElementById("showpgn").onclick = (ev) => {
    pgnEl.hidden = !pgnEl.hidden;
    pgnEl.value = data.pgn;
    ev.target.textContent = pgnEl.hidden ? "Show PGN" : "Hide PGN";
  };
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "ArrowUp" || e.key === "Home") step(-9999);
    else if (e.key === "ArrowDown" || e.key === "End") { follow = true; show(9999); }
    else if (e.key === "f") document.getElementById("flip").click();
    else if (e.key === "l") setAnalysis(!sf.on);
    else if (e.key === " ") { e.preventDefault(); togglePlay(); }
    else return;
  });
  initThemePicker(document.getElementById("themes"));
  let wantAnalysis = false;
  try { wantAnalysis = localStorage.getItem("scacc.analysis") === "1"; } catch (e) { /* private mode */ }
  if (wantAnalysis) setAnalysis(true);
  load();
}

// Mini boards for the live-games grid.
async function initMiniBoards() {
  const els = document.querySelectorAll("[data-mini-game]");
  for (const el of els) {
    try {
      const d = await (await fetch(`/api/games/${el.dataset.miniGame}`)).json();
      renderBoard(el, d.fens[d.fens.length - 1], d.moves[d.moves.length - 1], false);
    } catch (e) { /* ignore */ }
  }
}

// Reload the page when the public state changes (new games, new submissions...).
function autoRefresh(intervalMs) {
  let sig = null;
  setInterval(async () => {
    try {
      const s = await (await fetch("/api/state")).json();
      if (sig !== null && s.sig !== sig) location.reload();
      sig = s.sig;
    } catch (e) { /* offline, retry */ }
  }, intervalMs);
  initMiniBoards();
  setInterval(initMiniBoards, 2000);
}

// Client-side filters for the games table on a round page. State is kept in the URL hash
// so a filtered view can be shared and survives the auto-refresh while a round is running.
function initGameFilters(live) {
  const ids = ["player", "colour", "opp", "result", "end"];
  const el = Object.fromEntries(ids.map(k => [k, document.getElementById("f-" + k)]));
  const rows = [...document.querySelectorAll("#games tr[data-href]")];
  const params = new URLSearchParams(location.hash.slice(1));
  ids.forEach(k => { if (params.get(k)) el[k].value = params.get(k); });

  function apply() {
    const f = Object.fromEntries(ids.map(k => [k, el[k].value]));
    // colour / win / loss only make sense relative to a chosen player
    el.colour.disabled = !f.player;
    let shown = 0;
    for (const r of rows) {
      const { w, b, result, end } = r.dataset;
      let ok = true;
      if (f.player) {
        const side = w === f.player ? "w" : b === f.player ? "b" : null;
        const opp = side === "w" ? b : w;
        if (!side) ok = false;
        else {
          if (f.colour && side !== f.colour) ok = false;
          if (f.opp && opp !== f.opp) ok = false;
          const won = (side === "w" && result === "1-0") || (side === "b" && result === "0-1");
          const lost = (side === "w" && result === "0-1") || (side === "b" && result === "1-0");
          if (f.result === "win" && !won) ok = false;
          if (f.result === "loss" && !lost) ok = false;
        }
      } else {
        if (f.opp && w !== f.opp && b !== f.opp) ok = false;
        if ((f.result === "win" || f.result === "loss") && !(result === "1-0" || result === "0-1")) ok = false;
      }
      if (f.result === "draw" && result !== "1/2-1/2") ok = false;
      if (["1-0", "0-1", "live"].includes(f.result) && result !== f.result) ok = false;
      if (f.end && end !== f.end) ok = false;
      r.hidden = !ok;
      if (ok) shown++;
    }
    document.getElementById("noresults").hidden = shown > 0;
    document.getElementById("gcount").textContent = shown === rows.length ? `${rows.length} games` : `showing ${shown} of ${rows.length}`;
    const h = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
    history.replaceState(null, "", h ? "#" + h : location.pathname);
  }
  ids.forEach(k => el[k].addEventListener("change", apply));
  document.getElementById("f-reset").onclick = () => { ids.forEach(k => el[k].value = ""); apply(); };
  rows.forEach(r => r.addEventListener("click", (e) => { if (!e.target.closest("a")) location.href = r.dataset.href; }));
  apply();
  if (live) autoRefresh(5000);
}
