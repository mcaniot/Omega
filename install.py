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
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
AGENTS = HOME / '.agents'
CLAUDE = HOME / '.claude'
CODEX = HOME / '.codex'

# Codex settings per agent: (model, reasoning effort, sandbox or None to inherit).
# Codex does not let an agent file override the parent sandbox; read-only roles
# are also enforced by their instructions.
CODEX_AGENTS = {
    'architect': ('gpt-6.1-sol', 'high', 'read-only'),
    'coder': ('gpt-6.1-terra', 'medium', None),
    'scout': ('gpt-6-luna', 'low', 'read-only'),
}
# Keys of agent files written by this installer; used to recognize stale ones.
CODEX_AGENT_KEYS = {'name', 'description', 'model', 'model_reasoning_effort', 'sandbox_mode',
                    'developer_instructions'}
# Cargo binary -> installed path.
BINARIES = {
    'run': AGENTS / 'tools/robot-exec/native/run',
    'local_read': AGENTS / 'tools/robot-exec/local-read',
    'shell_guard': AGENTS / 'tools/shell_guard',
    'context_guard': AGENTS / 'tools/context_guard',
    'optimize': AGENTS / 'tools/optimize',
    'session_name': CLAUDE / 'hooks/session_name',
}
COMMANDS = {
    'omega': BINARIES['optimize'],
    'omega-optimize': BINARIES['optimize'],
    'Ω-optimize': BINARIES['optimize'],
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


def response_links():
    yield CODEX / 'instructions.md', REPO / 'prompts/codex.md'
    yield CLAUDE / 'output-styles/final-only.md', REPO / 'output-styles/final-only.md'


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
    yield CODEX / 'AGENTS.md', AGENTS / 'AGENTS.md'
    yield from response_links()
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


def install_commands(apply, names=None):
    for name, target in COMMANDS.items():
        if names is not None and name not in names:
            continue
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


def remove_stale_links(apply):
    """Remove our links whose repository source was deleted (removed agents or skills)."""
    for directory in (CLAUDE / 'agents', CLAUDE / 'skills', AGENTS / 'skills'):
        if not directory.is_dir():
            continue
        for dest in sorted(directory.iterdir()):
            if dest.is_symlink() and not dest.exists() and Path(os.readlink(dest)).is_relative_to(REPO):
                if apply:
                    dest.unlink()
                yield dest, 'removed stale link' if apply else 'would remove stale link'


def install_binaries(apply, names=None):
    """Build the Rust crate and install each binary atomically as a 0700 real file."""
    binaries = BINARIES if names is None else {name: BINARIES[name] for name in names}
    blocked = [dest for dest in binaries.values() if dest.is_dir() and not dest.is_symlink()]
    for dest in blocked:
        yield dest, 'CONFLICT (directory, left untouched)'
    if blocked:
        return
    if not apply:
        for dest in binaries.values():
            yield dest, 'would build and install'
        return
    command = ['cargo', 'build', '--release', '--locked']
    if names is not None:
        for name in names:
            command += ['--bin', name]
    subprocess.run(command, cwd=REPO / 'rust', check=True)
    for name, dest in binaries.items():
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
    lines = [f'name = "{fields["name"]}"',
             f'description = {json.dumps(fields["description"], ensure_ascii=False)}',
             f'model = "{model}"', f'model_reasoning_effort = "{effort}"']
    if sandbox:
        lines.append(f'sandbox_mode = "{sandbox}"')
    lines.append(f'developer_instructions = {json.dumps(body, ensure_ascii=False)}')
    return '\n'.join(lines) + '\n'


def legacy_agent_tables(text, names):
    """Drop `[agents.NAME]` tables holding only description/config_file = "agents/NAME.toml".

    Standalone agent files carry their own name and description; these legacy
    registrations would duplicate or resurrect roles. Other tables are kept verbatim.
    """
    lines = text.splitlines(keepends=True)
    kept, index, removed = [], 0, []
    while index < len(lines):
        match = re.match(r'\[agents\.([\w-]+)\]\s*$', lines[index])
        end = index + 1
        while end < len(lines) and not lines[end].lstrip().startswith('['):
            end += 1
        if match and match.group(1) in names:
            table = tomllib.loads(''.join(lines[index:end]))['agents'][match.group(1)]
            if set(table) <= {'description', 'config_file'} and \
                    table.get('config_file') in (None, f'agents/{match.group(1)}.toml'):
                removed.append(match.group(1))
                index = end
                continue
        kept.extend(lines[index:end])
        index = end
    return ''.join(kept), removed


def write_codex_agents(apply):
    names = set()
    for agent in sorted((REPO / 'agents').glob('*.md')):
        names.add(agent.stem)
        dest = CODEX / 'agents' / f'{agent.stem}.toml'
        text = codex_agent(agent)
        if dest.is_file() and dest.read_text() == text:
            yield dest, 'ok'
            continue
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        yield dest, 'written' if apply else 'would write'
    stale = []
    for path in sorted((CODEX / 'agents').glob('*.toml')):
        if path.stem in names:
            continue
        if set(tomllib.loads(path.read_text())) <= CODEX_AGENT_KEYS:
            stale.append(path.stem)
            if apply:
                path.unlink()
            yield path, 'removed stale agent' if apply else 'would remove stale agent'
        else:
            yield path, 'unmanaged (left untouched)'
    config = CODEX / 'config.toml'
    if config.exists():
        text, removed = legacy_agent_tables(config.read_text(), names | set(stale))
        if removed:
            tomllib.loads(text)  # never write an unparsable config
            if apply:
                target = config.resolve()
                tmp = target.with_name(f'.{target.name}.install-tmp')
                with tmp.open('w', encoding='utf-8') as stream:
                    os.chmod(tmp, target.stat().st_mode & 0o777)
                    stream.write(text)
                os.replace(tmp, target)
            yield config, ('removed' if apply else 'would remove') + f' legacy [agents.{",".join(removed)}]'


def write_response_settings(apply):
    """Select versioned response instructions without resetting user settings."""
    codex = CODEX / 'config.toml'
    codex_text = codex.read_text() if codex.exists() else ''
    config = tomllib.loads(codex_text)
    instructions = str(CODEX / 'instructions.md')
    if config.get('model_instructions_file') not in (None, instructions):
        yield codex, 'CONFLICT (model_instructions_file already configured, left untouched)'
        return
    claude = CLAUDE / 'settings.json'
    claude_text = claude.read_text() if claude.exists() else '{}\n'
    settings = json.loads(claude_text)
    updates = []
    if config.get('model_instructions_file') != instructions:
        updates.append((codex, f'model_instructions_file = {json.dumps(instructions)}\n' + codex_text))
    if settings.get('outputStyle') != 'Final only':
        settings['outputStyle'] = 'Final only'
        updates.append((claude, json.dumps(settings, indent=2, ensure_ascii=False) + '\n'))
    for dest, text in updates:
        if apply:
            target = dest.resolve()  # preserve any user-managed config symlink
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f'.{target.name}.install-tmp')
            with tmp.open('w', encoding='utf-8') as stream:
                os.chmod(tmp, target.stat().st_mode & 0o777 if target.exists() else 0o600)
                stream.write(text)
            os.replace(tmp, target)
        yield dest, 'written' if apply else 'would write'


