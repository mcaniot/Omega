#!/usr/bin/env python3
"""UserPromptSubmit + SessionStart hook: titles the session "Prénom — branche" and tells the agent its name.

The first name comes from a hash of the session id, probed past names held by
other sessions seen recently, and is remembered so resumes keep it. A title
already of the form "X — ..." keeps X (manual names win); only the branch is
refreshed. Prints nothing when the title is already right.
"""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REGISTRY = Path.home() / '.claude' / 'session-names.json'
ACTIVE_FOR = 24 * 3600  # a session unseen for a day frees its name
SEP = ' — '

NAMES = (
    'Abel Ada Aiko Alba Alix Alma Amos Ana Anouk Arlo Aster Aube Ava Axel '
    'Bao Basile Bea Bela Benji Blaise Bo Bran Brune Cael Cali Cami Cass Cleo '
    'Cosme Dana Dario Dina Dora Eda Eden Eli Elio Elsa Emil Ena Enzo Eva Ezra '
    'Fana Faro Fido Flo Gaby Gael Gia Gil Gina Hana Hugo Ida Ilan Ilia Ilo '
    'Ines Ira Iris Isa Ivo Jade Jani Jem Jo Joa Juno Kai Kali Kaya Kenzo Kira '
    'Koa Lana Lea Leni Leo Lia Lila Lino Liv Lola Lou Luca Lumi Luna Lya Mael '
    'Mali Mara Marlo Mattis Maya Mila Milo Mina Mira Mita Moana Nael Nala Nao '
    'Nate Neo Nia Nico Nils Nina Noa Noe Nola Nora Oda Odin Olga Oli Omer Ona '
    'Oren Otto Pablo Paco Pia Pim Pola Remi Rena Rio Risa Romy Rosa Ruben Ruth '
    'Sacha Sami Sana Sara Sela Siko Simi Sora Suna Taio Talia Tami Tara Tavi '
    'Teo Tia Timo Tino Toma Toni Uma Uri Vada Valo Vera Vito Wanda Wren Yael '
    'Yann Yara Yoko Yuki Yuna Yves Zack Zana Zara Zelie Zeno Zia Ziggy Zoe '
    'Zola Zuri Aria Bento Ciro Dahlia Edda Fleur Gwen Hiro Ilse Jules Kenji '
    'Ari Bruno Celia Dimi Elan Faye Gus Hanae Ivy'
).split()


def branch(cwd):
    try:
        out = subprocess.run(['git', '-C', cwd, 'branch', '--show-current'],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if out:
            return out
        # Detached HEAD: show the short commit instead of nothing.
        return subprocess.run(['git', '-C', cwd, 'rev-parse', '--short', 'HEAD'],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def claim(session, current, now, assign=True):
    """(name, name last told to the agent): kept from the title or registry, else hashed and probed past live names.

    With assign=False an unknown session gets (None, None) and nothing is written."""
    with open(REGISTRY.with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            reg = json.loads(REGISTRY.read_text())
        except (OSError, ValueError):
            reg = {}
        reg = {s: v for s, v in reg.items() if now - v.get('seen', 0) < ACTIVE_FOR}
        told = reg.get(session, {}).get('told')
        name = current or reg.get(session, {}).get('name')
        if not name and not assign:
            return None, None
        if not name:
            taken = {v['name'] for s, v in reg.items() if s != session}
            start = int(hashlib.sha256(session.encode()).hexdigest(), 16) % len(NAMES)
            # O(n) probe once per new session; falls back to the hashed name if all are taken.
            name = next((NAMES[(start + i) % len(NAMES)] for i in range(len(NAMES))
                         if NAMES[(start + i) % len(NAMES)] not in taken), NAMES[start])
        reg[session] = {'name': name, 'seen': now, 'told': name}
        tmp = REGISTRY.with_suffix('.tmp')
        tmp.write_text(json.dumps(reg))
        tmp.replace(REGISTRY)
    return name, told


def answer(event, now=None):
    """Hook output: the title on UserPromptSubmit when it changes; the name in context whenever the agent may not know it."""
    session = event.get('session_id') or ''
    if not session:
        return None
    hook = event.get('hook_event_name') or 'UserPromptSubmit'
    title = event.get('session_title') or ''
    current = title.split(SEP, 1)[0].strip() if SEP in title else ''
    # SessionStart carries no title: a fresh session may reuse an app row already
    # named by an earlier session, so leave naming to the first UserPromptSubmit.
    name, told = claim(session, current, now or time.time(), assign=hook != 'SessionStart')
    if not name:
        return None
    br = branch(event.get('cwd') or '.')
    new = f'{name}{SEP}{br}' if br else name
    out = {'hookEventName': hook}
    if hook == 'UserPromptSubmit' and new != title:
        out['sessionTitle'] = new
    # SessionStart fires on startup, resume, /clear and compaction: each may drop what the agent knew.
    if hook == 'SessionStart' or name != told:
        out['additionalContext'] = f'Ton prénom dans cette session : {name} (titre « {new} »).'
    # The desktop app ignores sessionTitle and auto-titles the first turn; only its MCP tool can fix it.
    if hook == 'UserPromptSubmit' and told is None and os.environ.get('CLAUDE_CODE_ENTRYPOINT') == 'claude-desktop':
        out['additionalContext'] += (f' Renomme la session sans bloquer le tour : charge mcp__ccd_session_mgmt__set_session_title'
                                     f' via ToolSearch dans le même message que tes premiers appels d\'outils, puis appelle-le'
                                     f' avec session_id="self", title="{new}" en parallèle des suivants (sans get_session).')
    return {'hookSpecificOutput': out} if len(out) > 1 else None


def main():
    try:
        result = answer(json.load(sys.stdin))
    except Exception:
        return  # naming must never block a prompt
    if result:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
