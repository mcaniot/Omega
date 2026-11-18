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
    assert f'href="{(tmp_path / "hub/index.html").as_uri()}"' in page  # link back to the hub


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


def run(tmp_path, *args):
    env = {**os.environ, 'PROGRESS_HUB': str(tmp_path / 'hub')}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def test_update_flags_edit_state_and_ask_ranks_first_in_hub(tmp_path):
    steps = [{'step': 'a', 'status': 'à faire'}, {'step': 'b', 'status': 'à faire'}]
    assert render(tmp_path, {'title': 'Autre', 'steps': [{'step': 'x', 'status': 'en cours'}]}).returncode == 0
    (tmp_path / 'other').mkdir()
    (tmp_path / 'other/state.json').write_text(json.dumps({'title': 'Question', 'steps': steps}))
    state = str(tmp_path / 'other/state.json')
    (tmp_path / 'other/img').mkdir()
    (tmp_path / 'other/img/a.png').write_bytes(b'')
    assert run(tmp_path, state, '--fig', 'img/missing.png=X').returncode == 1
    result = run(tmp_path, state, '--step', '2=fait::vérifié : ok', '--log', 'b fini', '--ask', 'Valider ?',
                 '--fig', 'img/a.png=Vue : face')
    assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / 'other/state.json').read_text())
    assert saved['steps'][1]['status'] == 'fait' and saved['steps'][1]['note'] == 'vérifié : ok'
    assert saved['steps'][1]['date'] and saved['log'][-1]['text'] == 'b fini' and saved['ask'] == 'Valider ?'
    assert saved['figures'][-1]['title'] == 'Vue : face'
    hub = (tmp_path / 'hub/index.html').read_text()
    assert '<title>(1) Suivi des tâches</title>' in hub and hub.index('Question') < hub.index('Autre')
    assert run(tmp_path, state, '--ask', '').returncode == 0
    assert 'ask' not in json.loads((tmp_path / 'other/state.json').read_text())
    assert '<title>Suivi des tâches</title>' in (tmp_path / 'hub/index.html').read_text()
    bad = run(tmp_path, state, '--step', '9=fait')
    assert bad.returncode == 1 and 'N in 1..2' in bad.stderr
    assert run(tmp_path, state, '--step', '2=en cours').returncode == 0  # keeps the note
    assert json.loads((tmp_path / 'other/state.json').read_text())['steps'][1]['note'] == 'vérifié : ok'
    assert run(tmp_path, state, '--step', '2=fait::', '--add-step', 'c', '--warn', 'GPU').returncode == 0
    saved = json.loads((tmp_path / 'other/state.json').read_text())
    assert 'note' not in saved['steps'][1] and saved['steps'][2]['step'] == 'c' and saved['warning'] == 'GPU'
    shown = run(tmp_path, state, '--show')
    assert '3. [à faire] c' in shown.stdout and 'warning: GPU' in shown.stdout
    assert run(tmp_path, state, '--show', '--log', 'x').returncode == 2


def test_title_counts_only_questions_and_parallel_updates_are_kept(tmp_path):
    assert render(tmp_path, {'title': 'B', 'steps': [{'step': 'a', 'status': 'bloqué'}]}).returncode == 0
    assert '<title>Suivi des tâches</title>' in (tmp_path / 'hub/index.html').read_text()
    state = str(tmp_path / 'page/state.json')
    env = {**os.environ, 'PROGRESS_HUB': str(tmp_path / 'hub')}
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), state, '--log', f'e{i}'], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(8)]
    assert all(p.wait() == 0 for p in procs)
    assert len(json.loads((tmp_path / 'page/state.json').read_text())['log']) == 8


def test_unmounted_page_stays_listed_and_forget_removes_it(tmp_path):
    ghost = '/nonexistent-mount/task/suivi/state.json'
    (tmp_path / 'hub').mkdir()
    (tmp_path / 'hub/pages.json').write_text(json.dumps([ghost]))
    assert render(tmp_path, {'title': 'Ok', 'steps': []}).returncode == 0
    assert ghost in json.loads((tmp_path / 'hub/pages.json').read_text())
    assert 'introuvable' in (tmp_path / 'hub/index.html').read_text()
    assert run(tmp_path, ghost, '--forget').returncode == 0
    assert ghost not in json.loads((tmp_path / 'hub/pages.json').read_text())


