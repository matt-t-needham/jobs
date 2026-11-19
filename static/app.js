"use strict";
// jobs UI: follows /api/events (SSE) and draws one tile per task, grouped by
// project. Every string from the server goes in through textContent: names,
// details and needs are written by a model and may carry hostile text.

const LABELS = {
  "needs-you": "needs you", stalled: "stalled", working: "working",
  idle: "idle", dead: "dead", done: "done",
};
const ORDER = Object.keys(LABELS);

let state = null;
let connected = false;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function mark(status) {
  const m = el("span", "mark " + status);
  m.setAttribute("role", "img");
  m.setAttribute("aria-label", LABELS[status] || status);
  m.title = LABELS[status] || status;
  return m;
}

function ago(ms) {
  if (!ms) return null;
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}

function joined(parts) {
  const p = el("p", "meta");
  parts.filter(Boolean).forEach((part, i) => {
    if (i) p.append(el("span", "sep", "·"));
    p.append(part);
  });
  return p;
}

function tile(t) {
  const li = el("li", "tile " + t.status);
  const head = el("div", "head");
  head.append(mark(t.status), el("span", "name", t.name || "untitled"));
  li.append(head);
  if (t.status === "needs-you" && t.needs) {
    li.append(el("p", "needs", t.needs));
  } else if (t.detail) {
    const d = el("p", "detail", t.detail);
    d.title = t.detail;
    li.append(d);
  }
  const where = [t.repo, t.branch].filter(Boolean).join(" · ");
  li.append(joined([
    where ? el("span", "mono", where) : null,
    ago(t.lastActivityAt),
    typeof t.costUsd === "number" ? el("span", "mono", "$" + t.costUsd.toFixed(2)) : null,
  ]));
  return li;
}

function renderSite() {
  const nav = document.getElementById("site");
  nav.replaceChildren();
  const site = state && state.site;
  const label = site ? site.name : (state && state.machine ? state.machine.label : "");
  if (site && site.url) {
    const a = el("a", null, site.name);
    a.href = site.url;
    nav.append(a);
  } else if (label) {
    nav.append(label);
  }
  if (label) nav.append(el("span", "sep", "/"));
  nav.append("jobs");
  document.title = label ? "Jobs · " + label : "Jobs";
}

function renderNotices() {
  const box = document.getElementById("notices");
  box.replaceChildren();
  if (!connected) {
    box.append(el("p", "notice muted", state ? "Reconnecting… tiles show the last update." : "Connecting…"));
  }
  for (const w of (state && state.warnings) || []) {
    const p = el("p", "notice");
    p.append(mark("needs-you"), el("span", null, w));
    box.append(p);
  }
}

function render() {
  renderSite();
  renderNotices();
  const root = document.getElementById("projects");
  root.replaceChildren();
  const tasks = (state && state.tasks) || [];
  for (const proj of (state && state.projects) || []) {
    const mine = tasks
      .filter((t) => t.projectId === proj.id)
      .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status)
        || (b.lastActivityAt || 0) - (a.lastActivityAt || 0));
    if (!mine.length) continue;
    const sec = el("section");
    const h = el("h2", "bix-label", proj.name);
    h.append(el("span", "count mono", String(mine.length)));
    const ul = el("ul", "tiles");
    mine.forEach((t) => ul.append(tile(t)));
    sec.append(h, ul);
    root.append(sec);
  }
  document.getElementById("empty").hidden = !state || tasks.length > 0;
}

// Latte when the system asks for light; bix.css switches on [data-theme].
const light = window.matchMedia("(prefers-color-scheme: light)");
function applyTheme() {
  if (light.matches) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
}
light.addEventListener("change", applyTheme);
applyTheme();

const events = new EventSource("/api/events");
events.onopen = () => { connected = true; renderNotices(); };
events.onmessage = (e) => {
  try {
    state = JSON.parse(e.data);
  } catch (err) {
    console.error("jobs: bad event", err);
    return;
  }
  connected = true;
  render();
};
events.onerror = () => { connected = false; renderNotices(); };

setInterval(render, 30000);  // keep "N min ago" current
render();
