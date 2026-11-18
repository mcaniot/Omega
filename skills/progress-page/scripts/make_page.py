#!/usr/bin/env python3
"""Render a static progress page (index.html) next to a JSON state file, and the hub of all pages.

Usage:
  make_page.py STATE.json [--step N=STATUS[::note]]... [--add-step TEXT]... [--log TEXT]
                          [--fig FILE=TITLE[::note]]... [--ask TEXT] [--warn TEXT]
  make_page.py STATE.json --show          print task, agent, numbered steps, question and warning
  make_page.py --find TASK                print the registered state of a task (after /clear or handoff)
  make_page.py STATE.json --forget        remove the page from the hub
  make_page.py --all                      re-render every registered page and the hub

Update flags change STATE.json (dated, validated, written atomically under a lock) before rendering;
steps are numbered from 1 as on the page. N=STATUS keeps the note, N=STATUS:: clears it, N=STATUS::x
sets it. --ask '' and --warn '' clear. --fig FILE is relative to the state directory and must exist. Each render also registers the page in
one hub, $PROGRESS_HUB/index.html (default ~/.agents/progress), which lists every tracked page in a
single tab: tasks waiting for the user first, then blocked, active and finished ones.

The page opens from file:// without a server; its labels are French. State keys (all text is plain;
`x` renders as code):
  title, subtitle?, summary?, warning?, ask? (text or list: a question waiting for the user),
  task? (basename of ~/.agents/tasks/active/<task>.md), agent? (session currently in charge),
  steps: [{step, status: fait|en cours|à faire|bloqué, note?, date?}],
  figures: [{file, title, note?, full?, date?}]  (relative to the state directory, oldest first;
                                                  .mp4/.webm → video; full: link to the full render),
  log?: [{date, text}]                           (oldest first; older than the 5 most recent fold away),
  sections?: [{title, items: [text]}].
"""
import argparse
import fcntl
import html
import json
import os
import pathlib
import re
import struct
import sys
import tempfile
import time
import urllib.parse

# Status → CSS class: one colour and one icon shape each. "en cours" is blue because solid orange is
# reserved for "À toi" (a question waiting for the user).
STATUS = {'fait': 'ok', 'en cours': 'run', 'à faire': 'todo', 'bloqué': 'bad'}
# Indexed by rank(): CSS class, label of one task, title of its hub group.
LEVELS = (('ask', 'À toi', 'À toi'), ('bad', 'Bloquée', 'Bloquées'), ('run', 'En cours', 'En cours'),
          ('ok', 'Terminée', 'Terminées'))
LOG_SHOWN = 5
VIDEO = ('.mp4', '.webm')
MIN_REFRESH = 5
HUB_REFRESH = 60
STALE_SECONDS = 2 * 3600
LOGO = (pathlib.Path(__file__).resolve().parents[1] / 'assets/omega.png').as_uri()
# Icons are inlined once per page and referenced with <use>: the page stays one offline file.
SPRITE = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
    '<symbol id="i-ok" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/>'
    '<path d="M4.8 8.3l2.2 2.2 4.2-4.7" fill="none" stroke="var(--surface)" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"/></symbol>'
    '<symbol id="i-run" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" '
    'stroke-width="2"/><path d="M8 3a5 5 0 0 1 0 10z" fill="currentColor"/></symbol>'
    '<symbol id="i-todo" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" '
    'stroke-width="1.6" stroke-dasharray="2.7 2"/></symbol>'
    '<symbol id="i-bad" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/>'
    '<path d="M4.6 8h6.8" stroke="var(--surface)" stroke-width="2" stroke-linecap="round"/></symbol>'
    '<symbol id="i-ask" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round"><circle cx="8" cy="8" r="6.2"/><path d="M6.2 6.4a1.9 1.9 0 1 1 2.8 1.6c-.6.4-1 .8-1 1.5"/>'
    '<circle cx="8" cy="11.6" r=".5" fill="currentColor"/></symbol>'
    '<symbol id="i-warn" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.2l6.3 11H1.7z"/><path d="M8 6.6v3"/>'
    '<circle cx="8" cy="11.4" r=".4" fill="currentColor"/></symbol>'
    '<symbol id="i-clock" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.3 1.5"/></symbol>'
    '<symbol id="i-log" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4.3 4.6A5.2 5.2 0 1 1 2.8 8"/><path d="M4.4 1.9v2.8H1.6"/></symbol>'
    '<symbol id="i-img" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" '
    'stroke-linecap="round" stroke-linejoin="round"><rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.8"/>'
    '<circle cx="5.6" cy="6.3" r="1.1"/><path d="M2.4 11.8l3.4-3.2 2.6 2.3 2-1.8 3.2 2.9"/></symbol>'
    '</defs></svg>')
