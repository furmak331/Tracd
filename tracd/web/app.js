// Tracd dashboard. Plain ES modules, no build step.

const $ = (sel, root = document) => root.querySelector(sel);

const S = {
  watches: new Map(),
  order: [],
  settings: {},
  shops: [],
  browser: {},
  logs: [],
  channels: [],
  alertProblem: null,
  demo: false,
  examples: [],
  version: "",
  filter: "all",
  resolving: false,
  resolveError: null,
  resolved: null,
  selected: new Set(),
  interval: 15,
  adding: false,
  openMenu: null,
  history: new Map(),
  online: true,
  notifyPerm: typeof Notification !== "undefined" ? Notification.permission : "unsupported",
};

const INTERVALS = [[10, "10s"], [15, "15s"], [30, "30s"], [60, "1 min"], [300, "5 min"]];

// ---------------------------------------------------------------------------
// tiny DOM helpers
// ---------------------------------------------------------------------------

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

function icon(name, cls = "i") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function replace(el, ...kids) {
  el.replaceChildren();
  append(el, kids);
}

const rupees = (v) => (v ? "₹" + Math.round(v).toLocaleString("en-IN") : "");

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function every(sec) {
  if (sec < 60) return sec + "s";
  if (sec % 60 === 0 && sec < 3600) return sec / 60 + " min";
  return Math.round(sec / 60) + " min";
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function when(ts) {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? clock(ts) : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + clock(ts);
}

function thumb(src, title, cls = "thumb") {
  if (!src) return h("div", { class: cls + " ph" }, (title || "?").trim().charAt(0).toUpperCase());
  const img = h("img", { class: cls, src, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(h("div", { class: cls + " ph" }, (title || "?").trim().charAt(0).toUpperCase())));
  return img;
}

function shopBadge(name, color) {
  return h("span", { class: "badge", style: `--c:${color || "#64748b"}` }, name);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new Error("Tracd is not running. Start it again on your computer.");
  }
  let data = {};
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error(data.error || `Something went wrong (HTTP ${r.status}).`);
  return data;
}

function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + kind, role: "status" },
    kind === "ok" ? icon("check") : kind === "bad" ? icon("alert") : null,
    h("span", { class: "grow" }, msg));
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, kind === "bad" ? 5200 : 2600);
  setTimeout(() => t.remove(), kind === "bad" ? 5600 : 3000);
}

async function busy(btn, fn) {
  btn?.classList.add("busy");
  try { return await fn(); } finally { btn?.classList.remove("busy"); }
}

// ---------------------------------------------------------------------------
// state in
// ---------------------------------------------------------------------------

// What a check found. Answers can arrive out of order (a slow reply to "add"
// after the live update of the first check), so an older one never wins.
const READING = ["state", "detail", "price", "qty", "last_checked", "last_change", "last_in_stock", "checks", "last_ms"];

function takeWatch(w, prev = S.watches.get(w.id)) {
  if (prev && (prev.checks || 0) > (w.checks || 0)) READING.forEach((k) => { w[k] = prev[k]; });
  w._nextAt = w.next_check_in == null ? null : Date.now() + w.next_check_in * 1000;
  S.watches.set(w.id, w);
  if (!S.order.includes(w.id)) S.order.push(w.id);
  return prev;
}

async function loadState() {
  const st = await api("GET", "/api/state");
  S.settings = st.settings;
  S.shops = st.shops;
  S.browser = st.browser;
  S.logs = st.logs;
  S.channels = st.channels;
  S.alertProblem = st.alert_problem;
  S.demo = st.demo;
  S.version = st.version;
  S.examples = st.examples || [];
  const before = new Map(S.watches);
  S.watches.clear();
  S.order = [];
  st.watches.forEach((w) => takeWatch(w, before.get(w.id)));
  S.interval = S.settings.default_interval || 15;
  renderAll();
  if (!S.settings.onboarded && S.watches.size === 0) showWelcome();
}

// Live updates. Browsers allow only a few open connections per site, so with
// several dashboard tabs open just one of them (the "leader", chosen with a
// Web Lock) holds the stream and passes every event on to the others.
const tabs = "BroadcastChannel" in window ? new BroadcastChannel("tracd-live") : null;
let leader = false;

function onEvent(ev) {
  if (ev.type === "_online") {
    // (Re)connected: reload everything, in case updates were missed meanwhile.
    if (ev.online) { S.online = true; loadState().catch(() => {}); }
    if (!ev.online && S.online) { S.online = false; renderPulse(); }
  } else if (ev.type === "watch") {
    const prev = takeWatch(ev.watch);
    renderWatch(ev.watch.id);
    renderPulse();
    renderFilters();
    if (S.history.has(ev.watch.id) && prev && prev.state !== ev.watch.state) loadHistory(ev.watch.id);
  } else if (ev.type === "removed") {
    S.watches.delete(ev.watch_id);
    S.order = S.order.filter((id) => id !== ev.watch_id);
    renderWatches();
    renderPulse();
  } else if (ev.type === "log") {
    S.logs.push(ev.log);
    if (S.logs.length > 200) S.logs.shift();
    renderLog();
  } else if (ev.type === "hit") {
    onHit(ev.watch_id);
  } else if (ev.type === "alert_status") {
    S.alertProblem = ev.problem;
    renderBanner();
  }
}

