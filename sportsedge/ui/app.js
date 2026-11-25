"use strict";
/* sportsedge UI - plain JS, no build step. Talks to the local API in sportsedge/web.py. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const GAME_MARKETS = ["Moneyline", "Spread", "Total"];
const SERIES_COLOR = { "Value bets": "var(--s1)", "Game lines": "var(--s2)", Props: "var(--s3)" }; // colour follows the entity
const BREAK_EVEN = 110 / 210; // win rate a -110 price needs

const S = {
  page: "slate",
  sport: localStorage.getItem("sport") || "all",
  status: null, slate: null, legs: {},
  slip: JSON.parse(localStorage.getItem("slip") || "[]"),
  bankroll: parseFloat(localStorage.getItem("bankroll") || "0") || 0,
  slipEval: null, slipOdds: null, slipStake: null,
  track: null, trackDays: 30, trackLoading: false,
  parlayTab: "day", parlayGame: null, likelyTab: "all",
  introSeen: localStorage.getItem("intro_seen") === "1",
  form: { sport: "", player: "", market: "", line: "", over: "", under: "", paste: "", from: localStorage.getItem("line_from") || "same" },
  fixOpen: null, // game whose "correct the line" form is open
  gp: {}, gpAll: {}, whyOpen: {},            // per game: chosen stat, "show all", model details open
  pv: localStorage.getItem("props_view") || "game", pg: localStorage.getItem("props_group") || "stat", pgOpen: null, // props page: view, grouping, open games
  focusGame: null, focusForm: false,
  odds: null, oddsAt: 0, movesGame: null, plays: null, gparOpen: {},
  started: [], st: {}, slipLines: {}, boxOpen: {},
  board: null, hedgeOdds: {},   // live prices for games in play; the odds typed into each hedge box
  importOpen: false, importText: "", importNote: null, importSingles: null, slipPayout: "",   // paste a slip from the sportsbook   // games that have started; per game: chosen stat, "show all"; lines typed in the slip
  live: null, liveErr: null, liveFirst: {}, offers: JSON.parse(localStorage.getItem("offers") || "{}"), cash: {},
  priceErrors: [],
  pb: { tab: "", game: "", stat: "", q: "", limit: 40 }, // prop board
  proj: { team: "", q: "", sort: null, dir: -1, open: false },
  all: { market: "", q: "", sort: "ev", dir: -1 },
};

// the address bar carries the page and, for player props, the sport (#props/nfl), so views can be bookmarked
function readHash() {
  const [page, arg] = location.hash.slice(1).split("/");
  S.page = ["slate", "props", "live", "moves", "track", "model"].includes(page) ? page : "slate";
  if (S.page === "props" && arg) S.form.sport = arg.toLowerCase();
}
readHash();

// ============================================================================ utilities
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.href = "/login"; throw new Error("Sign in first"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}
// How prices are written. A sportsbook quotes American odds (-110, +150). A trading app sells a contract that pays
// $1 for a price in cents (52¢), and charges a fee on top. Everything inside the tool stays in American odds:
// prices are turned into cents only where they are shown, and back where they are typed.
const PX = { mode: localStorage.getItem("px_mode") === "cents" ? "cents" : "odds",
  flat: parseFloat(localStorage.getItem("px_flat") || "0") || 0,     // fee per contract, in cents
  rate: parseFloat(localStorage.getItem("px_rate") || "0") || 0 };   // plus this % of price x (1 - price)
const inCents = () => PX.mode === "cents";
const centsOf = (a) => 100 / (a > 0 ? 1 + a / 100 : 1 + 100 / Math.abs(a));          // American odds -> cents
const americanOf = (c) => (c >= 50 ? (-100 * c) / (100 - c) : (100 * (100 - c)) / c);  // cents -> American odds
const feeOf = (c) => (inCents() ? PX.flat + (PX.rate / 100) * c * (100 - c) / 100 : 0);  // the fee on one contract bought at c cents
const fmtCents = (c) => (c == null || isNaN(c) ? "–" : `${c >= 10 && c <= 90 ? Math.round(c) : (Math.round(c * 10) / 10)}¢`);
const fmtOdds = (a) => (a == null || isNaN(a) ? "–" : inCents() ? fmtCents(centsOf(a)) : a > 0 ? `+${Math.round(a)}` : `${Math.round(a)}`);
/* What is in a price box as American odds, without adding a fee: for carrying a typed price across a switch. */
const typedOddsRaw = (v) => { const x = parseFloat(v); return isNaN(x) ? NaN : inCents() ? (x > 0 && x < 100 ? americanOf(x) : NaN) : x; };
/* A price as it goes in a box: American odds, or cents to one decimal. */
const boxPrice = (a) => (a == null || a === "" || isNaN(a) ? "" : inCents() ? String(Math.round(centsOf(a) * 10) / 10) : String(Math.round(a)));
/* What was typed where a price goes -> American odds. In cents the fee is added first, so what comes out is
   what the contract really costs. NaN when it is not a usable price. */
