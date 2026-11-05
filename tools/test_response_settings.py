"""Response configuration must preserve unrelated user settings and deployment."""
import importlib.util
import json
import os
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
        for clause in ('Work silently.',
                       'Return only the requested result.',
                       'For successful actions, output exactly one short line: `task <label> done`.',
                       'For failures, output only the blocker.',
                       'Never claim success without verification.',
                       'Provide details, commands, reviews, or explanations only when explicitly requested.'):
            self.assertIn(clause, text)
        for source in (ROOT / 'prompts/codex.md', ROOT / 'output-styles/final-only.md'):
            with self.subTest(source=source):
                text = source.read_text()
                self.assertIn('AGENTS.md', text)
                self.assertNotIn('`task <label> done`', text)
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
        self.assertEqual([s for _, s in self.installer.write_codex_agents(True) if s != 'ok'], [])

    def test_installer_preserves_custom_agents_and_configuration(self):
        agents = self.codex.parent / 'agents'
        agents.mkdir()
        # Same keys as Omega files, but not an Omega name: previously deleted as "stale".
        mine = 'name = "mine"\ndescription = "d"\nmodel = "m"\ndeveloper_instructions = "x"\n'
        (agents / 'mine.toml').write_text(mine)
        custom_architect = 'name = "architect"\nmodel = "m"\nmcp_servers = {}\n'
        (agents / 'architect.toml').write_text(custom_architect)
        custom_retired = 'name = "robotics-rl"\nskills = {}\n'
        (agents / 'robotics-rl.toml').write_text(custom_retired)
        (agents / 'broken.toml').write_text('not toml [')
        config = (self.original_codex + '[agents.architect]\ndescription = "mine"\n'
                  'config_file = "agents/architect.toml"\n\n[agents.robotics-rl]\ndescription = "mine"\n'
                  'config_file = "agents/robotics-rl.toml"\n')
        self.codex.write_text(config)
        results = dict(self.installer.write_codex_agents(True))
        self.assertEqual((agents / 'mine.toml').read_text(), mine)
        self.assertEqual((agents / 'architect.toml').read_text(), custom_architect)
        self.assertEqual((agents / 'robotics-rl.toml').read_text(), custom_retired)
        self.assertEqual((agents / 'broken.toml').read_text(), 'not toml [')
        self.assertEqual(self.codex.read_text(), config)
        self.assertTrue(results[agents / 'architect.toml'].startswith('CONFLICT'))
        for name in ('coder', 'scout'):
            self.assertNotIn('sandbox_mode', tomllib.loads((agents / f'{name}.toml').read_text()))
        # Previously generated files (including obsolete sandbox_mode) are upgraded in place.
        (agents / 'scout.toml').write_text('name = "scout"\nsandbox_mode = "read-only"\n')
        list(self.installer.write_codex_agents(True))
        self.assertEqual(tomllib.loads((agents / 'scout.toml').read_text())['model_reasoning_effort'], 'low')

    def test_links_never_replace_foreign_symlinks_or_files(self):
        claude_agents = self.installer.CLAUDE / 'agents'
        claude_agents.mkdir()
        own = self.home / 'my-scout.md'
        own.write_text('mine\n')
        (claude_agents / 'scout.md').symlink_to(own)
        (claude_agents / 'coder.md').write_text('mine\n')
        for name in ('scout', 'coder'):
            status = self.installer.link(claude_agents / f'{name}.md', ROOT / f'agents/{name}.md', True)
            self.assertTrue(status.startswith('CONFLICT'), status)
        self.assertEqual(Path(os.readlink(claude_agents / 'scout.md')), own)
        self.assertEqual((claude_agents / 'coder.md').read_text(), 'mine\n')
        (claude_agents / 'architect.md').symlink_to(ROOT / 'agents/old-name.md')
        self.assertEqual(self.installer.link(claude_agents / 'architect.md', ROOT / 'agents/architect.md', True),
                         'linked')

    def test_read_only_roles_are_enforced_where_the_runtime_allows(self):
        for name in ('architect', 'scout'):
            fields, _ = self.installer.frontmatter(ROOT / f'agents/{name}.md')
            tools = {tool.strip() for tool in fields['tools'].split(',')}
            self.assertFalse(tools & {'Edit', 'Write', 'NotebookEdit', 'Agent'}, name)
        self.assertNotIn('tools', self.installer.frontmatter(ROOT / 'agents/coder.md')[0])

    def test_agents_never_preload_skills(self):
        for agent in (ROOT / 'agents').glob('*.md'):
            self.assertNotIn('skills', self.installer.frontmatter(agent)[0], agent)

    def test_skill_references_and_trigger_descriptions(self):
        skills = {p.name for p in (ROOT / 'skills').iterdir()}
        sources = [*(ROOT / 'agents').glob('*.md'), *(ROOT / 'skills').glob('*/SKILL.md'), ROOT / 'AGENTS.md']
        for source in sources:
            for name in re.findall(r'[Ll]oad `([a-z][a-z-]*)`|and `([a-z][a-z-]*)` for', source.read_text()):
                self.assertIn(''.join(name), skills, source)
        for name in ('systematic-debugging', 'cpp-quality', 'robotics-learning', 'robotics-performance',
                     'robotics-validation', 'tool-output'):
            fields, _ = self.installer.frontmatter(ROOT / 'skills' / name / 'SKILL.md')
            self.assertEqual(fields['name'], name)
            self.assertTrue(fields['description'].startswith('Use when '), name)
            self.assertLessEqual(len(fields['description']), 1024, name)
