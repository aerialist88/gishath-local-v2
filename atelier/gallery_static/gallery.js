/* gallery.js — the Atelier's read-only public gallery.
 *
 * A trimmed cousin of atelier/static/atelier.js: same gallery + deck-detail
 * rendering, but it reads bundled data (data/decks.json, data/art.json) instead
 * of the live /api endpoints, and it drops everything a friend can't use — the
 * cost ledger, the "Price on 3vor Fetch" link (that points at localhost), and
 * every write control. Hash-routed: #gallery (default) and #deck/<id8>.
 */

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (v, cur = "SGD") => v == null ? "—" : cur + " " + Number(v).toFixed(2);

const $ = (sel, root = document) => root.querySelector(sel);
const VIEW = $("#view");
const HEAD_SUB = $("#head-sub");
const HEAD_RIGHT = $("#head-right");

let DECKS = [];          // full records, newest-first
let ART = {};            // name.toLowerCase() -> { art_crop, normal }
const byId = {};

async function loadData() {
  const [decks, art] = await Promise.all([
    fetch("data/decks.json").then((r) => r.json()),
    fetch("data/art.json").then((r) => r.json()).catch(() => ({})),
  ]);
  DECKS = decks;
  ART = art || {};
  for (const d of DECKS) byId[d.run_id8] = d;
}

/* ── card art (bundled, hotlinked to Scryfall) ──────────────────────────── */

function artFor(name) { return ART[String(name || "").trim().toLowerCase()] || null; }

function fillArt(root) {
  for (const ph of (root || document).querySelectorAll(".art-ph[data-art]")) {
    const kind = ph.dataset.artKind || "art_crop";
    const entry = artFor(ph.dataset.art);
    const url = entry && (entry[kind] || entry.normal || entry.art_crop);
    if (url && !ph.querySelector("img")) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = ph.dataset.art;
      ph.appendChild(img);
    }
  }
}

/* ── floating card-image preview on hover ───────────────────────────────── */

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
  const w = 240, h = Math.round(240 * (680 / 488));
  let left = x + margin;
  let top = y - h / 2;
  if (left + w > window.innerWidth - margin) left = x - w - margin;
  left = Math.max(margin, left);
  top = Math.min(Math.max(margin, top), window.innerHeight - h - margin);
  el.style.left = left + "px";
  el.style.top = top + "px";
}
function showCardPreview(name, x, y) {
  const el = ensureCardPreview();
  el.dataset.card = name;
  positionCardPreview(el, x, y);
  const entry = artFor(name);
  const url = entry && (entry.normal || entry.art_crop);
  if (!url) { el.style.display = "none"; return; }
  el.querySelector("img").src = url;
  el.style.display = "block";
}
function hideCardPreview() {
  if (cardPreviewEl) { cardPreviewEl.style.display = "none"; cardPreviewEl.dataset.card = ""; }
}
function wireCardPreviews(container) {
  if (container.dataset.previewWired) return;
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

/* ── gallery ────────────────────────────────────────────────────────────── */

function viewGallery() {
  HEAD_SUB.textContent = "The gallery";
  HEAD_RIGHT.innerHTML = `<span>${DECKS.length} decks forged</span>`;
  VIEW.innerHTML = `<div class="page">
    <div class="shelf-head" style="margin-top:28px"><div class="caps-lg">The gallery — every commission</div></div>
    ${DECKS.length ? `<div class="gallery-grid">${DECKS.map((d) => `
      <div class="shelf-card" onclick="location.hash='#deck/${esc(d.run_id8)}'">
        <div class="art-ph" data-art="${esc(d.commander)}" style="height:80px"><span>art crop</span></div>
        <div class="shelf-name">${esc(d.commander)}</div>
        <div class="shelf-arch">${esc(d.archetype || "")}</div>
        <div class="shelf-meta">${esc((d.generated_utc || "").slice(0, 10))} · ${money((d.price || {}).total_sgd)}</div>
      </div>`).join("")}</div>` : '<div class="empty-note">The shelves are bare.</div>'}
  </div>`;
  fillArt(VIEW);
}

/* ── deck detail ────────────────────────────────────────────────────────── */

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
  return `<div class="stats-grid">
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Mana curve (nonland)</div>
      <table class="stat-table">${["0","1","2","3","4","5","6+"].map((b) => `<tr><td>CMC ${b}</td><td>${(s.curve || {})[b] || 0}</td></tr>`).join("")}</table></div>
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Colour pips</div>
      <table class="stat-table">${Object.entries(pipNames).map(([k, n]) => `<tr><td>${n}</td><td>${(s.pips || {})[k] || 0}</td></tr>`).join("")}</table></div>
    <div class="rail-panel"><div class="caps" style="margin-bottom:10px">Roles</div>
      <table class="stat-table">${Object.entries(s.role_counts || {}).sort((a, b) => b[1] - a[1]).map(([r, n]) => `<tr><td>${esc(r)}</td><td>${n}</td></tr>`).join("")}</table></div>
  </div>`;
}

