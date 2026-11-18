#!/usr/bin/env python3
"""Render a static progress page (index.html) next to a JSON state file.

Usage: make_page.py STATE.json [--refresh SECONDS]

Each render also registers the page in one hub, $PROGRESS_HUB/index.html (default ~/.agents/progress),
which lists every tracked page in a single tab: open tasks first, then finished ones. A page leaves
the hub when its state file is deleted or moved.

The page opens from file:// without a server; its labels are French. State keys (all text is plain;
`x` renders as code):
  title, subtitle?, summary?, warning?,
  steps: [{step, status: fait|en cours|à faire|bloqué, note?, date?}],
  figures: [{file, title, note?}]   (relative to the state directory, oldest first; .mp4/.webm → video),
  log?: [{date, text}]              (oldest first; the 5 most recent are shown),
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

# White text on each colour stays at or above WCAG AA (4.5:1).
BADGES = {'fait': '#2e7d32', 'en cours': '#b45309', 'à faire': '#616161', 'bloqué': '#c62828'}
VIDEO = ('.mp4', '.webm')
MIN_REFRESH = 5
HUB_REFRESH = 60
CSS = """
body{font-family:system-ui,sans-serif;max-width:1180px;margin:2em auto;padding:0 1em;color:#222;line-height:1.45}
h1{margin-bottom:.1em} .sub{color:#666;margin-top:0} table{border-collapse:collapse;width:100%}
td{border-bottom:1px solid #ddd;padding:.45em .6em;vertical-align:top} .d{color:#666;white-space:nowrap}
.b{color:#fff;border-radius:4px;padding:.1em .5em;font-size:.85em;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(450px,1fr));gap:1em}
figure{margin:0;border:1px solid #ddd;border-radius:6px;overflow:hidden}
img,video{width:100%;height:auto;display:block} figcaption{padding:.5em .7em;font-size:.92em}
.warn{background:#fff3e0;border-left:4px solid #ef6c00;padding:.6em .9em}
.card{display:grid;grid-template-columns:240px 1fr;gap:1em;border:1px solid #ddd;border-radius:6px;padding:.8em;margin:.8em 0}
.card img{border-radius:4px} .card h3{margin:0 0 .2em}
"""
# Reload only while visible, not paused and no video is playing (WCAG 2.2.1: the user can stop it).
# The browser restores the scroll position on reload; open <details> are restored here.
RELOAD_JS = """
const K='progress-page:'+location.pathname, box=document.getElementById('auto');
box.checked=localStorage.getItem(K+':paused')!=='1';
box.onchange=()=>localStorage.setItem(K+':paused',box.checked?'0':'1');
document.querySelectorAll('details[id]').forEach(d=>{
  d.open=sessionStorage.getItem(K+':'+d.id)==='1';
  d.ontoggle=()=>sessionStorage.setItem(K+':'+d.id,d.open?'1':'0');});
const busy=()=>document.fullscreenElement||[...document.querySelectorAll('video')].some(v=>!v.paused);
setInterval(()=>{if(box.checked&&document.visibilityState==='visible'&&!busy())location.reload();},REFRESH*1000);
"""


def text(value):
    """Escape plain text; `code` spans become <code>."""
    return re.sub(r'`([^`]+)`', r'<code>\1</code>', html.escape(str(value)))


def validate(state):
    """Return a readable error for a malformed state, or None."""
    if not isinstance(state, dict) or not isinstance(state.get('title'), str):
        return 'state must be an object with a string "title"'
    steps = state.get('steps')
    if not isinstance(steps, list):
        return '"steps" must be a list'
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or 'step' not in step or step.get('status') not in BADGES:
            return f'steps[{i}] needs "step" and "status" among {", ".join(BADGES)}'
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


def figure(root, fig, eager):
    # Quoting keeps the URL relative: '#', '?', '%' stay in the name and 'javascript:' is not a scheme.
    src = html.escape(urllib.parse.quote(fig['file']), quote=True)
    if is_video(fig['file']):
        # No link around a video: clicks on its controls would open the file.
        media = f'<video src="{src}" controls muted loop preload="metadata"></video>'
    else:
        size = png_size(os.path.join(root, fig['file']))
        dims = f' width="{size[0]}" height="{size[1]}"' if size else ''
        lazy = '' if eager else ' loading="lazy"'
        media = f'<a href="{src}"><img src="{src}" alt="{html.escape(fig["title"], quote=True)}"{dims}{lazy}></a>'
    note = f'<br>{text(fig["note"])}' if fig.get('note') else ''
    return f'<figure>{media}<figcaption><b>{text(fig["title"])}</b>{note}</figcaption></figure>'


def document(title, body, refresh):
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body>\n{body}\n'
            f'<script>const REFRESH={int(refresh)};{RELOAD_JS}</script>\n</body></html>\n')


def header(title, subtitle=''):
    sub = ' · '.join(filter(None, [subtitle, f'mise à jour {time.strftime("%d/%m/%Y %H:%M")}',
                                   '<label><input type="checkbox" id="auto"> rechargement auto</label>']))
    return f'<h1>{text(title)}</h1><p class="sub">{sub}</p>'


def render(state, root, refresh):
    rows = ''.join(
        f'<tr><td>{text(s["step"])}</td>'
        f'<td><span class="b" style="background:{BADGES[s["status"]]}">{text(s["status"])}</span></td>'
        f'<td>{text(s.get("note", ""))}</td><td class="d">{text(s.get("date", ""))}</td></tr>'
        for s in state['steps'])
    figures = list(reversed(state.get('figures', [])))
    grid = ''.join(figure(root, f, i == 0) for i, f in enumerate(figures)) or '<p><i>Premiers rendus en préparation.</i></p>'
    parts = [header(state['title'], text(state.get('subtitle', '')))]
    if state.get('summary'):
        parts.append(f'<p>{text(state["summary"])}</p>')
    if state.get('warning'):
        parts.append(f'<p class="warn">{text(state["warning"])}</p>')
    parts.append(f'<h2>Avancement</h2><table>{rows}</table>')
    log = state.get('log', [])[-5:]
    if log:
        items = ''.join(f'<li><span class="d">{text(e["date"])}</span> — {text(e["text"])}</li>' for e in reversed(log))
        parts.append(f'<h2>Derniers changements</h2><ul>{items}</ul>')
    parts.append(f'<h2>Visuels (les plus récents d\'abord)</h2><div class="grid">{grid}</div>')
    for section in state.get('sections', []):
        items = ''.join(f'<li>{text(i)}</li>' for i in section['items'])
        parts.append(f'<h2>{text(section["title"])}</h2><ul>{items}</ul>')
    return document(state['title'], '\n'.join(parts), refresh)


def write_atomic(path, content):
    """Replace path in one step: a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def hub_card(state_path):
    root = pathlib.Path(state_path).parent
    with open(state_path, encoding='utf-8') as f:
        state = json.load(f)
    error = validate(state)
    if error:
        raise ValueError(error)
    steps = state['steps']
    counts = ' '.join(
        f'<span class="b" style="background:{color}">{sum(s["status"] == name for s in steps)} {name}</span>'
        for name, color in BADGES.items() if any(s['status'] == name for s in steps))
    current = ''.join(f'<li>{text(s["step"])}</li>' for s in steps if s['status'] in ('en cours', 'bloqué'))
    images = [f for f in state.get('figures', []) if not is_video(f['file'])]
    thumb = ''
    if images:
        uri = html.escape((root / images[-1]['file']).as_uri(), quote=True)
        thumb = f'<img src="{uri}" alt="" loading="lazy">'
    warning = f'<p class="warn">{text(state["warning"])}</p>' if state.get('warning') else ''
    updated = time.strftime('%d/%m %H:%M', time.localtime(os.path.getmtime(root / 'index.html')))
    page = html.escape((root / 'index.html').as_uri(), quote=True)
    card = (f'<div class="card"><a href="{page}">{thumb}</a><div><h3><a href="{page}">{text(state["title"])}</a></h3>'
            f'<p class="sub">mise à jour {updated} · {counts}</p>{warning}'
            + (f'<ul>{current}</ul>' if current else '') + '</div></div>')
    return bool(steps) and all(s['status'] == 'fait' for s in steps), card


def read_registry(path):
    try:
        paths = json.loads(path.read_text())
    except (OSError, ValueError):
        return []  # missing or corrupt: rebuilt from the pages rendered from now on
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def update_hub(state_path):
    """Register state_path and re-render the hub from every registered page that still exists."""
    hub = pathlib.Path(os.environ.get('PROGRESS_HUB') or pathlib.Path.home() / '.agents/progress').expanduser().resolve()
    hub.mkdir(parents=True, exist_ok=True)
    with open(hub / 'pages.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # several sessions may render at once
        registry = hub / 'pages.json'
        paths = [p for p in dict.fromkeys([state_path, *read_registry(registry)]) if os.path.exists(p)]
        write_atomic(str(registry), json.dumps(paths, indent=1) + '\n')
        cards = []
        for p in paths:
            try:
                cards.append((os.path.getmtime(p),) + hub_card(p))
            except Exception as error:  # noqa: BLE001 - one broken page must not hide the others
                cards.append((0, False, f'<div class="card"><div></div><p>{text(p)} : {text(error)}</p></div>'))
        cards.sort(key=lambda c: -c[0])
        active = ''.join(c[2] for c in cards if not c[1]) or '<p><i>Aucune tâche en cours.</i></p>'
        done = ''.join(c[2] for c in cards if c[1])
        body = (header('Suivi des tâches') + f'<h2>En cours</h2>{active}'
                + (f'<details id="done"><summary><h2 style="display:inline">Terminées</h2></summary>{done}</details>'
                   if done else ''))
        write_atomic(str(hub / 'index.html'), document('Suivi des tâches', body, HUB_REFRESH))
    return hub / 'index.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('state')
    parser.add_argument('--refresh', type=int, default=60, help=f'auto-reload period in seconds (>= {MIN_REFRESH})')
    args = parser.parse_args()
    if args.refresh < MIN_REFRESH:
        parser.error(f'--refresh must be at least {MIN_REFRESH} seconds')
    state_path = os.path.abspath(args.state)
    root = os.path.dirname(state_path)
    try:
        with open(state_path, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError) as error:
        sys.exit(f'{args.state}: {error}')
    error = validate(state)
    if error:
        sys.exit(f'{args.state}: {error}')
    write_atomic(os.path.join(root, 'index.html'), render(state, root, args.refresh))
    print(os.path.join(root, 'index.html'))
    try:
        print(update_hub(state_path))
    except Exception as error:  # noqa: BLE001 - the page is written; the hub is a convenience
        print(f'warning: hub not updated: {error}', file=sys.stderr)


if __name__ == '__main__':
    main()