function typedOdds(v) {
  const x = parseFloat(v);
  if (isNaN(x)) return NaN;
  if (!inCents()) return x > -100 && x < 100 ? NaN : x;
  if (x <= 0 || x >= 100) return NaN;
  return americanOf(Math.min(x + feeOf(x), 99.9));
}
/* The most a contract is worth paying for a pick with this chance of winning, once the fee is counted. */
function worthUpTo(p) {
  let c = 100 * p;
  for (let i = 0; i < 6; i++) c = 100 * p - feeOf(c);
  return Math.max(c, 0);
}
const priceWord = () => (inCents() ? "Price" : "Odds");
const feeText = () => (PX.flat || PX.rate ? `${PX.flat ? `${PX.flat}¢` : ""}${PX.flat && PX.rate ? " + " : ""}${PX.rate ? `${PX.rate}% of price × (1 − price)` : ""} a contract` : "no fee set");
function syncPx() { for (const b of $$(".pxseg button")) b.classList.toggle("on", b.dataset.mode === PX.mode); }
const pct = (p, d = 1) => (p == null || isNaN(p) ? "–" : `${(p * 100).toFixed(d)}%`);
// signed numbers: a value that rounds to zero is shown unsigned ("0.0", not "+0.0" / "−0.0")
function signed(x, d = 1) {
  if (x == null || isNaN(x)) return "–";
  const r = Number(Number(x).toFixed(d));
  return r === 0 ? (0).toFixed(d) : `${r > 0 ? "+" : "−"}${Math.abs(r).toFixed(d)}`;
}
const spct = (p, d = 1) => (p == null || isNaN(p) ? "–" : `${signed(p * 100, d)}%`);
const money = (x, d = 0) => (x == null || isNaN(x) ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`);
const cls = (x) => (x > 1e-9 ? "pos" : x < -1e-9 ? "neg" : "");
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "");
const day = (iso) => (iso ? new Date(`${iso}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" }) : "");
const decOf = (a) => (a > 0 ? 1 + a / 100 : 1 + 100 / Math.abs(a));
const favLine = (home, away, m) => (m == null ? "–" : Math.abs(m) < 0.05 ? "Pick'em" : m > 0 ? `${home} −${m.toFixed(1)}` : `${away} −${(-m).toFixed(1)}`);
const num = (x, d = 1) => (x == null || isNaN(x) ? "–" : Number(x).toFixed(d));
const lineNum = (x) => (x == null || isNaN(x) ? "–" : Number(x) % 1 ? Number(x).toFixed(1) : String(Number(x)));
const isProp = (l) => !GAME_MARKETS.includes(l.market);

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function settings() {
  return (S.slate && S.slate.settings) || { min_edge: 0.02, longshot_odds: 250, longshot_min_edge: 0.05, kelly_fraction: 0.25, kelly_cap: 0.03, prop_gap_flag: 0.3, prop_lean: 0.15, prop_weight: {}, prop_weight_source: {} };
}
// mirrors report.value_bets: positive expected return, a real edge, and not flagged as "check news"
function isValue(l) {
  const st = settings();
  const need = l.market === "Moneyline" && l.odds >= st.longshot_odds ? st.longshot_min_edge : st.min_edge;
  return l.ev > 0 && !l.flag && (l.edge == null || l.edge >= need);
}
const RATINGS = {
  value: ["🔥 Value", "Pays more than the risk: positive expected return and a real edge over the book's price."],
  fair: ["Fair", "Close to a fair price. Roughly break-even over many bets."],
  poor: ["Poor price", "The sportsbook's fee outweighs any edge. Loses on average, even if it usually wins."],
  news: ["👀 Check news", "The model is far from the sportsbook here. That usually means news the model has not seen (injury, role change)."],
};
function ratingOf(l) {
  if (l.flag) return "news";
  if (isValue(l)) return "value";
  return l.ev >= -0.025 ? "fair" : "poor";
}
const chip = (kind) => `<span class="rate r-${kind}" title="${esc(RATINGS[kind][1])}">${RATINGS[kind][0]}</span>`;
const sportsShown = () => (S.slate ? S.slate.sports.filter((s) => S.sport === "all" || s.sport === S.sport) : []);
const inSlip = (k) => S.slip.includes(k);
const guideLink = (text = "What do these numbers mean?") => `<button class="linkish" data-act="guide">${text}</button>`;
const pageHead = (title, desc) => `<div class="page-hd"><h1>${title}</h1><p>${desc}</p></div>`;

// ============================================================================ teams, rings
const SPORT_ICON = { nfl: "🏈", nba: "🏀" };
const TEAM_COLORS = {
  nfl: { ARI: "#97233F", ATL: "#A71930", BAL: "#241773", BUF: "#00338D", CAR: "#0085CA", CHI: "#0B162A", CIN: "#FB4F14", CLE: "#311D00",
    DAL: "#003594", DEN: "#FB4F14", DET: "#0076B6", GB: "#203731", HOU: "#03202F", IND: "#002C5F", JAX: "#006778", KC: "#E31837",
    LV: "#000000", LAC: "#0080C6", LA: "#003594", MIA: "#008E97", MIN: "#4F2683", NE: "#002244", NO: "#D3BC8D", NYG: "#0B2265",
    NYJ: "#125740", PHI: "#004C54", PIT: "#FFB612", SF: "#AA0000", SEA: "#002244", TB: "#D50A0A", TEN: "#4B92DB", WAS: "#5A1414" },
  nba: { ATL: "#E03A3E", BOS: "#007A33", BKN: "#000000", CHA: "#1D1160", CHI: "#CE1141", CLE: "#860038", DAL: "#00538C", DEN: "#0E2240",
    DET: "#C8102E", GSW: "#1D428A", HOU: "#CE1141", IND: "#002D62", LAC: "#C8102E", LAL: "#552583", MEM: "#5D76A9", MIA: "#98002E",
    MIL: "#00471B", MIN: "#0C2340", NOP: "#0C2340", NYK: "#006BB6", OKC: "#007AC1", ORL: "#0077C0", PHI: "#006BB6", PHX: "#1D1160",
    POR: "#E03A3E", SAC: "#5A2D81", SAS: "#C4CED4", TOR: "#CE1141", UTA: "#002B5C", WAS: "#002B5C" },
};
/* A team's badge: its abbreviation on its main colour, with the lettering dark or light to stay readable. */
function teamBadge(sport, abbr, small = false) {
  const c = (TEAM_COLORS[String(sport).toLowerCase()] || {})[abbr] || "#6a4cff";
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  const light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.42;
  return `<span class="tb ${small ? "sm" : ""}" style="background:${c};color:${light ? "#15131f" : "#fff"}" aria-hidden="true">${esc(abbr)}</span>`;
}
/* Win chance as a ring: the filled share of the circle is the chance. */
function ring(p, kind = "") {
  const c = 2 * Math.PI * 26, v = Math.max(0, Math.min(p || 0, 1));
  return `<div class="ring ${kind ? `k-${kind}` : ""}" role="img" aria-label="Win chance ${pct(p, 0)}"><svg viewBox="0 0 64 64"><circle class="bg" cx="32" cy="32" r="26"></circle><circle class="fg" cx="32" cy="32" r="26" stroke-dasharray="${(v * c).toFixed(1)} ${c.toFixed(1)}"></circle></svg>
    <div class="rv"><span>${pct(p, 0)}<small>to win</small></span></div></div>`;
}

// ============================================================================ data loading
async function poll() {
  try {
    const st = await api("/api/status");
    const prev = S.status;
    S.status = st;
    renderStatus();
    if (st.status === "running") {
      if (!S.slate || !S.slate.sports.length) renderMain();
      setTimeout(poll, 1500);
      return;
    }
    if (st.status === "idle" && (!S.slate || !S.slate.sports.length)) {
      await api("/api/run", { method: "POST", body: "{}" });
      setTimeout(poll, 800);
      return;
    }
    if (!S.slate || S.slate.run_at !== st.run_at || (prev && prev.status === "running")) await loadSlate();
    else if (st.status === "error") renderMain();
  } catch (e) {
    S.status = { status: "offline", log: [String(e.message || e)] };
    renderStatus();
    setTimeout(poll, 4000);
  }
}

async function loadSlate() {
  S.slate = await api("/api/slate");
  S.legs = {};
  S.started = S.slate.started || [];
  for (const g of S.started) for (const l of g.legs) S.legs[l.key] = l;  // a game that has started: its last numbers
  const P = S.slate.plays || { games: [], groups: [], days: [], lean_legs: [] };
  for (const l of P.lean_legs || []) S.legs[l.key] = l;  // props without a price, at an assumed one
  for (const s of S.slate.sports) for (const l of s.legs) S.legs[l.key] = l;  // a priced pick wins a shared key
  S.plays = { ...P, byGame: Object.fromEntries((P.games || []).map((g) => [`${g.sport}|${g.game_id}`, g])) };
  const before = S.slip.length;
  S.slip = S.slip.filter((k) => S.legs[k]);
  if (S.slip.length < before) toast("Some picks in your slip have started or are no longer offered and were removed.");
  saveSlip();
  if (!S.form.sport && S.slate.sports.length) S.form.sport = (S.slate.sports.find((s) => s.model) || S.slate.sports[0]).sport;
  S.track = null;
  S.odds = null;
  await evalSlip();
  renderMain();
  renderSlip();
}

async function loadTrack() {
  if (S.trackLoading) return;
  S.trackLoading = true;
  try {
    const q = new URLSearchParams();
    if (S.trackDays) q.set("days", S.trackDays);
    if (S.sport !== "all") q.set("sport", S.sport);
    S.track = await api(`/api/track?${q}`);
  } catch (e) {
    S.track = { error: String(e.message || e) };
  }
  S.trackLoading = false;
  if (S.page === "track") renderMain();
}

function saveSlip() { localStorage.setItem("slip", JSON.stringify(S.slip)); }

/* The lines typed into the slip that differ from the tool's own: pick key -> line. */
function slipLinesNow() {
  const out = {};
  for (const k of S.slip) { const v = parseFloat(S.slipLines[k]), l = S.legs[k]; if (l && !isNaN(v) && l.line != null && Math.abs(v - l.line) > 1e-9) out[k] = v; }
  return out;
}
async function evalSlip() {
  if (!S.slip.length) { S.slipEval = null; return; }
  try {
    S.slipEval = await api("/api/slip", { method: "POST", body: JSON.stringify({ keys: S.slip, lines: slipLinesNow() }) });
  } catch (_) { S.slipEval = null; }
  const start = S.keepOdds != null ? S.keepOdds : S.slipEval && S.slipEval.odds != null ? S.slipEval.odds : null;
  S.slipOdds = start == null ? null : boxPrice(start);
  S.slipOddsTyped = S.keepOdds != null;  // a price that came from the user's own slip counts as theirs
  S.keepOdds = null;
}

/* The box for pasting a bet slip, and what was found in the last one pasted. */
function importBox() {
  if (!S.importOpen) return "";
  return `<div class="imp"><label for="impText">Paste your bet slip</label>
    <textarea id="impText" placeholder="Copy the bet from your sportsbook (the slip, or My Bets) and paste the text here.">${esc(S.importText)}</textarea>
    <div class="row"><button class="btn primary small" data-act="import-read">Read it</button><button class="btn small ghost" data-act="import-toggle">Cancel</button></div>
    <div class="hint">Text only. From a screenshot, let your phone pull the text out first: open the picture, choose <i>Copy text</i> (Google Lens on Android, Live Text on iPhone), then paste here.
      Nothing is recorded until you check the picks and confirm.</div></div>`;
}
/* A pasted trade slip with a payout under every pick: those are separate single bets. */
function importSingles() {
  const list = S.importSingles;
  if (!list || list.length < 2) return "";
  const ready = list.every((x) => x.odds != null && x.stake > 0);
  const name = (x) => { const l = S.legs[x.key]; return x.line == null || !l ? esc(x.selection) : `${esc(x.selection)} <span class="muted">→ at your line ${["spread"].includes(l.market_key) ? signed(x.line, Math.abs(x.line) % 1 ? 1 : 0) : lineNum(x.line)}</span>`; };
  return `<div class="imp-note singles"><b>These look like ${list.length} separate single bets,</b> each with its own stake and payout.
    <ul>${list.map((x) => `<li>${name(x)}<br><span class="muted">${x.stake != null ? `${money(x.stake, 2)} to pay ${money(x.payout, 2)}` : "no stake found"}${x.odds != null && x.stake ? ` · ${fmtCents((100 * x.stake) / x.payout)} a contract with the fee in` : ""}${x.price != null ? ` · shown as ${lineNum(x.price)}%` : ""}</span></li>`).join("")}</ul>
    <div class="row">${ready ? `<button class="btn primary small" data-act="import-singles-record">Record ${list.length} separate bets</button>` : ""}
      <button class="btn small ghost" data-act="import-singles-combo">They are one combo</button></div>
    ${ready ? "" : `<div style="margin-top:4px">A stake or payout is missing for one of them: record them one at a time from the slip below.</div>`}</div>`;
}
function importNote() {
  const n = S.importNote;
  if (!n) return "";
  if (S.importSingles && S.importSingles.length > 1) return importSingles();
  const miss = (n.unmatched || []).map((u) => `<li>“${esc(u.text)}” <span class="muted">${esc(u.why)}</span></li>`).join("");
  const head = n.n ? `<b>Read ${n.n} pick${n.n === 1 ? "" : "s"} from your slip${n.odds != null ? `, odds ${fmtOdds(n.odds)}` : ""}${n.stake != null ? `, stake ${money(n.stake, 2)}` : ""}.</b> Check each one, and its line, before recording.`
    : `<b>No picks could be read from that text.</b> It needs the player or team, over or under, and the number, for a game the tool has.`;
  return `<div class="imp-note ${n.n && !miss ? "" : "warn"}">${head}${miss ? `<div style="margin-top:4px">Not matched:</div><ul>${miss}</ul>` : ""}
    ${n.odds == null && n.n ? `<div style="margin-top:4px">No odds found in the text: type them below.</div>` : ""}</div>`;
}

// ============================================================================ header / status
function renderStatus() {
  const el = $("#status");
  const st = S.status || {};
  el.className = "status " + (st.status || "");
  let text = "";
  if (st.status === "running") {
    const last = (st.log || []).slice(-1)[0] || "Starting";
    text = `Updating · ${esc(last.replace(/^\d\d:\d\d:\d\d\s+/, "")).slice(0, 48)}${st.elapsed ? ` · ${Math.round(st.elapsed)}s` : ""}`;
  } else if (st.status === "ready") {
    text = `Odds updated ${st.run_at ? new Date(st.run_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : ""}`;
  } else if (st.status === "error") {
    text = "Last update failed";
  } else if (st.status === "offline") {
    text = "Server not reachable";
  } else {
    text = "Starting";
  }
  el.innerHTML = `<span class="dot" aria-hidden="true"></span><span>${text}</span>`;
  $("#refreshBtn").disabled = st.status === "running";
  // away from the computer: a way to sign out. On the computer: where it can be reached from, if it is shared
  let extra = $("#remoteBox");
  if (!extra) { extra = document.createElement("span"); extra.id = "remoteBox"; el.after(extra); }
  extra.innerHTML = st.remote ? `<a class="btn small ghost" href="/logout" title="Sign out on this device">Sign out</a>`
    : st.share_url ? `<a class="rate r-lean" href="${esc(st.share_url)}" target="_blank" rel="noopener" title="Reachable from anywhere at ${esc(st.share_url)}${st.has_passcode ? "" : " (locked: no passcode set yet)"}">${st.has_passcode ? "Shared" : "Shared · locked"}</a>` : "";
  if (S.slate && S.slate.sports.length) $("#main").classList.toggle("dim", false);
}

// ============================================================================ main render
function renderMain() {
  $$("nav.tabs button").forEach((b) => {
    const on = b.dataset.page === S.page;
    b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  const main = $("#main");
  liveLoop();
  oddsLoop();
  if (S.page === "live") { main.innerHTML = pageLive(); return; }
  if (!S.slate || !S.slate.sports.length) {
    main.innerHTML = progressView();
    return;
  }
  const page = { slate: pageSlate, props: pageProps, live: pageLive, moves: pageMoves, track: pageTrack, model: pageModel }[S.page] || pageSlate;
  main.innerHTML = page();
  afterRender();
}

function progressView() {
  const st = S.status || {};
  if (!st.status || st.status === "offline" || (st.status === "ready" && !S.slate)) {
    return `<div class="progress"><h1><span class="spinner"></span>${st.status === "offline" ? "Waiting for the sportsedge server" : "Loading"}</h1>
      ${st.status === "offline" ? '<p class="note">Start it with <code>.\\sportsedge ui</code> in the project folder.</p>' : ""}</div>`;
  }
  if (st.status === "error") {
    return `<div class="progress"><h1>The update failed</h1><p class="note">This usually means a data source was briefly unreachable. Try again in a minute.</p>
      <pre>${esc(st.error || "")}</pre><button class="btn primary" data-act="refresh">Try again</button></div>`;
  }
  if (st.status === "ready" && S.slate && !S.slate.sports.length) {
    return `<div class="progress"><h1>No upcoming games found</h1><p class="note">Nothing is scheduled in the next few days, or the schedule could not be loaded.</p><button class="btn" data-act="refresh">Check again</button></div>`;
  }
  return `<div class="progress"><h1><span class="spinner"></span>Getting today's games ready</h1>
    <p class="note">Downloading schedules, odds, injuries and weather, then running the models and simulations.
    The very first run also downloads several seasons of history and tests the prop simulator on past lines, which can take a few minutes. After that it takes well under a minute.</p>
    <pre>${esc((st.log || []).join("\n") || "Starting ...")}</pre></div>`;
}

function sportSeg() {
  const opts = [["all", "All sports"], ...((S.slate && S.slate.sports) || []).map((s) => [s.sport, s.sport.toUpperCase()])];
  return `<div class="seg" role="group" aria-label="Sport">${opts
    .map(([v, l]) => `<button data-act="sport" data-sport="${v}" class="${S.sport === v ? "on" : ""}">${l}</button>`)
    .join("")}</div>`;
}
function jumpNav(items) {
  return `<nav class="jump" aria-label="Sections on this page"><span class="muted">Jump to</span>${items
    .map(([id, label, n]) => `<button data-act="jump" data-to="${id}">${label}${n == null ? "" : ` <span class="n">${n}</span>`}</button>`).join("")}</nav>`;
}

// ============================================================================ today page
function legsTable(legs, { empty = "", sport = false } = {}) {
  if (!legs.length) return `<div class="empty">${empty}</div>`;
  const rows = legs
    .map((l) => {
      const stake = S.bankroll ? money(S.bankroll * l.kelly) : pct(l.kelly);
      const on = inSlip(l.key);
      return `<tr>
        <td class="c-add"><button class="add ${on ? "on" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)} ${on ? "from" : "to"} the bet slip" title="Add to bet slip">${on ? "✓" : "+"}</button></td>
        <td><span class="pick">${esc(l.selection)}</span>
          <div class="sub2">${sport ? `<span class="badge">${esc(l.sport)}</span> ` : ""}${esc(l.market)} · ${esc(l.game)} · ${when(l.start)}</div></td>
        <td>${chip(ratingOf(l))}</td>
        <td class="num">${fmtOdds(l.odds)}</td>
        <td class="num"><div class="wc"><span>${pct(l.p_cond)}</span><span class="meter" aria-hidden="true"><i style="width:${Math.round((l.p_cond || 0) * 100)}%"></i></span></div></td>
        <td class="num">${pct(l.p_fair)}</td>
        <td class="num ${cls(l.edge)}">${l.edge == null ? "–" : `${signed(l.edge * 100)} pts`}</td>
        <td class="num ${cls(l.ev)}">${spct(l.ev)}</td>
        <td class="num">${l.ev > 0 ? stake : "–"}</td></tr>`;
    })
    .join("");
  return `<div class="tw"><table class="t"><thead><tr><th class="c-add"><span class="sr">Add to slip</span></th><th>Pick</th><th>Rating</th>
    <th class="num" title="American odds. −150: bet $150 to win $100. +130: bet $100 to win $130.">Odds</th>
    <th class="num" title="The tool's estimate that this pick wins (if it does not push)">Win chance</th>
    <th class="num" title="What the sportsbook's odds imply once its fee is removed">Book's chance</th>
    <th class="num" title="Win chance minus the book's chance, in percentage points">Edge</th>
    <th class="num" title="Average profit per $1 bet over many bets. +5% means 5 cents per dollar.">Expected return</th>
    <th class="num" title="Suggested stake (quarter-Kelly, never more than 3% of bankroll)">${S.bankroll ? "Stake" : "Stake (% of bankroll)"}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/* One bet as a card: what it is, its win chance as a ring, the price, and what it returns. */
function pickCard(l) {
  const k = ratingOf(l), on = inSlip(l.key);
  const stake = S.bankroll ? money(S.bankroll * l.kelly) : `${pct(l.kelly)} of bankroll`;
  return `<article class="pcard k-${k}">
    <div class="pc-top"><span class="pc-sport" aria-hidden="true">${SPORT_ICON[l.sport.toLowerCase()] || ""}</span>
      <div class="pc-title"><span class="pick">${esc(l.selection)}</span><span class="sub2">${esc(l.market)} · ${esc(l.game)} · ${when(l.start)}</span></div>
      <button class="add ${on ? "on" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)} ${on ? "from" : "to"} the bet slip" title="Add to bet slip">${on ? "✓" : "+"}</button></div>
    <div class="pc-body">${ring(l.p_cond, k)}
      <div class="pc-stats">
        <div title="${inCents() ? "The sportsbook's price for this pick, written as cents per contract. Your trading app sets its own price." : "American odds. −150: bet $150 to win $100. +130: bet $100 to win $130."}"><span>${inCents() ? "Sportsbook price" : "Odds"}</span><b>${fmtOdds(l.odds)}</b></div>
        <div title="What the sportsbook's odds imply once its fee is removed"><span>Book says</span><b>${pct(l.p_fair, 0)}</b></div>
        <div title="Average profit per $1 bet over many bets. +5% means 5 cents per dollar."><span>Return</span><b class="${cls(l.ev)}">${spct(l.ev)}</b></div>
      </div></div>
    <div class="pc-foot">${chip(k)}${inCents() ? `<span title="The most a contract is worth paying on your app, after the fee: the tool's win chance, less the fee">worth up to <b>${fmtCents(worthUpTo(l.p_cond))}</b></span>` : ""}${l.edge != null ? `<span title="Win chance minus the book's chance">edge ${signed(l.edge * 100)} pts</span>` : ""}${l.ev > 0 ? `<span title="Suggested stake: quarter-Kelly, never more than 3% of bankroll">stake ${stake}</span>` : ""}</div></article>`;
}
const pickCards = (legs, empty) => (legs.length ? `<div class="pcards">${legs.map(pickCard).join("")}</div>` : `<div class="empty">${empty}</div>`);

function parlayCards(rows) {
  if (!rows.length) return `<div class="empty" style="padding-left:0">None right now: a value parlay needs at least two value bets from different games.</div>`;
  const sgp = false;
  return `<div class="plcards">${rows.map((p, i) => `<article class="plcard">
      <div class="legchips">${p.legs.map((x) => `<span>${esc(x)}</span>`).join("")}</div>
      <div class="pl-nums">
        <div title="${sgp ? "Your sportsbook sets its own price for a same-game parlay" : "The standard parlay price for these picks"}"><span>${sgp ? "Price" : "Odds"}</span><b>${sgp ? "your book's" : fmtOdds(p.odds)}</b></div>
        <div title="The tool's chance that every pick wins"><span>Chance to hit</span><b>${pct(p.p_hit, p.p_hit < 0.1 ? 1 : 0)}</b></div>
        <div title="Break-even odds. Only bet if your sportsbook pays more than this."><span>Fair odds</span><b>${fmtOdds(p.fair_odds)}</b></div>
      </div>
      <div class="pc-foot"><span>${sgp ? `these win together <b>×${num(p.lift, 2)}</b> as often as unrelated picks` : `expected return <b class="${cls(p.ev)}">${spct(p.ev)}</b>`}</span>
        <button class="btn small" style="margin-left:auto" data-act="add-play" data-keys="${esc(JSON.stringify(p.leg_keys))}">Add to slip</button></div></article>`).join("")}</div>`;
}

// ============================================================================ games that have started
/* A game that has started, with every pick at the last number before the start. Nothing here is a suggestion:
   it is for recording a bet that was already placed at the sportsbook. */
const liveGame = (g) => ((S.live && S.live.scores) || []).find((x) => x.sport === g.sport && x.home === g.home && x.away === g.away);
function startedCard(g) {
  const id = `${g.sport}|${g.game_id}`, st = S.st[id] || {};
  const sc = liveGame(g), on = sc && sc.state !== "pre";
  const soFar = on ? Object.fromEntries(sc.players.map((p) => [p.key, p])) : null;
  const score = !on ? `<div class="st-score"><span class="sc-live"><span class="dot"></span>Started</span><span class="muted">score loading ...</span></div>`
    : `<div class="st-score">${sc.state === "in" ? `<span class="sc-live"><span class="dot"></span>Live</span>` : `<span class="rate r-fair">Final</span>`}
        <b>${esc(g.away)} ${Math.round(sc.away_score)} – ${Math.round(sc.home_score)} ${esc(g.home)}</b><span>${sc.state === "in" ? esc(sc.detail) : ""}</span>
        <span class="muted">total so far ${Math.round(sc.away_score + sc.home_score)}</span></div>`;
  const now = (r) => {
    if (!soFar) return "";
    const pl = soFar[r.pkey];
    if (!pl || pl[r.mkey] == null) return `<small>${g.sport === "nba" ? "not played" : "none yet"}</small>`;
    return `<small class="${pl[r.mkey] > r.line ? "hit" : ""}">now ${lineNum(pl[r.mkey])}</small>`;
  };
  const game = g.legs.filter((l) => !l.player_key), find = (mk, side) => game.find((l) => l.market_key === mk && l.side === side);
  const btn = (l, main) => {
    if (!l) return `<span class="mk-none">–</span>`;
    const on = inSlip(l.key);
    return `<button class="mk-btn ${on ? "on" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)}, ${fmtOdds(l.odds)}" title="${esc(l.selection)} at its last price before the game">
      <span class="mk-main">${main}</span><span class="mk-sub">${l.market_key === "ml" ? "to win" : fmtOdds(l.odds)}</span></button>`;
  };
  const sp = (l) => btn(l, l ? signed(l.line, Math.abs(l.line) % 1 ? 1 : 0) : "");
  const side = (l, label) => {
    if (!l) return `<span class="muted">–</span>`;
    const on = inSlip(l.key);
    return `<button class="side-btn ${on ? "on" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)}" title="${esc(l.selection)} at its last price before the game">
      <span class="s">${label}</span><span class="o">${fmtOdds(l.odds)}</span></button>`;
  };
  const rows = new Map();
  for (const l of g.legs) {
    if (!l.player_key) continue;
    const k = `${l.player_key}|${l.market_key}`;
    if (!rows.has(k)) rows.set(k, { player: l.subject, pkey: l.player_key, mkey: l.market_key, market: l.market, line: l.line });
    rows.get(k)[l.side === "under" ? "under" : "over"] = l;
  }
  const all = [...rows.values()], order = STAT_ORDER[g.sport] || [];
  const stats = [...order.filter((k) => all.some((r) => r.mkey === k)), ...[...new Set(all.map((r) => r.mkey))].filter((k) => !order.includes(k))];
  const stat = stats.includes(st.stat) ? st.stat : stats[0];
  const mine = all.filter((r) => r.mkey === stat).sort((a, b) => (b.line ?? 0) - (a.line ?? 0) || a.player.localeCompare(b.player));
  const shown = st.all ? mine : mine.slice(0, 6);
  const label = (k) => (all.find((r) => r.mkey === k) || {}).market || k;
  const props = !all.length ? "" : `<div class="gp-wrap"><div class="gp-hd"><span class="gp-title">Player props</span><span class="muted">${all.length} lines at their last price</span></div>
      <div class="gp-chips" role="group" aria-label="Stat">${stats.map((k) => `<button data-act="st-stat" data-id="${esc(id)}" data-stat="${esc(k)}" class="${k === stat ? "on" : ""}">${esc(label(k))}</button>`).join("")}</div>
      <div class="gp-cols"><span>${esc(label(stat))}</span><span>Line</span><span>Over</span><span>Under</span></div>
      ${shown.map((r) => `<div class="gp-row"><div class="gp-who"><span class="pick">${esc(r.player)}</span></div><div class="gp-line"><b>${lineNum(r.line)}</b>${now(r)}</div>
        <div class="gp-o">${side(r.over, "Over")}</div><div class="gp-u">${side(r.under, "Under")}</div></div>`).join("")}
      ${mine.length > 6 ? `<div class="gp-foot"><button class="linkish" data-act="st-more" data-id="${esc(id)}">${st.all ? "Show fewer" : `Show all ${mine.length} ${esc(label(stat))} lines`}</button><span></span></div>` : ""}</div>`;
  return `<article class="game started" id="st-${g.sport}-${esc(g.game_id)}">
    <div class="g-hd">${teamBadge(g.sport, g.away)}<span class="m">${esc(g.away)} <span class="at">@</span> ${esc(g.home)}</span>${teamBadge(g.sport, g.home)}
      <span class="time">${SPORT_ICON[g.sport] || ""} started ${when(g.start)}</span></div>
    ${score}
    <div class="mkt" role="table" aria-label="Main bets for ${esc(g.away)} at ${esc(g.home)}, at the last price before the game">
      <span></span><span class="mk-h">Moneyline</span><span class="mk-h">Over / Under</span><span class="mk-h">Spread</span>
      <span class="mk-team">${esc(g.away)}</span>${btn(find("ml", "away"), find("ml", "away") ? fmtOdds(find("ml", "away").odds) : "")}${btn(find("total", "over"), find("total", "over") ? `O ${lineNum(find("total", "over").line)}` : "")}${sp(find("spread", "away"))}
      <span class="mk-team">${esc(g.home)}</span>${btn(find("ml", "home"), find("ml", "home") ? fmtOdds(find("ml", "home").odds) : "")}${btn(find("total", "under"), find("total", "under") ? `U ${lineNum(find("total", "under").line)}` : "")}${sp(find("spread", "home"))}
    </div>${props}</article>`;
}
function startedSection() {
  const games = S.started.filter((g) => S.sport === "all" || g.sport === S.sport);
  if (!games.length) return "";
  return `<h2 class="sec-title" id="sec-started">🔴 Started games <span class="muted">${games.length}</span></h2>
  <p class="note sec-note">Live score, and each player's number so far next to his line. These games have started, so nothing here is a suggestion. The lines and prices are the last ones before each game began, kept so you can <b>record a bet you already placed</b>:
    tap the picks you bet, type the odds from your sportsbook's slip, and press <i>Record bet</i>. If your line was different, change it in the bet slip. The Live page then follows the bet.</p>
  <div class="games">${games.map(startedCard).join("")}</div>`;
}

// ============================================================================ suggested picks and parlays
const assumedOdds = () => fmtOdds((S.plays && S.plays.assumed_odds) || -115);
/* What a pick without a real price is: how strongly the simulator leans, never "value". */
function leanChip(l) {
  const strong = Math.abs(l.lean || 0) >= settings().prop_lean;
  return `<span class="rate r-lean" title="The simulator's projection is on this side of the line. The feed has no price for this prop, so the tool cannot say whether it pays: check the price on your sportsbook.">${strong ? "💪 Strong lean" : "Lean"}</span>`;
}
/* One suggested pick inside a game: what it is, its chance, its price, and a button to add it. */
function topPick(l) {
  const on = inSlip(l.key);
  const price = l.assumed ? `<span class="muted" title="The feed has no price for this prop. ${assumedOdds()} is a typical price, used until you type the real one from your slip.">no price in the feed</span>`
    : `<span>${fmtOdds(l.odds)}</span><span class="muted">return <span class="${cls(l.ev)}">${spct(l.ev)}</span></span>`;
  const worth = inCents() ? `<span class="muted" title="The most a contract is worth paying on your app, after the fee">worth up to <b>${fmtCents(worthUpTo(l.p_cond))}</b></span>` : "";
  return `<li class="tp-row"><button class="add ${on ? "on" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)} ${on ? "from" : "to"} the bet slip" title="Add to bet slip">${on ? "✓" : "+"}</button>
    <div class="tp-main"><div class="pick">${esc(l.selection)}</div><div class="tp-sub">${l.assumed ? leanChip(l) : chip(ratingOf(l))}${price}${worth}</div></div>
    <div class="tp-chance" title="The tool's chance this pick wins">${pct(l.p_cond, 0)}<small>to win</small></div></li>`;
}
/* A suggested parlay. The payout is exact only when every pick has a real price and no two share a game. */
function playCard(p) {
  const legs = p.leg_keys.map((k) => S.legs[k]).filter(Boolean);
  if (legs.length < p.leg_keys.length) return "";
  const all = legs.every((l) => inSlip(l.key));
  const why = [p.assumed ? `${p.assumed === p.n ? "these picks have" : `${p.assumed} of these picks ${p.assumed === 1 ? "has" : "have"}`} no price in the feed, so ${assumedOdds()} each is assumed` : "",
    p.same_game ? "picks from the same game get their own price from your sportsbook" : ""].filter(Boolean).join("; and ");
  return `<article class="plcard play t-${p.tier}">
      <div class="play-hd"><span class="tier">${esc(p.label)}</span><span class="muted">${p.n} picks${p.games.length > 1 ? ` · ${p.games.length} games` : ""}</span>
        <span class="pay" title="${p.est ? "An estimate: what these picks would pay if each were priced on its own" : "The standard parlay price for these picks"}">${p.est ? "about " : ""}${fmtOdds(p.odds)}</span></div>
      <ul class="play-legs">${legs.map((l) => `<li><span class="pick">${esc(l.selection)}</span><span class="muted">${p.games.length > 1 ? `${esc(l.game)} · ` : ""}${pct(l.p_cond, 0)}</span></li>`).join("")}</ul>
      <div class="pl-nums ${p.est ? "two" : ""}">
        <div title="The tool's chance that every pick wins. Picks from the same game keep the way they move together."><span>Chance to hit</span><b>${pct(p.p_hit, p.p_hit < 0.1 ? 1 : 0)}</b></div>
        <div title="${inCents() ? "The break-even price. Only buy for less than this." : "Break-even odds. Only bet if your sportsbook pays more than this."}"><span>Fair ${inCents() ? "price" : "odds"} (break-even)</span><b>${fmtOdds(p.fair_odds)}</b></div>
        ${p.est ? "" : `<div title="Average profit per $1 bet over many bets"><span>Expected return</span><b class="${cls(p.ev)}">${spct(p.ev)}</b></div>`}
      </div>
      ${p.est ? `<p class="play-note">The payout is an estimate: ${why}. ${inCents() ? `Price it on your app and buy it only if it costs less than <b>${fmtCents(worthUpTo(p.p_hit))}</b>, fee included.` : `Build it at your sportsbook and bet it only if the slip pays more than <b>${fmtOdds(p.fair_odds)}</b>.`}</p>` : ""}
      <button class="btn small ${all ? "" : "primary"}" data-act="add-play" data-keys="${esc(JSON.stringify(p.leg_keys))}" ${all ? "disabled" : ""}>${all ? "In your slip" : "Add all to slip"}</button></article>`;
}
const playCards = (list) => { const h = (list || []).map(playCard).join(""); return h ? `<div class="plcards">${h}</div>` : ""; };
const playsShown = (list) => (list || []).filter((p) => p.leg_keys.every((k) => S.legs[k] && (S.sport === "all" || S.legs[k].sport.toLowerCase() === S.sport)));
const dayName = (date) => { const d = new Date(`${date}T12:00:00`); return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }); };

/* A game's suggested picks, with its three parlays folded underneath. */
function gamePlays(s, g) {
  const gp = S.plays && S.plays.byGame[`${s.sport}|${g.game_id}`];
  const picks = gp ? gp.picks.map((k) => S.legs[k]).filter(Boolean) : [];
  if (!picks.length) return "";
  const pars = gp.parlays.filter((p) => p.leg_keys.every((k) => S.legs[k]));
  const id = `${s.sport}|${g.game_id}`;
  return `<div class="tp"><div class="mini-lab">⭐ Top picks for this game <span class="muted">best return for the price first</span></div>
    <ul class="tp-list">${picks.map(topPick).join("")}</ul>
    ${pars.length ? `<details class="gpar" data-gpar="${esc(id)}" ${S.gparOpen[id] ? "open" : ""}><summary>Parlays from this game: ${pars.map((p) => `${esc(p.label)} ${p.est ? "~" : ""}${fmtOdds(p.odds)}`).join(" · ")}</summary>${playCards(pars)}</details>` : ""}</div>`;
}

/* The Parlays section: the best parlay at three sizes of payout, for a day, a group of games, or one game. */
function parlaySection() {
  const P = S.plays || { days: [], groups: [], games: [] };
  const tabs = [["day", "By day"], ["group", "Group of games"], ["game", "Each game"], ["value", "Value only"]];
  let body;
  if (S.parlayTab === "value") {
    const rows = (S.slate.parlays.value || []).filter((p) => S.sport === "all" || p.leg_keys.every((k) => S.legs[k] && S.legs[k].sport.toLowerCase() === S.sport));
    body = `<p class="note" style="margin:0 0 8px">Every pick here has a positive expected return at a real price and comes from a different game, so the standard parlay price applies. Often there are none.</p>${parlayCards(rows)}`;
  } else if (S.parlayTab === "game") {
    const games = (P.games || []).filter((g) => (S.sport === "all" || g.sport === S.sport) && g.parlays.length);
    if (!games.some((g) => `${g.sport}|${g.game_id}` === S.parlayGame)) S.parlayGame = games.length ? `${games[0].sport}|${games[0].game_id}` : null;
    const sel = games.find((g) => `${g.sport}|${g.game_id}` === S.parlayGame);
    body = !sel ? `<div class="empty" style="padding-left:0">No game has enough picks for a parlay right now.</div>`
      : `<label class="play-pick">Game <select data-bind-play="game">${games.map((g) => `<option value="${g.sport}|${esc(g.game_id)}" ${`${g.sport}|${g.game_id}` === S.parlayGame ? "selected" : ""}>${SPORT_ICON[g.sport] || ""} ${esc(g.game)} · ${when(g.start)}</option>`).join("")}</select></label>
        ${playCards(sel.parlays)}`;
  } else {
    let list = (S.parlayTab === "day" ? P.days : P.groups) || [];
    if (S.parlayTab === "day") {
      const have = new Set(list.map((d) => d.date)), solo = {};
      for (const g of P.games || []) if (!have.has(g.date)) (solo[g.date] = solo[g.date] || []).push(g);
      const extra = Object.entries(solo).filter(([, gs]) => gs.length === 1 && gs[0].parlays.length)
        .map(([date, [g]]) => ({ date, start: g.start, n_games: 1, only: g.game, parlays: g.parlays }));
      list = [...list, ...extra].sort((a, b) => String(a.start).localeCompare(String(b.start)));
    }
    const blocks = list.map((d) => {
      const cards = playCards(playsShown(d.parlays));
      if (!cards) return "";
      const title = S.parlayTab === "day" ? `${dayName(d.date)} <span class="muted">${d.n_games === 1 ? `1 game: ${esc(d.only)}` : `${d.n_games} games`}</span>`
        : `${SPORT_ICON[d.sport] || ""} ${d.sport.toUpperCase()} · ${when(d.start)} <span class="muted">${d.n_games} games starting together: ${d.games.map(esc).join(", ")}</span>`;
      return `<h3 class="play-scope">${title}</h3>${cards}`;
    }).join("");
    body = blocks || `<div class="empty" style="padding-left:0">${S.parlayTab === "day" ? "No day has two or more games with picks right now." : "No group of games right now: a group needs two or more games of one sport starting within 90 minutes of each other."}</div>`;
  }
  const intro = S.parlayTab === "value" ? "" : `<p class="note" style="margin:0 0 4px">For each ${S.parlayTab === "day" ? "day" : S.parlayTab === "group" ? "group of games" : "game"}, the best parlay at three sizes of payout.
      <b>Best</b> means the picks that give up the least to the sportsbook for that payout, so you get the most odds the picks can justify. ${S.parlayTab === "game" ? "" : "No more than two picks come from any one game. "}
      Every added pick lowers the chance to hit, so check the <b>fair ${inCents() ? "price" : "odds"}</b>: ${inCents() ? "the bet is worth it only if it costs less than that" : "that is what the bet needs to pay to be worth it"}.</p>`;
  return `<section class="card" id="sec-parlays"><div class="hd"><h2>🧩 Parlays</h2><span class="sub">the best to play, by day, group of games or game</span>
      <div class="right"><div class="seg" role="group" aria-label="Parlays for">${tabs.map(([k, l]) => `<button data-act="ptab" data-tab="${k}" class="${S.parlayTab === k ? "on" : ""}">${l}</button>`).join("")}</div></div></div>
    <div class="bd">${intro}${body}</div></section>`;
}

function pageSlate() {
  const sports = sportsShown();
  const legs = sports.flatMap((s) => s.legs);
  const gameLegs = legs.filter((l) => !isProp(l));
  const propLegs = legs.filter(isProp);
  const lineOnly = sports.reduce((n, s) => n + (s.prop_lines || []).length, 0);
  const value = legs.filter(isValue).sort((a, b) => b.ev - a.ev);
  const pool = S.likelyTab === "games" ? gameLegs : S.likelyTab === "props" ? propLegs : legs;
  const likely = pool.filter((l) => l.odds >= -400 && !l.flag).sort((a, b) => b.p_win - a.p_win).slice(0, 12);
  const top = legs.filter((l) => l.odds >= -400 && !l.flag).sort((a, b) => b.p_win - a.p_win)[0];
  const nGames = sports.reduce((n, s) => n + s.games.length, 0);
  const both = S.sport === "all" && S.slate.sports.length > 1;
  const book = gameLegs[0]?.book || "the sportsbook";

  const ltabs = [["all", "Everything"], ["games", "Game bets"], ["props", "Player props"]];

  const sched = sports.flatMap((s) => s.games.map((g) => ({ g, s }))).sort((a, b) => String(a.g.start).localeCompare(String(b.g.start)));
  const dayKey = (iso) => new Date(iso).toDateString();
  const today0 = new Date().toDateString(), tomorrow0 = new Date(Date.now() + 86400000).toDateString();
  const dayLabel = (iso) => { const k = dayKey(iso), d = new Date(iso).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }); return k === today0 ? `Today · ${d}` : k === tomorrow0 ? `Tomorrow · ${d}` : d; };
  const days = [];
  for (const x of sched) { const k = dayKey(x.g.start); if (!days.length || days[days.length - 1].k !== k) days.push({ k, label: dayLabel(x.g.start), items: [] }); days[days.length - 1].items.push(x); }
  const games = days.map((d) => `<h3 class="day-hd">${d.label} <span class="muted">${d.items.length} game${d.items.length === 1 ? "" : "s"}</span></h3>
    <div class="games">${d.items.map(({ g, s }) => gameCard(g, s, both)).join("")}</div>`);
  const gtime = (g) => new Date(g.start).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  const strip = sched.length > 3 ? `<nav class="gstrip" aria-label="Jump to a game">${sched.map(({ g, s }) => `<button data-act="jump" data-to="g-${s.sport}-${esc(g.game_id)}">${esc(g.away)} @ ${esc(g.home)} <span class="muted">${gtime(g)}</span></button>`).join("")}</nav>
    <select class="gsel" data-jump="1" aria-label="Jump to a game"><option value="">Jump to a game ...</option>${sched.map(({ g, s }) => `<option value="g-${s.sport}-${esc(g.game_id)}">${esc(g.away)} @ ${esc(g.home)} · ${gtime(g)}</option>`).join("")}</select>` : "";
  const tonight = days.length && days[0].k === today0 ? days[0].items.length : 0;
  const nProps = propLegs.length / 2 + lineOnly;
  const intro = S.introSeen ? "" : `<div class="banner intro"><div><b>New here?</b>
      Each pick shows its <b>win chance</b> next to what the <b>book says</b> (the chance its odds imply, with the sportsbook's fee removed).
      When the win chance is clearly higher, the pick is rated ${chip("value")}. Most are not: a pick can be likely to win and still be a poor bet.
      Tap <b>+</b> or any price to add it to the bet slip. ${guideLink("Open the full guide")}</div>
      <button class="btn small" data-act="intro-ok">Got it</button></div>`;
  return `
  <section class="hero"><h1>${tonight ? `${tonight} game${tonight === 1 ? "" : "s"} today` : "Today's picks"}</h1>
    <p>${nGames} upcoming game${nGames === 1 ? "" : "s"} and ${Math.round(nProps).toLocaleString()} player props, each priced against ${esc(book)}.</p>
    <div class="hstats">
      <button class="hstat" data-act="jump" data-to="sec-games"><span class="hv">${nGames}</span><span class="hl">Games</span><span class="hd2">${sports.map((s) => `${s.games.length} ${s.sport.toUpperCase()}`).join(" · ")}</span></button>
      <button class="hstat" data-act="jump" data-to="sec-value"><span class="hv">${value.length}</span><span class="hl">Value bets</span><span class="hd2">${value.length ? `best ${esc(value[0].selection)} ${spct(value[0].ev)}` : "none right now, which is normal"}</span></button>
      <button class="hstat" data-act="jump" data-to="sec-likely"><span class="hv">${top ? pct(top.p_cond, 0) : "–"}</span><span class="hl">Most likely pick</span><span class="hd2">${top ? `${esc(top.selection)} (${fmtOdds(top.odds)})` : "nothing priced"}</span></button>
      <button class="hstat" data-act="nav" data-page="props"><span class="hv">${Math.round(nProps).toLocaleString()}</span><span class="hl">Player props</span><span class="hd2">${sports.filter((s) => s.model).map((s) => `${s.sport.toUpperCase()} ${(s.legs.filter(isProp).length / 2 + (s.prop_lines || []).length).toFixed(0)}`).join(" · ")}</span></button>
    </div></section>
  <div class="filters">${sportSeg()}${jumpNav([["sec-value", "Value bets", value.length], ["sec-likely", "Most likely"], ["sec-parlays", "Parlays"], ["sec-games", "Games", nGames], ["sec-all", "All bets", legs.length]])}</div>
  ${intro}
  ${S.started.some((g) => S.sport === "all" || g.sport === S.sport) ? `<div class="banner"><span>🔴 <b>${S.started.filter((g) => S.sport === "all" || g.sport === S.sport).length === 1 ? "A game has" : "Some games have"} started.</b> Placed a bet on one at your sportsbook? You can still record it here.</span><button class="btn small" data-act="jump" data-to="sec-started">Record a bet</button></div>` : ""}
  <section class="card" id="sec-value"><div class="hd"><h2>🔥 Value bets</h2><span class="sub">the win chance beats the sportsbook's price by a real margin</span><div class="right">${guideLink()}</div></div>
    ${pickCards(value, `<b>No value bets right now.</b> That is the normal state. Sportsbook prices are accurate, and this tool only calls something value where testing showed a real edge, so it will not invent picks.
      Check again closer to game time, or compare your own sportsbook's line on the <button class="linkish" data-act="nav" data-page="props">Player props</button> page.`)}</section>
  <section class="card" id="sec-likely"><div class="hd"><h2>🎯 Most likely to win</h2><span class="sub">highest win chance, odds no shorter than −400. Likely is not the same as profitable: check the rating.</span>
      <div class="right"><div class="seg" role="group" aria-label="Type of pick">${ltabs.map(([k, l]) => `<button data-act="ltab" data-tab="${k}" class="${S.likelyTab === k ? "on" : ""}">${l}</button>`).join("")}</div></div></div>
    ${pickCards(likely, S.likelyTab === "props" ? "No priced props right now. NFL props come without prices: see the Player props page." : "Nothing priced.")}</section>
  ${parlaySection()}
  ${startedSection()}
  <h2 class="sec-title" id="sec-games">🏟️ Games <span class="muted">${nGames}</span></h2>
  <p class="note sec-note">Tap any price to add it to the bet slip. A green dot marks a value bet. These lines reach the tool through ESPN's feed, which can run behind the sportsbook: if yours shows a different number, correct it at the bottom of the game.</p>
  ${strip}
  ${games.join("") || '<div class="empty">No games.</div>'}
  <section class="card" style="margin-top:18px" id="sec-all"><details id="allBets" ${S.all.open ? "open" : ""}><summary class="hd" style="cursor:pointer"><h2>All priced bets</h2><span class="sub">${legs.length.toLocaleString()} bets · search and sort everything · click to ${S.all.open ? "hide" : "show"}</span></summary>
    <div class="bd">${allBetsTable(legs, both)}</div></details></section>`;
}

/* A price in a game's main grid: click to add it to the bet slip. */
function mkBtn(l, main, sub) {
  if (!l) return `<span class="mk-none">–</span>`;
  const on = inSlip(l.key);
  return `<button class="mk-btn ${on ? "on" : ""} ${isValue(l) ? "val" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)}, ${fmtOdds(l.odds)}"
    title="${esc(l.selection)}: win chance ${pct(l.p_cond)} · book's chance ${pct(l.p_fair)} · expected return ${spct(l.ev)} (${RATINGS[ratingOf(l)][0]})">
    <span class="mk-main">${isValue(l) ? '<span class="ev-dot" aria-label="value bet"></span>' : ""}${main}</span><span class="mk-sub">${sub}</span></button>`;
}

/* How far a game's numbers have moved since the sportsbook first posted them. */
function moveChips(g, s) {
  const o = g.open_line || {}, n = g.line || {}, out = [];
  const arrow = (d) => `<span class="ar">${d > 0 ? "▲" : "▼"} ${lineNum(Math.abs(d))}</span>`;
  if (o.total != null && n.total != null && Math.abs(n.total - o.total) > 1e-9) out.push(`<span class="mv" title="The total opened at ${lineNum(o.total)} and is ${lineNum(n.total)} now">Total <b>${lineNum(o.total)} → ${lineNum(n.total)}</b> ${arrow(n.total - o.total)}</span>`);
  if (o.spread_home != null && n.spread_home != null && Math.abs(n.spread_home - o.spread_home) > 1e-9) out.push(`<span class="mv" title="${esc(g.home)}'s spread opened at ${signed(o.spread_home)} and is ${signed(n.spread_home)} now">${esc(g.home)} <b>${signed(o.spread_home)} → ${signed(n.spread_home)}</b></span>`);
  if (!out.length) return "";
  return `<div class="chips"><span class="mini-lab" style="margin:0;align-self:center">Since it opened</span>${out.join("")}<button class="linkish" style="font-size:12px" data-act="moves-game" data-id="${s.sport}|${esc(g.game_id)}">see the chart</button></div>`;
}

/* One game, laid out like a sportsbook: the two teams with moneyline, over/under and spread, then the
   game's player props underneath in the same component. */
function gameCard(g, s, showSport) {
  const id = `${s.sport}|${g.game_id}`;
  const legs = s.legs.filter((l) => l.game_id === g.game_id && !isProp(l));
  const find = (m, side) => legs.find((l) => l.market === m && l.side === side);
  const ml = { home: find("Moneyline", "home"), away: find("Moneyline", "away") };
  const sp = { home: find("Spread", "home"), away: find("Spread", "away") };
  const tot = { over: find("Total", "over"), under: find("Total", "under") };
  const mlBtn = (l) => mkBtn(l, l ? fmtOdds(l.odds) : "", l ? `${pct(l.p_cond, 0)} to win` : "");
  const spBtn = (l) => mkBtn(l, l ? signed(l.line, Math.abs(l.line) % 1 ? 1 : 0) : "", l ? `${fmtOdds(l.odds)} · ${pct(l.p_cond, 0)}` : "");
  const totBtn = (l, tag) => mkBtn(l, l ? `${tag} ${lineNum(l.line)}` : "", l ? `${fmtOdds(l.odds)} · ${pct(l.p_cond, 0)}` : "");
  const ctx = [];
  if (s.sport === "nfl") {
    if (g.qb_away || g.qb_home) ctx.push(`QB ${esc(g.qb_away)} / ${esc(g.qb_home)}`);
    if (g.indoor) ctx.push("Indoors");
    else if (g.temp != null) ctx.push(`${Math.round(g.temp)}°F · wind ${Math.round(g.wind || 0)} mph${g.precip > 0.5 ? ` · rain ${num(g.precip)} mm` : ""}`);
  } else if (g.b2b && g.b2b.length) ctx.push(`Second night of a back-to-back: ${g.b2b.map(esc).join(", ")}`);
  if (legs.some((l) => l.flag)) ctx.push(chip("news"));
  const drivers = (g.drivers || []).slice(0, 4);
  const maxAbs = Math.max(3, ...drivers.map((d) => Math.abs(d[1])));
  const drv = drivers
    .map(([name, v]) => {
      const w = (Math.abs(v) / maxAbs) * 50;
      const style = v >= 0 ? `left:50%;width:${w}%;background:var(--div-pos);border-radius:0 4px 4px 0` : `left:${50 - w}%;width:${w}%;background:var(--div-neg);border-radius:4px 0 0 4px`;
      return `<span title="${esc(name)}">${esc(name)}</span><span class="track"><span class="zero"></span><span class="b" style="${style}"></span></span><span class="v">${signed(v)}</span>`;
    })
    .join("");
  const pw = g.p_home ?? 0.5;
  return `<article class="game" id="g-${s.sport}-${esc(g.game_id)}">
    <div class="g-hd">${teamBadge(s.sport, g.away)}<span class="m">${esc(g.away)} <span class="at">@</span> ${esc(g.home)}</span>${teamBadge(s.sport, g.home)}
      ${g.edited ? '<span class="rate r-lean" title="You corrected this game\'s line. It is kept until the feed changes.">corrected by you</span>' : ""}
      <span class="time">${showSport ? `${SPORT_ICON[s.sport] || ""} ` : ""}${when(g.start)}</span></div>
    <div class="wp" aria-label="Win chance: ${esc(g.away)} ${pct(1 - pw, 0)}, ${esc(g.home)} ${pct(pw, 0)}"><span>${esc(g.away)} ${pct(1 - pw, 0)}</span>
      <span class="bar"><span style="width:${(1 - pw) * 100}%;background:var(--s2)"></span><span style="width:${pw * 100}%;background:var(--s1)"></span></span><span>${pct(pw, 0)} ${esc(g.home)}</span></div>
    <div class="mkt" role="table" aria-label="Main bets for ${esc(g.away)} at ${esc(g.home)}">
      <span></span><span class="mk-h">Moneyline</span><span class="mk-h">Over / Under</span><span class="mk-h">Spread</span>
      <span class="mk-team">${esc(g.away)}</span>${mlBtn(ml.away)}${totBtn(tot.over, "O")}${spBtn(sp.away)}
      <span class="mk-team">${esc(g.home)}</span>${mlBtn(ml.home)}${totBtn(tot.under, "U")}${spBtn(sp.home)}
    </div>
    ${moveChips(g, s)}
    ${ctx.length ? `<div class="chips">${ctx.map((c) => (c.startsWith("<span class=\"rate") ? c : `<span class="chip">${c}</span>`)).join("")}</div>` : ""}
    <details class="why" data-why="${esc(id)}" ${S.whyOpen[id] ? "open" : ""}><summary>The model's own numbers: ${favLine(g.home, g.away, g.model_margin)}, total ${num(g.model_total)}</summary>
      <div class="lines"><span class="h"></span><span class="h">Sportsbook${g.edited ? " (your numbers)" : ""}</span><span class="h">Model's own number</span>
        <span class="muted">Spread</span><span class="v">${favLine(g.home, g.away, g.mkt_margin)}</span><span class="v">${favLine(g.home, g.away, g.model_margin)}</span>
        <span class="muted">Total</span><span class="v">${num(g.mkt_total)}</span><span class="v">${num(g.model_total)}</span></div>
      ${drv ? `<div class="mini-lab" style="margin-top:8px">What moves the model most <span class="muted">(points toward ${esc(g.home)} →)</span></div><div class="drivers">${drv}</div>` : ""}
      <p class="note" style="margin:8px 0 0">The win chances above start from the sportsbook's price and move toward the model only as far as testing has justified.</p></details>
    ${gamePlays(s, g)}
    ${gameProps(s, g)}
    ${fixForm(g, s)}</article>`;
}

/* The feed can run behind the sportsbook: let the user type in what their book shows now. */
function fixForm(g, s) {
  const L = g.line || {};
  const v = (x) => (x == null ? "" : x);
  const priced = (name) => name.endsWith("_odds") || name.startsWith("ml_");  // a price, as against a line
  const inp = (name, val, label, step = "1") => `<label><span>${priced(name) && inCents() ? label.replace("odds", "price ¢") : label}</span><input type="number" step="${priced(name) && inCents() ? "0.1" : step}" name="${name}" ${priced(name) ? 'data-price="1"' : ""} value="${priced(name) ? boxPrice(val) : v(val)}" aria-label="${label}"></label>`;
  const open = S.fixOpen === `${s.sport}|${g.game_id}`;
  return `<details class="fix" data-fix="${s.sport}|${esc(g.game_id)}" ${open ? "open" : ""}>
    <summary>${g.edited ? "Change your corrected line" : "Different at your sportsbook? Correct the line"}</summary>
    <p class="note">Type what your sportsbook shows now. The game is re-priced at those numbers, as if the feed had delivered them.${g.edited && g.feed_line ? ` The feed says: spread ${esc(g.home)} ${signed(g.feed_line.spread_home)}, total ${lineNum(g.feed_line.total)}.` : ""}</p>
    <div class="fix-grid" data-sport="${s.sport}" data-game="${esc(g.game_id)}">
      <span class="fl">Spread</span>${inp("spread_home", L.spread_home, `${esc(g.home)} line`, "0.5")}${inp("spread_home_odds", L.spread_home_odds, `${esc(g.home)} odds`)}${inp("spread_away_odds", L.spread_away_odds, `${esc(g.away)} odds`)}
      <span class="fl">Total</span>${inp("total", L.total, "Total", "0.5")}${inp("over_odds", L.over_odds, "Over odds")}${inp("under_odds", L.under_odds, "Under odds")}
      <span class="fl">To win</span>${inp("ml_home", L.ml_home, `${esc(g.home)} odds`)}${inp("ml_away", L.ml_away, `${esc(g.away)} odds`)}<span></span>
    </div>
    <div class="fix-act"><button class="btn small primary" data-act="fix-save">Re-price this game</button>${g.edited ? '<button class="btn small" data-act="fix-reset">Go back to the feed\'s line</button>' : ""}</div></details>`;
}

function allBetsTable(legs, both) {
  const f = S.all;
  let rows = legs.filter((l) => !f.market || (f.market === "Props" ? isProp(l) : l.market === f.market));
  if (f.q) rows = rows.filter((l) => `${l.selection} ${l.game}`.toLowerCase().includes(f.q.toLowerCase()));
  rows.sort((a, b) => f.dir * ((a[f.sort] ?? -99) - (b[f.sort] ?? -99)));
  const sel = ["", "Moneyline", "Spread", "Total", "Props"].map((m) => `<option value="${m}" ${f.market === m ? "selected" : ""}>${m ? (m === "Props" ? "Player props" : m) : "All bet types"}</option>`).join("");
  const sorts = [["ev", "Expected return"], ["p_win", "Win chance"], ["edge", "Edge"]].map(([k, l]) => `<option value="${k}" ${f.sort === k ? "selected" : ""}>Sort by ${l.toLowerCase()}</option>`).join("");
  return `<div class="filters" style="margin-bottom:8px"><select data-bind="all.market" aria-label="Bet type">${sel}</select><select data-bind="all.sort" aria-label="Sort">${sorts}</select>
    <input type="search" placeholder="Search team or player" data-bind="all.q" value="${esc(f.q)}" aria-label="Search"></div>${legsTable(rows.slice(0, 200), { sport: both, empty: "Nothing matches." })}
    ${rows.length > 200 ? `<p class="note" style="padding:8px 0 0">Showing the first 200 of ${rows.length.toLocaleString()}. Narrow it with the filters.</p>` : ""}`;
}

// ============================================================================ player props page
/* One row per prop line, the way a sportsbook shows it: player, stat, line, Over and Under. */
function propRows(s) {
  const by = new Map();
  for (const l of s.legs) {
    if (!isProp(l)) continue;
    if (!by.has(l.group)) by.set(l.group, []);
    by.get(l.group).push(l);
  }
  const rows = [];
  const have = new Set();
  for (const ls of by.values()) {
    const over = ls.find((l) => l.side === "over" || l.side === "yes"), under = ls.find((l) => l.side === "under");
    const a = over || under;
    const simOver = a.sim_over ?? null;
    const fairOver = over ? over.p_fair : under ? 1 - under.p_fair : null;
    have.add(`${a.player_key}|${a.market_key}`);
    rows.push({ id: a.group, player: a.subject, pkey: a.player_key, mkey: a.market_key, market: a.market, game: a.game, game_id: a.game_id, start: a.start,
      line: a.side === "yes" ? null : a.line, proj: typical(a.line, a.proj, a.proj_med), over, under, yesOnly: a.side === "yes", priced: true,
      mine: a.book === "manual" || a.book === "corrected", refLine: a.ref_line ?? null, book: a.book, flag: a.flag,
      gap: a.side === "yes" || simOver == null || fairOver == null ? null : (a.ref_line != null ? null : simOver - fairOver),
      best: Math.max(over ? over.p_cond : 0, under ? under.p_cond : 0), ev: Math.max(over ? over.ev : -9, under ? under.ev : -9),
      value: ls.some(isValue) });
  }
  for (const p of s.prop_lines || []) {
    if (have.has(`${p.player_key}|${p.market_key}`)) continue;
    rows.push({ id: `${p.game_id}|${p.player_key}|${p.market_key}`, player: p.player, pkey: p.player_key, mkey: p.market_key, market: p.market, game: p.game, game_id: p.game_id, start: p.start,
      line: p.line, proj: typical(p.line, p.proj, p.proj_med), over: null, under: null, yesOnly: false, priced: false, mine: false, book: p.book,
      flag: Math.abs(p.p_over - 0.5) >= settings().prop_gap_flag ? "far" : "",
      gap: p.p_over - 0.5, best: 0, ev: -9, value: false });
  }
  return rows;
}

const typical = (line, mean, med) => (line != null && line < 10 ? mean : med ?? mean);

const STAT_ORDER = {
  nba: ["pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra", "stl", "blk"],
  nfl: ["pass_yds", "pass_tds", "pass_cmp", "pass_att", "rush_yds", "rush_att", "rec_yds", "receptions", "rush_rec_yds", "anytime_td"],
};

/* Prop rows for a sport, worked out once per update: each with the player's team, plus an index by game. */
function sportRows(s) {
  if (s._rows) return s._rows;
  const team = {};
  for (const r of s.proj || []) if (r.key) team[r.key] = r.team;
  const rows = propRows(s), byGame = new Map();
  for (const r of rows) {
    r.team = team[r.pkey] || "";
    if (!byGame.has(r.game_id)) byGame.set(r.game_id, []);
    byGame.get(r.game_id).push(r);
  }
  s._rows = { rows, byGame };
  return s._rows;
}

function leanText(r) {
  if (r.gap == null) return "";
  const pts = Math.abs(r.gap) * 100;
  if (pts < 1) return `<span class="lt">simulator agrees</span>`;
  const over = r.gap >= 0;
  return `<span class="lt" title="The simulator thinks the ${over ? "over" : "under"} is ${pts.toFixed(0)} points more likely than ${r.priced ? "the sportsbook's price says" : "an even bet"}"><i class="ldot ${over ? "o" : "u"}"></i>leans ${over ? "over" : "under"} +${pts.toFixed(0)}</span>`;
}

function propTags(r) {
  const st = settings(), tags = [];
  if (r.value) tags.push(chip("value"));
  if (r.flag) tags.push(chip("news"));
  else if (r.gap != null && Math.abs(r.gap) >= st.prop_lean) tags.push(`<span class="rate r-lean" title="The simulator disagrees with the ${r.priced ? "sportsbook" : "line"} by 15 points or more">💪 Strong lean</span>`);
  if (r.mine) tags.push(`<span class="badge">${r.book === "corrected" ? "corrected by you" : "your book"}</span>`);
  return tags.join(" ");
}

/* One prop line as a row: who or what, the line, Over and Under. `label` is the player (by stat) or the stat (by player). */
function gpRow(r, label, s) {
  const sub = leanText(r);  // the simulator's own number is on the ranked list and the projections table
  const buttons = r.priced
    ? `<div class="gp-o">${sideBtn(r.over, r.yesOnly ? "Yes" : "Over")}</div><div class="gp-u">${r.yesOnly ? "" : sideBtn(r.under, "Under")}</div>`
    : `<div class="gp-o gp-two"><button class="btn small" data-act="price-row" data-sport="${s.sport}" data-player="${esc(r.player)}" data-market="${esc(r.mkey)}" data-line="${r.line}" title="This line came without a price. Enter the odds your sportsbook shows to get an expected return.">Enter odds</button></div>`;
  return `<div class="gp-row"><div class="gp-who">${label} ${propTags(r)}<div class="sub2">${sub}</div></div>
    <div class="gp-line">${r.yesOnly ? "" : `<b>${lineNum(r.line)}</b>`}</div>${buttons}</div>`;
}

/* A game's player props. On the home page: the top few lines for one stat, with chips to switch stat.
   On the Player props page (full): every line, grouped by stat or by player. */
function gameProps(s, g, { full = false } = {}) {
  const id = `${s.sport}|${g.game_id}`;
  let rows = sportRows(s).byGame.get(g.game_id) || [];
  const head = (extra) => `<div class="gp-hd"><span class="gp-title">Player props</span>${extra}</div>`;
  if (!rows.length) return full ? `<div class="empty">No prop lines are posted for this game yet.</div>` : `<div class="gp-wrap" data-gp="${esc(id)}">${head('<span class="muted">no lines posted yet for this game</span>')}</div>`;
  const total = rows.length;
  if (full && S.pb.q) rows = rows.filter((r) => r.player.toLowerCase().includes(S.pb.q.toLowerCase()));
  const order = STAT_ORDER[s.sport] || [];
  const stats = [...order.filter((k) => rows.some((r) => r.mkey === k)), ...[...new Set(rows.map((r) => r.mkey))].filter((k) => !order.includes(k))];
  const label = (k) => (s.markets || {})[k] || k;
  const who = (r) => `<span class="pick">${esc(r.player)}</span>${r.team ? ` <span class="badge">${esc(r.team)}</span>` : ""}`;
  const byLine = (a, b) => (b.line ?? 0) - (a.line ?? 0) || (b.best - a.best) || a.player.localeCompare(b.player);
  let body;
  if (full && S.pg === "player") {
    const players = new Map();
    for (const r of rows) { if (!players.has(r.pkey)) players.set(r.pkey, []); players.get(r.pkey).push(r); }
    const rank = (rs) => { for (const k of order) { const r = rs.find((x) => x.mkey === k); if (r) return order.length - order.indexOf(k) + (r.line ?? 0) / 1000; } return 0; };
    const list = [...players.values()].sort((a, b) => (a[0].team || "").localeCompare(b[0].team || "") || rank(b) - rank(a));
    body = list.map((rs) => `<div class="gp-player">${who(rs[0])}<span class="muted">${rs.length} line${rs.length === 1 ? "" : "s"}</span></div>
      ${rs.sort((a, b) => stats.indexOf(a.mkey) - stats.indexOf(b.mkey)).map((r) => gpRow(r, `<span class="gp-stat">${esc(r.market)}</span>`, s)).join("")}`).join("") || `<div class="empty">No players match.</div>`;
    return `<div class="gp-wrap full" data-gp="${esc(id)}" data-full="1"><div class="gp-cols"><span>Player and stat</span><span>Line</span><span>Over</span><span>Under</span></div>${body}</div>`;
  }
  let stat = S.gp[id];
  if (!stats.includes(stat)) stat = stats[0];
  const mine = rows.filter((r) => r.mkey === stat).sort(byLine);
  const limit = full || S.gpAll[id] ? mine.length : 5;
  const chips = `<div class="gp-chips" role="group" aria-label="Stat">${stats.map((k) => `<button data-act="gp-stat" data-gp="${esc(id)}" data-stat="${esc(k)}" class="${k === stat ? "on" : ""}">${esc(label(k))}</button>`).join("")}</div>`;
  body = mine.slice(0, limit).map((r) => gpRow(r, who(r), s)).join("") || `<div class="empty">No players match.</div>`;
  const more = full ? "" : `<div class="gp-foot">${mine.length > 5 ? `<button class="linkish" data-act="gp-more" data-gp="${esc(id)}">${S.gpAll[id] ? "Show fewer" : `Show all ${mine.length} ${esc(label(stat))} lines`}</button>` : "<span></span>"}
      <button class="linkish" data-act="game-props" data-sport="${s.sport}" data-game="${esc(g.game_id)}">All ${total} props for this game →</button></div>`;
  return `<div class="gp-wrap ${full ? "full" : ""}" data-gp="${esc(id)}" ${full ? 'data-full="1"' : ""}>
    ${full ? "" : head(`<span class="muted">${total} lines${s.prop_book ? ` · ${esc(s.prop_book)}` : ""}</span>`)}${chips}
    <div class="gp-cols"><span>${esc(label(stat))}</span><span>Line</span><span>${mine.some((r) => r.priced) ? "Over" : ""}</span><span>${mine.some((r) => r.priced) ? "Under" : ""}</span></div>${body}${more}</div>`;
}

function redrawGameProps(id) {
  const [sport, gid] = [id.slice(0, id.indexOf("|")), id.slice(id.indexOf("|") + 1)];
  const s = S.slate.sports.find((x) => x.sport === sport);
  const g = s && s.games.find((x) => String(x.game_id) === gid);
  if (!g) return;
  for (const el of $$(".gp-wrap")) if (el.dataset.gp === id) el.outerHTML = gameProps(s, g, { full: el.dataset.full === "1" });
}

/* Player props page, grouped by game: each game is a section with its lines by stat or by player. */
function propsByGame(s) {
  const by = sportRows(s).byGame;
  const q = (S.pb.q || "").toLowerCase();
  const games = [...s.games].sort((a, b) => String(a.start).localeCompare(String(b.start)));
  const withLines = games.filter((g) => (by.get(g.game_id) || []).some((r) => !q || r.player.toLowerCase().includes(q)));
  const without = games.filter((g) => !(by.get(g.game_id) || []).length);
  if (S.pgOpen == null) S.pgOpen = withLines.length ? { [`${s.sport}|${withLines[0].game_id}`]: true } : {};
  const sections = withLines.map((g) => {
    const id = `${s.sport}|${g.game_id}`, n = (by.get(g.game_id) || []).length;
    const open = q ? true : !!S.pgOpen[id];
    const L = g.line || {};
    const mainLine = [L.ml_home != null ? `${esc(g.home)} ${fmtOdds(L.ml_home)} / ${esc(g.away)} ${fmtOdds(L.ml_away)}` : "", L.total != null ? `O/U ${lineNum(L.total)}` : ""].filter(Boolean).join(" · ");
    return `<section class="card pgame" id="pg-${s.sport}-${esc(g.game_id)}"><details data-pg="${esc(id)}" ${open ? "open" : ""}>
      <summary class="hd">${teamBadge(s.sport, g.away, true)}<h2>${esc(g.away)} @ ${esc(g.home)}</h2>${teamBadge(s.sport, g.home, true)}<span class="sub">${when(g.start)} · ${n} lines${mainLine ? ` · ${mainLine}` : ""}</span><span class="right muted caret" aria-hidden="true"></span></summary>
      <div class="bd">${open ? gameProps(s, g, { full: true }) : ""}</div></details></section>`;
  }).join("");
  const gtime = (g) => new Date(g.start).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  const strip = withLines.length > 1 ? `<nav class="gstrip" aria-label="Jump to a game">${withLines.map((g) => `<button data-act="pg-jump" data-pg="${s.sport}|${esc(g.game_id)}">${esc(g.away)} @ ${esc(g.home)} <span class="muted">${gtime(g)}</span></button>`).join("")}</nav>
    <select class="gsel" data-pgjump="1" aria-label="Jump to a game"><option value="">Jump to a game ...</option>${withLines.map((g) => `<option value="${s.sport}|${esc(g.game_id)}">${esc(g.away)} @ ${esc(g.home)} · ${gtime(g)}</option>`).join("")}</select>` : "";
  return `<div id="sec-board">${strip}
    ${sections || `<section class="card"><div class="empty">${q ? "No player matches that search." : "No prop lines are posted yet."}</div></section>`}
    ${without.length && !q ? `<p class="note">No lines posted yet for: ${without.map((g) => `${esc(g.away)} @ ${esc(g.home)}`).join(", ")}. Sportsbooks usually post them the day before a game.</p>` : ""}</div>`;
}

function sideBtn(l, label) {
  if (!l) return `<span class="muted">–</span>`;
  const on = inSlip(l.key);
  return `<button class="side-btn ${on ? "on" : ""} ${isValue(l) ? "val" : ""}" data-act="toggle" data-key="${esc(l.key)}" aria-label="${on ? "Remove" : "Add"} ${esc(l.selection)}"
    title="${esc(l.selection)}: win chance ${pct(l.p_cond)} · book's chance ${pct(l.p_fair)} · expected return ${spct(l.ev)}">
    <span class="s">${label}</span><span class="o">${fmtOdds(l.odds)}</span><span class="p">${pct(l.p_cond, 0)}</span></button>`;
}

function leanCell(r) {
  if (r.gap == null) return `<span class="muted">–</span>`;
  const st = settings();
  const pts = Math.abs(r.gap) * 100;
  const w = Math.min(Math.abs(r.gap) / st.prop_gap_flag, 1) * 50;
  const style = r.gap >= 0 ? `left:50%;width:${w}%;background:var(--div-pos);border-radius:0 4px 4px 0` : `left:${50 - w}%;width:${w}%;background:var(--div-neg);border-radius:4px 0 0 4px`;
  const side = r.gap >= 0 ? "Over" : "Under";
  return `<div class="lean" title="The simulator thinks the ${side.toLowerCase()} is ${pts.toFixed(0)} points more likely than ${r.priced ? "the sportsbook's price says" : "an even bet"}">
    <span class="track"><span class="zero"></span><span class="b" style="${style}"></span></span><span class="lv">${pts < 1 ? "Agrees" : `${side} +${pts.toFixed(0)}`}</span></div>`;
}

function propBoard(s) {
  const st = settings();
  const all = sportRows(s).rows;
  const hasPriced = all.some((r) => r.priced);
  const mine = all.filter((r) => r.mine);
  const nValue = all.filter((r) => r.value).length;
  const tabs = [];
  if (hasPriced) tabs.push(["likely", "Most likely"], ["value", `Value <span class="muted">${nValue}</span>`]);
  tabs.push(["lean", "Biggest disagreements"], ["az", "A to Z"]);
  if (mine.length) tabs.push(["mine", `Your lines <span class="muted">${mine.length}</span>`]);
  if (!tabs.some(([k]) => k === S.pb.tab)) S.pb.tab = tabs[0][0];
  let rows = all;
  if (S.pb.game) rows = rows.filter((r) => String(r.game_id) === S.pb.game);
  if (S.pb.stat) rows = rows.filter((r) => r.mkey === S.pb.stat);
  if (S.pb.q) rows = rows.filter((r) => r.player.toLowerCase().includes(S.pb.q.toLowerCase()));
  if (S.pb.tab === "likely") rows = rows.filter((r) => r.priced).sort((a, b) => b.best - a.best);
  else if (S.pb.tab === "value") rows = rows.filter((r) => r.value).sort((a, b) => b.ev - a.ev);
  else if (S.pb.tab === "lean") rows = rows.filter((r) => r.gap != null).sort((a, b) => Math.abs(b.gap) - Math.abs(a.gap));
  else if (S.pb.tab === "mine") rows = rows.filter((r) => r.mine);
  else rows = [...rows].sort((a, b) => a.player.localeCompare(b.player) || a.market.localeCompare(b.market));

  const gamesOpt = s.games.map((g) => `<option value="${esc(g.game_id)}" ${S.pb.game === String(g.game_id) ? "selected" : ""}>${esc(g.away)} @ ${esc(g.home)}</option>`).join("");
  const stats = [...new Map(all.map((r) => [r.mkey, r.market])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const statsOpt = stats.map(([k, l]) => `<option value="${esc(k)}" ${S.pb.stat === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  const blurb = {
    likely: "The props with the highest win chance on either side. A high win chance usually comes with short odds, so check the expected return in the tooltip.",
    value: "Props where the tool's win chance beats the sportsbook's price by a real margin.",
    lean: `Where the simulator and the sportsbook ${hasPriced ? "disagree" : "line disagree"} most. A disagreement is a lean, not a bet. The very largest are marked <span class="rate r-news">Check news</span>: a gap that big usually means the simulator is missing something (an injury return, a new role), and in testing those lost more often than they won. <span class="rate r-lean">Strong lean</span> marks the range that did best in testing.`,
    az: "Every prop line, by player.",
    mine: "Lines you typed in. A corrected line replaces the tool's copy; a line from another sportsbook is priced against the reference line for the same player.",
  }[S.pb.tab];
  const empty = {
    value: `<b>No value props right now.</b> That is expected. In testing, the sportsbook's prop prices were more accurate than the simulator, so the tool leans on them heavily.
      Value is most likely to show up when <i>your</i> sportsbook's line differs from DraftKings: enter it under “Price a line from your sportsbook” below.`,
  }[S.pb.tab] || "Nothing matches these filters.";

  const body = rows.slice(0, S.pb.limit).map((r) => {
    const tags = [];
    if (r.value) tags.push(chip("value"));
    if (r.flag) tags.push(chip("news"));
    else if (r.gap != null && Math.abs(r.gap) >= st.prop_lean) tags.push(`<span class="rate r-lean" title="The simulator disagrees with the ${r.priced ? "sportsbook" : "line"} by 15 points or more. See the test results above for how these have done.">💪 Strong lean</span>`);
    if (r.mine) tags.push(r.book === "corrected" ? `<span class="badge" title="You entered the sportsbook's current line">corrected by you</span>` : `<span class="badge" title="A line you entered from another sportsbook">your book</span>`);
    return `<tr>
      <td><span class="pick">${esc(r.player)}</span> ${tags.join(" ")}<div class="sub2">${esc(r.game)} · ${when(r.start)}${r.refLine != null ? ` · DraftKings line ${lineNum(r.refLine)}` : ""}</div></td>
      <td>${esc(r.market)}</td>
      <td class="num"><b>${r.yesOnly ? "–" : lineNum(r.line)}</b></td>
      <td class="num">${r.yesOnly ? "–" : num(r.proj, 1)}</td>
      <td class="num">${r.priced ? sideBtn(r.over, r.yesOnly ? "Yes" : "Over") : ""}</td>
      <td class="num">${r.priced ? (r.yesOnly ? `<span class="muted">–</span>` : sideBtn(r.under, "Under")) : `<button class="btn small" data-act="price-row" data-player="${esc(r.player)}" data-market="${esc(r.mkey)}" data-line="${r.line}">Enter odds</button>`}</td>
      <td>${leanCell(r)}</td></tr>`;
  }).join("");
  const table = rows.length ? `<div class="tw"><table class="t board"><thead><tr><th>Player</th><th>Stat</th>
      <th class="num" title="The sportsbook's line">Line</th>
      <th class="num" title="A typical result in the simulation: the middle outcome for yards and points, the average for low counts such as threes or receptions. It is the simulator's own number, before it is compared with other players' lines.">Simulator projects</th>
      <th class="num" title="Odds, then the tool's win chance. Click to add to the bet slip.">${hasPriced ? "Over <span class=\"muted\">odds · win chance</span>" : "Over"}</th>
      <th class="num">${hasPriced ? "Under <span class=\"muted\">odds · win chance</span>" : "Under"}</th>
      <th title="How many percentage points more likely the simulator thinks the over (blue, to the right) or the under (red, to the left) is, compared with the sportsbook">Simulator vs ${hasPriced ? "book" : "line"}</th></tr></thead><tbody>${body}</tbody></table></div>
      ${rows.length > S.pb.limit ? `<div class="more"><button class="btn" data-act="pb-more">Show more (${(rows.length - S.pb.limit).toLocaleString()} left)</button></div>` : ""}` : `<div class="empty">${empty}</div>`;

  return `<section class="card" id="sec-board"><div class="hd"><h2>Prop board</h2><span class="sub">${all.length.toLocaleString()} lines${s.prop_book ? ` from ${esc(s.prop_book)}` : ""}${hasPriced ? "" : " · no prices in the free feed"}</span>
      <div class="right">${guideLink("How to read this")}</div></div>
    <div class="bd">
      <div class="filters" style="margin-bottom:8px"><div class="seg" role="group" aria-label="View">${tabs.map(([k, l]) => `<button data-act="pb-tab" data-tab="${k}" class="${S.pb.tab === k ? "on" : ""}">${l}</button>`).join("")}</div>
        <span class="spacer"></span>
        <select data-bind="pb.game" data-rerender="1" aria-label="Game"><option value="">All games</option>${gamesOpt}</select>
        <select data-bind="pb.stat" data-rerender="1" aria-label="Stat"><option value="">All stats</option>${statsOpt}</select>
        <input type="search" placeholder="Search player" data-bind="pb.q" data-rerender="1" value="${esc(S.pb.q)}" aria-label="Search player"></div>
      <p class="note" style="margin:0 0 8px">${blurb}</p></div>
    <div class="bd flush">${table}</div></section>`;
}

function trustCard(s) {
  const c = s.prop_check;
  const st = settings();
  const w = st.prop_weight[s.sport];
  const src = st.prop_weight_source[s.sport];
  const SP = s.sport.toUpperCase();
  if (!c) {
    return `<section class="card"><div class="hd"><h2>How much to trust ${SP} prop picks</h2></div><div class="bd"><p class="note">The ${SP} prop simulator has not been tested against real lines yet${src === "borrowed" ? `, so it uses the weight learned for the other sport (${pct(w, 0)})` : ""}. It is tested automatically when lines for finished games are available.</p></div></section>`;
  }
  const lean = c.lean || {};
  let tiles, text;
  if (c.priced) {
    tiles = `
      <div class="tile"><div class="label">Real lines tested</div><div class="value">${c.n_lines.toLocaleString()}</div><div class="delta">${c.days} game days · ${day(c.first)} to ${day(c.last)}</div></div>
      <div class="tile"><div class="label">Weight on the simulator</div><div class="value">${pct(w, 0)}</div><div class="delta">the sportsbook's price gets the other ${pct(1 - w, 0)}</div></div>
      <div class="tile"><div class="label">Simulator's strong leans won</div><div class="value">${pct(lean.won)}</div><div class="delta">${(lean.n || 0).toLocaleString()} lines · book's price implied ${pct(lean.market)}</div></div>
      <div class="tile"><div class="label">Needed to beat the book's fee</div><div class="value">${pct(BREAK_EVEN)}</div><div class="delta">at a standard −110 price</div></div>`;
    text = `<b>What this means.</b> The sportsbook's closing prices predicted ${SP} props better than the simulator did, so a prop's win chance here is ${pct(1 - w, 0)} the book's own price and ${pct(w, 0)} the simulator.
      The simulator's strong leans won a little more often than the book implied, but not often enough to cover the fee. So expect few or no <span class="rate r-value">Value</span> props at DraftKings prices.
      This page is most useful for finding the most likely props, comparing <i>your</i> sportsbook's line with DraftKings, and building same-game parlays.`;
  } else {
    const ly = c.lean_yards || {};
    tiles = `
      <div class="tile"><div class="label">Real lines tested</div><div class="value">${c.n_lines.toLocaleString()}</div><div class="delta">last ${c.days} weeks · DraftKings lines, no prices</div></div>
      <div class="tile"><div class="label">Weight on the simulator</div><div class="value">${pct(w, 0)}</div><div class="delta">the line as an even bet gets the other ${pct(1 - w, 0)}</div></div>
      <div class="tile"><div class="label">Simulator's strong leans won</div><div class="value">${pct(lean.won)}</div><div class="delta">${(lean.n || 0).toLocaleString()} lines${ly.n ? ` · yardage stats only: ${pct(ly.won)}` : ""}</div></div>
      <div class="tile"><div class="label">Needed to beat the book's fee</div><div class="value">52–55%</div><div class="delta">at typical −110 to −120 prices</div></div>`;
    text = `<b>What this means.</b> The free feed has ${SP} prop <i>lines</i> but not their prices, so nothing here is rated Value until you enter your sportsbook's odds.
      When the simulator disagreed strongly with the real DraftKings line, its side won ${pct(lean.won, 0)} of the time over the last ${c.days} weeks. That is encouraging, but it is not proof of profit:
      without the prices there is no telling how much of it the sportsbook had already charged for, especially on receptions and touchdowns, where one side often costs much more.
      Use the “Biggest disagreements” view to find candidates, then click <b>Enter odds</b> to see the expected return at your book's price.`;
  }
  return `<section class="card"><div class="hd"><h2>How much to trust ${SP} prop picks</h2><span class="sub">the simulator was replayed on past games and compared with real sportsbook lines</span>
      <div class="right"><button class="linkish" data-act="nav" data-page="model">See the full test</button></div></div>
    <div class="bd"><div class="tiles" style="margin-bottom:10px">${tiles}</div><p class="note" style="margin:0">${text}</p></div></section>`;
}

function pageProps() {
  const loaded = S.slate.sports.filter((s) => s.model);
  if (!loaded.length) return pageHead("Player props", "No games are loaded.");
  if (!S.form.sport || !loaded.some((s) => s.sport === S.form.sport)) S.form.sport = loaded[0].sport;
  const s = loaded.find((x) => x.sport === S.form.sport);
  const SP = s.sport.toUpperCase();
  const markets = Object.entries(s.markets || {});
  if (!S.form.market || !markets.some(([k]) => k === S.form.market)) S.form.market = markets[0]?.[0] || "";
  const proj = (s.proj || []).filter((r) => r.status !== "out");
  const outs = (s.proj || []).filter((r) => r.status === "out").map((r) => r.player);
  const players = [...new Set(proj.map((r) => r.player))].sort();
  const hasPriced = s.legs.some((l) => isProp(l) && l.book !== "manual" && l.book !== "corrected");
  const seg = `<div class="seg" role="group" aria-label="Sport">${loaded.map((x) => `<button data-act="prop-sport" data-sport="${x.sport}" class="${x.sport === s.sport ? "on" : ""}">${x.sport.toUpperCase()}</button>`).join("")}</div>`;

  const refBook = esc(s.prop_book || "DraftKings");
  const same = S.form.from !== "other";
  const form = `
    <div class="form-row" style="margin-bottom:10px"><label>Where is this line from?<select data-bind="form.from" data-rerender="1" aria-label="Where is this line from?">
      <option value="same" ${same ? "selected" : ""}>${refBook}: its line changed, or the tool has no price for it</option>
      <option value="other" ${same ? "" : "selected"}>Another sportsbook: compare it with ${refBook}</option></select></label></div>
    <p class="note" style="margin:0 0 10px">${same
      ? `The tool's copy of the ${refBook} line arrives through ESPN's feed and can run behind${hasPriced ? "" : ", and for " + SP + " it comes without prices"}. Enter the line and odds ${refBook} shows now and it replaces the tool's copy. A line that simply moved is not a bargain, so it is priced on its own odds.`
      : `Sportsbooks often post different lines for the same player. The tool prices your book's line using the ${refBook} line as the benchmark${hasPriced ? "" : " (taken as an even bet at its line)"}, adjusted for the difference between the two lines. Only trust the result if ${refBook} still shows the line listed on the board.`}</p>
    <div class="form-row">
      <label class="player">Player<input type="text" list="players" data-bind="form.player" id="pf-player" value="${esc(S.form.player)}" placeholder="Start typing a name"></label>
      <label>Stat<select data-bind="form.market" id="pf-market">${markets.map(([k, l]) => `<option value="${k}" ${k === S.form.market ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <label>Line<input type="number" step="0.5" data-bind="form.line" id="pf-line" value="${esc(S.form.line)}" placeholder="24.5"></label>
      <label>${inCents() ? "Over (Yes) price ¢" : "Over odds"}<input type="number" step="${inCents() ? "0.1" : "1"}" data-bind="form.over" id="pf-over" value="${esc(S.form.over)}" placeholder="${inCents() ? "52" : "-110"}"></label>
      <label>${inCents() ? "Under (No) price ¢" : "Under odds"}<input type="number" step="${inCents() ? "0.1" : "1"}" data-bind="form.under" value="${esc(S.form.under)}" placeholder="${inCents() ? "50" : "-110"}"></label>
      <button class="btn primary" data-act="price-one">Price it</button>
    </div>
    <datalist id="players">${players.map((p) => `<option value="${esc(p)}">`).join("")}</datalist>
    <details style="margin-top:12px"><summary class="muted" style="cursor:pointer">Paste several lines at once</summary>
      <p class="note">One per line: <code>player, stat, line, over odds, under odds</code>. Example: <code>${s.sport === "nba" ? "Nikola Jokic, Points, 27.5, -115, -105" : "Josh Allen, Pass Yds, 245.5, -110, -110"}</code></p>
      <textarea data-bind="form.paste" placeholder="player, stat, line, over odds, under odds">${esc(S.form.paste)}</textarea>
      <div style="margin-top:8px"><button class="btn" data-act="price-many">Price all</button></div></details>
    ${S.priceErrors.length ? `<div class="banner warn" style="margin:12px 0 0">${S.priceErrors.map(esc).join("<br>")}</div>` : ""}`;

  const nLines = sportRows(s).rows.length;
  const byGame = S.pv !== "list";
  const views = `<div class="seg" role="group" aria-label="View">${[["game", "By game"], ["list", "Ranked list"]].map(([k, l]) => `<button data-act="pv" data-v="${k}" class="${(byGame ? "game" : "list") === k ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const grouping = byGame ? `<div class="seg" role="group" aria-label="Inside each game, group by">${[["stat", "By stat"], ["player", "By player"]].map(([k, l]) => `<button data-act="pg" data-v="${k}" class="${S.pg === k ? "on" : ""}">${l}</button>`).join("")}</div>
    <input type="search" placeholder="Search player" data-bind="pb.q" data-rerender="1" value="${esc(S.pb.q)}" aria-label="Search player">
    <button class="btn small ghost" data-act="pg-all" data-v="1">Open all</button><button class="btn small ghost" data-act="pg-all" data-v="0">Close all</button>` : "";
  return `
  ${pageHead("🎯 Player props", `Every prop line for ${SP}, game by game, with the simulator's projection beside the sportsbook's number. ${nLines ? `${nLines.toLocaleString()} lines from ${esc(s.prop_book || "your entries")}.` : "No lines posted yet."}`)}
  <div class="filters">${seg}${jumpNav([["sec-board", byGame ? "Props by game" : "Ranked list", nLines], ["sec-trust", "How much to trust them"], ["sec-price", "Price your book's line"], ["sec-proj", "Projections"]])}</div>
  <div class="filters">${views}${grouping}</div>
  ${!nLines ? `<section class="card" id="sec-board"><div class="hd"><h2>Player props</h2></div><div class="empty"><b>No ${SP} prop lines are posted yet.</b> Sportsbooks usually post them the day before a game. You can still price a line yourself below, and the projections table has every player.</div></section>`
    : byGame ? propsByGame(s) : propBoard(s)}
  <div id="sec-trust">${trustCard(s)}</div>
  <section class="card" id="sec-price"><div class="hd"><h2>Price a line from your sportsbook</h2><span class="sub">find where your book is out of step with ${esc(s.prop_book || "the market")}</span></div><div class="bd">${form}</div></section>
  <section class="card" id="sec-proj"><details id="projBox" ${S.proj.open ? "open" : ""}><summary class="hd" style="cursor:pointer"><h2>${SP} projections</h2><span class="sub">average simulated stat line for every player · click to ${S.proj.open ? "hide" : "show"}</span></summary>
    <div class="bd"><div class="filters" style="margin-bottom:8px"><select data-bind="proj.team" data-rerender="1" aria-label="Team"><option value="">All teams</option>${[...new Set(proj.map((r) => r.team))].sort().map((t) => `<option ${S.proj.team === t ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
      <input type="search" placeholder="Search player" data-bind="proj.q" data-rerender="1" value="${esc(S.proj.q)}" aria-label="Search player">
      <span class="muted" style="font-size:12.5px">Averages run higher than a sportsbook's line for yardage, because a few big games pull the average up. Click a player to price their prop.</span></div></div>
    <div class="bd flush">${projTable(S.form.sport, proj)}${outs.length ? `<p class="note" style="padding:8px 16px 0">${outs.map(esc).join("<br>")}</p>` : ""}</div></details></section>`;
}

const PROJ_COLS = {
  nfl: [["player", "Player"], ["team", "Team"], ["pos", "Pos"], ["touch_share", "Touch share", "p"], ["tgt_share", "Target share", "p"], ["car_share", "Carry share", "p"],
    ["pass_yds", "Pass yds"], ["rush_yds", "Rush yds"], ["receptions", "Rec"], ["rec_yds", "Rec yds"], ["p_anytime_td", "Anytime TD", "p"]],
  nba: [["player", "Player"], ["team", "Team"], ["min", "Min"], ["pts", "Pts"], ["reb", "Reb"], ["ast", "Ast"], ["fg3m", "3PM"], ["stl", "Stl"], ["blk", "Blk"], ["pra", "PRA"]],
};

function projTable(sport, rows) {
  const cols = PROJ_COLS[sport] || [];
  let r = rows;
  if (S.proj.team) r = r.filter((x) => x.team === S.proj.team);
  if (S.proj.q) r = r.filter((x) => x.player.toLowerCase().includes(S.proj.q.toLowerCase()));
  const key = S.proj.sort || (sport === "nfl" ? "touch_share" : "pra");
  r = [...r].sort((a, b) => {
    const x = a[key], y = b[key];
    if (typeof x === "string" || typeof y === "string") return S.proj.dir * String(x ?? "").localeCompare(String(y ?? ""));
    return S.proj.dir * ((x ?? -1) - (y ?? -1));
  });
  if (!r.length) return `<div class="empty">No players match.</div>`;
  return `<div class="tw"><table class="t"><thead><tr>${cols.map(([k, l]) => `<th class="sortable ${k === "player" || k === "team" || k === "pos" ? "" : "num"}" data-act="proj-sort" data-col="${k}">${l}${k === key ? (S.proj.dir < 0 ? " ↓" : " ↑") : ""}</th>`).join("")}</tr></thead><tbody>${r
    .slice(0, 150)
    .map((x) => `<tr class="clickable" data-act="prefill" data-player="${esc(x.player)}">${cols
      .map(([k, , f]) => (k === "player" ? `<td><b>${esc(x.player)}</b>${x.status ? ` <span class="flag" title="Listed as questionable or day-to-day">${esc(x.status)}</span>` : ""}</td>` : k === "team" || k === "pos" ? `<td>${esc(x[k])}</td>` : `<td class="num">${f === "p" ? pct(x[k], 0) : x[k] == null ? "–" : num(x[k])}</td>`))
      .join("")}</tr>`)
    .join("")}</tbody></table></div>`;
}

async function priceLines(lines) {
  S.priceErrors = [];
  try {
    const res = await api("/api/props/price", { method: "POST", body: JSON.stringify({ lines }) });
    S.priceErrors = res.errors || [];
    for (const l of res.legs) {
      S.legs[l.key] = l;
      const sp = S.slate.sports.find((x) => x.sport.toUpperCase() === l.sport);
      if (sp) { sp.legs = sp.legs.filter((x) => x.key !== l.key).concat([l]); sp._rows = null; }
    }
    if (res.legs.length) {
      toast(`Priced ${res.legs.length} bet${res.legs.length > 1 ? "s" : ""}. See “Your lines” on the prop board.`);
      S.pv = "list"; S.pb.tab = "mine"; S.pb.game = ""; S.pb.stat = ""; S.pb.q = "";
    }
  } catch (e) {
    S.priceErrors = [String(e.message || e)];
  }
  renderMain();
  if (S.pb.tab === "mine") { const b = $("#sec-board"); if (b) b.scrollIntoView({ block: "start", behavior: "smooth" }); }
}

function parsePaste(text, sport) {
  const s = S.slate.sports.find((x) => x.sport === sport);
  const labels = Object.entries((s && s.markets) || {});
  const lines = [], bad = [];
  for (const raw of text.split(/\r?\n/)) {
    if (!raw.trim()) continue;
    const parts = raw.split(/[,\t]/).map((x) => x.trim());
    const m = labels.find(([k, l]) => k.toLowerCase() === (parts[1] || "").toLowerCase() || l.toLowerCase() === (parts[1] || "").toLowerCase());
    const line = parseFloat(parts[2]);
    if (parts.length < 3 || !m || isNaN(line)) { bad.push(raw); continue; }
    lines.push({ sport, player: parts[0], market: m[0], line, over_odds: parts[3] ? typedOdds(parts[3]) : null, under_odds: parts[4] ? typedOdds(parts[4]) : null, same_book: S.form.from !== "other" });
  }
  return { lines, bad };
}

// ============================================================================ live page
let liveTimer = null;
const watchingLive = () => S.page === "live" || (S.page === "slate" && S.started.length > 0);
function liveLoop() {
  clearTimeout(liveTimer);
  if (!watchingLive()) return;
  const due = !S.live || Date.now() - (S.liveAt || 0) > 5000;
  if (due) loadLive();
  else liveTimer = setTimeout(loadLive, S.live && S.live.in_play ? 30000 : 120000);
}

async function loadLive() {
  clearTimeout(liveTimer);
  try {
    const [liveNow, board] = await Promise.all([api("/api/live"), S.page === "live" ? api("/api/live/board").catch(() => null) : Promise.resolve(S.board)]);
    S.live = liveNow;
    S.board = board;
    S.liveErr = null;
    for (const b of S.live.bets) if (!(b.id in S.liveFirst)) S.liveFirst[b.id] = b.value;
  } catch (e) { S.liveErr = String(e.message || e); }
  S.liveAt = Date.now();
  if (S.page === "slate" && S.started.length) {  // only the started games change: redraw those, leave the rest of the page alone
    for (const g of S.started) { const el = document.getElementById(`st-${g.sport}-${g.game_id}`); if (el) el.outerHTML = startedCard(g); }
    liveTimer = setTimeout(loadLive, S.live && S.live.in_play ? 30000 : 120000);
    return;
  }
  if (S.page !== "live") return;
  // do not redraw under someone typing an offer: refresh the numbers on the next pass instead
  const typing = document.activeElement && document.activeElement.classList.contains("offer");
  if (!typing) { $("#main").innerHTML = pageLive(); labelTables(); }
  for (const b of (S.live ? S.live.bets : [])) if (S.offers[b.id] && b.value > 0 && b.open) checkOffer(b.id, true);
  liveTimer = setTimeout(loadLive, S.live && S.live.in_play ? 30000 : 120000);
}

/* A recorded pick against the sportsbook's number for it now, in words: which way it has gone, then the numbers. */
const SINCE = {
  for: ["▲ In your favour", "You hold a better number than the sportsbook offers now. Picks that beat the later line win more often than their price implied."],
  against: ["▼ Against you", "The sportsbook now offers a better number than the one you took."],
  same: ["= No real change", "The sportsbook's number for this pick is where it was when you bet."],
};
function sinceBet(m, mk) {
  if (!m) return "";
  const [tag, tip] = SINCE[m.verdict] || SINCE.same;
  const ln = (x) => (mk === "spread" ? signed(x, Math.abs(x) % 1 ? 1 : 0) : lineNum(x));
  const span = m.final ? "between your bet and the start of the game" : "since you bet";
  const priced = m.odds_then != null && m.odds_now != null;
  let what;
  if (m.kind === "line" && m.verdict !== "same") {
    const n = Math.abs(m.move), by = `${lineNum(n)}${mk === "spread" || mk === "total" ? ` point${n === 1 ? "" : "s"}` : ""}`;
    what = `line ${ln(m.line_then)} → <b>${ln(m.line_now)}</b> ${span}: ${by} ${m.move > 0 ? "better" : "worse"} than ${m.final ? "the closing number" : "you would get now"}`;
  } else if (m.kind === "line") {
    what = `line still ${ln(m.line_now)} ${span}`;
  } else {
    const price = !priced ? "" : Math.abs(m.odds_then - m.odds_now) < 0.5 ? `price still ${fmtOdds(m.odds_now)}` : `price ${fmtOdds(m.odds_then)} → <b>${fmtOdds(m.odds_now)}</b>`;
    what = [m.line_now != null ? `line still ${ln(m.line_now)}` : "", price].filter(Boolean).join(", ") + ` ${span}`;
    if (m.verdict !== "same") what += `: the book rates this pick ${(Math.abs(m.move) * 100).toFixed(1)} points ${m.move > 0 ? "more" : "less"} likely`;
  }
  const extra = [m.other_book ? `set against ${esc(feedBook())}'s number` : "",
    m.stale ? `last seen ${m.seen ? new Date(m.seen).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "earlier"}, not on the board right now` : ""].filter(Boolean).join(" · ");
  return `<div class="since v-${m.verdict}"><span class="sv" title="${esc(tip)}">${tag}</span><span>${what}${extra ? ` <span class="muted">· ${extra}</span>` : ""}</span></div>`;
}
const feedBook = () => { for (const s of (S.slate ? S.slate.sports : [])) for (const l of s.legs) if (l.book && !["manual", "corrected"].includes(l.book)) return l.book; return "the feed's sportsbook"; };
const tallyText = (t) => (!t ? "" : [t.for ? `${t.for} in your favour` : "", t.against ? `${t.against} against you` : "", t.same ? `${t.same} unchanged` : ""].filter(Boolean).join(" · "));

const LEG_STATUS = {
  won: ["✓", "Won", "s-won"], lost: ["✕", "Lost", "s-lost"], push: ["=", "Push: this pick drops out", "s-void"],
  void: ["–", "Void: this pick drops out", "s-void"], live: ["●", "In play", "s-live"], pre: ["○", "Not started", "s-pre"],
};

function liveLeg(v) {
  const [icon, label, klass] = LEG_STATUS[v.status] || LEG_STATUS.pre;
  const where = v.state === "pre" ? `starts ${when(v.start)}` : `${esc(v.detail)}${v.score ? ` · ${esc(v.score)}` : ""}`;
  const show = v.current != null && v.state !== "pre";
  const cur = !show ? "" : v.market_key === "ml" || v.market_key === "spread" ? esc(v.need)
    : `<b>${lineNum(v.current)}</b>${v.line != null && v.market_key !== "anytime_td" ? ` <span class="muted">of ${lineNum(v.line)}</span>` : ""} · ${esc(v.need)}`;
  const bar = show && v.progress != null && v.market_key !== "anytime_td" ? `<span class="meter wide ${v.side === "under" ? "under" : ""}" aria-hidden="true"><i style="width:${Math.round(v.progress * 100)}%"></i></span>` : "";
  const chance = v.decided ? "" : `<div class="lchance" title="Rough live estimate. Before the game it was ${pct(v.p_pre, 0)}.">${pct(v.p_now, 0)}<span class="muted"> chance now</span>${v.state === "in" ? `<div class="muted">was ${pct(v.p_pre, 0)}</div>` : ""}</div>`;
  return `<li class="lleg ${klass}"><span class="lic" title="${label}" aria-label="${label}">${icon}</span>
    <div class="lmain"><div class="pick">${esc(v.selection)}</div><div class="sub2">${esc(v.game)} · ${where}</div>
      ${cur ? `<div class="lcur">${cur}</div>${bar}` : v.need ? `<div class="lcur">${esc(v.need)}</div>` : ""}
      ${sinceBet(v.move, v.market_key)}</div>
    ${v.decided ? `<span class="rate ${v.status === "won" ? "r-value" : v.status === "lost" ? "r-poor" : "r-fair"}">${label.split(":")[0]}</span>` : chance}</li>`;
}

function cashBox(b) {
  if (!b.open || b.value <= 0) return "";
  const c = S.cash[b.id];
  let res = `<p class="note" style="margin:8px 0 0">Type the cash-out amount your sportsbook is showing for this bet. The tool compares it with what the bet is worth right now.</p>`;
  if (c && c.error) res = `<div class="banner warn" style="margin:8px 0 0">${esc(c.error)}</div>`;
  else if (c) {
    const kind = { take: "good", reasonable: "good", hold: "bad", gone: "bad" }[c.verdict] || "";
    const cut = c.offer_share == null ? null : 1 - c.offer_share;
    res = `<div class="verdict-box ${kind}"><b>${esc(c.text)}</b></div>
      <div class="kvs">
        <div class="kv"><span class="k">Worth right now <span class="muted">(what it pays × chance it wins)</span></span><span class="v">${money(c.fair, 2)}</span></div>
        <div class="kv"><span class="k">The offer</span><span class="v">${money(c.offer, 2)} <span class="muted" style="font-weight:400">${c.offer_share == null ? "" : cut > 0 ? `· the sportsbook keeps ${pct(cut, 0)}` : `· ${pct(-cut, 0)} above its worth`}</span></span></div>
        ${c.risk_value != null ? `<div class="kv" title="The sure amount that is exactly as good as holding this bet, given how large the swing is next to your bankroll. It uses the same risk setting that sizes your stakes."><span class="k">Cash out at or above <span class="muted">(for your ${money(S.bankroll)} bankroll)</span></span><span class="v">${money(c.risk_value, 2)}</span></div>` : `<div class="kv"><span class="k muted">Set your bankroll at the top to allow for risk</span><span class="v"></span></div>`}
        <div class="kv" title="If the true chance of winning is below this, the offer is the better deal"><span class="k">The offer is the better deal only if the chance is below</span><span class="v">${pct(c.breakeven_p, 0)} <span class="muted" style="font-weight:400">· estimate now ${pct(c.p, 0)}</span></span></div>
      </div>`;
  }
  return `<div class="cash"><h3 class="h3">Thinking of cashing out?</h3>
    <label class="cash-in">Cash-out offer on your sportsbook $<input class="offer" type="number" min="0" step="0.01" inputmode="decimal" data-bet="${esc(b.id)}" value="${esc(S.offers[b.id] ?? "")}" placeholder="0.00"></label>
    <div id="cash-${esc(b.id)}">${res}</div></div>`;
}

/* A bet with one pick left, in play: what staking the other side returns. The price comes from the feed when it
   has one and can be typed over, because a live price moves faster than any feed. */
function hedgeFor(b) {
  if (b.error || b.open !== 1 || b.status === "lost") return null;
  const leg = b.legs.find((v) => !v.decided);
  return leg && leg.state === "in" ? leg : null;
}
function hedgeCalc(b) {
  const h = ((S.board && S.board.hedges) || {})[b.id];
  const odds = typedOdds(S.hedgeOdds[b.id] ?? (h ? boxPrice(h.odds) : ""));
  if (isNaN(odds)) return `<p class="note" style="margin:8px 0 0">Type the ${inCents() ? "price in cents your app" : "odds your sportsbook"} shows right now for the other side.</p>`;
  const H = b.payout / decOf(odds), locked = b.payout - b.stake - H;
  const span = (x) => `${lineNum(x.lo)} and ${lineNum(x.hi)}`;
  return `<div class="kvs">
      <div class="kv"><span class="k">Bet this much on the other side</span><span class="v">${money(H, 2)}</span></div>
      <div class="kv" title="One of the two bets wins: this is what you are left with after both stakes"><span class="k">Then you finish with, whichever wins</span><span class="v ${cls(locked)}">${locked >= 0 ? "+" : ""}${money(locked, 2)}</span></div>
      <div class="kv"><span class="k muted">Without a hedge</span><span class="v" style="font-weight:500">+${money(b.profit_if_win, 2)} if it wins · −${money(b.stake, 2)} if it loses</span></div>
      ${h && h.middle ? `<div class="kv" title="The live line is on the other side of yours, so there is a stretch where both bets win"><span class="k">If the ${h.middle.unit === "margin" ? "home team's winning margin" : "final number"} lands between ${span(h.middle)}: both win</span><span class="v pos">+${money(2 * b.payout - b.stake - H, 2)}</span></div>` : ""}
      ${h && h.gap ? `<div class="kv" title="The live line has moved past yours, so there is a stretch where both bets lose"><span class="k">If the ${h.gap.unit === "margin" ? "home team's winning margin" : "final number"} lands between ${span(h.gap)}: both lose</span><span class="v neg">−${money(b.stake + H, 2)}</span></div>` : ""}
    </div>
    ${locked < 0 && !(h && h.middle) ? `<p class="note" style="margin:6px 0 0">At this price a hedge locks in a loss. It is only worth it to cut the risk of losing the whole stake.</p>` : ""}`;
}
function hedgeBox(b) {
  const leg = hedgeFor(b);
  if (!leg) return "";
  const h = ((S.board && S.board.hedges) || {})[b.id];
  return `<div class="cash hedge"><h3 class="h3">Or hedge it yourself</h3>
    <p class="note" style="margin:0 0 8px">One pick is left. Betting the other side now, <b>${h ? esc(h.selection) : `against ${esc(leg.selection)}`}</b>, locks in the same result whichever way it goes.
      ${h ? `The feed shows ${fmtOdds(h.odds)} for it (${esc(h.book)}). Live prices move every few seconds, so` : "The feed has no live price for it, so"} type the odds on your sportsbook.
      This is often a better deal than the cash-out button.</p>
    <label class="cash-in">Live ${inCents() ? "price for the other side, in cents" : "odds for the other side"}<input class="offer" type="number" step="${inCents() ? "0.1" : "1"}" inputmode="decimal" data-hedge="${esc(b.id)}" value="${esc(S.hedgeOdds[b.id] ?? (h ? boxPrice(h.odds) : ""))}" placeholder="${inCents() ? "52" : "-110"}"></label>
    <div id="hedge-${esc(b.id)}">${hedgeCalc(b)}</div></div>`;
}

function liveBet(b) {
  if (b.error) return `<section class="card"><div class="hd"><h2>${esc(b.label)}</h2></div><div class="bd"><div class="banner warn">This bet could not be read: ${esc(b.error)}</div></div></section>`;
  const pill = { won: ["Won", "r-value"], lost: ["Lost", "r-poor"], push: ["Push: stake returned", "r-fair"], live: [`${b.won} of ${b.n} won · ${b.open} to go`, "r-lean"], pre: ["Not started", "r-fair"] }[b.status] || ["", "r-fair"];
  const first = S.liveFirst[b.id];
  const moved = b.open && first != null && Math.abs(b.value - first) >= 0.01 ? `<div class="delta ${cls(b.value - first)}">${b.value > first ? "up" : "down"} from ${money(first, 2)} when you opened this page</div>` : `<div class="delta">${b.open ? `${pct(b.p_all, b.p_all < 0.1 ? 1 : 0)} chance everything left wins` : b.status === "won" ? "paid in full" : ""}</div>`;
  return `<section class="card live-bet"><div class="hd"><h2>${b.n === 1 ? "Single bet" : `${b.n}-pick parlay`}</h2>
      <span class="sub">${money(b.stake, 2)} at ${fmtOdds(b.odds)}${b.placed_at ? ` · recorded ${new Date(b.placed_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}</span>
      <div class="right"><span class="rate ${pill[1]}">${pill[0]}</span></div></div>
    <div class="bd">
      <div class="tiles live-tiles">
        <div class="tile"><div class="label">Worth right now</div><div class="value ${b.status === "lost" ? "neg" : ""}">${money(b.value, 2)}</div>${moved}</div>
        <div class="tile"><div class="label">Pays if it all wins</div><div class="value">${money(b.payout, 2)}</div><div class="delta">${money(b.profit_if_win, 2)} profit on ${money(b.stake, 2)}</div></div>
        <div class="tile"><div class="label">Picks</div><div class="value">${b.won}<span class="muted" style="font-size:15px"> of ${b.n} won</span></div><div class="delta">${b.open} still to settle${b.lost ? ` · ${b.lost} lost` : ""}</div></div>
      </div>
      <ol class="live-legs">${b.legs.map(liveLeg).join("")}</ol>
      ${b.open && b.p_all > 0 && b.open > 1 ? `<p class="note">The chance everything left wins (${pct(b.p_all, b.p_all < 0.1 ? 1 : 0)}) multiplies the chances above${b.same_game ? ". Some remaining picks are in the same game; they are treated as unrelated here, which can understate or overstate the true chance" : ""}.</p>` : ""}
      ${cashBox(b)}
      ${hedgeBox(b)}
    </div></section>`;
}

/* One game on the scoreboard: both teams with their score, the clock, and the live win chance while it is on. */
function scoreCard(g) {
  const live = g.state === "in", done = g.state === "post";
  const hs = g.home_score, as = g.away_score, lead = hs === as ? "" : hs > as ? "home" : "away";
  const state = live ? `<span class="sc-live"><span class="dot"></span>Live</span> ${esc(g.detail)}` : done ? esc(g.detail || "Final") : when(g.start);
  const row = (side, abbr, pts) => `<div class="sc-row ${done && lead && lead !== side ? "lost" : ""}">${teamBadge(g.sport, abbr, true)}<span class="sc-team">${esc(abbr)}</span>
    <b class="sc-pts">${g.state === "pre" ? "" : Math.round(pts)}</b></div>`;
  const wp = live && g.home_wp != null ? `<div class="wp" title="ESPN's own live estimate, not this tool's"><span>${esc(g.away)} ${pct(1 - g.home_wp, 0)}</span>
      <span class="bar"><span style="width:${(1 - g.home_wp) * 100}%;background:var(--s2)"></span><span style="width:${g.home_wp * 100}%;background:var(--s1)"></span></span><span>${pct(g.home_wp, 0)} ${esc(g.home)}</span></div>
      <div class="sc-note">win chance right now (ESPN's estimate)</div>` : "";
  const late = S.started.find((x) => x.sport === g.sport && x.home === g.home && x.away === g.away);
  const lb = live && S.board ? (S.board.games || []).find((x) => x.event_id === g.event_id) : null;
  const L = lb && lb.line ? lb.line : null;
  const pre = (mk, side) => { const r = lb && (lb.lines || []).find((x) => x.market_key === mk && x.side === side); return r && r.pre_line != null ? r.pre_line : null; };
  const fav = L && L.spread_home != null ? (L.spread_home < 0 ? `${esc(g.home)} ${signed(L.spread_home, Math.abs(L.spread_home) % 1 ? 1 : 0)}` : L.spread_home > 0 ? `${esc(g.away)} ${signed(-L.spread_home, Math.abs(L.spread_home) % 1 ? 1 : 0)}` : "even") : null;
  const prices = !L ? "" : `<div class="sc-live-px" title="The sportsbook's in-game prices from the feed. They can lag: check your sportsbook before betting.">
      <span class="mini-lab" style="margin:0">Live prices${lb.book ? ` · ${esc(String(lb.book).replace(" - Live Odds", ""))}` : ""}</span>
      ${fav ? `<span>Spread <b>${fav}</b>${pre("spread", "home") != null ? ` <span class="muted">was ${signed(pre("spread", "home"), Math.abs(pre("spread", "home")) % 1 ? 1 : 0)} ${esc(g.home)}</span>` : ""}</span>` : ""}
      ${L.total != null ? `<span>Total <b>${lineNum(L.total)}</b>${pre("total", "over") != null ? ` <span class="muted">was ${lineNum(pre("total", "over"))}</span>` : ""}</span>` : ""}
      ${L.ml_home != null && L.ml_away != null ? `<span>To win <b>${esc(g.away)} ${fmtOdds(L.ml_away)}</b> · <b>${esc(g.home)} ${fmtOdds(L.ml_home)}</b></span>` : ""}</div>`;
  return `<article class="sc ${live ? "live" : ""}"><div class="sc-top"><span>${SPORT_ICON[g.sport] || ""} ${state}</span></div>
    ${row("away", g.away, as)}${row("home", g.home, hs)}${wp}${prices}
    ${g.players && g.players.length ? `<details class="sc-box" data-box="${esc(g.event_id)}" ${S.boxOpen[g.event_id] ? "open" : ""}><summary>Player stats${live ? " so far" : ""}</summary>${boxTable(g)}</details>` : ""}
    ${late && !done ? `<button class="linkish sc-rec" data-act="go-started" data-to="st-${late.sport}-${esc(late.game_id)}">Record a bet on this game</button>` : ""}</article>`;
}
/* Every player's line so far for one game, a team at a time. */
function boxTable(g) {
  const cols = g.sport === "nba" ? [["min", "MIN"], ["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["fg3m", "3PM"], ["stl", "STL"], ["blk", "BLK"]]
    : [["pass_yds", "PASS"], ["rush_yds", "RUSH"], ["rec_yds", "REC YD"], ["receptions", "REC"], ["anytime_td", "TD"]];
  const weight = g.sport === "nba" ? (x) => x.pts || 0 : (x) => (x.pass_yds || 0) + (x.rush_yds || 0) + (x.rec_yds || 0);
  const team = (t) => {
    const ps = g.players.filter((x) => x.team === t).sort((a, b) => weight(b) - weight(a) || a.name.localeCompare(b.name));
    if (!ps.length) return "";
    return `<div class="bx-team">${teamBadge(g.sport, t, true)}<span>${esc(t)}</span></div>
      <div class="bx-row bx-hd"><span></span>${cols.map((c) => `<span>${c[1]}</span>`).join("")}</div>
      ${ps.map((x) => `<div class="bx-row"><span class="bx-name">${esc(x.name)}</span>${cols.map((c) => `<span>${x[c[0]] == null ? "–" : lineNum(x[c[0]])}</span>`).join("")}</div>`).join("")}`;
  };
  return `<div class="bx n${cols.length}">${team(g.away)}${team(g.home)}</div>`;
}

function scoreboard() {
  const games = (S.live.scores || []).filter((g) => S.sport === "all" || g.sport === S.sport);
  if (!games.length) return "";
  const n = games.filter((g) => g.state === "in").length;
  return `<section class="card"><div class="hd"><h2>Today's scoreboard</h2><span class="sub">${n ? `${n} game${n === 1 ? "" : "s"} in play` : "nothing in play right now"} · ${games.length} today</span></div>
    <div class="bd"><div class="scores">${games.map(scoreCard).join("")}</div>
    ${n ? `<p class="note" style="margin:10px 0 0"><b>About betting live.</b> The live prices shown are the sportsbook's own, from the feed, and can lag. The tool does not suggest new live bets:
      its live estimate only knows the score, the clock and the box score, and when it was set against real live prices it disagreed with a third of them by a wide margin, which means the estimate is wrong, not the price.
      It is logging live prices against results so that can be tested. What it can work out exactly is a <b>hedge</b>: it appears on any recorded bet with one pick left.</p>` : ""}</div></section>`;
}

/* Everything that depends on each game: a parlay counts in full for every game it still needs, because any
   one of them can lose it. */
function exposure(open) {
  const games = new Map();
  for (const b of open) {
    const left = b.legs.filter((v) => !v.decided);
    for (const name of new Set(left.map((v) => v.game))) {
      if (!games.has(name)) games.set(name, { game: name, stake: 0, pays: 0, bets: 0, picks: [], state: "pre", detail: "", score: "", start: null });
      const g = games.get(name), mine = left.filter((v) => v.game === name);
      g.stake += b.stake; g.pays += b.payout; g.bets += 1;
      for (const v of mine) { g.picks.push(v.selection); if (v.state === "in") { g.state = "in"; g.detail = v.detail; g.score = v.score; } g.start = g.start || v.start; }
    }
  }
  const list = [...games.values()].sort((a, b) => b.stake - a.stake);
  if (list.length < 2 && open.length < 2) return "";
  const total = open.reduce((n, b) => n + b.stake, 0);
  return `<section class="card"><div class="hd"><h2>What is riding on each game</h2><span class="sub">${money(total, 2)} staked across ${open.length} open bet${open.length === 1 ? "" : "s"}</span></div>
    <div class="bd"><div class="expo">${list.map((g) => `<div class="ex ${g.state === "in" ? "live" : ""}">
        <div class="ex-hd"><b>${esc(g.game)}</b><span class="muted">${g.state === "in" ? `<span class="sc-live"><span class="dot"></span>Live</span> ${esc(g.detail)}` : g.start ? `starts ${when(g.start)}` : ""}</span></div>
        <div class="ex-amt" title="The stakes of every open bet that still needs this game">${money(g.stake, 2)}<small>${total > 0 ? ` · ${pct(g.stake / total, 0)} of what you have staked` : ""}</small></div>
        <div class="ex-bar" aria-hidden="true"><i style="width:${total > 0 ? Math.round((100 * g.stake) / total) : 0}%"></i></div>
        <div class="muted ex-picks">${g.bets} bet${g.bets === 1 ? "" : "s"}: ${[...new Set(g.picks)].map(esc).join(", ")}</div></div>`).join("")}</div>
      <p class="note" style="margin:10px 0 0">A parlay counts in full for every game it still needs, since any one of them can lose it. So these can add up to more than you have staked.</p></div></section>`;
}

function pageLive() {
  const head = pageHead("🔴 Live", "Your recorded bets as they stand right now, pick by pick, with real scores and stat lines, plus today's scoreboard with every player's stats. It checks for new numbers every 30 seconds while a game is on.");
  if (!S.live && !S.liveErr) return `${head}<div class="empty"><span class="spinner"></span>Checking the games ...</div>`;
  if (!S.live) return `${head}<div class="banner warn">Could not load live data: ${esc(S.liveErr)}</div>`;
  const bets = S.live.bets;
  const bar = `<div class="filters"><span class="muted" style="font-size:12.5px">${S.liveErr ? `<span class="neg">Last check failed: ${esc(S.liveErr)}.</span> ` : ""}Last checked ${new Date(S.liveAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })} · ${S.live.in_play ? "games in play" : "no games in play"}</span>
    <span class="spacer"></span><button class="btn" data-act="live-refresh">Check now</button></div>`;
  if (!bets.length) {
    return `${head}${bar}<section class="card"><div class="empty"><b>No open bets to follow.</b> After you place a bet at your sportsbook, add the same picks to the bet slip here, enter the odds and your stake, and click <i>Record bet</i>.
      It then shows up on this page with live scores, and you can check any cash-out offer against it.
      ${S.started.length ? `<br><br><b>Already bet on a game that has started?</b> <button class="linkish" data-act="go-started" data-to="sec-started">Record it from the Started games list</button>.` : ""}</div></section>${scoreboard()}`;
  }
  const open = bets.filter((b) => b.open), done = bets.filter((b) => !b.open);
  const atStake = open.reduce((n, b) => n + b.stake, 0), worth = open.reduce((n, b) => n + b.value, 0);
  return `${head}${bar}
    ${open.length ? `<div class="tiles"><div class="tile"><div class="label">Open bets</div><div class="value">${open.length}</div><div class="delta">${money(atStake, 2)} staked</div></div>
      <div class="tile"><div class="label">Worth right now</div><div class="value">${money(worth, 2)}</div><div class="delta">all open bets, at the tool's live estimates</div></div></div>` : ""}
    ${exposure(open)}
    <div class="banner"><span aria-hidden="true">ⓘ</span><span>Scores and stats are real. The <b>chance now</b> figures are rough estimates from the score, the clock and each player's pace: they do not know about foul trouble, injuries during the game or who has the ball. Use them as a guide, and check the final result on your sportsbook.</span></div>
    ${open.map(liveBet).join("")}
    ${done.length ? `<h2 class="sec-title">Finished in the last day</h2>${done.map(liveBet).join("")}` : ""}
    ${scoreboard()}`;
}

async function checkOffer(betId, quiet = false) {
  const offer = parseFloat(S.offers[betId]);
  const box = document.getElementById(`cash-${betId}`);
  if (isNaN(offer)) { delete S.cash[betId]; if (!quiet && S.page === "live") renderMain(); return; }
  try {
    S.cash[betId] = await api("/api/live/cashout", { method: "POST", body: JSON.stringify({ bet_id: betId, offer, bankroll: S.bankroll || null }) });
  } catch (e) { S.cash[betId] = { error: String(e.message || e) }; }
  if (S.page !== "live") return;
  const b = S.live && S.live.bets.find((x) => x.id === betId);
  if (box && b) { const t = document.createElement("div"); t.innerHTML = cashBox(b); box.innerHTML = $(`#cash-${CSS.escape(betId)}`, t).innerHTML; }
}

// ============================================================================ line moves page
let oddsTimer = null;
function oddsLoop() {
  clearTimeout(oddsTimer);
  if (S.page !== "moves") return;
  if (!S.odds || Date.now() - S.oddsAt > 60000) loadOdds();
  else oddsTimer = setTimeout(loadOdds, 60000);
}
async function loadOdds() {
  clearTimeout(oddsTimer);
  try { S.odds = await api("/api/odds"); S.oddsErr = null; } catch (e) { S.oddsErr = String(e.message || e); }
  S.oddsAt = Date.now();
  if (S.page === "moves") { $("#main").innerHTML = pageMoves(); afterRender(); oddsTimer = setTimeout(loadOdds, 60000); }
}
const moveSize = (g) => Math.abs(g.move.total || 0) + Math.abs(g.move.spread_home || 0) + Math.abs(g.move.p_home || 0) * 25;

function moveCard(g, on) {
  const m = g.move, o = g.open || {}, n = g.now || {};
  const row = (label, a, b, d, fmt, unit) => `<div class="mc-row"><span class="k">${label}</span><span>${a == null ? "–" : fmt(a)} → <b>${b == null ? "–" : fmt(b)}</b></span>
    ${d == null || Math.abs(d) < 1e-9 ? '<span class="same">no change</span>' : `<span class="d">${unit(d)}</span>`}</div>`;
  const arrow = (fmt) => (d) => `${d > 0 ? "▲" : "▼"} ${fmt(Math.abs(d))}`;
  // a home spread that goes up means the home team is getting more points: the line moved toward the away team
  const toward = (d) => `toward ${esc(d > 0 ? g.away : g.home)}`;
  return `<button class="mcard ${on ? "on" : ""}" data-act="moves-game" data-id="${g.sport}|${esc(g.game_id)}" aria-pressed="${on}">
    <div class="mc-hd">${teamBadge(g.sport, g.away, true)}<span>${esc(g.away)} @ ${esc(g.home)}</span>${teamBadge(g.sport, g.home, true)}<span class="time">${when(g.start)}</span></div>
    <div class="mc-rows">
      ${row("Total", o.total, n.total, m.total, lineNum, arrow(lineNum))}
      ${row(`${esc(g.home)} spread`, o.spread_home, n.spread_home, m.spread_home, (x) => signed(x), toward)}
      ${row(`${esc(g.home)} to win`, m.p_home_open, m.p_home_now, m.p_home, (x) => pct(x, 0), arrow((x) => `${(x * 100).toFixed(0)} pts`))}
    </div></button>`;
}

/* Your recorded bets on the Line moves page: each pick with the way its line has gone since you placed it. */
function myBetsMoves(bets) {
  const head = `<div class="hd"><h2>Your bets and the line</h2><span class="sub">has the sportsbook's number moved your way since you bet?</span></div>`;
  if (!bets.length) {
    return `<section class="card">${head}<div class="empty"><b>No open bets recorded.</b> Record a bet from the bet slip and this shows, pick by pick, whether the line has moved in your favour or against you since you placed it.
      Beating the later line is the earliest sign a pick was a good one, long before the game is played.</div></section>`;
  }
  const leg = (l) => `<li><div class="pick">${esc(l.selection)}</div>
      <div class="sub2">${esc(l.game)} · ${l.started ? "started" : `starts ${when(l.start)}`}${["ml", "spread", "total"].includes(l.market_key) && !l.started ? ` · <button class="linkish" data-act="moves-game" data-id="${String(l.sport).toLowerCase()}|${esc(l.game_id)}">chart</button>` : ""}</div>
      ${sinceBet(l.move, l.market_key) || `<div class="since v-none"><span class="sv">No comparison yet</span><span>${l.started ? "this tool had no number for it before the game started" : "the sportsbook's number has not been read again since you bet"}</span></div>`}</li>`;
  return `<section class="card" id="sec-mybets">${head}<div class="bd"><div class="mybets">${bets.map((b) => `<article class="mybet">
      <div class="mb-hd"><b>${b.n === 1 ? "Single bet" : `${b.n}-pick parlay`}</b><span class="muted">${money(b.stake, 2)} at ${fmtOdds(b.odds)}${b.placed_at ? ` · recorded ${new Date(b.placed_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}</span></div>
      ${tallyText(b.tally) ? `<div class="mb-tally">${tallyText(b.tally)}</div>` : ""}
      <ul class="mb-legs">${b.legs.map(leg).join("")}</ul></article>`).join("")}</div>
    <p class="note" style="margin:10px 0 0">Each pick is compared with the number the sportsbook's feed shows for it now. Once a game starts, the comparison stays at the last number seen before it.</p></div></section>`;
}

function pageMoves() {
  const head = pageHead("📈 Line moves", "How each game's numbers have moved since the sportsbook first posted them. A line that moves toward a pick after you take it is the earliest sign the pick was a good one. Opening numbers can be posted long before a game, so big moves since then are common.");
  if (!S.odds && !S.oddsErr) return `${head}<div class="empty"><span class="spinner"></span>Reading the lines ...</div>`;
  if (!S.odds) return `${head}<div class="banner warn">Could not load line moves: ${esc(S.oddsErr)}</div>`;
  const O = S.odds;
  const games = O.games.filter((g) => S.sport === "all" || g.sport === S.sport).sort((a, b) => moveSize(b) - moveSize(a));
  if (!games.length) return `${head}<div class="filters">${sportSeg()}</div><section class="card"><div class="empty">No upcoming games to follow.</div></section>`;
  const moved = games.filter((g) => moveSize(g) > 1e-9);
  const bigT = [...games].sort((a, b) => Math.abs(b.move.total || 0) - Math.abs(a.move.total || 0))[0];
  const id = (g) => `${g.sport}|${g.game_id}`;
  if (!S.movesGame || !games.some((g) => id(g) === S.movesGame)) S.movesGame = id(games[0]);
  const sel = games.find((g) => id(g) === S.movesGame);
  const props = O.props.filter((p) => S.sport === "all" || p.sport === S.sport).slice(0, 60);
  const nPropMoves = S.sport === "all" ? O.prop_moves : (O.prop_moves_by_sport || {})[S.sport] || 0;
  const checked = O.checked_at ? new Date(O.checked_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "not yet";
  const myBets = (O.bets || []).map((b) => ({ ...b, legs: b.legs.filter((l) => S.sport === "all" || String(l.sport).toLowerCase() === S.sport) })).filter((b) => b.legs.length);
  const stale = O.stale_games ? `<div class="banner warn"><span>The sportsbook's feed has moved on ${O.stale_games} game${O.stale_games === 1 ? "" : "s"} since the picks were last worked out. They update by themselves within ${O.auto_update_minutes} minutes, or do it now.</span><button class="btn small" data-act="refresh">Update now</button></div>` : "";
  const m = sel.move, o = sel.open || {}, n = sel.now || {};
  const box = (key, title, now, sub) => `<div class="cbox"><h3>${title}</h3><div class="cnow">${now}</div><div class="csub">${sub}</div><div class="chart" data-step="${key}"></div></div>`;
  const moveTxt = (opened, d, unit) => (d == null ? "no opening number in the feed" : Math.abs(d) < 1e-9 ? `opened ${opened}, unchanged` : `opened ${opened} · ${d > 0 ? "up" : "down"} ${unit(Math.abs(d))} since`);
  const spreadTxt = m.spread_home == null ? "no opening number in the feed" : Math.abs(m.spread_home) < 1e-9 ? `opened ${signed(o.spread_home)}, unchanged`
    : `opened ${signed(o.spread_home)} · moved ${lineNum(Math.abs(m.spread_home))} toward ${esc(m.spread_home > 0 ? sel.away : sel.home)}`;
  const first = sel.series.length ? new Date(sel.series[0].t) : null;
  const watching = first ? `This tool has watched this game since ${first.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })} and has logged ${sel.series.length} set${sel.series.length === 1 ? "" : "s"} of numbers.${sel.series.length < 2 ? " The line steps each time the sportsbook changes a number, so it fills in from here." : ""}` : "";
  return `${head}
  <div class="filters">${sportSeg()}<span class="spacer"></span><span class="muted" style="font-size:12.5px">Feed checked ${checked} · every ${O.check_minutes} min</span><button class="btn small" data-act="odds-refresh">Check now</button></div>
  ${stale}
  <div class="tiles">
    <div class="tile"><div class="label">Games followed</div><div class="value">${games.length}</div><div class="delta">${O.log_rows.toLocaleString()} changes logged${O.log_since ? ` since ${new Date(O.log_since).toLocaleDateString([], { month: "short", day: "numeric" })}` : ""}</div></div>
    <div class="tile"><div class="label">Moved since opening</div><div class="value">${moved.length}<span class="muted" style="font-size:15px"> of ${games.length}</span></div><div class="delta">spread, total or moneyline</div></div>
    <div class="tile"><div class="label">Biggest total move</div><div class="value">${bigT && bigT.move.total ? `${bigT.move.total > 0 ? "▲" : "▼"} ${lineNum(Math.abs(bigT.move.total))}` : "–"}</div><div class="delta">${bigT && bigT.move.total ? `${esc(bigT.away)} @ ${esc(bigT.home)}: ${lineNum(bigT.open.total)} → ${lineNum(bigT.now.total)}` : "nothing has moved"}</div></div>
    <div class="tile"><div class="label">Prop lines moved</div><div class="value">${nPropMoves.toLocaleString()}</div><div class="delta">since they opened</div></div>
  </div>
  ${myBets.length ? myBetsMoves(myBets) : ""}
  <section class="card" id="sec-chart"><div class="hd">${teamBadge(sel.sport, sel.away, true)}<h2>${esc(sel.away)} @ ${esc(sel.home)}</h2>${teamBadge(sel.sport, sel.home, true)}<span class="sub">${when(sel.start)} · pick another game below</span></div>
    <div class="bd"><div class="cgrid">
      ${box("total", "Total points (over / under)", lineNum(n.total), moveTxt(lineNum(o.total), m.total, lineNum))}
      ${box("spread_home", `${esc(sel.home)} spread`, n.spread_home == null ? "–" : signed(n.spread_home), spreadTxt)}
      ${box("p_home", `${esc(sel.home)} chance to win`, pct(m.p_home_now, 0), moveTxt(pct(m.p_home_open, 0), m.p_home, (x) => `${(x * 100).toFixed(0)} points`))}
    </div>
    <p class="note" style="margin:10px 0 0">Solid line: each number the sportsbook has shown while this tool was watching. Dashed line: the opening number. ${watching}
      ${sel.model_total != null ? `The model's own numbers for this game: total ${num(sel.model_total)}, ${favLine(sel.home, sel.away, sel.model_margin)}.` : ""}</p></div></section>
  <section class="card"><div class="hd"><h2>Every game, biggest movers first</h2><span class="sub">opening number → now · tap a game to chart it</span></div>
    <div class="bd"><div class="mcards">${games.map((g) => moveCard(g, id(g) === S.movesGame)).join("")}</div></div></section>
  <section class="card"><div class="hd"><h2>Player prop lines that moved</h2><span class="sub">${props.length ? (props.length < nPropMoves ? `the ${props.length} biggest of ${nPropMoves.toLocaleString()}` : `${props.length} line${props.length === 1 ? "" : "s"}`) : "none yet"}</span></div>
    <div class="bd">${props.length ? `<div class="pm-list">${props.map((p) => `<div class="pm"><div class="pick">${esc(p.player)}</div>
        <div class="mvn">${p.move > 0 ? "▲" : "▼"} ${lineNum(Math.abs(p.move))}<small>${lineNum(p.open_line)} → ${lineNum(p.line)}</small></div>
        <div class="sub2">${esc(p.market)} · ${esc(p.game)}</div></div>`).join("")}</div>` : '<div class="empty" style="padding-left:0">No prop line has moved since it opened.</div>'}</div></section>
  ${myBets.length ? "" : myBetsMoves([])}`;
}

/* A line's history as a stepped line (a line holds until it changes), with the opening number dashed. */
function stepChart(el, pts, opened, fmt, name, { minPad = 0.5, clamp = null } = {}) {
  const W = el.clientWidth || 320, H = 170, m = { l: 44, r: 14, t: 12, b: 24 };
  pts = pts.filter((p) => p.v != null);
  if (!pts.length) { el.innerHTML = '<div class="empty" style="padding:12px 0">No changes logged yet for this number.</div>'; return; }
  const now = Date.now(), t0 = Math.min(pts[0].t, now - 60000), t1 = Math.max(now, pts[pts.length - 1].t);
  const vals = pts.map((p) => p.v).concat(opened != null ? [opened] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max((hi - lo) * 0.25, minPad);
  let a = lo - pad, b = hi + pad;
  if (clamp) { a = Math.max(a, clamp[0]); b = Math.min(b, clamp[1]); }
  const ticks = niceTicks(a, b, 4), step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  while (ticks[0] > lo + 1e-9) ticks.unshift(+(ticks[0] - step).toFixed(10));
  while (ticks[ticks.length - 1] < hi - 1e-9) ticks.push(+(ticks[ticks.length - 1] + step).toFixed(10));
  lo = ticks[0]; hi = ticks[ticks.length - 1];
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  let d = `M${X(pts[0].t)},${Y(pts[0].v)}`;
  for (let i = 1; i < pts.length; i++) d += ` H${X(pts[i].t)} V${Y(pts[i].v)}`;
  d += ` H${X(t1)}`;
  const tl = (t) => new Date(t).toLocaleString([], t1 - t0 > 86400000 ? { weekday: "short", hour: "numeric" } : { hour: "numeric", minute: "2-digit" });
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(name)} over time">`;
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"></line>`).join("")}</g>`;
  svg += ticks.map((v) => `<text class="tick" x="${m.l - 7}" y="${Y(v) + 4}" text-anchor="end">${fmt(v)}</text>`).join("");
  svg += `<text class="tick" x="${m.l}" y="${H - 6}">${tl(t0)}</text><text class="tick" x="${W - m.r}" y="${H - 6}" text-anchor="end">now</text>`;
  if (opened != null) svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(opened)}" y2="${Y(opened)}" stroke="var(--ink-2)" stroke-width="1" stroke-dasharray="4 4"></line>
    <text class="tick" x="${W - m.r}" y="${Y(opened) - 5}" text-anchor="end">opened ${fmt(opened)}</text>`;
  svg += `<path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"></path>`;
  svg += pts.map((p) => `<circle cx="${X(p.t)}" cy="${Y(p.v)}" r="4" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"></circle>`).join("");
  svg += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--axis)" stroke-width="1" style="display:none"></line>`;
  svg += `<rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"></rect></svg>`;
  el.innerHTML = svg;
  const tip = tooltip(el), xh = $(".xh", el);
  $(".hit", el).addEventListener("pointermove", (ev) => {
    const r = el.getBoundingClientRect(), t = t0 + (((ev.clientX - r.left) / r.width) * W - m.l) / (W - m.l - m.r) * (t1 - t0);
    let cur = pts[0];
    for (const p of pts) if (p.t <= t) cur = p;
    const px = Math.max(X(cur.t), Math.min(((ev.clientX - r.left) / r.width) * W, W - m.r));
    xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.style.display = "";
    tip.innerHTML = `<div class="t">${new Date(Math.max(t, pts[0].t)).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</div>
      <div class="r"><span class="k" style="background:var(--s1)"></span><b>${fmt(cur.v)}</b><span class="muted">${esc(name)}</span></div>
      ${opened != null ? `<div class="r"><b>${fmt(opened)}</b><span class="muted">at the open</span></div>` : ""}`;
    placeTip(el, tip, (px / W) * r.width, (Y(cur.v) / H) * r.height);
  });
  $(".hit", el).addEventListener("pointerleave", () => { tip.style.display = "none"; xh.style.display = "none"; });
}

function drawMoves() {
  const sel = S.odds && S.odds.games.find((g) => `${g.sport}|${g.game_id}` === S.movesGame);
  if (!sel) return;
  const o = sel.open || {}, n = sel.now || {};
  // what the tool is using now is the last point, in case the log has not caught the latest change yet
  const cur = { total: n.total, spread_home: n.spread_home, p_home: sel.move.p_home_now };
  const opened = { total: o.total, spread_home: o.spread_home, p_home: sel.move.p_home_open };
  const fmts = { total: lineNum, spread_home: (v) => signed(v, Math.abs(v) % 1 ? 1 : 0), p_home: (v) => `${Math.round(v * 100)}%` };
  const names = { total: "total", spread_home: `${sel.home} spread`, p_home: `${sel.home} to win` };
  for (const el of $$("[data-step]")) {
    const k = el.dataset.step;
    let pts = sel.series.map((p) => ({ t: Date.parse(p.t), v: p[k] })).filter((p) => p.v != null);
    const dedup = [];
    for (const p of pts) if (!dedup.length || Math.abs(dedup[dedup.length - 1].v - p.v) > 1e-9) dedup.push(p);
    pts = dedup;
    if (cur[k] != null && (!pts.length || Math.abs(pts[pts.length - 1].v - cur[k]) > 1e-9)) pts.push({ t: Date.now() - 1000, v: cur[k] });
    stepChart(el, pts, opened[k], fmts[k], names[k], k === "p_home" ? { minPad: 0.04, clamp: [0, 1] } : {});
  }
}

// ============================================================================ results page
function pageTrack() {
  const title = pageHead("🏆 Results", "How the tool's picks and your own recorded bets have actually done. Every priced pick is logged automatically and settled after the game.");
  if (!S.track) { loadTrack(); return `${title}<div class="filters">${sportSeg()}</div><div class="empty"><span class="spinner"></span>Loading results ...</div>`; }
  const T = S.track;
  if (T.error) return `${title}<div class="banner warn">${esc(T.error)}</div>`;
  const ranges = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [0, "All time"]];
  const g = (name) => T.groups.find((x) => x.name === name);
  const vb = g("Value bets"), gl = g("All game lines");
  const clvTot = gl ? gl.clv_beat + gl.clv_same + gl.clv_worse : 0;
  const PC = T.prop_clv || { n: 0, beat: 0, same: 0, line_moved: 0, line_moved_for: 0, no_close: 0 };
  const clvAll = clvTot + PC.n, clvBeat = (gl ? gl.clv_beat : 0) + PC.beat;
  const B = T.bets;
  const head = `${title}<div class="filters">${sportSeg()}<div class="seg" role="group" aria-label="Date range">${ranges
    .map(([d, l]) => `<button data-act="days" data-days="${d}" class="${(S.trackDays || 0) === d ? "on" : ""}">${l}</button>`)
    .join("")}</div><span class="spacer"></span><button class="btn" data-act="grade" title="Look up final scores and box scores and settle every logged pick whose game has finished">Settle finished games</button></div>`;
  const tiles = `<div class="tiles">
    <div class="tile"><div class="label">Picks settled</div><div class="value">${T.settled.toLocaleString()}</div><div class="delta">${T.open.toLocaleString()} waiting for their games · ${T.void} void</div></div>
    <div class="tile"><div class="label">Value bets: return</div><div class="value ${vb ? cls(vb.roi) : ""}">${vb ? spct(vb.roi) : "–"}</div><div class="delta">${vb ? `${vb.w} won · ${vb.l} lost · ${vb.p} push · ${signed(vb.units)} units` : "none settled yet"}</div></div>
    <div class="tile" title="Whether the odds you were shown were better than the final odds before the game. Beating the closing line consistently is the earliest reliable sign of a real edge."><div class="label">Beat the closing line</div><div class="value">${clvAll ? pct(clvBeat / clvAll, 0) : "–"}</div><div class="delta">${clvAll ? `${clvTot ? `games ${gl.clv_beat} of ${clvTot}` : ""}${clvTot && PC.n ? " · " : ""}${PC.n ? `props ${PC.beat} of ${PC.n}` : ""}` : "once picks are settled"}</div></div>
    <div class="tile"><div class="label">My bets: profit</div><div class="value ${cls(B.pnl)}">${B.settled ? money(B.pnl) : "–"}</div><div class="delta">${B.settled ? `${spct(B.roi)} on ${money(B.staked)} staked` : `${B.open} open`}</div></div></div>`;
  if (!T.settled) {
    return head + tiles + `<section class="card"><div class="empty"><b>Nothing has been settled yet.</b> Each time the odds update, every priced pick is logged (${T.open.toLocaleString()} are waiting for their games).
      After the games finish, click <i>Settle finished games</i>, or just refresh: it also runs automatically. To track real money, build a bet in the bet slip and click <i>Record bet</i>.</div></section>` + myBetsCard(B);
  }
  const groupsRows = T.groups.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td class="num">${x.n}</td><td class="num">${x.w}-${x.l}-${x.p}</td><td class="num">${pct(x.win)}</td><td class="num">${pct(x.model)}</td><td class="num">${pct(x.market)}</td>
      <td class="num ${cls(x.units)}">${signed(x.units)}</td><td class="num ${cls(x.roi)}">${spct(x.roi)}</td><td class="num muted">${x.roi_lo == null ? "–" : `${spct(x.roi_lo, 0)} to ${spct(x.roi_hi, 0)}`}</td></tr>`).join("");
  const marketRows = T.by_market.map((x) => `<tr><td>${esc(x.sport)} ${esc(x.label)}</td><td class="num">${x.n}</td><td class="num">${x.w}-${x.l}-${x.p}</td><td class="num">${pct(x.win)}</td><td class="num">${pct(x.model)}</td>
      <td class="num ${cls(x.units)}">${signed(x.units)}</td><td class="num ${cls(x.roi)}">${spct(x.roi)}</td><td class="num">${x.clv_unit === "count" ? (x.clv_n ? `beat ${x.clv_beat} of ${x.clv_n}` : "–") : x.clv_avg == null ? "–" : x.clv_unit === "prob" ? `${signed(x.clv_avg * 100, 2)} pts` : `${signed(x.clv_avg, 2)}`}</td></tr>`).join("");
  const scoreRows = T.scores.map((x) => `<tr><td>${esc(x.name)}</td><td class="num">${x.n}</td><td class="num">${num(x.tool, 4)}</td><td class="num">${num(x.market, 4)}</td><td class="num">${x.raw == null ? "–" : num(x.raw, 4)}</td></tr>`).join("");
  const pw = T.prop_weight;
  const srcText = { tested: "learned from real closing prop lines", borrowed: "borrowed from the other sport's test", default: "a default, not yet tested" }[pw.source] || "the starting weight";
  const recentRows = T.recent.map((r) => `<tr><td>${esc(r.date)}</td><td><b>${esc(r.selection)}</b></td><td>${esc(r.game)}</td><td class="num">${fmtOdds(r.odds)}</td><td class="num">${pct(r.p)}</td>
      <td class="status-${esc(r.status)}">${esc(r.status)}</td><td class="num">${r.actual == null ? "–" : num(r.actual, r.actual % 1 ? 1 : 0)}</td><td class="num ${cls(r.pnl)}">${signed(r.pnl, 2)}</td></tr>`).join("");
  const parlayRows = T.parlays.map((p) => `<tr><td>${{ value: "Best value", safe: "Most likely to hit", sgp: "Same game" }[p.mode] || esc(p.mode)}</td><td class="num">${p.n}</td><td class="num">${p.hits}</td><td class="num">${pct(p.hits / p.n)}</td><td class="num">${pct(p.predicted)}</td>
      <td class="num ${cls(p.units)}">${p.units == null ? "–" : signed(p.units)}</td><td class="num ${cls(p.roi)}">${p.roi == null ? "–" : spct(p.roi)}</td></tr>`).join("");

  return head + tiles + `
  <div class="two">
    <section class="card"><div class="hd"><h2>Units won over time</h2><span class="sub">as if 1 unit were bet on every pick at the price shown that day</span></div>
      <div class="bd">${T.series.length ? `<div class="legend">${T.series.map((s) => `<span><span class="key" style="background:${SERIES_COLOR[s.name]}"></span>${esc(s.name)}</span>`).join("")}</div><div class="chart" id="chart-units"></div>` : '<div class="empty">No settled picks in this range.</div>'}</div></section>
    <section class="card"><div class="hd"><h2>Are the win chances honest?</h2><span class="sub">picks given a 60% chance should win about 60% of the time: dots on the line are perfect</span></div>
      <div class="bd">${T.calibration.length ? '<div class="chart" id="chart-cal"></div>' : '<div class="empty">Not enough settled picks yet.</div>'}</div></section>
  </div>
  <section class="card" style="margin-top:18px"><div class="hd"><h2>Results by list</h2><span class="sub">small samples swing a lot. The likely range shows where the true return probably sits.</span></div>
    <div class="bd flush"><div class="tw"><table class="t"><thead><tr><th>List</th><th class="num">Picks</th><th class="num" title="Won - lost - push">W-L-P</th><th class="num">Won</th><th class="num" title="Average win chance the tool gave these picks">Tool expected</th><th class="num" title="Average chance the sportsbook's price implied">Book expected</th><th class="num" title="Profit in units, 1 unit per pick">Units</th><th class="num" title="Profit per unit bet">Return</th><th class="num" title="95% range for the true return, given the sample size">Likely range</th></tr></thead><tbody>${groupsRows}</tbody></table></div></div></section>
  <div class="two">
    <section class="card"><div class="hd"><h2>By bet type</h2><span class="sub">Closing line: + means the pick had a better number than the final one before the game</span></div>
      <div class="bd flush"><div class="tw"><table class="t"><thead><tr><th>Bet type</th><th class="num">Picks</th><th class="num">W-L-P</th><th class="num">Won</th><th class="num">Tool expected</th><th class="num">Units</th><th class="num">Return</th><th class="num" title="Game bets: average points (or percentage points for moneylines) the pick was ahead of the closing line. Props: how many picks had a better line or price than the close.">Vs closing line</th></tr></thead><tbody>${marketRows}</tbody></table></div>
      <p class="note" style="padding:8px 16px 0">${PC.n ? `<b>Props against the closing line:</b> ${PC.beat} of ${PC.n} picks (${pct(PC.beat / PC.n, 0)}) had a better line or price than the sportsbook's last number before the game${PC.same ? `, ${PC.same} the same` : ""}.
        The line itself moved on ${PC.line_moved} of them, ${PC.line_moved_for} toward the pick. Picks that beat the close more than half the time are the earliest sign of a real edge, long before the win-loss record means anything.${PC.no_close ? ` ${PC.no_close} had no closing line posted.` : ""}` : "Props are compared with the sportsbook's last line before the game once they are settled."}</p></div></section>
    <section class="card"><div class="hd"><h2>Who predicted better?</h2><span class="sub">prediction error (log loss): lower is better</span></div>
      <div class="bd flush"><div class="tw"><table class="t"><thead><tr><th>Picks</th><th class="num">Count</th><th class="num">This tool</th><th class="num">Sportsbook</th><th class="num">Simulator alone</th></tr></thead><tbody>${scoreRows || '<tr><td colspan="5" class="muted">Not enough data yet.</td></tr>'}</tbody></table></div>
      <p class="note" style="padding:8px 16px 0">Weight on the prop simulator: <b>${pct(pw.used, 0)}</b>. ${pw.n >= pw.needed ? `Your ${pw.n.toLocaleString()} settled props fit best at ${pct(pw.w_fit, 0)}; that is blended with the starting weight of ${pct(pw.default, 0)} (${srcText}).` : `The starting weight is ${pct(pw.default, 0)} (${srcText}). It will also learn from your own results once ${pw.needed} props are settled (${pw.n} so far).`}</p></div></section>
  </div>
  <div style="margin-top:18px">${myBetsCard(B)}</div>
  ${parlayRows ? `<section class="card"><div class="hd"><h2>Listed parlays</h2><span class="sub">1 unit each at standard parlay odds</span></div><div class="bd flush"><div class="tw"><table class="t"><thead><tr><th>List</th><th class="num">Settled</th><th class="num">Hit</th><th class="num">Hit rate</th><th class="num">Tool expected</th><th class="num">Units</th><th class="num">Return</th></tr></thead><tbody>${parlayRows}</tbody></table></div></div></section>` : ""}
  <section class="card"><div class="hd"><h2>Recently settled</h2></div>
    <div class="bd flush">${recentRows ? `<div class="tw"><table class="t"><thead><tr><th>Date</th><th>Pick</th><th>Game</th><th class="num">Odds</th><th class="num">Win chance</th><th>Result</th><th class="num" title="What actually happened: the final margin, total or stat">Actual</th><th class="num">Units</th></tr></thead><tbody>${recentRows}</tbody></table></div>` : '<div class="empty">None yet.</div>'}</div></section>
  <p class="muted" style="font-size:12px">History files: ${esc(T.history_dir)}</p>`;
}

function myBetsCard(B) {
  const rows = B.rows.map((b) => `<tr><td>${esc(b.date)}</td><td>${esc(b.legs)}</td><td class="num">${fmtOdds(b.odds)}</td><td class="num">${money(b.stake, 2)}</td><td class="num">${pct(b.p_win_all ?? b.p_hit)}</td>
      <td class="status-${esc(b.status)}">${esc(b.status)}</td><td class="num ${cls(b.pnl)}">${b.pnl == null ? "–" : money(b.pnl, 2)}</td>
      <td class="num"><button class="btn small ghost" data-act="del-bet" data-id="${esc(b.id)}" title="Delete this record">Delete</button></td></tr>`).join("");
  return `<section class="card"><div class="hd"><h2>My bets</h2><span class="sub">bets you recorded from the bet slip · ${B.open} open</span></div>
    <div class="bd flush">${B.rows.length ? `<div class="tw"><table class="t"><thead><tr><th>Date</th><th>Bet</th><th class="num">Odds</th><th class="num">Stake</th><th class="num">Win chance</th><th>Result</th><th class="num">Profit</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">No recorded bets. Add picks to the bet slip, enter the odds and stake, and click <i>Record bet</i>.</div>'}</div></section>`;
}

// ============================================================================ how it works page
/* The live estimate against the sportsbook's live prices, on games that are over. No verdict until there is enough. */
function liveTestCard() {
  const t = S.slate.live_test;
  if (!t) return "";
  const head = `<div class="hd"><h2>Live estimate: on trial</h2><span class="sub">the tool's in-game read, tested against the sportsbook's live prices</span></div>`;
  const why = `<p class="note" style="margin:0 0 10px">During a game the tool estimates each bet's chance from the score, the clock and the box score. It does not suggest live bets from that, because the estimate is unproven.
      Instead every live price it sees is saved beside its own estimate and settled when the game ends. Once there is enough, this shows whether the estimate adds anything to the price.</p>`;
  if (!t.n) return `<section class="card">${head}<div class="bd">${why}<div class="empty" style="padding-left:0">No live prices settled yet. They are collected while the dashboard is running and games are on.</div></div></section>`;
  const tiles = `<div class="tiles" style="margin-bottom:10px">
      <div class="tile"><div class="label">Live prices settled</div><div class="value">${t.n.toLocaleString()}</div><div class="delta">from ${t.games} game${t.games === 1 ? "" : "s"} · needs ${t.need_prices} from ${t.need_games}</div></div>
      ${t.ready ? `<div class="tile"><div class="label">Weight the estimate has earned</div><div class="value">${pct(t.w, 0)}</div><div class="delta">${t.w > 0 ? "blended with the live price, it predicted better than the price alone" : "the live price alone predicted better"}</div></div>
      <div class="tile"><div class="label">Prediction error: estimate vs price</div><div class="value">${num(t.ll_tool, 3)} <span class="muted" style="font-size:15px">vs ${num(t.ll_book, 3)}</span></div><div class="delta">lower is better</div></div>` : `<div class="tile"><div class="label">Verdict</div><div class="value">Not yet</div><div class="delta">too few to tell skill from luck</div></div>`}</div>`;
  const rows = t.ready && t.buckets && t.buckets.length ? `<div class="tw"><table class="t"><thead><tr><th>Estimate and price disagree by</th><th class="num">Prices</th><th class="num">Estimate's side won</th><th class="num">The price gave it</th></tr></thead><tbody>
      ${t.buckets.map((b) => `<tr><td>${Math.round(b.lo * 100)} to ${Math.round(b.hi * 100)} points</td><td class="num">${b.n}</td><td class="num">${pct(b.won, 0)}</td><td class="num">${pct(b.book, 0)}</td></tr>`).join("")}</tbody></table></div>` : "";
  return `<section class="card">${head}<div class="bd">${why}${tiles}${rows}
    <p class="note" style="margin:8px 0 0">Read a good result here with suspicion. The feed's live prices can be minutes old while the score is current, which flatters the estimate.
      Bets already settled by the score and the last few minutes of a game are left out for that reason.</p></div></section>`;
}

function pageModel() {
  const steps = `<section class="card"><div class="hd"><h2>How a pick is made</h2></div><div class="bd"><ol class="steps">
      <li><b>Rate the teams and players.</b> Several seasons of results are turned into ratings that adjust for the opponent, with recent games counting more. Injuries, rest, travel, weather and the starting quarterback are added on top.</li>
      <li><b>Simulate the game.</b> Each game is played out 20,000 times. Player stats come from the same simulated games, using each player's share of minutes, targets and carries.</li>
      <li><b>Compare with the sportsbook.</b> The book's odds are converted to a chance with its fee removed. The tool then moves away from that only as far as testing on past seasons has justified.</li>
      <li><b>Call it value only when it pays.</b> A pick is rated Value when its win chance beats the book's price by enough to cover the fee. Every pick is logged and settled, so the Results page shows whether that holds up.</li></ol></div></section>`;
  const cards = S.slate.sports.filter((s) => s.model).map((s) => {
    const m = s.model;
    const SP = s.sport.toUpperCase();
    const game = `<section class="card"><div class="hd"><h2>${SP} games: model vs sportsbook</h2><span class="sub">${m.n_oos ? `tested on ${m.n_oos.toLocaleString()} games the model had not seen` : ""}</span></div><div class="bd">
      <div class="tiles" style="margin-bottom:12px">
        <div class="tile"><div class="label">Model misses the final margin by</div><div class="value">${num(m.mae_model, 2)}</div><div class="delta">points per game, on average</div></div>
        <div class="tile"><div class="label">Sportsbook's closing line misses by</div><div class="value">${num(m.mae_market, 2)}</div><div class="delta">${m.mae_model > m.mae_market ? "the sportsbook is more accurate" : "the model is more accurate"}</div></div>
        <div class="tile"><div class="label">Weight on the model: who wins</div><div class="value">${pct(m.w_margin, 0)}</div><div class="delta">${m.w_margin > 0 ? "it improved on the book in unseen seasons" : "it did not beat the book, so the book's line is used"}</div></div>
        <div class="tile"><div class="label">Weight on the model: total points</div><div class="value">${pct(m.w_total, 0)}</div><div class="delta">${m.w_total > 0 ? "it improved on the book in unseen seasons" : "it did not beat the book, so the book's line is used"}</div></div></div>
      <details><summary class="muted" style="cursor:pointer">What the model learned: every factor and its weight</summary>
      <p class="note">Bars show how many points each factor moves the prediction when it is one typical step above average. Blue pushes the number up, red pushes it down. All weights were fitted from data, none set by hand.</p>
      <div class="two"><div><h3 class="h3">Who wins <span class="muted">+ favours the home team</span></h3><div class="chart" data-bars="${s.sport}|margin"></div></div>
        <div><h3 class="h3">Total points <span class="muted">+ means more points</span></h3><div class="chart" data-bars="${s.sport}|total"></div></div></div></details>
    </div></section>`;
    const c = s.prop_check;
    let prop = "";
    if (c) {
      const w = settings().prop_weight[s.sport];
      const rows = (c.by_market || []).map((x) => `<tr><td>${esc((s.markets || {})[x.market] || x.market)}</td><td class="num">${x.n.toLocaleString()}</td><td class="num">${pct(x.over_rate)}</td><td class="num">${c.priced ? pct(x.market_over) : "–"}</td><td class="num">${pct(x.sim_over_raw)}</td></tr>`).join("");
      prop = `<section class="card"><div class="hd"><h2>${SP} player props: simulator vs real lines</h2><span class="sub">${c.n_lines.toLocaleString()} real ${c.priced ? "closing lines with prices" : "lines (no prices available)"} · ${c.priced ? `${c.days} game days, ${day(c.first)} to ${day(c.last)}` : `last ${c.days} weeks`}</span></div><div class="bd">
        <p class="note" style="margin-top:0">For each past game the simulator was run as it would have been that morning, then compared with the sportsbook's line and with what the player actually did.
          The chart shows how often the simulator's side won, grouped by how strongly it disagreed with ${c.priced ? "the sportsbook's price" : "the line"}. To make money the dots need to sit above the dashed line.</p>
        <div class="legend"><span><span class="key" style="background:var(--s1)"></span>Simulator's side actually won</span><span><span class="key" style="background:var(--s2)"></span>${c.priced ? "What the book's price implied" : "An even bet"}</span><span><span class="key dash"></span>Needed to beat a −110 price</span></div>
        <div class="chart" data-gap="${s.sport}"></div>
        <p class="note"><b>Result:</b> the simulator gets <b>${pct(w, 0)}</b> of the weight for ${SP} props${c.beats_market ? "" : " (it did not improve on the sportsbook at all)"}.
          ${c.priced ? `Betting every one of these props blind would have returned ${spct(c.all_bets.roi)}: that is the sportsbook's fee.` : "Prices were not available, so this test cannot say whether the leans were profitable."}</p>
        <details><summary class="muted" style="cursor:pointer">By stat: how often the over won</summary>
        <div class="tw"><table class="t"><thead><tr><th>Stat</th><th class="num">Lines</th><th class="num">Over actually won</th><th class="num">Book's price said</th><th class="num" title="Before the simulator is centred on the market">Simulator said</th></tr></thead><tbody>${rows}</tbody></table></div>
        <p class="note">The simulator's average is centred on the sportsbook's before pricing, so only its opinion about one player relative to the others is used.</p></details>
      </div></section>`;
    }
    return game + prop;
  });
  return `${pageHead("💡 How it works", "What the tool does with the data, and how well it has done against real sportsbook lines.")}${steps}${cards.join("")}${liveTestCard()}
    <p class="muted" style="font-size:12.5px">For the full betting replay of the game model, run <code>.\\sportsedge backtest</code> in a terminal.</p>`;
}

// ============================================================================ guide
const feeExample = () => { const f = feeOf(50); return inCents() ? `At these settings a 50¢ contract costs <b>${fmtCents(50 + f)}</b> (${fmtCents(f)} fee) and a 20¢ contract <b>${fmtCents(20 + feeOf(20))}</b>.` : ""; };
function settingsBlock() {
  return `<div class="prefs"><h3 style="margin-top:0">Settings: how your app shows prices</h3>
    <div class="seg pxseg" role="group" aria-label="How prices are shown">
      <button data-act="px-mode" data-mode="odds" class="${inCents() ? "" : "on"}">American odds <span class="muted">−110, +150</span></button>
      <button data-act="px-mode" data-mode="cents" class="${inCents() ? "on" : ""}">Cents per contract <span class="muted">52¢</span></button></div>
    <p class="note" style="margin:8px 0 0">${inCents()
      ? "For a trading app, where you buy a contract that pays $1 if you are right. Every price in the tool is shown in cents, and every box takes a price in cents."
      : "For a sportsbook. Choose <b>cents per contract</b> if you use a trading app, where you buy a contract that pays $1 if you are right."}</p>
    ${inCents() ? `<div class="form-row" style="margin-top:10px"><label>Fee per contract, in cents<input type="number" min="0" step="0.1" data-px="flat" value="${PX.flat || ""}" placeholder="0"></label>
        <label>Plus this % of price × (1 − price)<input type="number" min="0" step="0.5" data-px="rate" value="${PX.rate || ""}" placeholder="0"></label></div>
      <p class="note" style="margin:8px 0 0" id="px-example">${feeExample()}</p>
      <p class="note" style="margin:6px 0 0">Set this from your app's fee page, or every return here will look better than it is. Apps charge in different ways: a flat amount per contract goes in the first box.
        When this was written Kalshi published 7% × price × (1 − price), which is 7 in the second box. Check, because fees change.</p>` : ""}</div>`;
}
function renderGuide() {
  const d = $("#guide");
  d.innerHTML = `<div class="g-hd"><h2 id="guide-title">Guide and settings</h2><button class="btn small" data-act="guide-close" aria-label="Close the guide">Close</button></div>
  <div class="g-bd">
    ${settingsBlock()}
    <h3>What the tool does</h3>
    <p>It estimates how likely each bet is to win, compares that with the price your sportsbook offers, and tells you whether the bet pays enough for the risk. It never places bets: you do that at your sportsbook.</p>
    <h3>The numbers</h3>
    <dl class="gloss">
      <dt>Odds</dt><dd>American odds. <b>−150</b> means you bet $150 to win $100. <b>+130</b> means you bet $100 to win $130.</dd>
      <dt>Win chance</dt><dd>The tool's estimate that the pick wins. It starts from the sportsbook's own price and moves only as far as testing on past games has justified.</dd>
      <dt>Book's chance</dt><dd>What the sportsbook's odds imply once its fee is removed. Sportsbooks build a fee into every price, usually 4% to 7%.</dd>
      <dt>Edge</dt><dd>Win chance minus the book's chance, in percentage points. Positive means the tool thinks the pick is more likely than the price suggests.</dd>
      <dt>Expected return</dt><dd>Your average profit per $1 if you could make the same bet many times. <b>+5%</b> is 5 cents per dollar. Negative means you lose on average, even when the pick usually wins.</dd>
      <dt>Stake</dt><dd>A suggested bet size: a quarter of the Kelly formula, never more than 3% of your bankroll. Set your bankroll at the top to see it in dollars.</dd>
      <dt>Fair odds</dt><dd>The break-even price. Only bet if your sportsbook pays more than this.</dd>
      <dt>Closing line</dt><dd>The sportsbook's last line and price before the game starts, when it knows the most. If your picks regularly have a better number than the close, that is the earliest reliable sign of a real edge. The Results page tracks it for game bets and props.</dd>
    </dl>
    <h3>The ratings</h3>
    <dl class="gloss">
      <dt>${chip("value")}</dt><dd>Positive expected return and a real edge over the book's price. Rare, by design.</dd>
      <dt>${chip("fair")}</dt><dd>Close to a fair price. Roughly break-even.</dd>
      <dt>${chip("poor")}</dt><dd>The sportsbook's fee outweighs any edge. This is most bets.</dd>
      <dt>${chip("news")}</dt><dd>The model is far from the sportsbook. That usually means news the model has not seen, so it is never listed as value.</dd>
    </dl>
    <h3>Finding your way around</h3>
    <p>On <b>Today</b>, each game is one block: the two teams with their moneyline, over/under and spread, then that game's player props underneath. Tap a stat (Points, Rebounds ...) to switch, and tap any price to add it to the bet slip.
      The <b>Player props</b> page has every line, <b>by game</b> (grouped by stat or by player) or as a <b>ranked list</b> across all games.</p>
    <h3>Player props</h3>
    <dl class="gloss">
      <dt>Line</dt><dd>The number the sportsbook sets, for example 24.5 points. You bet on the player finishing over or under it.</dd>
      <dt>Simulator projects</dt><dd>A typical result across 20,000 simulated games: the middle outcome for yards and points, the average for low counts such as threes or receptions.</dd>
      <dt>Simulator vs book</dt><dd>How many percentage points more likely the simulator thinks the over (blue, right) or under (red, left) is than the sportsbook does. A lean, not a bet.</dd>
    </dl>
    <h3>During the games</h3>
    <p>The <b>Live</b> page follows every bet you recorded, pick by pick, with real scores and stat lines. If your sportsbook offers a cash-out, type the amount in and the tool compares it with what the bet is worth right now:
      what it would pay, times the chance the remaining picks win. Sportsbooks usually offer less than that. Cashing out can still make sense when the possible win is large next to your bankroll, and the page tells you the amount at which it does.</p>
    <h3>Trading apps (prices in cents)</h3>
    <p>A trading app sells a contract that pays $1 if you are right, for a price in cents, and charges a fee. Switch the tool to <b>cents per contract</b> above (or with the −110 / 52¢ switch at the top) and set the fee.
      Some apps, DraftKings' trade slip among them, write that price as a percent: 52% there is 52¢ here. The tool keeps ¢ for prices so they are not mistaken for its own win chances.
      A price in cents is a chance: a 62¢ contract is worth buying only if the pick wins more than 62% of the time, fee included.
      A trade slip also shows the <b>payout</b> for your spend. Type that into the bet slip when you can: it already has the fee in it, so it is the most exact. So each pick shows <b>worth up to</b>: the most it is worth paying on your app.
      The tool's own prices still come from a sportsbook's lines, shown in cents for comparison. Your app's price and line can differ, so type yours into the bet slip.</p>
    <p>Selling before the game ends is the same question as a cash-out: on the Live page, type what you would get for selling into the cash-out box. The hedge box takes a price in cents too.</p>
    <h3>Betting during a game</h3>
    <p>The Live page shows the sportsbook's in-game spread, total and moneyline for games in play. The tool does <b>not</b> suggest new live bets: its live estimate is too rough to beat a live price, and it says so rather than guess.
      What it does work out is a <b>hedge</b>. When a recorded bet has one pick left and that game is on, it shows how much to bet on the other side so you finish with the same amount whichever way it goes,
      and whether the two lines leave a stretch where both bets win (a middle) or both lose.</p>
    <h3>A different line on your app</h3>
    <p>Every pick in the bet slip has a <b>Your line</b> box. If your app or sportsbook has the pick at another line, type it and the pick is priced there: its win chance at your line, and in cents the most it is worth paying.
      It works for spreads, totals and player props on games still to come. A game that has started cannot be priced at another line, only recorded at it.</p>
    <h3>Pasting a bet slip</h3>
    <p><b>Paste a slip</b>, at the top of the bet slip, reads a bet you copied as text from your sportsbook: it finds the picks, the odds and the stake and fills the slip in.
      Anything it cannot match to a pick the tool knows is listed, not guessed. Check each pick and its line, then record it. A screenshot cannot be read, only text.</p>
    <h3>Recording a bet on a game that has started</h3>
    <p>Once a game starts it leaves the list of games to come, because its prices are no longer on offer. It moves to <b>Started games</b> on the Today page, with every pick at its last number before the start.
      Use that to record a bet you already placed: tap the picks, type the odds from your slip, change a line if yours was different, and record it. The Live page then follows it.</p>
    <h3>Top picks and suggested parlays</h3>
    <p>Each game lists its <b>top picks</b>: the picks that return the most for their price. The <b>Parlays</b> section then gives the best parlay for each day,
      each group of games that start together, and each game, at three sizes of payout: <b>Safer</b> (about +100 to +300), <b>Balanced</b> (+300 to +800) and <b>Long shot</b> (+800 and up).
      Across several games no more than two picks come from one game, so a single game cannot sink the bet on its own.</p>
    <p>A parlay multiplies the sportsbook's fee along with the payout, so bigger odds are not better by themselves. What matters is the <b>fair odds</b>: bet it when your slip pays more than that.
      NFL props reach the tool without prices. They are listed as a <b>lean</b>, never as value, and their payout is an estimate until you type in the odds from your slip.</p>
    <h3>Line moves</h3>
    <p>The <b>Line moves</b> page shows how each game's spread, total and moneyline have moved since the sportsbook first posted them, with a chart per game.
      The tool checks the feed every few minutes, logs every change, and reworks the picks by itself when a line has moved.</p>
    <p>For a bet you have recorded, each pick also says whether its line has moved <b>in your favour</b> or <b>against you</b> since you placed it, on the Live and Line moves pages.
      In your favour means you hold a better number than the sportsbook offers now: over many bets that is the best early sign of a good pick. One bet proves nothing either way.</p>
    <h3>If a line looks wrong</h3>
    <p>The tool reads sportsbook lines through ESPN's feed, which can be minutes or hours behind. If your sportsbook shows a different spread or total, open <b>Different at your sportsbook? Correct the line</b> on that game's card and type in the current numbers.
      For a player prop, use <b>Price a line from your sportsbook</b> on the Player props page. A pick is only as good as the line it was priced at.</p>
    <h3>A good routine</h3>
    <ol class="steps">
      <li><b>Refresh</b> close to game time, when injuries and lines are settled.</li>
      <li>Look at <b>Value bets</b>. An empty list is a real answer: nothing is worth betting right now.</li>
      <li><b>Check the line</b> against your sportsbook before betting, and correct it here if it has moved.</li>
      <li>Click <b>+</b> to add picks to the <b>bet slip</b>. Build the same bet at your sportsbook, copy the odds from its slip into <b>Odds on your sportsbook's slip</b>, and read the expected return.</li>
      <li>Click <b>Record bet</b> after you place it. The <b>Results</b> page settles it and keeps score.</li>
    </ol>
    <p class="muted">Every number here is a probability, not a promise. Only stake money you can afford to lose.</p>
  </div>`;
}

// ============================================================================ charts (inline SVG)
function niceTicks(lo, hi, n = 5) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function tooltip(el) {
  let t = $(".tip", el);
  if (!t) { t = document.createElement("div"); t.className = "tip"; el.appendChild(t); }
  return t;
}
function placeTip(el, t, x, y) {
  t.style.display = "block";
  const w = t.offsetWidth, W = el.clientWidth;
  t.style.left = `${Math.min(Math.max(x + 12, 0), W - w)}px`;
  t.style.top = `${Math.max(y - t.offsetHeight - 10, 0)}px`;
}

function lineChart(el, series) {
  const W = el.clientWidth || 600, narrow = W < 520, H = 250, m = { l: 46, r: narrow ? 16 : 120, t: 10, b: 26 };
  const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))].sort();
  const t = (d) => Date.parse(d + "T12:00:00");
  let t0 = t(dates[0]), t1 = t(dates[dates.length - 1]);
  if (t0 === t1) { t0 -= 86400000; t1 += 86400000; }
  const vals = series.flatMap((s) => s.points.map((p) => p.units)).concat([0]);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const X = (d) => m.l + ((t(d) - t0) / (t1 - t0)) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Cumulative units won by group over time">`;
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"></line>`).join("")}</g>`;
  svg += `<line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}"></line>`;
  svg += ticks.map((v) => `<text class="tick" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${signed(v, Math.abs(y1 - y0) < 5 ? 1 : 0)}</text>`).join("");
  const xt = dates.length <= 6 ? dates : [dates[0], dates[Math.floor(dates.length / 2)], dates[dates.length - 1]];
  svg += xt.map((d) => `<text class="tick" x="${X(d)}" y="${H - 6}" text-anchor="middle">${new Date(t(d)).toLocaleDateString([], { month: "short", day: "numeric" })}</text>`).join("");
  const ends = [];
  for (const s of series) {
    const c = SERIES_COLOR[s.name] || "var(--s1)";
    const pts = s.points.map((p) => `${X(p.date)},${Y(p.units)}`).join(" L");
    if (s.points.length > 1) svg += `<path d="M${pts}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>`;
    const last = s.points[s.points.length - 1];
    svg += `<circle cx="${X(last.date)}" cy="${Y(last.units)}" r="4" fill="${c}" stroke="var(--surface)" stroke-width="2"></circle>`;
    ends.push({ y: Y(last.units), x: X(last.date), text: `${s.name} ${signed(last.units)}u` });
  }
  ends.sort((a, b) => a.y - b.y);
  const collide = ends.some((e, i) => i && e.y - ends[i - 1].y < 15);
  if (!collide && !narrow) svg += ends.map((e) => `<text class="lab" x="${e.x + 9}" y="${e.y + 4}">${esc(e.text)}</text>`).join("");
  svg += `<line id="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--axis)" stroke-width="1" style="display:none"></line>`;
  svg += `<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" id="hit"></rect></svg>`;
  el.innerHTML = svg;
  const tip = tooltip(el), xh = $("#xh", el);
  const valueAt = (s, d) => { let v = null; for (const p of s.points) if (p.date <= d) v = p.units; return v; };
  const hit = $("#hit", el);
  const move = (ev) => {
    const r = el.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    let best = dates[0];
    for (const d of dates) if (Math.abs(X(d) - px) < Math.abs(X(best) - px)) best = d;
    xh.setAttribute("x1", X(best)); xh.setAttribute("x2", X(best)); xh.style.display = "";
    tip.innerHTML = "";
    const head = document.createElement("div"); head.className = "t";
    head.textContent = new Date(t(best)).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    tip.appendChild(head);
    for (const s of series) {
      const v = valueAt(s, best);
      if (v == null) continue;
      const row = document.createElement("div"); row.className = "r";
      row.innerHTML = `<span class="k" style="background:${SERIES_COLOR[s.name]}"></span><b>${signed(v, 2)}u</b>`;
      const nm = document.createElement("span"); nm.className = "muted"; nm.textContent = s.name; row.appendChild(nm);
      tip.appendChild(row);
    }
    placeTip(el, tip, (X(best) / W) * r.width, ((m.t + 10) / H) * r.height + 40);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; xh.style.display = "none"; });
}

function calibrationChart(el, bins) {
  const W = el.clientWidth || 400, H = 250, m = { l: 46, r: 16, t: 10, b: 34 };
  const vals = bins.flatMap((b) => [b.predicted, b.actual]);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 0.05) * 10) / 10);
  const hi = Math.min(1, Math.ceil((Math.max(...vals) + 0.05) * 10) / 10);
  const X = (v) => m.l + ((v - lo) / (hi - lo)) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const ticks = niceTicks(lo, hi, 4).filter((v) => v >= lo - 1e-9 && v <= hi + 1e-9);
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Predicted versus actual win rate">`;
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"></line>`).join("")}</g>`;
  svg += ticks.map((v) => `<text class="tick" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>`).join("");
  svg += ticks.map((v) => `<text class="tick" x="${X(v)}" y="${H - 18}" text-anchor="middle">${Math.round(v * 100)}%</text>`).join("");
  svg += `<text class="tick" x="${(m.l + W - m.r) / 2}" y="${H - 2}" text-anchor="middle">Win chance the tool gave</text>`;
  svg += `<line x1="${X(lo)}" y1="${Y(lo)}" x2="${X(hi)}" y2="${Y(hi)}" stroke="var(--axis)" stroke-width="1"></line>`;
  svg += `<text class="tick" x="${X(hi) - 4}" y="${Y(hi) + 14}" text-anchor="end">perfect</text>`;
  bins.forEach((b, i) => {
    svg += `<circle cx="${X(b.predicted)}" cy="${Y(b.actual)}" r="5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"></circle>`;
    svg += `<circle cx="${X(b.predicted)}" cy="${Y(b.actual)}" r="13" fill="transparent" data-i="${i}" tabindex="0" class="hitdot"></circle>`;
  });
  svg += "</svg>";
  el.innerHTML = svg;
  const tip = tooltip(el);
  const show = (c) => {
    const b = bins[+c.dataset.i];
    tip.innerHTML = `<div class="t">Picks given ${Math.round(b.lo * 100)}–${Math.round(b.hi * 100)}%</div>
      <div class="r"><b>${pct(b.actual)}</b><span class="muted">actually won</span></div>
      <div class="r"><b>${pct(b.predicted)}</b><span class="muted">the tool expected</span></div>
      <div class="r"><b>${b.n}</b><span class="muted">picks</span></div>`;
    const r = el.getBoundingClientRect();
    placeTip(el, tip, (+c.getAttribute("cx") / W) * r.width, (+c.getAttribute("cy") / H) * r.height);
  };
  $$(".hitdot", el).forEach((c) => {
    c.addEventListener("pointerenter", () => show(c));
    c.addEventListener("focus", () => show(c));
    c.addEventListener("pointerleave", () => (tip.style.display = "none"));
    c.addEventListener("blur", () => (tip.style.display = "none"));
  });
}

/* How often the simulator's side won, by how strongly it disagreed with the sportsbook. */
function gapChart(el, c) {
  const b = c.gap_buckets || [];
  if (!b.length) { el.innerHTML = '<div class="empty">Not enough lines yet.</div>'; return; }
  const W = el.clientWidth || 600, H = 240, m = { l: 46, r: 18, t: 14, b: 46 };
  const vals = b.flatMap((x) => [x.won, x.market]).concat([BREAK_EVEN, 0.5]);
  const lo = Math.floor((Math.min(...vals) - 0.02) * 50) / 50, hi = Math.ceil((Math.max(...vals) + 0.02) * 50) / 50;
  const X = (i) => m.l + ((i + 0.5) / b.length) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const ticks = niceTicks(lo, hi, 4).filter((v) => v >= lo - 1e-9 && v <= hi + 1e-9);
  const label = (x) => (x.hi >= 1 ? `${Math.round(x.lo * 100)}+` : `${Math.round(x.lo * 100)}–${Math.round(x.hi * 100)}`);
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Win rate of the simulator's side by size of disagreement with the sportsbook">`;
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"></line>`).join("")}</g>`;
  svg += ticks.map((v) => `<text class="tick" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>`).join("");
  svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(BREAK_EVEN)}" y2="${Y(BREAK_EVEN)}" stroke="var(--ink-2)" stroke-width="1" stroke-dasharray="4 4"></line>`;
  svg += b.map((x, i) => `<text class="tick" x="${X(i)}" y="${H - 26}" text-anchor="middle">${label(x)}</text>`).join("");
  svg += `<text class="tick" x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">Disagreement with the ${c.priced ? "sportsbook" : "line"}, in percentage points</text>`;
  const path = (k) => b.map((x, i) => `${i ? "L" : "M"}${X(i)},${Y(x[k])}`).join(" ");
  svg += `<path d="${path("market")}" fill="none" stroke="var(--s2)" stroke-width="2"></path>`;
  svg += `<path d="${path("won")}" fill="none" stroke="var(--s1)" stroke-width="2"></path>`;
  b.forEach((x, i) => {
    svg += `<circle cx="${X(i)}" cy="${Y(x.market)}" r="4" fill="var(--s2)" stroke="var(--surface)" stroke-width="2"></circle>`;
    svg += `<circle cx="${X(i)}" cy="${Y(x.won)}" r="5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"></circle>`;
    svg += `<text class="lab" x="${X(i)}" y="${Y(x.won) - 10}" text-anchor="middle">${pct(x.won)}</text>`;
    svg += `<rect x="${X(i) - (W - m.l - m.r) / b.length / 2}" y="${m.t}" width="${(W - m.l - m.r) / b.length}" height="${H - m.t - m.b}" fill="transparent" data-i="${i}" tabindex="0" class="hitdot"></rect>`;
  });
  el.innerHTML = svg + "</svg>";
  const tip = tooltip(el);
  const show = (r0) => {
    const i = +r0.dataset.i, x = b[i];
    tip.innerHTML = `<div class="t">Disagreed by ${label(x)} points</div>
      <div class="r"><span class="k" style="background:var(--s1)"></span><b>${pct(x.won)}</b><span class="muted">simulator's side won</span></div>
      <div class="r"><span class="k" style="background:var(--s2)"></span><b>${pct(x.market)}</b><span class="muted">${c.priced ? "book's price implied" : "an even bet"}</span></div>
      <div class="r"><b>${x.n.toLocaleString()}</b><span class="muted">lines</span></div>`;
    const r = el.getBoundingClientRect();
    placeTip(el, tip, (X(i) / W) * r.width, (Y(x.won) / H) * r.height);
  };
  $$(".hitdot", el).forEach((r0) => {
    r0.addEventListener("pointerenter", () => show(r0));
    r0.addEventListener("focus", () => show(r0));
    r0.addEventListener("pointerleave", () => (tip.style.display = "none"));
    r0.addEventListener("blur", () => (tip.style.display = "none"));
  });
}

function divBars(el, rows) {
  rows = [...rows].sort((a, b) => Math.abs(b.v) - Math.abs(a.v));
  const W = el.clientWidth || 420, rowH = 22, labelW = Math.min(190, W * 0.42), valW = 46;
  const H = rows.length * rowH + 8;
  const x0 = labelW + 8, x1 = W - valW;
  const zero = (x0 + x1) / 2;
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.v)), 0.01);
  const half = (x1 - x0) / 2;
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Factor weights">`;
  svg += `<line class="baseline" x1="${zero}" x2="${zero}" y1="0" y2="${H}"></line>`;
  rows.forEach((r, i) => {
    const y = i * rowH + 5, h = 12, w = (Math.abs(r.v) / maxAbs) * half, rr = Math.min(4, w);
    const fill = r.v >= 0 ? "var(--div-pos)" : "var(--div-neg)";
    let d;
    if (r.v >= 0) d = `M${zero},${y} H${zero + w - rr} Q${zero + w},${y} ${zero + w},${y + rr} V${y + h - rr} Q${zero + w},${y + h} ${zero + w - rr},${y + h} H${zero} Z`;
    else d = `M${zero},${y} H${zero - w + rr} Q${zero - w},${y} ${zero - w},${y + rr} V${y + h - rr} Q${zero - w},${y + h} ${zero - w + rr},${y + h} H${zero} Z`;
    const room = Math.max(8, Math.floor(labelW / 6.4));  // letters that fit in the label column
    const short = r.label.length > room ? `${r.label.slice(0, room - 1).trimEnd()}…` : r.label;
    svg += `<text class="lab" x="${labelW}" y="${y + 10}" text-anchor="end">${esc(short)}<title>${esc(r.label)}</title></text>`;
    svg += `<path d="${d}" fill="${fill}"><title>${esc(r.label)}: ${signed(r.v, 2)} points</title></path>`;
    const tx = r.v >= 0 ? zero + w + 5 : zero - w - 5;
    svg += `<text class="tick" x="${tx}" y="${y + 10}" text-anchor="${r.v >= 0 ? "start" : "end"}">${signed(r.v, 2)}</text>`;
  });
  el.innerHTML = svg + "</svg>";
}

function drawBars() {
  $$("[data-bars]").forEach((el) => {
    if (!el.clientWidth) return; // inside a closed <details>
    const [sport, model] = el.dataset.bars.split("|");
    const s = S.slate.sports.find((x) => x.sport === sport);
    const rows = (s.model.factors || []).filter((f) => f.model === model).map((f) => ({ label: f.feature, v: f.points_per_sd }));
    divBars(el, rows);
  });
}

function redrawPropGame(id) {
  const [sport, gid] = [id.slice(0, id.indexOf("|")), id.slice(id.indexOf("|") + 1)];
  const s = S.slate.sports.find((x) => x.sport === sport);
  const g = s && s.games.find((x) => String(x.game_id) === gid);
  const box = $$("details").find((d) => d.dataset.pg === id);
  if (g && box) $(".bd", box).innerHTML = gameProps(s, g, { full: true });
}

/* On a narrow screen a wide table is shown as a list of cards (see styles.css). For that each cell needs to
   know its column's name, and the row's title cell and action cells need marking. */
function labelTables(root = document) {
  for (const t of $$("table.t", root)) {
    if (t.dataset.labelled) continue;
    t.dataset.labelled = "1";
    const heads = $$("thead th", t).map((th) => ({ text: th.textContent.replace(/[↓↑]/g, "").replace(/\s+/g, " ").trim(), add: th.classList.contains("c-add") }));
    if (!heads.length) continue;
    t.classList.add("stack");
    if (heads.length > 9) t.classList.add("dense");
    const mainAt = heads.findIndex((h) => !h.add);
    for (const tr of $$("tbody tr", t)) {
      const tds = [...tr.children];
      if (tds.length !== heads.length) continue;  // a full-width message row
      tds.forEach((td, i) => {
        if (heads[i].add) { td.classList.add("c-add"); tr.classList.add("has-add"); }
        else if (i === mainAt) td.classList.add("main");
        else if (heads[i].text) td.dataset.label = heads[i].text;
        else td.classList.add("act");
      });
    }
  }
}

function afterRender() {
  labelTables();
  if (S.page === "moves") drawMoves();
  if (S.focusGame) { const el = document.getElementById(S.focusGame); S.focusGame = null; if (el) setTimeout(() => el.scrollIntoView({ block: "start", behavior: "smooth" }), 30); }
  if (S.focusForm && S.page === "props") { S.focusForm = false; const t = $("#pf-over"); if (t) setTimeout(() => { t.scrollIntoView({ block: "center", behavior: "smooth" }); t.focus({ preventScroll: true }); }, 30); }
  if (S.page === "track" && S.track && !S.track.error) {
    const u = $("#chart-units"); if (u) lineChart(u, S.track.series);
    const c = $("#chart-cal"); if (c) calibrationChart(c, S.track.calibration);
  }
  if (S.page === "model") {
    drawBars();
    $$("[data-gap]").forEach((el) => {
      const s = S.slate.sports.find((x) => x.sport === el.dataset.gap);
      if (s && s.prop_check) gapChart(el, s.prop_check);
    });
  }
}

// ============================================================================ bet slip
function renderSlip() {
  const el = $("#slip");
  const legs = S.slip.map((k) => S.legs[k]).filter(Boolean);
  $("#slipToggle").textContent = `Bet slip${legs.length ? ` (${legs.length})` : ""}`;
  $("#slipToggle").classList.toggle("empty", !legs.length && !$("#slip").classList.contains("open"));
  let html = `<div class="hd"><h2>Bet slip</h2>${legs.length ? `<span class="count">${legs.length}</span>` : ""}
    <button class="btn small ghost" data-act="import-toggle" title="Paste the text of a bet from your sportsbook">Paste a slip</button>${legs.length ? `<button class="btn small ghost" data-act="clear-slip">Clear</button>` : ""}</div>${importBox()}${importNote()}`;
  if (!legs.length) {
    el.innerHTML = html + `<div class="empty"><b>Build a bet here.</b><br>Click <b>+</b> next to any pick, or any bet on a game card or the prop board.
      Add two or more to price a parlay. Already placed a bet? Use <b>Paste a slip</b> to fill this in from your sportsbook's text. Nothing is placed for you: this only does the maths.</div>`;
    return;
  }
  const alts = (S.slipEval && S.slipEval.alts) || {};
  html += `<div class="items">${legs.map((l) => {
    const alt = alts[l.key], mine = slipLinesNow()[l.key];
    // a pick taken at another line: priced there if its game is still to come, otherwise only renamed
    const where = mine == null ? "" : alt && alt.priced ? `<span class="sline-note ok">priced at your line: <b>${esc(alt.selection)}</b>, win chance <b>${pct(alt.p_cond, 0)}</b>${inCents() ? `, worth up to <b>${fmtCents(worthUpTo(alt.p_cond))}</b>` : ""}. The tool's own line is ${lineNum(l.line)}.</span>`
      : `<span class="sline-note">the tool has ${lineNum(l.line)}${l.started ? " and cannot price a game that has started at another line" : ""}: the chance shown is for that line</span>`;
    return `<div class="item"><div class="sel">${esc(alt && alt.priced && mine != null ? alt.selection : l.selection)}</div>
      <button class="x" data-act="toggle" data-key="${esc(l.key)}" aria-label="Remove ${esc(l.selection)}">×</button>
      <div class="meta">${esc(l.game)} · ${l.started ? `started · last price ${fmtOdds(l.odds)}` : l.assumed ? `no price in the feed (${fmtOdds(l.odds)} assumed)` : `${fmtOdds(l.odds)}${mine != null ? " at the tool's line" : ""}`} · win chance ${l.started ? "before the game " : ""}${pct(alt && alt.priced && mine != null ? alt.p_cond : l.p_cond, 0)}</div>
      ${l.line != null ? `<label class="sline" title="If your app or sportsbook has this pick at a different line, type it: the pick is priced at your line.">Your line <input type="number" step="0.5" data-line-key="${esc(l.key)}" value="${esc(S.slipLines[l.key] ?? l.line)}" aria-label="The line you are betting for ${esc(l.selection)}"></label>${where}` : ""}</div>`;
  }).join("")}</div>`;
  const nAssumed = legs.filter((l) => l.assumed).length, nStarted = legs.filter((l) => l.started).length;
  const E = S.slipEval;
  if (E && E.n) {
    const conflicts = E.conflicts && E.conflicts.length;
    html += `<div class="sum">
      ${conflicts ? `<div class="banner warn" style="margin:0">Two sides of the same bet are in the slip. Remove one.</div>` : ""}
      <div class="kv"><span class="k">${legs.length > 1 ? `${inCents() ? "Combined price" : "Parlay odds"} (${nAssumed ? "estimate" : inCents() ? "from sportsbook odds" : "standard"})` : nAssumed ? `${priceWord()} (assumed)` : inCents() ? "Sportsbook price" : "Odds"}</span><span class="v">${fmtOdds(E.odds)}</span></div>
      <div class="kv"><span class="k">${legs.length > 1 ? "Chance every pick wins" : "Win chance"}</span><span class="v">${pct(E.p_win_all)}${E.p_hit - E.p_win_all > 0.005 ? ` <span class="muted" style="font-weight:400">+${pct(E.p_hit - E.p_win_all)} push</span>` : ""}</span></div>
      <div class="kv" title="${inCents() ? "Only buy for less than this" : "Only bet if your sportsbook pays more than this"}"><span class="k">Fair ${inCents() ? "price" : "odds"} (break-even)</span><span class="v">${fmtOdds(E.fair_odds)}</span></div>
      ${Object.keys(slipLinesNow()).some((k) => alts[k] && alts[k].priced) ? `<div class="hint">A pick above is priced at <b>your line</b>, not the tool's. The ${inCents() ? "price" : "odds"} the tool shows belong to its own line, so type the ${inCents() ? "price or payout" : "odds"} your ${inCents() ? "app" : "sportsbook"} gives for yours.</div>` : ""}
      ${nStarted ? `<div class="hint"><b>${nStarted === legs.length ? (legs.length === 1 ? "This game has" : "These games have") : `${nStarted} of these picks are from games that have`} started.</b> The odds and chances shown are the last ones before the start. Check each line matches the one you bet, type the odds from your slip, and record it: the Live page takes over from there.</div>` : ""}
      ${nAssumed ? `<div class="hint"><b>${nAssumed === legs.length ? (legs.length === 1 ? "This pick has" : "These picks have") : `${nAssumed} of these picks ${nAssumed === 1 ? "has" : "have"}`} no price in the feed.</b> The odds shown assume ${assumedOdds()} for each, a typical price. Type the real odds from your sportsbook's slip below before trusting the return.</div>` : ""}
      ${E.same_game ? `<div class="hint">These picks are from the same game and win together ×${num(E.lift, 2)} as often as unrelated picks would. Your sportsbook prices same-game parlays itself, so type in the odds it offers.</div>` : ""}
      <div class="row"><label>${inCents() ? "Price on your app, in cents" : "Odds on your sportsbook's slip"}<input type="number" step="${inCents() ? "0.1" : "1"}" id="slipOdds" value="${S.slipOdds ?? ""}" aria-describedby="slipOddsHelp"></label>
        <label>${inCents() ? "Spend $" : "Stake $"}<input type="number" min="0" step="1" id="slipStake" value="${S.slipStake ?? ""}" placeholder="0"></label></div>
      ${inCents() ? `<div class="row one"><label>Payout your trade slip shows for that spend $<input type="number" min="0" step="1" id="slipPayout" value="${esc(S.slipPayout ?? "")}" placeholder="optional"></label></div>` : ""}
      <div class="hint" id="slipOddsHelp">${inCents()
        ? `Type the price your app shows for ${legs.length > 1 ? "this combination" : "this contract"}: the percent on a trade slip, or cents (52 for 52% or 52¢). The box starts at ${fmtOdds(E.odds)}, what a sportsbook's odds for ${legs.length > 1 ? "these picks" : "it"} work out to.
          <b>Better still, type the payout</b> your slip shows for your spend: that already has the fee in it, so the price box and the fee setting (${feeText()}) are then not used. <button class="linkish" data-act="guide">Fee setting</button>`
        : legs.length > 1
          ? `Build this same parlay at your sportsbook and copy the odds it shows for the whole bet (for example +575). The box starts at ${fmtOdds(E.odds)}, the standard price worked out from the ${legs.length} picks' own odds.`
          : `Copy the odds your sportsbook shows for this pick right now. The box starts at the odds the tool has.`} Also check that each line above matches your ${inCents() ? "app" : "sportsbook"}: the result is only as good as the lines.</div>
      <div id="slipCalc"></div>
      <button class="btn primary" data-act="record-bet" ${conflicts ? "disabled" : ""}>Record bet</button>
      <div class="hint">Saves it so the Live page can follow it during the games and Results can settle it afterwards. Place the actual bet at your sportsbook.</div>
    </div>`;
  }
  el.innerHTML = html;
  updateSlipCalc();
}

const americanOfDec = (d) => (d >= 2 ? (d - 1) * 100 : -100 / (d - 1));
/* The price the slip is judged at. A trade slip shows what your stake pays: when that is typed it is used as it
   stands, because it already holds the fee. Otherwise the price box is used, and in cents only whole contracts
   are bought, so a few cents of the spend can be left over. null when nothing usable is typed. */
function slipPrice() {
  const stake = parseFloat(S.slipStake), pay = parseFloat(S.slipPayout);
  if (inCents() && pay > 0 && stake > 0 && pay > stake) {
    return { from: "payout", dec: pay / stake, odds: americanOfDec(pay / stake), cost: (100 * stake) / pay, spent: stake, payout: pay };
  }
  const odds = typedOdds(S.slipOdds);
  if (isNaN(odds)) return null;
  if (!inCents()) return { from: "odds", dec: decOf(odds), odds, cost: null, spent: stake, payout: stake > 0 ? stake * decOf(odds) : null };
  const typed = parseFloat(S.slipOdds), cost = typed + feeOf(typed);
  const n = stake > 0 ? Math.floor(stake / (cost / 100) + 1e-9) : null;
  return { from: "price", dec: 100 / cost, odds, cost, contracts: n, spent: n == null ? null : (n * cost) / 100, payout: n };
}

function updateSlipCalc() {
  const box = $("#slipCalc");
  const E = S.slipEval;
  if (!box || !E) return;
  const sp = slipPrice();
  if (!sp) {
    box.innerHTML = `<div class="hint">${inCents() ? "Enter the price in cents (a number between 1 and 99, for example 52 for 52¢ or 52%), or the payout your trade slip shows for your spend." : "Enter American odds: a number of 100 or more, with a minus sign for favourites (for example -110, or 250 for +250)."}</div>`;
    return;
  }
  const dec = sp.dec, pw = E.p_win_all, ph = E.p_hit;
  const ev = pw * dec + (ph - pw) - 1;
  const st = settings();
  const b = dec - 1;
  const f = Math.max(0, Math.min(((b * pw - (1 - ph)) / b) * st.kelly_fraction, st.kelly_cap));
  const stake = parseFloat(S.slipStake);
  let html = "";
  if (sp.from === "payout") html += `<div class="kv" title="Your spend divided by the payout: the fee is already in it"><span class="k">That works out to, per contract</span><span class="v">${fmtCents(sp.cost)}</span></div>`;
  if (sp.from === "price") html += `<div class="kv" title="The price you typed plus the fee"><span class="k">Cost of one contract, fee included</span><span class="v">${fmtCents(sp.cost)}${sp.contracts != null ? ` <span class="muted" style="font-weight:400">· ${money(sp.spent, 2)} buys ${sp.contracts}</span>` : ""}</span></div>`;
  if (sp.payout != null && stake > 0) html += `<div class="kv"><span class="k">Pays if it wins</span><span class="v">${money(sp.payout, 2)}</span></div>`;
  // a bet on a game that has started, or at a line other than the tool's: the chance above is not for this bet
  const legs = S.slip.map((k) => S.legs[k]).filter(Boolean);
  const moved = slipLinesNow(), alts = E.alts || {};
  const offLine = legs.some((l) => l.started || (moved[l.key] != null && !(alts[l.key] && alts[l.key].priced)));
  if (offLine) {
    box.innerHTML = `${html}<div class="hint">No verdict for this one: it has a game that has started${legs.some((l) => !l.started && moved[l.key] != null) ? ", or a line the tool could not price" : ""}, so the chance shown above is not the chance of this exact bet. It can still be recorded and followed.</div>`;
    return;
  }
  html += `<div class="kv"><span class="k">Expected return at ${inCents() ? "this price" : "these odds"}</span><span class="v ${cls(ev)}">${spct(ev)}${stake > 0 ? ` (${money(ev * (sp.spent || stake), 2)})` : ""}</span></div>`;
  if (sp.from === "price" && sp.contracts === 0) html += `<div class="hint">That spend does not buy one contract at this price.</div>`;
  if (f > 0) html += `<div class="hint verdict good">${ev < 0.02 ? `A small edge at ${inCents() ? "this price" : "these odds"}: worth it only if every line and price still matches your ${inCents() ? "app" : "sportsbook"}.` : `Worth a bet at ${inCents() ? "this price" : "these odds"}.`} ${S.bankroll ? `Suggested stake for your $${S.bankroll.toLocaleString()} bankroll: <button data-act="use-stake" data-v="${(S.bankroll * f).toFixed(2)}">${money(S.bankroll * f, 2)}</button>` : `Suggested stake: ${pct(f)} of your bankroll (set your bankroll at the top to see dollars).`}</div>`;
  else html += `<div class="hint verdict bad">${inCents() ? `Not worth it at this price: this bet loses money on average. A contract is worth at most ${fmtCents(100 * pw)} with the fee in.` : `Not worth it at these odds: this bet loses money on average. It needs better than ${fmtOdds(E.fair_odds)}.`}</div>`;
  box.innerHTML = html;
}

async function toggleLeg(key) {
  if (inSlip(key)) S.slip = S.slip.filter((k) => k !== key);
  else S.slip.push(key);
  saveSlip();
  $$(`[data-act="toggle"][data-key="${CSS.escape(key)}"]`).forEach((b) => {
    const on = inSlip(key);
    b.classList.toggle("on", on);
    if (b.classList.contains("add")) b.textContent = on ? "✓" : "+";
  });
  S.slipStake = S.slipStake ?? null;
  await evalSlip();
  renderSlip();
  // tablets open the slip when a pick is added; on a phone it would cover the page, so the button shows the count instead
  const w = document.documentElement.clientWidth;
  if (w <= 1100 && w > 640 && S.slip.length) { $("#slip").classList.add("open"); $("#slipToggle").classList.remove("empty"); }
}

// ============================================================================ events
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-act]");
  if (!a) return;
  const act = a.dataset.act;
  if (act === "nav") { if (location.hash.slice(1) === a.dataset.page) { renderMain(); window.scrollTo(0, 0); } else location.hash = a.dataset.page; return; }
  if (act === "nav-sport") { S.pb = { tab: "", game: "", stat: "", q: "", limit: 40 }; location.hash = `props/${a.dataset.sport}`; return; }
  if (act === "live-refresh") { a.disabled = true; await loadLive(); return; }
  if (act === "odds-refresh") { a.disabled = true; await loadOdds(); return; }
  if (act === "moves-game") {
    S.movesGame = a.dataset.id;
    if (S.page !== "moves") { location.hash = "moves"; return; }
    $("#main").innerHTML = pageMoves(); afterRender();
    const c = document.getElementById("sec-chart"); if (c) c.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }
  if (act === "guide") { e.preventDefault(); renderGuide(); const d = $("#guide"); if (!d.open) d.showModal(); return; }
  if (act === "guide-close") { $("#guide").close(); return; }
  if (act === "intro-ok") { S.introSeen = true; localStorage.setItem("intro_seen", "1"); renderMain(); return; }
  if (act === "jump") { const t = document.getElementById(a.dataset.to); if (t) { if (t.tagName === "SECTION") { const d = $("details", t); if (d && !d.open) d.open = true; } t.scrollIntoView({ block: "start", behavior: "smooth" }); } return; }
  if (act === "sport") { S.sport = a.dataset.sport; localStorage.setItem("sport", S.sport); S.track = null; renderMain(); return; }
  if (act === "prop-sport") { S.form.market = ""; S.form.player = ""; S.form.line = ""; S.proj.team = ""; S.pb = { tab: "", game: "", stat: "", q: "", limit: 40 }; S.priceErrors = []; location.hash = `props/${a.dataset.sport}`; return; }
  if (act === "game-props") {
    const id = `${a.dataset.sport}|${a.dataset.game}`;
    S.pv = "game"; S.pb.q = ""; S.pgOpen = { [id]: true }; S.focusGame = `pg-${a.dataset.sport}-${a.dataset.game}`;
    if (location.hash === `#props/${a.dataset.sport}`) renderMain(); else location.hash = `props/${a.dataset.sport}`;
    return;
  }
  if (act === "st-stat" || act === "st-more") {
    const id = a.dataset.id, cur = S.st[id] || {};
    S.st[id] = act === "st-stat" ? { stat: a.dataset.stat, all: false } : { ...cur, all: !cur.all };
    const g = S.started.find((x) => `${x.sport}|${x.game_id}` === id), el = g && document.getElementById(`st-${g.sport}-${g.game_id}`);
    if (el) el.outerHTML = startedCard(g);
    return;
  }
  if (act === "go-started") { S.focusGame = a.dataset.to; if (S.page === "slate") renderMain(); else location.hash = "slate"; return; }
  if (act === "gp-stat") { S.gp[a.dataset.gp] = a.dataset.stat; S.gpAll[a.dataset.gp] = false; redrawGameProps(a.dataset.gp); return; }
  if (act === "gp-more") { S.gpAll[a.dataset.gp] = !S.gpAll[a.dataset.gp]; redrawGameProps(a.dataset.gp); return; }
  if (act === "pv") { S.pv = a.dataset.v; localStorage.setItem("props_view", S.pv); renderMain(); return; }
  if (act === "pg") { S.pg = a.dataset.v; localStorage.setItem("props_group", S.pg); renderMain(); return; }
  if (act === "pg-all") {
    const sp = S.slate.sports.find((x) => x.sport === S.form.sport);
    S.pgOpen = {};
    if (a.dataset.v === "1" && sp) for (const g of sp.games) S.pgOpen[`${sp.sport}|${g.game_id}`] = true;
    renderMain(); return;
  }
  if (act === "pg-jump") {
    const id = a.dataset.pg;
    S.pgOpen = { ...(S.pgOpen || {}), [id]: true };
    S.focusGame = `pg-${id.replace("|", "-")}`;
    renderMain(); return;
  }
  if (act === "toggle") { e.preventDefault(); await toggleLeg(a.dataset.key); return; }
  if (act === "px-mode") {
    if (a.dataset.mode === PX.mode) return;
    const was = typedOddsRaw(S.slipOdds);
    PX.mode = a.dataset.mode;
    localStorage.setItem("px_mode", PX.mode);
    S.slipOdds = isNaN(was) ? S.slipOdds : boxPrice(was);  // the same price, written the other way
    S.hedgeOdds = {}; S.form.over = ""; S.form.under = "";
    syncPx(); renderMain(); renderSlip();
    if ($("#guide").open) renderGuide();
    toast(inCents() ? "Prices are now shown in cents per contract" : "Prices are now shown as American odds");
    return;
  }
  if (act === "import-toggle") { S.importOpen = !S.importOpen; renderSlip(); if (S.importOpen) { const t = $("#impText"); if (t) t.focus(); } return; }
  if (act === "import-read") {
    const text = ($("#impText") || {}).value || "";
    if (!text.trim()) { toast("Paste the text of your bet slip first"); return; }
    a.disabled = true;
    try {
      const res = await api("/api/slip/import", { method: "POST", body: JSON.stringify({ text }) });
      const picks = res.picks.filter((x) => S.legs[x.key]);
      if (picks.length) {
        S.slip = picks.map((x) => x.key);
        S.slipLines = {};
        for (const x of picks) if (x.line != null) S.slipLines[x.key] = x.line;
        S.keepOdds = res.odds;  // the odds on the slip, not the standard price (shown in cents if that is the setting)
        if (res.stake != null) S.slipStake = res.stake;
        S.slipPayout = res.payout != null ? res.payout : "";
        // a trade slip with a payout under every pick is several separate bets, not one
        S.importSingles = (res.singles || []).filter((x) => S.legs[x.key]).length > 1 ? res.singles.filter((x) => S.legs[x.key]) : null;
        S.importOpen = false; S.importText = "";
        saveSlip(); await evalSlip();
      } else { S.importText = text; S.importSingles = null; }
      S.importNote = { n: picks.length, odds: res.odds, stake: res.stake, unmatched: res.unmatched };
      if (window.innerWidth <= 1100) $("#slip").classList.add("open");
      renderSlip(); renderMain();
    } catch (err) { toast(String(err.message || err)); a.disabled = false; }
    return;
  }
  if (act === "import-singles-record") {
    const list = (S.importSingles || []).filter((x) => S.legs[x.key] && x.odds != null && x.stake > 0);
    if (!list.length) return;
    a.disabled = true;
    let done = 0;
    try {
      for (const x of list) {
        await api("/api/bets", { method: "POST", body: JSON.stringify({ keys: [x.key], odds: x.odds, stake: x.stake, lines: x.line != null ? { [x.key]: x.line } : {} }) });
        done += 1;
      }
      toast(`Recorded ${done} separate bets. Follow them on the Live page.`);
    } catch (err) { toast(`${done} recorded, then: ${String(err.message || err)}`); }
    S.importSingles = null; S.importNote = null; S.slip = []; S.slipLines = {}; S.slipEval = null; S.slipPayout = ""; S.live = null; S.odds = null; S.track = null;
    saveSlip(); renderSlip(); renderMain();
    return;
  }
  if (act === "import-singles-combo") { S.importSingles = null; renderSlip(); return; }
  if (act === "clear-slip") { S.slipLines = {}; S.importNote = null; S.importSingles = null; S.slipPayout = ""; S.slip = []; saveSlip(); S.slipEval = null; renderSlip(); $$('[data-act="toggle"].on').forEach((b) => { b.classList.remove("on"); if (b.classList.contains("add")) b.textContent = "+"; }); return; }
  if (act === "ptab") { S.parlayTab = a.dataset.tab; renderMain(); return; }
  if (act === "ltab") { S.likelyTab = a.dataset.tab; renderMain(); return; }
  if (act === "pb-tab") { S.pb.tab = a.dataset.tab; S.pb.limit = 40; renderMain(); return; }
  if (act === "pb-more") { S.pb.limit += 100; renderMain(); return; }
  if (act === "add-play") {
    let keys = [];
    try { keys = JSON.parse(a.dataset.keys || "[]"); } catch (err) { keys = []; }
    keys = keys.filter((k) => S.legs[k]);
    if (!keys.length) return;
    S.slip = [...new Set([...S.slip, ...keys])];
    saveSlip(); await evalSlip(); renderSlip(); renderMain();
    toast(`${keys.length} picks added to the bet slip`);
    return;
  }
  if (act === "refresh") {
    try { await api("/api/run", { method: "POST", body: JSON.stringify({ sports: ["nfl", "nba"] }) }); } catch (err) { toast(String(err.message || err)); }
    if (S.slate && S.slate.sports.length) $("#main").classList.add("dim");
    poll();
    return;
  }
  if (act === "slip-toggle") {
    const open = $("#slip").classList.toggle("open");
    a.classList.toggle("empty", !S.slip.length && !open);  // on phones the button hides while the slip is empty and closed
    return;
  }
  if (act === "days") { S.trackDays = +a.dataset.days || 0; S.track = null; renderMain(); return; }
  if (act === "grade") {
    a.disabled = true; a.textContent = "Settling ...";
    try { const r = await api("/api/grade", { method: "POST" }); toast(`Settled ${r.settled} picks${r.bets ? ` and ${r.bets} of your bets` : ""}`); } catch (err) { toast(String(err.message || err)); }
    S.track = null; renderMain(); return;
  }
  if (act === "del-bet") {
    if (!confirm("Delete this bet record? This only removes it from your tracking.")) return;
    try { await api(`/api/bets/${encodeURIComponent(a.dataset.id)}`, { method: "DELETE" }); toast("Deleted"); S.odds = null; } catch (err) { toast(String(err.message || err)); }
    S.track = null; renderMain(); return;
  }
  if (act === "record-bet") {
    const sp = slipPrice();
    if (!sp || !(parseFloat(S.slipStake) > 0)) { toast(inCents() ? "Enter what you spent, and the price or the payout, first" : "Enter the odds and a stake first"); return; }
    if (sp.from === "price" && !sp.contracts) { toast("That spend does not buy one contract at this price"); return; }
    const odds = sp.odds, stake = sp.spent;  // in cents only whole contracts are bought, so what was spent can be a little under what was typed
    try {
      await api("/api/bets", { method: "POST", body: JSON.stringify({ keys: S.slip, odds, stake, lines: slipLinesNow() }) });
      toast("Recorded. Follow it on the Live page.");
      S.live = null; S.odds = null;
      S.slip = []; saveSlip(); S.slipEval = null; S.slipStake = null; S.track = null; S.slipLines = {}; S.importNote = null; S.slipPayout = ""; S.importSingles = null;
      renderSlip(); renderMain();
    } catch (err) { toast(String(err.message || err)); }
    return;
  }
  if (act === "fix-save" || act === "fix-reset") {
    const box = a.closest("details.fix"), grid = $(".fix-grid", box);
    const body = { sport: grid.dataset.sport, game_id: grid.dataset.game, reset: act === "fix-reset" };
    if (!body.reset) for (const i of $$("input", grid)) if (i.value !== "") body[i.name] = i.dataset.price ? typedOdds(i.value) : parseFloat(i.value);
    if (!body.reset && Object.values(body).some((x) => typeof x === "number" && isNaN(x))) { toast(inCents() ? "Prices must be between 1 and 99 cents" : "Odds must be American odds, such as -110 or +120"); return; }
    a.disabled = true;
    try {
      await api("/api/games/line", { method: "POST", body: JSON.stringify(body) });
      S.fixOpen = null;
      await loadSlate();
      toast(body.reset ? "Back to the feed's line" : "Re-priced at your sportsbook's line");
    } catch (err) { toast(String(err.message || err)); a.disabled = false; }
    return;
  }
  if (act === "use-stake") { S.slipStake = a.dataset.v; const i = $("#slipStake"); if (i) i.value = a.dataset.v; updateSlipCalc(); return; }
  if (act === "proj-sort") { const c = a.dataset.col; S.proj.dir = S.proj.sort === c ? -S.proj.dir : -1; S.proj.sort = c; renderMain(); return; }
  if (act === "prefill" || act === "price-row") {
    S.form.player = a.dataset.player;
    if (a.dataset.market) S.form.market = a.dataset.market;
    if (a.dataset.line) S.form.line = a.dataset.line;
    if (S.page !== "props") {  // from a game on the home page: open the pricing form on the Player props page
      S.form.over = ""; S.form.under = ""; S.focusForm = true;
      location.hash = `props/${a.dataset.sport || S.form.sport}`;
      return;
    }
    const set = (id, v) => { const i = $(id); if (i && v != null) i.value = v; };
    set("#pf-player", S.form.player); set("#pf-market", S.form.market); set("#pf-line", S.form.line);
    const target = act === "price-row" ? $("#pf-over") : $("#pf-line");
    if (target) { target.scrollIntoView({ block: "center", behavior: "smooth" }); target.focus({ preventScroll: true }); }
    return;
  }
  if (act === "price-one") {
    const f = S.form;
    const line = parseFloat(f.line);
    if (!f.player || isNaN(line)) { S.priceErrors = ["Enter a player and a line."]; renderMain(); return; }
    if (f.over === "" && f.under === "") { S.priceErrors = ["Enter the odds for at least one side (for example -110)."]; renderMain(); return; }
    await priceLines([{ sport: f.sport, player: f.player, market: f.market, line, over_odds: f.over === "" ? null : typedOdds(f.over), under_odds: f.under === "" ? null : typedOdds(f.under), same_book: f.from !== "other" }]);
    return;
  }
  if (act === "price-many") {
    const { lines, bad } = parsePaste(S.form.paste || "", S.form.sport);
    if (!lines.length) { S.priceErrors = ["No valid lines found. Format: player, stat, line, over odds, under odds"]; renderMain(); return; }
    await priceLines(lines);
    if (bad.length) { S.priceErrors = S.priceErrors.concat(bad.map((b) => `Could not read: ${b}`)); renderMain(); }
  }
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.id === "bankroll") { S.bankroll = parseFloat(t.value) || 0; localStorage.setItem("bankroll", S.bankroll); updateSlipCalc(); clearTimeout(window._br); window._br = setTimeout(() => { if (S.page === "slate") renderMain(); if (S.page === "live") for (const id of Object.keys(S.offers)) checkOffer(id, true); }, 400); return; }
  if (t.dataset.hedge) {
    S.hedgeOdds[t.dataset.hedge] = t.value;
    const b = S.live && S.live.bets.find((x) => x.id === t.dataset.hedge), box = document.getElementById(`hedge-${t.dataset.hedge}`);
    if (b && box) box.innerHTML = hedgeCalc(b);
    return;
  }
  if (t.classList.contains("offer")) {
    const id = t.dataset.bet;
    S.offers[id] = t.value; localStorage.setItem("offers", JSON.stringify(S.offers));
    clearTimeout(window._co); window._co = setTimeout(() => checkOffer(id, true), 350);
    return;
  }
  if (t.id === "slipOdds") { S.slipOdds = t.value; S.slipOddsTyped = true; updateSlipCalc(); return; }
  if (t.dataset.lineKey) { S.slipLines[t.dataset.lineKey] = t.value; updateSlipCalc(); return; }
  if (t.id === "impText") { S.importText = t.value; return; }
  if (t.dataset.px) {
    PX[t.dataset.px] = Math.max(parseFloat(t.value) || 0, 0);
    localStorage.setItem(`px_${t.dataset.px}`, PX[t.dataset.px]);
    const ex = $("#px-example"); if (ex) ex.innerHTML = feeExample();
    updateSlipCalc();
    clearTimeout(window._px); window._px = setTimeout(() => { renderMain(); renderSlip(); }, 500);
    return;
  }
  if (t.id === "slipStake") { S.slipStake = t.value; updateSlipCalc(); return; }
  if (t.id === "slipPayout") { S.slipPayout = t.value; updateSlipCalc(); return; }
  const bind = t.dataset.bind;
  if (!bind) return;
  const [obj, key] = bind.split(".");
  S[obj][key] = t.value;
  if ((t.dataset.rerender && t.type === "search") || (obj === "all" && key === "q")) {
    clearTimeout(window._rr);
    window._rr = setTimeout(() => { if (obj === "pb") S.pb.limit = 40; renderMain(); const i = $(`[data-bind="${bind}"]`); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }, 250);
  }
});

