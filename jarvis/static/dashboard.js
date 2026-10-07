// FRIDAY dashboard (Phase 10B). READ-ONLY: this page only ever sends GET /api/dashboard.
// Every value shown comes from FRIDAY's recorded state; nothing is invented and nothing can trade.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const SVG = "http://www.w3.org/2000/svg";
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const svg = (tag, attrs, text) => { const e = document.createElementNS(SVG, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };
  const money = (x) => x == null ? "—" : (x < 0 ? "−$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const signed = (x) => x == null ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + "$" + Math.abs(x).toFixed(2);
  const pct = (x, d) => x == null ? "—" : (x * 100).toFixed(d == null ? 1 : d) + "%";
  const tone = (x) => x > 0 ? "pos" : x < 0 ? "neg" : "";
  const compact = (x) => x == null ? "—" : x >= 999500 ? "$" + (x / 1e6).toFixed(1) + "M" : x >= 1e3 ? "$" + Math.round(x / 1e3) + "K" : "$" + Math.round(x);
  const hhmm = (iso) => { const d = new Date(iso); return isNaN(d) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  const failed = (part) => !part || part.error;

  const STATUS = { APPROVED_FOR_PAPER: ["approved", "Approved for paper"], FORWARD_TEST: ["forward", "Forward test"], NOT_APPROVED: ["rejected", "Not approved"] };
  const NAMES = { CALIBRATION_EDGE: "Calibration edge", BOOK_IMBALANCE: "Book imbalance", MEAN_REVERSION: "Mean reversion", MOMENTUM: "Momentum (order flow)",
                  LONGSHOT_BIAS: "Long-shot bias", PRICING_INCONSISTENCY: "Pricing inconsistency", RESEARCH_FORECAST: "Research forecast" };

  function evidence(s) {
    const e = s.evidence || {};
    if (e.forward) {
      const got = e.trades ? e.trades + " paper trades on " + e.days + " days, " + (e.pnl < 0 ? "−$" + Math.abs(e.pnl).toFixed(2) : "$" + e.pnl.toFixed(2)) + " so far. " : "No paper trades yet. ";
      if (e.promoted_by_owner) return got + "Promoted by the owner after its forward test succeeded. Re-judged on every trade.";
      if (e.standing === "SUCCEEDED") return got + "The forward test succeeded. Waiting for the owner's decision.";
      return got + "Judged at " + e.needs + "; stops itself at a $" + e.loss_limit + " loss.";
    }
    if (e.book) {
      if (!e.trades) return "Tested on hourly history: the rule never traded.";
      const iv = e.interval ? " (99.5% range " + (e.interval[0] * 100).toFixed(2) + " to " + (e.interval[1] * 100).toFixed(2) + "¢)" : "";
      return e.trades.toLocaleString() + " trades over " + e.days + " days: " + (e.avg_net * 100).toFixed(2) + "¢ a share after costs" + iv
        + ", " + (e.avg_gross * 100).toFixed(2) + "¢ before costs.";
    }
    if (e.scored != null) {
      const net = e.avg_net == null ? "no result yet" : "net " + (e.avg_net * 100).toFixed(2) + "¢ after costs";
      return e.scored.toLocaleString() + " signals scored over " + e.days + " days, " + net + ".";
    }
    if (e.markets_24h != null) return e.bets_24h + " bet(s) in " + e.markets_24h.toLocaleString() + " resolved markets, out of sample.";
    if (e.observations != null) return e.observations.toLocaleString() + " recorded order books checked, " + e.episodes_1c + " gap(s) of 1¢ or more.";
    return "No history test is possible. Its paper record will be the evidence.";
  }

  function clock() {
    const now = new Date();
    $("date").textContent = now.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    $("time").textContent = now.toLocaleTimeString([], { hour12: false });
  }

  function rows(list, cells) {
    return list.map((item) => { const tr = el("tr"); cells(item).forEach((c) => tr.appendChild(c)); return tr; });
  }
  const td = (text, cls) => el("td", cls, text);
  const chip = (text, cls) => { const c = el("td"); c.appendChild(el("span", "chip " + cls, text)); return c; };

  function system(d) {
    const s = d.system || {}, node = $("system");
    const ok = !failed(s) && s.status === "HEALTHY";
    node.className = "state " + (ok ? "ok" : "bad");
    node.lastElementChild.textContent = ok ? "System online" : failed(s) ? "Status unavailable" : "System " + String(s.status || "unknown").toLowerCase();
  }

  function briefing(d) {
    const p = d.portfolio, sc = d.scanner, g = d.gate, r = d.risk, facts = clear($("facts"));
    const hour = new Date().getHours();
    const hello = hour < 12 ? "Good morning." : hour < 18 ? "Good afternoon." : "Good evening.";
    $("greet").textContent = failed(sc) ? hello + " The market scanner is not answering."
      : hello + " FRIDAY is watching " + sc.markets + " " + sc.venue + " markets.";
    const add = (text) => facts.appendChild(el("li", null, text));
    if (!failed(p)) add("Paper account " + money(p.equity) + ", " + p.open_positions + " open position" + (p.open_positions === 1 ? "" : "s") + ".");
    if (!failed(g)) add(g.approved_for_paper.length ? "Approved for paper trading: " + g.approved_for_paper.map((s) => NAMES[s] || s).join(", ") + "."
      : "No strategy has passed testing yet.");
    if (!failed(r) && r.kill_switch && r.kill_switch.engaged) add("Kill switch engaged: no new positions.");
    if (!failed(r) && r.drawdown_halt && r.drawdown_halt.engaged) add("Drawdown halt: waiting for the owner to resume.");
    add("Live trading is locked.");
  }

  function ledger(d) {
    const p = d.portfolio, dl = clear($("ledger"));
    const put = (k, v, cls) => { dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", cls, v)); };
    if (failed(p)) { put("Paper ledger", "unavailable", "warn"); return; }
    put("Account balance", money(p.equity));
    put("Total profit and loss", signed(p.total_pnl) + " (" + pct(p.total_return, 2) + ")", tone(p.total_pnl));
    put("Open positions", String(p.open_positions));
    put("Available funds", money(p.cash));
    put("At risk", money(p.at_risk));
    put("Largest drawdown", pct(p.max_drawdown, 2));
    put("Today", signed(p.daily_pnl), tone(p.daily_pnl));
  }

  function risk(d) {
    const r = d.risk, dl = clear($("risk"));
    const put = (k, v, cls) => { dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", cls, v)); };
    if (failed(r)) { put("Risk governor", "unavailable", "warn"); return; }
    const s = r.settings;
    put("Kill switch", r.kill_switch.engaged ? "Engaged" : "Off", r.kill_switch.engaged ? "neg" : "");
    put("Drawdown halt", r.drawdown_halt.engaged ? "Halted" : "Clear", r.drawdown_halt.engaged ? "neg" : "");
    put("Largest position", pct(s.max_position_fraction, 0) + " of equity");
    put("Total at risk", "up to " + pct(s.max_total_exposure, 0));
    put("Daily loss limit", pct(s.daily_loss_limit, 0));
    put("Drawdown limit", pct(s.max_drawdown, 0));
    const c = r.decisions || {};
    put("Checks, 30 days", (c.APPROVED || 0) + " passed, " + (c.REDUCED || 0) + " cut, " + (c.REJECTED || 0) + " refused");
  }

  const SOURCE = { FOUND_IN_SOURCE: " Found in FRIDAY's copy.", PARTLY_FOUND: " Partly found in FRIDAY's copies.", NOT_FOUND_IN_SOURCE: " Not found in FRIDAY's copy.", NOT_VERIFIED: " Book not in the library; not verified." };

  function board(d) {
    const ring = clear($("ring")), tiles = clear($("strategies")), g = d.gate, books = d.book_strategies || { names: [] };
    const tested = failed(g) ? [] : g.strategies.map((s) => ({ key: s.strategy, name: (s.evidence && s.evidence.name) || NAMES[s.strategy] || s.strategy, cls: STATUS[s.status][0], label: STATUS[s.status][1], why: evidence(s) }));
    const inGate = new Set(tested.map((t) => t.key));
    const waiting = books.items ? books.items.filter((b) => !inGate.has(b.key)).map((b) => ({ name: b.name, cls: "pending", label: b.kind === "OVERLAY" ? "Applies to every strategy" : "Registered, not yet tested", why: b.source + (SOURCE[b.source_status] || "") }))
      : (books.names || []).map((n) => ({ name: n, cls: "pending", label: "Not yet registered", why: "" }));
    const all = tested.concat(waiting);
    const approved = tested.filter((s) => s.cls === "approved").length;
    $("board-sub").textContent = failed(g) ? "the validation gate is not answering" : tested.length + " tested, " + approved + " approved" + (waiting.length ? ", " + waiting.length + " not tested" : "");
    $("lib-sub").textContent = tested.length + " tested" + (waiting.length ? ", " + waiting.length + " not tested" : "");

    ring.appendChild(svg("circle", { class: "orbit", r: 150 }));
    ring.appendChild(svg("circle", { class: "orbit dash", r: 118 }));
    const half = Math.ceil(all.length / 2);
    all.forEach((s, i) => {
      // two fans, left and right of the core, so labels never collide
      const right = i < half, k = right ? i : i - half, n = right ? half : all.length - half;
      const t = n === 1 ? 0 : (k / (n - 1)) * 2 - 1;                       // -1 .. 1, top to bottom
      const a = t * 1.12, x = (right ? 1 : -1) * Math.cos(a) * 150, y = Math.sin(a) * 150;
      ring.appendChild(svg("line", { class: "spoke", x1: (right ? 1 : -1) * 46, y1: y * 0.2, x2: x, y2: y }));
      ring.appendChild(svg("circle", { class: "node " + s.cls, cx: x, cy: y, r: 5.5 }));
      const tx = x + (right ? 11 : -11);
      ring.appendChild(svg("text", { class: "name", x: tx, y: y + 4, "text-anchor": right ? "start" : "end" }, s.name));
    });
    ring.appendChild(svg("circle", { class: "core", r: 46 }));
    ring.appendChild(svg("text", { class: "core-n", y: 6 }, String(approved)));
    ring.appendChild(svg("text", { class: "core-t", y: 24 }, "approved"));

    all.forEach((s) => {
      const t = el("div", "tile " + s.cls);
      t.appendChild(el("b", null, s.name)); t.appendChild(el("span", null, s.label)); if (s.why) t.appendChild(el("p", null, s.why));
      tiles.appendChild(t);
    });
  }

  function library(d) {
    const lib = d.library, box = clear($("books"));
    if (failed(lib)) { $("books-sub").textContent = ""; box.appendChild(el("p", "empty", "The research library is not available on this server.")); return; }
    $("books-sub").textContent = lib.documents + " document" + (lib.documents === 1 ? "" : "s") + " in the library";
    if (!lib.books.length) { box.appendChild(el("p", "empty", "No books yet. Add one and FRIDAY will read it.")); return; }
    lib.books.forEach((b) => {
      const card = el("div", "book"), done = b.sections ? Math.min(1, b.read / b.sections) : 0;
      card.appendChild(el("b", null, b.title || "Untitled"));
      card.appendChild(el("small", null, (b.author || "Unknown author") + ", " + Math.round(done * 100) + "% read"));
      const m = el("div", "meter"), bar = el("i"); bar.style.width = (done * 100) + "%"; m.appendChild(bar); card.appendChild(m);
      box.appendChild(card);
    });
  }

  function scanner(d) {
    const sc = d.scanner, body = clear($("scanner").tBodies[0]);
    if (failed(sc)) { $("scan-sub").textContent = "not answering"; return; }
    $("scan-sub").textContent = sc.venue + ", " + sc.markets + " markets" + (sc.observed_at ? ", read at " + hhmm(sc.observed_at) : "");
    rows(sc.rows, (r) => [
      td(r.question, "q"), td(r.price == null ? "—" : r.price.toFixed(2), "n"),
      td(r.change_24h == null ? "—" : (r.change_24h > 0 ? "+" : "") + (r.change_24h * 100).toFixed(1) + "¢", "n " + tone(r.change_24h)),
      td(compact(r.volume_24h), "n"),
      chip(r.signals && r.signals.length ? "Signal" : r.decision === "WATCH" ? "Watching" : "Ignored", r.decision === "WATCH" ? "watch" : "ignore"),
    ]).forEach((tr) => body.appendChild(tr));
  }

  function trades(d) {
    const p = d.portfolio, box = clear($("trades"));
    if (failed(p)) { $("trades-sub").textContent = ""; box.appendChild(el("p", "empty", "The paper ledger is not available.")); return; }
    $("trades-sub").textContent = p.positions.length + " open, " + p.closed_trades + " closed";
    if (!p.positions.length) { box.appendChild(el("p", "empty", "No open trades. Paper trading starts in Phase 12, and only for strategies the validation gate allows.")); return; }
    const table = el("table"), body = el("tbody");
    rows(p.positions, (x) => [td(x.label || x.market_id, "q"), td(NAMES[x.strategy] || x.strategy), td(money(x.stake), "n"),
      td(signed(x.value - x.stake), "n " + tone(x.value - x.stake))]).forEach((tr) => body.appendChild(tr));
    table.appendChild(body); box.appendChild(table);
  }

  function activity(d) {
    const a = d.activity, body = clear($("activity").tBodies[0]);
    if (failed(a) || !a.length) { const tr = el("tr"); const c = td("Nothing recorded yet.", "q"); c.colSpan = 4; tr.appendChild(c); body.appendChild(tr); return; }
    const cls = { APPROVED: "ok", COMPLETED: "ok", REDUCED: "cut", REJECTED: "no", FAILED: "no" };
    rows(a, (x) => [td(hhmm(x.at)), td(x.type), td(x.what, "q"), chip(String(x.result).charAt(0) + String(x.result).slice(1).toLowerCase(), cls[x.result] || "watch")])
      .forEach((tr) => body.appendChild(tr));
  }

  function pnl(d) {
    const p = d.portfolio, chart = clear($("pnl")), stats = clear($("pnl-stats"));
    const put = (k, v, cls) => { const w = el("div"); w.appendChild(el("dt", null, k)); w.appendChild(el("dd", cls, v)); stats.appendChild(w); };
    if (failed(p)) { put("Paper ledger", "unavailable"); return; }
    const days = p.equity_days.length ? p.equity_days : [{ day: "", end_equity: p.equity }];
    const ys = [p.bankroll].concat(days.map((x) => x.end_equity));
    const lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys), span = (hi - lo) || p.bankroll * 0.02;
    const X = (i) => ys.length === 1 ? 200 : 6 + (i / (ys.length - 1)) * 388, Y = (v) => 96 - ((v - lo) / span) * 82;
    const line = ys.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
    chart.appendChild(svg("line", { class: "base", x1: 0, x2: 400, y1: Y(p.bankroll), y2: Y(p.bankroll) }));
    chart.appendChild(svg("path", { class: "area", d: line + " L" + X(ys.length - 1).toFixed(1) + " 110 L" + X(0).toFixed(1) + " 110 Z" }));
    chart.appendChild(svg("path", { class: "curve", d: line }));
    chart.appendChild(svg("text", { x: 4, y: Y(p.bankroll) - 5 }, "start " + money(p.bankroll)));
    put("Total", signed(p.total_pnl), tone(p.total_pnl));
    put("Best trade", p.best_trade == null ? "—" : signed(p.best_trade), tone(p.best_trade));
    put("Win rate", p.win_rate == null ? "—" : pct(p.win_rate, 0));
  }

  async function refresh() {
    try {
      const res = await fetch("/api/dashboard", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      [system, briefing, ledger, risk, board, library, scanner, trades, activity, pnl].forEach((draw) => {
        try { draw(d); } catch (err) { console.error(draw.name, err); }
      });
      $("foot").textContent = "Read-only view of FRIDAY's recorded state, refreshed at " + hhmm(d.at) + ". Live trading is locked.";
    } catch (err) {
      const node = $("system"); node.className = "state bad"; node.lastElementChild.textContent = "FRIDAY is not answering";
      $("foot").textContent = "Could not reach FRIDAY (" + err.message + "). Trying again in 30 seconds.";
    }
  }

  clock(); setInterval(clock, 1000);
  refresh(); setInterval(refresh, 30000);
})();