# Omega palette: navy #17212a, orange #ff6905, gold #fbbc1d. Every text/background pair is WCAG AA in both themes.
CSS = """
:root{color-scheme:light dark;--bg:#f5f6f7;--surface:#fff;--surface-2:#eef0f2;--border:#e1e5e9;--border-2:#8a949d;
--track:#d6dbe0;--text:#17212a;--text-2:#4a5661;--text-3:#5f6b76;--accent:#ff6905;--on-accent:#17212a;
--accent-text:#b54708;--accent-tint:#fff1e6;--accent-line:#ffc59b;--accent-strong:#c2410c;--ok:#17723c;
--ok-tint:#e7f5ec;--ok-fill:#2da160;--run:#1f5fcc;--run-tint:#e9f0fd;--run-fill:#3b82f6;--warn:#7a5200;
--warn-tint:#fff5d6;--bad:#b4232c;--bad-tint:#fdecec;--bad-fill:#dc3b45;--todo:#5f6b76;--todo-tint:#eef0f2;
--matte:#e7eaed;--shadow:0 1px 2px rgb(23 33 42/.06),0 8px 20px rgb(23 33 42/.07);--r1:6px;--r2:10px;--r3:14px;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",Ubuntu,Cantarell,sans-serif;
--mono:ui-monospace,"SFMono-Regular","JetBrains Mono","DejaVu Sans Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#17212a;--surface:#1d2832;--surface-2:#243039;--border:#2e3a45;
--border-2:#5b6772;--track:#34414d;--text:#f4f6f8;--text-2:#c5c8cb;--text-3:#a2a6aa;--accent-text:#ff9248;
--accent-tint:#3a2a1f;--accent-line:#7a431c;--accent-strong:#ff6905;--ok:#7bd88f;--ok-tint:#1f3a31;--ok-fill:#3fbf68;
--run:#7cb7ff;--run-tint:#1f3348;--run-fill:#5aa2f5;--warn:#fbbc1d;--warn-tint:#3a3220;--bad:#ff8a9c;
--bad-tint:#3d2630;--bad-fill:#ff6883;--todo:#a2a6aa;--todo-tint:#243039;--matte:#111920;
--shadow:0 0 0 1px var(--border-2)}}
.ok{--c:var(--ok);--t:var(--ok-tint);--f:var(--ok-fill)} .run{--c:var(--run);--t:var(--run-tint);--f:var(--run-fill)}
.bad{--c:var(--bad);--t:var(--bad-tint);--f:var(--bad-fill)} .todo{--c:var(--todo);--t:var(--todo-tint);--f:var(--track)}
.warn{--c:var(--warn);--t:var(--warn-tint)} .ask{--c:var(--on-accent);--t:var(--accent)}
*,*::before,*::after{box-sizing:border-box}
html{scroll-padding-top:68px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:450 15px/1.5 var(--sans);font-variant-numeric:tabular-nums}
body.hub{font-size:14px}
h1,h2,h3,p,ol,ul,figure{margin:0} ol,ul{padding:0;list-style:none}
a{color:inherit;text-decoration-color:var(--border-2);text-underline-offset:2px} a:hover{text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--accent-strong);outline-offset:2px;border-radius:var(--r1)}
code{font:.88em var(--mono);background:var(--surface-2);border-radius:4px;padding:.1em .34em;overflow-wrap:anywhere}
img,video{max-width:100%}
.wrap{max-width:1200px;margin:0 auto;padding:0 24px}
.i{width:14px;height:14px;flex:none;color:var(--c,currentColor)} .i.ask{color:var(--accent-text)}
.top{position:sticky;top:0;z-index:5;height:52px;border-bottom:1px solid var(--border);
 background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
.top .wrap{height:100%;display:flex;align-items:center;gap:10px}
.brand{flex:none;display:flex;align-items:center;gap:10px;font-weight:650;text-decoration:none;white-space:nowrap}
.brand img{width:30px;height:30px}
.crumb{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-3)}
.crumb::before{content:"/";margin-right:10px;color:var(--border-2)} .sp{flex:1}
.sw{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2);white-space:nowrap;cursor:pointer}
.sw input{appearance:none;flex:none;position:relative;width:30px;height:18px;margin:0;border-radius:9px;
 background:var(--border-2);cursor:pointer}
.sw input::before{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--surface)}
.sw input:checked{background:var(--text)} .sw input:checked::before{left:14px}
.sw input:focus-visible{border-radius:9px}
h1{font-size:28px;font-weight:680;letter-spacing:-.015em;line-height:1.2}
h2{font-size:18px;font-weight:650;line-height:1.3;margin-bottom:12px}
h2 small{margin-left:10px;font-size:12px;font-weight:450;color:var(--text-3)}
.d{font-size:12px;color:var(--text-3);white-space:nowrap}
.empty{padding:20px;border:1px dashed var(--border-2);border-radius:var(--r3);color:var(--text-3);font-size:13px;text-align:center}
.pill{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 9px 0 5px;border-radius:11px;
 background:var(--t);color:var(--c);font-size:12px;font-weight:600;line-height:1;white-space:nowrap;vertical-align:middle}
.pill .i{color:currentColor}
.bar{display:flex;gap:2px;height:6px} .bar.big{height:8px} .bar i{flex:1;min-width:2px;border-radius:4px;background:var(--f)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r3);padding:16px 20px}

.head{display:flex;flex-wrap:wrap;align-items:center;gap:12px 24px;margin:28px 0 8px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}
.chip{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 12px 0 8px;border:1px solid var(--border);
 border-radius:14px;background:var(--surface);color:var(--text-2);font-size:13px;text-decoration:none;white-space:nowrap}
.chip b{color:var(--text);font-weight:650} a.chip:hover{border-color:var(--border-2)}
.chip.zero{background:none} .chip.zero b{color:inherit;font-weight:450} .chip.zero .i{color:currentColor}
.chip.ask:not(.zero){background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.chip.ask:not(.zero) b,.chip.ask:not(.zero) .i{color:inherit}
.lead{display:flex;align-items:center;gap:8px;color:var(--text-2);font-size:15px}
.lead .i{width:16px;height:16px} .lead.wait{color:var(--text);font-weight:600}
.group{margin-top:24px} .hub>.wrap{padding-bottom:48px}
.gh{margin:0;font-size:12px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3)}
.gh span{margin-left:8px;color:var(--text-2)}
summary{cursor:pointer;color:var(--text-2);font-size:13px} summary .gh{display:inline}
.tasks{display:grid;gap:8px;margin-top:8px}
.task{position:relative;display:grid;grid-template-columns:176px minmax(0,1fr) 180px;gap:4px 16px;align-items:start;
 padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r3)}
.task:hover{box-shadow:var(--shadow)} .task.r0{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.task.r0:hover{box-shadow:inset 0 0 0 1px var(--accent),var(--shadow)}
.thumb{position:relative;z-index:1;display:grid;place-items:center;aspect-ratio:16/9;overflow:hidden;
 border-radius:var(--r2);background:var(--matte);color:var(--border-2)}
.thumb img{width:100%;height:100%;object-fit:cover;display:block} .thumb .i{width:28px;height:28px}
.task h3{font-size:16px;font-weight:650;line-height:1.3;overflow-wrap:anywhere}
.task h3 a{text-decoration:none} .task h3 a::after{content:"";position:absolute;inset:0;border-radius:var(--r3)}
.task h3 a:focus-visible{outline:none} .task h3 a:focus-visible::after{outline:2px solid var(--accent-strong);outline-offset:2px}
.new{display:none;margin-left:8px;padding:1px 7px;border-radius:9px;background:var(--text);color:var(--bg);
 font-size:11px;font-weight:600;vertical-align:2px} .isnew .new{display:inline-block}
.cur,.line{display:flex;align-items:center;gap:7px;min-width:0}
.cur{margin-top:6px;font-weight:600} .line{margin-top:3px;font-size:13px;color:var(--text-3)}
.tx{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.more{flex:none;font-size:12px;font-weight:450;color:var(--text-3)}
.q{display:flex;gap:8px;margin-top:10px;padding:8px 12px;border:1px solid var(--accent-line);border-radius:var(--r2);
 background:var(--accent-tint);font-weight:600} .q .i{margin-top:4px}
.meta{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:6px 8px;padding-top:6px;
 font-size:12px;color:var(--text-3)}
.meta .bar,.who{flex:0 0 100%} .meta b{color:var(--text);font-weight:650}
.stalemsg{display:none;align-items:center;gap:5px;color:var(--text);font-weight:600} .stale .stalemsg{display:inline-flex}
.gone .body{grid-column:2/-1} .gone h3{font-family:var(--mono);font-size:13px;font-weight:600}
.gone .cur{align-items:flex-start;font-weight:450} .gone .cur .i{margin-top:4px} .gone .line{display:block;position:relative;z-index:1}

.hero{margin:28px 0 20px} .hero .h{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.sub{max-width:92ch;margin-top:6px;color:var(--text-2)}
.prog{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;margin-top:16px;font-size:13px;color:var(--text-2)}
.prog .bar{flex:0 1 300px;min-width:160px} .prog b{color:var(--text);font-weight:650}
.prog>span:not(.bar){display:inline-flex;align-items:center;gap:5px}
.banner{display:flex;align-items:flex-start;gap:12px;margin-bottom:20px;padding:12px 16px;border:1px solid var(--accent-line);
 border-radius:var(--r3);background:var(--accent-tint);font-weight:600}
.banner .i{width:18px;height:18px;margin-top:2px} .banner b{color:var(--accent-text);font-weight:680}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 332px;gap:24px;align-items:start;padding-bottom:48px}
main{display:grid;gap:28px;min-width:0}
aside{position:sticky;top:68px;display:grid;gap:16px;max-height:calc(100vh - 84px);overflow:auto}
aside h2{font-size:15px}
.prose{max-width:76ch}
.reserve{display:flex;gap:9px;max-width:76ch;margin-top:12px;padding:9px 12px;border-radius:var(--r2);
 background:var(--t);color:var(--c);font-size:14px} .reserve .i{margin-top:4px} .reserve:first-of-type{margin-top:0}
.steps{background:var(--surface);border:1px solid var(--border);border-radius:var(--r3);overflow:hidden}
.step{display:grid;grid-template-columns:16px 20px minmax(0,1fr) auto;gap:0 10px;align-items:start;padding:10px 16px;
 border-top:1px solid var(--border)} .step:first-child{border-top:0}
.step.run,.step.bad{background:color-mix(in srgb,var(--t) 50%,var(--surface))}
.step>.i{width:16px;height:16px;margin-top:3px}
.step .n{font-size:13px;line-height:22px;text-align:right;color:var(--text-3)}
.step .t{color:var(--text-2)} .step.run .t,.step.bad .t{color:var(--text);font-weight:600}
.step .pill{margin-left:8px}
.step .note{max-width:74ch;margin-top:2px;font-size:13px;color:var(--text-3)}
.step.run .note,.step.bad .note{color:var(--text-2)} .step .d{line-height:22px}
.figs{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr));gap:12px}
.fig{display:flex;flex-direction:column;overflow:hidden;background:var(--surface);border:1px solid var(--border);
 border-radius:var(--r3)} .fig.wide{grid-column:1/-1}
.m{display:grid;place-items:center;flex:1;background:var(--matte)} .zoom{cursor:zoom-in}
.m img,.m video{display:block;width:auto;height:auto;max-width:100%;max-height:380px}
.fig.wide .m img,.fig.wide .m video{max-height:520px}
figcaption{display:grid;gap:2px;padding:10px 14px;font-size:13px;color:var(--text-2)}
figcaption b{font-size:14px;font-weight:600;color:var(--text)}
.list{max-width:78ch} .list li{position:relative;margin-top:6px;padding-left:16px}
.list li::before{content:"";position:absolute;left:2px;top:.62em;width:5px;height:5px;border-radius:50%;background:var(--border-2)}
.journal li{position:relative;padding:0 0 14px 20px;font-size:13px;line-height:1.45;color:var(--text-2)}
.journal li:last-child{padding-bottom:0} .journal .d{display:block}
.journal li::before{content:"";position:absolute;z-index:1;left:0;top:4px;width:9px;height:9px;border-radius:50%;
 background:var(--track);box-shadow:0 0 0 3px var(--surface)}
.journal li::after{content:"";position:absolute;left:4px;top:8px;bottom:-8px;width:1px;background:var(--border)}
.journal li:last-child::after{display:none} .journal.recent li:first-child::before{background:var(--accent)}
aside details{margin-top:14px} aside details .journal{margin-top:14px}
.toc a{display:block;padding:3px 0;font-size:13px;color:var(--text-2);text-decoration:none} .toc a:hover{color:var(--text)}
#lb{width:100vw;height:100vh;max-width:100vw;max-height:100vh;margin:0;padding:0;border:0;background:rgb(13 19 25/.94);
 color:#f4f6f8} #lb[open]{display:grid;place-items:center} #lb::backdrop{background:none}
#lb .in{display:grid;justify-items:center;gap:10px;max-width:calc(100vw - 128px)}
#lb img{display:block;max-width:100%;max-height:calc(100vh - 96px);object-fit:contain;border-radius:var(--r1)}
#lb .cap{font-size:13px;text-align:center} #lb .cap a{margin-left:12px;color:#c5c8cb}
#lb button{position:fixed;width:40px;height:40px;border:0;border-radius:50%;background:rgb(255 255 255/.14);color:#fff;
 font:400 24px/1 var(--sans);cursor:pointer} #lb button:hover{background:rgb(255 255 255/.26)}
#lb :focus-visible{outline-color:#ff9248}
#lb .x{top:16px;right:16px} #lb .p{left:16px;top:calc(50% - 20px)} #lb .n{right:16px;top:calc(50% - 20px)}
#lb.one .nav{display:none}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}
 .task,.chip,.sw input,.sw input::before{transition:box-shadow .15s,border-color .15s,background-color .15s,left .15s}
 .i.run{animation:pulse 2.4s ease-in-out infinite} @keyframes pulse{50%{opacity:.5}}}
@media (max-width:980px){.layout{grid-template-columns:minmax(0,1fr)} aside{position:static;max-height:none;overflow:visible}}
@media (max-width:820px){.task{grid-template-columns:120px minmax(0,1fr)}
 .meta{grid-column:1/-1;flex-wrap:nowrap;padding-top:4px} .meta .bar{flex:1 1 80px} .meta>*,.who{flex:none}
 .stats{margin-left:0}}
@media (max-width:520px){.wrap{padding:0 14px} h1{font-size:24px} .sw{font-size:0;gap:0}
 .task{grid-template-columns:minmax(0,1fr) 84px} .thumb{grid-column:2;grid-row:1} .body{grid-column:1;grid-row:1}
 .gone .body{grid-column:1/-1} .gone .thumb{display:none} .meta{flex-wrap:wrap} .meta .bar{flex:0 0 100%} .who{margin-left:auto}
 .step{grid-template-columns:16px minmax(0,1fr) auto;padding:10px 12px} .step .n{display:none} .card{padding:14px}}
"""
# Reload while not paused, no video plays and the image viewer is closed (WCAG 2.2.1: the user can stop
# it). Task pages reload only while visible; the hub also reloads in the background so its tab title
# count stays current. The browser restores the scroll position on reload; open <details> are restored here.
# Times are relative ("il y a 2 h 10"); cards unseen since their last update get "nouveau".
RELOAD_JS = """
const K='progress-page:'+location.pathname, box=document.getElementById('auto');
box.checked=localStorage.getItem(K+':paused')!=='1';
box.onchange=()=>localStorage.setItem(K+':paused',box.checked?'0':'1');
document.querySelectorAll('details[id]').forEach(d=>{
  d.open=sessionStorage.getItem(K+':'+d.id)==='1';
  d.ontoggle=()=>sessionStorage.setItem(K+':'+d.id,d.open?'1':'0');});
const age=s=>{const m=Math.max(0,Math.round(Date.now()/1000-s)/60|0);
  return m<1?"à l'instant":m<60?'il y a '+m+' min':m<2880?'il y a '+(m/60|0)+' h '+String(m%60).padStart(2,'0')
    :'il y a '+(m/1440|0)+' j';};
document.querySelectorAll('time[data-t]').forEach(t=>t.textContent=age(+t.dataset.t));
// "nouveau": updated since the user last left the hub (the hub's own reloads do not count as leaving).
const V='progress-hub-visit', visit=+(localStorage.getItem(V)||0);
let reloading=false;
const leave=()=>{if(!reloading)localStorage.setItem(V,Date.now()/1000|0);};
document.querySelectorAll('.task[data-t]').forEach(c=>{
  const t=+c.dataset.t;
  if(c.dataset.open==='1'&&Date.now()/1000-t>STALE)c.classList.add('stale');
  if(visit&&t>visit)c.classList.add('isnew');});
const silent=document.getElementById('nsilent');
if(silent){const n=document.querySelectorAll('.task.stale').length;
  silent.textContent=n;silent.parentNode.classList.toggle('zero',!n);}
if(BG){addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')leave();});
  addEventListener('pagehide',leave);if(!visit)leave();}
// Image viewer: a figure opens in the <dialog>; arrows browse, Escape or a click outside the image closes.
const lb=document.getElementById('lb');
if(lb){const zs=[...document.querySelectorAll('a.zoom')],im=lb.querySelector('img'),raw=lb.querySelector('.cap a');
  let k=0;
  const go=i=>{k=(i+zs.length)%zs.length;im.src=raw.href=zs[k].href;
    im.alt=lb.querySelector('.cap b').textContent=zs[k].dataset.title;};
  lb.classList.toggle('one',zs.length<2);
  zs.forEach((a,i)=>a.onclick=e=>{if(e.ctrlKey||e.metaKey||e.shiftKey)return;e.preventDefault();go(i);lb.showModal();});
  lb.onclick=e=>{if(e.target===lb||e.target.matches('.in,.x'))lb.close();};
  lb.querySelector('.p').onclick=()=>go(k-1);lb.querySelector('.n').onclick=()=>go(k+1);
  lb.onkeydown=e=>{if(e.key==='ArrowLeft')go(k-1);if(e.key==='ArrowRight')go(k+1);};}
const busy=()=>document.fullscreenElement||(lb&&lb.open)||[...document.querySelectorAll('video')].some(v=>!v.paused);
const tick=()=>{if(box.checked&&(BG||document.visibilityState==='visible')&&!busy()){reloading=true;location.reload();}};
setInterval(tick,REFRESH*1000);
addEventListener('pageshow',e=>{if(e.persisted)tick();});
"""
VIEWER = ('<dialog id="lb" aria-label="Visuel agrandi"><div class="in"><img alt=""><p class="cap"><b></b>'
          '<a href="">ouvrir le fichier</a></p></div><button class="nav p" aria-label="Précédent">‹</button>'
          '<button class="nav n" aria-label="Suivant">›</button>'
          '<button class="x" aria-label="Fermer" autofocus>×</button></dialog>')


