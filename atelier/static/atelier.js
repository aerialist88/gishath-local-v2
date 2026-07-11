/* The Deckwright's Atelier — frontend. Vanilla JS single-page app:
   hash routing, SSE-driven live view, no build step. */
"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const VIEW = $("#view");
const HEAD_SUB = $("#head-sub");
const HEAD_RIGHT = $("#head-right");
const HEADER = $("#app-header");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (v, cur = "SGD") => v == null ? "—" :
  (cur === "$" ? "$" + Number(v).toFixed(4) : cur + " " + Number(v).toFixed(2));

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
};

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

/* Commander art: fills .art-ph[data-art] placeholders once the index warms up. */
const artCache = {};
async function fillArt(root) {
  for (const ph of (root || document).querySelectorAll(".art-ph[data-art]")) {
    const name = ph.dataset.art;
    const kind = ph.dataset.artKind || "art_crop";
    if (!name) continue;
    try {
      if (!(name in artCache)) {
        const res = await fetch("/api/art?name=" + encodeURIComponent(name));
        artCache[name] = res.ok ? await res.json() : null;
      }
      const entry = artCache[name];
      const url = entry && (entry[kind] || entry.normal || entry.art_crop);
      if (url && !ph.querySelector("img")) {
        const img = document.createElement("img");
        img.src = url;
        img.alt = name;
        ph.appendChild(img);
      }
    } catch (e) { /* placeholder stays — art is never load-bearing */ }
  }
}

/* Floating card-image preview on hover — any element with class "card-name"
   and a data-card="<exact name>" attribute gets one. Uses the same artCache/
   /api/art lookup as the thumbnail placeholders (fillArt), just rendered at
   full "normal" size in a box that follows the cursor. One shared element,
   appended to <body> (not inside VIEW) so it survives route changes without
   needing to be recreated. */
let cardPreviewEl = null;

function ensureCardPreview() {
  if (!cardPreviewEl) {
    cardPreviewEl = document.createElement("div");
    cardPreviewEl.className = "card-preview";
    cardPreviewEl.innerHTML = "<img alt=''>";
    document.body.appendChild(cardPreviewEl);
  }
  return cardPreviewEl;
}

function positionCardPreview(el, x, y) {
  const margin = 16;
  const w = 240, h = Math.round(240 * (680 / 488)); // Scryfall card aspect ratio
  let left = x + margin;
  let top = y - h / 2;
  if (left + w > window.innerWidth - margin) left = x - w - margin;
  left = Math.max(margin, left);
  top = Math.min(Math.max(margin, top), window.innerHeight - h - margin);
  el.style.left = left + "px";
  el.style.top = top + "px";
}

async function showCardPreview(name, x, y) {
  const el = ensureCardPreview();
  el.dataset.card = name;
  positionCardPreview(el, x, y);
  if (!(name in artCache)) {
    try {
      const res = await fetch("/api/art?name=" + encodeURIComponent(name));
      artCache[name] = res.ok ? await res.json() : null;
    } catch { artCache[name] = null; }
  }
  if (el.dataset.card !== name) return; // the cursor moved to a different card while this was in flight
  const entry = artCache[name];
  const url = entry && (entry.normal || entry.art_crop);
  if (!url) { el.style.display = "none"; return; }
  el.querySelector("img").src = url;
  el.style.display = "block";
}

function hideCardPreview() {
  if (cardPreviewEl) { cardPreviewEl.style.display = "none"; cardPreviewEl.dataset.card = ""; }
}

function wireCardPreviews(container) {
  if (container.dataset.previewWired) return; // container persists across tab switches — wire once
  container.dataset.previewWired = "1";
  let currentCard = null;

  container.addEventListener("mouseover", (e) => {
    const el = e.target.closest(".card-name");
    if (!el || !el.dataset.card || el.dataset.card === currentCard) return;
    currentCard = el.dataset.card;
    showCardPreview(currentCard, e.clientX, e.clientY);
  });
  container.addEventListener("mousemove", (e) => {
    if (!currentCard || !e.target.closest(".card-name")) return;
    positionCardPreview(ensureCardPreview(), e.clientX, e.clientY);
  });
  container.addEventListener("mouseout", (e) => {
    const el = e.target.closest(".card-name");
    if (!el || el.contains(e.relatedTarget)) return;
    currentCard = null;
    hideCardPreview();
  });
}

/* ── stage gauge ─────────────────────────────────────────────────────────── */

const GAUGE_STAGES = ["Select", "Draft", "Judge", "Validate", "Optimize", "Price", "Deliver"];

/* Map a claude-call label (e.g. "draft/attempt1/2") or a coarse run stage to
   a gauge index. Old ideate/synthesize/build labels (pre widen-back-out runs
   still replayable from the event log) map onto the nearest new column. */
function stageIndexOf(label) {
  const l = (label || "").toLowerCase();
  if (l.startsWith("select") || l.startsWith("mechanic")) return 0;
  // repair checked before draft/build: "draft/attempt1/repair-2" is Validate work.
  if (l.includes("repair") && !l.includes("synergy") && !l.includes("budget")) return 3;
  if (l.startsWith("draft") || l.startsWith("ideate") || l.startsWith("build") || l.startsWith("edhrec")) return 1;
  if (l.startsWith("judge") || l.startsWith("synthesize")) return 2;
  if (l.startsWith("validate")) return 3;
  if (l.startsWith("optimize") || l.includes("synergy")) return 4;
  if (l.startsWith("price") || l.startsWith("budget") || l.startsWith("card_tag")) return 5;
  if (l.startsWith("export") || l.startsWith("deliver")) return 6;
  if (l.startsWith("scryfall") || l.startsWith("startup")) return 0;
  return null;
}

/* Bench personas — the in-world names for each pipeline stage's workbench. */
function benchPersona(label) {
  const l = (label || "").toLowerCase();
  // draft = the widened-back-out parallel deckwrights; ideate = pre-2026-07-06
  // runs still replayable from their event logs.
  const draft = l.match(/^(?:draft|ideate)\/attempt\d+\/(\d)/);
  if (draft) {
    const n = Number(draft[1]);
    const names = [null, ["C", "Deckwright Cog"], ["V", "Deckwright Verse"], ["F", "Deckwright Flux"]];
    const angles = [null, "drafting the hundred · first line", "drafting the hundred · second line",
      "drafting the hundred · third line"];
    const p = names[n] || ["D", "Deckwright"];
    return { initial: p[0], name: p[1], angle: angles[n] || "drafting bench" };
  }
  // Checked BEFORE the generic "select" branch below — "select/mechanic-tokens"
  // also starts with "select", so the order here matters (previously this
  // branch was unreachable and every mechanic-token call showed up mislabeled
  // as "The Guildmaster — choosing tonight's commander").
  if (l.startsWith("select/mechanic") || l.includes("mechanic-token"))
    return {
      initial: "L", name: "The Lexicographer", angle: "naming the commander's mechanics",
      quiet: "(a quick keyword pass — little to narrate here)",
    };
  if (l.startsWith("select")) return { initial: "G", name: "The Guildmaster", angle: "choosing tonight's commander" };
  if (l.startsWith("judge")) return { initial: "J", name: "The Adjudicator", angle: "judging the three drafts" };
  // Pre-widen-back-out labels, kept so old runs replay with sensible benches:
  if (l.startsWith("synthesize")) return { initial: "S", name: "The Scrivener", angle: "merging the three angles" };
  if (l.startsWith("build")) return { initial: "B", name: "The Builder", angle: "drafting the hundred" };
  if (l.includes("synergy-repair")) return { initial: "M", name: "The Master Deckwright", angle: "synergy mending" };
  if (l.includes("repair") || l.startsWith("validate"))
    return { initial: "M", name: "The Master Deckwright", angle: labelSuffix(l, "repair pass") };
  if (l.startsWith("optimize")) return { initial: "O", name: "The Optimizer", angle: "swap-delta passes" };
  if (l.startsWith("budget")) return { initial: "P", name: "The Purser", angle: "minding the per-card cap" };
  if (l.startsWith("card_tag"))
    return {
      initial: "A", name: "The Archivist", angle: "tagging every card",
      quiet: "(a quick labeling pass — little to narrate here)",
    };
  return { initial: (label || "?")[0].toUpperCase(), name: label, angle: "at the bench" };
}

function labelSuffix(l, prefix) {
  const m = l.match(/repair-(\d+)/);
  return m ? `${prefix} ${m[1]}` : prefix;
}

/* Which pixel figure works a bench — the four characters from the "Deck
   Artificer Explorations" prototype (static/px/), mapped onto the personas.
   Only four sprites exist, so several personas share a figure: the brown-robed
   master covers every elder role (select / judge / validate / repair). */
function benchSpriteChar(label) {
  const l = (label || "").toLowerCase();
  const draft = l.match(/^(?:draft|ideate)\/attempt\d+\/(\d)/);
  if (draft) return ["cog", "verse", "flux"][(Number(draft[1]) - 1) % 3];
  if (l.startsWith("select/mechanic") || l.includes("mechanic-token")) return "verse";
  if (l.startsWith("card_tag") || l.startsWith("build")) return "cog";
  if (l.startsWith("optimize")) return "flux";
  if (l.startsWith("budget")) return "verse";
  return "master";
}

/* ── router ─────────────────────────────────────────────────────────────── */

let liveES = null;      // active EventSource
let liveTicker = null;  // elapsed-clock interval
let statusCache = null;

function stopLive() {
  if (liveES) { liveES.close(); liveES = null; }
  if (liveTicker) { clearInterval(liveTicker); liveTicker = null; }
}

async function route() {
  stopLive();
  hideCardPreview();
  HEADER.classList.remove("halted");
  const hash = location.hash || "#home";
  const [name, arg] = hash.slice(1).split("/");
  document.querySelectorAll("#main-nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === name || (name === "deck" && a.dataset.nav === "gallery"));
  });
  try {
    if (name === "live") await viewLive();
    else if (name === "deck" && arg) await viewDeck(arg);
    else if (name === "gallery") await viewGallery();
    else if (name === "match") await viewMatch();
    else if (name === "rules") await viewRules();
    else await viewHome();
  } catch (err) {
    VIEW.innerHTML = `<div class="page"><div class="empty-note">Something jammed in the workshop: ${esc(err.message)}</div></div>`;
  }
}
window.addEventListener("hashchange", route);

/* ── home / commission ──────────────────────────────────────────────────── */