document.addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.lineKey) {
    S.slipLines[t.dataset.lineKey] = t.value;
    const kept = S.slipOdds, typed = S.slipOddsTyped;
    await evalSlip();
    // A price the user typed stays. The tool's own price belongs to its own line, so once a pick is taken at
    // another line the box is emptied rather than left judging the bet at a price that is not for it.
    const priced = Object.keys(slipLinesNow()).length > 0;
    S.slipOdds = typed ? kept : priced ? "" : S.slipOdds;
    S.slipOddsTyped = typed;
    renderSlip();
    return;
  }
  if (t.dataset.jump && t.value) { const el = document.getElementById(t.value); t.value = ""; if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); return; }
  if (t.dataset.bindPlay === "game") { S.parlayGame = t.value; renderMain(); return; }
  if (t.dataset.pgjump && t.value) { const id = t.value; S.pgOpen = { ...(S.pgOpen || {}), [id]: true }; S.focusGame = `pg-${id.replace("|", "-")}`; renderMain(); return; }
  const bind = t.dataset.bind;
  if (bind && t.tagName === "SELECT") {
    const [obj, key] = bind.split(".");
    S[obj][key] = t.value;
    if (obj === "pb") S.pb.limit = 40;
    if (bind === "form.from") localStorage.setItem("line_from", t.value);
    if (t.dataset.rerender || obj === "all") renderMain();
  }
});