function share(ev) {
  onEvent(ev);
  if (tabs) try { tabs.postMessage(ev); } catch { /* tab closing */ }
}

function stream() {
  let es;
  const open = () => {
    es = new EventSource("/api/stream");
    es.addEventListener("hello", () => share({ type: "_online", online: true }));
    es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      share(ev);
    };
    es.onerror = () => {
      share({ type: "_online", online: false });
      // The browser retries by itself unless the server answered with an error.
      if (es.readyState === EventSource.CLOSED) setTimeout(open, 3000);
    };
  };
  open();
}

function connect() {
  if (tabs && navigator.locks) {
    tabs.onmessage = (m) => { if (!leader) onEvent(m.data); };
    // Held for as long as this tab is open; the next tab takes over when it closes.
    navigator.locks.request("tracd-live", () => new Promise(() => {
      leader = true;
      document.documentElement.dataset.live = "leader";
      stream();
    }));
  } else {
    leader = true;
    stream();
  }
}

// ---------------------------------------------------------------------------
// hits: sound, desktop notification, flash
// ---------------------------------------------------------------------------

let audio;
document.addEventListener("pointerdown", () => {
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch { /* no audio */ }
}, { once: false, passive: true });

function chime() {
  if (!audio) return;
  const t0 = audio.currentTime;
  [880, 1175, 1568, 1175, 1568].forEach((f, i) => {
    const o = audio.createOscillator();
    const g = audio.createGain();
    o.type = "sine";
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + i * 0.16);
    g.gain.exponentialRampToValueAtTime(0.35, t0 + i * 0.16 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.16 + 0.15);
    o.connect(g).connect(audio.destination);
    o.start(t0 + i * 0.16);
    o.stop(t0 + i * 0.16 + 0.16);
  });
}

let titleTimer;
function onHit(id) {
  const w = S.watches.get(id);
  if (!w) return;
  if (leader) chime();  // one sound and one notification, however many tabs are open
  if (leader && S.notifyPerm === "granted") {
    try {
      const n = new Notification(`${w.size_label} in stock on ${w.shop_name}`, {
        body: w.title, icon: w.image || "/static/icon.svg", requireInteraction: true, tag: "tracd-" + id,
      });
      n.onclick = () => { window.open(w.open_url, "_blank", "noopener"); n.close(); };
    } catch { /* ignore */ }
  }
  const card = document.querySelector(`[data-watch="${id}"]`);
  if (card) {
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
  }
  clearInterval(titleTimer);
  let on = false;
  let n = 0;
  titleTimer = setInterval(() => {
    document.title = (on = !on) ? `● ${w.size_label} IN STOCK` : "Tracd";
    if (++n > 30 || document.hasFocus()) { clearInterval(titleTimer); document.title = "Tracd"; }
  }, 900);
}

// ---------------------------------------------------------------------------
// render: header, banner, shops
// ---------------------------------------------------------------------------

function renderAll() {
  renderPulse();
  renderBanner();
  renderShops();
  renderPicker();
  renderFilters();
  renderWatches();
  renderLog();
  $("#version").textContent = "v" + S.version;
}

function renderPulse() {
  const el = $("#pulse");
  const ws = [...S.watches.values()];
  const live = ws.filter((w) => w.enabled).length;
  const hits = ws.filter((w) => w.enabled && w.state === "IN_STOCK").length;
  if (!S.online) {
    replace(el, h("span", { class: "beat idle" }), "Reconnecting…");
    return;
  }
  const bits = [];
  if (S.demo) bits.push("Demo mode");
  bits.push(live ? `Watching ${live} size${live === 1 ? "" : "s"}` : "Nothing watched yet");
  if (hits) bits.push(`${hits} in stock`);
  replace(el, h("span", { class: "beat" + (live ? "" : " idle") }), bits.join(" · "),
    S.browser?.browser ? h("span", { class: "long muted" }, " · " + S.browser.browser) : null);
  $("#settings-dot").hidden = S.channels.length > 0 && !S.alertProblem && !!S.settings.alerts_verified;
}

function renderBanner() {
  const el = $("#banner");
  el.replaceChildren();
  if (S.alertProblem) {
    el.append(h("div", { class: "banner" }, icon("alert"),
      h("span", { class: "grow" }, "Phone alerts are failing: " + S.alertProblem),
      h("button", { class: "btn small ghost", type: "button", onclick: openSettings }, "Fix")));
  } else if (!S.channels.length) {
    el.append(h("div", { class: "banner info" }, icon("phone"),
      h("span", { class: "grow" }, "Phone alerts are off. Turn one on so a restock reaches you wherever you are."),
      h("button", { class: "btn small primary", type: "button", onclick: openSettings }, "Set up")));
  } else if (!S.settings.alerts_verified) {
    el.append(h("div", { class: "banner info" }, icon("phone"),
      h("span", { class: "grow" }, "Connect your phone: subscribe to your alert topic in the ntfy app and send yourself a test."),
      h("button", { class: "btn small primary", type: "button", onclick: openSettings }, "Connect phone")));
  }
  if (S.demo) {
    el.append(h("div", { class: "banner info" }, icon("spark"),
      h("span", { class: "grow" }, "Demo mode: the shops are simulated and a sold-out size restocks every minute or so. Try one of the example links below.")));
  }
}