def text(value):
    """Escape plain text; `code` spans become <code>."""
    return re.sub(r'`([^`]+)`', r'<code>\1</code>', html.escape(str(value)))


def asks(state):
    value = state.get('ask') or []
    return [value] if isinstance(value, str) else value


def validate(state):
    """Return a readable error for a malformed state, or None."""
    if not isinstance(state, dict) or not isinstance(state.get('title'), str):
        return 'state must be an object with a string "title"'
    steps = state.get('steps')
    if not isinstance(steps, list):
        return '"steps" must be a list'
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or 'step' not in step or step.get('status') not in STATUS:
            return f'steps[{i}] needs "step" and "status" among {", ".join(STATUS)}'
    ask = state.get('ask')
    if ask is not None and not isinstance(ask, str) and not (isinstance(ask, list) and all(isinstance(a, str) for a in ask)):
        return '"ask" must be a text or a list of texts'
    for key, fields in (('figures', ('file', 'title')), ('log', ('date', 'text')), ('sections', ('title', 'items'))):
        items = state.get(key, [])
        if not isinstance(items, list) or not all(isinstance(e, dict) and all(f in e for f in fields) for e in items):
            return f'"{key}" must be a list of objects with {", ".join(fields)}'
    return None


def png_size(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None  # not a PNG, or still being written
    return struct.unpack('>II', head[16:24])


def is_video(name):
    return name.lower().endswith(VIDEO)


def url(name):
    # Quoting keeps the URL relative: '#', '?', '%' stay in the name and 'javascript:' is not a scheme.
    return html.escape(urllib.parse.quote(name), quote=True)


def icon(name, label=''):
    """Sprite icon; `label` names a status that no visible text repeats."""
    aria = f'role="img" aria-label="{html.escape(label, quote=True)}"' if label else 'aria-hidden="true"'
    return f'<svg class="i {name}" {aria}><use href="#i-{name}"/></svg>'


def pill(cls, label):
    return f'<span class="pill {cls}">{icon(cls)}{text(label)}</span>'


def short_date(value):
    """Display only: '2026-11-18 16:24' → '18/11 16:24'; any other format is kept."""
    m = re.fullmatch(r'\d{4}-(\d\d)-(\d\d)(?:[ T](\d\d:\d\d)(?::\d\d)?)?', str(value).strip())
    return f'{m[2]}/{m[1]}' + (f' {m[3]}' if m[3] else '') if m else text(value)


def progress(steps, big=False):
    """Segmented bar, one segment per step in order, and the 'done/total' count."""
    counts = [(sum(s['status'] == name for s in steps), name) for name in STATUS]
    label = ', '.join(f'{n} {name}' for n, name in counts if n) or 'aucune étape'
    segments = ''.join(f'<i class="{STATUS[s["status"]]}"></i>' for s in steps) or '<i class="todo"></i>'
    bar = f'<span class="bar{" big" if big else ""}" role="img" aria-label="{html.escape(label, quote=True)}">{segments}</span>'
    return bar, f'{counts[0][0]}/{len(steps)}'


def figure(root, fig, eager, lead=False):
    src = url(fig['file'])
    if is_video(fig['file']):
        # No link around a video: clicks on its controls would open the file.
        media = f'<div class="m"><video src="{src}" controls muted loop preload="metadata"></video></div>'
    else:
        size = png_size(os.path.join(root, fig['file']))
        dims = f' width="{size[0]}" height="{size[1]}"' if size else ''
        lazy = '' if eager else ' loading="lazy"'
        title = html.escape(fig['title'], quote=True)
        # The viewer script opens .zoom links in the <dialog>; without script the link opens the file.
        media = f'<a class="m zoom" href="{src}" data-title="{title}"><img src="{src}" alt="{title}"{dims}{lazy}></a>'
    note = f'<span>{text(fig["note"])}</span>' if fig.get('note') else ''
    extra = [f'<a href="{url(fig["full"])}">rendu complet</a>'] if fig.get('full') else []
    if fig.get('date'):
        extra.append(short_date(fig['date']))
    foot = f'<span class="d">{" · ".join(extra)}</span>' if extra else ''
    return (f'<figure class="fig{" wide" if lead else ""}">{media}'
            f'<figcaption><b>{text(fig["title"])}</b>{note}{foot}</figcaption></figure>')


def hub_dir():
    return pathlib.Path(os.environ.get('PROGRESS_HUB') or pathlib.Path.home() / '.agents/progress').expanduser().resolve()


def document(title, body, refresh, background=False, crumb=''):
    """Page shell; `background` marks the hub, `crumb` is the escaped title shown after the hub link."""
    hub = html.escape((hub_dir() / 'index.html').as_uri(), quote=True)
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f'<link rel="icon" href="{LOGO}"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body class="{"hub" if background else "page"}">\n{SPRITE}\n'
            f'<header class="top"><div class="wrap"><a class="brand" href="{hub}"><img src="{LOGO}" alt="">'
            'Suivi des tâches</a>' + (f'<span class="crumb">{crumb}</span>' if crumb else '') + '<span class="sp"></span>'
            '<label class="sw"><input type="checkbox" role="switch" id="auto">Rechargement auto</label></div></header>\n'
            f'<div class="wrap">\n{body}\n</div>\n'
            f'<script>const REFRESH={int(refresh)},BG={str(background).lower()},STALE={STALE_SECONDS};'
            f'{RELOAD_JS}</script>\n</body></html>\n')