async function viewHome() {
  const st = await api("/api/status");
  statusCache = st;
  if (st.running) { location.hash = "#live"; return; }

  HEAD_SUB.textContent = "";
  HEAD_RIGHT.innerHTML = `<span>night ${st.night_no}${st.nightly_enabled ? " · next nightly run " + esc(st.nightly_time) : ""}</span>`;

  const latest = st.latest_deck;
  const k = st.knobs;
  const bracketNames = { "1": "Exhibition", "2": "Core", "3": "Upgraded", "3-4": "Upgraded", "4": "Optimized", "5": "cEDH" };

  VIEW.innerHTML = `<div class="page">
    <div class="hero">
      <div class="hero-left">
        <div>
          <div class="caps-lg" style="margin-bottom:6px">Commission No. ${st.night_no}</div>
          <div class="hero-title">What shall the guild artifice tonight?</div>
          <div class="hero-blurb" style="margin-top:6px">Name a commander, or let the guild choose one — commanders forged within the last ${k.dedupe_days} days are excluded from the draw.</div>
        </div>
        <div class="commission-row">
          <div class="commander-field">
            <div class="commander-input"><span>&#8981;</span>
              <input id="commander-input" type="text" placeholder="Name a commander&hellip; e.g. &ldquo;Braids, Arisen Nightmare&rdquo;" autocomplete="off">
            </div>
            <div class="suggest-box" id="commander-suggest" style="display:none"></div>
          </div>
          <button class="btn-guild" id="btn-guild">Let the guild choose <span class="die"></span></button>
        </div>
        <div class="knobs">
          <div class="knob"><span class="caps">Bracket</span><b>${esc(k.bracket)} — ${bracketNames[k.bracket] || ""}</b><small>guild rules, editable</small></div>
          <div class="knob"><span class="caps">Budget</span><b>&le; ${money(k.deck_budget_sgd)}</b><small>per-card cap SGD ${Number(k.max_card_price_sgd).toFixed(0)}</small></div>
          <div class="knob"><span class="caps">Colors</span><b>Any</b><small>guild's discretion</small></div>
        </div>
        <button class="btn-dark" id="btn-begin" style="align-self:flex-start">Begin the commission &rarr;</button>
        <span class="demo-link" id="btn-demo">or run a rehearsal — the full live view, no API spend</span>
        ${st.pricing_up === false ? `<div class="warn-chip" style="align-self:flex-start"><span>⚠</span><span>The pricing scrapers (make run, port 5003) aren't up — a commission tonight would ship unpriced.</span></div>` : ""}
      </div>
      ${latest ? `
      <div class="panel fresh-plaque">
        <div class="caps">Fresh from the forge — last night</div>
        <div class="fresh-body">
          <div class="art-ph" style="width:72px;height:100px" data-art="${esc(latest.commander)}" data-art-kind="normal"><span>commander<br>art</span></div>
          <div>
            <div class="fresh-name">${esc(latest.commander)}</div>
            <div class="fresh-arch">${esc(latest.archetype)}</div>
            <div class="fresh-meta">${money(latest.total_sgd)}<br>${esc(latest.ts.replace("_", " · ").replace(/-/g, "/"))}${latest.legal ? " · legal ✓" : ""}</div>
          </div>
        </div>
        <button class="btn-ghost" onclick="location.hash='#deck/${esc(latest.id)}'">Open in the gallery &rarr;</button>
      </div>` : `<div class="panel fresh-plaque"><div class="caps">Fresh from the forge</div><div class="empty-note" style="padding:20px 0">Nothing yet — commission the first deck.</div></div>`}
    </div>
    <div class="shelf-head">
      <div class="caps-lg">The gallery — recent commissions</div>
      <span class="mono" style="font-size:10.5px;color:var(--faint)">${st.night_no - 1} decks forged</span>
    </div>
    ${st.decks.length ? `<div class="shelf">${st.decks.slice(0, 10).map((d) => `
      <div class="shelf-card" onclick="location.hash='#deck/${esc(d.id)}'">
        <div class="art-ph" data-art="${esc(d.commander)}"><span>art crop</span></div>
        <div class="shelf-name">${esc(d.commander)}</div>
        <div class="shelf-arch">${esc(d.archetype)}</div>
        <div class="shelf-meta">${esc(d.ts.slice(0, 10))} · ${money(d.total_sgd)}</div>
      </div>`).join("")}</div>` :
      `<div class="empty-note">The shelves are bare — the first commission awaits.</div>`}
  </div>`;

  const begin = async (commander) => {
    try {
      await api("/api/commission", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commander: commander || null }),
      });
      location.hash = "#live";
    } catch (err) { toast(err.message); }
  };
  $("#btn-guild").onclick = () => begin(null);
  $("#btn-begin").onclick = () => begin($("#commander-input").value.trim());
  wireCommanderAutocomplete($("#commander-input"), $("#commander-suggest"), begin);
  $("#btn-demo").onclick = async () => {
    try {
      await api("/api/commission", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ demo: true }),
      });
      location.hash = "#live";
    } catch (err) { toast(err.message); }
  };
  fillArt(VIEW);
}

/* Commander name autocomplete — searches /api/commanders (a local Scryfall
   index of commander-eligible cards, same eligibility check select-time
   validation uses) as the user types. Debounced, keyboard-navigable,
   click-outside-to-close; Enter submits the commission either way (the
   active suggestion if the dropdown is open, otherwise whatever was typed). */
function wireCommanderAutocomplete(input, box, onSubmit) {
  let items = [];
  let activeIdx = -1;
  let debounceTimer = null;
  let requestSeq = 0;

  const colorLabel = (colors) => (colors && colors.length ? colors.join("") : "C");

  const close = () => {
    box.style.display = "none";
    box.innerHTML = "";
    items = [];
    activeIdx = -1;
  };

  const render = () => {
    if (!items.length) { close(); return; }
    box.innerHTML = items.map((it, i) => `<div class="suggest-item${i === activeIdx ? " active" : ""}" data-idx="${i}">
      <span>${esc(it.name)}</span><span class="colors">${esc(colorLabel(it.colors))}</span>
    </div>`).join("");
    box.style.display = "block";
    box.querySelectorAll(".suggest-item").forEach((el) => {
      el.onmousedown = (e) => { // mousedown, not click — fires before the input's blur closes the box
        e.preventDefault();
        input.value = items[Number(el.dataset.idx)].name;
        close();
      };
    });
  };

  input.addEventListener("input", () => {
    const q = input.value.trim();
    clearTimeout(debounceTimer);
    if (q.length < 2) { close(); return; }
    debounceTimer = setTimeout(async () => {
      const seq = ++requestSeq;
      try {
        const results = await api("/api/commanders?q=" + encodeURIComponent(q));
        if (seq !== requestSeq) return; // a newer keystroke's response already landed
        items = results;
        activeIdx = -1;
        render();
      } catch { /* suggestions are a nicety — a failed lookup just shows none */ }
    }, 150);
  });

  input.addEventListener("keydown", (e) => {
    if (box.style.display === "block" && items.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, items.length - 1); render(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); return; }
      if (e.key === "Escape") { close(); return; }
      if (e.key === "Enter" && activeIdx >= 0) { e.preventDefault(); input.value = items[activeIdx].name; close(); return; }
    }
    if (e.key === "Enter") { close(); onSubmit(input.value.trim()); }
  });

  // Self-unregistering: viewHome() re-renders VIEW.innerHTML on every visit,
  // which detaches this exact `input` node without ever calling back here to
  // clean up — checking document.body.contains() lets a stale listener from
  // a previous render remove itself instead of accumulating forever.
  document.addEventListener("click", function onOutsideClick(e) {
    if (!document.body.contains(input)) { document.removeEventListener("click", onOutsideClick); return; }
    if (e.target !== input) close();
  });
}

/* ── live run (workshop) ────────────────────────────────────────────────── */

async function viewLive() {
  let snap;
  try { statusCache = await api("/api/status"); } catch { /* keep the stale one */ }
  try {
    snap = await api("/api/run/snapshot");
  } catch (e) {
    // No run in memory — maybe the last one halted before a restart.
    try {
      snap = await api("/api/run/last_failed");
      renderLive(snap, true);
      return;
    } catch (e2) {
      VIEW.innerHTML = `<div class="page"><div class="empty-note">The workshop is quiet — no commission on the bench.<br><br>
        <button class="btn-dark" onclick="location.hash='#home'">Commission a deck &rarr;</button></div></div>`;
      HEAD_SUB.textContent = "";
      HEAD_RIGHT.innerHTML = "";
      return;
    }
  }
  renderLive(snap, false);
  if (snap.status === "running") {
    subscribeLive(snap);
  }
}

function liveState(snap) {
  /* derived helpers shared by render + SSE updates */
  const calls = snap.call_order.map((l) => snap.calls[l]).filter(Boolean);
  const active = calls.filter((c) => !c.done);
  const finished = calls.filter((c) => c.done);
  const cost = calls.reduce((s, c) => s + (c.cost_usd || 0), 0);
  return { calls, active, finished, cost };
}

let liveBaselineSeq = 0; // events below this are already baked into the rendered snapshot

function renderLive(snap, postMortem) {
  liveBaselineSeq = snap.next_seq || 0;
  const failed = snap.status === "failed" || snap.status === "cancelled";
  const { calls, active, finished, cost } = liveState(snap);
  const commissionNo = statusCache ? statusCache.night_no : "—";

  HEADER.classList.toggle("halted", failed);
  HEAD_SUB.textContent = failed
    ? `Commission · halted`
    : snap.status === "delivered" ? "Commission · delivered" : `Commission · in progress${snap.demo ? " · rehearsal" : ""}`;

  if (failed) { renderFailure(snap, postMortem); return; }

  HEAD_RIGHT.innerHTML = `
    <div class="head-stat"><span class="caps">Elapsed</span><b id="stat-elapsed">—</b></div>
    <div class="head-stat"><span class="caps">Spend</span><b id="stat-spend">$${cost.toFixed(4)}</b></div>
    <div class="head-stat"><span class="caps">Calls</span><b id="stat-calls">${finished.length} / ~14</b></div>`;

  const concept = snap.concept;
  const gaugeIdx = currentGaugeIndex(snap);

  VIEW.innerHTML = `<div class="page">
    <div class="commission-plaque">
      <div class="art-ph" style="width:86px;height:120px" data-art="${esc(concept ? concept.commander : "")}" data-art-kind="normal"><span>commander<br>art</span></div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="caps-lg">Tonight the guild artifices</div>
        <div class="cp-name" id="cp-name">${concept ? esc(concept.commander) : '<span class="pulse">choosing&hellip;</span>'}</div>
        <div class="cp-concept" id="cp-concept">${concept ? esc(concept.rationale || concept.archetype) : "the guildmaster considers the draw"}</div>
      </div>
      <div class="head-spacer"></div>
      <div class="cp-pills" id="cp-pills">${concept ? conceptPills(concept) : ""}</div>
    </div>
    <div class="gauge" id="gauge">${gaugeHTML(gaugeIdx, snap)}</div>
    <div class="caps-lg live-section-title" id="benches-title">${benchesTitle(snap, active)}</div>
    <div class="live-cols">
      <div class="benches" id="benches">${active.length ? active.map((c) => benchHTML(c)).join("") :
        (snap.status === "running" ? emptyBenchesHTML() : snap.status === "delivered" ? finaleHTML(snap) : "")}</div>
      <aside style="display:flex;flex-direction:column;gap:14px">
        <div class="ledger" id="ledger">
          <div class="caps" style="margin-bottom:8px">Ledger — the night so far</div>
          <div id="ledger-rows">${finished.map((c) => ledgerRowHTML(c, false)).join("")}</div>
        </div>
        <div class="plaque-dark crucible" id="crucible">${crucibleHTML(cost)}</div>
        ${snap.status === "running" && !snap.demo ? `<button class="btn-ghost btn-rust" id="btn-abandon">Abandon commission</button>` : ""}
        ${snap.demo ? `<div class="fail-note">A rehearsal — scripted apprentices, not the real guild.</div>` : ""}
      </aside>
    </div>
    ${(snap.budget_swaps || []).length ? `<div class="budget-swaps" id="budget-swaps">
      <div class="caps-lg" style="margin-bottom:10px">Where the purse was minded — budget swaps</div>
      ${snap.budget_swaps.map((sw) => swapRowHTML(sw, false)).join("")}
    </div>` : `<div class="budget-swaps" id="budget-swaps"></div>`}
    <div id="delivered-slot">${snap.status === "delivered" ? deliveredHTML(snap) : ""}</div>
  </div>`;

  const abandonBtn = $("#btn-abandon");
  if (abandonBtn) abandonBtn.onclick = async () => {
    if (!confirm("Abandon this commission? The current bench finishes, then the run halts.")) return;
    try { await api("/api/run/abandon", { method: "POST" }); } catch (err) { toast(err.message); }
  };

  // elapsed clock
  const t0 = (snap.started_ts || (Date.now() / 1000)) * 1000;
  const tick = () => {
    const el = $("#stat-elapsed");
    if (el) {
      const s = Math.max(0, Math.floor((Date.now() - t0) / 1000));
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }
    // Live requery each tick (not cached) — picks up benches appended after
    // this interval was set up, and naturally stops touching a placeholder
    // the moment call_text clears its "placeholder" class/attribute.
    document.querySelectorAll(".stream-text.placeholder[data-waiting-since]").forEach((span) => {
      const since = Number(span.dataset.waitingSince);
      if (!since) return;
      const secs = Math.max(0, Math.floor((Date.now() - since) / 1000));
      span.textContent = `(waiting for the first line… ${secs}s)`;
    });
  };
  tick();
  if (snap.status === "running") liveTicker = setInterval(tick, 1000);
  fillArt(VIEW);
}

function conceptPills(concept) {
  const colorNames = { W: "White", U: "Blue", B: "Black", R: "Red", G: "Green" };
  const colors = (concept.colors || []).map((c) => colorNames[c] || c);
  const colorLabel = colors.length === 0 ? "Colorless" :
    colors.length === 1 ? "Mono-" + colors[0] : colors.join(" ");
  return `<div class="pill">${esc(colorLabel)}</div><div class="pill">${esc(concept.archetype || "")}</div>`;
}

function currentGaugeIndex(snap) {
  if (snap.status === "delivered") return GAUGE_STAGES.length;
  let idx = stageIndexOf(snap.stage);
  // calls are finer-grained than run.py's coarse stages — take the max signal
  for (const label of snap.call_order) {
    const i = stageIndexOf(label);
    if (i != null && (idx == null || i > idx)) idx = i;
  }
  return idx == null ? 0 : idx;
}

function gaugeHTML(activeIdx, snap) {
  return GAUGE_STAGES.map((name, i) => {
    const cls = i < activeIdx ? "done" : i === activeIdx ? "active" : "";
    const glyph = i < activeIdx ? "✓" : i === activeIdx ? "⚙" : String(i + 1);
    return `<div class="gauge-seg ${cls}" data-idx="${i}">
      <div class="gauge-node"><div class="gauge-dot">${glyph}</div><div class="gauge-label">${name}</div></div>
      ${i < GAUGE_STAGES.length - 1 ? '<div class="gauge-rail"></div>' : ""}
    </div>`;
  }).join("");
}

/* Patches the ALREADY-RENDERED gauge nodes in place (classList + glyph text)
   rather than rebuilding gaugeHTML() from scratch — the CSS transitions on
   .gauge-dot/.gauge-rail/.gauge-label only animate when the same element
   persists across the state change; a full rebuild would swap in a fresh
   node already in its final state and nothing would visibly move. */
function updateGaugeDOM(activeIdx) {
  const gauge = $("#gauge");
  if (!gauge) return;
  gauge.querySelectorAll(".gauge-seg").forEach((seg) => {
    const i = Number(seg.dataset.idx);
    const dot = seg.querySelector(".gauge-dot");
    const cls = i < activeIdx ? "done" : i === activeIdx ? "active" : "";
    seg.classList.remove("done", "active");
    if (cls) seg.classList.add(cls);
    dot.textContent = i < activeIdx ? "✓" : i === activeIdx ? "⚙" : String(i + 1);
  });
}

function benchesTitle(snap, active) {
  if (snap.status === "delivered") return "The work is done — the commission is delivered";
  if (!active.length) return "Between benches — the guild confers";
  const l = active[0].label.toLowerCase();
  if (l.startsWith("draft") && !l.includes("repair"))
    return "The deckwrights convene — three benches, three whole decks, drafted in parallel";
  if (l.startsWith("ideate")) return "The apprentices convene — three ideation benches, working in parallel";
  if (l.includes("repair")) return "The master inspects the work — flaws found, repair underway";
  if (l.startsWith("judge")) return "The adjudicator weighs the three drafts";
  if (l.startsWith("build")) return "The builder drafts the hundred";
  if (l.startsWith("optimize")) return "The optimizer fact-checks and fine-tunes";
  if (l.startsWith("select")) return "The guildmaster chooses tonight's commander";
  return "At the bench";
}

/* Shown in #benches whenever no call is currently in flight (between
   stages) — matches the "guild reads the commission" beat from the design
   exploration, reused generically for any inter-stage gap rather than
   one specific moment. */
function emptyBenchesHTML() {
  return `<div class="benches-waiting">
    <div class="px-sprite px-cog"></div>
    <div class="px-sprite px-verse"></div>
    <div class="px-sprite px-flux"></div>
  </div>`;
}

/* The concluding tableau — once delivered, the whole guild gathers in the
   space where the benches stood to admire the finished deck. The mirrored
   right-hand pair turns everyone in toward the card. */
function finaleHTML(snap) {
  const commander = snap.concept ? snap.concept.commander : "";
  return `<div class="guild-finale">
    <div class="finale-scene">
      <span class="finale-fig"><span class="px-sprite px-cog"></span></span>
      <span class="finale-fig"><span class="px-sprite px-master"></span></span>
      <div class="finale-card"><div class="art-ph flip-in" data-art="${esc(commander)}" data-art-kind="normal"><span>commander<br>art</span></div></div>
      <span class="finale-fig"><span class="px-sprite px-verse"></span></span>
      <span class="finale-fig"><span class="px-sprite px-flux"></span></span>
    </div>
    <div class="finale-caption">The guild gathers to admire the night&rsquo;s work.</div>
  </div>`;
}

function swapRowHTML(sw, animate) {
  const addedStr = sw.added_price != null ? `SGD ${Number(sw.added_price).toFixed(2)}` : "unpriced";
  return `<div class="swap-row${animate ? " entering" : ""}">
    <span class="from">${esc(sw.remove)}</span><span class="arrow">→</span>
    <span class="to">${esc(sw.add)}</span>
    <span class="reason">${esc(sw.reason || "")} · ${addedStr}</span>
  </div>`;
}

function benchHTML(call, entering) {
  const p = benchPersona(call.label);
  const hasText = !!(call.text_tail && call.text_tail.length);
  const hasThink = !!(call.thinking_tail && call.thinking_tail.length);
  // Quiet stages (mechanic-tokens, card tagging) are EXPECTED to stay near-empty —
  // showing a growing timer there would read as "why is a quick pass taking so
  // long?". Only the generic placeholder gets a live counter, so a genuinely
  // slow first token (large prompt context, e.g. build/optimize) reads as "still
  // working" rather than "might be stuck". A visible thinking stream also
  // silences the timer — the reasoning itself is the proof of life.
  const showTimer = !hasText && !hasThink && !p.quiet;
  const streamText = hasText ? esc(call.text_tail)
    : (p.quiet || (hasThink ? "(the reply follows the reasoning…)" : "(waiting for the first line…)"));
  const waitingAttr = showTimer && call.started_at ? ` data-waiting-since="${Math.round(call.started_at * 1000)}"` : "";
  const thinkHTML = hasThink
    ? `<div class="stream-think"><span class="caps think-caps">The reasoning</span><span class="tt">${esc(call.thinking_tail.slice(-1200))}</span></div>`
    : "";
  return `<div class="bench ${call.is_error ? "errored" : ""}${entering ? " bench-entering" : ""}" data-label="${esc(call.label)}">
    <div class="bench-head">
      <div class="bench-badge">${esc(p.initial)}</div>
      <div><div class="bench-name">${esc(p.name)}</div><div class="bench-angle">${esc(p.angle)}</div></div>
      <div class="head-spacer"></div>
      <span class="model-chip">${esc(call.model)}</span>
    </div>
    <div class="bench-stream">${thinkHTML}<div><span class="stream-text${hasText ? "" : " placeholder"}"${waitingAttr}>${streamText}</span>${call.done ? "" : '<span class="caret"></span>'}</div></div>
    <div class="bench-foot">
      <div class="px-sprite working px-${benchSpriteChar(call.label)}"></div>
      <span class="pulse status">⚙ ${esc(call.status)}</span><span class="head-spacer"></span>
      <span class="meta">${call.done ? `${call.duration_s.toFixed(0)}s · $${call.cost_usd.toFixed(4)} · ${call.num_turns} turns` : ""}</span>
    </div>
  </div>`;
}

function ledgerRowHTML(call, entering) {
  const glyph = call.is_error ? "✕" : "✓";
  const cls = call.is_error ? "err" : "ok";
  return `<div class="ledger-row${entering ? " entering" : ""}" data-label="${esc(call.label)}"><span class="glyph ${cls}">${glyph}</span>
    <span class="stage-name">${esc(call.label)}</span><span class="dotlead"></span>
    <span>${call.duration_s.toFixed(0)}s · $${call.cost_usd.toFixed(4)}</span></div>`;
}

function crucibleCapUsd() {
  const s = statusCache && statusCache.knobs;
  return s && Number(s.max_run_spend_usd) > 0 ? Number(s.max_run_spend_usd) : null;
}

function crucibleHTML(cost) {
  const capUsd = crucibleCapUsd();
  const pct = capUsd ? Math.min(100, (cost / capUsd) * 100) : Math.min(100, cost * 20);
  return `<div class="caps" style="color:var(--brass);margin-bottom:8px">Crucible spend</div>
    <div class="amount" id="crucible-amount">$${cost.toFixed(4)}</div>
    <div class="crucible-bar"><div id="crucible-fill" style="width:${pct}%"></div></div>
    <div class="mono" style="font-size:9.5px;color:var(--faint);margin-top:6px">${capUsd ? "cap $" + capUsd.toFixed(2) : "no cap set — Guild rules can set one"}</div>`;
}

/* Patches the existing crucible bar/amount nodes in place (not a fresh
   crucibleHTML() re-render) so the width change actually transitions instead
   of jumping — see .crucible-bar div's CSS transition. */
function updateCrucibleDOM(cost) {
  const amount = $("#crucible-amount");
  const fill = $("#crucible-fill");
  if (!amount || !fill) return;
  amount.textContent = "$" + cost.toFixed(4);
  const capUsd = crucibleCapUsd();
  fill.style.width = (capUsd ? Math.min(100, (cost / capUsd) * 100) : Math.min(100, cost * 20)) + "%";
}

function deliveredHTML(snap) {
  const d = snap.delivered || {};
  const deckId = d.deck_id || "";
  const commander = snap.concept ? snap.concept.commander : "";
  return `<div class="panel" style="margin-top:24px;padding:22px;display:flex;flex-direction:column;gap:14px">
    <div style="display:flex;align-items:center;gap:20px;perspective:600px">
      <div class="art-ph flip-in" style="width:64px;height:88px" data-art="${esc(commander)}" data-art-kind="normal"><span>commander<br>art</span></div>
      <div>
        <span class="seal-badge" style="margin-bottom:8px">✓ Delivered</span>
        <div class="serif" style="font-size:24px;font-weight:700;margin-top:8px">The commission is complete.</div>
        <div class="mono" style="font-size:11px;color:var(--ink3);margin-top:6px">run cost $${(d.cost_usd || 0).toFixed(4)} · ${d.turns || 0} turns</div>
      </div>
      <div class="head-spacer"></div>
      ${deckId ? `<button class="btn-brass" onclick="location.hash='#deck/${esc(deckId)}'">Open the deck &rarr;</button>` : ""}
      <button class="btn-ghost" onclick="location.hash='#home'">Back to the atelier</button>
    </div>
    ${d.email_error ? `<div class="warn-chip" style="align-self:flex-start"><span>⚠</span><span>The report email didn't send (saved locally instead) — ${esc(d.email_error)}</span></div>` : ""}
  </div>`;
}

/* Client-side mirror of RunEventLog's state — mirrors runner.py's
   RunEventLog._apply() so structural events (a bench starting/finishing, a
   stage advancing) can patch the ALREADY-RENDERED DOM in place instead of
   re-fetching a snapshot and rebuilding the whole live view from scratch.
   That full-rebuild approach (the original implementation) is why the
   animation pass from the design exploration couldn't just be dropped in as
   CSS: a fresh node has no "before" state for a transition to animate from,
   and a one-shot entrance animation would replay on every single re-render
   instead of once. Only delivered/failed (one true screen-shape change per
   run) and a couple of defensive fallbacks still go through a full re-render. */
let liveMirror = null;

function applyEventToMirror(s, e) {
  switch (e.type) {
    case "run_started":
      s.forced_commander = e.forced_commander ?? null;
      s.demo = !!e.demo;
      break;
    case "stage":
      s.stage = e.stage;
      break;
    case "concept":
      s.concept = { commander: e.commander, archetype: e.archetype, rationale: e.rationale, colors: e.colors };
      break;
    case "budget_swaps":
      s.budget_swaps = (s.budget_swaps || []).concat(e.swaps || []);
      break;
    case "call_started":
      s.calls[e.label] = { label: e.label, model: e.model, text_tail: "", thinking_tail: "",
                           status: "thinking...",
                           done: false, is_error: false, cost_usd: 0, num_turns: 0, duration_s: 0,
                           started_at: e.t };
      s.call_order.push(e.label);
      break;
    case "call_text": {
      const call = s.calls[e.label];
      if (call) call.text_tail = (call.text_tail + e.chunk).slice(-4000);
      break;
    }
    case "call_thinking": {
      const call = s.calls[e.label];
      if (call) call.thinking_tail = (call.thinking_tail + e.chunk).slice(-4000);
      break;
    }
    case "call_status": {
      const call = s.calls[e.label];
      if (call) call.status = e.status;
      break;
    }
    case "call_finished": {
      const call = s.calls[e.label];
      if (call) Object.assign(call, { done: true, is_error: e.is_error, cost_usd: e.cost_usd,
                                      num_turns: e.num_turns, duration_s: e.duration_s });
      break;
    }
    // delivered/failed are handled by a full re-render (see applyLiveEvent) —
    // no need to mirror their fields locally.
  }
}

function subscribeLive(snap) {
  // Defensive: two overlapping EventSources (e.g. a hashchange listener and
  // an explicit route() call both landing on viewLive() in the same tick)
  // would each independently apply every event, silently duplicating bench
  // cards and doubling streamed text — close any prior connection first.
  if (liveES) { liveES.close(); liveES = null; }
  liveMirror = JSON.parse(JSON.stringify(snap)); // deep copy — this tab's own live-updating state
  const since = snap.next_seq || 0;
  const es = new EventSource(`/api/run/events?since=${since}`);
  liveES = es;

  es.onmessage = (msg) => {
    let e;
    try { e = JSON.parse(msg.data); } catch { return; }
    if (typeof e.seq === "number" && e.seq < liveBaselineSeq) return; // already in the snapshot we rendered
    applyEventToMirror(liveMirror, e);
    applyLiveEvent(e);
  };
  es.addEventListener("done", async () => {
    es.close();
    liveES = null;
    // final re-render from an authoritative snapshot (delivered / failed states)
    try {
      const fresh = await api("/api/run/snapshot");
      renderLive(fresh, false);
    } catch { /* ignore */ }
  });
  es.onerror = () => { /* EventSource auto-reconnects; snapshot re-render on done covers gaps */ };
}

/* Defensive fallback only — used when an incremental patch expected a DOM
   node that wasn't there (e.g. this tab missed an earlier event, or the
   live view wasn't open yet when subscribeLive() started). Not part of the
   normal per-event path anymore. */
let rerenderQueued = false;
async function rerenderSoon() {
  if (rerenderQueued) return;
  rerenderQueued = true;
  setTimeout(async () => {
    rerenderQueued = false;
    try {
      const fresh = await api("/api/run/snapshot");
      if ((location.hash || "#home").startsWith("#live")) renderLive(fresh, false);
      if (fresh.status === "running") {
        if (!liveES) subscribeLive(fresh);
        // subscribeLive() above already reset the mirror to `fresh` when it
        // (re)subscribes; if we were already subscribed, do it here instead
        // so the mirror doesn't drift from the DOM this fallback just redrew.
        else liveMirror = JSON.parse(JSON.stringify(fresh));
      }
    } catch { /* ignore */ }
  }, 120);
}

function refreshLiveStats() {
  const calls = liveMirror.call_order.map((l) => liveMirror.calls[l]).filter(Boolean);
  const finished = calls.filter((c) => c.done);
  const cost = calls.reduce((sum, c) => sum + (c.cost_usd || 0), 0);
  const statSpend = $("#stat-spend");
  const statCalls = $("#stat-calls");
  if (statSpend) statSpend.textContent = "$" + cost.toFixed(4);
  if (statCalls) statCalls.textContent = finished.length + " / ~14";
  updateCrucibleDOM(cost);
}

function refreshBenchesTitle() {
  const el = $("#benches-title");
  if (!el) return;
  const active = liveMirror.call_order.map((l) => liveMirror.calls[l]).filter((c) => c && !c.done);
  el.textContent = benchesTitle(liveMirror, active);
}

function appendBenchNode(call) {
  const container = $("#benches");
  if (!container) return;
  if (container.querySelector(`.bench[data-label="${CSS.escape(call.label)}"]`)) return; // already there
  const waiting = container.querySelector(".benches-waiting");
  if (waiting) waiting.remove();
  container.insertAdjacentHTML("beforeend", benchHTML(call, true));
}

/* Removes a finished call's bench card and appends its one-line ledger
   summary in its place — the real-world analogue of the design's flaw rows
   "resolving": the bench disappears, the fact that it happened persists. */
function collapseCallToLedger(call) {
  const rows = $("#ledger-rows");
  if (rows && rows.querySelector(`.ledger-row[data-label="${CSS.escape(call.label)}"]`)) return; // already logged

  const bench = document.querySelector(`.bench[data-label="${CSS.escape(call.label)}"]`);
  if (bench) bench.remove();
  const container = $("#benches");
  if (container && !container.querySelector(".bench")) container.innerHTML = emptyBenchesHTML();

  // "beforeend" — matches the bulk-render order (oldest first, top to bottom).
  if (rows) rows.insertAdjacentHTML("beforeend", ledgerRowHTML(call, true));
}

function appendBudgetSwapRows(swaps) {
  let panel = $("#budget-swaps");
  if (!panel) return;
  if (!panel.querySelector(".caps-lg")) {
    panel.insertAdjacentHTML("beforeend",
      `<div class="caps-lg" style="margin-bottom:10px">Where the purse was minded — budget swaps</div>`);
  }
  for (const sw of swaps) panel.insertAdjacentHTML("beforeend", swapRowHTML(sw, true));
}

function applyLiveEvent(e) {
  // Text/status patches stay exactly as before — highest-frequency path,
  // already correct (direct textContent mutation on a persisting node).
  switch (e.type) {
    case "call_text": {
      const bench = document.querySelector(`.bench[data-label="${CSS.escape(e.label)}"] .stream-text`);
      if (bench) {
        if (bench.classList.contains("placeholder")) {
          // First real chunk after the "(waiting for the first line…)" filler —
          // clear it rather than prepending real text onto the placeholder.
          bench.classList.remove("placeholder");
          bench.textContent = "";
        }
        bench.textContent = (bench.textContent + e.chunk).slice(-1200);
      } else rerenderSoon();
      return;
    }
    case "call_thinking": {
      const benchEl = document.querySelector(`.bench[data-label="${CSS.escape(e.label)}"]`);
      if (!benchEl) { rerenderSoon(); return; }
      const streamBox = benchEl.querySelector(".bench-stream");
      let think = streamBox.querySelector(".stream-think .tt");
      if (!think) {
        streamBox.insertAdjacentHTML("afterbegin",
          `<div class="stream-think"><span class="caps think-caps">The reasoning</span><span class="tt"></span></div>`);
        think = streamBox.querySelector(".stream-think .tt");
        // The reasoning stream is proof of life — retire the waiting timer and
        // quiet the placeholder (the class stays so the first call_text chunk
        // still clears it the normal way).
        const ph = streamBox.querySelector(".stream-text.placeholder");
        if (ph) {
          delete ph.dataset.waitingSince;
          ph.textContent = "(the reply follows the reasoning…)";
        }
      }
      think.textContent = (think.textContent + e.chunk).slice(-1200);
      return;
    }
    case "call_status": {
      const el = document.querySelector(`.bench[data-label="${CSS.escape(e.label)}"] .status`);
      if (el) el.textContent = "⚙ " + e.status; else rerenderSoon();
      return;
    }
  }

  if (!liveMirror || !(location.hash || "#home").startsWith("#live")) return; // not on the live screen — nothing to patch
  if (!$("#gauge")) { rerenderSoon(); return; } // live screen not actually painted yet — fall back once

  switch (e.type) {
    case "stage":
      updateGaugeDOM(currentGaugeIndex(liveMirror));
      refreshBenchesTitle();
      break;
    case "concept": {
      const nameEl = $("#cp-name"), conceptEl = $("#cp-concept"), pillsEl = $("#cp-pills");
      if (nameEl) nameEl.textContent = e.commander;
      if (conceptEl) conceptEl.textContent = e.rationale || e.archetype;
      if (pillsEl) pillsEl.innerHTML = conceptPills({ colors: e.colors, archetype: e.archetype });
      const art = document.querySelector(".commission-plaque .art-ph");
      if (art) art.dataset.art = e.commander;
      fillArt(document.querySelector(".commission-plaque"));
      break;
    }
    case "call_started": {
      const call = liveMirror.calls[e.label];
      if (call) appendBenchNode(call);
      updateGaugeDOM(currentGaugeIndex(liveMirror));
      refreshBenchesTitle();
      break;
    }
    case "call_finished": {
      const call = liveMirror.calls[e.label];
      if (call) collapseCallToLedger(call);
      updateGaugeDOM(currentGaugeIndex(liveMirror));
      refreshLiveStats();
      refreshBenchesTitle();
      break;
    }
    case "budget_swaps":
      appendBudgetSwapRows(e.swaps || []);
      break;
    case "delivered":
    case "failed":
      rerenderSoon(); // one true screen-shape change per run — a full redraw is correct here
      break;
    case "announce":
      break; // no visible surface in this UI (yet)
  }
}

/* ── failure (the forge gutters out) ────────────────────────────────────── */

function renderFailure(snap, postMortem) {
  const f = snap.failed || {};
  const { finished, cost } = liveState(snap);
  const cancelled = snap.status === "cancelled";
  const isCap = (f.error || "").includes("crucible cap");
  const stageIdx = stageIndexOf(f.stage) ?? currentGaugeIndex(snap);
  const benchName = GAUGE_STAGES[Math.min(stageIdx ?? 0, GAUGE_STAGES.length - 1)] || "workshop";
  const spendStages = f.spend_stages || finished.map((c) => ({ stage: c.label, cost_usd: c.cost_usd }));
  const maxSpend = Math.max(0.0001, ...spendStages.map((s) => s.cost_usd || 0));
  const s = statusCache && statusCache.knobs;
  const capUsd = s && Number(s.max_run_spend_usd) > 0 ? Number(s.max_run_spend_usd) : null;

  HEAD_RIGHT.innerHTML = `<div class="warn-chip"><span>⚠</span><span>${cancelled ? "The commission was abandoned" : isCap ? "The crucible ran dry — spend cap reached" : "The forge guttered out — the run halted"}</span></div>`;

  VIEW.innerHTML = `<div class="page">
    <div class="fail-cols">
      <div style="display:flex;flex-direction:column;gap:18px">
        <div>
          <div class="fail-headline">The work stopped at the ${esc(benchName)} bench.</div>
          <div class="fail-blurb" style="margin-top:8px">${esc(f.error || "The run halted without a reason on record.")}</div>
        </div>
        <div class="panel preserved">
          <div class="art-ph" style="width:56px;height:78px" data-art="${esc(snap.concept ? snap.concept.commander : "")}"><span>art</span></div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <span class="caps">${snap.concept ? "The commission" : "No commander was chosen yet"}</span>
            <span class="serif" style="font-weight:700;font-size:20px">${esc(snap.concept ? snap.concept.commander : "—")}</span>
            <span class="mono" style="font-size:10.5px;color:var(--ink3)">halted at the ${esc(f.stage || "?")} stage · $${(f.cost_usd || cost).toFixed(4)} spent</span>
          </div>
          <div class="head-spacer"></div>
          <div class="preserved-actions">
            ${isCap && capUsd ? `<button class="btn-brass" id="btn-raise">Raise cap to $${(capUsd * 1.5).toFixed(2)} &amp; recommission</button>` : ""}
            <button class="btn-ghost" id="btn-retry">${snap.concept ? "Recommission " + esc(snap.concept.commander) : "Commission again"}</button>
            <button class="btn-ghost btn-rust" onclick="location.hash='#home'">Abandon commission</button>
          </div>
        </div>
        ${spendStages.length ? `<div class="panel-plain spend-bars">
          <div class="caps" style="margin-bottom:12px">Where the crucible burned — spend by stage</div>
          ${spendStages.map((sb) => `<div class="spend-bar-row">
            <span class="lbl">${esc(sb.stage)}</span>
            <div class="spend-bar-track"><div class="${isCap && sb === spendStages[spendStages.length - 1] ? "hot" : ""}" style="width:${((sb.cost_usd || 0) / maxSpend * 100).toFixed(1)}%"></div></div>
            <span class="amt">$${(sb.cost_usd || 0).toFixed(4)}</span>
          </div>`).join("")}
        </div>` : ""}
      </div>
      <aside style="display:flex;flex-direction:column;gap:14px">
        <div class="ledger">
          <div class="caps" style="margin-bottom:8px">Ledger — how the night went</div>
          ${finished.map((c) => ledgerRowHTML(c, false)).join("") || '<div class="ledger-row">· no calls completed</div>'}
        </div>
        <div class="plaque-dark crucible dry">
          <div class="caps" style="color:#c98a5a;margin-bottom:8px">Crucible spend${isCap ? " — dry" : ""}</div>
          <div class="amount">$${(f.cost_usd || cost).toFixed(4)}</div>
          <div class="crucible-bar"><div style="width:${isCap ? 100 : Math.min(100, (f.cost_usd || cost) * 20)}%"></div></div>
          <div class="mono" style="font-size:9.5px;color:#c98a5a;margin-top:6px">${capUsd ? "cap $" + capUsd.toFixed(2) + " · " : ""}${finished.length} calls</div>
        </div>
        <div class="fail-note">A halt is never silent — the report email still goes out, marked incomplete, with everything the guild managed.</div>
      </aside>
    </div>
  </div>`;

  const retry = async (raiseCap) => {
    try {
      if (raiseCap && capUsd) {
        await api("/api/settings", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_run_spend_usd: Number((capUsd * 1.5).toFixed(2)) }),
        });
      }
      await api("/api/commission", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commander: snap.forced_commander || (snap.concept && snap.concept.commander) || null }),
      });
      location.hash = "#live";
      route();
    } catch (err) { toast(err.message); }
  };
  const raiseBtn = $("#btn-raise");
  if (raiseBtn) raiseBtn.onclick = () => retry(true);
  $("#btn-retry").onclick = () => retry(false);
  fillArt(VIEW);
}

/* ── finished deck ──────────────────────────────────────────────────────── */

async function viewDeck(id) {
  const deck = await api("/api/decks/" + encodeURIComponent(id));
  HEAD_SUB.textContent = "";
  HEAD_RIGHT.innerHTML = "";

  const p = deck.price || {};
  const flaggedCount = (deck.cards || []).filter((c) => c.over_cap).length;
  const delivered = (deck.generated_utc || "").slice(11, 16);
  const pills = deck.owner_deck ? [
    "3vor's own deck",
    "Imported decklist",
    `${(deck.cards || []).length} cards`,
  ] : [
    `Bracket ${esc(deck.bracket || "?")}`,
    deck.legal ? "Legal ✓" : "Legality unverified",
    deck.synergy_gate_fired ? "Synergy gate repaired" : "Synergy gate passed",
  ];

  VIEW.innerHTML = `<div class="page">
    <div class="deck-header">
      <div class="art-ph" style="width:92px;height:128px;box-shadow:0 3px 8px rgba(120,90,30,.25)" data-art="${esc(deck.commander)}" data-art-kind="normal"><span>commander<br>art</span></div>
      <div style="display:flex;flex-direction:column;gap:5px">
        <div class="caps-lg">${deck.owner_deck
          ? `From 3vor's collection · added ${esc((deck.generated_utc || "").slice(0, 10))}`
          : `Commission ${esc(deck.run_id8 || "")}${delivered ? " · Delivered " + delivered : ""}`}</div>
        <div class="deck-title">${esc(deck.commander)}</div>
        <div class="cp-concept">${esc(deck.archetype || "")}${deck.summary ? " — " + esc(deck.summary) : ""}</div>
        <div style="display:flex;gap:8px;margin-top:6px">${pills.map((x) => `<span class="pill">${x}</span>`).join("")}</div>
      </div>
      <div class="head-spacer"></div>
      <div class="deck-header-right">
        <div class="deck-price">${money(p.total_sgd)}</div>
        <div class="deck-price-note">${deck.owner_deck ? "already in 3vor's collection — nothing to price" :
          `cheapest across the store scrapers${flaggedCount ? ` · ${flaggedCount} card${flaggedCount > 1 ? "s" : ""} over cap, flagged` : ""}${p.unpriced_count ? ` · ${p.unpriced_count} unpriced` : ""}`}</div>
        <div style="display:flex;gap:8px">
          ${deck.files && deck.files.moxfield_txt ? `<a class="btn-brass" href="/api/decks/${esc(id)}/file/txt">Moxfield .txt</a>` : ""}
          ${deck.files && deck.files.xlsx ? `<a class="btn-ghost" href="/api/decks/${esc(id)}/file/xlsx">.xlsx</a>` : ""}
          ${priceDeckLink(deck)}
        </div>
      </div>
    </div>
    <div class="tabs" id="deck-tabs">
      ${["Decklist", "Breakdown", "Gameplan", "Stats"].map((t, i) => `<div class="tab ${i === 0 ? "active" : ""}" data-tab="${t.toLowerCase()}">${t}</div>`).join("")}
    </div>
    <div id="deck-body"></div>
  </div>`;

  const body = $("#deck-body");
  const tabs = { decklist: () => decklistTab(deck), breakdown: () => breakdownTab(deck), gameplan: () => gameplanTab(deck), stats: () => statsTab(deck) };
  const show = (name) => {
    document.querySelectorAll("#deck-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    body.innerHTML = tabs[name]();
    fillArt(body);
  };
  document.querySelectorAll("#deck-tabs .tab").forEach((t) => (t.onclick = () => show(t.dataset.tab)));
  wireCardPreviews(body); // delegated — covers every tab's card names, wired once
  show("decklist");
  fillArt(VIEW);
}

/* "Price on 3vor Fetch" — hands the whole decklist to the pricing app
   (port 5003) via a #list= URL fragment; its buy-list box prefills and the
   user fires the search themselves. Fragment, not query string: a 100-card
   list stays client-side with no length concerns. */
function priceDeckLink(deck) {
  const names = (deck.cards || []).map((c) => c.name).filter(Boolean);
  if (!names.length) return "";
  const url = "http://127.0.0.1:5003/#list=" + encodeURIComponent(names.join("\n"));
  return `<a class="btn-ghost" href="${esc(url)}" target="_blank" rel="noopener" title="Open 3vor Fetch with this decklist prefilled">Price on 3vor Fetch</a>`;
}

function groupByRole(cards) {
  const groups = new Map();
  for (const c of cards) {
    const role = c.is_commander ? "Commander" : (c.role || "Unsorted");
    if (!groups.has(role)) groups.set(role, []);
    groups.get(role).push(c);
  }
  return groups;
}

function decklistTab(deck) {
  const groups = groupByRole(deck.cards || []);
  const stats = deck.stats || {};
  const curve = stats.curve || {};
  const maxCurve = Math.max(1, ...Object.values(curve));
  const lands = (deck.cards || []).filter((c) => (c.type_line || "").toLowerCase().includes("land")).length;
  const nonland = (deck.cards || []).filter((c) => c.cmc != null && !(c.type_line || "").toLowerCase().includes("land"));
  const avgCmc = nonland.length ? (nonland.reduce((s, c) => s + c.cmc, 0) / nonland.length).toFixed(2) : "—";
  const spend = deck.spend || {};
  const top = (deck.price && deck.price.top_expensive) || [];
  const gameplanQuote = (deck.gameplan && deck.gameplan.early) || deck.summary || "";

  return `<div class="deck-cols">
    <div class="rolelist">
      ${[...groups.entries()].map(([role, cards]) => `
        <div class="rolegroup">
          <div class="rolegroup-head"><span class="serif">${esc(role)}</span><span class="count">${cards.length} card${cards.length > 1 ? "s" : ""}</span></div>
          ${cards.map((c) => `<div class="cardrow"><span class="card-name" data-card="${esc(c.name)}">${esc(c.name)}</span><span class="dotlead"></span>
            ${c.ck_price_usd != null ? `<span class="ck-ref" title="Card Kingdom (US) reference price">${c.ck_url ? `<a href="${esc(c.ck_url)}" target="_blank" rel="noopener">CK $${c.ck_price_usd.toFixed(2)}</a>` : `CK $${c.ck_price_usd.toFixed(2)}`}</span>` : ""}
            <span class="price ${c.over_cap ? "flag" : ""}">${c.price_sgd != null ? c.price_sgd.toFixed(2) + (c.over_cap ? " ⚑" : "") : "—"}</span></div>`).join("")}
        </div>`).join("")}
    </div>
    <aside class="rail">
      <div class="rail-panel">
        <div class="caps" style="margin-bottom:12px">Mana curve</div>
        <div class="curve">
          ${["0", "1", "2", "3", "4", "5", "6+"].map((b) => `
            <div class="curve-col"><span class="n">${curve[b] || 0}</span>
              <div class="bar" style="height:${Math.max(2, ((curve[b] || 0) / maxCurve) * 64)}px"></div>
              <span class="x">${b}</span></div>`).join("")}
        </div>
        <div class="mono" style="font-size:10.5px;color:var(--ink3);margin-top:10px">avg CMC ${avgCmc} · ${lands} lands</div>
      </div>
      ${top.length ? `<div class="rail-panel">
        <div class="caps" style="margin-bottom:12px">Priciest inclusions</div>
        ${top.map(([n, pr]) => `<div class="cardrow" style="font-size:12.5px"><span class="card-name" data-card="${esc(n)}">${esc(n)}</span><span class="dotlead"></span>
          <span class="price ${pr > (deck.price.per_card_cap_sgd || Infinity) ? "flag" : ""}">${pr.toFixed(2)}</span></div>`).join("")}
      </div>` : ""}
      <div class="note-plaque">
        <div class="caps" style="color:var(--brass);margin-bottom:8px">The artificers&rsquo; note</div>
        <div class="quote">&ldquo;${esc(gameplanQuote.slice(0, 220))}${gameplanQuote.length > 220 ? "&hellip;" : ""}&rdquo;</div>
        ${spend.total_cost_usd != null ? `<div class="mono" style="font-size:10px;color:var(--faint);margin-top:10px">run cost $${Number(spend.total_cost_usd).toFixed(2)} · ${spend.total_turns || 0} turns</div>` : ""}
      </div>
    </aside>
  </div>`;
}

function breakdownTab(deck) {
  return `<div style="padding:24px 0;overflow-x:auto">
    <table class="breakdown-table">
      <thead><tr><th>Card</th><th>SG Price</th><th>Store</th><th>CK Ref (US)</th><th>Role</th><th>Phase</th><th>CMC</th><th>Type</th><th>Rarity</th></tr></thead>
      <tbody>${(deck.cards || []).map((c) => `<tr>
        <td class="card-name" data-card="${esc(c.name)}"${c.is_commander ? ' style="font-weight:700"' : ""}>${esc(c.name)}</td>
        <td class="mono ${c.over_cap ? "flag" : ""}" style="${c.over_cap ? "color:var(--rust)" : "color:#065f46"}">${c.price_sgd != null ? "SGD " + c.price_sgd.toFixed(2) + (c.over_cap ? " ⚑" : "") : "unavailable"}</td>
        <td class="mono">${esc(c.store || "—")}</td>
        <td class="mono" style="color:var(--brass-dark)">${c.ck_price_usd != null ? (c.ck_url ? `<a href="${esc(c.ck_url)}" target="_blank" rel="noopener" style="color:inherit">USD ${c.ck_price_usd.toFixed(2)}</a>` : `USD ${c.ck_price_usd.toFixed(2)}`) : "—"}</td>
        <td>${esc(c.role)}</td><td>${esc(c.phase)}</td>
        <td class="mono">${c.cmc ?? ""}</td>
        <td style="color:var(--ink3)">${esc(c.type_line)}</td>
        <td>${esc((c.rarity || "").replace(/^\w/, (ch) => ch.toUpperCase()))}</td>
      </tr>`).join("")}</tbody>
    </table>
  </div>`;
}

function gameplanTab(deck) {
  const g = deck.gameplan || {};
  const sections = [
    ["Why this pick", deck.summary],
    ["Early game", g.early], ["Mid game", g.mid], ["Late game", g.late],
    ["Changes made during the optimize pass", g.changes_made],
  ].filter(([, txt]) => txt);
  return `<div class="gameplan-block">
    ${sections.map(([h, txt]) => `<div><h3>${esc(h)}</h3><p>${esc(txt)}</p></div>`).join("") || '<div class="empty-note">No gameplan on record for this commission.</div>'}
  </div>`;
}

function statsTab(deck) {
  const s = deck.stats || {};
  const pipNames = { W: "White", U: "Blue", B: "Black", R: "Red", G: "Green", C: "Colourless" };
  const spend = deck.spend || {};
  return `<div class="stats-grid">
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Mana curve (nonland)</div>
      <table class="stat-table">${["0","1","2","3","4","5","6+"].map((b) => `<tr><td>CMC ${b}</td><td>${(s.curve || {})[b] || 0}</td></tr>`).join("")}</table></div>
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Colour pips</div>
      <table class="stat-table">${Object.entries(pipNames).map(([k, n]) => `<tr><td>${n}</td><td>${(s.pips || {})[k] || 0}</td></tr>`).join("")}</table></div>
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Roles</div>
      <table class="stat-table">${Object.entries(s.role_counts || {}).sort((a, b) => b[1] - a[1]).map(([r, n]) => `<tr><td>${esc(r)}</td><td>${n}</td></tr>`).join("")}</table></div>
    ${spend.total_cost_usd != null ? `<div class="rail-panel"><div class="caps" style="margin-bottom:10px">The night's ledger</div>
      <table class="stat-table">
        <tr><td>API cost</td><td>$${Number(spend.total_cost_usd).toFixed(4)}</td></tr>
        <tr><td>Turns</td><td>${spend.total_turns || 0}</td></tr>
        ${spend.cache_hit_ratio != null ? `<tr><td>Cache hit ratio</td><td>${(spend.cache_hit_ratio * 100).toFixed(0)}%</td></tr>` : ""}
      </table></div>` : ""}
  </div>`;
}

/* ── match rehearsal ────────────────────────────────────────────────────── */

async function viewMatch() {
  const [decks, ruleStatus, engine] = await Promise.all([api("/api/decks"), api("/api/rules"), api("/api/engine").catch(() => ({}))]);
  const forge = !!engine.forge; // Forge games ignore the seed and never read the LLM grounding documents
  HEAD_SUB.textContent = "grounded Commander playtest";
  HEAD_RIGHT.innerHTML = "";
  const cards = decks.map((d, i) => `<label class="match-deck ${i < 4 ? "selected" : ""}">
    <input type="checkbox" value="${esc(d.id)}" ${i < 4 ? "checked" : ""}>
    <div class="art-ph" data-art="${esc(d.commander)}"><span>art</span></div>
    <span><b>${esc(d.commander)}</b><small>${esc(d.archetype || "Saved commission")}</small></span>
  </label>`).join("");
  VIEW.innerHTML = `<div class="page match-page">
    <div class="match-hero">
      <div><div class="caps-lg">Commander match simulation</div>
        <h1>Play the decks against each other, to a winner.</h1>
        <p>Choose 2–4 finished commissions. The game is played on the <b>Forge rules engine</b> — real zones, real priority, real combat, an AI that plays to win — and each deck gets an honest performance report at the end.${forge ? "" : " (Forge isn't installed, so the LLM referee will run this game.)"}</p>
      </div>
      <aside class="plaque-dark"><div class="caps" style="color:var(--plaque-text)">Engine truth</div>
        <p>Hands, libraries, and the battlefield are engine data structures, not narration — nothing can be hallucinated. The full typed game log is kept as receipts beside each session.</p>
      </aside>
    </div>
    ${decks.length >= 2 ? `<section class="match-panel">
      <div class="match-panel-head"><div><div class="caps">Seats at the table</div><h2>Choose your pod</h2></div><span id="match-count" class="pill">${Math.min(decks.length, 4)} selected</span></div>
      <div class="match-decks">${cards}</div>
      <div class="match-actions">${forge ? "" : `<label class="seed-field">Replay seed <input id="match-seed" inputmode="numeric" placeholder="Random"></label>`}<button class="btn-brass" id="btn-run-match">Play the match</button></div>
      <p class="match-note">Played to an actual winner — the deck reports at the end are how the Atelier grades its own builds.${forge ? "" : " Genuinely ambiguous rules moments are resolved reasonably and logged as judgement calls."}</p>
    </section>` : `<div class="empty-note">Forge at least two decks before staging a rehearsal.</div>`}
    ${forge ? "" : `<section class="guide-card"><div class="caps">Source documents</div><div><b>Commander Match Simulation Guide</b><span>deck_engine/prompts/commander_match_guide.md</span></div><div><b>Card Oracle text</b><span>Local Scryfall bulk cache — exact text is bundled only for the selected decks.</span></div><div><b>Magic Comprehensive Rules</b><span>${ruleStatus.available ? `local indexed copy · effective ${esc(ruleStatus.effective_date || "date unavailable")}` : "not cached yet"}</span><button class="rule-refresh" id="btn-refresh-rules">${ruleStatus.available ? "Refresh official rules" : "Download official rules"}</button></div></section>`}
    <div id="match-result"></div>
    <section id="match-history"></section>
  </div>`;
  fillArt(VIEW);
  const checks = [...VIEW.querySelectorAll(".match-deck input")];
  const updateCount = () => {
    const count = checks.filter((input) => input.checked).length;
    $("#match-count").textContent = `${count} selected`;
    checks.forEach((input) => input.closest(".match-deck").classList.toggle("selected", input.checked));
  };
  checks.forEach((input) => input.onchange = () => {
    if (checks.filter((item) => item.checked).length > 4) { input.checked = false; toast("A Commander pod has at most four seats here."); }
    updateCount();
  });
  const run = $("#btn-run-match");
  const refreshRules = $("#btn-refresh-rules");
  if (refreshRules) refreshRules.onclick = async () => {
    refreshRules.disabled = true; refreshRules.textContent = "Refreshing…";
    try { await api("/api/rules", { method: "POST" }); await viewMatch(); }
    catch (err) { refreshRules.disabled = false; refreshRules.textContent = "Refresh official rules"; toast(err.message); }
  };
  if (run) run.onclick = async () => {
    const deckIds = checks.filter((input) => input.checked).map((input) => input.value);
    const rawSeed = ($("#match-seed")?.value || "").trim();
    if (deckIds.length < 2) { toast("Choose at least two decks."); return; }
    if (rawSeed && !/^\d+$/.test(rawSeed)) { toast("Use a whole-number seed, or leave it blank."); return; }
    run.disabled = true;
    run.textContent = "Dealing the hands…";
    try {
      const session = await api("/api/simulations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ deck_ids: deckIds, seed: rawSeed || null }) });
      watchSimulation(session.id, run);
    } catch (err) { run.disabled = false; run.textContent = "Play the match"; toast(err.message); }
  };
  loadMatchHistory();
}

/* The rehearsal ledger — every past game, reopenable. Sessions persist as
   state/simulations/<id>.json on the deck_engine side, so the ledger survives
   server restarts; before this existed a result was only visible in the
   moments after its run finished. */
async function loadMatchHistory() {
  const rootEl = $("#match-history");
  if (!rootEl) return;
  let sessions = [];
  try { sessions = await api("/api/simulations"); } catch { return; } // ledger is decoration — never block the match page on it
  sessions = sessions.filter((s) => s.status !== "running");
  if (!sessions.length) { rootEl.innerHTML = ""; return; }
  rootEl.innerHTML = `<section class="match-panel" style="margin-top:26px">
    <div class="match-panel-head"><div><div class="caps">The rehearsal ledger</div><h2>Past games</h2></div><span class="pill">${sessions.length} on record</span></div>
    <div>${sessions.map((s) => `
      <button class="history-row" data-sim="${esc(s.id)}" style="display:flex;gap:14px;align-items:baseline;width:100%;text-align:left;background:none;border:0;border-top:1px solid rgba(120,90,30,.15);padding:10px 4px;cursor:pointer;font:inherit">
        <span class="mono" style="font-size:11px;color:var(--ink3);white-space:nowrap">${esc((s.created_utc || "").slice(0, 16).replace("T", " "))}</span>
        <b style="flex:1">${(s.commanders || []).map(esc).join(" · ")}</b>
        <span class="mono" style="font-size:11px;color:var(--ink3);white-space:nowrap">${s.seed != null ? "seed " + esc(s.seed) : "Forge engine"}</span>
        <span class="pill">${s.status === "complete" ? "verified" : "held"}</span>
      </button>`).join("")}</div>
  </section>`;
  rootEl.querySelectorAll(".history-row").forEach((row) => row.onclick = async () => {
    const resultEl = $("#match-result");
    try {
      const session = await api("/api/simulations/" + encodeURIComponent(row.dataset.sim));
      if (session.status === "failed") {
        resultEl.innerHTML = `<div class="match-error"><b>Rehearsal held for review.</b><span>${esc(session.error || "The evidence ledger could not be verified.")}</span></div>`;
      } else renderSimulation(resultEl, session);
      resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) { toast(err.message); }
  });
}

async function watchSimulation(id, button) {
  const resultEl = $("#match-result");
  resultEl.innerHTML = `<div class="match-running"><span class="pulse">Playing the game to a finish…</span><small>Forge engine games take about a minute (JVM boot + the whole game); the LLM fallback takes a few.</small></div>`;
  const poll = async () => {
    try {
      const session = await api("/api/simulations/" + encodeURIComponent(id));
      if (session.status === "running") { setTimeout(poll, 1500); return; }
      button.disabled = false; button.textContent = "Play another match";
      if (session.status === "failed") {
        resultEl.innerHTML = `<div class="match-error"><b>Rehearsal held for review.</b><span>${esc(session.error || "The evidence ledger could not be verified.")}</span></div>`;
      } else renderSimulation(resultEl, session);
      loadMatchHistory(); // the just-finished game joins the ledger immediately
    } catch (err) { button.disabled = false; button.textContent = "Play the match"; resultEl.innerHTML = `<div class="match-error">${esc(err.message)}</div>`; }
  };
  poll();
}

function renderSimulation(root, session) {
  const g = session.grounding || {}, result = session.result || {};
  // Pre-2026-07-10 sessions used the bounded-rehearsal shape (narration +
  // state per turn, no winner) — map them onto the game-log fields so old
  // ledger entries stay readable.
  const turns = (result.turns || []).map((t) => t.play != null ? t
    : { ...t, play: t.narration || "", life: (t.state || {}).life || [] });
  const seat = (n) => (g.players || []).find((p) => p.seat === n) || {};
  const winner = result.winner || {};
  // Every card name that appeared in this game becomes hoverable — same
  // floating art preview the deck views use (wireCardPreviews below). Names
  // are collected from engine facts only: cards played, commanders, key cards.
  const hoverNames = new Set();
  (g.players || []).forEach((p) => p.commander && hoverNames.add(p.commander));
  turns.forEach((t) => (t.cards_played || []).forEach((n) => n && hoverNames.add(n)));
  (result.deck_reports || []).forEach((r) => (r.key_cards || []).forEach((n) => n && hoverNames.add(n)));
  const escapedNames = [...hoverNames].map(esc).sort((a, b) => b.length - a.length)
    .map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const nameRe = escapedNames.length ? new RegExp("(" + escapedNames.join("|") + ")", "g") : null;
  const markCards = (txt) => {
    // "Seat N" -> "Player N" at display time, so sessions recorded before the
    // 2026-07-10 rename read consistently with new ones.
    const safe = esc(String(txt ?? "").replace(/\bSeat (\d)\b/g, "Player $1"));
    return nameRe ? safe.replace(nameRe, '<span class="card-name" data-card="$1">$1</span>') : safe;
  };
  const champ = seat(winner.seat);
  // The engine's method string is a semicolon chain ("X has won because …;
  // Y has lost because …; Z has lost because …") — keep the win as the
  // headline and fold the per-player losses into a quieter second line.
  const reasons = String(winner.method || "").split("; ");
  const headline = reasons[0] || "";
  const losses = reasons.slice(1);
  // One muted, parchment-friendly tint per seat, used everywhere a player is
  // named — turn rows, board strips, life totals, deck reports — so "who is
  // P3" reads at a glance.
  const SEAT_TINTS = ["#a2543a", "#3f6488", "#5c7a44", "#7c5482"];
  const tint = (s) => SEAT_TINTS[(s - 1) % SEAT_TINTS.length] || "var(--ink3)";
  const dot = (s, size = 7) => `<span style="display:inline-block;width:${size}px;height:${size}px;border-radius:50%;background:${tint(s)};vertical-align:baseline;flex:none"></span>`;
  // Per-turn resource tracker (Forge games only): each seat's lands in play,
  // and the mana it produced that turn. Lands and mana are the two board
  // resources the engine log exposes by object id, so they're exact; creatures
  // and other permanents leave no id trail and are deliberately not counted.
  const boardStrip = (t) => {
    if (!t.board) return "";
    const cells = (g.players || []).map((p) => {
      const b = t.board[String(p.seat)]; if (!b) return "";
      if ((t.life || [])[p.seat - 1] <= 0)  // eliminated — their battlefield is gone, don't show stale counts
        return `<span title="${esc(p.commander)} — eliminated" style="display:inline-flex;align-items:center;gap:4px;opacity:.35">${dot(p.seat, 6)}out</span>`;
      const active = p.seat === t.seat;
      const mana = active && b.mana ? ` ${esc(b.mana)}◈` : "";
      return `<span title="${esc(p.commander)}" style="display:inline-flex;align-items:center;gap:4px;${active ? "color:var(--ink2);font-weight:600" : "opacity:.75"}">${dot(p.seat, 6)}${esc(b.lands)}🜨${mana}</span>`;
    }).filter(Boolean).join('<span style="opacity:.35">·</span>');
    return `<div class="mono" style="font-size:10.5px;color:var(--ink3);padding:0 4px 6px 42px;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline">${cells}</div>`;
  };
  // Whether any turn carries a board tracker — decides if the legend is shown.
  const hasBoard = turns.some((t) => t.board);
  const rowStyle = "display:flex;gap:12px;align-items:baseline;padding:7px 4px 3px;font-size:13.5px;";
  const engineLabel = g.engine === "forge" ? "Forge rules engine · real game" : "LLM referee · seed " + esc(g.seed);
  root.innerHTML = `<section class="simulation-result">
    <div class="result-head"><div><div class="caps">Simulated game · ${engineLabel}</div>
      <h2>${champ.commander ? esc(champ.commander) + " takes the table" : "Game complete"}</h2>
      <p>${headline ? markCards(headline) + (winner.turn ? " — turn " + esc(winner.turn) : "") : markCards(result.opening_note || "")}</p>
      ${losses.length ? `<p class="mono" style="margin:4px 0 0;font-size:11px;color:var(--ink3)">${losses.map((l) => markCards(l.replace(" has lost because", " —").replace("life total reached 0", "life hit 0"))).join(" &nbsp;·&nbsp; ")}</p>` : ""}</div>
      <span class="seal-badge">${g.engine === "forge" ? "Engine-tracked zones" : "Decklists verified"}</span></div>
    <div class="source-strip">${(g.players || []).map((p) => `<span>${dot(p.seat, 6)} Player ${esc(p.seat)}: ${esc(p.commander)} · ${(p.cards || {}).mulligans || 0} mulligan${(p.cards || {}).mulligans === 1 ? "" : "s"}${(p.cards || {}).kept_hand ? ` · kept ${esc(p.cards.kept_hand)}` : ""}</span>`).join("")}${g.rules_effective_date ? `<span>CR effective ${esc(g.rules_effective_date)}</span>` : ""}</div>
    ${g.engine === "forge" && session.id ? `<p class="match-note" style="margin-top:8px">Receipts: <a href="/api/simulations/${esc(session.id)}/details">per-turn detail (JSON)</a> · <a href="/api/simulations/${esc(session.id)}/forge-log">raw Forge log</a></p>` : ""}
    ${result.opening_note && winner.method ? `<p class="match-note">${markCards(result.opening_note)}</p>` : ""}
    <div style="margin-top:14px">
      <div class="caps" style="margin-bottom:6px;display:flex;justify-content:space-between;align-items:baseline;gap:12px">
        <span>The game, turn by turn</span>
        ${hasBoard ? `<span class="mono" style="text-transform:none;letter-spacing:0;font-size:10px;color:var(--ink3)">🜨 lands in play · ◈ mana produced · numbers on the right are life</span>` : ""}
      </div>
      ${turns.map((t, i) => {
        // Rows are grouped by game round: the turn number is printed once per
        // round and the boundary gets a stronger rule, so a whole table-turn
        // reads as one block instead of four repeating labels.
        const roundStart = i === 0 || turns[i - 1].turn !== t.turn;
        return `<div>
        <div style="${rowStyle}border-top:1px solid rgba(120,90,30,${roundStart ? ".38" : ".10"})">
          <span class="mono" style="font-size:11px;color:var(--ink3);min-width:30px;font-weight:${roundStart ? 700 : 400}">${roundStart ? "T" + esc(t.turn) : ""}</span>
          <b style="min-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(seat(t.seat).commander || "")}">${dot(t.seat)} <span class="mono" style="font-size:10.5px;color:${tint(t.seat)};font-weight:600">P${esc(t.seat)}</span> ${esc((seat(t.seat).commander || "Player " + t.seat).split(",")[0])}</b>
          <span style="flex:1">${markCards(t.play)}</span>
          <span class="mono" style="font-size:11px;color:var(--ink3);white-space:nowrap;display:inline-flex;align-items:baseline;gap:7px">${(t.life || []).map((v, i2) => `<span style="display:inline-flex;align-items:center;gap:3px;${v <= 0 ? "opacity:.4;text-decoration:line-through" : ""}">${dot(i2 + 1, 5)}${esc(v)}</span>`).join("")}</span>
        </div>
        ${boardStrip(t)}
      </div>`; }).join("")}
    </div>
    <div class="snapshot"><div class="caps">Deck reports — how each build performed</div>
      ${(result.deck_reports || []).map((r) => { const p = seat(r.seat); return `<div style="margin-top:12px">
        <b>${dot(r.seat)} Player ${esc(r.seat)} — ${esc(p.commander || "")}${winner.seat === r.seat ? " 🏆" : ""}</b>
        <p style="margin:4px 0 0">${markCards(r.verdict)}</p>
        ${(r.key_cards || []).length ? `<small style="color:var(--ink3)">Key cards: ${r.key_cards.map((c) => `<span class="card-name" data-card="${esc(c)}">${esc(c)}</span>`).join(", ")}</small>` : ""}
      </div>`; }).join("")}
      ${(result.unresolved_questions || []).length ? `<div class="unresolved" style="margin-top:14px"><b>Judgement calls made along the way</b><ul>${result.unresolved_questions.map((item) => `<li>${markCards(item)}</li>`).join("")}</ul></div>` : ""}
    </div>
  </section>`;
  wireCardPreviews(root); // hover any card name in the log or reports for its real art
}

/* ── gallery ────────────────────────────────────────────────────────────── */

async function viewGallery() {
  const decks = await api("/api/decks");
  const mine = decks.filter((d) => d.owner_deck);
  const forged = decks.filter((d) => !d.owner_deck);
  HEAD_SUB.textContent = "";
  HEAD_RIGHT.innerHTML = `<span>${forged.length} decks forged${mine.length ? ` · ${mine.length} of 3vor's` : ""}</span>`;

  const forgedCard = (d) => `
    <div class="shelf-card" onclick="location.hash='#deck/${esc(d.id)}'">
      <div class="art-ph" data-art="${esc(d.commander)}" style="height:80px"><span>art crop</span></div>
      <div class="shelf-name">${esc(d.commander)}</div>
      <div class="shelf-arch">${esc(d.archetype)}</div>
      <div class="shelf-meta">${esc(d.ts.slice(0, 10))} · ${money(d.total_sgd)}</div>
    </div>`;
  const ownCard = (d) => `
    <div class="shelf-card own-card" onclick="location.hash='#deck/${esc(d.id)}'">
      <button class="own-remove" data-id="${esc(d.id)}" title="Take this deck off the shelf">×</button>
      <div class="art-ph" data-art="${esc(d.commander)}" style="height:80px"><span>art crop</span></div>
      <div class="shelf-name">${esc(d.commander)}</div>
      <div class="shelf-arch">${esc(d.archetype)}</div>
      <div class="shelf-meta">${esc(d.ts.slice(0, 10))} · 3vor's own</div>
    </div>`;

  VIEW.innerHTML = `<div class="page">
    <div class="shelf-head" style="margin-top:28px">
      <div class="caps-lg">3vor's decks — the master's own</div>
      <button class="btn-ghost" id="btn-own-toggle">+ Add a deck</button>
    </div>
    <div class="panel own-import" id="own-import" style="display:none">
      <div class="own-import-grid">
        <div>
          <div class="caps" style="margin-bottom:5px">Commander</div>
          <div class="commander-field">
            <div class="commander-input"><span>&#9813;</span>
              <input id="own-commander" type="text" placeholder="e.g. Gishath, Sun's Avatar" autocomplete="off">
            </div>
            <div class="suggest-box" id="own-suggest" style="display:none"></div>
          </div>
          <div class="caps" style="margin:12px 0 5px">Deck name — optional</div>
          <input class="email-input" id="own-label" placeholder="e.g. Dino stompy">
          <div class="own-hint">Paste the full list — Moxfield / Archidekt / MTGO exports or plain
            &ldquo;1 Card Name&rdquo; lines all work. A Commander section in the paste wins over the
            field above. Once shelved, the deck can take a seat in the match simulator.</div>
          <button class="btn-brass" id="btn-own-save" style="margin-top:12px">Shelve the deck</button>
          <div class="own-error" id="own-error"></div>
        </div>
        <textarea id="own-text" spellcheck="false" placeholder="1 Gishath, Sun's Avatar&#10;1 Regisaur Alpha&#10;1 Sol Ring&#10;&hellip;"></textarea>
      </div>
    </div>
    ${mine.length ? `<div class="gallery-grid">${mine.map(ownCard).join("")}</div>`
      : `<div class="empty-note">No decks of your own on the shelf yet — add one to pit it against the guild's builds.</div>`}
    <div class="shelf-head" style="margin-top:34px"><div class="caps-lg">The gallery — every commission</div></div>
    ${forged.length ? `<div class="gallery-grid">${forged.map(forgedCard).join("")}</div>` : '<div class="empty-note">The shelves are bare — the first commission awaits.</div>'}
  </div>`;

  $("#btn-own-toggle").onclick = () => {
    const panel = $("#own-import");
    const open = panel.style.display === "none";
    panel.style.display = open ? "" : "none";
    if (open) $("#own-commander").focus();
  };
  wireCommanderAutocomplete($("#own-commander"), $("#own-suggest"), () => {});
  $("#btn-own-save").onclick = async () => {
    const btn = $("#btn-own-save"), errEl = $("#own-error");
    errEl.textContent = "";
    btn.disabled = true;
    btn.textContent = "Checking the list…";
    try {
      const res = await api("/api/decks/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          commander: $("#own-commander").value.trim(),
          label: $("#own-label").value.trim(),
          text: $("#own-text").value,
        }),
      });
      toast(`${res.commander} shelved — ${res.count} cards.`);
      viewGallery();
    } catch (err) {
      errEl.textContent = err.message; // the paste stays put so a typo is a one-line fix
      btn.disabled = false;
      btn.textContent = "Shelve the deck";
    }
  };
  VIEW.querySelectorAll(".own-remove").forEach((btn) => btn.onclick = async (e) => {
    e.stopPropagation(); // the card behind it navigates to the deck view
    if (!confirm("Take this deck off the shelf? Guild commissions are never touched.")) return;
    try {
      await api("/api/decks/" + encodeURIComponent(btn.dataset.id), { method: "DELETE" });
      toast("Deck removed from the shelf.");
      viewGallery();
    } catch (err) { toast(err.message); }
  });
  fillArt(VIEW);
}

/* ── guild rules ────────────────────────────────────────────────────────── */

async function viewRules() {
  const s = await api("/api/settings");
  HEAD_SUB.textContent = "";
  HEAD_RIGHT.innerHTML = `<button class="btn-brass" id="btn-save">Inscribe changes</button><span class="save-feedback" id="save-note"></span>`;

  const brackets = [["1", "Exhibition"], ["2", "Core"], ["3", "Upgraded"], ["4", "Optimized"], ["5", "cEDH"]];
  const stages = [
    ["select", "Select", "concept + dedupe"],
    ["draft", "Draft ×3", "three whole decks in parallel"],
    ["judge", "Judge", "picks the winning draft"],
    ["validate_repair", "Validate + repair", "code-level checks, LLM mends"],
    ["optimize", "Optimize", "swap-delta passes"],
    ["card_tagger", "Card tagger", "roles + phases for the sheets"],
    ["simulate", "Match rehearsal", "grounded early-game playtest"],
  ];
  const state = JSON.parse(JSON.stringify(s));

  VIEW.innerHTML = `<div class="page"><div class="rules-grid">
    <div class="rules-col">
      <div class="rule-panel">
        <div class="rule-panel-head">The purse</div>
        <div class="rule-panel-body">
          ${sliderRow("deck_budget_sgd", "Deck budget", "total, cheapest across the stores — display only, never enforced", 50, 1000, 10, s.deck_budget_sgd, (v) => "SGD " + Number(v).toFixed(0))}
          ${sliderRow("max_card_price_sgd", "Per-card cap", "singles above this are flagged / swapped", 5, 300, 5, s.max_card_price_sgd, (v) => "SGD " + Number(v).toFixed(0))}
          ${sliderRow("max_run_spend_usd", "Crucible cap", "API spend per nightly run · 0 disables the halt", 0, 20, 0.5, s.max_run_spend_usd, (v) => Number(v) > 0 ? "$" + Number(v).toFixed(2) : "off")}
        </div>
      </div>
      <div class="rule-panel">
        <div class="rule-panel-head">The bracket</div>
        <div class="rule-panel-body"><div class="bracket-cards" id="bracket-cards">
          ${brackets.map(([n, label]) => `<div class="bracket-card ${String(s.bracket).startsWith(n) ? "active" : ""}" data-bracket="${n}">
            <span class="n">${n}</span><span class="lbl">${label}</span></div>`).join("")}
        </div></div>
      </div>
      <div class="rule-panel">
        <div class="rule-panel-head">The nightly bell</div>
        <div class="rule-panel-body">
          ${toggleRow("nightly_enabled", "Nightly commission", "run_nightly.sh's schedule — shown here, scheduled outside the app", s.nightly_enabled,
            `<span class="toggle-value"><input id="in-nightly-time" value="${esc(s.nightly_time)}" maxlength="5"></span>`)}
          ${toggleRow("exclude_recent_commanders", "Exclude recent commanders", "no repeats within the window", s.exclude_recent_commanders,
            `<span class="toggle-value"><input id="in-dedupe-days" value="${Number(s.dedupe_commander_days)}" maxlength="3"> days</span>`)}
          ${toggleRow("resume_session_chaining", "Resume chaining", "experimental — chains claude sessions between stages", s.resume_session_chaining, "")}
        </div>
      </div>
    </div>
    <div class="rules-col">
      <div class="rule-panel">
        <div class="rule-panel-head">The workforce <small>which mind works each bench</small></div>
        <div class="rule-panel-body" style="gap:0">
          ${stages.map(([key, label, note]) => `<div class="workforce-row">
            <span class="stage">${label}</span><span class="note">${note}</span>
            <div style="display:flex;gap:4px">${["haiku", "sonnet", "opus"].map((tier) =>
              `<button class="tier-chip ${s.model_tiers[key] === tier ? "active" : ""}" data-stage="${key}" data-tier="${tier}">${tier}</button>`).join("")}</div>
          </div>`).join("")}
        </div>
      </div>
      <div class="rule-panel">
        <div class="rule-panel-head">The courier</div>
        <div class="rule-panel-body">
          <div>
            <div class="caps" style="letter-spacing:1px;margin-bottom:5px">Master's copy — full diagnostics</div>
            <input class="email-input" id="in-email" value="${esc(s.email_to)}" placeholder="you@example.com">
          </div>
          <div>
            <div class="caps" style="letter-spacing:1px;margin-bottom:5px">The newsletter — clean copy, no diagnostics</div>
            <div class="bcc-box" id="bcc-box">
              ${s.newsletter_bcc.map((a) => `<span class="bcc-chip" data-addr="${esc(a)}">${esc(a)} ×</span>`).join("")}
              <input class="bcc-add" id="bcc-add" placeholder="+ add a friend&hellip;">
            </div>
          </div>
          <div class="courier-note">Friends receive the deck and the tale — never the cost sheet.</div>
        </div>
      </div>
    </div>
  </div></div>`;

  // wire the controls into `state`
  for (const key of ["deck_budget_sgd", "max_card_price_sgd", "max_run_spend_usd"]) {
    const input = $(`#slider-${key}`);
    input.oninput = () => {
      state[key] = Number(input.value);
      $(`#slider-val-${key}`).textContent = input.dataset.fmt === "usd"
        ? (state[key] > 0 ? "$" + state[key].toFixed(2) : "off")
        : "SGD " + state[key].toFixed(0);
    };
  }
  document.querySelectorAll("#bracket-cards .bracket-card").forEach((card) => {
    card.onclick = () => {
      state.bracket = card.dataset.bracket;
      document.querySelectorAll("#bracket-cards .bracket-card").forEach((c) => c.classList.toggle("active", c === card));
    };
  });
  document.querySelectorAll(".toggle[data-key]").forEach((tog) => {
    tog.onclick = () => {
      state[tog.dataset.key] = !state[tog.dataset.key];
      tog.classList.toggle("on", state[tog.dataset.key]);
    };
  });
  document.querySelectorAll(".tier-chip").forEach((chip) => {
    chip.onclick = () => {
      state.model_tiers[chip.dataset.stage] = chip.dataset.tier;
      document.querySelectorAll(`.tier-chip[data-stage="${chip.dataset.stage}"]`).forEach((c) =>
        c.classList.toggle("active", c === chip));
    };
  });
  const bccBox = $("#bcc-box");
  bccBox.addEventListener("click", (e) => {
    const chip = e.target.closest(".bcc-chip");
    if (chip) {
      state.newsletter_bcc = state.newsletter_bcc.filter((a) => a !== chip.dataset.addr);
      chip.remove();
    }
  });
  $("#bcc-add").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      const addr = e.target.value.trim();
      state.newsletter_bcc.push(addr);
      const chip = document.createElement("span");
      chip.className = "bcc-chip";
      chip.dataset.addr = addr;
      chip.textContent = addr + " ×";
      e.target.before(chip);
      e.target.value = "";
    }
  });

  $("#btn-save").onclick = async () => {
    const note = $("#save-note");
    state.email_to = $("#in-email").value.trim();
    state.nightly_time = $("#in-nightly-time").value.trim();
    state.dedupe_commander_days = Number($("#in-dedupe-days").value) || state.dedupe_commander_days;
    try {
      await api("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
      });
      note.className = "save-feedback";
      note.textContent = "inscribed ✓";
    } catch (err) {
      note.className = "save-feedback err";
      note.textContent = err.message;
    }
    setTimeout(() => { note.textContent = ""; }, 4000);
  };
}

function sliderRow(key, label, hint, min, max, step, value, fmt) {
  const isUsd = key === "max_run_spend_usd";
  return `<div class="slider-row">
    <div class="slider-label"><b>${label}</b><small>${hint}</small></div>
    <input type="range" id="slider-${key}" min="${min}" max="${max}" step="${step}" value="${Number(value)}" data-fmt="${isUsd ? "usd" : "sgd"}">
    <span class="slider-value" id="slider-val-${key}">${fmt(value)}</span>
  </div>`;
}

function toggleRow(key, label, hint, on, extra) {
  return `<div class="toggle-row">
    <div class="toggle ${on ? "on" : ""}" data-key="${key}"><div class="knob-dot"></div></div>
    <div class="toggle-label"><b>${label}</b><small>${hint}</small></div>
    ${extra}
  </div>`;
}

/* ── boot ───────────────────────────────────────────────────────────────── */

(async () => {
  try { statusCache = await api("/api/status"); } catch { /* first paint can live without it */ }
  if (statusCache && statusCache.running && !location.hash) location.hash = "#live";
  route();
})();