function renderShops() {
  const ul = $("#shop-list");
  replace(ul, S.shops.map((s) => h("li", {}, h("i", { style: `background:${s.color}` }), s.name)),
    h("li", {}, h("i", { style: "background:var(--text-3)" }), "+ Shopify stores"));
}

// ---------------------------------------------------------------------------
// render: add flow (size picker)
// ---------------------------------------------------------------------------

function renderPicker() {
  const el = $("#picker");
  if (S.resolving) {
    el.hidden = false;
    replace(el,
      h("div", { class: "product" }, h("div", { class: "thumb skeleton" }),
        h("div", { style: "flex:1" }, h("div", { class: "skeleton", style: "height:18px;width:70%" }),
          h("div", { class: "skeleton", style: "height:14px;width:40%;margin-top:10px" }))),
      h("div", { class: "label" }, "Reading sizes from the shop…"),
      h("div", { class: "sizes" }, Array.from({ length: 6 }, () => h("div", { class: "skeleton", style: "height:64px;border-radius:14px" }))));
    return;
  }
  if (S.resolveError) {
    el.hidden = false;
    replace(el, h("div", { class: "note warn", role: "alert" }, S.resolveError));
    return;
  }
  const r = S.resolved;
  if (!r) {
    el.hidden = true;
    el.replaceChildren();
    if (S.demo) {
      el.hidden = false;
      el.append(h("div", { class: "label" }, "Example links"),
        h("div", { class: "inline" }, S.examples.map((ex) => {
          const s = S.shops.find((x) => x.key === ex.shop) || { name: ex.shop, color: "#64748b" };
          return h("button", { class: "btn small ghost", type: "button", onclick: () => { $("#link").value = ex.url; resolve(); } },
            h("i", { style: `width:8px;height:8px;border-radius:3px;background:${s.color}` }), s.name);
        })));
    }
    return;
  }
  const p = r.product;
  const watched = r.existing || {};
  el.hidden = false;
  const chosen = p.sizes.filter((s) => S.selected.has(s.id));
  const inNow = chosen.filter((s) => s.available === true);
  replace(el,
    h("div", { class: "product" },
      thumb(p.image, p.title),
      h("div", { style: "flex:1;min-width:0" },
        h("h2", {}, p.title),
        h("div", { class: "meta" },
          shopBadge(r.shop.name, r.shop.color),
          p.brand ? h("span", {}, p.brand) : null,
          p.price ? h("span", { class: "price" }, rupees(p.price), p.mrp && p.mrp > p.price ? h("s", {}, rupees(p.mrp)) : null) : null,
          p.colour ? h("span", { class: "muted" }, p.colour) : null)),
      h("button", { class: "btn icon ghost small", type: "button", "aria-label": "Close", onclick: clearPicker }, icon("x"))),
    p.note ? h("div", { class: "note" }, p.note) : null,
    h("div", { class: "label" }, p.sizes.length > 1 ? "Pick the sizes to watch" : "Size"),
    h("div", { class: "sizes", role: "group", "aria-label": "Sizes" }, p.sizes.map((s) => {
      const isWatched = s.id in watched;
      const sub = isWatched ? "Watching" : s.available === true ? (s.qty ? `${s.qty} left` : "In stock") : s.available === false ? "Sold out" : "Tap to watch";
      return h("button", {
        class: "size " + (s.available === true ? "in" : s.available === false ? "out" : "") + (isWatched ? " watched" : ""),
        type: "button", "aria-pressed": S.selected.has(s.id) ? "true" : "false",
        onclick: () => { S.selected.has(s.id) ? S.selected.delete(s.id) : S.selected.add(s.id); renderPicker(); },
      }, h("b", {}, s.label), h("small", {}, sub));
    })),
    inNow.length ? h("div", { class: "note" },
      `${inNow.map((s) => s.label).join(", ")} ${inNow.length === 1 ? "is" : "are"} in stock right now, so you'll get an alert on the first check.`) : null,
    h("div", { class: "picker-foot" },
      h("div", { class: "every" }, icon("clock"), "Check every",
        h("div", { class: "seg", role: "group", "aria-label": "Check every" }, INTERVALS.map(([v, l]) =>
          h("button", { type: "button", "aria-pressed": S.interval === v ? "true" : "false", onclick: () => { S.interval = v; renderPicker(); } }, l)))),
      h("button", {
        class: "btn primary" + (S.adding ? " busy" : ""), type: "button", disabled: chosen.length === 0, id: "watch-btn", onclick: addWatches,
      }, icon("bell"), h("span", {}, chosen.length === 0 ? "Pick a size" : chosen.length === 1 ? `Watch ${chosen[0].label}` : `Watch ${chosen.length} sizes`))));
}

function clearPicker() {
  S.resolved = null;
  S.resolveError = null;
  S.selected.clear();
  renderPicker();
}

