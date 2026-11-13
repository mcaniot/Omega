"""Golden tests for the local_read launcher.

The binary is built here with throwaway approved roots (LOCAL_READ_PROJECT / LOCAL_READ_TOOLS) inside a
fresh /tmp/lrg-XXXXXXXX tree (fixed length: long-path cases depend on it) next to a stub native/run.
The stub prints the exact command and environment the launcher would hand to the real runner and
executes the internal read verbs, so the whole launcher contract is observable without git, rg or
robots. fixtures/local_read_golden.json.gz holds argv, environment and the expected (exit code,
stdout, stderr) recorded from the original C launcher; paths under the temporary tree appear as <B>.
Outputs above 4 KiB are stored as length and SHA-256.

The default build uses the repository's offline Cargo cache and a private target directory; set
OMEGA_LOCAL_READ_TARGET to reuse a target directory between runs.
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
RUST_DIR = HERE.parent.parent / 'rust'
GOLDEN = HERE / 'fixtures' / 'local_read_golden.json.gz'

STUB = '''#!/usr/bin/python3 -I
import os, subprocess, sys
args = sys.argv[1:]
here = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
def show(text):
    return os.fsencode(text).replace(os.fsencode(here), b"<BIN>")
out = sys.stdout.buffer
out.write(b"runner:" + show(sys.argv[0]) + b"\\n")
for arg in args:
    out.write(b"arg:" + show(arg) + b"\\n")
for key, value in sorted(os.environ.items()):
    if key != "LC_CTYPE":
        out.write(b"env:" + os.fsencode(key) + b"=" + os.fsencode(value) + b"\\n")
out.flush()
rest = args[args.index("--") + 1:]
if len(rest) > 1 and rest[1].startswith("--internal-"):
    sys.exit(subprocess.run(rest).returncode)
'''


class Fixture:
    """Project, tools and outside trees with the awkward files the launcher must classify."""

    def __init__(self, base):
        self.base = Path(base)
        self.project = self.base / 'project'
        self.tools = self.base / 'tools'
        self.outside = self.base / 'outside'
        for directory in (self.project / 'sub' / 'deep', self.project / '.git', self.project / 'Token_dir',
                          self.tools / 'sub_dir', self.outside):
            directory.mkdir(parents=True)
        numbered = ''.join(f'line {i} alpha beta {"needle" if i % 7 == 0 else "hay"}\n' for i in range(1, 201))
        files = {
            'project/a.txt': numbered,
            'project/sub/deep/b.py': 'import os\nprint("hello")\n# TODO needle\n',
            'project/Token_dir/ok.txt': 'inside a sensitive directory\n',
            'project/.hidden': 'hidden\n',
            'project/secret_notes.txt': 'secret needle\n',
            'project/MyPassWord.txt': 'password needle\n',
            'project/cert.pem': 'needle\n',
            'project/a.key': 'needle\n',
            'project/a.p12': 'needle\n',
            'project/a.txt.bak': 'needle\n',
            'project/private': 'needle\n',
            'project/credentials': 'needle\n',
            'project/.git/config': 'needle\n',
            'project/empty.txt': '',
            'project/no_newline.txt': 'first\nsecond needle without newline',
            'project/crlf.txt': 'a needle\r\nb\r\n',
            'project/unicode.txt': 'café 日本 needle \U0001f600\nsecond\n',
            'project/quote.txt': "it's a \"needle\" $HOME `x` *.py [a-z]+ (a|b) .*\n",
            'project/with space.txt': 'space needle\n',
            'project/-dash.txt': 'dash needle\n',
            'project/two\nlines.txt': 'newline in name needle\n',
            'project/many.txt': ''.join(f'{i:05d} ' + 'x' * 1990 + ' needle\n' for i in range(60)),
            'project/long_line.txt': 'y' * 70000 + ' needle\nshort needle\n',
            'project/lines_only.txt': ''.join('n\n' for _ in range(500)),
            'project/locked.txt': 'x needle\n',
            'tools/tool.py': 'print("tool")\n# needle\n',
            'tools/sub_dir/inner.sh': '#!/bin/sh\necho needle\n',
            'outside/outside.txt': 'outside needle\n',
        }
        for name, content in files.items():
            (self.base / name).write_text(content, encoding='utf-8', newline='')
        (self.project / 'locked.txt').chmod(0)
        (self.project / 'nul.bin').write_bytes(b'before\x00needle after\nplain needle\nbin\xff\xfe needle\n')
        (self.project / 'nul_only.bin').write_bytes(b'\x00' * 100 + b'\nneedle\n')
        for name, size in (('big_ok.bin', 16 * 1024 * 1024), ('big_bad.bin', 16 * 1024 * 1024 + 1)):
            (self.project / name).write_bytes(b'')
            os.truncate(self.project / name, size)
        os.symlink('a.txt', self.project / 'link_to_a.txt')
        os.symlink('a.txt', self.project / '.env')
        os.symlink('a.txt', self.project / 'cred_alias_token')
        os.symlink('sub', self.project / 'dir_link')
        os.symlink(self.outside / 'outside.txt', self.project / 'to_outside.txt')
        os.symlink('/nonexistent/target', self.project / 'dangling')
        os.symlink('../tools/tool.py', self.project / 'to_tools.py')
        os.symlink(self.project, self.outside / 'project_alias')
        os.symlink('../.hidden', self.project / 'sub' / 'to_hidden')
        os.link(self.project / 'a.txt', self.project / 'hard_a.txt')
        os.link(self.outside / 'outside.txt', self.project / 'hard_outside.txt')
        os.mkfifo(self.project / 'fifo')
        os.mkfifo(self.project / 'fifo.txt')
        (self.project / 'a_dir').mkdir()
        self.long_dir = self.project
        for index in range(30):
            self.long_dir = self.long_dir / ('d' * 120 + str(index))
        self.long_dir.mkdir(parents=True, exist_ok=True)
        (self.long_dir / 'f.txt').write_text('deep needle\n')


def build(base, project, tools):
    """Release build with the throwaway roots; returns the directory holding local-read and native/run."""
    target = os.environ.get('OMEGA_LOCAL_READ_TARGET') or str(base / 'target')
    env = {**os.environ, 'LOCAL_READ_PROJECT': str(project), 'LOCAL_READ_TOOLS': str(tools), 'CARGO_TARGET_DIR': target}
    subprocess.run(['cargo', 'build', '--release', '--offline', '--bin', 'local_read'], cwd=RUST_DIR, env=env,
                   check=True, capture_output=True)
    directory = base / 'bin'
    (directory / 'native').mkdir(parents=True)
    shutil.copy(Path(target) / 'release' / 'local_read', directory / 'local-read')
    stub = directory / 'native' / 'run'
    stub.write_text(STUB)
    stub.chmod(0o755)
    return directory


def normalize(data, directory, base):
    return data.replace(os.fsencode(directory), b'<BIN>').replace(os.fsencode(base), b'<B>')


def digest(data):
    """Compact comparable form of one stream."""
    if len(data) <= 4096:
        return base64.b64encode(data).decode()
    return f'sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}'


def invoke(directory, base, name, argv, env):
    done = subprocess.run([str(directory / name), *argv], capture_output=True, env=env, stdin=subprocess.DEVNULL)
    return [done.returncode, *(digest(normalize(stream, directory, base)) for stream in (done.stdout, done.stderr))]


class LocalReadTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.base = Path(os.path.realpath(tempfile.mkdtemp(dir='/tmp', prefix='lrg-')))
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        assert len(str(cls.base)) == 17, cls.base
        cls.fixture = Fixture(cls.base)
        cls.directory = build(cls.base, cls.fixture.project, cls.fixture.tools)
        with gzip.open(GOLDEN, 'rt', encoding='utf-8') as handle:
            cls.golden = json.load(handle)

    def expand(self, text):
        return text.replace('<B>', str(self.base))

    def run_case(self, case):
        argv, env_name, expected = case
        env = {key: self.expand(value) for key, value in self.golden['envs'][env_name].items()}
        return argv, invoke(self.directory, self.base, 'local-read', [self.expand(a) for a in argv], env), expected

    def test_recorded_outputs(self):
        cases = [case for case in self.golden['cases'] if not (os.geteuid() == 0 and any('locked.txt' in arg for arg in case[0]))]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.run_case, cases))
        bad = [(argv, actual, expected) for argv, actual, expected in results if actual != expected]
        self.assertEqual(bad[:3], [], f'{len(bad)} of {len(results)} cases differ')

    def test_roots_and_environment(self):
        env = {key: self.expand(value) for key, value in self.golden['envs']['default'].items()}
        code, out, _ = invoke(self.directory, self.base, 'local-read', ['status'], env)
        self.assertEqual(code, 0)
        text = base64.b64decode(out).decode()
        self.assertIn('arg:<B>/project\n', text)
        self.assertIn('env:PATH=/usr/bin:/bin\n', text)
        for leaked in ('GIT_DIR', 'FOO', 'PYTHONPATH', 'LD_PRELOAD'):
            self.assertNotIn(leaked, text)

    def test_build_without_project_refuses(self):
        target = self.base / 'target-no-project'
        env = {key: value for key, value in os.environ.items() if key != 'LOCAL_READ_PROJECT'}
        subprocess.run(['cargo', 'build', '--release', '--offline', '--bin', 'local_read'], cwd=RUST_DIR,
                       env={**env, 'LOCAL_READ_TOOLS': str(self.fixture.tools), 'CARGO_TARGET_DIR': str(target)},
                       check=True, capture_output=True)
        lone = self.directory / 'no-project'
        shutil.copy(target / 'release' / 'local_read', lone)
        for argv in (['status'], ['--internal-text', 'a.txt']):
            done = subprocess.run([str(lone), *argv], capture_output=True, env={'PATH': '/usr/bin:/bin'})
            self.assertEqual(done.returncode, 64)
            self.assertIn(b'built without an absolute LOCAL_READ_PROJECT', done.stderr)

    def test_launcher_without_runner(self):
        lone = self.base / 'lone'
        shutil.copy(self.directory / 'local-read', lone)
        done = subprocess.run([str(lone), 'status'], capture_output=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(done.returncode, 127)
        self.assertIn(b'No such file or directory', done.stderr)

    def test_launcher_runs_native_run_beside_its_real_path(self):
        alias = self.directory / 'alias'
        os.symlink(self.directory / 'local-read', alias)
        env = {'PATH': '/usr/bin:/bin'}
        self.assertEqual(invoke(self.directory, self.base, 'alias', ['status'], env),
                         invoke(self.directory, self.base, 'local-read', ['status'], env))


if __name__ == '__main__':
    unittest.main()
