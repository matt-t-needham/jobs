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

// ---------------------------------------------------------------- Open view
// The whole conversation with one session, through the broker (workbench plan §1).
// On open it loads the transcript from disk, so a terminal session shows its history,
// then follows live events by number. New rows are appended, never rebuilt, so the page
// doesn't flicker and the scroll position holds. The composer is built once per session
// and kept across polls, so typing survives.

function openId() {
  const m = /^#open=([A-Za-z0-9_-]{1,64})$/.exec(location.hash);
  return m ? m[1] : null;
}

// The session being viewed. `rendered` counts the items already in the log;
// `live` is the streaming reply's row, which is kept at the end and updated in place.
let openSession = null;

async function api(action, body) {
  const r = await fetch("/api/workbench/" + action, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  let data = {};
  try { data = await r.json(); } catch (err) { data = {}; }
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || ("request failed: HTTP " + r.status));
  }
  return data;
}

function historyItem(h) {
  if (h.kind === "user") return { role: "user", text: h.text };
  if (h.kind === "text") return { role: "assistant", text: h.text };
  if (h.kind === "tool") return { role: "tool", text: "used " + h.name };
  if (h.kind === "tool_result") return { role: "tool", text: "a tool failed" };
  return null;
}

function flushPending(o) {
  if (o.pending) {
    o.items.push({ role: "assistant", text: o.pending });
    o.pending = "";
  }
}

function applyEvent(o, e) {
  if (typeof e.seq === "number") o.after = e.seq;
  switch (e.kind) {
    case "user":
      flushPending(o);
      o.items.push({ role: "user", text: e.text });
      break;
    case "delta":
      o.pending += e.text;
      break;
    case "text":
      o.pending = "";
      o.items.push({ role: "assistant", text: e.text });
      break;
    case "tool":
      o.lastTool = e.name;
      o.items.push({ role: "tool", text: "used " + e.name });
      break;
    case "tool_result":
      if (e.error) o.items.push({ role: "tool", text: "a tool failed" });
      break;
    case "turn_end":
      o.pending = "";
      o.lastTool = null;
      if (!e.ok) o.items.push({ role: "note", text: "That turn ended with an error." });
      break;
    case "driver":
      o.items.push({ role: "note", text: "The session's process has ended. Send a message to carry on from here." });
      break;
    default:
      break; // approvals are shown from the poll's own list
  }
}

function logItem(item) {
  const d = el("div", "msg " + item.role);
  d.append(el("span", "who", item.role === "user" ? "You" : item.role === "assistant" ? "Claude" : item.role));
  d.append(el("p", "turn-text", item.text));
  return d;
}

function resetLog(o) {
  const log = document.getElementById("open-log");
  log.replaceChildren();
  o.rendered = 0;
  o.live = null;
  o.liveText = null;
}

function paintApprovals(o) {
  const sig = JSON.stringify(o.approvals);
  if (sig === o.approvalSig) return;
  o.approvalSig = sig;
  const box = document.getElementById("open-approvals");
  box.replaceChildren();
  for (const ap of o.approvals) {
    const card = el("div", "approval");
    card.append(el("p", "v-label", "Waiting for your decision"), el("p", "turn-text summary", ap.summary));
    const row = el("div", "composer-row");
    const yes = el("button", "btn", "Allow");
    yes.type = "button";
    yes.addEventListener("click", () => decide(ap.id, true));
    const no = el("button", "btn quiet", "Deny");
    no.type = "button";
    no.addEventListener("click", () => decide(ap.id, false));
    row.append(yes, no);
    card.append(row);
    box.append(card);
  }
}