async function resolve(ev) {
  ev?.preventDefault();
  const text = $("#link").value.trim();
  if (!text) { $("#link").focus(); return; }
  S.resolving = true;
  S.resolveError = null;
  S.resolved = null;
  S.selected.clear();
  renderPicker();
  const btn = $("#find-btn");
  try {
    await busy(btn, async () => {
      const r = await api("POST", "/api/resolve", { text });
      S.resolved = r;
      S.interval = r.default_interval || S.interval;
      const sel = r.product.selected_size_id;
      if (sel && r.product.sizes.some((s) => s.id === sel) && !(sel in (r.existing || {}))) S.selected.add(sel);
      else if (r.product.sizes.length === 1) S.selected.add(r.product.sizes[0].id);
    });
  } catch (e) {
    S.resolveError = e.message;
  } finally {
    S.resolving = false;
    renderPicker();
  }
}

async function addWatches() {
  const r = S.resolved;
  if (!r || !S.selected.size) return;
  S.adding = true;
  renderPicker();
  const added = [];
  try {
    for (const sid of [...S.selected]) {
      const res = await api("POST", "/api/watches", { url: r.product.url, size_id: sid, interval: S.interval });
      takeWatch(res.watch);
      added.push(res.watch);
    }
    toast(added.length === 1 ? `Watching ${added[0].size_label} on ${added[0].shop_name}` : `Watching ${added.length} sizes on ${added[0].shop_name}`, "ok");
    $("#link").value = "";
    clearPicker();
    renderWatches();
    renderPulse();
    renderFilters();
    const card = document.querySelector(`[data-watch="${added[0].id}"]`);
    card?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (!S.settings.onboarded) api("PUT", "/api/settings", { onboarded: true }).then((d) => { S.settings = d.settings; }).catch(() => {});
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    S.adding = false;
    renderPicker();
  }
}

// ---------------------------------------------------------------------------
// render: watches
// ---------------------------------------------------------------------------

const FILTERS = [
  ["all", "All", () => true],
  ["in", "In stock", (w) => w.enabled && w.state === "IN_STOCK"],
  ["out", "Sold out", (w) => w.enabled && w.state === "OUT"],
  ["issues", "Needs a look", (w) => w.enabled && (w.state === "UNKNOWN" || w.state === "BLOCKED")],
  ["paused", "Paused", (w) => !w.enabled],
];

function sortedWatches() {
  const rank = (w) => (!w.enabled ? 4 : w.state === "IN_STOCK" ? 0 : w.state === "BLOCKED" || w.state === "UNKNOWN" ? 1 : 2);
  return S.order.map((id) => S.watches.get(id)).filter(Boolean)
    .sort((a, b) => rank(a) - rank(b) || b.created_at - a.created_at);
}

function renderFilters() {
  const ws = [...S.watches.values()];
  const el = $("#filters");
  if (ws.length < 2) { el.replaceChildren(); return; }
  replace(el, FILTERS.map(([key, label, fn]) => {
    const n = ws.filter(fn).length;
    if (key !== "all" && n === 0 && S.filter !== key) return null;
    return h("button", {
      type: "button", role: "tab", "aria-selected": S.filter === key ? "true" : "false",
      onclick: () => { S.filter = key; renderFilters(); renderWatches(); },
    }, label, h("span", { class: "n" }, n));
  }));
}

function renderWatches() {
  const list = $("#watch-list");
  const fn = (FILTERS.find((f) => f[0] === S.filter) || FILTERS[0])[2];
  const ws = sortedWatches().filter(fn);
  if (!S.watches.size) {
    replace(list, h("div", { class: "empty" },
      h("div", { class: "art" }, icon("bell")),
      h("h3", {}, "Nothing watched yet"),
      h("p", {}, "Paste a product link above and choose the size you want. The moment it's back in stock, your phone buzzes.")));
    return;
  }
  if (!ws.length) {
    replace(list, h("div", { class: "empty" }, h("p", {}, "Nothing here right now.")));
    return;
  }
  replace(list, ws.map(watchCard));
}

function renderWatch(id) {
  const w = S.watches.get(id);
  const old = document.querySelector(`[data-watch="${id}"]`);
  const fn = (FILTERS.find((f) => f[0] === S.filter) || FILTERS[0])[2];
  const order = sortedWatches().filter(fn).map((x) => x.id);
  const oldOrder = [...document.querySelectorAll("[data-watch]")].map((e) => Number(e.dataset.watch));
  if (!w || !old || order.join() !== oldOrder.join()) { renderWatches(); return; }
  old.replaceWith(watchCard(w));
}

function statusOf(w) {
  if (!w.enabled) return ["paused", "Paused", "Not checking. Resume to keep watching."];
  if (!w.state) return ["out", w.checking ? "Checking…" : "Starting…", "First check is on its way"];
  const price = w.price ? rupees(w.price) : "";
  if (w.state === "IN_STOCK") {
    const bits = [price, w.qty ? `only ${w.qty} left` : "", w.last_in_stock ? "since " + ago(w.last_in_stock).replace(" ago", "") : ""].filter(Boolean);
    return ["in", "In stock now", bits.join(" · ") || "Go go go"];
  }
  if (w.state === "OUT") return ["out", "Sold out", w.last_in_stock ? `Last in stock ${ago(w.last_in_stock)}` : "Watching for a restock"];
  if (w.state === "BLOCKED") return ["blocked", "Shop wants a quick check", "Open the shop window and clear its verification page"];
  return ["unknown", "Can't read right now", w.detail || "Retrying"];
}

