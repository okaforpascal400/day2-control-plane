// Vanilla ES2020 — no bundler, no framework, no build step. nginx proxies /api to
// the api service, so the dashboard has no host configuration of its own.
"use strict";

const API = "/api";
const REFRESH_MS = 2000;
const STATUSES = ["pending", "processing", "completed", "failed"];

const el = {
  status: document.getElementById("status"),
  stats: document.getElementById("stats"),
  jobs: document.getElementById("jobs"),
  items: document.getElementById("items"),
  form: document.getElementById("item-form"),
  name: document.getElementById("item-name"),
  description: document.getElementById("item-description"),
};

async function api(path, options) {
  const response = await fetch(API + path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${options?.method || "GET"} ${path} -> ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

function setStatus(message, isError) {
  el.status.textContent = message;
  el.status.classList.toggle("error", Boolean(isError));
}

function cell(row, text, className) {
  const td = row.insertCell();
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function formatTime(value) {
  return value ? new Date(value).toLocaleTimeString() : "—";
}

function duration(job) {
  if (!job.started_at || !job.finished_at) return "—";
  const ms = new Date(job.finished_at) - new Date(job.started_at);
  return `${ms} ms`;
}

function renderStats(stats) {
  el.stats.replaceChildren(
    ...STATUSES.map((status) => {
      const li = document.createElement("li");
      li.className = `stat status-${status}`;
      const count = document.createElement("strong");
      count.textContent = stats[status] ?? 0;
      const label = document.createElement("span");
      label.textContent = status;
      li.append(count, label);
      return li;
    }),
  );
}

function renderJobs(jobs) {
  el.jobs.replaceChildren();
  for (const job of jobs) {
    const row = el.jobs.insertRow();
    cell(row, job.id);
    cell(row, job.item_id ?? "—");
    cell(row, job.kind);
    cell(row, job.status, `status-${job.status}`);
    cell(row, job.attempts);
    cell(row, duration(job));
    if (job.last_error) row.title = job.last_error;
  }
}

function renderItems(items) {
  el.items.replaceChildren();
  for (const item of items) {
    const row = el.items.insertRow();
    cell(row, item.id);
    cell(row, item.name);
    cell(row, item.description ?? "—");
    cell(row, formatTime(item.created_at));

    const button = document.createElement("button");
    button.textContent = "Delete";
    button.className = "danger";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/items/${item.id}`, { method: "DELETE" });
        await refresh();
      } catch (error) {
        setStatus(error.message, true);
        button.disabled = false;
      }
    });
    row.insertCell().append(button);
  }
}

async function refresh() {
  const [items, jobs, stats] = await Promise.all([
    api("/items?limit=20"),
    api("/jobs?limit=20"),
    api("/jobs/stats"),
  ]);
  renderItems(items);
  renderJobs(jobs);
  renderStats(stats);
  setStatus(`Updated ${new Date().toLocaleTimeString()}`, false);
}

el.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = el.name.value.trim();
  if (!name) return;
  try {
    await api("/items", {
      method: "POST",
      body: JSON.stringify({
        name,
        description: el.description.value.trim() || null,
      }),
    });
    el.form.reset();
    el.name.focus();
    await refresh();
  } catch (error) {
    setStatus(error.message, true);
  }
});

async function tick() {
  try {
    await refresh();
  } catch (error) {
    setStatus(`API unreachable — ${error.message}`, true);
  }
}

tick();
setInterval(tick, REFRESH_MS);