def ask_text(state):
    return '<br>'.join(text(a) for a in asks(state))


def log_items(entries):
    return ''.join(f'<li><span class="d">{short_date(e["date"])}</span>{text(e["text"])}</li>' for e in entries)


def render(state, root, refresh, mtime):
    steps = state['steps']
    rows = ''
    for i, s in enumerate(steps, 1):
        cls = STATUS[s['status']]
        # Only active and blocked steps carry a pill; done and pending ones recede behind their icon.
        loud = pill(cls, s['status'].capitalize()) if cls in ('run', 'bad') else ''
        note = f'<p class="note">{text(s["note"])}</p>' if s.get('note') else ''
        date = f'<span class="d">{short_date(s["date"])}</span>' if s.get('date') else '<span></span>'
        rows += (f'<li class="step {cls}">{icon(cls, "" if loud else s["status"])}<span class="n">{i}</span>'
                 f'<div><span class="t">{text(s["step"])}</span>{loud}{note}</div>{date}</li>')
    figures = list(reversed(state.get('figures', [])))
    grid = ''.join(figure(root, f, i == 0, i == 0 and len(figures) >= 3) for i, f in enumerate(figures))
    bar, count = progress(steps, big=True)
    cls, label, _ = LEVELS[rank(state)]
    facts = [f'{bar}<b>{count} étapes faites</b>',
             f'<span>{icon("clock")}mis à jour <time data-t="{int(mtime)}"></time></span>',
             f'<span>agent {text(state["agent"])}</span>' if state.get('agent') else '',
             f'<span>tâche <code>{text(state["task"])}</code></span>' if state.get('task') else '']
    parts = [f'<header class="hero"><div class="h"><h1>{text(state["title"])}</h1>{pill(cls, label)}</div>'
             + (f'<p class="sub">{text(state["subtitle"])}</p>' if state.get('subtitle') else '')
             + f'<div class="prog">{"".join(facts)}</div></header>']
    if asks(state):
        parts.append(f'<div class="banner">{icon("ask")}<p><b>À toi.</b> {ask_text(state)}</p></div>')
    main, toc = [], []
    if state.get('summary') or state.get('warning'):
        summary = f'<p class="prose">{text(state["summary"])}</p>' if state.get('summary') else ''
        warning = (f'<p class="reserve warn">{icon("warn")}<span><b>Réserve.</b> {text(state["warning"])}</span></p>'
                   if state.get('warning') else '')
        main.append(f'<section id="resume" class="card"><h2>Résumé</h2>{summary}{warning}</section>')
        toc.append(('resume', 'Résumé'))
    main.append('<section id="avancement"><h2>Étapes</h2>'
                + (f'<ol class="steps">{rows}</ol>' if rows else '<p class="empty">Aucune étape définie.</p>') + '</section>')
    main.append('<section id="visuels"><h2>Visuels<small>les plus récents d\'abord</small></h2>'
                + (f'<div class="figs">{grid}</div>' if grid else '<p class="empty">Premiers rendus en préparation.</p>')
                + '</section>')
    toc += [('avancement', 'Étapes'), ('visuels', 'Visuels')]
    for i, section in enumerate(state.get('sections', []), 1):
        items = ''.join(f'<li>{text(x)}</li>' for x in section['items'])
        main.append(f'<section id="section-{i}" class="card"><h2>{text(section["title"])}</h2>'
                    f'<ul class="list">{items}</ul></section>')
        toc.append((f'section-{i}', text(section['title'])))
    log = list(reversed(state.get('log', [])))
    aside = ''
    if log:
        older = log[LOG_SHOWN:]
        aside = (f'<section id="changements" class="card"><h2>Journal</h2>'
                 f'<ol class="journal recent">{log_items(log[:LOG_SHOWN])}</ol>'
                 + (f'<details id="journal-ancien"><summary>{len(older)} plus ancien{"s" if len(older) > 1 else ""}</summary>'
                    f'<ol class="journal">{log_items(older)}</ol></details>' if older else '') + '</section>')
    # No TOC entry for the journal: it sits directly above the TOC, so the link would not move.
    links = ''.join(f'<li><a href="#{anchor}">{name}</a></li>' for anchor, name in toc)
    aside += f'<nav class="card toc" aria-label="Sur cette page"><h2>Sur cette page</h2><ul>{links}</ul></nav>'
    parts.append(f'<div class="layout"><main>{"".join(main)}</main><aside>{aside}</aside></div>')
    if any(not is_video(f['file']) for f in figures):
        parts.append(VIEWER)
    return document(state['title'], '\n'.join(parts), refresh, crumb=text(state['title']))