function paintOpen(o) {
  if (openSession !== o) return;  // another session has been opened since
  const log = document.getElementById("open-log");
  if (!log) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;

  const where = { web: "Driven from the web", terminal: "Running in a terminal", none: "Not running" }[o.driver] || "";
  const doing = o.busy ? (o.lastTool ? "running " + o.lastTool : "answering…") : null;
  document.getElementById("open-status").textContent = [where, doing].filter(Boolean).join(" · ");

  const err = document.getElementById("open-error");
  err.textContent = o.error || "";
  err.hidden = !o.error;

  // A terminal holds the session: the composer waits behind the one handover card.
  const inTerminal = o.driver === "terminal";
  document.getElementById("open-terminal").hidden = !inTerminal;
  document.getElementById("open-text").disabled = inTerminal;
  document.getElementById("open-send").disabled = inTerminal;

  while (o.rendered < o.items.length) {
    log.insertBefore(logItem(o.items[o.rendered]), o.live || null);
    o.rendered += 1;
  }
  if (o.pending) {
    if (!o.live) {
      o.live = logItem({ role: "assistant", text: "" });
      o.live.classList.add("live");
      o.liveText = o.live.querySelector(".turn-text");
      log.append(o.live);
    }
    o.liveText.textContent = o.pending;
  } else if (o.live) {
    o.live.remove();
    o.live = null;
    o.liveText = null;
  }
  if (nearBottom) log.scrollTop = log.scrollHeight;

  paintApprovals(o);
}

async function loadHistory(o) {
  try {
    const r = await api("history", { sessionId: o.id });
    o.items = [];
    if (r.truncated) o.items.push({ role: "note", text: "Only the end of a long conversation is shown." });
    for (const h of r.items) {
      const item = historyItem(h);
      if (item) o.items.push(item);
    }
    o.after = r.cursor;
    o.pending = "";
    o.lastTool = null;
    o.loaded = true;
    o.error = null;
  } catch (err) {
    o.error = err.message;
  }
  if (openSession === o) {
    resetLog(o);
    paintOpen(o);
    if (o.loaded) pollOpen(o);  // don't wait for the next tick to learn who's driving
  }
}

async function pollOpen(o) {
  if (!o.loaded || o.inFlight) return;
  o.inFlight = true;
  try {
    const r = await api("events", { sessionId: o.id, after: o.after });
    if (r.reset) {
      // The broker restarted since we last looked: our cursor means nothing now. Reload.
      o.loaded = false;
      o.inFlight = false;
      loadHistory(o);
      return;
    }
    if (r.gap) o.items.push({ role: "note", text: "Some earlier messages were too old to fetch." });
    for (const e of r.events) applyEvent(o, e);
    o.driver = r.driver;
    o.busy = r.busy;
    o.approvals = r.approvals || [];
    o.error = null;
  } catch (err) {
    o.error = err.message;
  } finally {
    o.inFlight = false;
    paintOpen(o);
  }
}

// One tick: load the history if that hasn't worked yet, otherwise follow live events.
function tickOpen(o) {
  if (openSession !== o) return;
  if (!o.loaded) loadHistory(o);
  else pollOpen(o);
}

async function decide(approvalId, allow) {
  const o = openSession;
  try {
    await api("approve", { sessionId: o.id, approvalId, allow });
  } catch (err) {
    o.error = err.message;
  }
  pollOpen(o);
}