function watchCard(w) {
  const [cls, big, small] = statusOf(w);
  const hit = w.enabled && w.state === "IN_STOCK";
  const menuOpen = S.openMenu === w.id;
  const card = h("article", {
    class: "card watch" + (hit ? " hit" : "") + (w.enabled ? "" : " paused"),
    dataset: { watch: String(w.id) },
    "aria-label": `${w.title}, ${w.size_label}, ${big}`,
  },
    h("div", { class: "top" },
      thumb(w.image, w.title),
      h("div", { style: "min-width:0;flex:1" },
        h("div", { class: "title", title: w.title }, w.title),
        h("div", { class: "row" }, h("span", { class: "sizetag" }, w.size_label), shopBadge(w.shop_name, w.shop_color)))),
    h("div", { class: "status " + cls },
      w.checking ? h("span", { class: "spinner", role: "img", "aria-label": "Checking" }) : h("span", { class: "lamp" }),
      h("div", { class: "grow" }, h("div", { class: "big" }, big), h("div", { class: "small", title: small }, small))),
    h("div", { class: "meta-line" },
      h("span", { title: w.last_checked ? `Last checked ${when(w.last_checked)}` + (w.last_ms != null ? `, answered in ${w.last_ms} ms` : "") : "" }, icon("refresh"), h("span", { dataset: { ago: w.last_checked || "" } }, w.last_checked ? "checked " + ago(w.last_checked) : "not checked yet")),
      h("span", {}, icon("clock"), "every " + every(w.interval)),
      w.enabled && w._nextAt && !w.checking ? h("span", { dataset: { next: String(w._nextAt) } }, nextText(w._nextAt)) : null),
    actions(w),
    menuOpen ? menu(w) : null,
    S.history.has(w.id) ? historyView(w.id) : null);
  return card;
}

function nextText(at) {
  const s = Math.round((at - Date.now()) / 1000);
  return s <= 0 ? "checking soon" : `next in ${s}s`;
}

function actions(w) {
  const hit = w.enabled && w.state === "IN_STOCK";
  const kids = [];
  if (hit) {
    kids.push(h("a", { class: "btn ok buy", href: w.open_url, target: "_blank", rel: "noopener noreferrer" },
      h("span", {}, `Buy on ${w.shop_name}`), icon("ext")));
    if (!w.acked && w.alerts_sent > 0) {
      kids.push(h("button", { class: "btn ghost", type: "button", title: "Stop the reminder alerts for this restock", onclick: (e) => act(e.currentTarget, w, "ack") }, icon("check"), h("span", {}, "Got it")));
    }
  } else {
    if (w.enabled && w.state === "BLOCKED") {
      kids.push(h("button", { class: "btn small primary", type: "button", onclick: (e) => showShop(e.currentTarget, w) }, icon("window"), h("span", {}, "Open shop window")));
    } else {
      kids.push(h("a", { class: "btn small ghost", href: w.open_url, target: "_blank", rel: "noopener noreferrer" }, h("span", {}, "Open"), icon("ext")));
    }
    kids.push(h("span", { class: "spacer" }));
    if (w.enabled) {
      kids.push(h("button", { class: "btn icon small ghost", type: "button", title: "Check now", "aria-label": "Check now", onclick: (e) => act(e.currentTarget, w, "check") }, icon("refresh")));
    }
    kids.push(h("button", {
      class: "btn icon small ghost", type: "button", title: w.enabled ? "Pause" : "Resume", "aria-label": w.enabled ? "Pause" : "Resume",
      onclick: (e) => act(e.currentTarget, w, w.enabled ? "pause" : "resume"),
    }, icon(w.enabled ? "pause" : "play")));
  }
  kids.push(h("button", {
    class: "btn icon small ghost", type: "button", "aria-label": "More", "aria-expanded": S.openMenu === w.id ? "true" : "false",
    onclick: (e) => { e.stopPropagation(); S.openMenu = S.openMenu === w.id ? null : w.id; renderWatch(w.id); },
  }, icon("more")));
  return h("div", { class: "actions" }, kids);
}

function menu(w) {
  return h("div", { class: "menu", role: "menu", onclick: (e) => e.stopPropagation() },
    h("div", { class: "menu-label" }, "Check every"),
    h("div", { class: "seg" }, INTERVALS.map(([v, l]) => h("button", {
      type: "button", "aria-pressed": w.interval === v ? "true" : "false",
      onclick: async () => { try { const r = await api("PATCH", `/api/watches/${w.id}`, { interval: v }); takeWatch(r.watch); renderWatch(w.id); } catch (e) { toast(e.message, "bad"); } },
    }, l))),
    h("hr"),
    w.enabled && w.state !== "IN_STOCK" ? null : h("button", { type: "button", role: "menuitem", onclick: (e) => { S.openMenu = null; act(e.currentTarget, w, w.enabled ? "pause" : "resume"); } }, icon(w.enabled ? "pause" : "play"), w.enabled ? "Pause" : "Resume"),
    h("button", { type: "button", role: "menuitem", onclick: () => { S.openMenu = null; toggleHistory(w.id); } }, icon("history"), S.history.has(w.id) ? "Hide history" : "History"),
    h("button", { type: "button", role: "menuitem", onclick: (e) => { S.openMenu = null; showShop(e.currentTarget, w); } }, icon("window"), "Show in tracker browser"),
    h("button", { type: "button", role: "menuitem", onclick: () => { navigator.clipboard?.writeText(w.open_url); S.openMenu = null; renderWatch(w.id); toast("Link copied", "ok"); } }, icon("copy"), "Copy link"),
    h("hr"),
    h("button", { type: "button", role: "menuitem", class: "danger", onclick: () => remove(w) }, icon("trash"), "Stop watching"));
}