def write_context_settings(apply):
    """Add only our Read hook; preserve third-party hooks, permissions and symlinks."""
    dest = CLAUDE / 'settings.json'
    settings = json.loads(dest.read_text()) if dest.exists() else {}
    groups = settings.setdefault('hooks', {}).setdefault('PreToolUse', [])
    command = str(BINARIES['context_guard'])
    for group in groups:
        if group.get('matcher') in ('Read', '*') and any(
                hook.get('type') == 'command' and hook.get('command') == command
                for hook in group.get('hooks', [])):
            return
    groups.append({'matcher': 'Read', 'hooks': [{'type': 'command', 'command': command}]})
    if apply:
        target = dest.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f'.{target.name}.install-tmp')
        with tmp.open('w', encoding='utf-8') as stream:
            os.chmod(tmp, target.stat().st_mode & 0o777 if target.exists() else 0o600)
            stream.write(json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
        os.replace(tmp, target)
    yield dest, 'written' if apply else 'would write'


def budget():
    """Bytes injected into every session before any work starts."""
    rows = [('AGENTS.md', (REPO / 'AGENTS.md').stat().st_size)]
    rows += [(str(path.relative_to(REPO)), path.stat().st_size)
             for path in (REPO / 'prompts/codex.md', REPO / 'output-styles/final-only.md')]
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
    parser.add_argument('--responses-only', action='store_true', help='deploy only response instructions and settings')
    parser.add_argument('--context-only', action='store_true', help='deploy only the Claude Read guard and its hook')
    args = parser.parse_args()
    if args.context_only:
        if args.responses_only or args.budget:
            parser.error('--context-only cannot be combined with --responses-only or --budget')
        # Parse settings before building/installing anything; malformed settings stay untouched.
        list(write_context_settings(False))
        results = list(install_binaries(args.apply, names=('context_guard', 'optimize', 'shell_guard')))
        if not any(status.startswith('CONFLICT') for _, status in results):
            results += list(write_context_settings(args.apply))
            results += list(install_commands(args.apply, names=('omega', 'omega-optimize', 'Ω-optimize')))
        for dest, status in results:
            print(f'{status:12s} {dest}')
        return int(any(status.startswith('CONFLICT') for _, status in results))
    if args.budget:
        return budget()
    sources = response_links() if args.responses_only else links()
    results = [(dest, link(dest, source, args.apply)) for dest, source in sources]
    if not any(status.startswith('CONFLICT') for _, status in results):
        results += list(write_response_settings(args.apply))
    if not args.responses_only:
        results += list(remove_stale_links(args.apply))
        results += list(install_binaries(args.apply))
        results += list(write_context_settings(args.apply))
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