function mountOpen(id) {
  if (openSession && openSession.timer) clearInterval(openSession.timer);
  const o = {
    id, loaded: false, after: 0, items: [], rendered: 0, pending: "", live: null, liveText: null,
    approvals: [], approvalSig: "", driver: "", busy: false, lastTool: null, error: null,
    inFlight: false, timer: null,
  };
  openSession = o;
  const view = document.getElementById("view");
  view.replaceChildren();
  const back = el("a", "back", "Back to the task");
  back.href = "#task=" + encodeURIComponent(id);
  const head = el("header", "v-head");
  const status = el("p", "meta");
  status.id = "open-status";
  head.append(el("h2", "v-title", "Conversation"), status);
  const err = el("p", "notice");
  err.id = "open-error";
  err.setAttribute("role", "alert");
  err.hidden = true;

  const term = el("section", "approval");
  term.id = "open-terminal";
  term.hidden = true;
  const cmd = el("p", "mono", "claude --resume " + id);
  const takeBtn = el("button", "btn", "Continue here");
  takeBtn.type = "button";
  term.append(
    el("p", "v-label", "Running in a terminal"),
    el("p", "turn-text", "Messages from here can't reach that terminal. Continue here stops its Claude process; the conversation is kept. To go back, run this from the project folder in the terminal:"),
    cmd,
    el("div", "composer-row"),
  );
  term.lastChild.append(takeBtn);
  takeBtn.addEventListener("click", async () => {
    if (!window.confirm("Stop the terminal's Claude process and continue here? The conversation is kept.")) return;
    try {
      const r = await api("take_over", { sessionId: id });
      if (!r.released) o.error = "Nothing was holding the session any more.";
    } catch (e) {
      o.error = e.message;
    }
    pollOpen(o);
  });

  const log = el("div", "log");
  log.id = "open-log";
  log.setAttribute("aria-live", "polite");
  const approvals = el("div");
  approvals.id = "open-approvals";

  const form = el("form", "composer");
  const text = el("textarea");
  text.id = "open-text";
  text.rows = 3;
  text.placeholder = "Message";
  text.setAttribute("aria-label", "Message");
  const row = el("div", "composer-row");
  const send = el("button", "btn", "Send");
  send.type = "submit";
  send.id = "open-send";
  const stop = el("button", "btn quiet", "Stop");
  stop.type = "button";
  stop.id = "open-stop";
  row.append(send, stop);
  form.append(text, row);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = text.value.trim();
    if (!msg) return;
    send.disabled = true;
    try {
      const clientId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
      // The broker picks the model (the top one available); the page doesn't choose.
      await api("send", { sessionId: id, text: msg, clientId });
      text.value = "";
      o.busy = true;
      o.error = null;
    } catch (e2) {
      o.error = e2.message;
    } finally {
      send.disabled = false;
      pollOpen(o);
    }
  });
  stop.addEventListener("click", async () => {
    try { await api("stop", { sessionId: id }); } catch (e3) { o.error = e3.message; }
    pollOpen(o);
  });

  view.append(back, head, err, term, log, approvals, form);
  // The page shows "loading" until the transcript arrives; the tick keeps retrying if it fails.
  o.timer = setInterval(() => tickOpen(o), 1500);
  loadHistory(o);
}

function renderView() {
  const view = document.getElementById("view");
  const oid = openId();
  if (oid) {
    document.body.dataset.view = "task";
    if (!openSession || openSession.id !== oid || document.getElementById("open-log") === null) {
      mountOpen(oid);
    }
    return;
  }
  if (openSession && openSession.timer) {
    clearInterval(openSession.timer);
    openSession = null;
  }
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
  const open = el("a", "back open-link", "Open the conversation");
  open.href = "#open=" + encodeURIComponent(t.id);
  view.append(back, open, viewport(t));
}

// ---------------------------------------------------------------- Projects
// Each project's todos (its markdown file, four states: 0 unapproved, 1 approved,
// 2 in progress, 3 done) and its pull requests, both from the broker (DESIGN.md §12).
// The broker writes the todo files; this page only asks it to move one item at a time.

const TODO_STATES = ["Unapproved", "Approved", "In progress", "Done"];
const PROJECTS_EVERY = 20000;   // ms between refreshes while the page is open on projects
let projectsData = null;        // { projects: [{ name, items, error }] }
let prsData = null;             // { repos: [{ project, repo, prs, error }] }
let projectsErr = null;
let projectsFetchedAt = 0;

function inProjects() {
  return /^#projects$|^#project=/.test(location.hash);
}

function selectedProject() {
  const m = /^#project=([A-Za-z0-9_.-]{1,64})$/.exec(location.hash);
  return m ? m[1] : null;
}

async function refreshProjects() {
  projectsFetchedAt = Date.now();
  try {
    projectsData = await api("todos", {});
    prsData = await api("prs", {});
    projectsErr = null;
  } catch (err) {
    projectsErr = err.message;
  }
  render();
}

async function todoSet(project, todoId, expected, to, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return;
  try {
    await api("todo_set", { project, todoId, expected, to });
    projectsErr = null;
  } catch (err) {
    projectsErr = err.message;  // e.g. "that todo is approved, not unapproved; reload"
  }
  refreshProjects();
}

function projectCounts(p) {
  const counts = [0, 0, 0, 0];
  for (const it of p.items) counts[it.state] += 1;
  return counts;
}