def test_all_rerenders_registered_pages(tmp_path):
    assert render(tmp_path, {'title': 'Ok', 'steps': []}).returncode == 0
    (tmp_path / 'page/index.html').write_text('old')
    assert run(tmp_path, '--all').returncode == 0
    assert 'Ok' in (tmp_path / 'page/index.html').read_text()


def test_handoff_and_clear_keep_one_page(tmp_path):
    assert render(tmp_path, {'title': 'Mission', 'steps': []}).returncode == 0
    state = str(tmp_path / 'page/state.json')
    assert run(tmp_path, state, '--task', 'mission-x', '--agent', 'Aube').returncode == 0
    # After /clear or a handoff the new session finds the same page from the task name.
    found = run(tmp_path, '--find', 'mission-x')
    assert found.returncode == 0 and found.stdout.strip() == state
    assert run(tmp_path, '--find', 'other').returncode == 1
    assert run(tmp_path, state, '--agent', 'Bela').returncode == 0
    saved = json.loads((tmp_path / 'page/state.json').read_text())
    assert saved['agent'] == 'Bela' and saved['log'][-1]['text'] == 'reprise par Bela (après Aube)'
    assert 'task: mission-x · agent: Bela' in run(tmp_path, state, '--show').stdout
    assert 'Bela' in (tmp_path / 'hub/index.html').read_text()


def test_page_and_hub_show_each_state_once(tmp_path):
    (tmp_path / 'page').mkdir()
    (tmp_path / 'page/a.png').write_bytes(b'')
    steps = [{'step': 'a', 'status': 'fait', 'date': '2026-11-18 16:24'}, {'step': 'b', 'status': 'en cours'},
             {'step': 'c', 'status': 'bloqué'}, {'step': 'd', 'status': 'à faire'}]
    state = {'title': 'T', 'steps': steps, 'figures': [{'file': 'a.png', 'title': 'Vue'}],
             'log': [{'date': f'2026-11-18 10:0{i}', 'text': f'entrée {i}'} for i in range(7)]}
    assert render(tmp_path, state).returncode == 0
    page = (tmp_path / 'page/index.html').read_text()
    assert '<meta name="color-scheme" content="light dark">' in page and 'prefers-reduced-motion' in page
    # One bar segment and one row per step, in order; only active and blocked steps carry a pill.
    assert '<i class="ok"></i><i class="run"></i><i class="bad"></i><i class="todo"></i>' in page
    assert [page.count(f'<li class="step {c}">') for c in ('ok', 'run', 'bad', 'todo')] == [1, 1, 1, 1]
    assert page.count('<span class="pill run">') == 1 and '<span class="pill ok">' not in page
    assert '<span class="pill bad">' in page.split('<h1>')[1].split('</header>')[0]  # page status: blocked
    assert '18/11 16:24' in page and '2026-11-18' not in page  # short dates on display only
    # The five latest log entries are visible, the older ones fold away; images open in the viewer.
    recent, older = page.split('<details id="journal-ancien">')
    assert 'entrée 2' in recent and 'entrée 1' not in recent and 'entrée 1' in older and 'entrée 0' in older
    assert '<a class="m zoom" href="a.png" data-title="Vue">' in page and '<dialog id="lb"' in page
    hub = (tmp_path / 'hub/index.html').read_text()
    assert "Rien n'attend ta décision." in hub and '<article class="task r1"' in hub and '<b>1/4</b>' in hub
    assert 'Étape 3 · c' in hub and '+1 en parallèle' in hub  # the blocked step leads
    assert render(tmp_path, state, '--ask', 'Valider ?').returncode == 0
    assert '<div class="banner">' in (tmp_path / 'page/index.html').read_text()
    hub = (tmp_path / 'hub/index.html').read_text()
    assert '1 mission attend ta décision.' in hub and '<article class="task r0"' in hub and 'Valider ?' in hub
