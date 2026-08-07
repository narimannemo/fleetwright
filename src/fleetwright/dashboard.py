"""A live view of the fleet, served from the standard library.

    fleetwright dashboard --db work.db          # http://127.0.0.1:8787
    fleetwright dashboard --out fleet.html      # a static snapshot

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

from . import __version__, leases

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

#: How long a login lasts, server-side. Matches the cookie's Max-Age, so the
#: two cannot disagree about whether someone is still signed in.
SESSION_SECONDS = 86400

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<!-- Inline, so the browser never requests /favicon.ico and logs a 404.
     A draining queue: three bars, each shorter than the last. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2032%2032%22%3E%3Crect%20width%3D%2232%22%20height%3D%2232%22%20rx%3D%227%22%20fill%3D%22%230b1a2b%22%2F%3E%3Crect%20x%3D%226%22%20y%3D%228%22%20width%3D%2220%22%20height%3D%224.5%22%20rx%3D%222.25%22%20fill%3D%22%23fbeecd%22%2F%3E%3Crect%20x%3D%226%22%20y%3D%2215%22%20width%3D%2213%22%20height%3D%224.5%22%20rx%3D%222.25%22%20fill%3D%22%23ef2b23%22%2F%3E%3Crect%20x%3D%226%22%20y%3D%2222%22%20width%3D%227%22%20height%3D%224.5%22%20rx%3D%222.25%22%20fill%3D%22%23fbeecd%22%20opacity%3D%22.55%22%2F%3E%3C%2Fsvg%3E">
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
  --cancelled:#c7ccd6;
  --failed-bg:#fdf2f2; --warn-bg:#fdf8ee; --warn:#8a6416;
  /* Brand, not state. Kept in its own three tokens so nothing here can drift
     into meaning "good" or "critical". */
  --wm-ink:#0b1a2b; --wm-red:#ef2b23; --wm-cream:#fbeecd;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground:#0b0e14; --surface:#141922; --raise:#1a2029;
    --ink:#e4e9f1; --ink2:#9aa5b5; --ink3:#69748a;
    --line:#242c39; --line2:#313b4b;
    --accent:#8f9cd0;
    --done:#4ec97a; --leased:#6ba9ff; --open:#69748a; --failed:#ff7b72;
  --cancelled:#3a4351;
    --failed-bg:#2a1618; --warn-bg:#2a2318; --warn:#d7b45e;
  --wm-ink:#0b1a2b; --wm-red:#ff3b32; --wm-cream:#f6e6c4;
  }
}
:root[data-theme="dark"] {
  --ground:#0b0e14; --surface:#141922; --raise:#1a2029;
  --ink:#e4e9f1; --ink2:#9aa5b5; --ink3:#69748a;
  --line:#242c39; --line2:#313b4b;
  --accent:#8f9cd0;
  --done:#4ec97a; --leased:#6ba9ff; --open:#69748a; --failed:#ff7b72;
  --cancelled:#3a4351;
  --failed-bg:#2a1618; --warn-bg:#2a2318; --warn:#d7b45e;
  --wm-ink:#0b1a2b; --wm-red:#ff3b32; --wm-cream:#f6e6c4;
}
:root[data-theme="light"] {
  --ground:#f5f6f9; --surface:#fff; --raise:#fafbfd;
  --ink:#131a24; --ink2:#4a5566; --ink3:#8b95a6;
  --line:#e3e7ee; --line2:#d3d9e4;
  --accent:#55618c;
  --done:#27754b; --leased:#2a5fa8; --open:#98a2b3; --failed:#b3282f;
  --cancelled:#c7ccd6;
  --cancelled:#c7ccd6;
  --failed-bg:#fdf2f2; --warn-bg:#fdf8ee; --warn:#8a6416;
  --wm-ink:#0b1a2b; --wm-red:#ef2b23; --wm-cream:#fbeecd;
}
* { box-sizing:border-box; }
/* The `hidden` attribute works only through the UA stylesheet's
   `[hidden] { display: none }`, so ANY author rule that sets `display` on the
   same element silently beats it. `#gate` and `.shell` both set display:grid,
   which made the login overlay render permanently on top of every dashboard —
   including ones with no token configured at all. Restore the semantics
   globally rather than remembering it at each call site. */
[hidden] { display: none !important; }
.shell { display:grid; grid-template-columns:196px 248px minmax(0,1fr);
         min-height:100vh; }
aside.second { background:var(--raise); }
aside { background:var(--surface); border-right:1px solid var(--line);
        display:flex; flex-direction:column; gap:18px; padding:18px 0 14px;
        position:sticky; top:0; height:100vh; overflow-y:auto; }
.brand { display:flex; align-items:center; gap:9px; padding:0 16px; }
/* The wordmark is set as TYPE, not embedded as an image. A raster logo would
   add weight to every page and to every static snapshot, and the snapshot is
   the thing people mail to each other. The cream keyline is the sticker
   outline from the artwork, done with paint-order so the stroke sits behind
   the fill instead of eating into the letterforms. */
.wordmark { font: italic 900 17px/1.05 "Helvetica Neue", Helvetica, Arial,
            ui-sans-serif, system-ui, sans-serif;
            letter-spacing:-.035em; white-space:nowrap; user-select:none;
            -webkit-text-stroke:3.5px var(--wm-cream); paint-order:stroke fill; }
.wordmark .wm-s { color:var(--wm-ink); }
.wordmark .wm-a { color:var(--wm-red); }
.wordmark.short { display:none; font-size:18px; -webkit-text-stroke-width:3px; }
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
       display:flex; flex-direction:column; align-items:stretch; gap:8px; }
.session { font-size:11.5px; color:var(--ink3); line-height:1.35; }
.signout { font:inherit; font-size:12px; font-weight:600; padding:6px 10px;
           border:1px solid var(--line2); border-radius:6px; background:none;
           color:var(--ink2); cursor:pointer; }
.signout:hover:not(:disabled) { background:var(--ground); color:var(--ink);
                                border-color:var(--ink3); }
.signout:disabled { opacity:.45; cursor:not-allowed; }
.railfoot { margin-top:auto; padding:12px 18px 0; border-top:1px solid var(--line); }
.version { font-size:10.5px; color:var(--ink3); font-variant-numeric:tabular-nums;
           letter-spacing:.02em; }
.brand { position:relative; }
.collapse { margin-left:auto; border:0; background:none; color:var(--ink3);
            cursor:pointer; font-size:17px; line-height:1; padding:2px 4px;
            border-radius:5px; }
.collapse:hover { background:var(--ground); color:var(--ink); }
/* Collapsed: the rail keeps its identity and its controls, and loses only the
   words. Hiding it entirely would leave no way back without knowing where to
   click. */
.shell.railshut { grid-template-columns:52px 248px minmax(0,1fr); }
.railshut .rail .wordmark:not(.short),
.railshut .rail .navlabel,
.railshut .rail .session,
.railshut .rail .version,
.railshut .rail .signout,
.railshut .rail .navitem .lbl { display:none; }
.railshut .rail { padding-left:0; padding-right:0; }
.railshut .rail .brand { justify-content:center; padding:0 4px; gap:4px; }
.railshut .rail .wordmark.short { display:inline; }
.railshut .rail .collapse { margin-left:0; transform:rotate(180deg); }
.railshut .rail .navitem { justify-content:center; padding:8px 0; }
.railshut .rail .navitem::before { content:attr(data-initial); font-weight:660;
                                   font-size:12px; }
.railshut .rail .who { padding:12px 6px 0; align-items:center; }
.pager { display:flex; align-items:center; gap:8px; margin-top:14px;
         padding-top:12px; border-top:1px solid var(--line); flex-wrap:wrap; }
.pager button { font:inherit; font-size:12px; padding:5px 10px; cursor:pointer;
                border:1px solid var(--line2); border-radius:6px;
                background:none; color:var(--ink2); }
.pager button:hover:not(:disabled) { background:var(--raise); color:var(--ink); }
.pager button:disabled { opacity:.4; cursor:not-allowed; }
.pager .where { margin-left:auto; font-size:12px; color:var(--ink3);
                font-variant-numeric:tabular-nums; }
.filters { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
           margin:-4px 0 14px; }
.filters input { font:inherit; font-size:13px; padding:6px 10px; border-radius:7px;
                 border:1px solid var(--line2); background:var(--ground);
                 color:var(--ink); min-width:230px; }
.filters input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
.segs { display:inline-flex; border:1px solid var(--line2); border-radius:7px;
        overflow:hidden; }
.segs button { font:inherit; font-size:12px; padding:5px 10px; border:0;
               background:none; color:var(--ink2); cursor:pointer; }
.segs button + button { border-left:1px solid var(--line2); }
.segs button.on { background:var(--accent); color:#fff; font-weight:600; }
.pill.st { border:1px solid transparent; }
.pill.done   { background:var(--done);   color:#fff; }
.pill.failed { background:var(--failed); color:#fff; }
.pill.leased { background:var(--leased); color:#fff; }
.pill.open   { background:var(--line2);  color:var(--ink2); }
.pill.cancelled { background:transparent; color:var(--ink3);
                  border-color:var(--line2); }
.body { min-width:0; }
@media (max-width:1080px) { .shell { grid-template-columns:180px 210px minmax(0,1fr); } }
@media (max-width:860px) {
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
        font-size:11.5px; margin-left:auto; font-variant-numeric:tabular-nums;
        white-space:nowrap; }
.railfoot .live { margin-left:0; }
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
.lane { display:grid; grid-template-columns:150px 1fr 78px; align-items:center;
        gap:10px; margin-bottom:3px; }
.lane .who { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
             font-size:11.5px; color:var(--ink2); overflow:hidden;
             text-overflow:ellipsis; white-space:nowrap; }
.lane .track { position:relative; height:15px; background:var(--line);
               border-radius:3px; overflow:hidden; }
.lane .track i { position:absolute; top:0; bottom:0; border-radius:2px;
                 min-width:2px; }
.lane .pct { font-size:11px; color:var(--ink3); text-align:right;
             font-variant-numeric:tabular-nums; }
/* The pipeline diagram. SVG inherits none of the page's colours, so every
   fill and stroke is named here: an unstyled <text> or <path> is BLACK, which
   on a dark ground is not a faint bug but an invisible one. */
#dag { padding:2px 0 12px }
#dag svg { display:block; max-width:100%; height:auto }
#dag .nodebox { fill:var(--raise); stroke:var(--line2); stroke-width:1 }
#dag .nodebox:hover { stroke:var(--accent); stroke-width:1.5 }
#dag .nlabel { fill:var(--ink);
  font:600 12.5px ui-monospace,SFMono-Regular,Menlo,monospace }
#dag .nsub { fill:var(--ink3); font:11px system-ui,-apple-system,sans-serif }
#dag .edge { fill:none; stroke:var(--line2); stroke-linecap:round; opacity:.9 }
#dag .edge:hover { stroke:var(--accent); opacity:1 }
#dag .arrow { fill:var(--line2); stroke:none }
/* An invisible fat copy of each edge, so a 1px line is still hoverable. */
#dag .hit { fill:none; stroke:transparent; cursor:default }
#dag .elabel { fill:var(--ink3); text-anchor:middle;
  font:10.5px system-ui,-apple-system,sans-serif }
.flowrow { display:grid; grid-template-columns:auto 1fr auto; align-items:center;
           gap:12px; margin-bottom:8px; font-size:13px; }
.flowbar { height:12px; border-radius:3px; background:var(--done); }
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
    <h1>fleetwright</h1>
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
  <aside class="rail" id="rail">
    <div class="brand">
      <span class="wordmark" aria-label="fleetwright">
        <span class="wm-s">fleet</span><span class="wm-a">wright</span></span>
      <span class="wordmark short" aria-hidden="true">
        <span class="wm-s">f</span><span class="wm-a">w</span></span>
      <button class="collapse" id="collapse" title="Collapse sidebar"
              aria-label="Collapse sidebar">&#8249;</button>
    </div>

    <div class="navgroup">
      <div class="navlabel">Projects</div>
      <div id="projects"></div>
    </div>

    <div class="who">
      <div class="session" id="session"></div>
      <button class="signout" id="logout">Sign out</button>
      <div class="version" id="version"></div>
    </div>
  </aside>

  <aside class="second">
    <div class="navgroup">
      <div class="navlabel">Views</div>
      <button class="navitem" id="nav-overview"><span class="lbl">Overview</span></button>
      <button class="navitem" id="nav-jobs"><span class="lbl">Jobs</span>
        <span class="meta" id="jobcount"></span></button>
    </div>

    <div class="navgroup grow">
      <div class="navlabel">Runs <span id="runcount" class="muted"></span></div>
      <div id="sideruns"></div>
    </div>

    <div class="railfoot">
      <span class="live" id="freshness" title="Time since this page last heard from the server">
        <span class="dot" id="dot"></span><span id="ago">not yet updated</span></span>
    </div>
  </aside>

  <div class="body">
<header>
  <h1 id="pagetitle">Overview</h1>
  <span class="sub mono" id="db"></span>
  <span class="live"><span class="dot" id="dot2"></span><span id="ago2">not yet updated</span></span>
</header>
<main id="view-jobs" hidden>
  <div class="card wide">
    <h2>Jobs</h2>
    <div class="filters">
      <input id="jobq" type="search" placeholder="filter by name, worker or note">
      <span class="segs" id="jobstatus"></span>
      <span class="muted" id="jobmeta"></span>
    </div>
    <div class="scroll" id="jobs"></div>
    <div class="pager" id="pager"></div>
  </div>
</main>
<main id="view-overview">
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
  <div class="card wide" id="tlcard">
    <h2>Who held what, when</h2>
    <div class="scroll" id="timeline"></div>
  </div>
  <div class="card wide" id="flowcard" hidden>
    <h2>What caused what</h2>
    <div class="scroll"><div id="dag"></div></div>
    <div class="scroll" id="flow"></div>
  </div>
  <div class="card wide">
    <h2>Skills in use</h2>
    <div class="scroll" id="skills"></div>
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

// -- The pipeline as nodes and edges. LAYERED, not force-directed: a pipeline
// has a direction, and a force layout throws it away -- the same data lands
// somewhere different on every load, and "which way does the work flow" stops
// being answerable at a glance. Node height is unit count, edge width is how
// many units one kind caused in another, so the shape carries the numbers
// rather than decorating them. One node per KIND: 255 unit nodes is a
// hairball and 400,000 is a dead tab. The exact counts are in the list
// underneath, which is also the reading that does not depend on colour.
function drawDag(g) {
  const el = $("#dag");
  if (!g.nodes || !g.nodes.length) { el.innerHTML = ""; return; }
  const cols = {};
  g.nodes.forEach(n => (cols[n.depth] = cols[n.depth] || []).push(n));
  const depths = Object.keys(cols).map(Number).sort((a, b) => a - b);

  const NW = 152, GAP = 18, COLGAP = 94, PAD = 8;
  const maxUnits = Math.max(...g.nodes.map(n => n.units), 1);
  const h = n => Math.round(58 + 52 * Math.sqrt(n.units / maxUnits));

  const place = {}; let tallest = 0;
  const colHeight = dp => cols[dp].reduce((a, n) => a + h(n), 0)
                        + GAP * (cols[dp].length - 1);
  depths.forEach(dp => { tallest = Math.max(tallest, colHeight(dp)); });
  depths.forEach((dp, ci) => {
    let y = PAD + (tallest - colHeight(dp)) / 2;
    cols[dp].forEach(n => {
      place[n.kind] = {x: PAD + ci * (NW + COLGAP), y, w: NW, h: h(n)};
      y += h(n) + GAP;
    });
  });
  const W = PAD * 2 + depths.length * NW + (depths.length - 1) * COLGAP;
  const H = PAD * 2 + tallest;

  const maxEdge = Math.max(...g.edges.map(e => e.units), 1);
  const edges = g.edges.map(e => {
    const a = place[e.from], b = place[e.to];
    if (!a || !b) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const c = Math.max(30, (x2 - x1) / 2);
    const wid = 1.2 + 9 * Math.sqrt(e.units / maxEdge);
    // The head is a plain polygon, not a <marker>. Markers scale with
    // stroke-width by default, so a fat edge grew a head the size of a node,
    // and `markerUnits` did not survive being parsed out of innerHTML. Every
    // curve is built to arrive horizontally, including a backward one, so the
    // heading is known and does not need computing.
    return `<path class="edge" stroke-width="${wid.toFixed(1)}"
      d="M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2 - 9},${y2}"
      ></path>
      <path class="arrow" d="M${x2 - 10},${y2 - 4.5} L${x2 - 1},${y2} L${
      x2 - 10},${y2 + 4.5} z"></path>
      <path class="hit" stroke-width="${Math.max(wid, 12).toFixed(1)}"
      d="M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2 - 9},${y2}"
      ><title>${esc(e.from)} caused ${
      e.units.toLocaleString()} ${esc(e.to)} unit(s)${
      e.failed ? ", " + e.failed + " failed" : ""}</title></path>
      <text class="elabel" x="${(x1 + x2) / 2}" y="${
      (y1 + y2) / 2 - wid / 2 - 6}">${e.units.toLocaleString()}</text>`;
  }).join("");

  const STATUS = ["done", "leased", "open", "failed", "cancelled"];
  const nodes = g.nodes.map(n => {
    const p = place[n.kind];
    let sx = p.x;
    const strip = STATUS.map(k => {
      const v = n[k] || 0;
      if (!v) return "";
      const w = p.w * v / Math.max(n.units, 1);
      const r = `<rect x="${sx.toFixed(2)}" y="${p.y + p.h - 5}"
        width="${w.toFixed(2)}" height="5" fill="var(--${k})"></rect>`;
      sx += w;
      return r;
    }).join("");
    return `<g><rect class="nodebox" x="${p.x}" y="${p.y}" width="${p.w}"
        height="${p.h}" rx="6"><title>${esc(n.kind)}: ${
        n.units.toLocaleString()} unit(s), ${n.done} done${
        n.failed ? ", " + n.failed + " failed" : ""}${
        n.cost ? ", $" + n.cost.toFixed(3) : ""}</title></rect>
      <text class="nlabel" x="${p.x + 12}" y="${p.y + 23}">${esc(n.kind)}</text>
      <text class="nsub" x="${p.x + 12}" y="${p.y + 40}">${
        n.units.toLocaleString()} unit(s)</text>
      <text class="nsub" x="${p.x + 12}" y="${p.y + 55}">${
        n.mean_seconds ? dur(n.mean_seconds) + " each" : ""}</text>
      ${strip}</g>`;
  }).join("");

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
    role="img" aria-label="the pipeline: one node per kind of work">
    ${edges}${nodes}</svg>`;
}

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
let VIEW = params.get("view") === "jobs" ? "jobs" : "overview";
let JOBSTATUS = params.get("status") || "";
let JOBQ = "";
let PAGE_N = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
const PER = 100;

function syncUrl() {
  const u = new URL(location.href);
  const set = (k, v) => v ? u.searchParams.set(k, v) : u.searchParams.delete(k);
  set("run", SELECTED);
  set("project", PROJECT);
  set("view", VIEW === "jobs" ? "jobs" : "");
  set("status", JOBSTATUS);
  set("page", VIEW === "jobs" && PAGE_N > 1 ? String(PAGE_N) : "");
  history.replaceState(null, "", u);
}

function setView(v) {
  VIEW = v;
  syncUrl();
  $("#view-jobs").hidden = v !== "jobs";
  $("#view-overview").hidden = v === "jobs";
  $("#nav-jobs").classList.toggle("on", v === "jobs");
  $("#nav-overview").classList.toggle("on", v !== "jobs");
  if (v === "jobs") loadJobs();
}

const STATUSES = ["", "leased", "open", "done", "failed"];

async function loadJobs() {
  if (DATA) { $("#jobs").innerHTML =
    `<div class="empty">A static snapshot has no live job list.</div>`; return; }
  const q = new URLSearchParams();
  if (SELECTED) q.set("run", SELECTED);
  if (PROJECT) q.set("project", PROJECT);
  if (JOBSTATUS) q.set("status", JOBSTATUS);
  if (JOBQ) q.set("q", JOBQ);
  q.set("limit", String(PER));
  q.set("offset", String((PAGE_N - 1) * PER));
  let d;
  try { d = await (await fetch("api/units?" + q)).json(); }
  catch (e) { return; }
  if (d.auth_required) { showGate(); return; }

  $("#jobstatus").innerHTML = STATUSES.map(st =>
    `<button data-st="${st}" class="${st === JOBSTATUS ? "on" : ""}">${
      st || "all"}</button>`).join("");
  $("#jobstatus").querySelectorAll("[data-st]").forEach(b =>
    b.addEventListener("click", () => {
      JOBSTATUS = b.dataset.st;
      // Back to page 1: staying on page 7 of a filter that now has two pages
      // shows an empty table and looks broken.
      PAGE_N = 1;
      syncUrl();
      loadJobs();
    }));

  $("#jobmeta").textContent =
    `${d.total.toLocaleString()} job${d.total === 1 ? "" : "s"}`;

  const from = d.total ? d.offset + 1 : 0, to = d.offset + d.shown;
  $("#pager").innerHTML = d.pages > 1 ? `
    <button data-go="1"    ${d.page === 1 ? "disabled" : ""}>&laquo; first</button>
    <button data-go="${d.page - 1}" ${d.page === 1 ? "disabled" : ""}>&lsaquo; prev</button>
    <button data-go="${d.page + 1}" ${d.page >= d.pages ? "disabled" : ""}>next &rsaquo;</button>
    <button data-go="${d.pages}"    ${d.page >= d.pages ? "disabled" : ""}>last &raquo;</button>
    <span class="where">${from.toLocaleString()}–${to.toLocaleString()} of
      ${d.total.toLocaleString()} · page ${d.page} of ${d.pages}</span>` : "";
  $("#pager").querySelectorAll("[data-go]").forEach(b =>
    b.addEventListener("click", () => {
      PAGE_N = Math.min(Math.max(1, +b.dataset.go), d.pages);
      syncUrl();
      loadJobs();
      $("#jobs").scrollIntoView({block: "nearest"});
    }));

  // A page beyond the end after a filter change: land the user somewhere real
  // rather than on an empty table.
  if (!d.units.length && d.total && d.page > d.pages) {
    PAGE_N = d.pages; syncUrl(); loadJobs(); return;
  }

  $("#jobs").innerHTML = d.units.length ? `<table><tr>
      <th style="width:3px"></th><th>job</th><th>kind</th><th>status</th>
      <th>worker</th><th>model</th><th class="num">tries</th>
      <th class="num">took</th><th>note / result</th></tr>
    ${d.units.map(u => {
      const bad = u.status === "failed";
      return `<tr class="${bad ? "bad" : ""}"><td class="stripe"></td>
      <td class="mono">${esc(u.name)}</td>
      <td class="muted">${esc(u.kind)}</td>
      <td><span class="pill st ${u.status}">${u.status}</span></td>
      <td class="mono muted">${esc(u.worker || "—")}</td>
      <td class="mono muted">${esc(u.model || "—")}</td>
      <td class="num ${u.attempts > 1 ? "" : "muted"}">${u.attempts}</td>
      <td class="num muted">${u.seconds == null ? "—" : dur(u.seconds)}${
        u.lease_left != null ? ` <span class="muted">/ ${dur(u.lease_left)} left</span>` : ""}</td>
      <td class="muted">${esc(u.note || (u.result == null ? ""
        : JSON.stringify(u.result)))}</td></tr>`;
    }).join("")}</table>`
    : `<div class="empty">No jobs match.</div>`;
}

function select(run) {
  SELECTED = run;
  syncUrl();
  if (!DATA) { poll(); if (VIEW === "jobs") loadJobs(); }
}

function selectProject(name) {
  if (name === PROJECT) return;
  PROJECT = name;
  // A run id belongs to one database. Carrying it across would scope the new
  // project to a run it has never heard of and show an empty page.
  SELECTED = null;
  syncUrl();
  if (!DATA) { poll(); if (VIEW === "jobs") loadJobs(); }
}

function renderSidebar(d) {
  const ps = d.projects || [];
  $("#projects").innerHTML = ps.length ? ps.map(n =>
    `<button class="navitem ${n === d.project ? "on" : ""}" data-p="${esc(n)}"
       data-initial="${esc(n.slice(0, 2).toUpperCase())}" title="${esc(n)}">
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
  $("#version").textContent = d.version ? "v" + d.version : "";
  $("#jobcount").textContent = (d.totals.all || 0).toLocaleString();
  const sel = rs.find(r => r.run_id === SELECTED);
  $("#pagetitle").textContent = VIEW === "jobs" ? "Jobs"
    : (sel ? (sel.label || sel.run_id) : "Overview");

  // The session row is always present, and always honest. Hiding the button
  // when no token is configured makes it look like a missing feature; showing
  // a live one that ends nothing is worse.
  const lo = $("#logout");
  if (d.auth) {
    $("#session").textContent = "Signed in";
    lo.disabled = false;
    lo.title = "End this session";
  } else {
    $("#session").textContent = "No access token set — this dashboard is open "
      + "on loopback.";
    lo.disabled = true;
    lo.title = "Nothing to sign out of: no token was configured at startup.";
  }
}

function renderRuns(d) {
  const rs = d.runs || [];
  if (!rs.length) {
    $("#runs").innerHTML = `<div class="empty">No runs yet. Start one with
      <span class="mono">fleetwright start --label "…"</span> and enqueue with
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
  if (VIEW === "jobs") { $("#view-jobs").hidden = false; $("#view-overview").hidden = true; }
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
    tile("Cost", d.cost && d.cost.priced
           ? (d.cost.total < 10 ? "$" + d.cost.total.toFixed(3)
                                : "$" + d.cost.total.toFixed(2))
           : "—",
         d.cost && d.cost.priced
           ? (d.cost.priced < d.cost.units
               ? `${d.cost.priced} of ${d.cost.units} reported`
               : `${((d.cost.tokens_in + d.cost.tokens_out) / 1000).toFixed(0)}k tokens`)
           : "nothing reported"),
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
  const order = ["done", "leased", "open", "failed", "cancelled"];
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

  // -- who held what, when. The question a fleet actually raises is not what
  // caused what, but whether it was saturated and what held up the end. A
  // time axis shows both; a node-link diagram shows neither.
  const tl = d.timeline || {bars: [], lanes: []};
  if (!tl.bars.length) {
    $("#timeline").innerHTML =
      `<div class="empty">Nothing has been claimed yet.</div>`;
  } else {
    const t0 = tl.from, wall = tl.wall || 1;
    const byLane = {};
    tl.bars.forEach(b => (byLane[b.worker] = byLane[b.worker] || []).push(b));
    $("#timeline").innerHTML = tl.lanes.map(l => {
      const bars = (byLane[l.worker] || []).map(b => {
        const left = 100 * (b.start - t0) / wall;
        const w = Math.max(0.15, 100 * (b.end - b.start) / wall);
        return `<i style="left:${left.toFixed(3)}%;width:${w.toFixed(3)}%;
          background:var(--${b.status})" title="${esc(b.name)} · ${b.status} · ${
          dur(b.end - b.start)}"></i>`;
      }).join("");
      const busy = 100 * (1 - l.idle);
      return `<div class="lane"><span class="who" title="${esc(l.worker)}${
        l.spawned_by ? " (spawned by " + esc(l.spawned_by) + ")" : ""}">${
        esc(l.worker)}</span>
        <span class="track">${bars}</span>
        <span class="pct" title="${l.units} units, ${dur(l.busy)} busy">${
          busy.toFixed(0)}% busy</span></div>`;
    }).join("")
    + `<div class="legend"><span class="chip">${tl.lanes.length} worker(s) over ${
        dur(wall)}</span>${tl.truncated
        ? '<span class="chip">showing the first 4,000 units</span>' : ""}</div>`;
  }

  // -- lineage, aggregated to kinds. A forest of 400,000 individual chains is
  // not a picture; three stages with counts on the edges is. Hidden entirely
  // when nothing chains, rather than showing an empty box.
  const fl = d.flow || [];
  $("#flowcard").hidden = !fl.length;
  if (fl.length) drawDag(d.graph || {nodes: [], edges: [], depths: 0});
  if (fl.length) {
    const mx = Math.max(...fl.map(f => f.units));
    $("#flow").innerHTML = fl.map(f => `<div class="flowrow">
      <span class="mono">${esc(f.from)} &rarr; ${esc(f.to)}</span>
      <span><span class="flowbar" style="width:${100 * f.units / mx}%;display:block"></span></span>
      <span class="muted">${f.units.toLocaleString()} unit(s)${
        f.failed ? `, <span style="color:var(--failed)">${f.failed} failed</span>` : ""}</span>
      </div>`).join("");
  }

  const sk = d.skills || [];
  $("#skills").innerHTML = sk.length ? `<table><tr>
      <th style="width:3px"></th><th>skill</th><th>version</th><th>digest</th>
      <th class="num">units</th><th>source</th></tr>
    ${sk.map(k => `<tr class="${k.unregistered ? "attn" : ""}">
      <td class="stripe"></td>
      <td class="mono">${esc(k.name)}</td>
      <td class="muted">${esc(k.version || "—")}</td>
      <td class="mono muted">${esc(k.digest || "—")}</td>
      <td class="num">${(k.units || 0).toLocaleString()}</td>
      <td class="muted">${k.unregistered
        ? '<span class="pill slow">not registered</span> nothing records where to get this'
        : esc(k.source || "—")}</td></tr>`).join("")}</table>`
    : `<div class="empty">No skills registered. A kind can require them with
       <span class="mono">--skill</span>, and
       <span class="mono">fleetwright skill &lt;name&gt; --source FILE</span>
       says what the name means.</div>`;

  const pm = d.per_model || [];
  if (pm.length) {
    $("#workers").insertAdjacentHTML("beforeend", `<table style="margin-top:14px"><tr>
        <th>model</th><th class="num">done</th><th class="num">failed</th>
        <th class="num">mean</th><th class="num">cost</th>
        <th class="num">per unit</th><th class="num">tokens</th></tr>
      ${pm.map(m => `<tr><td class="mono">${esc(m.model)}</td>
        <td class="num">${m.done}</td>
        <td class="num" ${m.failed ? 'style="color:var(--failed)"' : ""}>${m.failed}</td>
        <td class="num muted">${dur(m.done ? m.seconds / m.done : null)}</td>
        <td class="num">${m.priced ? "$" + m.cost.toFixed(3) : "—"}</td>
        <td class="num muted">${m.priced ? "$" + (m.cost / m.priced).toFixed(4) : "—"}</td>
        <td class="num muted">${((m.tokens_in + m.tokens_out) / 1000).toFixed(0)}k</td>
        </tr>`).join("")}</table>`);
  }

  $("#failures").innerHTML = d.failures.length ? `<table><tr>
      <th style="width:3px"></th><th>unit</th><th class="num">tries</th>
      <th>why</th></tr>
    ${d.failures.map(f => `<tr class="bad"><td class="stripe"></td>
      <td class="mono">${esc(f.name)}</td>
      <td class="num">${f.attempts}</td>
      <td>${esc(f.note || "no reason recorded")}</td></tr>`).join("")}</table>`
    : `<div class="empty">Nothing has been given up on.</div>`;

  // A bare clock time next to a dot reads as a timer or a countdown — it was
  // neither, and nobody could tell what it counted. It is how stale the page
  // is, said in those words, and it ticks locally between polls so a server
  // that has stopped answering is visible rather than frozen at a plausible
  // time.
  LAST_UPDATE = Date.now();
  LAST_LEASED = t.leased;
  tickFreshness();
}

let LAST_UPDATE = null, LAST_LEASED = 0;

function tickFreshness() {
  const stale = LAST_UPDATE === null;
  const secs = stale ? null : Math.round((Date.now() - LAST_UPDATE) / 1000);
  const text = stale ? "not yet updated"
    : secs < 5 ? "updated just now"
    : secs < 90 ? `updated ${secs}s ago`
    : `updated ${Math.round(secs / 60)}m ago`;
  // Amber past ten seconds: the poll is every two, so anything older means the
  // server is not answering.
  const colour = stale ? "var(--open)"
    : secs > 10 ? "var(--warn)"
    : LAST_LEASED ? "var(--leased)" : "var(--done)";
  for (const [a, b] of [["#ago", "#dot"], ["#ago2", "#dot2"]]) {
    $(a).textContent = text;
    $(b).style.background = colour;
  }
}
setInterval(tickFreshness, 1000);

let TIMER = null;

function startPolling() {
  if (TIMER === null) TIMER = setInterval(poll, 2000);
}

function showGate() {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  // Stop polling. Without this the page 401s every two seconds for as long as
  // the login screen is open — a console full of errors, and a request the
  // server can only ever refuse.
  if (TIMER !== null) { clearInterval(TIMER); TIMER = null; }
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
  if (r.ok) {
    $("#token").value = "";
    $("#gateerr").hidden = true;
    await poll();
    startPolling();
  }
  else { $("#gateerr").hidden = false; $("#token").select(); }
});

// Auto-collapse below the width where three columns stop fitting, but never
// override a choice the user has made: an explicit toggle is remembered and
// wins at every width.
const RAIL_KEY = "sa_rail";
function applyRail(shut) {
  $("#shell").classList.toggle("railshut", shut);
  $("#collapse").title = shut ? "Expand sidebar" : "Collapse sidebar";
}
function autoRail() {
  const stored = localStorage.getItem(RAIL_KEY);
  applyRail(stored === null ? window.innerWidth < 1180 : stored === "1");
}
$("#collapse").addEventListener("click", () => {
  const shut = !$("#shell").classList.contains("railshut");
  localStorage.setItem(RAIL_KEY, shut ? "1" : "0");
  applyRail(shut);
});
addEventListener("resize", () => {
  if (localStorage.getItem(RAIL_KEY) === null) autoRail();
});
autoRail();

$("#nav-overview").addEventListener("click", () => setView("overview"));
$("#nav-jobs").addEventListener("click", () => setView("jobs"));
let qtimer = null;
$("#jobq").addEventListener("input", e => {
  JOBQ = e.target.value.trim();
  PAGE_N = 1;
  // Debounced: a keystroke per request would put a query on the database for
  // every letter typed.
  clearTimeout(qtimer);
  qtimer = setTimeout(loadJobs, 200);
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
} else {
  // The first poll decides: it either renders, or discovers a 401 and puts the
  // gate up. Only then does the interval start.
  poll().then(() => {
    if (!$("#gate").hidden) return;
    setView(VIEW);
    startPolling();
  });
}
</script>
"""


