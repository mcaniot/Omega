"""Repeatable context audits use measured tokens and never rewrite sessions."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('OMEGA_OPTIMIZE_BINARY', ROOT / 'rust/target/release/optimize'))


class OptimizeTest(unittest.TestCase):
    def read_rows(self, calls):
        rows = []
        for i, (name, arguments, content) in enumerate(calls):
            rows.extend([
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': f'read-{i}', 'name': name, 'input': arguments}]}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': f'read-{i}', 'content': content}]}}])
        return ''.join(json.dumps(row) + '\n' for row in rows)

    def test_content_overlap_across_commands_ranges_and_checkpoints(self):
        a, b, c = [f'Unique source line {i}: with enough content to fingerprint.' for i in range(3)]
        self.transcript.write_text(self.read_rows([
            ('Read', {'file_path': '/source', 'offset': 1, 'limit': 2}, f'1→{a}\n     2\t{b}'),
            ('Bash', {'command': "'Ω-run' --shell 'sed -n \"2,3p\" source'"},
             json.dumps({'status': 'PASS', 'stdout': b + '\n' + c})),
            ('Bash', {'command': "'Ω-inspect' evidence --stream stdout"},
             json.dumps({'fragments': [{'offset': 0, 'text': c}]})),
        ]))
        before = self.transcript.read_bytes()
        report = json.loads(self.run_command('--check', '--json').stdout)
        row, = report['sessions']
        self.assertEqual((row['read_calls'], row['overlapping_read_calls'],
                          row['repeated_read_lines'], row['novel_read_lines']), (3, 2, 2, 3))
        self.assertEqual(row['repeated_exact_calls_extra'], 0)
        self.assertTrue(any(f['metric'] == 'overlapping_read_calls' for f in report['findings']))
        self.assertEqual(self.transcript.read_bytes(), before)
        # Changed data remains novel even when file name/range or timestamps match.
        with self.transcript.open('a') as stream:
            stream.write(self.read_rows([
                ('Read', {'file_path': '/source', 'offset': 1, 'limit': 2}, a + '\n' + b + ' MODIFIED')]))
        row, = json.loads(self.run_command('--check', '--json').stdout)['sessions']
        self.assertEqual((row['read_calls'], row['repeated_read_lines'], row['novel_read_lines']), (1, 1, 1))
        state = self.state_path().read_text()
        self.assertNotIn(a, state)  # only digests, never source text

    def test_codex_content_overlap_uses_returned_text_not_command(self):
        path = self.home / '.codex/sessions/session.jsonl'
        path.parent.mkdir(parents=True)
        source = 'Actual source contents: this line is long enough to fingerprint.'
        rows = []
        for i, command in enumerate(('cat file', 'sed -n "1p" file')):
            rows.extend([
                {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': str(i),
                 'name': 'exec_command', 'arguments': json.dumps({'cmd': command})}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': str(i),
                 'output': json.dumps({'output': json.dumps({'stdout': source})})}}])
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        report = json.loads(self.run_command('--check', '--json').stdout)
        row, = [r for r in report['sessions'] if r['provider'] == 'codex']
        self.assertEqual((row['read_calls'], row['repeated_read_lines']), (2, 1))

    def test_whitespace_changes_are_novel_and_images_are_not_read_data(self):
        source = 'source_line_with_semantically_significant_indentation()'
        self.transcript.write_text(self.read_rows([
            ('Read', {'file_path': '/source'}, source),
            ('Read', {'file_path': '/source'}, '    ' + source),
            ('Read', {'file_path': '/source'}, source + ' '),
            ('Read', {'file_path': '/image.png'}, [{'type': 'image', 'source': {'data': source}}]),
            ('Bash', {'command': 'pytest'}, source),
        ]))
        row, = json.loads(self.run_command('--check', '--json').stdout)['sessions']
        self.assertEqual((row['read_calls'], row['repeated_read_lines'], row['novel_read_lines']), (3, 0, 3))

    def test_resume_reads_task_first_without_transcripts_or_evidence(self):
        tasks = self.home / '.agents/tasks'
        (tasks / 'completed').mkdir(parents=True)
        (tasks / 'active').mkdir()
        task = tasks / 'completed/work.md'
        task.write_text('Current: complete.\nEvidence: /do/not/open/stdout.log\nNext: none.\n')
        result = json.loads(self.run_command('resume', 'work').stdout)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['content'], task.read_text())
        self.assertFalse(self.state_path().exists())
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)
        (tasks / 'active/work.md').write_text('Current: work reopened.\n')
        self.assertEqual(json.loads(self.run_command('resume', 'work.md').stdout)['state'], 'active')
        self.assertEqual(json.loads(self.run_command('resume').stdout)['total'], 2)

    def test_resume_rejects_traversal_symlinks_and_missing_tasks(self):
        tasks = self.home / '.agents/tasks/active'
        tasks.mkdir(parents=True)
        (tasks / 'link.md').symlink_to(self.transcript)
        for task in ('../secret', '/tmp/secret', 'link', 'missing', '.', 'a' * 129):
            with self.subTest(task=task):
                self.run_command('resume', task, success=False)

    def test_resume_lists_newest_tasks_before_truncation_with_stable_ties(self):
        tasks = self.home / '.agents/tasks'
        for state in ('active', 'completed'):
            (tasks / state).mkdir(parents=True)
        for i in range(25):
            task = tasks / 'active' / f'a-{i:02}.md'
            task.write_text('old')
            os.utime(task, (100, 100))
        for state, name in (('completed', 'z-new'), ('active', 'z-new'), ('active', 'b-new')):
            task = tasks / state / f'{name}.md'
            task.write_text('recent')
            os.utime(task, (200, 200))
        (tasks / 'active' / 'symlink.md').symlink_to(self.transcript)
        result = json.loads(self.run_command('resume').stdout)
        self.assertEqual(result['total'], 28)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['tasks']), 20)
        self.assertEqual(result['tasks'][:4], [
            ['active', 'b-new.md'], ['active', 'z-new.md'],
            ['completed', 'z-new.md'], ['active', 'a-00.md']])

    def test_resume_output_budget_and_truncation_are_explicit(self):
        tasks = self.home / '.agents/tasks/active'
        tasks.mkdir(parents=True)
        (tasks / 'large.md').write_text('\x01' * 20000)
        done = self.run_command('resume', 'large')
        self.assertLessEqual(len(done.stdout.encode()), 8000)
        self.assertTrue(json.loads(done.stdout)['truncated'])

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
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{invalid}\n')
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

    def state_path(self):
        return self.home / '.agents/tasks/state/omega-optimize.json'

    def test_incremental_skip_timestamp_and_force(self):
        first = json.loads(self.run_command('--json').stdout)
        self.assertEqual(len(first['sessions']), 1)
        state = json.loads(self.state_path().read_text())
        entry = state['sessions']['project/session.jsonl']
        self.assertGreater(entry['last_analyzed_unix_seconds'], 0)
        self.assertIn('mtime_nanoseconds', entry['fingerprint'])
        self.assertEqual(self.state_path().stat().st_mode & 0o777, 0o600)
        original = self.state_path().read_bytes()
        second = json.loads(self.run_command('--json').stdout)
        self.assertEqual(second['sessions'], [])
        self.assertEqual(second['sessions_skipped_unchanged'], 1)
        self.assertEqual(self.state_path().read_bytes(), original)
        forced = json.loads(self.run_command('--force', '--json').stdout)
        self.assertEqual(len(forced['sessions']), 1)
        self.assertEqual(forced['sessions_skipped_unchanged'], 0)

    def test_modified_session_is_reanalyzed_even_with_restored_mtime_and_same_size(self):
        self.run_command('--json')
        before = self.transcript.stat()
        text = self.transcript.read_text().replace('"input_tokens": 20,', '"input_tokens": 21,')
        self.assertNotEqual(text, self.transcript.read_text())
        self.transcript.write_text(text)
        os.utime(self.transcript, ns=(before.st_atime_ns, before.st_mtime_ns))
        report = json.loads(self.run_command('--json').stdout)
        row, = report['sessions']
        self.assertEqual(row['context_last'], 71)
        self.assertEqual(report['sessions_skipped_unchanged'], 0)

    def test_failed_report_or_configuration_does_not_advance_markers(self):
        report = self.home / 'existing.json'
        report.write_text('preserve')
        self.run_command('--report', str(report), success=False)
        self.assertFalse(self.state_path().exists())
        self.run_command('--apply', success=False)
        self.assertFalse(self.state_path().exists())
        result = json.loads(self.run_command('--json').stdout)
        self.assertEqual(len(result['sessions']), 1)

    def test_state_lock_prevents_concurrent_marker_updates(self):
        directory = self.state_path().parent
        directory.mkdir(parents=True)
        with (directory / 'omega-optimize.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            done = self.run_command('--json', success=False)
            self.assertIn('holds the state lock', done.stderr)
            self.assertFalse(self.state_path().exists())

    def test_invalid_state_is_preserved_even_with_force(self):
        self.state_path().parent.mkdir(parents=True)
        for text in ('{invalid', '{"version":99,"sessions":{}}'):
            self.state_path().write_text(text)
            self.run_command('--force', '--json', success=False)
            self.assertEqual(self.state_path().read_text(), text)

    def test_same_session_filename_in_different_projects_has_distinct_markers(self):
        other = self.transcript.parent.parent / 'other-project/session.jsonl'
        other.parent.mkdir()
        other.write_bytes(self.original_transcript)
        report = json.loads(self.run_command('--json').stdout)
        self.assertEqual(len(report['sessions']), 2)
        state = json.loads(self.state_path().read_text())
        self.assertEqual(set(state['sessions']), {'project/session.jsonl', 'other-project/session.jsonl'})
        repeated = json.loads(self.run_command('--json').stdout)
        self.assertEqual(repeated['sessions_skipped_unchanged'], 2)

    def append(self, rows, path=None):
        with (path or self.transcript).open('a') as stream:
            for row in rows:
                stream.write(json.dumps(row) + '\n')

    def test_incremental_window_ignores_historical_findings_and_persists_report(self):
        first = json.loads(self.run_command('--json').stdout)
        offset = self.transcript.stat().st_size
        self.append([{'type': 'assistant', 'timestamp': '2026-11-04T15:00:00Z',
                      'message': {'id': 'new', 'usage': {'input_tokens': 17}}}])
        report = json.loads(self.run_command('--json').stdout)
        row, = report['sessions']
        self.assertEqual(row['requests'], 1)
        self.assertEqual(row['tool_errors'], 0)
        self.assertEqual(row['large_tool_outputs'], 0)
        self.assertEqual(row['start_offset'], offset)
        self.assertEqual(row['analysis_scope'], 'since previous checkpoint')
        self.assertGreater(row['since_unix_seconds'], 0)
        self.assertEqual(report['findings'], [])
        saved = Path(report['report_file'])
        self.assertEqual(json.loads(saved.read_text()), report)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(first['report_file'], report['report_file'])

    def test_codex_sessions_recursive_and_cached_tokens_not_double_counted(self):
        path = self.home / '.codex/sessions/2026/11/04/rollout.jsonl'
        path.parent.mkdir(parents=True)
        self.append([
            {'type': 'session_meta', 'payload': {'cwd': '/codex-project'}},
            {'type': 'event_msg', 'timestamp': '2026-11-04T15:00:00Z', 'payload': {
                'type': 'token_count', 'info': {'last_token_usage': {
                    'input_tokens': 123, 'cached_input_tokens': 100},
                    'total_token_usage': {'input_tokens': 9999}}}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'call',
                'name': 'exec_command', 'arguments': '{"cmd":"example"}'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output',
                'call_id': 'call', 'output': json.dumps({'exit_code': 1, 'output': 'x' * 9000})}},
            {'type': 'compacted', 'timestamp': '2026-11-04T15:01:00Z', 'payload': {}},
        ], path)
        report = json.loads(self.run_command('--json').stdout)
        row, = [r for r in report['sessions'] if r['provider'] == 'codex']
        self.assertEqual(row['project'], '/codex-project')
        self.assertEqual(row['context_last'], 123)
        self.assertEqual(row['tool_calls'], 1)
        self.assertEqual(row['tool_errors'], 1)
        self.assertEqual(row['large_tool_outputs'], 1)
        self.assertEqual(row['compactions'], 1)
        self.assertTrue(any(f['provider'] == 'codex' for f in report['findings']))
        self.append([{'type': 'event_msg', 'payload': {'type': 'token_count',
            'info': {'last_token_usage': {'input_tokens': 42}}}}], path)
        row, = json.loads(self.run_command('--json').stdout)['sessions']
        self.assertEqual(row['context_last'], 42)
        self.assertEqual(row['tool_errors'], 0)
        self.assertEqual(row['tool_calls'], 0)

    def test_codex_native_exit_status_is_an_error_without_guessing_from_prose(self):
        path = self.home / '.codex/sessions/rollout.jsonl'
        path.parent.mkdir(parents=True)
        for identifier, output in [('failed', 'Wall time: 0.1 seconds\nProcess exited with code 2\nFinal output:\n'),
                                   ('ok', 'Process exited with code 0\nFinal output:\nerror mentioned in documentation')]:
            self.append([
                {'type': 'response_item', 'payload': {'type': 'function_call',
                    'name': 'exec_command', 'arguments': '{}', 'call_id': identifier}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output',
                    'output': output, 'call_id': identifier}},
            ], path)
        report = json.loads(self.run_command('--json').stdout)
        row, = [r for r in report['sessions'] if r['provider'] == 'codex']
        self.assertEqual(row['tool_errors'], 1)

    def test_partial_record_and_pending_tool_result_survive_checkpoint(self):
        self.append([{'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'pending', 'name': 'Read', 'input': {'file_path': '/code'}}]}}])
        result = json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'pending', 'content': 'x' * 9000}]}})
        with self.transcript.open('a') as stream:
            stream.write(result[:100])
        first = json.loads(self.run_command('--json').stdout)
        self.assertEqual(first['sessions'][0]['malformed_records'], 1)
        with self.transcript.open('a') as stream:
            stream.write(result[100:] + '\n')
        row, = json.loads(self.run_command('--json').stdout)['sessions']
        self.assertEqual(row['malformed_records'], 0)
        self.assertEqual(row['tool_calls'], 0)
        self.assertEqual(row['large_unbounded_text_reads'], 1)

    def test_rewritten_prefix_is_not_treated_as_append_only(self):
        self.run_command('--json')
        self.transcript.write_text(self.transcript.read_text().replace('input_tokens', 'other_tokens'))
        row, = json.loads(self.run_command('--json').stdout)['sessions']
        self.assertEqual(row['start_offset'], 0)
        self.assertEqual(row['analysis_scope'], 'full baseline')

    def test_changed_session_budget_is_not_consumed_by_unchanged_files(self):
        self.run_command('--json')
        old = self.transcript.with_name('older.jsonl')
        old.write_bytes(self.original_transcript)
        os.utime(old, (1, 1))
        report = json.loads(self.run_command('--sessions', '1', '--json').stdout)
        self.assertEqual(report['sessions'][0]['session'], 'older')
        self.assertEqual(report['sessions_skipped_unchanged'], 1)

    def test_evidence_driven_fix_and_check_mode(self):
        guard = self.home / '.agents/tools/context_guard'
        guard.parent.mkdir(parents=True)
        guard.write_text('#!/bin/sh\nexit 0\n')
        guard.chmod(0o700)
        checked = json.loads(self.run_command('--check', '--json').stdout)
        self.assertEqual(checked['fixes_applied'], 0)
        self.assertEqual(json.loads(self.settings.read_text()), self.original_settings)
        fixed = json.loads(self.run_command('--force', '--json').stdout)
        self.assertEqual(fixed['fixes_applied'], 1)
        self.assertTrue(any(f['status'] == 'applied' for f in fixed['findings']))
        settings = json.loads(self.settings.read_text())
        settings['hooks']['PreToolUse'].pop()
        self.assertEqual(settings, self.original_settings)
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)

    def test_force_reaudits_history_and_failed_report_preserves_cursor(self):
        self.run_command('--json')
        state = self.state_path().read_bytes()
        self.append([{'type': 'system'}])
        existing = self.home / 'existing.json'
        existing.write_text('keep')
        self.run_command('--report', str(existing), success=False)
        self.assertEqual(self.state_path().read_bytes(), state)
        row, = json.loads(self.run_command('--force', '--json').stdout)['sessions']
        self.assertEqual(row['start_offset'], 0)
        self.assertEqual(row['tool_errors'], 1)

    def write_rows(self, rows):
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    @staticmethod
    def tool(id, name, arguments, content, error=False):
        return [{'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': id, 'name': name, 'input': arguments}]}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': id, 'content': content, 'is_error': error}]}}]

    def force_row(self):
        row, = json.loads(self.run_command('--force', '--json').stdout)['sessions']
        return row

    def test_token_totals_count_usage_once_per_message_id(self):
        usage = {'input_tokens': 10, 'cache_creation_input_tokens': 20,
                 'cache_read_input_tokens': 30, 'output_tokens': 5}
        rows = [{'type': 'assistant', 'message': {'id': 'm1', 'usage': usage}},
                {'type': 'assistant', 'message': {'id': 'm1', 'usage': usage}},
                {'type': 'assistant', 'message': {'id': 'm2', 'usage': usage}}]
        self.write_rows(rows)
        row = self.force_row()
        self.assertEqual((row['tokens_input'], row['tokens_cache_creation'], row['tokens_cache_read'],
                          row['tokens_output'], row['tokens_total'], row['tokens_per_request']),
                         (20, 40, 60, 10, 130, 65))
        default = self.run_command('--force').stdout
        self.assertIn('TOK/REQ', default)

    def test_duplicate_tool_results_count_once(self):
        rows = self.tool('t', 'Bash', {'command': 'false'}, 'Exit code 2\nboom', True)
        self.write_rows(rows + rows[1:] * 3)
        row = self.force_row()
        self.assertEqual((row['tool_calls'], row['tool_errors']), (1, 1))

    def test_error_classification(self):
        cases = [
            ('Bash', {'command': 'ls'}, 'PreToolUse:Bash hook error: Unwrapped or noncanonical shell command', 'guard'),
            ('Read', {'file_path': '/x'}, 'Large text file: use limit', 'guard'),
            ('Bash', {'command': "'Ω-inspect' evidence"}, 'usage: Ω-inspect: unrecognized arguments', 'wrapper_usage'),
            ('Edit', {'file_path': '/x'}, 'File has not been read yet. Read it first', 'wrapper_usage'),
            ('Read', {'file_path': '/h/.agents/tasks/evidence/r/out.log'}, 'File does not exist.', 'wrapper_usage'),
            ('Bash', {'command': 'yes | head'}, 'Exit code 141', 'sigpipe'),
            ('Bash', {'command': 'rg missing file'}, 'Exit code 1', 'no_match'),
            ('Bash', {'command': 'rg missing file'}, 'Exit code 1\nrg: file: permission denied', 'real'),
            ('Bash', {'command': 'cp a b'}, 'cp: No space left on device', 'data_loss'),
            ('Bash', {'command': 'run'}, json.dumps({'status': 'DATA_LOSS'}), 'data_loss'),
            ('Bash', {'command': 'make'}, 'Exit code 2\nerror: build failed', 'real'),
        ]
        rows = []
        for i, (name, arguments, content, _) in enumerate(cases):
            rows += self.tool(f'e{i}', name, arguments, content, True)
        self.write_rows(rows)
        row = self.force_row()
        self.assertEqual(row['error_classes'], {'guard': 2, 'wrapper_usage': 3, 'sigpipe': 1,
                                                'no_match': 1, 'data_loss': 2, 'real': 2})
        self.assertEqual(row['workflow_errors'], 7)
        self.assertEqual(row['tool_errors'], len(cases))

    def test_reread_lines_split_legit_and_avoidable(self):
        text = '\n'.join(f'Source line number {i} is long enough to fingerprint.' for i in range(2))
        read = {'file_path': '/src'}
        rows = (self.tool('r1', 'Read', read, text) + self.tool('r2', 'Read', read, text)
                + self.tool('ed', 'Edit', {'file_path': '/src'}, 'ok')
                + self.tool('r3', 'Read', read, text) + self.tool('r4', 'Read', read, text)
                + [{'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'c'}]
                + self.tool('r5', 'Read', read, text))
        self.write_rows(rows)
        row = self.force_row()
        self.assertEqual((row['repeated_read_lines'], row['legit_reread_lines'],
                          row['avoidable_reread_lines']), (8, 4, 4))

    def test_compare_prints_gates_and_exits_zero_even_when_ko(self):
        base = self.home / 'base.json'
        base.write_text(json.dumps({'sessions': [{'tokens_total': 3000}, {'tokens_total': 3000}]}))
        report = json.loads(self.run_command('--force', '--json', '--compare', str(base)).stdout)
        gates = {name: gate['ok'] for name, gate in report['gates'].items()}
        self.assertEqual(gates, {'avoidable_reread_lines': True, 'compactions': False, 'context_peak': True,
                                 'data_loss': True, 'repeated_exact_calls': False,
                                 'workflow_error_ratio': True,
                                 'median_tokens_per_session': True})
        table = self.run_command('--force', '--compare', str(base)).stdout
        self.assertIn('compactions', table)
        self.assertRegex(table, r'compactions\s+1\s+== 0\s+KO')
        base.write_text(json.dumps({'sessions': [{'tokens_total': 900}]}))
        report = json.loads(self.run_command('--force', '--json', '--compare', str(base)).stdout)
        self.assertFalse(report['gates']['median_tokens_per_session']['ok'])

    def test_compare_rejects_base_without_new_fields_before_checkpointing(self):
        base = self.home / 'old.json'
        base.write_text(json.dumps({'sessions': [{'context_peak': 1}]}))
        done = self.run_command('--compare', str(base), success=False)
        self.assertIn('--force', done.stderr)
        self.assertFalse(self.state_path().exists())


class ReplayTest(unittest.TestCase):
    """Counterfactual replay; every synthetic result has bytes = 3.5 * context delta, so bytes/token = 3.5."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        self.transcript = self.home / '.claude/projects/project/session.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.settings = self.home / '.claude/settings.json'
        self.settings.write_text('{"permissions": {}}')

    @staticmethod
    def request(id, context, calls=(), output=10):
        content = [{'type': 'tool_use', 'id': i, 'name': n, 'input': inp} for i, n, inp in calls]
        return {'type': 'assistant', 'message': {'id': id, 'content': content, 'usage': {
            'input_tokens': context, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
            'output_tokens': output}}}

    @staticmethod
    def result(id, text, error=False):
        return {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': id, 'content': text, 'is_error': error}]}}

    @staticmethod
    def source(char):
        return (char * 99 + '\n') * 35  # 3500 bytes, 35 fingerprintable lines

    def replay(self, rows, *args):
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        done = subprocess.run([BINARY, 'replay', '--json', *args], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def read_rows(self, second_text):
        read = {'file_path': '/src/a.rs'}
        return [self.request('a', 1000, [('t1', 'Read', read)]), self.result('t1', self.source('x')),
                self.request('b', 2000, [('t2', 'Read', read)]), self.result('t2', second_text),
                self.request('c', 3000)]

    def test_noop_session_reduces_nothing(self):
        # baseline 1000+2000+3000 + 3*10 = 6030; no rule applies.
        row, = self.replay(self.read_rows(self.source('y')))['sessions']
        self.assertEqual((row['baseline_tokens'], row['counterfactual_tokens'], row['reduction_pct']), (6030, 6030, 0))
        # calibration: 3500 result bytes over (2000-1000-10 output) = 990 tokens
        self.assertAlmostEqual(row['bytes_per_token'], 3500 / 990)
        self.assertEqual(row['segments'], 1)

    def test_avoidable_reread_saves_result_minus_deny(self):
        # t2 is 3500 bytes = 990 tokens (calibrated net of output), replaced by 40: request c loses 950. 6030 - 950 = 5080.
        row, = self.replay(self.read_rows(self.source('x')))['sessions']
        self.assertEqual(row['counterfactual_tokens'], 5080)
        self.assertEqual(row['savings'], {'r1': 950, 'r2': 0, 'r3': 0})

    def bash_rows(self, error_text, error=True):
        pad = lambda text: text.ljust(35)  # 35 bytes = 10 tokens
        bash = {'command': 'ls | head'}
        return [self.request('a', 1000, [('t1', 'Bash', bash)]), self.result('t1', pad(error_text), error),
                self.request('b', 1010, [('t2', 'Bash', bash)]), self.result('t2', pad('ok')),
                self.request('c', 1020)]

    def test_sigpipe_retry_is_removed_with_its_result(self):
        # baseline 1000+1010+1020+30 = 3060. Request a (1000+10) vanishes and its 10 result tokens
        # leave b and c: (1010-10+10) + (1020-10+10) = 2030. Saving 1030.
        row, = self.replay(self.bash_rows('Exit code 141'))['sessions']
        self.assertEqual(row['counterfactual_tokens'], 2030)
        self.assertEqual(row['savings'], {'r1': 0, 'r2': 1030, 'r3': 0})

    def test_guard_and_real_errors_save_nothing(self):
        for text in ('PreToolUse hook error: Unwrapped or noncanonical', 'boom'):
            with self.subTest(text=text):
                row, = self.replay(self.bash_rows(text))['sessions']
                self.assertEqual(row['counterfactual_tokens'], 3060)
                self.assertEqual(row['savings'], {'r1': 0, 'r2': 0, 'r3': 0})

    def test_budget_splits_session_at_hard_limit(self):
        # contexts 50k,100k,130k,135k,140k, output 10 each: baseline 555050. Cut after 130k request;
        # later contexts 50000+3000+(135000-130000)=58000 and 50000+3000+10000=63000.
        # Counterfactual 50000+100000+130000+58000+63000+50 = 401050; r3 saves 154000.
        rows = [self.request(str(i), c) for i, c in enumerate([50000, 100000, 130000, 135000, 140000])]
        row, = self.replay(rows)['sessions']
        self.assertEqual((row['baseline_tokens'], row['counterfactual_tokens'], row['segments']), (555050, 401050, 2))
        self.assertEqual(row['savings'], {'r1': 0, 'r2': 0, 'r3': 154000})

    def handoff_rows(self, first, extra=()):
        # First result is 3500 bytes = 1000 tokens (a->b delta 2010-1000-10). Request c (10010) crosses
        # the 10000 handoff; d and later run in the new segment; the result of d adds 1000 tokens to e.
        f = {'file_path': '/src/f.rs'}
        rows = [self.request('a', 1000, [('t1', *first)]), self.result('t1', self.source('x')),
                self.request('b', 2010), self.request('c', 10010),
                self.request('d', 10020, [('t2', 'Read', f)]), self.result('t2', self.source('y')),
                self.request('e', 11030, [('t3', 'Edit', f)] if extra else []), self.result('t3', 'ok')]
        return rows + ([self.request('g', 11040)] if extra else [])

    def test_post_cut_reread_is_charged(self):
        # Cut at c: shift = 1000+3000-10010 = -6010. Segment 2 effective d=4010; d Reads f (read before
        # the cut, 1000 tokens): extra request 4010, shift -5010; e = 11030-5010 = 6020.
        # Counterfactual 1010+2020+10020+4020+4010+6030 = 27110; without charge 1010+2020+10020+4020+5030 = 22100.
        # Baseline 1000+2010+10010+10020+11030+50 = 34120.
        rows = self.handoff_rows(('Read', {'file_path': '/src/f.rs'}))
        row, = self.replay(rows, '--handoff-tokens', '10000')['sessions']
        self.assertEqual((row['baseline_tokens'], row['counterfactual_tokens'], row['handoffs']), (34120, 27110, 1))
        self.assertEqual((row['r3_gross'], row['r3_reread_charge'], row['post_cut_rereads']), (12020, 5010, 1))
        self.assertEqual(row['savings']['r3'], 7010)

    def test_file_read_only_after_cut_is_not_charged(self):
        rows = self.handoff_rows(('Bash', {'command': 'ls'}))
        row, = self.replay(rows, '--handoff-tokens', '10000')['sessions']
        self.assertEqual((row['counterfactual_tokens'], row['r3_reread_charge'], row['post_cut_rereads']), (22100, 0, 0))

    def test_second_post_cut_access_is_not_charged_again(self):
        # As the first case plus Edit f in e and a final request g (11040): e = 6020 (cost 6030, no charge),
        # g = 6030 (cost 6040). Counterfactual 1010+2020+10020+4020+4010+6030+6040 = 33150; without charge 27140.
        rows = self.handoff_rows(('Read', {'file_path': '/src/f.rs'}), extra=True)
        row, = self.replay(rows, '--handoff-tokens', '10000')['sessions']
        self.assertEqual((row['counterfactual_tokens'], row['r3_reread_charge'], row['post_cut_rereads']), (33150, 6010, 1))

    def test_replay_never_writes_state_or_settings(self):
        before = self.settings.read_bytes()
        self.replay(self.read_rows(self.source('x')))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob('*') if p.is_file()),
                         ['.claude/projects/project/session.jsonl', '.claude/settings.json'])

    def test_replay_is_deterministic(self):
        rows = self.read_rows(self.source('x'))
        self.assertEqual(self.replay(rows), self.replay(rows))


