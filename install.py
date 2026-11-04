#!/usr/bin/env python3
"""Deploy this repository into ~/.agents, ~/.claude and ~/.codex.

Dry run by default; --apply writes. Text sources are symlinked so edits here are
live. The Rust binaries (rust/) are built with cargo and installed as real 0700
files; runtime state (tasks, evidence) also stays real files in ~/.agents. An
existing file or directory is replaced only when it is already a link or has
identical content; anything else is reported as a conflict.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
AGENTS = HOME / '.agents'
CLAUDE = HOME / '.claude'
CODEX = HOME / '.codex'

# Codex settings per agent: (model, reasoning effort, sandbox or None to inherit).
CODEX_AGENTS = {
    'architect': ('gpt-6.1-sol', 'high', 'read-only'),
    'coder': ('gpt-6-sol', 'medium', None),
    'robotics-rl': ('gpt-6.1-sol', 'medium', None),
    'scout': ('gpt-5.6-luna', 'low', 'read-only'),
}
# Cargo binary -> installed path.
BINARIES = {
    'run': AGENTS / 'tools/robot-exec/native/run',
    'local_read': AGENTS / 'tools/robot-exec/local-read',
    'shell_guard': AGENTS / 'tools/shell_guard',
    'session_name': CLAUDE / 'hooks/session_name',
}
COMMANDS = {
    'omega-run': BINARIES['run'],
    'omega-inspect': BINARIES['run'],
    'omega-prune': BINARIES['run'],
    'omega-read': BINARIES['local_read'],
    'Ω-run': BINARIES['run'],
    'Ω-inspect': BINARIES['run'],
    'Ω-prune': BINARIES['run'],
    'Ω-read': BINARIES['local_read'],
}
# Codex scans ~/.agents/skills natively; Claude sees only skills linked here.
CLAUDE_SKIP_SKILLS = {'orchestration'}


def tracked(root):
    for path in sorted(root.rglob('*')):
        parts = path.relative_to(root).parts
        if path.is_file() and not any(p.startswith('.') or p == '__pycache__' for p in parts):
            yield path


def deployable(root):
    """Sources to link: tests and golden fixtures stay in the repository."""
    for path in tracked(root):
        parts = path.relative_to(root).parts
        if not (path.name.startswith('test_') or 'fixtures' in parts):
            yield path


def links():
    yield AGENTS / 'AGENTS.md', REPO / 'AGENTS.md'
    for skill in sorted((REPO / 'skills').iterdir()):
        yield AGENTS / 'skills' / skill.name, skill
        if skill.name not in CLAUDE_SKIP_SKILLS:
            yield CLAUDE / 'skills' / skill.name, skill
    for source in deployable(REPO / 'tools'):
        yield AGENTS / 'tools' / source.relative_to(REPO / 'tools'), source
    for agent in sorted((REPO / 'agents').glob('*.md')):
        yield CLAUDE / 'agents' / agent.name, agent
    for hook in deployable(REPO / 'hooks'):
        yield CLAUDE / 'hooks' / hook.name, hook


def install_commands(apply):
    for name, target in COMMANDS.items():
        yield HOME / '.local/bin' / name, link(HOME / '.local/bin' / name, target, apply)


def same(dest, source):
    if source.is_file():
        return dest.is_file() and dest.read_bytes() == source.read_bytes()
    if not dest.is_dir():
        return False
    ours = {p.relative_to(source) for p in tracked(source)}
    theirs = {p.relative_to(dest) for p in tracked(dest)}
    return ours == theirs and all((dest / p).read_bytes() == (source / p).read_bytes() for p in ours)


def link(dest, source, apply):
    if dest.is_symlink() and Path(os.readlink(dest)) == source:
        return 'ok'
    if dest.exists() and not dest.is_symlink() and not same(dest, source):
        return 'CONFLICT (content differs, left untouched)'
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f'.{dest.name}.install-tmp')
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(source)
        if dest.is_dir() and not dest.is_symlink():
            old = dest.with_name(f'.{dest.name}.install-old')
            dest.rename(old)
            tmp.rename(dest)
            shutil.rmtree(old)
        else:
            os.replace(tmp, dest)  # atomic: live hooks never see a missing file
    return 'linked' if apply else 'would link'


def install_binaries(apply):
    """Build the Rust crate and install each binary atomically as a 0700 real file."""
    blocked = [dest for dest in BINARIES.values() if dest.is_dir() and not dest.is_symlink()]
    for dest in blocked:
        yield dest, 'CONFLICT (directory, left untouched)'
    if blocked:
        return
    if not apply:
        for dest in BINARIES.values():
            yield dest, 'would build and install'
        return
    subprocess.run(['cargo', 'build', '--release', '--locked'], cwd=REPO / 'rust', check=True)
    for name, dest in BINARIES.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f'.{dest.name}.install-tmp')
        tmp.unlink(missing_ok=True)
        shutil.copyfile(REPO / 'rust/target/release' / name, tmp)
        tmp.chmod(0o700)
        os.replace(tmp, dest)
        yield dest, 'installed'


def frontmatter(path):
    head, body = path.read_text().split('\n---\n', 1)
    fields = {}
    for key, value, folded in re.findall(r'^(\w+): *(.*)\n((?:  .*\n?)*)', head + '\n', re.M):
        fields[key] = ' '.join(folded.split()) if value in ('>-', '>', '|', '|-') else value
    return fields, body.strip()


def codex_agent(path):
    fields, body = frontmatter(path)
    model, effort, sandbox = CODEX_AGENTS[fields['name']]
    lines = [f'model = "{model}"', f'model_reasoning_effort = "{effort}"']
    if sandbox:
        lines.append(f'sandbox_mode = "{sandbox}"')
    lines.append(f'developer_instructions = {json.dumps(body, ensure_ascii=False)}')
    return '\n'.join(lines) + '\n'


def write_codex_agents(apply):
    for agent in sorted((REPO / 'agents').glob('*.md')):
        dest = CODEX / 'agents' / f'{agent.stem}.toml'
        text = codex_agent(agent)
        if dest.is_file() and dest.read_text() == text:
            yield dest, 'ok'
            continue
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        yield dest, 'written' if apply else 'would write'
    for stale in sorted((CODEX / 'agents').glob('*.toml')):
        if not (REPO / 'agents' / f'{stale.stem}.md').exists():
            yield stale, 'STALE (not in repo; remove it and its [agents.*] entry in config.toml)'


def budget():
    """Bytes injected into every session before any work starts."""
    rows = [('AGENTS.md', (REPO / 'AGENTS.md').stat().st_size)]
    for agent in sorted((REPO / 'agents').glob('*.md')):
        rows.append((f'agent {agent.stem} description', len(frontmatter(agent)[0]['description'].encode())))
    for skill in sorted((REPO / 'skills').iterdir()):
        rows.append((f'skill {skill.name} description', len(frontmatter(skill / 'SKILL.md')[0]['description'].encode())))
    for name, size in rows:
        print(f'{size:6d}  {name}')
    print(f'{sum(s for _, s in rows):6d}  total bytes (Claude omits skill: {", ".join(sorted(CLAUDE_SKIP_SKILLS))})')


def main():
    parser = argparse.ArgumentParser(description='Deploy this repository into ~/.agents, ~/.claude and ~/.codex.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='write changes (default is a dry run)')
    mode.add_argument('--budget', action='store_true', help='print always-loaded context size in bytes')
    args = parser.parse_args()
    if args.budget:
        return budget()
    results = [(dest, link(dest, source, args.apply)) for dest, source in links()]
    results += list(install_binaries(args.apply))
    results += list(install_commands(args.apply))
    results += list(write_codex_agents(args.apply))
    for dest, status in results:
        if status != 'ok':
            print(f'{status:12s} {dest}')
    problems = [s for _, s in results if s.startswith(('CONFLICT', 'STALE'))]
    print(f'{len(results)} targets, {len(problems)} need attention' + ('' if args.apply else ' (dry run)'))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
