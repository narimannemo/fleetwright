"""A live view of the fleet, served from the standard library.

    superagentic dashboard --db work.db          # http://127.0.0.1:8787
    superagentic dashboard --out fleet.html      # a static snapshot

The thing a queue's status line cannot tell you is whether the fleet is
*healthy*. `12 left` is the same number whether four workers are moving through
it steadily or three have died and one is stuck on a poison unit. What
distinguishes those is the shape of the throughput, who is holding what and for
how long, and how the tail of the duration distribution compares to its middle.

So the layout is: the six numbers you would check first, then throughput over
time, then the live in-flight state — which is the part a request-tracing tool
has no equivalent of, because a fleet has work that is *neither* finished nor
waiting.

**No dependencies, here of all places.** A dashboard that needs a web framework
is one that does not get installed on the box where the fleet is actually
running. `http.server` from the standard library, one HTML file with its CSS
and JS inline, and SVG drawn by hand rather than a charting library.

**Read-only.** It opens the database, reads, and serves. Nothing here claims,
finishes or deletes, so pointing it at a live run cannot disturb the run.

**About the login.** It is a shared access token, not user accounts — there is
no user model in this library and inventing one for a dashboard would be
pretending to an identity system that does not exist. The token is compared in
constant time and never stored: it is given at startup and lives in the
process.

There is no TLS. On a network, an access token typed into a form travels in
clear text, so the server **refuses to bind to anything but loopback unless a
token is set**, and warns every time it binds off-loopback anyway. A login form
that makes an unencrypted service feel safe is worse than no login form.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import leases

# Status colours, and they are status colours: reserved, never reused as
# "series 4", and every one of them appears beside a written label so the
# meaning never rests on hue alone. Checked for separation under deuteranopia
# and protanopia, which is where a naive green/red pairing fails.
PALETTE = {
    "done":   "#2f855a",   # good
    "leased": "#2b6cb0",   # in flight
    "open":   "#94a3b8",   # not started
    "failed": "#c53030",   # critical
}

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
/* Neutrals are cooled a few degrees toward the accent rather than being taken
   off the shelf: this is a console for watching machines, and a dead-neutral
   grey reads as unconsidered. `--accent` is chrome only -- the identity mark,
   focus rings, the header rule. It is deliberately NOT one of the four state
   colours, so "brand" can never be mistaken for "status". */
:root {
  --ground:#f5f6f9; --surface:#fff; --raise:#fafbfd;
  --ink:#131a24; --ink2:#4a5566; --ink3:#8b95a6;
  --line:#e3e7ee; --line2:#d3d9e4;
  --accent:#55618c;
  --done:#27754b; --leased:#2a5fa8; --open:#98a2b3; --failed:#b3282f;
  --failed-bg:#fdf2f2; --warn-bg:#fdf8ee; --warn:#8a6416;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground:#0b0e14; --surface:#141922; --raise:#1a2029;
    --ink:#e4e9f1; --ink2:#9aa5b5; --ink3:#69748a;
    --line:#242c39; --line2:#313b4b;
    --accent:#8f9cd0;
    --done:#4ec97a; --leased:#6ba9ff; --open:#69748a; --failed:#ff7b72;
    --failed-bg:#2a1618; --warn-bg:#2a2318; --warn:#d7b45e;
  }
}
:root[data-theme="dark"] {
  --ground:#0b0e14; --surface:#141922; --raise:#1a2029;
  --ink:#e4e9f1; --ink2:#9aa5b5; --ink3:#69748a;
  --line:#242c39; --line2:#313b4b;
  --accent:#8f9cd0;
  --done:#4ec97a; --leased:#6ba9ff; --open:#69748a; --failed:#ff7b72;
  --failed-bg:#2a1618; --warn-bg:#2a2318; --warn:#d7b45e;
}
:root[data-theme="light"] {
  --ground:#f5f6f9; --surface:#fff; --raise:#fafbfd;
  --ink:#131a24; --ink2:#4a5566; --ink3:#8b95a6;
  --line:#e3e7ee; --line2:#d3d9e4;
  --accent:#55618c;
  --done:#27754b; --leased:#2a5fa8; --open:#98a2b3; --failed:#b3282f;
  --failed-bg:#fdf2f2; --warn-bg:#fdf8ee; --warn:#8a6416;
}
* { box-sizing:border-box; }
.shell { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
aside { background:var(--surface); border-right:1px solid var(--line);
        display:flex; flex-direction:column; gap:18px; padding:18px 0 14px;
        position:sticky; top:0; height:100vh; overflow-y:auto; }
.brand { display:flex; align-items:center; gap:9px; padding:0 18px;
         font-weight:660; font-size:14px; letter-spacing:-.01em; }
.brand .mark { width:3px; height:15px; border-radius:2px; background:var(--accent); }
.navgroup { display:flex; flex-direction:column; gap:3px; }
.navgroup.grow { flex:1; min-height:0; }
.navlabel { font-size:10px; text-transform:uppercase; letter-spacing:.08em;
            color:var(--ink3); font-weight:660; padding:0 18px 5px; }
.navitem { display:flex; align-items:center; gap:8px; padding:6px 18px;
           cursor:pointer; font-size:13px; color:var(--ink2); border:0;
           background:none; width:100%; text-align:left; font-family:inherit;
           border-left:2px solid transparent; }
.navitem:hover { background:var(--raise); color:var(--ink); }
.navitem.on { color:var(--ink); font-weight:620; border-left-color:var(--accent);
              background:var(--raise); }
.navitem .meta { margin-left:auto; font-size:11px; color:var(--ink3);
                 font-variant-numeric:tabular-nums; }
.navitem .lbl { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.who { margin-top:auto; padding:12px 18px 0; border-top:1px solid var(--line);
       display:flex; align-items:center; gap:10px; }
.body { min-width:0; }
@media (max-width:820px) {
  .shell { grid-template-columns:1fr; }
  aside { position:static; height:auto; }
  .navgroup.grow { flex:none; }
}
#gate { position:fixed; inset:0; display:grid; place-items:center;
        background:var(--ground); z-index:20; padding:20px; }
.gatebox { background:var(--surface); border:1px solid var(--line);
           border-radius:12px; padding:28px; width:min(360px,100%);
           display:flex; flex-direction:column; gap:12px; }
.gatebox h1 { font-size:15px; }
.gatebox input { font:inherit; padding:9px 11px; border-radius:7px;
                 border:1px solid var(--line2); background:var(--ground);
                 color:var(--ink); }
.gatebox input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
.gatebox button { font:inherit; font-weight:620; padding:9px 11px; border:0;
                  border-radius:7px; background:var(--accent); color:#fff;
                  cursor:pointer; }
.gateerr { color:var(--failed); font-size:12px; margin:0; }
.tiny { font-size:11px; line-height:1.45; margin:2px 0 0; }
body { margin:0; background:var(--ground); color:var(--ink);
       font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
       -webkit-font-smoothing:antialiased; }
/* Machine strings -- unit ids, worker names, paths -- are set in mono
   throughout, and human chrome in sans. That split is the whole typographic
   system and it is what the subject is made of. */
.mono { font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
        font-size:12.5px; letter-spacing:-.01em; }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
         padding:18px 24px 14px; border-bottom:1px solid var(--line);
         background:var(--surface); }
h1 { font-size:14px; margin:0; font-weight:640; letter-spacing:-.005em;
     text-wrap:balance; display:flex; align-items:center; gap:9px; }
h1::before { content:""; width:3px; height:15px; border-radius:2px;
             background:var(--accent); }
.sub { color:var(--ink3); font-size:12px; }
.live { display:inline-flex; align-items:center; gap:7px; color:var(--ink3);
        font-size:12px; margin-left:auto; font-variant-numeric:tabular-nums; }
.dot { width:7px; height:7px; border-radius:50%; background:var(--open); }
main { padding:18px 24px 48px; display:grid; gap:14px; max-width:1200px; }
.tiles { display:grid; gap:12px;
         grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); }
.card { background:var(--surface); border:1px solid var(--line);
        border-radius:9px; padding:14px 16px; }
.card h2 { font-size:10.5px; text-transform:uppercase; letter-spacing:.075em;
           color:var(--ink3); margin:0 0 12px; font-weight:640; }
.tile { position:relative; overflow:hidden; }
.tile .k { font-size:10.5px; color:var(--ink3); text-transform:uppercase;
           letter-spacing:.075em; font-weight:640; }
.tile .v { font-size:27px; font-weight:640; letter-spacing:-.025em;
           font-variant-numeric:tabular-nums; line-height:1.15; margin-top:4px; }
.tile .sub2 { font-size:11.5px; color:var(--ink3); margin-top:3px; }
.wide { grid-column:1/-1; }
.row { display:grid; gap:14px; grid-template-columns:1fr 1fr; }
@media (max-width:880px){ .row { grid-template-columns:1fr; } }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th { text-align:left; font-size:10.5px; text-transform:uppercase;
     letter-spacing:.06em; color:var(--ink3); font-weight:640;
     padding:0 10px 9px 0; }
td { padding:7px 10px 7px 0; border-top:1px solid var(--line); font-size:13px;
     vertical-align:middle; }
tr.attn td { background:var(--warn-bg); }
tr.bad td { background:var(--failed-bg); }
/* A severity stripe, so a row that needs a human is distinguishable before
   any number is read -- and distinguishable in greyscale, which colour alone
   is not. */
td.stripe { width:3px; padding:0; border-top:0; }
tr.bad td.stripe { background:var(--failed); }
tr.attn td.stripe { background:var(--warn); }
td.num, th.num { text-align:right; }
.scroll { overflow-x:auto; }
.muted { color:var(--ink3); }
.pill { display:inline-block; font-size:10.5px; font-weight:640; padding:1px 6px;
        border-radius:4px; letter-spacing:.02em; vertical-align:1px; }
.pill.slow { background:var(--warn); color:var(--surface); }
.pill.retry { background:var(--failed); color:var(--surface); }
.chip { display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
        color:var(--ink2); }
.sw { width:9px; height:9px; border-radius:2px; display:inline-block; }
.legend { display:flex; gap:15px; flex-wrap:wrap; margin-top:12px;
          padding-top:11px; border-top:1px solid var(--line); }
.empty { color:var(--ink3); font-size:13px; padding:12px 0; }
tr.run { cursor:pointer; }
tr.run:hover td { background:var(--raise); }
tr.run.sel td { background:var(--raise); font-weight:600; }
.mini { display:inline-flex; align-items:flex-end; gap:1px; height:16px; }
.mini i { width:3px; background:var(--done); border-radius:1px; display:block; }
.scopebar { display:flex; align-items:center; gap:10px; background:var(--surface);
            border:1px solid var(--line); border-left:3px solid var(--accent);
            border-radius:9px; padding:10px 14px; font-size:13px; }
.scopebar b { font-weight:640; }
button.link { background:none; border:0; color:var(--accent); cursor:pointer;
              font:inherit; padding:0; text-decoration:underline; }
.live-dot { width:6px; height:6px; border-radius:50%; background:var(--leased);
            display:inline-block; }
.bar { height:15px; border-radius:3px; overflow:hidden; display:flex;
       background:var(--line2); }
.bar span { height:100%; }
.tip { position:fixed; pointer-events:none; background:var(--surface);
       color:var(--ink); border:1px solid var(--line2); border-radius:6px;
       padding:7px 9px; font-size:12px; box-shadow:0 8px 24px rgba(0,0,0,.16);
       opacity:0; transition:opacity .12s; z-index:9; }
a:focus-visible, [tabindex]:focus-visible, rect:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px; }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
</style>

<div id="gate" hidden>
  <form class="gatebox" id="loginform">
    <h1>superagentic</h1>
    <p class="muted">This dashboard is protected by an access token.</p>
    <input type="password" id="token" placeholder="access token" autocomplete="off"
           autofocus>
    <button type="submit">Sign in</button>
    <p class="gateerr" id="gateerr" hidden>That token was not accepted.</p>
    <p class="muted tiny">Served over plain HTTP — this token is not encrypted
      in transit. Use it on a trusted network only.</p>
  </form>
</div>

<div class="shell" id="shell" hidden>
  <aside>
    <div class="brand"><span class="mark"></span>superagentic</div>

    <div class="navgroup">
      <div class="navlabel">Projects</div>
      <div id="projects"></div>
    </div>

    <div class="navgroup grow">
      <div class="navlabel">Runs <span id="runcount" class="muted"></span></div>
      <div id="sideruns"></div>
    </div>

    <div class="who">
      <span class="live"><span class="dot" id="dot"></span><span id="ago">—</span></span>
      <button class="link" id="logout" hidden>Sign out</button>
    </div>
  </aside>

  <div class="body">
<header>
  <h1 id="pagetitle">Overview</h1>
  <span class="sub mono" id="db"></span>
  <span class="live"><span class="dot" id="dot2"></span><span id="ago2">—</span></span>
</header>
<main>
  <div id="scope"></div>
  <div class="tiles" id="tiles"></div>
  <div class="card wide">
    <h2>Units finished over time</h2>
    <div id="chart"></div>
  </div>
  <div class="row">
    <div class="card"><h2>Progress by kind</h2><div id="kinds"></div></div>
    <div class="card"><h2>In flight right now</h2><div class="scroll" id="inflight"></div></div>
  </div>
  <div class="row">
    <div class="card"><h2>Workers</h2><div class="scroll" id="workers"></div></div>
    <div class="card"><h2>Could not finish</h2><div class="scroll" id="failures"></div></div>
  </div>
  <div class="card wide" id="runscard">
    <h2>All runs</h2>
    <div class="scroll" id="runs"></div>
  </div>
</main>
  </div>
</div>
<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;          // null when served live
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function dur(s) {
  if (s == null) return "—";
  if (s < 90) return s.toFixed(s < 10 ? 1 : 0) + "s";
  if (s < 5400) return (s / 60).toFixed(s < 600 ? 1 : 0) + "m";
  return (s / 3600).toFixed(1) + "h";
}

function tile(k, v, sub, colour) {
  return `<div class="card tile"><div class="k">${esc(k)}</div>
    <div class="v" ${colour ? `style="color:${colour}"` : ""}>${esc(v)}</div>
    ${sub ? `<div class="sub2">${esc(sub)}</div>` : ""}</div>`;
}

function ago(s) {
  const d = Date.now() / 1000 - s;
  if (d < 90) return Math.round(d) + "s ago";
  if (d < 5400) return Math.round(d / 60) + "m ago";
  if (d < 86400) return (d / 3600).toFixed(1) + "h ago";
  return Math.round(d / 86400) + "d ago";
}

const params = new URLSearchParams(location.search);
let SELECTED = params.get("run");
let PROJECT = params.get("project");

function syncUrl() {
  const u = new URL(location.href);
  SELECTED ? u.searchParams.set("run", SELECTED) : u.searchParams.delete("run");
  PROJECT ? u.searchParams.set("project", PROJECT) : u.searchParams.delete("project");
  history.replaceState(null, "", u);
}

function select(run) {
  SELECTED = run;
  syncUrl();
  if (!DATA) poll();
}

function selectProject(name) {
  if (name === PROJECT) return;
  PROJECT = name;
  // A run id belongs to one database. Carrying it across would scope the new
  // project to a run it has never heard of and show an empty page.
  SELECTED = null;
  syncUrl();
  if (!DATA) poll();
}

function renderSidebar(d) {
  const ps = d.projects || [];
  $("#projects").innerHTML = ps.length ? ps.map(n =>
    `<button class="navitem ${n === d.project ? "on" : ""}" data-p="${esc(n)}">
       <span class="lbl">${esc(n)}</span></button>`).join("")
    : `<div class="navlabel">none</div>`;
  $("#projects").querySelectorAll("[data-p]").forEach(b =>
    b.addEventListener("click", () => selectProject(b.dataset.p)));

  const rs = d.runs || [];
  $("#runcount").textContent = rs.length ? `(${rs.length})` : "";
  $("#sideruns").innerHTML =
    `<button class="navitem ${!SELECTED ? "on" : ""}" data-r="">
       <span class="lbl">All runs</span>
       <span class="meta">${(d.totals.all || 0).toLocaleString()}</span></button>`
    + rs.map(r => `<button class="navitem ${r.run_id === SELECTED ? "on" : ""}"
        data-r="${esc(r.run_id)}" title="${esc(r.label || r.run_id)}">
        ${r.running ? '<span class="live-dot"></span>' : ""}
        <span class="lbl">${esc(r.label || r.run_id)}</span>
        <span class="meta">${r.done}/${r.units}</span></button>`).join("");
  $("#sideruns").querySelectorAll("[data-r]").forEach(b =>
    b.addEventListener("click", () => select(b.dataset.r || null)));

  $("#db").textContent = d.project || "";
  const sel = rs.find(r => r.run_id === SELECTED);
  $("#pagetitle").textContent = sel ? (sel.label || sel.run_id) : "Overview";
  const lo = $("#logout");
  lo.hidden = !d.auth;
}

function renderRuns(d) {
  const rs = d.runs || [];
  if (!rs.length) {
    $("#runs").innerHTML = `<div class="empty">No runs yet. Start one with
      <span class="mono">superagentic start --label "…"</span> and enqueue with
      <span class="mono">--run</span>.</div>`;
    $("#scope").innerHTML = "";
    return;
  }
  $("#runs").innerHTML = `<table><tr>
      <th>run</th><th>label</th><th class="num">units</th><th class="num">done</th>
      <th class="num">failed</th><th class="num">left</th>
      <th class="num">workers</th><th class="num">elapsed</th>
      <th class="num">parallel</th><th>started</th></tr>
    ${rs.map(r => `<tr class="run ${r.run_id === SELECTED ? "sel" : ""}"
        data-run="${esc(r.run_id)}">
      <td class="mono">${r.running ? '<span class="live-dot"></span> ' : ""}${esc(r.run_id)}</td>
      <td>${esc(r.label || "")}</td>
      <td class="num">${r.units.toLocaleString()}</td>
      <td class="num">${r.done.toLocaleString()}</td>
      <td class="num" ${r.failed ? 'style="color:var(--failed)"' : ""}>${r.failed}</td>
      <td class="num">${r.left.toLocaleString()}</td>
      <td class="num">${r.workers}</td>
      <td class="num muted">${dur(r.elapsed)}</td>
      <td class="num muted" title="worker-seconds ÷ wall-clock">${
        r.elapsed > 0 && r.busy ? (r.busy / r.elapsed).toFixed(1) + "x" : "—"}</td>
      <td class="muted">${r.started_at ? ago(r.started_at) : ""}</td>
    </tr>`).join("")}</table>`;
  $("#runs").querySelectorAll("tr.run").forEach(tr =>
    tr.addEventListener("click", () =>
      select(tr.dataset.run === SELECTED ? null : tr.dataset.run)));

  const sel = rs.find(r => r.run_id === SELECTED);
  $("#scope").innerHTML = sel
    ? `<div class="scopebar"><span>Showing <b>${esc(sel.label || sel.run_id)}</b>
         <span class="muted mono">${esc(sel.run_id)}</span>
         ${sel.started_by ? `<span class="muted">· started by ${esc(sel.started_by)}</span>` : ""}
         </span>
       <button class="link" id="clear" style="margin-left:auto">show everything</button></div>`
    : (rs.length ? `<div class="scopebar"><span class="muted">Showing
         <b>every run</b> — click one above to scope to it.</span></div>` : "");
  const c = $("#clear");
  if (c) c.addEventListener("click", () => select(null));
}

function render(d) {
  $("#gate").hidden = true;
  $("#shell").hidden = false;
  renderSidebar(d);
  renderRuns(d);
  const t = d.totals, pct = t.all ? Math.round(100 * t.done / t.all) : 0;
  $("#tiles").innerHTML = [
    tile("Left", t.left.toLocaleString(), `${t.all.toLocaleString()} total`),
    tile("Done", t.done.toLocaleString(), pct + "% complete", "var(--done)"),
    tile("In flight", t.leased.toLocaleString(),
         d.workers.length + " worker" + (d.workers.length === 1 ? "" : "s"),
         t.leased ? "var(--leased)" : null),
    tile("Failed", t.failed.toLocaleString(),
         d.retried ? d.retried + " retried" : "none retried",
         t.failed ? "var(--failed)" : null),
    tile("Throughput", d.units_per_min ? d.units_per_min + "/min" : "—", "recent rate"),
    tile("ETA", d.eta_seconds == null ? (t.left ? "—" : "done") : dur(d.eta_seconds),
         d.duration.p50 == null ? "no timings yet"
           : `p50 ${dur(d.duration.p50)} · p95 ${dur(d.duration.p95)}`),
  ].join("");

  // -- throughput. One series, so no legend: the heading names it.
  const s = d.throughput, W = 900, H = 150, P = {l: 34, r: 8, t: 10, b: 20};
  if (!s.length || !s.some(b => b.n)) {
    $("#chart").innerHTML = `<div class="empty">Nothing has finished yet.</div>`;
  } else {
    const max = Math.max(...s.map(b => b.n)), n = s.length;
    const bw = (W - P.l - P.r) / n;
    const y = v => P.t + (H - P.t - P.b) * (1 - v / max);
    const ticks = [0, Math.ceil(max / 2), max].filter((v, i, a) => a.indexOf(v) === i);
    // 2px gap between bars so adjacent marks never fuse into one shape, and
    // a 4px rounded top anchored to the baseline.
    // The last non-empty bucket is where the fleet is NOW; everything else is
    // history. Emphasising it costs nothing and is the first thing anyone
    // looks for on a live chart.
    const last = s.reduce((a, b, i) => b.n ? i : a, -1);
    const bars = s.map((b, i) => b.n ? `<rect x="${(P.l + i * bw + 1).toFixed(1)}"
        y="${y(b.n).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}"
        height="${(H - P.b - y(b.n)).toFixed(1)}" rx="3"
        fill="${i === last ? "var(--leased)" : "var(--done)"}"
        data-i="${i}" tabindex="0"></rect>` : "").join("");
    $("#chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%"
        height="${H}" id="svg" role="img"
        aria-label="units finished per time bucket">
      ${ticks.map(v => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}"
          stroke="var(--line)" stroke-width="1"></line>
        <text x="${P.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10"
          fill="var(--ink3)">${v}</text>`).join("")}
      ${bars}
      <text x="${P.l}" y="${H - 5}" font-size="10" fill="var(--ink3)">
        ${new Date(s[0].t * 1000).toLocaleTimeString()}</text>
      <text x="${W - P.r}" y="${H - 5}" font-size="10" fill="var(--ink3)"
        text-anchor="end">${new Date(s[n - 1].t * 1000).toLocaleTimeString()}</text>
    </svg>`;
    const tip = $("#tip");
    $("#svg").querySelectorAll("rect").forEach(r => {
      r.addEventListener("mousemove", e => {
        const b = s[+r.dataset.i];
        tip.innerHTML = `<b>${b.n}</b> finished<br><span class="muted">${
          new Date(b.t * 1000).toLocaleTimeString()}</span>`;
        tip.style.left = (e.clientX + 12) + "px";
        tip.style.top = (e.clientY - 10) + "px";
        tip.style.opacity = 1;
      });
      r.addEventListener("mouseleave", () => tip.style.opacity = 0);
    });
  }

  // -- per kind
  const order = ["done", "leased", "open", "failed"];
  $("#kinds").innerHTML = Object.entries(d.by_kind).map(([k, c]) => {
    const tot = order.reduce((a, s) => a + c[s], 0) || 1;
    return `<div style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;font-size:13px;
                  margin-bottom:5px"><span class="mono">${esc(k)}</span>
        <span class="muted">${c.done}/${tot}</span></div>
      <div class="bar">${order.map(s => c[s]
        ? `<span style="width:${100 * c[s] / tot}%;background:var(--${s});
             ${s !== "done" ? "margin-left:2px" : ""}"
             title="${c[s]} ${s}"></span>` : "").join("")}</div></div>`;
  }).join("") || `<div class="empty">Nothing queued.</div>`;
  $("#kinds").insertAdjacentHTML("beforeend", `<div class="legend">${
    order.map(s => `<span class="chip"><span class="sw"
      style="background:var(--${s})"></span>${s}</span>`).join("")}</div>`);

  // -- in flight. The state a request tracer has no equivalent of.
  // "Is anyone stuck?" is the question this panel exists to answer, and a raw
  // duration column does not answer it. A unit held more than 3x the p95 is
  // outside what this fleet normally takes, so it gets a stripe and a pill.
  const slowAt = d.duration.p95 ? d.duration.p95 * 3 : Infinity;
  $("#inflight").innerHTML = d.workers.length ? `<table><tr>
      <th style="width:3px"></th><th>worker</th><th>unit</th>
      <th class="num">held</th><th class="num">lease</th></tr>
    ${d.workers.map(w => {
      const slow = w.seconds_held != null && w.seconds_held > slowAt;
      return `<tr class="${slow ? "attn" : ""}"><td class="stripe"></td>
      <td class="mono">${esc(w.worker)}</td>
      <td class="mono">${esc(w.name)}${w.attempts > 1
        ? ` <span class="pill retry">retry ${w.attempts}</span>` : ""}</td>
      <td class="num">${dur(w.seconds_held)}${
        slow ? ` <span class="pill slow">slow</span>` : ""}</td>
      <td class="num muted">${dur(w.seconds_left)}</td></tr>`;
    }).join("")}</table>`
    : `<div class="empty">No unit is being worked on.</div>`;

  // -- per worker
  const mx = Math.max(1, ...d.per_worker.map(w => w.done));
  $("#workers").innerHTML = d.per_worker.length ? `<table><tr>
      <th>worker</th><th class="num">done</th><th class="num">failed</th>
      <th class="num">mean</th><th></th></tr>
    ${d.per_worker.map(w => `<tr><td class="mono">${esc(w.worker)}</td>
      <td class="num">${w.done}</td>
      <td class="num" ${w.failed ? 'style="color:var(--failed)"' : ""}>${w.failed}</td>
      <td class="num muted">${dur(w.done ? w.seconds / w.done : null)}</td>
      <td style="width:34%"><div class="bar"><span
        style="width:${100 * w.done / mx}%;background:var(--done)"></span></div></td>
      </tr>`).join("")}</table>`
    : `<div class="empty">No worker has finished anything yet.</div>`;

  $("#failures").innerHTML = d.failures.length ? `<table><tr>
      <th style="width:3px"></th><th>unit</th><th class="num">tries</th>
      <th>why</th></tr>
    ${d.failures.map(f => `<tr class="bad"><td class="stripe"></td>
      <td class="mono">${esc(f.name)}</td>
      <td class="num">${f.attempts}</td>
      <td>${esc(f.note || "no reason recorded")}</td></tr>`).join("")}</table>`
    : `<div class="empty">Nothing has been given up on.</div>`;

  const when = new Date(d.now * 1000).toLocaleTimeString();
  const colour = t.leased ? "var(--leased)" : "var(--open)";
  for (const [a, b] of [["#ago", "#dot"], ["#ago2", "#dot2"]]) {
    $(a).textContent = when;
    $(b).style.background = colour;
  }
}

function showGate() {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
}

async function poll() {
  try {
    const q = new URLSearchParams();
    if (SELECTED) q.set("run", SELECTED);
    if (PROJECT) q.set("project", PROJECT);
    const r = await fetch("api" + (q.toString() ? "?" + q : ""));
    if (r.status === 401) { showGate(); return; }
    render(await r.json());
    $("#dot").style.opacity = 1;
  } catch (e) { $("#dot").style.opacity = .25; }
}

const form = $("#loginform");
if (form) form.addEventListener("submit", async e => {
  e.preventDefault();
  const r = await fetch("login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token: $("#token").value})});
  if (r.ok) { $("#token").value = ""; $("#gateerr").hidden = true; poll(); }
  else { $("#gateerr").hidden = false; $("#token").select(); }
});

const out = $("#logout");
if (out) out.addEventListener("click", async () => {
  await fetch("logout", {method: "POST"});
  showGate();
});

if (DATA) {
  // A snapshot: no server to poll, no session to end.
  $("#shell").hidden = false;
  document.querySelectorAll(".live").forEach(e => e.style.display = "none");
  render(DATA);
} else { poll(); setInterval(poll, 2000); }
</script>
"""


