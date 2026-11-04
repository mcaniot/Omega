#!/usr/bin/env python3
"""Require the evidence wrapper without granting native execution permission."""
import json
import os
import re
import shlex
import sys

WRAPPER = os.path.expanduser('~/.agents/tools/run.py')
NATIVE = os.path.expanduser('~/.agents/tools/robot-exec/native/run')
LOCAL_READ = os.path.expanduser('~/.agents/tools/robot-exec/local-read')
PYTHONS = {'python3', sys.executable, os.path.realpath(sys.executable)}
for directory in os.get_exec_path():
    candidate = os.path.join(directory, 'python3')
    if os.path.exists(candidate) and os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
        PYTHONS.add(candidate)
        break
GUIDANCE = (
    'Shell output must use the evidence wrapper. Invoke canonical shlex.join(argv): '
    f'python3 {WRAPPER} run [--cwd DIR] [--timeout S] -- COMMAND ARGS (or inspect ARTIFACT). '
    "For shell syntax pass one single-quoted --shell 'SCRIPT' argument; no outer operators, "
    'redirection or cd (use --cwd).'
)

# Every command starts with the wrapper, so native prefix rules such as Bash(git push *)
# never match; the policy is applied here to the wrapped text instead. Matching is
# conservative (anywhere in the text): a false "ask" costs one prompt.
GIT = r'\bgit(?:\s+-[Cc]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+'
ARGS = r'(?:[^\s;&|]+\s+)*'  # arguments of the same simple command
DENY = [
    (r'\bsudo\b|\bsu\s+-', 'privilege escalation'),
    (GIT + r'push\s+' + ARGS + r'(?:-f|--force)(?=\s|$)', 'git force push'),
    (r'\brm\s+' + ARGS + r'-[a-zA-Z]*(?:[rR][a-zA-Z]*f|f[a-zA-Z]*[rR])', 'rm -rf'),
    (r'\bmkfs(?:\.\w+)?\b|\bdd\s+' + ARGS + r'of=/dev/', 'raw device write'),
]
ASK = [
    (r'(?:^|[\s\'"/])(?:\.ssh|\.aws|\.gnupg)(?:/|[\s\'"$])'
     r'|(?:^|[\s\'"/])\.env(?:[.\s\'"/]|$)'
     r'|(?:^|[\s\'"/])(?:id_rsa|id_ed25519|credentials)(?:[.\s\'"/]|$)',
     'sensitive credential access'),
    (GIT + r'(?:push|commit|rebase|filter-branch)\b', 'git history or remote change'),
    (GIT + r'(?:reset\s+--hard|clean\s+-\w*f|branch\s+-D|checkout\s+--\s|restore\b)', 'git destructive change'),
    (r'\brm\s+' + ARGS + r'(?:-[a-zA-Z]*[rR]|--recursive)\b', 'recursive delete'),
    (r'\b(?:ssh|scp|sftp|rsync)\b', 'remote host access (robot or server)'),
    (r'\bcurl\s+' + ARGS + r'(?:-T|--upload-file|-d|--data[\w-]*|-F|--form|-X\s*(?:POST|PUT|PATCH|DELETE))\b'
     r'|\bwget\s+' + ARGS + r'--post', 'network upload'),
    (r'\bros2\s+(?:topic\s+pub|service\s+call|action\s+send_goal|param\s+set|lifecycle\s+set|launch|run|bag\s+play)\b'
     r'|\bz_(?:put|pub|delete|queryable)\b', 'possible robot actuation'),
    (r'(?:^|[\s;&|(`])(?:env|printenv|set)\s*(?:$|[;&|)`])|\bprintenv\b|\bexport\s+-p\b'
     r'|(?:^|[\s/\'"=])(?:\.env(?:\.\w+)?|auth\.json|[\w.-]*\.key|id_(?:rsa|ed25519))\b', 'possible secret exposure'),
]


def decision(kind, reason):
    return {'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': kind,
        'permissionDecisionReason': reason,
    }}


def denied(reason):
    return decision('deny', reason + ' ' + GUIDANCE)


def inner_text(argv):
    """Return the command the wrapper will execute, as text for policy matching."""
    if argv[2] != 'run':
        return ''
    if '--shell' in argv:
        index = argv.index('--shell')
        if index + 1 < len(argv):
            return argv[index + 1]
    if '--' in argv:
        return shlex.join(argv[argv.index('--') + 1:])
    return shlex.join(argv[3:])


def policy(text):
    for pattern, label in DENY:
        if re.search(pattern, text, re.M):
            return decision('deny', f'Blocked by agent policy: {label}. Ask the user to run it themselves.')
    for pattern, label in ASK:
        if re.search(pattern, text, re.M):
            return decision('ask', f'Agent policy requires explicit user approval: {label}.')
    return {}


def evaluate(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('tool_name'), str):
        return denied('Invalid hook input.')
    if payload['tool_name'] not in ('Bash', 'exec_command', 'shell_command'):
        return {}
    tool_input = payload.get('tool_input')
    command = tool_input.get('command', tool_input.get('cmd')) if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return denied('Missing shell command.')
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return denied('Malformed shell quoting.')
    if not argv or command.strip() != shlex.join(argv):
        return denied('Unwrapped or noncanonical shell command.')
    if argv[0] == LOCAL_READ:
        sizes = {'status': (2,), 'files': (2,), 'text': (3, 4, 5), 'find': (4,)}
        if len(argv) < 2 or len(argv) not in sizes.get(argv[1], ()):
            return denied('Unsupported read capability or arguments.')
        # The compiled launcher validates roots, sensitive paths and numeric ranges.
        # Empty output preserves native sandbox/permission checks; never grant allow.
        return {}
    if argv[0] == NATIVE:
        argv = [sys.executable, WRAPPER, *argv[1:]]
    # Canonical quoting makes metacharacters literal arguments to the wrapper.
    # Never return "allow": native sandbox and approval checks still apply.
    if (len(argv) < 3 or argv[0] not in PYTHONS or argv[1] != WRAPPER
            or argv[2] not in ('run', 'inspect', 'prune')
            ):
        return denied('Unwrapped or noncanonical shell command.')
    if any(arg.startswith('=') for arg in argv):
        # zsh EQUALS expansion rewrites an unquoted "=cmd" into a path.
        return denied('Argument starting with "=" is expanded by zsh.')
    if argv[2] == 'prune' and '--apply' in argv:
        return decision('ask', 'Deleting old evidence requires explicit user approval.')
    return policy(inner_text(argv))


def main():
    # Fail closed: any internal error denies instead of letting the command through.
    try:
        result = evaluate(json.load(sys.stdin))
    except (ValueError, UnicodeError):
        result = denied('Invalid hook JSON.')
    except BaseException as exc:  # a guard must never fail open
        result = denied(f'Guard internal error ({type(exc).__name__}).')
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