def write_atomic(path, content):
    """Replace path in one step: a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def rank(state):
    """0 waits for the user, 1 blocked, 2 active, 3 finished."""
    steps = state['steps']
    if asks(state):
        return 0
    if any(s['status'] == 'bloqué' for s in steps):
        return 1
    return 3 if steps and all(s['status'] == 'fait' for s in steps) else 2


def hub_line(cls, name, content, extra=''):
    """One single-line row of a hub card: icon, truncated text, optional suffix."""
    return f'<p class="{cls}">{icon(name)}<span class="tx">{content}</span>{extra}</p>'


def hub_card(state_path):
    root = pathlib.Path(state_path).parent
    with open(state_path, encoding='utf-8') as f:
        state = json.load(f)
    error = validate(state)
    if error:
        raise ValueError(error)
    steps, level, mtime = state['steps'], rank(state), int(os.path.getmtime(state_path))
    numbered = list(enumerate(steps, 1))
    # A blocked step leads; the other steps under way are counted next to it.
    current = sorted((e for e in numbered if e[1]['status'] in ('bloqué', 'en cours')),
                     key=lambda e: e[1]['status'] != 'bloqué')
    if current:
        i, step = current[0]
        more = f'<span class="more">+{len(current) - 1} en parallèle</span>' if len(current) > 1 else ''
        lines = [hub_line('cur', STATUS[step['status']], f'Étape {i} · {text(step["step"])}', more)]
    elif level == 3:
        lines = [hub_line('cur', 'ok', 'Toutes les étapes sont faites')]
    elif steps:
        i, step = next(e for e in numbered if e[1]['status'] == 'à faire')
        lines = [hub_line('cur', 'todo', f'À venir : étape {i} · {text(step["step"])}')]
    else:
        lines = [hub_line('cur', 'todo', 'Aucune étape définie')]
    if state.get('log'):
        last = state['log'][-1]
        lines.append(hub_line('line', 'log', f'{short_date(last["date"])} — {text(last["text"])}'))
    if state.get('warning'):
        lines.append(hub_line('line', 'warn', text(state['warning'])))
    if asks(state):
        lines.append(f'<p class="q">{icon("ask")}<span>{ask_text(state)}</span></p>')
    page = html.escape((root / 'index.html').as_uri(), quote=True)
    images = [f for f in state.get('figures', []) if not is_video(f['file'])]
    thumb = (f'<a class="thumb" href="{page}#visuels" tabindex="-1" aria-hidden="true">'
             f'<img src="{html.escape((root / images[-1]["file"]).as_uri(), quote=True)}" alt="" loading="lazy"></a>'
             if images else f'<span class="thumb">{icon("img")}</span>')
    bar, count = progress(steps)
    agent = f'<span class="who">{text(state["agent"])}</span>' if state.get('agent') else ''
    card = (f'<article class="task r{level}" data-t="{mtime}" data-k="{html.escape(state_path, quote=True)}" '
            f'data-open="{int(level == 2)}">{thumb}<div class="body">'
            f'<h3><a href="{page}">{text(state["title"])}</a><span class="new">nouveau</span></h3>'
            + ''.join(lines) + f'</div><div class="meta">{bar}<span><b>{count}</b> étapes</span>'
            f'<span><time data-t="{mtime}"></time></span>'
            f'<span class="stalemsg">{icon("clock")}sans nouvelles</span>{agent}</div></article>')
    return level, mtime, card


def read_registry(path):
    try:
        paths = json.loads(path.read_text())
    except (OSError, ValueError):
        return []  # missing or corrupt: rebuilt from the pages rendered from now on
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def reachable(path):
    """A state is gone only if its parent directory's parent exists (an unmounted disk keeps its entry)."""
    return os.path.exists(path) or not os.path.isdir(os.path.dirname(os.path.dirname(path)))


