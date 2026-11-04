"""Response configuration must preserve unrelated user settings and deployment."""
import importlib.util
import json
from pathlib import Path
import re
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

    def test_completion_contract_is_centralized_in_agents(self):
        text = (ROOT / 'AGENTS.md').read_text()
        for clause in ('entire final response must be one short line',
                       '`commit and push done`', '`task <short label> done`',
                       'Never claim done after failure or partial completion',
                       'unless explicitly requested'):
            self.assertIn(clause, text)
        for source in (ROOT / 'prompts/codex.md', ROOT / 'output-styles/final-only.md'):
            with self.subTest(source=source):
                text = source.read_text()
                self.assertIn('AGENTS.md', text)
                self.assertNotIn('`task <short label> done`', text)
                self.assertLess(len(text.encode()), 400)
        style = (ROOT / 'output-styles/final-only.md').read_text()
        self.assertIn('keep-coding-instructions: true', style)
        self.assertIn('over default preamble and progress-update guidance', style)

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

    def test_codex_agents_are_standalone_and_obsolete_roles_are_removed(self):
        self.installer.AGENTS = self.home / '.agents'
        agents = self.codex.parent / 'agents'
        agents.mkdir()
        (agents / 'robotics-rl.toml').write_text('model = "m"\ndeveloper_instructions = "x"\n')
        (agents / 'custom.toml').write_text('name = "custom"\nmcp_servers = {}\n')
        self.codex.write_text(self.original_codex + '[agents]\nenabled = true\n\n'
                              '[agents.robotics-rl]\ndescription = "old"\nconfig_file = "agents/robotics-rl.toml"\n\n'
                              '[agents.coder]\ndescription = "old"\nconfig_file = "agents/coder.toml"\n\n'
                              '[agents.custom]\ndescription = "keep"\nconfig_file = "agents/custom.toml"\n'
                              '[tui]\nanimations = false\n')
        claude_agents = self.installer.CLAUDE / 'agents'
        claude_agents.mkdir()
        (claude_agents / 'robotics-rl.md').symlink_to(ROOT / 'agents/robotics-rl.md')
        (claude_agents / 'foreign.md').symlink_to(self.home / 'missing.md')
        self.assertEqual(sorted(p.stem for p in (ROOT / 'agents').glob('*.md')), ['architect', 'coder', 'scout'])
        self.assertEqual(set(self.installer.CODEX_AGENTS), {'architect', 'coder', 'scout'})
        list(self.installer.write_codex_agents(True))
        list(self.installer.remove_stale_links(True))
        for name in ('architect', 'coder', 'scout'):
            role = tomllib.loads((agents / f'{name}.toml').read_text())
            self.assertEqual(role['name'], name)
            self.assertTrue(role['description'] and role['developer_instructions'])
            self.assertNotIn('AGENTS.md', role['developer_instructions'])  # inherited, never duplicated
        self.assertFalse((agents / 'robotics-rl.toml').exists())
        self.assertTrue((agents / 'custom.toml').exists())
        config = tomllib.loads(self.codex.read_text())
        self.assertEqual(config['agents'], {'enabled': True,
                                            'custom': {'description': 'keep', 'config_file': 'agents/custom.toml'}})
        self.assertEqual(config['tui'], {'animations': False})
        self.assertIn('# keep comment', self.codex.read_text())
        self.assertFalse((claude_agents / 'robotics-rl.md').is_symlink())
        self.assertTrue((claude_agents / 'foreign.md').is_symlink())
        self.assertEqual([s for _, s in self.installer.write_codex_agents(True) if s != 'ok'],
                         ['unmanaged (left untouched)'])

    def test_agent_skill_references_exist(self):
        skills = {p.name for p in (ROOT / 'skills').iterdir()}
        for agent in (ROOT / 'agents').glob('*.md'):
            text = agent.read_text()
            preloaded = re.search(r'^skills:\n((?:  - .*\n)+)', text, re.M)
            names = re.findall(r'`([a-z-]+)` for', text)
            names += re.findall(r'- ([a-z-]+)', preloaded.group(1)) if preloaded else []
            for name in names:
                self.assertIn(name, skills, agent)