def page(db: Path, data: dict | None = None) -> str:
    return (PAGE.replace("__TITLE__", f"superagentic · {db.name}")
                .replace("__DATA__", json.dumps(data) if data else "null"))


def _payload(conn, run: str | None, *, projects: list[str] | None = None,
             project: str | None = None, auth: bool = False) -> dict:
    """One response with every half.

    The runs list and the selected run's statistics travel together because
    they are read together, and two round trips against a file a fleet is
    writing to can disagree about how much is done.

    `projects`/`project`/`auth` are here rather than only in the request
    handler so that a STATIC snapshot carries them too — otherwise the file
    renders with an empty sidebar, which is how this was found.
    """
    return {**leases.stats(conn, run=run),
            "runs": leases.runs(conn, limit=25),
            "selected": run,
            "run_meta": leases.run(conn, run) if run else None,
            "projects": projects if projects is not None else [],
            "project": project,
            "auth": auth,
            "authed": True}


class _Handler(BaseHTTPRequestHandler):
    projects: dict[str, Path]
    token: str | None
    sessions: set

    # -- plumbing ----------------------------------------------------------

    def _send(self, body: bytes, kind: str, *, status: int = 200,
              cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # A dashboard that serves a cached snapshot is worse than none: it is
        # confidently wrong about a number someone is about to act on.
        self.send_header("Cache-Control", "no-store")
        # Cheap and worth having on a page that renders names from a database.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", status=status)

    def _session(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        return SimpleCookie(raw).get("sa_session", None) and \
            SimpleCookie(raw)["sa_session"].value

    def _authed(self) -> bool:
        if not self.token:
            return True
        sid = self._session()
        return bool(sid and sid in self.sessions)

    def _project(self, query: dict) -> tuple[str, Path] | tuple[None, None]:
        names = list(self.projects)
        want = (query.get("project") or [None])[0] or (names[0] if names else None)
        if want not in self.projects:
            return None, None
        return want, self.projects[want]

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        u = urllib.parse.urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", urllib.parse.parse_qs(u.query)

        if path == "/":
            # The page always renders; it asks for the token itself when the
            # API says it needs one. Serving a separate login page would mean
            # two templates that drift.
            self._send(page(next(iter(self.projects.values()), Path("work.db"))
                            ).encode(), "text/html; charset=utf-8")
            return

        if path == "/api":
            if not self._authed():
                self._json({"auth_required": True}, status=401)
                return
            name, db = self._project(q)
            if db is None:
                self._json({"error": "no such project"}, status=404)
                return
            run = (q.get("run") or [None])[0]
            # A fresh connection per request. sqlite3 objects are not safe to
            # share across threads, and this is a ThreadingHTTPServer.
            conn = leases.connect(db)
            try:
                self._json(_payload(conn, run, projects=list(self.projects),
                                    project=name, auth=bool(self.token)))
            finally:
                conn.close()
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/logout":
            sid = self._session()
            self.sessions.discard(sid)
            self._send(b'{"ok":true}', "application/json",
                       cookie="sa_session=; Path=/; Max-Age=0; HttpOnly; "
                              "SameSite=Strict")
            return

        if path == "/login":
            if not self.token:
                self._json({"ok": True})
                return
            n = int(self.headers.get("Content-Length") or 0)
            # Bounded read: an unbounded one lets any client make the server
            # allocate whatever it likes.
            body = self.rfile.read(min(n, 4096)) if n else b""
            try:
                given = json.loads(body or b"{}").get("token", "")
            except json.JSONDecodeError:
                given = ""
            # compare_digest, so a wrong token does not leak its correct
            # prefix through how long the comparison took.
            if not hmac.compare_digest(str(given), self.token):
                # Slows a brute-force attempt without being a real rate limit;
                # the actual defence is that the token should be long.
                time.sleep(0.5)
                self._json({"ok": False, "error": "wrong token"}, status=401)
                return
            sid = secrets.token_urlsafe(32)
            self.sessions.add(sid)
            self._send(b'{"ok":true}', "application/json",
                       cookie=f"sa_session={sid}; Path=/; HttpOnly; "
                              "SameSite=Strict; Max-Age=86400")
            return

        self.send_error(404)

    def log_message(self, *_a) -> None:
        """Silent. A poll every two seconds would bury anything worth reading."""


def _projects(db: Path | list[Path]) -> dict[str, Path]:
    """Name -> database. A project IS a database; there is nothing else to it.

    Inventing a project table inside one of the databases would make one file
    the registry for the others, and then moving or deleting that file breaks
    the rest. The filename is already the name people use.
    """
    paths = [db] if isinstance(db, Path | str) else list(db)
    out: dict[str, Path] = {}
    for raw in paths:
        q = Path(raw)
        found = sorted(q.glob("*.db")) if q.is_dir() else [q]
        for f in found:
            name = f.stem
            # Two projects with the same stem in different directories would
            # otherwise silently shadow each other.
            if name in out and out[name] != f:
                name = str(f)
            out[name] = f
    return out


def snapshot(db: Path, run: str | None = None) -> str:
    """A single self-contained HTML file, with the data baked in.

    For attaching to a run, mailing to someone, or looking at a fleet that has
    already finished. No server, no refresh, nothing fetched.
    """
    conn = leases.connect(db)
    try:
        return page(db, _payload(conn, run, projects=[db.stem], project=db.stem))
    finally:
        conn.close()


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def serve(db: Path | list[Path], *, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True, token: str | None = None) -> None:
    """Serve one or more project databases.

    Refuses to bind off-loopback without a token. That refusal is the whole
    security design: the dashboard exposes unit names, notes and machine names,
    it has no TLS, and the easiest mistake in the world is to pass
    `--host 0.0.0.0` once and forget. Making that combination impossible is
    worth more than any amount of documentation saying not to.
    """
    projects = _projects(db)
    if host not in LOOPBACK and not token:
        raise SystemExit(
            f"refusing to bind {host} without --token.\n"
            "  This serves queue contents and machine names over plain HTTP.\n"
            "  Either keep it on 127.0.0.1, or set a token:\n"
            "      superagentic dashboard --host 0.0.0.0 --token \"$(openssl rand -hex 24)\"")
    handler = type("Handler", (_Handler,), {
        "projects": projects, "token": token, "sessions": set()})
    with ThreadingHTTPServer((host, port), handler) as httpd:
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        url = f"http://{shown}:{port}"
        print(f"superagentic dashboard  {url}   (ctrl-c to stop)")
        print(f"  projects: {', '.join(projects)}")
        if token:
            print("  access token required")
        if host not in LOOPBACK:
            # Said every time, not once in a README. There is no TLS here.
            print(f"  WARNING: bound to {host} over plain HTTP — the token and "
                  "everything it protects travel unencrypted.")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
