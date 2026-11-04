"""Golden-output tests for the session_name hook.

OMEGA_SESSION_NAME_BINARY selects the hook under test (default: the deployed
binary). Event sequences run against private HOME/registry directories with an
injected clock (SESSION_NAMES_NOW); the exit code, stdout and raw registry text
after every event must equal fixtures/session_name_golden.json.gz, recorded from
the original Python hook.
"""
import gzip
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BINARY = os.environ.get('OMEGA_SESSION_NAME_BINARY', str(Path.home() / '.claude/hooks/session_name'))
GOLDEN = Path(__file__).with_name('fixtures') / 'session_name_golden.json.gz'
T0 = 1_700_000_000.0
DAY = 24 * 3600


def git(cwd, *args):
    """Fixed identity and dates keep commit hashes (shown for detached HEADs) reproducible."""
    env = {**os.environ, 'GIT_AUTHOR_DATE': '2023-11-14T22:13:20Z', 'GIT_COMMITTER_DATE': '2023-11-14T22:13:20Z'}
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', *args],
                   cwd=cwd, check=True, capture_output=True, env=env)


class Side:
    """One hook invocation context with its own HOME and registry."""

    def __init__(self, root, name):
        self.home = Path(root) / name
        (self.home / '.claude').mkdir(parents=True)
        self.registry = self.home / '.claude' / 'session-names.json'

    def run(self, raw, now, env_extra=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_ENTRYPOINT'}
        env.update(HOME=str(self.home), SESSION_NAMES_NOW=repr(now), SESSION_NAMES_REGISTRY=str(self.registry))
        env.update(env_extra or {})
        done = subprocess.run([BINARY], input=raw, env=env, cwd=cwd, capture_output=True, timeout=30)
        return done.returncode, done.stdout

    def registry_text(self):
        return self.registry.read_text() if self.registry.exists() else None

    def seed(self, text):
        self.registry.write_text(text)


class SessionNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.access(BINARY, os.X_OK):
            raise unittest.SkipTest(f'missing executable {BINARY}')
        with gzip.open(GOLDEN, 'rt', encoding='utf-8') as handle:
            cls.golden = json.load(handle)
        cls.root = tempfile.mkdtemp(prefix='sn-golden-')
        root = Path(cls.root)
        cls.plain = root / 'plain'
        cls.plain.mkdir()
        (cls.plain / 'sub').mkdir()
        git(cls.plain, 'init', '-q', '-b', 'main')
        git(cls.plain, 'commit', '-q', '--allow-empty', '-m', 'one')
        cls.feature = root / 'feature'
        cls.feature.mkdir()
        git(cls.feature, 'init', '-q', '-b', 'feat/é-ünï')
        git(cls.feature, 'commit', '-q', '--allow-empty', '-m', 'one')
        cls.detached = root / 'detached'
        cls.detached.mkdir()
        git(cls.detached, 'init', '-q', '-b', 'main')
        git(cls.detached, 'commit', '-q', '--allow-empty', '-m', 'one')
        git(cls.detached, 'commit', '-q', '--allow-empty', '-m', 'two')
        git(cls.detached, 'checkout', '-q', '--detach', 'HEAD~1')
        cls.worktree = root / 'wt'
        git(cls.plain, 'worktree', 'add', '-q', '-b', 'wt-branch', str(cls.worktree))
        cls.unborn = root / 'unborn'
        cls.unborn.mkdir()
        git(cls.unborn, 'init', '-q', '-b', 'fresh')
        cls.nonrepo = root / 'nonrepo'
        cls.nonrepo.mkdir()
        cls.file = root / 'afile'
        cls.file.write_text('x')
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.step = 0

    def side(self):
        SessionNameTest.counter += 1
        return Side(Path(self.root) / f'case{SessionNameTest.counter}', 'hook')

    def observe(self, side, raw, now, env_extra=None, cwd=None):
        """Exit code, stdout and registry text after one event."""
        code, out = side.run(raw, now, env_extra, cwd)
        return [code, out.decode('utf-8', 'surrogateescape'), side.registry_text()]

    def check(self, side, raw, now, env_extra=None, cwd=None):
        """Run one event and compare everything observable with the recorded reference."""
        key = f'{self._testMethodName}#{self.step}'
        self.step += 1
        observed = self.observe(side, raw, now, env_extra, cwd)
        self.assertEqual(observed, self.golden[key], f'{key}: {raw!r}')
        return observed[1].encode('utf-8', 'surrogateescape')

    def seq(self, events, seed=None, env_extra=None, cwd=None):
        """events: dicts or (dict|bytes, now) pairs run on one registry; returns the stdout list."""
        side = self.side()
        if seed is not None:
            side.seed(seed)
        outs = []
        for i, ev in enumerate(events):
            now = T0 + i
            if isinstance(ev, tuple):
                ev, now = ev
            raw = ev if isinstance(ev, bytes) else json.dumps(ev, ensure_ascii=False).encode()
            outs.append(self.check(side, raw, now, env_extra, cwd))
        return outs

    @staticmethod
    def ev(session='s1', hook='UserPromptSubmit', title=None, cwd=None, **extra):
        e = {'session_id': session, 'hook_event_name': hook, **extra}
        if title is not None:
            e['session_title'] = title
        if cwd is not None:
            e['cwd'] = str(cwd)
        return e

    def test_fresh_resume_and_title_cycle(self):
        outs = self.seq([self.ev(cwd=self.plain)])
        title = json.loads(outs[0])['hookSpecificOutput']['sessionTitle']
        self.assertTrue(title.endswith(' — main'))
        outs = self.seq([self.ev(cwd=self.plain),
                         self.ev(cwd=self.plain, title=title),  # title already correct: nothing
                         self.ev(cwd=self.plain, title=title),
                         self.ev(cwd=self.plain, hook='SessionStart'),
                         self.ev(session='other', cwd=self.plain, title=title)])
        self.assertEqual(outs[2], b'')

    def test_manual_title(self):
        self.seq([self.ev(title='Bob — x', cwd=self.plain), self.ev(title='Bob — main', cwd=self.plain),
                  self.ev(title=' — x', cwd=self.plain), self.ev(title=' Bob\u001f — x', cwd=self.plain),
                  self.ev(title='no separator', cwd=self.plain), self.ev(title='A — B — C', cwd=self.plain)])

    def test_session_start(self):
        self.seq([self.ev(hook='SessionStart', cwd=self.plain),  # unknown: nothing, no write
                  self.ev(hook='UserPromptSubmit', cwd=self.plain),
                  self.ev(hook='SessionStart', cwd=self.plain),
                  self.ev(hook='SessionStart', title='Zed — q', cwd=self.plain),
                  self.ev(hook='Other', cwd=self.plain),
                  self.ev(hook='', cwd=self.plain)])

    def test_many_active_sessions(self):
        events = [self.ev(session=f'sess-{i}', cwd=self.nonrepo) for i in range(40)]
        self.seq(events)

    def test_all_names_taken(self):
        names = self.golden['names']
        reg = {f'taken-{i}': {'name': n, 'seen': T0 - 10, 'told': n} for i, n in enumerate(names)}
        self.seq([self.ev(session='newbie', cwd=self.nonrepo), self.ev(session='newbie', cwd=self.nonrepo)],
                 seed=json.dumps(reg))
        part = {f'taken-{i}': {'name': n, 'seen': T0 - 10, 'told': n} for i, n in enumerate(names[:-1])}
        self.seq([self.ev(session=f'n{i}', cwd=self.nonrepo) for i in range(5)], seed=json.dumps(part))
        for i in range(60):  # exercise many hash starts incl. the probe wrap-around
            self.seq([self.ev(session=f'wrap-{i}', cwd=self.nonrepo)], seed=json.dumps(part))

    def test_expired_entries(self):
        reg = {'old': {'name': 'Abel', 'seen': T0 - DAY - 1, 'told': 'Abel'},
               'edge': {'name': 'Ada', 'seen': T0 - DAY, 'told': 'Ada'},
               'fresh': {'name': 'Aiko', 'seen': T0 - DAY + 1, 'told': 'Aiko'},
               'int': {'name': 'Alba', 'seen': int(T0) - 5, 'told': 'Alba'},
               'noseen': {'name': 'Alix', 'told': 'Alix'},
               'bool': {'name': 'Alma', 'seen': True}}
        self.seq([self.ev(session='old', cwd=self.nonrepo), self.ev(session='fresh', cwd=self.nonrepo),
                  self.ev(session='int', cwd=self.nonrepo), self.ev(session='x', cwd=self.nonrepo),
                  self.ev(session='noseen', cwd=self.nonrepo)], seed=json.dumps(reg))

    def test_foreign_entries_kept_and_unicode_registry(self):
        reg = {'a': {'name': 'Zoë', 'seen': T0, 'told': None, 'extra': [1, 2.5, 'é', {'k': None}]},
               'b': {'name': 'Lou', 'seen': 1.5e9 + T0, 'told': 'Lou'}}
        self.seq([self.ev(session='a', cwd=self.nonrepo), self.ev(session='c', title='Zoë — ü😀', cwd=self.nonrepo),
                  self.ev(session='c', title='Zoë — ü😀', cwd=self.nonrepo)], seed=json.dumps(reg, ensure_ascii=False))

    def test_corrupt_registry(self):
        for seed in ('', '{', 'not json', '[]', '"s"', '123', 'null', '{"a": 1}', '{"a": {"seen": "x"}}',
                     '{"a": {"seen": null}}', '{"a": {"name": [1], "seen": 1700000000}}',
                     '{"a": {"seen": 1700000000}}',                     '{"s1": {"name": "", "seen": 1700000000}}', '{"s1": {"name": 0, "seen": 1700000000, "told": 3}}',
                     '{"s1": {"name": "Nox", "seen": 1700000000, "told": "Other"}}',
                     '{"s1": {"name": "Nox", "seen": 1700000000, "told": null}}',
                     '{"s1": 1, "s1": {"name": "Dup", "seen": 1700000000}}', '﻿{}'):
            with self.subTest(seed=seed):
                self.seq([self.ev(cwd=self.nonrepo), self.ev(hook='SessionStart', cwd=self.nonrepo)], seed=seed)
        side = self.side()
        side.registry.write_bytes(b'\xff\xfe{"a": 1}')
        self.check(side, json.dumps(self.ev(cwd=self.nonrepo)).encode(), T0)

    def test_desktop_entrypoint(self):
        env = {'CLAUDE_CODE_ENTRYPOINT': 'claude-desktop'}
        self.seq([self.ev(cwd=self.plain), self.ev(cwd=self.plain), self.ev(hook='SessionStart', cwd=self.plain),
                  self.ev(session='z', title='Q — r', cwd=self.plain, hook='SessionStart')], env_extra=env)
        self.seq([self.ev(cwd=self.plain)], env_extra={'CLAUDE_CODE_ENTRYPOINT': 'cli'})

    def test_bad_input(self):
        self.seq([b'', b'{', b'[]', b'[1]', b'"x"', b'null', b'{}', b'\xff', b'{"session_id": ""}',
                  b'{"session_id": null}', b'{"session_id": 0}', b'{"session_id": false}', b'{"session_id": []}',
                  b'{"session_id": 5}', b'{"session_id": ["a"]}', b'{"session_id": "s", "session_title": 5}',
                  b'{"session_id": "s", "session_title": ["x"]}', b'{"session_id": "s", "cwd": 5}',
                  b'{"session_id": "s", "cwd": "a\\u0000b"}', b'{"session_id": "s", "cwd": ""}',
                  b'{"session_id": "s", "hook_event_name": 5}', b'{"session_id": "s", "hook_event_name": null}',
                  b'{"session_id": "s"} trailing', b'  {"session_id": "s2"}  \n',
                  b'{"session_id": "s3", "session_id": "s4"}'])

    def test_unicode_and_escaping(self):
        self.seq([self.ev(title='Zoë — ünï 😀', cwd=self.plain),
                  self.ev(session='b', title='Q"uote\\ — \t\n\u0001\u007f ', cwd=self.plain),
                  self.ev(session='c', title='😀 — x', cwd=self.feature),
                  self.ev(session='d', cwd=self.feature),
                  self.ev(session='e', title='日本 — 語', cwd=self.nonrepo)])

    def test_cwd_cases(self):
        sub = self.plain / 'sub'
        self.seq([self.ev(session=f'c{i}', cwd=c) for i, c in enumerate(
            [self.plain, sub, self.feature, self.detached, self.worktree, self.worktree / '.git', self.unborn,
             self.nonrepo, self.nonrepo / 'missing', self.file, self.plain / '.git', self.plain / '.git' / 'refs',
             self.plain / 'sub' / '..' / 'sub'])])
        self.seq([self.ev(session='r', hook='SessionStart', cwd=self.worktree),
                  self.ev(session='r', cwd=self.worktree)])

    def test_cwd_default_is_process_cwd(self):
        for d in (self.plain, self.detached, self.worktree, self.nonrepo):
            self.seq([self.ev()], cwd=d)

    def test_symlinked_cwd_and_relative_gitdir(self):
        link = Path(self.root) / 'link'
        if not link.exists():
            link.symlink_to(self.plain)
        self.seq([self.ev(cwd=link)])

    def test_git_env_override(self):
        self.seq([self.ev(cwd=self.nonrepo)], env_extra={'GIT_DIR': str(self.feature / '.git')})

    def test_clock_formats(self):
        for now in (T0, T0 + 0.123456, 1e16, 1e22, 123456789012345678.0, 0.00001, 0.0001, 1.5, 100.0):
            with self.subTest(now=now):
                self.seq([({**self.ev(cwd=self.nonrepo)}, now)])

    def test_repeated_sessions_share_names(self):
        self.seq([self.ev(session=f'x{i % 5}', cwd=self.plain) for i in range(14)])


if __name__ == '__main__':
    unittest.main()
