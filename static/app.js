"use strict";
// jobs UI (the workbench, DESIGN.md §8 and workbench plan §1): follows
// /api/events (SSE) and draws every task as a row in a list grouped by project.
// The selected task (#task=<id>) fills the viewport: goal, now, what it is
// blocked on, and the last few turns. Every string from the server goes in
// through textContent: names, details, needs and turns are written by a model
// and may carry hostile text.

const LABELS = {
  "needs-you": "needs you", stalled: "stalled", working: "working",
  idle: "idle", dead: "dead", done: "done",
};
const ORDER = Object.keys(LABELS);
const BLOCKED = {
  you: { label: "Blocked on you", mark: "needs-you" },
  limit: { label: "Blocked on a usage limit", mark: "stalled" },
  other: { label: "Blocked on something else", mark: "stalled" },
};

let state = null;
let connected = false;
// Context slices by task id: { key, status: "loading" | "ok" | "error", turns }.
// The key is the task's last activity, so a slice is refetched only when it changes.
const contexts = new Map();

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

function selectedId() {
  const m = /^#task=([A-Za-z0-9_-]{1,64})$/.exec(location.hash);
  return m ? m[1] : null;
}

function metaParts(t) {
  const where = [t.repo, t.branch].filter(Boolean).join(" · ");
  return [
    where ? el("span", "mono", where) : null,
    t.kind === "bg" ? el("span", null, "background") : null,
    ago(t.lastActivityAt),
    typeof t.costUsd === "number" ? el("span", "mono", "$" + t.costUsd.toFixed(2)) : null,
  ];
}

function row(t) {
  const li = el("li");
  const a = el("a", "row " + t.status);
  a.href = "#task=" + encodeURIComponent(t.id);
  if (selectedId() === t.id) a.setAttribute("aria-current", "true");
  const head = el("span", "head");
  head.append(mark(t.status), el("span", "name", t.name || "untitled"));
  a.append(head);
  if (t.status === "needs-you" && t.needs) {
    a.append(el("p", "needs", t.needs));
  } else if (t.detail) {
    const d = el("p", "detail", t.detail);
    d.title = t.detail;
    a.append(d);
  }
  a.append(joined(metaParts(t)));
  li.append(a);
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

function renderList() {
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
    const ul = el("ul", "rows");
    mine.forEach((t) => ul.append(row(t)));
    sec.append(h, ul);
    root.append(sec);
  }
  document.getElementById("empty").hidden = !state || tasks.length > 0;
}

function loadContext(t) {
  const key = String(t.lastActivityAt);
  const cached = contexts.get(t.id);
  if (cached && cached.key === key) return;
  contexts.set(t.id, { key, status: "loading", turns: [] });
  fetch("/api/tasks/" + encodeURIComponent(t.id) + "/context", { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error("context request failed: HTTP " + r.status);
      return r.json();
    })
    .then((body) => {
      contexts.set(t.id, { key, status: "ok", turns: Array.isArray(body.turns) ? body.turns : [] });
    })
    .catch((err) => {
      console.error("jobs: context", err);
      contexts.set(t.id, { key, status: "error", turns: [] });
    })
    .finally(() => {
      if (selectedId() === t.id) renderView();
    });
}

function block(label, nodes) {
  const s = el("section", "v-block");
  s.append(el("h3", "bix-label", label), ...nodes);
  return s;
}

// The plan from the session's status card. Each step has a shape and a word for
// its state, so it reads without colour.
function progressBlock(p) {
  const head = el("p", "v-status", p.done + " of " + p.total + " steps done");
  const list = el("ul", "steps");
  for (const s of p.steps) {
    const li = el("li", "step " + (s.done ? "done" : "todo"));
    li.append(el("span", "step-mark"), el("span", "step-text", s.text), el("span", "step-state", s.done ? "done" : "to do"));
    list.append(li);
  }
  return block("Progress", [head, list]);
}

function muted(text) {
  return el("p", "v-muted", text);
}

function turn(x) {
  const d = el("div", "turn");
  d.append(el("span", "who", x.role === "user" ? "You" : "Claude"));
  d.append(el("p", "turn-text", x.text));
  return d;
}

function contextNodes(t) {
  const c = contexts.get(t.id);
  if (!c || c.status === "loading") return [muted("Loading…")];
  if (c.status === "error") return [muted("Context is unavailable right now.")];
  if (!c.turns.length) return [muted("No messages in the transcript yet.")];
  return c.turns.map(turn);
}

function viewport(t) {
  const v = el("div", "viewport-body");
  const head = el("header", "v-head");
  head.append(el("h2", "v-title", t.name || "untitled"));
  head.append(joined(metaParts(t)));
  v.append(head);

  v.append(block("Goal", [t.goal ? el("p", "v-text", t.goal) : muted("Not known yet.")]));

  const now = el("p", "v-status");
  now.append(mark(t.status), el("span", null, LABELS[t.status] || t.status));
  const nowNodes = [now];
  if (t.now) nowNodes.push(el("p", "v-text", t.now));
  v.append(block("Now", nowNodes));

  if (t.progress) v.append(progressBlock(t.progress));

  if (t.blocked) {
    const b = BLOCKED[t.blocked.on] || { label: "Blocked", mark: "stalled" };
    const line = el("p", "v-status");
    line.append(mark(b.mark), el("span", null, b.label));
    const nodes = [line];
    if (t.blocked.why) nodes.push(el("p", "v-text", t.blocked.why));
    v.append(block("Blocked", nodes));
  }

  v.append(block("Context", contextNodes(t)));
  return v;
}

function renderView() {
  const view = document.getElementById("view");
  const id = selectedId();
  document.body.dataset.view = id ? "task" : "list";
  view.replaceChildren();
  if (!id) {
    view.append(el("p", "v-muted", "Pick a task to see what it is doing."));
    return;
  }
  const t = state && (state.tasks || []).find((x) => x.id === id);
  if (!t) {
    view.append(el("p", "v-muted", state ? "That task is no longer running." : "Connecting…"));
    return;
  }
  loadContext(t);
  const back = el("a", "back", "All tasks");
  back.href = "#";
  view.append(back, viewport(t));
}

function render() {
  renderSite();
  renderNotices();
  renderList();
  renderView();
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
window.addEventListener("hashchange", render);

setInterval(render, 30000);  // keep "N min ago" current
render();