class PrefixTest(unittest.TestCase):
    """Fixed-context audit with a fake HOME; every number is derived from the synthetic files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        self.cwd = self.home / 'work/proj'
        files = {
            '.claude/CLAUDE.md': '@~/.agents/AGENTS.md\n',
            '.agents/AGENTS.md': 'g' * 100,
            'work/CLAUDE.md': 'w' * 30,
            'work/proj/CLAUDE.md': 'see @docs/extra.md.\n```\n@docs/missing.md\n```\n',
            'work/proj/docs/extra.md': 'e' * 50,
            'work/proj/.claude/skills/s/SKILL.md': '---\nname: s\ndescription: ' + 'd' * 100 + '\n---\nbody ' + 'b' * 500,
            '.claude/skills/g/SKILL.md': '---\nname: g\ndescription: "' + 'd' * 40 + '"\n---\n',
        }
        for name, text in files.items():
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.cwd / 'AGENTS.md').symlink_to(self.cwd / 'CLAUDE.md')
        (self.home / '.claude/settings.json').write_text('{"permissions": {}}')
        self.claude_md = len((self.cwd / 'CLAUDE.md').read_text())

    @staticmethod
    def message(id, input, cache_creation, cache_read, sidechain=False):
        return {'type': 'assistant', 'cwd': '/ignored', 'isSidechain': sidechain, 'message': {'id': id, 'usage': {
            'input_tokens': input, 'cache_creation_input_tokens': cache_creation,
            'cache_read_input_tokens': cache_read, 'output_tokens': 1}}}

    def session(self, directory, name, rows, age_days=0):
        path = self.home / '.claude/projects' / directory / f'{name}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(dict(r, cwd=str(self.cwd))) + '\n' for r in rows))
        if age_days:
            stamp = path.stat().st_mtime - age_days * 86400
            os.utime(path, (stamp, stamp))

    def snapshot(self):
        return {str(p): p.read_bytes() for p in sorted(self.home.rglob('*')) if p.is_file() and not p.is_symlink()}

    def run_prefix(self, *args):
        done = subprocess.run([BINARY, 'prefix', *args], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_prefix_instruction_chain_and_skills(self):
        self.session('p1', 'a', [self.message('a', 10, 500, 0), self.message('a', 10, 500, 0),
                                 self.message('side', 9999, 0, 0, sidechain=True), self.message('b', 5, 0, 600)])
        self.session('p1', 'b', [self.message('c', 20, 300, 400), self.message('d', 800, 0, 0)])
        self.session('p1', 'old', [self.message('o', 1, 0, 0)], age_days=40)
        self.session('bench-run', 'x', [self.message('x', 1, 0, 0)])
        before = self.snapshot()
        out = self.run_prefix('--json')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(out, self.run_prefix('--json'))
        report = json.loads(out)
        project, = report['projects']
        self.assertEqual((project['cwd'], project['sessions'], project['requests']), (str(self.cwd), 2, 4))
        self.assertEqual((project['first_context_min'], project['first_context_median'], project['context_total']), (510, 615.0, 2635))
        self.assertAlmostEqual(project['prefix_share'], 510 * 4 / 2635)
        project_bytes = self.claude_md + 50 + 30
        self.assertEqual([(Path(f['path']).name, f['bytes'], f['scope']) for f in project['instruction_files']],
                         [('CLAUDE.md', 21, 'global'), ('AGENTS.md', 100, 'global'),
                          ('CLAUDE.md', self.claude_md, 'project'), ('extra.md', 50, 'project'), ('CLAUDE.md', 30, 'project')])
        self.assertEqual(project['project_instruction_bytes'], project_bytes)
        self.assertEqual([(Path(d['path']).name, d['bytes']) for d in project['duplicates']], [('AGENTS.md', self.claude_md)])
        self.assertEqual(project['skills'], {'count': 1, 'description_bytes': 100})
        self.assertEqual(report['global']['skills'], {'count': 1, 'description_bytes': 40})
        reducible = project_bytes + 100 + self.claude_md
        self.assertEqual((project['reducible_bytes'], project['reducible_tokens_per_request']), (reducible, reducible // 4))
        self.assertEqual([f['kind'] for f in report['findings']], ['duplicate_instruction_file'])
        self.assertEqual(report['aggregate']['global_floor_tokens'], 510)
        self.assertAlmostEqual(report['aggregate']['weighted_prefix_share'], 510 * 4 / 2635)

    def test_report_is_create_new_private(self):
        self.session('p1', 'a', [self.message('a', 1, 2, 3)])
        report = self.home / 'report.json'
        self.run_prefix('--report', str(report))
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report.read_text())['projects'][0]['first_context_min'], 6)
        again = subprocess.run([BINARY, 'prefix', '--report', str(report)], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertNotEqual(again.returncode, 0)


if __name__ == '__main__':
    unittest.main()