function renderProjectList() {
  const root = document.getElementById("projects");
  root.replaceChildren();
  const projects = (projectsData && projectsData.projects) || [];
  const h = el("h2", "bix-label", "Projects");
  h.append(el("span", "count mono", String(projects.length)));
  root.append(h);
  if (projectsErr) root.append(el("p", "notice", projectsErr));
  const ul = el("ul", "rows");
  for (const p of projects) {
    const c = projectCounts(p);
    const li = el("li");
    const a = el("a", "row");
    a.href = "#project=" + encodeURIComponent(p.name);
    if (selectedProject() === p.name) a.setAttribute("aria-current", "true");
    a.append(el("span", "head"));
    a.firstChild.append(el("span", "name", p.name));
    a.append(joined([
      el("span", null, c[0] + " unapproved"),
      el("span", null, c[1] + " approved"),
      el("span", null, c[2] + " in progress"),
    ]));
    li.append(a);
    ul.append(li);
  }
  root.append(ul);
  document.getElementById("empty").hidden = true;
}

function prState(pr) {
  if (pr.state === "MERGED") return "merged";
  if (pr.state === "CLOSED") return "closed";
  return pr.draft ? "draft" : "open";
}

function prNodes(repo) {
  if (!repo) return [muted("Loading…")];
  if (repo.error) return [el("p", "notice", "Pull requests: " + repo.error)];
  if (!repo.prs.length) return [muted("No open pull requests, and none merged in the last 14 days.")];
  return repo.prs.map((pr) => {
    const d = el("div", "turn");
    const title = "#" + pr.number + " " + pr.title;
    if (pr.url) {
      const a = el("a", "pr-link", title);
      a.href = pr.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      d.append(a);
    } else {
      d.append(el("span", null, title));
    }
    d.append(joined([el("span", null, prState(pr)), el("span", "mono", pr.branch), ago(pr.updatedAt)]));
    return d;
  });
}

function todoRow(project, it) {
  const li = el("li", "step " + (it.state === 3 ? "done" : "todo"));
  li.append(el("span", "step-mark"), el("span", "step-text", it.text));
  li.append(el("span", "step-state", TODO_STATES[it.state]));
  if (it.state === 0) {
    const b = el("button", "btn quiet", "Approve");
    b.type = "button";
    b.addEventListener("click", () => todoSet(project, it.id, 0, 1));
    li.append(b);
  } else if (it.state === 2) {
    const b = el("button", "btn quiet", "Release");
    b.type = "button";
    b.addEventListener("click", () => todoSet(project, it.id, 2, 1,
      "Release this todo back to approved? The agent working on it will stop being the owner."));
    li.append(b);
  }
  return li;
}

function renderProjectView(name) {
  const view = document.getElementById("view");
  view.replaceChildren();
  const back = el("a", "back", "All projects");
  back.href = "#projects";
  view.append(back);
  const p = projectsData && projectsData.projects.find((x) => x.name === name);
  if (!p) {
    view.append(el("p", "v-muted", projectsData ? "No todo file for this project." : "Loading…"));
    return;
  }
  const repoEntry = prsData && prsData.repos.find((r) => r.project === name);
  const head = el("header", "v-head");
  head.append(el("h2", "v-title", name));
  head.append(joined([repoEntry ? el("span", "mono", repoEntry.repo) : null]));
  view.append(head);

  if (p.error) view.append(el("p", "notice", p.error));
  for (let s = 0; s < 4; s += 1) {
    const items = p.items.filter((it) => it.state === s);
    const list = el("ul", "steps");
    for (const it of items) list.append(todoRow(name, it));
    const nodes = items.length ? [list] : [muted("None.")];
    view.append(block(TODO_STATES[s] + " · " + items.length, nodes));
  }
  view.append(block("Pull requests", prNodes(repoEntry)));
}

function renderProjectsView() {
  const view = document.getElementById("view");
  const name = selectedProject();
  if (openSession && openSession.timer) {
    clearInterval(openSession.timer);
    openSession = null;
  }
  document.body.dataset.view = name ? "task" : "list";
  if (!name) {
    view.replaceChildren(el("p", "v-muted", "Pick a project to see its todos and pull requests."));
    return;
  }
  renderProjectView(name);
}

function render() {
  renderSite();
  renderNotices();
  if (inProjects()) {
    if (Date.now() - projectsFetchedAt > PROJECTS_EVERY) refreshProjects();
    renderProjectList();
    renderProjectsView();
    return;
  }
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
