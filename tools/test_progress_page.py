"""progress-page skill: page rendering, input validation and the multi-page hub."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/progress-page/scripts/make_page.py'


def render(tmp_path, state, *args):
    page = tmp_path / 'page'
    page.mkdir(exist_ok=True)
    (page / 'state.json').write_text(json.dumps(state))
    env = {**os.environ, 'PROGRESS_HUB': str(tmp_path / 'hub')}  # never touch the real hub
    return subprocess.run([sys.executable, str(SCRIPT), str(page / 'state.json'), *args],
                          capture_output=True, text=True, env=env, check=False)


def test_text_and_urls_are_escaped(tmp_path):
    state = {'title': 'T `c`', 'steps': [{'step': '<script>x</script>', 'status': 'fait'}],
             'figures': [{'file': 'javascript:alert(1)#a.png', 'title': '"t"'}]}
    assert render(tmp_path, state).returncode == 0
    page = (tmp_path / 'page/index.html').read_text()
    assert '<script>x' not in page and '&lt;script&gt;x' in page
    assert 'href="javascript:alert%281%29%23a.png"' not in page and 'javascript%3Aalert' in page
    assert '<title>T `c`</title>' in page


def test_invalid_state_fails_with_a_readable_message(tmp_path):
    result = render(tmp_path, {'title': 'T', 'steps': [{'step': 'a', 'status': 'done'}]})
    assert result.returncode == 1 and 'steps[0]' in result.stderr and 'Traceback' not in result.stderr
    assert not (tmp_path / 'page/index.html').exists()
    assert render(tmp_path, {'title': 'T', 'steps': []}, '--refresh', '0').returncode == 2


def test_hub_lists_pages_and_survives_a_corrupt_registry(tmp_path):
    (tmp_path / 'hub').mkdir()
    (tmp_path / 'hub/pages.json').write_text('[')
    assert render(tmp_path, {'title': 'Vide', 'steps': []}).returncode == 0
    hub = (tmp_path / 'hub/index.html').read_text()
    assert 'Vide' in hub and 'id="done"' not in hub  # no steps is not finished
    assert json.loads((tmp_path / 'hub/pages.json').read_text()) == [str(tmp_path / 'page/state.json')]
    assert render(tmp_path, {'title': 'Finie', 'steps': [{'step': 'a', 'status': 'fait'}]}).returncode == 0
    hub = (tmp_path / 'hub/index.html').read_text()
    assert hub.index('id="done"') < hub.index('Finie')


def test_broken_registered_page_does_not_break_the_hub(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'state.json').write_text('{"title": "x", "steps": null}')
    (tmp_path / 'hub').mkdir()
    (tmp_path / 'hub/pages.json').write_text(json.dumps([str(other / 'state.json')]))
    result = render(tmp_path, {'title': 'Ok', 'steps': [{'step': 'a', 'status': 'en cours'}]})
    assert result.returncode == 0 and 'warning' not in result.stderr
    hub = (tmp_path / 'hub/index.html').read_text()
    assert 'Ok' in hub and '&quot;steps&quot; must be a list' in hub