def missing_card(path, error):
    label = 'illisible' if os.path.exists(path) else 'introuvable (disque démonté ?)'
    forget = f'make_page.py {path} --forget'
    return 2, 0, (f'<article class="task r1 gone"><span class="thumb">{icon("warn")}</span><div class="body">'
                  f'<h3>{text(path)}</h3>'
                  f'<p class="cur">{icon("bad")}<span>{label} : {text(error)}</span></p>'
                  f'<p class="line">Retirer du hub : <code>{text(forget)}</code></p></div></article>')


def update_hub(add=None, forget=None):
    """Register `add`, drop `forget`, and re-render the hub from every registered page."""
    hub = hub_dir()
    hub.mkdir(parents=True, exist_ok=True)
    with open(hub / 'pages.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # several sessions may render at once
        registry = hub / 'pages.json'
        paths = [p for p in dict.fromkeys(([add] if add else []) + read_registry(registry))
                 if p != forget and reachable(p)]
        write_atomic(str(registry), json.dumps(paths, indent=1) + '\n')
        cards = []
        for p in paths:
            try:
                cards.append(hub_card(p))
            except Exception as error:  # noqa: BLE001 - one broken page must not hide the others
                cards.append(missing_card(p, error))
        cards.sort(key=lambda c: (c[0], -c[1]))
        counts = [sum(c[0] == level for c in cards) for level in range(len(LEVELS))]
        waiting = counts[0]
        chips, groups = [], []
        for level, (cls, one, name) in enumerate(LEVELS):
            n, anchor = counts[level], 'done' if level == 3 else f'g{level}'
            label = f'{icon(cls)}<b>{n}</b> {(name if n > 1 else one).lower()}'
            chips.append(f'<a class="chip {cls}" href="#{anchor}">{label}</a>' if n
                         else f'<span class="chip {cls} zero">{label}</span>')
            if not n:
                continue
            head = f'<h2 class="gh">{name}<span>{n}</span></h2>'
            tasks = '<div class="tasks">' + ''.join(c[2] for c in cards if c[0] == level) + '</div>'
            groups.append(f'<details id="done" class="group"><summary>{head}</summary>{tasks}</details>' if level == 3
                          else f'<section id="{anchor}" class="group">{head}{tasks}</section>')
        # The number of silent tasks depends on the clock: the page script fills it in.
        chips.insert(3, f'<span class="chip zero">{icon("clock")}<b id="nsilent">0</b> sans nouvelles</span>')
        lead = (f'<p class="lead wait">{icon("ask")}{waiting} mission{"s attendent" if waiting > 1 else " attend"} '
                'ta décision.</p>' if waiting else f'<p class="lead">{icon("ok")}Rien n\'attend ta décision.</p>')
        if not any(counts[:3]):
            groups.insert(0, '<p class="empty group">Aucune tâche en cours.</p>')
        body = (f'<div class="head"><h1>Missions</h1><div class="stats">{"".join(chips)}</div></div>{lead}'
                + ''.join(groups))
        title = (f'({waiting}) ' if waiting else '') + 'Suivi des tâches'
        write_atomic(str(hub / 'index.html'), document(title, body, HUB_REFRESH, background=True))
    return hub / 'index.html', paths


def stamp():
    return time.strftime('%d/%m %H:%M')


def apply_updates(state, args, root):
    """Apply the update flags to state; return a short description of each change."""
    changes = []
    for label in args.add_step:
        state['steps'].append({'step': label, 'status': 'à faire'})
        changes.append(f'étape {len(state["steps"])} « {label} » ajoutée')
    if args.task and args.task != state.get('task'):
        state['task'] = args.task
        changes.append(f'tâche {args.task}')
    if args.agent and args.agent != state.get('agent'):
        # A new agent name means a handoff or a fresh session after /clear: keep it in the log.
        previous = state.get('agent')
        state['agent'] = args.agent
        state.setdefault('log', []).append(
            {'date': stamp(), 'text': f'reprise par {args.agent}' + (f' (après {previous})' if previous else '')})
        changes.append(f'agent {args.agent}')
    for spec in args.step:
        index, _, rest = spec.partition('=')
        status, has_note, note = rest.partition('::')
        if not index.isdigit() or not 1 <= int(index) <= len(state['steps']) or status not in STATUS:
            raise ValueError(f'--step {spec!r}: expected N=STATUS[::note], N in 1..{len(state["steps"])}, '
                             f'STATUS among {", ".join(STATUS)}')
        step = state['steps'][int(index) - 1]
        step.update(status=status, date=stamp())
        if note:
            step['note'] = note
        elif has_note:
            step.pop('note', None)
        changes.append(f'étape {index} « {step["step"]} » → {status}')
    for spec in args.fig:
        file, _, rest = spec.partition('=')
        title, _, note = rest.partition('::')
        if not file or not title:
            raise ValueError(f'--fig {spec!r}: expected FILE=TITLE[::note]')
        if not os.path.isfile(os.path.join(root, file)):
            raise ValueError(f'--fig {spec!r}: {file} not found in {root} (FILE is relative to the state directory)')
        state.setdefault('figures', []).append({'file': file, 'title': title, **({'note': note} if note else {}),
                                                'date': stamp()})
        changes.append(f'visuel {file}')
    for key, value, name in (('ask', args.ask, 'question'), ('warning', args.warn, 'alerte')):
        if value is not None:
            if value:
                state[key] = value
            else:
                state.pop(key, None)
            changes.append(name + (' posée' if value else ' retirée'))
    if args.log:
        state.setdefault('log', []).append({'date': stamp(), 'text': args.log})
        changes.append('journal')
    return changes


def load(path):
    with open(path, encoding='utf-8') as f:
        state = json.load(f)
    error = validate(state)
    if error:
        raise ValueError(error)
    return state


def task_of(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('task')
    except (OSError, ValueError, AttributeError):
        return None


def show(state):
    print(f'task: {state.get("task", "?")} · agent: {state.get("agent", "?")}')
    for i, s in enumerate(state['steps'], 1):
        print(f'{i}. [{s["status"]}] {s["step"]}' + (f' — {s["note"]}' if s.get('note') else ''))
    for a in asks(state):
        print(f'ask: {a}')
    if state.get('warning'):
        print(f'warning: {state["warning"]}')


def render_page(state_path, state, refresh):
    root = os.path.dirname(state_path)
    write_atomic(os.path.join(root, 'index.html'), render(state, root, refresh, os.path.getmtime(state_path)))
    return os.path.join(root, 'index.html')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('state', nargs='?')
    parser.add_argument('--refresh', type=int, default=60, help=f'auto-reload period in seconds (>= {MIN_REFRESH})')
    parser.add_argument('--step', action='append', default=[], metavar='N=STATUS[::note]')
    parser.add_argument('--fig', action='append', default=[], metavar='FILE=TITLE[::note]')
    parser.add_argument('--add-step', action='append', default=[], metavar='TEXT', help='append a step (à faire)')
    parser.add_argument('--log', metavar='TEXT')
    parser.add_argument('--warn', metavar='TEXT', help="warning banner ('' clears it)")
    parser.add_argument('--show', action='store_true', help='print task, agent, numbered steps, question and warning')
    parser.add_argument('--task', metavar='TASK', help='link the page to ~/.agents/tasks/active/TASK.md')
    parser.add_argument('--agent', metavar='NAME', help='session in charge; a change is logged as a handoff')
    parser.add_argument('--find', metavar='TASK', help='print the registered state.json of TASK')
    parser.add_argument('--ask', metavar='TEXT', help="question waiting for the user ('' clears it)")
    parser.add_argument('--all', action='store_true', help='re-render every registered page and the hub')
    parser.add_argument('--forget', action='store_true', help='remove STATE from the hub')
    args = parser.parse_args()
    if args.refresh < MIN_REFRESH:
        parser.error(f'--refresh must be at least {MIN_REFRESH} seconds')
    updates = (args.step or args.add_step or args.fig or args.log or args.task or args.agent
               or args.ask is not None or args.warn is not None)
    if args.find:
        found = [p for p in read_registry(hub_dir() / 'pages.json') if task_of(p) == args.find]
        if not found:
            sys.exit(f'no registered page for task {args.find}')
        print('\n'.join(found))
        return
    if not args.state and not args.all:
        parser.error('STATE is required unless --all')
    if (args.all or args.forget or args.show) and updates:
        parser.error('--all, --forget and --show take no update flag')
    if args.forget:
        print(update_hub(forget=os.path.realpath(args.state))[0])
        return
    if args.all:
        for path in update_hub()[1]:
            try:
                print(render_page(path, load(path), args.refresh))
            except Exception as error:  # noqa: BLE001 - one bad page must not stop the others
                print(f'warning: {path}: {error}', file=sys.stderr)
        print(update_hub()[0])
        return
    state_path = os.path.realpath(args.state)
    root = os.path.dirname(state_path)
    try:
        # Lock the read-modify-write: several commands may update the same state at once.
        with open(os.path.join(root, '.state.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load(state_path)
            if args.show:
                show(state)
                return
            changes = apply_updates(state, args, root)
            if changes:
                error = validate(state)
                if error:
                    raise ValueError(error)
                write_atomic(state_path, json.dumps(state, indent=1, ensure_ascii=False) + '\n')
    except (OSError, ValueError) as error:
        sys.exit(f'{args.state}: {error}')
    for change in changes:
        print(change)
    print(render_page(state_path, state, args.refresh))
    try:
        print(update_hub(add=state_path)[0])
    except Exception as error:  # noqa: BLE001 - the page is written; the hub is a convenience
        print(f'warning: hub not updated: {error}', file=sys.stderr)


if __name__ == '__main__':
    main()