document.addEventListener("toggle", (e) => {
  if (e.target.dataset && e.target.dataset.why) S.whyOpen[e.target.dataset.why] = e.target.open;
  if (e.target.dataset && e.target.dataset.gpar) S.gparOpen[e.target.dataset.gpar] = e.target.open;
  if (e.target.dataset && e.target.dataset.box) S.boxOpen[e.target.dataset.box] = e.target.open;
  if (e.target.dataset && e.target.dataset.pg) {
    const id = e.target.dataset.pg, was = !!(S.pgOpen || {})[id];
    S.pgOpen = { ...(S.pgOpen || {}), [id]: e.target.open };
    if (e.target.open && !was && !$(".gp-wrap", e.target)) redrawPropGame(id);  // its lines are drawn when first opened
  }
  if (e.target.classList && e.target.classList.contains("fix")) { if (e.target.open) S.fixOpen = e.target.dataset.fix; else if (S.fixOpen === e.target.dataset.fix) S.fixOpen = null; }
  if (e.target.id === "allBets") S.all.open = e.target.open;
  if (e.target.id === "projBox") S.proj.open = e.target.open;
  if (e.target.open && S.page === "model") drawBars();
}, true);
$("#guide").addEventListener("click", (e) => { if (e.target.id === "guide") e.target.close(); }); // click outside closes
window.addEventListener("hashchange", () => { readHash(); renderMain(); $("#main").focus({ preventScroll: true }); window.scrollTo(0, 0); });
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(afterRender, 150); });

// ============================================================================ boot
$("#bankroll").value = S.bankroll || "";
syncPx();
renderStatus();
renderMain();
renderSlip();
poll();