def _json_for_script(data: dict) -> str:
    """JSON safe to inline in a <script> block.

    `json.dumps` escapes quotes and backslashes but NOT `</`, so a unit named
    `</script><img src=x onerror=...>` closes the tag and the rest of the
    document is attacker-controlled. That name does not need database access:
    a worker calls `finish_job(then={"audit": ["</script>..."]})` over MCP and
    the orchestrator's snapshot carries it. Unit names, run labels, notes,
    results, worker names and model strings all reach this string, and a
    snapshot is the file people mail to each other.

    U+2028 and U+2029 are here because they are legal inside a JSON string and
    are literal line terminators in JavaScript, so one in a name is a syntax
    error rather than an exploit -- a blank page with a console message nobody
    reads.
    """
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def page(db: Path, data: dict | None = None) -> str:
    return (PAGE.replace("__TITLE__", f"fleetwright · {db.name}")
                .replace("__DATA__", _json_for_script(data) if data else "null"))


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
    return {**leases.stats(conn, run=run, reclaim_first=False),
            "runs": leases.runs(conn, limit=25),
            "flow": leases.flow(conn, run=run),
            "graph": leases.graph(conn, run=run),
            "timeline": leases.timeline(conn, run=run),
            "skills": leases.skills(conn),
            "selected": run,
            "run_meta": leases.run(conn, run) if run else None,
            "projects": projects if projects is not None else [],
            "project": project,
            "auth": auth,
            "authed": True,
            "version": __version__}


