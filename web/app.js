/* NERD Reader 🦞 — frontend estático (GitHub Pages)
   Os artigos vêm de data/meta.json (gerado pelo GitHub Actions a cada 30 min).
   Lido/não lido e salvos ficam SÓ neste navegador (localStorage). */
"use strict";

// ---------------------------------------------------------------- estado

const state = {
  scope: { type: "all", id: null, label: "Todos os artigos" },
  filter: localStorage.getItem("nr-filter") || "unread",
  view: localStorage.getItem("nr-view") || "list",
  q: "",
  meta: null,          // conteúdo de data/meta.json
  feedsById: {},
  visible: [],         // artigos do escopo/filtro atual (ordenados)
  shown: 0,            // quantos já estão renderizados (paginação)
  selected: -1,
  readerId: null,
  contentCache: new Map(),  // feedId -> {articleId: html}
};

const PAGE = 60;

// lido: {articleId: timestamp}; salvo: {articleId: snapshot do artigo}
let readMap = readJSON("nr-read", {});
let starMap = readJSON("nr-star", {});

function readJSON(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return v && typeof v === "object" ? v : fallback;
  } catch (e) { return fallback; }
}
function saveRead() { localStorage.setItem("nr-read", JSON.stringify(readMap)); }
function saveStar() { localStorage.setItem("nr-star", JSON.stringify(starMap)); }

const isRead = (a) => !!readMap[a.id];
const isStar = (a) => !!starMap[a.id];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

// ---------------------------------------------------------------- datas

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "agora";
  if (s < 3600) return "há " + Math.floor(s / 60) + " min";
  if (s < 86400) return "há " + Math.floor(s / 3600) + " h";
  const d = Math.floor(s / 86400);
  if (d === 1) return "ontem";
  if (d < 30) return "há " + d + " dias";
  return new Date(ts * 1000).toLocaleDateString("pt-BR", { day: "numeric", month: "short", year: "numeric" });
}

function fullDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("pt-BR", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function todayStart() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

// ---------------------------------------------------------------- dados

async function loadMeta(bustCache) {
  const url = "data/meta.json" + (bustCache ? "?t=" + Date.now() : "");
  const resp = await fetch(url, bustCache ? { cache: "no-store" } : {});
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  state.meta = await resp.json();
  state.feedsById = {};
  for (const cat of state.meta.categories) {
    for (const f of cat.feeds) state.feedsById[f.id] = { ...f, category: cat.name };
  }
  // artigos salvos que saíram do site continuam disponíveis pelo snapshot
  const liveIds = new Set(state.meta.articles.map((a) => a.id));
  for (const [id, snap] of Object.entries(starMap)) {
    if (!liveIds.has(id) && snap && snap.id) state.meta.articles.push(snap);
  }
  // poda lidos antigos que já saíram do site (não deixa o localStorage crescer)
  let pruned = false;
  for (const id of Object.keys(readMap)) {
    if (!liveIds.has(id)) { delete readMap[id]; pruned = true; }
  }
  if (pruned) saveRead();
  updateGeneratedAt();
}

function updateGeneratedAt() {
  const el = $("#updated-at");
  if (state.meta && state.meta.generated_at) {
    el.textContent = "Feeds atualizados " + timeAgo(state.meta.generated_at) +
      " · o site busca novidades a cada 30 min";
  } else {
    el.textContent = "";
  }
}

async function contentFor(article) {
  let map = state.contentCache.get(article.feed);
  if (!map) {
    try {
      const resp = await fetch("data/content-" + article.feed + ".json");
      map = resp.ok ? await resp.json() : {};
    } catch (e) { map = {}; }
    state.contentCache.set(article.feed, map);
  }
  return map[article.id] || "";
}

// ---------------------------------------------------------------- sidebar

const openCats = new Set(JSON.parse(localStorage.getItem("nr-open-cats") || "[]"));

function unreadCounts() {
  const perFeed = {};
  let total = 0, today = 0;
  const t0 = todayStart();
  for (const a of state.meta.articles) {
    if (isRead(a)) continue;
    perFeed[a.feed] = (perFeed[a.feed] || 0) + 1;
    total++;
    if (a.published >= t0) today++;
  }
  return { perFeed, total, today, starred: Object.keys(starMap).length };
}

function renderSidebar() {
  if (!state.meta) return;
  const counts = unreadCounts();

  const setCount = (id, n, badge) => {
    const el = $(id);
    el.textContent = n > 0 ? String(n) : "";
    el.classList.toggle("badge", !!badge && n > 0);
  };
  setCount("#count-today", counts.today, true);
  setCount("#count-all", counts.total, true);
  setCount("#count-starred", counts.starred, false);

  const tree = $("#cat-tree");
  tree.innerHTML = "";

  for (const cat of state.meta.categories) {
    const catUnread = cat.feeds.reduce((s, f) => s + (counts.perFeed[f.id] || 0), 0);
    const block = document.createElement("div");
    block.className = "cat-block" + (openCats.has(cat.name) ? " open" : "");

    const row = document.createElement("div");
    row.className = "cat-row";
    if (state.scope.type === "category" && state.scope.id === cat.name) row.classList.add("active");
    row.innerHTML =
      '<span class="cat-caret">▶</span>' +
      '<span class="cat-name">' + esc(cat.name) + "</span>" +
      '<span class="count' + (catUnread ? " badge" : "") + '">' + (catUnread || "") + "</span>";
    row.querySelector(".cat-caret").addEventListener("click", (ev) => {
      ev.stopPropagation();
      block.classList.toggle("open");
      if (block.classList.contains("open")) openCats.add(cat.name); else openCats.delete(cat.name);
      localStorage.setItem("nr-open-cats", JSON.stringify([...openCats]));
    });
    row.addEventListener("click", () => setScope({ type: "category", id: cat.name, label: cat.name }));
    block.appendChild(row);

    const feedsBox = document.createElement("div");
    feedsBox.className = "cat-feeds";
    for (const feed of cat.feeds) {
      const unread = counts.perFeed[feed.id] || 0;
      const errored = feed.status && feed.status.startsWith("erro");
      const fr = document.createElement("div");
      fr.className = "feed-row" + (unread ? " has-unread" : "") + (errored ? " errored" : "");
      if (state.scope.type === "feed" && state.scope.id === feed.id) fr.classList.add("active");
      fr.innerHTML =
        '<span class="feed-dot"></span>' +
        '<span class="feed-name">' + esc(feed.title) + "</span>" +
        (errored ? '<span class="feed-err" title="' + esc(feed.status) + '">⚠️</span>' : "") +
        '<span class="count">' + (unread || "") + "</span>";
      fr.addEventListener("click", () => setScope({ type: "feed", id: feed.id, label: feed.title }));
      feedsBox.appendChild(fr);
    }
    block.appendChild(feedsBox);
    tree.appendChild(block);
  }

  $$(".nav-item").forEach((el) => {
    el.classList.toggle("active", state.scope.type === el.dataset.scope);
  });
}

// ---------------------------------------------------------------- lista

function computeVisible() {
  const t0 = todayStart();
  const q = state.q.toLowerCase();
  let arts = state.meta.articles;

  if (state.scope.type === "feed") arts = arts.filter((a) => a.feed === state.scope.id);
  else if (state.scope.type === "category") {
    const ids = new Set((state.meta.categories.find((c) => c.name === state.scope.id) || { feeds: [] }).feeds.map((f) => f.id));
    arts = arts.filter((a) => ids.has(a.feed));
  } else if (state.scope.type === "starred") arts = arts.filter(isStar);
  else if (state.scope.type === "today") arts = arts.filter((a) => a.published >= t0);

  if (state.filter === "unread" && state.scope.type !== "starred") arts = arts.filter((a) => !isRead(a));

  if (q) {
    arts = arts.filter((a) =>
      (a.title || "").toLowerCase().includes(q) || (a.summary || "").toLowerCase().includes(q));
  }
  state.visible = arts;
}

function renderList(reset) {
  const listEl = $("#list");
  const status = $("#list-status");
  if (reset) {
    listEl.innerHTML = "";
    state.shown = 0;
    state.selected = -1;
  }
  const upto = Math.min(state.visible.length, state.shown + PAGE);
  for (let i = state.shown; i < upto; i++) {
    listEl.appendChild(buildRow(state.visible[i], i));
  }
  state.shown = upto;
  $("#btn-more").hidden = state.shown >= state.visible.length;
  status.textContent = state.visible.length === 0
    ? (state.q ? "Nenhum resultado para “" + state.q + "”."
      : state.filter === "unread" ? "Tudo lido por aqui. 🦞" : "Nenhum artigo.")
    : "";
  updateTitleCount();
}

function buildRow(a, idx) {
  const row = document.createElement("div");
  row.className = "art-row" + (isRead(a) ? " is-read" : "");
  row.dataset.idx = idx;
  const feed = state.feedsById[a.feed] || { title: a.feedTitle || "?" };
  const thumb = a.image
    ? '<img class="art-thumb" loading="lazy" src="' + esc(a.image) + '" alt="" onerror="this.remove()">'
    : "";
  row.innerHTML =
    thumb +
    '<div class="art-main">' +
      '<div class="art-title">' + esc(a.title) + "</div>" +
      '<div class="art-meta"><span class="art-source">' + esc(feed.title) + "</span>" +
        "<span>·</span><span>" + timeAgo(a.published) + "</span>" +
        (a.author ? "<span>·</span><span>" + esc(a.author) + "</span>" : "") +
      "</div>" +
      '<div class="art-snippet">' + esc(a.summary) + "</div>" +
    "</div>" +
    '<div class="art-side">' +
      '<button class="star-btn' + (isStar(a) ? " on" : "") + '" title="Salvar (s)">' + (isStar(a) ? "★" : "☆") + "</button>" +
    "</div>";
  row.addEventListener("click", () => openReader(idx));
  row.querySelector(".star-btn").addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleStar(idx);
  });
  return row;
}