document.addEventListener("click", () => {
  if (S.openMenu != null) { const id = S.openMenu; S.openMenu = null; renderWatch(id); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (S.openMenu != null) { const id = S.openMenu; S.openMenu = null; renderWatch(id); }
  if (!$("#sheet").hidden) closeSettings();
  if (!$("#modal").hidden) closeModal();
});

async function act(btn, w, what) {
  try {
    await busy(btn, async () => {
      let r;
      if (what === "check") r = await api("POST", `/api/watches/${w.id}/check`);
      else if (what === "ack") r = await api("POST", `/api/watches/${w.id}/ack`);
      else r = await api("PATCH", `/api/watches/${w.id}`, { enabled: what === "resume" });
      takeWatch(r.watch);
    });
    renderWatch(w.id);
    renderPulse();
    renderFilters();
    if (what === "check") {
      const x = S.watches.get(w.id);
      toast(x.state === "IN_STOCK" ? `${x.size_label} is in stock!` : x.state === "OUT" ? `${x.size_label} is still sold out` : "Couldn't read the shop: " + (x.detail || "try again"), x.state === "UNKNOWN" || x.state === "BLOCKED" ? "bad" : "ok");
    }
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function remove(w) {
  S.openMenu = null;
  try {
    await api("DELETE", `/api/watches/${w.id}`);
    S.watches.delete(w.id);
    S.order = S.order.filter((id) => id !== w.id);
    S.history.delete(w.id);
    renderWatches();
    renderPulse();
    renderFilters();
    toast(`Stopped watching ${w.size_label}`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function showShop(btn, w) {
  try {
    await busy(btn, () => api("POST", "/api/browser/show", { url: w.poll_url }));
    toast("The shop is open in the tracker's browser window. Clear any check there, then come back.", "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function loadHistory(id) {
  try {
    const r = await api("GET", `/api/watches/${id}/events`);
    S.history.set(id, r.events);
  } catch {
    S.history.set(id, []);
  }
  renderWatch(id);
}

function toggleHistory(id) {
  if (S.history.has(id)) { S.history.delete(id); renderWatch(id); return; }
  S.history.set(id, null);
  renderWatch(id);
  loadHistory(id);
}

const STATE_WORDS = { IN_STOCK: "In stock", OUT: "Sold out", UNKNOWN: "Couldn't read", BLOCKED: "Verification page" };

function historyView(id) {
  const evs = S.history.get(id);
  if (evs == null) return h("div", { class: "history" }, h("div", { class: "skeleton", style: "height:14px" }));
  if (!evs.length) return h("div", { class: "history muted" }, "No changes yet.");
  return h("div", { class: "history" }, evs.map((e) => h("div", { class: "ev " + e.state },
    h("i"), h("span", {}, STATE_WORDS[e.state] || e.state, e.price ? " · " + rupees(e.price) : "", e.state === "UNKNOWN" && e.detail ? h("span", { class: "muted" }, " · " + e.detail) : null),
    h("time", {}, when(e.at)))));
}

// ---------------------------------------------------------------------------
// activity log
// ---------------------------------------------------------------------------

function renderLog() {
  const ol = $("#log");
  const logs = S.logs.slice(-150).reverse();
  $("#activity-count").textContent = logs.length ? `${logs.length} recent` : "";
  replace(ol, logs.length ? logs.map((l) => h("li", {}, h("time", {}, clock(l.at)), h("span", { class: l.level }, l.msg)))
    : h("li", {}, h("time", {}, ""), h("span", { class: "muted" }, "Nothing yet.")));
}

// ---------------------------------------------------------------------------
// settings sheet
// ---------------------------------------------------------------------------

function openSettings() {
  $("#sheet").hidden = false;
  renderSettings();
  setTimeout(() => $("#sheet .sheet-panel button")?.focus(), 50);
}

function closeSettings() {
  $("#sheet").hidden = true;
  $("#open-settings").focus();
}

$("#open-settings").addEventListener("click", openSettings);
$("#sheet").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSettings(); });

async function save(patch, quiet = false) {
  try {
    const r = await api("PUT", "/api/settings", patch);
    S.settings = r.settings;
    S.channels = r.channels;
    renderBanner();
    renderPulse();
    if (!quiet) toast("Saved", "ok");
    return true;
  } catch (e) {
    toast(e.message, "bad");
    renderSettings();
    return false;
  }
}

function sw(key, label, hint) {
  return h("label", { class: "switch" },
    h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null),
    h("input", { type: "checkbox", role: "switch", checked: !!S.settings[key], onchange: async (e) => { await save({ [key]: e.target.checked }); renderSettings(); } }));
}

function num(key, label, min, max, suffix) {
  return h("label", { class: "num" }, h("span", {}, label),
    h("span", { class: "inline", style: "flex-wrap:nowrap" },
      h("input", { class: "input", type: "number", min, max, value: S.settings[key], onchange: (e) => save({ [key]: Number(e.target.value) }) }),
      suffix ? h("span", { class: "muted" }, suffix) : null));
}

function renderSettings() {
  const s = S.settings;
  const body = $("#sheet-body");
  const topic = s.ntfy_topic || "";
  const server = (s.ntfy_server || "https://ntfy.sh").replace(/\/$/, "");
  const ntfyOn = S.channels.includes("ntfy");
  const tgOn = S.channels.includes("telegram");

  const testBtn = h("button", { class: "btn primary", type: "button", disabled: !S.channels.length, onclick: async (e) => {
    try {
      const r = await busy(e.currentTarget, () => api("POST", "/api/settings/test"));
      const bad = Object.entries(r.results).filter(([, v]) => v);
      if (!bad.length) toast("Test sent. Check your phone.", "ok");
      else toast(bad.map(([k, v]) => `${k}: ${v}`).join(" · "), "bad");
      if (r.ok) { S.alertProblem = null; S.settings = r.settings; }
      renderBanner();
      renderPulse();
    } catch (err) { toast(err.message, "bad"); }
  } }, icon("send"), h("span", {}, "Send a test alert"));

  const tokenInput = h("input", { class: "input", type: "password", placeholder: s.telegram_token_set ? "Saved. Paste a new one to change it" : "123456:ABC-your-bot-token", autocomplete: "off" });
  const chatInput = h("input", { class: "input", type: "text", placeholder: "Your chat id", value: s.telegram_chat_id || "", onchange: (e) => save({ telegram_chat_id: e.target.value }) });

  replace(body,
    h("p", { class: "muted", style: "margin:0" }, "Alerts go to every channel you turn on, at the same moment. ntfy is the quickest to set up."),
    // ntfy
    h("section", { class: "panel" },
      h("h3", {}, icon("phone"), "ntfy push", h("span", { class: "tag" + (ntfyOn ? " on" : "") }, ntfyOn ? "On" : "Off")),
      sw("ntfy_enabled", "Send alerts with ntfy", "Free, no account, loud on your phone."),
      h("ol", { class: "steps" },
        h("li", {}, h("span", {}, "Install the ntfy app: ",
          h("a", { href: "https://play.google.com/store/apps/details?id=io.heckel.ntfy", target: "_blank", rel: "noopener" }, "Android"), " or ",
          h("a", { href: "https://apps.apple.com/app/ntfy/id1625396347", target: "_blank", rel: "noopener" }, "iPhone"), ".")),
        h("li", {}, h("div", { style: "display:grid;gap:10px" },
          h("span", {}, "Tap ", h("b", {}, "+"), " in the app and subscribe to this topic, or scan the code with your camera."),
          h("div", { class: "qr" },
            h("img", { src: "/api/ntfy-qr.svg?t=" + encodeURIComponent(server + topic), alt: "QR code for your ntfy topic" }),
            h("div", { style: "display:grid;gap:8px;min-width:0;flex:1" },
              h("div", { class: "topic" }, h("span", {}, topic),
                h("button", { class: "btn icon small ghost", type: "button", "aria-label": "Copy topic", onclick: () => { navigator.clipboard?.writeText(topic); toast("Topic copied", "ok"); } }, icon("copy"))),
              h("span", { class: "muted", style: "font-size:12.5px" }, "Keep it to yourself: anyone with this topic can read your alerts."))))),
        h("li", {}, h("span", {}, "On Android, open the topic's settings in ntfy and turn on ", h("b", {}, "Instant delivery"), " so alerts land within a second."))),
      h("details", { class: "adv" }, h("summary", {}, "Advanced"),
        h("div", { style: "display:grid;gap:12px" },
          h("div", { class: "form-row" }, h("label", {}, "Server"), h("input", { class: "input", value: s.ntfy_server, onchange: (e) => save({ ntfy_server: e.target.value }) })),
          h("div", { class: "form-row" }, h("label", {}, "Topic"), h("input", { class: "input", value: topic, onchange: async (e) => { if (await save({ ntfy_topic: e.target.value })) renderSettings(); } })),
          h("div", { class: "form-row" }, h("label", {}, "Access token (only for a protected topic)"),
            h("input", { class: "input", type: "password", placeholder: s.ntfy_token_set ? "Saved" : "tk_…", onchange: (e) => save({ ntfy_token: e.target.value }) }))))),
    // telegram
    h("section", { class: "panel" },
      h("h3", {}, icon("send"), "Telegram", h("span", { class: "tag" + (tgOn ? " on" : "") }, tgOn ? "On" : "Off")),
      sw("telegram_enabled", "Send alerts on Telegram", "Handy if you live in Telegram anyway."),
      h("ol", { class: "steps" },
        h("li", {}, h("span", {}, "In Telegram, open ", h("a", { href: "https://t.me/BotFather", target: "_blank", rel: "noopener" }, "@BotFather"), ", send ", h("b", {}, "/newbot"), " and copy the token it gives you.")),
        h("li", {}, h("div", { style: "display:grid;gap:8px;width:100%" }, h("span", {}, "Paste the token, then send your new bot any message."), tokenInput)),
        h("li", {}, h("div", { style: "display:grid;gap:8px;width:100%" }, h("span", {}, "Find your chat:"),
          h("div", { class: "inline" }, chatInput,
            h("button", { class: "btn ghost", type: "button", onclick: async (e) => {
              try {
                const r = await busy(e.currentTarget, () => api("POST", "/api/telegram/find-chat", { token: tokenInput.value.trim() }));
                S.settings = r.settings; S.channels = r.channels;
                toast("Telegram is connected", "ok");
                renderSettings(); renderBanner(); renderPulse();
              } catch (err) { toast(err.message, "bad"); }
            } }, "Find my chat")))))),
    h("section", { class: "panel" }, h("h3", {}, icon("send"), "Check it works"),
      h("p", {}, "Sends one urgent test alert to every channel that's on."), testBtn),
    // this computer
    h("section", { class: "panel" },
      h("h3", {}, icon("window"), "On this computer"),
      sw("auto_open", "Open the product page on a restock", "So it's ready to buy when you get to your desk."),
      sw("beep", "Beep on a restock"),
      h("div", { class: "switch" }, h("span", {}, "Desktop notifications", h("span", { class: "hint" }, S.notifyPerm === "granted" ? "On while this page is open." : S.notifyPerm === "denied" ? "Blocked in your browser's site settings." : "Shown while this page is open.")),
        S.notifyPerm === "granted" ? h("span", { class: "tag on" }, "On") : S.notifyPerm === "unsupported" || S.notifyPerm === "denied" ? h("span", { class: "tag" }, "Off") :
          h("button", { class: "btn small ghost", type: "button", onclick: async () => { S.notifyPerm = await Notification.requestPermission(); renderSettings(); } }, "Allow"))),
    // behaviour
    h("section", { class: "panel" },
      h("h3", {}, icon("bell"), "Alert behaviour"),
      num("repeat_alerts", "Alerts per restock", 1, 30, "times"),
      num("repeat_every", "Time between them", 5, 600, "sec"),
      sw("notify_sold_out_again", "Tell me when it sells out again"),
      num("blind_after", "Warn me if a shop can't be read for", 30, 3600, "sec"),
      num("default_interval", "Default check speed", 5, 3600, "sec")));
}

// ---------------------------------------------------------------------------
// welcome
// ---------------------------------------------------------------------------

function closeModal() {
  $("#modal").hidden = true;
  $("#modal").replaceChildren();
}

function showWelcome() {
  const m = $("#modal");
  m.hidden = false;
  const done = (then) => { closeModal(); api("PUT", "/api/settings", { onboarded: true }).then((d) => { S.settings = d.settings; }).catch(() => {}); then?.(); };
  replace(m, h("div", { class: "modal-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "welcome-title" },
    h("h2", { id: "welcome-title" }, "Welcome to Tracd"),
    h("p", { class: "lead" }, "Never miss your size again. Three steps:"),
    h("div", { class: "three" },
      h("div", {}, h("span", { class: "ic" }, icon("link")), h("span", {}, h("b", {}, "Paste a product link"), "From Flipkart, Myntra, AJIO, Amazon, Tata CLiQ or Nykaa.")),
      h("div", {}, h("span", { class: "ic" }, icon("check")), h("span", {}, h("b", {}, "Pick your size"), "Sold out is fine. That's the point.")),
      h("div", {}, h("span", { class: "ic" }, icon("phone")), h("span", {}, h("b", {}, "Get an instant alert"), "Your phone buzzes the second it's back."))),
    h("div", { class: "btns" },
      h("button", { class: "btn ghost", type: "button", onclick: () => done(() => $("#link").focus()) }, "Later"),
      h("button", { class: "btn primary", type: "button", onclick: () => done(openSettings) }, icon("phone"), h("span", {}, "Set up phone alerts")))));
  setTimeout(() => m.querySelector(".btn.primary")?.focus(), 50);
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

$("#add-form").addEventListener("submit", resolve);
$("#link").addEventListener("paste", () => setTimeout(() => { if ($("#link").value.trim()) resolve(); }, 0));

if (navigator.clipboard?.readText && window.isSecureContext) {
  const b = $("#paste-btn");
  b.hidden = false;
  b.addEventListener("click", async () => {
    try { $("#link").value = await navigator.clipboard.readText(); resolve(); } catch { $("#link").focus(); }
  });
}

setInterval(() => {
  document.querySelectorAll("[data-ago]").forEach((el) => {
    const ts = Number(el.dataset.ago);
    if (ts) el.textContent = "checked " + ago(ts);
  });
  document.querySelectorAll("[data-next]").forEach((el) => { el.textContent = nextText(Number(el.dataset.next)); });
}, 1000);

// If Tracd is not running yet, say so and keep trying: the page fills in by
// itself once it starts.
S.online = false;
loadState().then(() => { S.online = true; renderPulse(); }).catch(() => {
  replace($("#banner"), h("div", { class: "banner" }, icon("alert"),
    h("span", { class: "grow" }, "Can't reach Tracd on this computer. Is it running? This page will update by itself once it is.")));
  renderPulse();
});
connect();