class _Handler(BaseHTTPRequestHandler):
    projects: dict[str, Path]
    token: str | None
    sessions: dict

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
        # Strict, because everything here is inline and nothing is fetched:
        # no CDN, no font, no image, no XHR to anywhere but this origin. A
        # policy this tight would be painful on an ordinary page and costs
        # nothing on one with no external dependencies -- and it is a second
        # line behind the snapshot escaping, since an injected <script> has
        # nowhere to send what it steals.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
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
        if not sid:
            return False
        born = self.sessions.get(sid)
        if born is None:
            return False
        if time.time() - born > SESSION_SECONDS:
            del self.sessions[sid]
            return False
        return True

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

        if path == "/favicon.ico":
            # Belt and braces: the inline <link> means this is rarely reached,
            # but a 404 in someone's console is a bug report waiting to happen.
            self._send(b"", "image/svg+xml", status=204)
            return

        if path == "/api/units":
            if not self._authed():
                self._json({"auth_required": True}, status=401)
                return
            _, db = self._project(q)
            if db is None:
                self._json({"error": "no such project"}, status=404)
                return
            conn = leases.connect_readonly(db)
            try:
                one = lambda k: (q.get(k) or [None])[0]  # noqa: E731
                try:
                    # `?limit=abc` used to raise ValueError out of the handler,
                    # which drops the connection: the browser reports a network
                    # error and the log shows a traceback, for a typo.
                    limit = min(int(one("limit") or 100), 500)
                    offset = max(0, int(one("offset") or 0))
                except ValueError:
                    self._json({"error": "limit and offset must be whole "
                                         "numbers"}, status=400)
                    return
                self._json(leases.units(
                    conn, run=one("run"), kind=one("kind"),
                    status=one("status"), q=one("q"),
                    limit=limit, offset=offset))
            finally:
                conn.close()
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
            conn = leases.connect_readonly(db)
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
            self.sessions.pop(sid, None)
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
            # Expire server-side too. The cookie says Max-Age=86400 and the
            # server used to keep the id for the life of the process, so a
            # cookie a browser had already discarded still authenticated.
            now = time.time()
            for old, born in list(self.sessions.items()):
                if now - born > SESSION_SECONDS:
                    del self.sessions[old]
            self.sessions[sid] = now
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
    conn = leases.connect_readonly(db)
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
            "      fleetwright dashboard --host 0.0.0.0 --token \"$(openssl rand -hex 24)\"")
    handler = type("Handler", (_Handler,), {
        "projects": projects, "token": token, "sessions": {}})
    with ThreadingHTTPServer((host, port), handler) as httpd:
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        url = f"http://{shown}:{port}"
        print(f"fleetwright dashboard  {url}   (ctrl-c to stop)")
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