function rowEl(idx) {
  return $('#list .art-row[data-idx="' + idx + '"]');
}

function updateRow(idx) {
  const a = state.visible[idx];
  const row = rowEl(idx);
  if (!a || !row) return;
  row.classList.toggle("is-read", isRead(a));
  const btn = row.querySelector(".star-btn");
  btn.classList.toggle("on", isStar(a));
  btn.textContent = isStar(a) ? "★" : "☆";
}

function selectRow(idx, scroll) {
  if (state.selected >= 0) {
    const prev = rowEl(state.selected);
    if (prev) prev.classList.remove("selected");
  }
  state.selected = idx;
  const row = rowEl(idx);
  if (row) {
    row.classList.add("selected");
    if (scroll) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

// ---------------------------------------------------------------- ações

function markRead(idx, read) {
  const a = state.visible[idx];
  if (!a) return;
  if (read) readMap[a.id] = Math.floor(Date.now() / 1000);
  else delete readMap[a.id];
  saveRead();
  updateRow(idx);
  renderSidebar();
}

function toggleStar(idx) {
  const a = state.visible[idx];
  if (!a) return;
  if (isStar(a)) delete starMap[a.id];
  else starMap[a.id] = { ...a };  // snapshot: sobrevive quando o artigo sai do site
  saveStar();
  updateRow(idx);
  renderSidebar();
}

function markAllRead() {
  if (!state.visible.length) return;
  if (!confirm("Marcar os " + state.visible.length + " artigos de “" + state.scope.label + "” como lidos?")) return;
  const ts = Math.floor(Date.now() / 1000);
  for (const a of state.visible) readMap[a.id] = ts;
  saveRead();
  toast(state.visible.length + " artigos marcados como lidos");
  refreshView();
}

async function reloadData() {
  $("#refresh-bar").hidden = false;
  try {
    await loadMeta(true);
    toast("Artigos recarregados");
  } catch (e) {
    toast("Erro ao recarregar: " + e.message);
  } finally {
    $("#refresh-bar").hidden = true;
  }
  state.contentCache.clear();
  renderSidebar();
  refreshView();
}

function refreshView() {
  computeVisible();
  renderList(true);
}

// ---------------------------------------------------------------- leitor

function currentReaderIdx() {
  if (state.readerId == null) return -1;
  return state.visible.findIndex((a) => a.id === state.readerId);
}

async function openReader(idx) {
  const a = state.visible[idx];
  if (!a) return;
  selectRow(idx, false);
  state.readerId = a.id;

  const feed = state.feedsById[a.feed] || { title: "?" };
  const html = await contentFor(a);
  if (state.readerId !== a.id) return; // navegou para outro durante o fetch

  const c = $("#reader-content");
  c.innerHTML =
    '<div class="r-source">' + esc(feed.title) + "</div>" +
    '<h1 class="r-title">' + (a.url ? '<a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer">' + esc(a.title) + "</a>" : esc(a.title)) + "</h1>" +
    '<div class="r-meta">' + esc(fullDate(a.published)) + (a.author ? " · " + esc(a.author) : "") + "</div>" +
    '<div class="r-body">' + (html || "<p><i>" + (a.summary ? esc(a.summary) : "Este feed não traz o conteúdo completo.") + "</i></p>") + "</div>" +
    (a.url ? '<div class="r-footer"><a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer">Ler no site original ↗</a></div>' : "");

  $("#reader-open").href = a.url || "#";
  updateReaderButtons();
  $("#reader").hidden = false;
  $("#reader-scroll").scrollTop = 0;
  document.body.style.overflow = "hidden";

  if (!isRead(a)) markRead(idx, true);
}

function updateReaderButtons() {
  const idx = currentReaderIdx();
  const a = state.visible[idx];
  if (!a) return;
  const star = $("#reader-star");
  star.textContent = isStar(a) ? "★" : "☆";
  star.classList.toggle("on", isStar(a));
  const unreadBtn = $("#reader-unread");
  unreadBtn.classList.toggle("on", !isRead(a));
  unreadBtn.title = isRead(a) ? "Marcar como não lido (m)" : "Marcar como lido (m)";
  $("#reader-prev").disabled = idx <= 0;
  $("#reader-next").disabled = idx >= state.visible.length - 1;
}

function closeReader() {
  $("#reader").hidden = true;
  document.body.style.overflow = "";
  const idx = currentReaderIdx();
  if (idx >= 0) selectRow(idx, true);
  state.readerId = null;
}

function readerNav(delta) {
  const cur = currentReaderIdx();
  if (cur < 0) {
    if (state.visible.length) openReader(0);
    return;
  }
  const next = cur + delta;
  if (next < 0 || next >= state.visible.length) return;
  if (next >= state.shown) renderList(false); // garante que a linha existe
  openReader(next);
}

// ---------------------------------------------------------------- escopo / navegação

function setScope(scope, skipHash) {
  state.scope = scope;
  state.q = "";
  $("#search").value = "";
  $("#title-text").textContent = scope.label;
  if (!skipHash) {
    const h = scope.type === "feed" ? "#/feed/" + scope.id
      : scope.type === "category" ? "#/category/" + encodeURIComponent(scope.id)
      : "#/" + scope.type;
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  if (window.innerWidth <= 860) $("#app").classList.remove("side-open");
  renderSidebar();
  refreshView();
}

function updateTitleCount() {
  if (!state.meta) { $("#title-count").textContent = ""; return; }
  if (state.scope.type === "starred") {
    $("#title-count").textContent = state.visible.length + " salvos";
  } else if (state.filter === "unread") {
    $("#title-count").textContent = state.visible.length + " não lidos";
  } else {
    const unread = state.visible.filter((a) => !isRead(a)).length;
    $("#title-count").textContent = unread + " não lidos";
  }
}

function applyHash() {
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/feed\/([0-9a-f]+)$/))) {
    const id = m[1];
    const feed = state.feedsById[id];
    setScope({ type: "feed", id: id, label: feed ? feed.title : "Feed" }, true);
  } else if ((m = h.match(/^#\/category\/(.+)$/))) {
    const name = decodeURIComponent(m[1]);
    setScope({ type: "category", id: name, label: name }, true);
  } else if (h === "#/today") {
    setScope({ type: "today", id: null, label: "Hoje" }, true);
  } else if (h === "#/starred") {
    setScope({ type: "starred", id: null, label: "Salvos" }, true);
  } else {
    setScope({ type: "all", id: null, label: "Todos os artigos" }, true);
  }
}

// ---------------------------------------------------------------- tema / visual

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("nr-theme", theme);
}

function applyView() {
  $("#list").className = state.view === "cards" ? "view-cards" : "view-list";
  $("#btn-view").textContent = state.view === "cards" ? "☰" : "▦";
  localStorage.setItem("nr-view", state.view);
}

function applyFilterButton() {
  const btn = $("#btn-filter");
  btn.textContent = state.filter === "unread" ? "Não lidos" : "Todos";
  btn.classList.toggle("on", state.filter === "unread");
  localStorage.setItem("nr-filter", state.filter);
}

// ---------------------------------------------------------------- eventos

function bindEvents() {
  $("#btn-menu").addEventListener("click", () => $("#app").classList.toggle("side-open"));
  $("#btn-collapse").addEventListener("click", () => $("#app").classList.toggle("side-collapsed"));

  $$(".nav-item").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      const t = el.dataset.scope;
      const labels = { today: "Hoje", all: "Todos os artigos", starred: "Salvos" };
      setScope({ type: t, id: null, label: labels[t] });
    });
  });

  $("#btn-refresh").addEventListener("click", reloadData);
  $("#btn-mark-all").addEventListener("click", markAllRead);
  $("#btn-more").addEventListener("click", () => renderList(false));

  $("#btn-filter").addEventListener("click", () => {
    state.filter = state.filter === "unread" ? "all" : "unread";
    applyFilterButton();
    refreshView();
  });

  $("#btn-view").addEventListener("click", () => {
    state.view = state.view === "cards" ? "list" : "cards";
    applyView();
  });

  $("#btn-theme").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || "light";
    applyTheme(cur === "dark" ? "light" : "dark");
  });

  let searchTimer = null;
  $("#search").addEventListener("input", (ev) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = ev.target.value.trim();
      refreshView();
    }, 250);
  });

  // leitor
  $("#reader-close").addEventListener("click", closeReader);
  $("#reader-backdrop").addEventListener("click", closeReader);
  $("#reader-prev").addEventListener("click", () => readerNav(-1));
  $("#reader-next").addEventListener("click", () => readerNav(1));
  $("#reader-star").addEventListener("click", () => {
    const idx = currentReaderIdx();
    if (idx >= 0) toggleStar(idx);
    updateReaderButtons();
  });
  $("#reader-unread").addEventListener("click", () => {
    const idx = currentReaderIdx();
    const a = state.visible[idx];
    if (a) markRead(idx, !isRead(a));
    updateReaderButtons();
  });

  // teclado
  document.addEventListener("keydown", (ev) => {
    const inInput = /^(input|textarea|select)$/i.test(document.activeElement.tagName);
    if (ev.key === "Escape") {
      if (!$("#reader").hidden) { closeReader(); return; }
      if (inInput) document.activeElement.blur();
      return;
    }
    if (inInput) return;
    if ((ev.key === "Enter" || ev.key === " ") && /^(button|a)$/i.test(document.activeElement.tagName)) return;

    const readerOpen = !$("#reader").hidden;
    switch (ev.key) {
      case "j":
        if (readerOpen) readerNav(1);
        else if (state.selected < state.visible.length - 1) {
          if (state.selected + 1 >= state.shown) renderList(false);
          selectRow(state.selected + 1, true);
        }
        break;
      case "k":
        if (readerOpen) readerNav(-1);
        else if (state.selected > 0) selectRow(state.selected - 1, true);
        break;
      case "o":
      case "Enter":
        if (!readerOpen && state.selected >= 0) openReader(state.selected);
        break;
      case "m": {
        const idx = readerOpen ? currentReaderIdx() : state.selected;
        const a = state.visible[idx];
        if (a) { markRead(idx, !isRead(a)); if (readerOpen) updateReaderButtons(); }
        break;
      }
      case "s": {
        const idx = readerOpen ? currentReaderIdx() : state.selected;
        if (idx >= 0) { toggleStar(idx); if (readerOpen) updateReaderButtons(); }
        break;
      }
      case "v": {
        const idx = readerOpen ? currentReaderIdx() : state.selected;
        const a = state.visible[idx];
        if (a && a.url) window.open(a.url, "_blank", "noopener");
        break;
      }
      case "r":
        reloadData();
        break;
      case "A":
        markAllRead();
        break;
      case "/":
        ev.preventDefault();
        $("#search").focus();
        break;
    }
  });

  window.addEventListener("hashchange", applyHash);
}

// ---------------------------------------------------------------- init

async function init() {
  applyTheme(localStorage.getItem("nr-theme") ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  applyView();
  applyFilterButton();
  bindEvents();

  $("#refresh-bar").hidden = false;
  try {
    await loadMeta(false);
  } catch (e) {
    $("#list-status").textContent = "Erro ao carregar os dados: " + e.message;
    $("#refresh-bar").hidden = true;
    return;
  }
  $("#refresh-bar").hidden = true;

  if (openCats.size === 0 && state.meta.categories.length) {
    openCats.add(state.meta.categories[0].name);
  }
  renderSidebar();
  applyHash();

  // recarrega os dados de tempos em tempos (o Actions publica a cada 30 min)
  setInterval(async () => {
    const before = state.meta && state.meta.generated_at;
    try { await loadMeta(true); } catch (e) { return; }
    if (state.meta.generated_at !== before) {
      state.contentCache.clear();
      renderSidebar();
      refreshView();
    }
  }, 10 * 60 * 1000);
}

init();
