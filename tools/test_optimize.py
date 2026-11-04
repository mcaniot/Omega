"""Repeatable context audits use measured tokens and never rewrite sessions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('OMEGA_OPTIMIZE_BINARY', ROOT / 'rust/target/release/optimize'))


class OptimizeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        self.transcript = self.home / '.claude/projects/project/session.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.settings = self.home / '.claude/settings.json'
        self.original_settings = {'permissions': {'deny': ['keep']}, 'hooks': {
            'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'third-party'}]}]}}
        self.settings.write_text(json.dumps(self.original_settings))
        self.settings.chmod(0o600)
        source = {'name': 'Read', 'input': {'file_path': str(self.home / '.agents/tasks/evidence/run/stdout.log')}}
        def assistant(id, tokens, content=()):
            return {'type': 'assistant', 'cwd': '/project', 'timestamp': '2026-11-04T12:00:00Z',
                    'message': {'id': id, 'usage': {'input_tokens': tokens, 'cache_creation_input_tokens': 20,
                                                 'cache_read_input_tokens': 30}, 'content': list(content)}}
        rows = [assistant('a', 100, [{'type': 'tool_use', 'id': 't1', **source}]),
                assistant('a', 100, [{'type': 'tool_use', 'id': 't1', **source}]),
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x' * 9000}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x' * 9000}]}},
                assistant('b', 150, [{'type': 'tool_use', 'id': 't2', **source},
                                     {'type': 'tool_use', 'id': 'image', 'name': 'Read', 'input': {'file_path': '/image.png'}}]),
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 't2', 'content': 'failed', 'is_error': True},
                    {'type': 'tool_result', 'tool_use_id': 'image', 'content': [{'type': 'image', 'source': {'data': 'x' * 10000}}]}]}},
                {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'compact'},
                {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'compact'},
                assistant('c', 20)]
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{partial')
        self.original_transcript = self.transcript.read_bytes()

    def run_command(self, *args, success=True):
        done = subprocess.run([BINARY, *args], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(done.returncode == 0, success, done.stderr)
        return done

    def test_default_audit_counts_tokens_deduplicates_and_ignores_image_bytes(self):
        report = json.loads(self.run_command('optimize', '--json').stdout)
        self.assertIn('not proof of live sessions', report['selection'])
        row, = report['sessions']
        for key, expected in {'requests': 3, 'context_first': 150, 'context_last': 70,
                              'context_peak': 200, 'compactions': 1, 'tool_calls': 3,
                              'tool_errors': 1, 'repeated_exact_calls_extra': 1,
                              'large_unbounded_text_reads': 1, 'large_evidence_text_reads': 1,
                              'malformed_records': 1}.items():
            self.assertEqual(row[key], expected, key)
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)
        self.assertEqual(json.loads(self.settings.read_text()), self.original_settings)

    def test_apply_is_targeted_idempotent_and_preserves_symlink_mode(self):
        self.run_command('--apply', success=False)
        self.assertEqual(json.loads(self.settings.read_text()), self.original_settings)
        guard = self.home / '.agents/tools/context_guard'
        guard.parent.mkdir(parents=True)
        guard.write_text('#!/bin/sh\nexit 0\n')
        guard.chmod(0o700)
        target = self.home / 'managed-settings.json'
        self.settings.rename(target)
        self.settings.symlink_to(target)
        self.run_command('--apply')
        updated = json.loads(self.settings.read_text())
        added = updated['hooks']['PreToolUse'].pop()
        self.assertEqual(added['matcher'], 'Read')
        self.assertEqual(added['hooks'][0]['command'], str(guard))
        self.assertEqual(updated, self.original_settings)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        first = target.read_bytes()
        self.run_command('--apply')
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)

    def test_report_is_private_and_cannot_overwrite(self):
        path = self.home / 'report.json'
        self.run_command('--report', str(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_bytes()
        self.run_command('--report', str(path), success=False)
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_args_and_settings_fail_without_mutation(self):
        for args in (('--sessions', '0'), ('--sessions', '51'), ('--sessions', 'abc'), ('--unknown',)):
            self.run_command(*args, success=False)
        self.settings.write_text('{invalid')
        self.run_command('--apply', success=False)
        self.assertEqual(self.settings.read_text(), '{invalid')
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)

    def test_empty_home_is_read_only(self):
        self.transcript.unlink()
        self.settings.unlink()
        report = json.loads(self.run_command('--json').stdout)
        self.assertEqual(report['sessions'], [])
        self.assertFalse(self.settings.exists())

    def test_globally_disabled_hooks_are_not_silently_enabled(self):
        self.original_settings['disableAllHooks'] = True
        self.settings.write_text(json.dumps(self.original_settings))
        report = json.loads(self.run_command('--json').stdout)
        self.assertIn('disabled globally', report['read_guard'])
        self.run_command('--apply', success=False)
        self.assertEqual(json.loads(self.settings.read_text()), self.original_settings)

    def test_session_sample_uses_file_recency_and_is_bounded(self):
        other = self.transcript.with_name('newer.jsonl')
        other.write_text(self.transcript.read_text())
        os.utime(self.transcript, (1000, 1000))
        os.utime(other, (2000, 2000))
        report = json.loads(self.run_command('--sessions', '1', '--json').stdout)
        row, = report['sessions']
        self.assertEqual(row['session'], 'newer')


if __name__ == '__main__':
    unittest.main()