function viewDeck(id) {
  const deck = byId[id];
  if (!deck) { location.hash = "#gallery"; return; }
  HEAD_SUB.textContent = "";
  HEAD_RIGHT.innerHTML = `<a href="#gallery" class="btn-ghost">&larr; Gallery</a>`;

  const p = deck.price || {};
  const flaggedCount = (deck.cards || []).filter((c) => c.over_cap).length;
  const delivered = (deck.generated_utc || "").slice(11, 16);
  const pills = [
    `Bracket ${esc(deck.bracket || "?")}`,
    deck.legal ? "Legal ✓" : "Legality unverified",
    deck.synergy_gate_fired ? "Synergy gate repaired" : "Synergy gate passed",
  ];

  VIEW.innerHTML = `<div class="page">
    <div class="deck-header">
      <div class="art-ph" style="width:92px;height:128px;box-shadow:0 3px 8px rgba(120,90,30,.25)" data-art="${esc(deck.commander)}" data-art-kind="normal"><span>commander<br>art</span></div>
      <div style="display:flex;flex-direction:column;gap:5px">
        <div class="caps-lg">Commission ${esc(deck.run_id8 || "")}${delivered ? " · Delivered " + delivered : ""}</div>
        <div class="deck-title">${esc(deck.commander)}</div>
        <div class="cp-concept">${esc(deck.archetype || "")}${deck.summary ? " — " + esc(deck.summary) : ""}</div>
        <div style="display:flex;gap:8px;margin-top:6px">${pills.map((x) => `<span class="pill">${x}</span>`).join("")}</div>
      </div>
      <div class="head-spacer"></div>
      <div class="deck-header-right">
        <div class="deck-price">${money(p.total_sgd)}</div>
        <div class="deck-price-note">cheapest across the store scrapers${flaggedCount ? ` · ${flaggedCount} card${flaggedCount > 1 ? "s" : ""} over cap, flagged` : ""}${p.unpriced_count ? ` · ${p.unpriced_count} unpriced` : ""}</div>
        <div style="display:flex;gap:8px">
          ${deck.files && deck.files.moxfield_txt ? `<a class="btn-brass" href="${esc(deck.files.moxfield_txt)}" download>Moxfield .txt</a>` : ""}
          ${deck.files && deck.files.xlsx ? `<a class="btn-ghost" href="${esc(deck.files.xlsx)}" download>.xlsx</a>` : ""}
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
  wireCardPreviews(body);
  show("decklist");
  fillArt(VIEW);
  window.scrollTo(0, 0);
}

/* ── router ─────────────────────────────────────────────────────────────── */

function route() {
  const hash = location.hash.slice(1);
  if (hash.startsWith("deck/")) viewDeck(hash.slice(5));
  else viewGallery();
}

window.addEventListener("hashchange", route);
loadData().then(route).catch((e) => {
  VIEW.innerHTML = `<div class="page"><div class="empty-note">Could not load the gallery data.</div></div>`;
  console.error(e);
});
