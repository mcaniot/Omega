"""Response configuration must preserve unrelated user settings and deployment."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ResponseSettingsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        spec = importlib.util.spec_from_file_location('omega_responses_install', ROOT / 'install.py')
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.installer.CODEX = self.home / '.codex'
        self.installer.CLAUDE = self.home / '.claude'
        self.codex = self.installer.CODEX / 'config.toml'
        self.claude = self.installer.CLAUDE / 'settings.json'
        self.codex.parent.mkdir()
        self.claude.parent.mkdir()
        self.original_codex = '# keep comment\nmodel = "chosen-model"\n[permissions]\npolicy = "keep"\n'
        self.original_claude = {'env': {'EXAMPLE': 'keep'}, 'hooks': {'PreToolUse': []},
                                'permissions': {'deny': ['example']}}
        self.codex.write_text(self.original_codex)
        self.claude.write_text(json.dumps(self.original_claude))

    def test_completion_contract_is_explicit_in_all_instruction_sources(self):
        for source in (ROOT / "AGENTS.md", ROOT / "prompts/codex.md",
                       ROOT / "output-styles/final-only.md"):
            with self.subTest(source=source):
                text = source.read_text()
                self.assertIn("entire final response must be one short line", text)
                self.assertIn("`commit and push done`", text)
                self.assertIn("`task <short label> done`", text)
                self.assertIn("Never claim done after failure or partial completion", text)
                self.assertIn("unless explicitly requested", text)

    def test_preserves_settings_and_is_idempotent(self):
        original = tomllib.loads(self.codex.read_text())
        self.codex.chmod(0o600)
        self.assertEqual(len(list(self.installer.write_response_settings(True))), 2)
        config = tomllib.loads(self.codex.read_text())
        self.assertEqual(config.pop('model_instructions_file'), str(self.codex.parent / 'instructions.md'))
        self.assertEqual(config, original)
        self.assertIn('# keep comment', self.codex.read_text())
        settings = json.loads(self.claude.read_text())
        self.assertEqual(settings.pop('outputStyle'), 'Final only')
        self.assertEqual(settings, self.original_claude)
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.installer.write_response_settings(True)), [])

    def test_dry_run_and_conflicting_override_do_not_write(self):
        self.assertEqual(len(list(self.installer.write_response_settings(False))), 2)
        self.assertEqual(self.codex.read_text(), self.original_codex)
        original = 'model_instructions_file = "/custom/prompt.md"\n' + self.original_codex
        self.codex.write_text(original)
        result = list(self.installer.write_response_settings(True))
        self.assertTrue(result[0][1].startswith('CONFLICT'))
        self.assertEqual(self.codex.read_text(), original)
        self.assertEqual(json.loads(self.claude.read_text()), self.original_claude)

    def test_invalid_claude_config_does_not_partially_update_codex(self):
        self.claude.write_text('{invalid')
        with self.assertRaises(json.JSONDecodeError):
            list(self.installer.write_response_settings(True))
        self.assertEqual(self.codex.read_text(), self.original_codex)

    def test_preserves_config_symlink(self):
        target = self.home / 'managed-config.toml'
        self.codex.rename(target)
        self.codex.symlink_to(target)
        list(self.installer.write_response_settings(True))
        self.assertTrue(self.codex.is_symlink())
        self.assertIn('model_instructions_file', tomllib.loads(target.read_text()))

    def test_targeted_install_never_builds_or_installs_unrelated_tools(self):
        with patch('sys.argv', ['install.py', '--responses-only', '--apply']), \
                patch.object(self.installer, 'install_binaries') as binaries, \
                patch.object(self.installer, 'install_commands') as commands, \
                patch.object(self.installer, 'write_codex_agents') as agents:
            self.assertEqual(self.installer.main(), 0)
        binaries.assert_not_called()
        commands.assert_not_called()
        agents.assert_not_called()
        for dest, source in self.installer.response_links():
            self.assertEqual(dest.resolve(), source)
