"""progress-page skill: page rendering, input validation and the multi-page hub."""
import base64
import json
import re
import os
import subprocess
import sys
import urllib.parse
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
    assert 'href="../hub/index.html"' in page  # relative link back to the hub


def test_invalid_state_fails_with_a_readable_message(tmp_path):
    result = render(tmp_path, {'title': 'T', 'steps': [{'step': 'a', 'status': 'done'}]})
    assert result.returncode == 1 and 'steps[0]' in result.stderr and 'Traceback' not in result.stderr
    assert not (tmp_path / 'page/index.html').exists()
    result = render(tmp_path, {'title': 'T', 'steps': [{'step': 'a', 'status': []}]})
    assert result.returncode == 1 and 'steps[0]' in result.stderr and 'Traceback' not in result.stderr
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


def run(tmp_path, *args, session=None, stdin=None):
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_SESSION_ID'}
    env['PROGRESS_HUB'] = str(tmp_path / 'hub')
    if session:
        env['CLAUDE_CODE_SESSION_ID'] = session
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env,
                          input=stdin, check=False)


def test_user_prompt_clears_the_question_of_the_asking_session_only(tmp_path):
    (tmp_path / 'p').mkdir()
    state = str(tmp_path / 'p/state.json')
    (tmp_path / 'p/state.json').write_text(json.dumps({'title': 'Q', 'steps': []}))
    assert run(tmp_path, state, '--ask', 'Commit ?', session='s1').returncode == 0
    saved = json.loads((tmp_path / 'p/state.json').read_text())
    assert saved['ask_session'] == 's1'
    # With a tracked session, a progress update in the same turn keeps the question.
    assert run(tmp_path, state, '--log', 'en attente', session='s1').returncode == 0
    other = run(tmp_path, '--answered', stdin=json.dumps({'session_id': 's2'}))
    assert other.returncode == 0 and other.stdout == ''
    assert json.loads((tmp_path / 'p/state.json').read_text())['ask'] == 'Commit ?'
    answer = run(tmp_path, '--answered', stdin=json.dumps({'session_id': 's1'}))
    assert answer.returncode == 0 and answer.stdout == ''
    saved = json.loads((tmp_path / 'p/state.json').read_text())
    assert 'ask' not in saved and 'ask_session' not in saved
    assert saved['log'][-1]['text'] == 'réponse reçue : question retirée'
    assert 'Commit ?' not in (tmp_path / 'p/index.html').read_text()
    assert '<title>Suivi des tâches</title>' in (tmp_path / 'hub/index.html').read_text()
    # Fail-open: malformed hook input never errors or prints.
    bad = run(tmp_path, '--answered', stdin='not json')
    assert bad.returncode == 0 and bad.stdout == bad.stderr == ''


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
    # The first progress update after the answer clears a forgotten question; --agent alone keeps it.
    assert run(tmp_path, state, '--ask', 'Encore ?').returncode == 0
    assert run(tmp_path, state, '--agent', 'Bela').returncode == 0
    assert json.loads((tmp_path / 'other/state.json').read_text())['ask'] == 'Encore ?'
    assert run(tmp_path, state, '--log', 'réponse : oui').returncode == 0
    assert 'ask' not in json.loads((tmp_path / 'other/state.json').read_text())
    bad = run(tmp_path, state, '--step', '9=fait')
    assert bad.returncode == 1 and 'N in 1..2' in bad.stderr
    sup = run(tmp_path, state, '--step', '²=fait')  # isdigit() would accept it and int() crash
    assert sup.returncode == 1 and 'expected N=STATUS' in sup.stderr and 'Traceback' not in sup.stderr
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


def test_images_are_embedded_so_the_page_shows_without_its_files(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR\0\0\0\x02\0\0\0\x01" + b"\0" * 20
    (tmp_path / "page/img").mkdir(parents=True)
    (tmp_path / "page/img/a.png").write_bytes(png)
    (tmp_path / "page/img/big.png").write_bytes(png + b"\0" * 2_000_001)
    state = {"title": "T", "steps": [], "figures": [{"file": "img/a.png", "title": "a"},
                                                    {"file": "img/big.png", "title": "b"}]}
    assert render(tmp_path, state).returncode == 0
    page = (tmp_path / "page/index.html").read_text()
    assert 'href="img/a.png"' in page and '<img src="data:image/png;base64,' in page
    assert '<img src="img/big.png"' in page  # too large: left as a relative link
    assert '<img src="file://' not in page and '<link rel="icon" href="data:image/png;base64,' in page  # logo
    hub = (tmp_path / "hub/index.html").read_text()
    assert '<img src="../page/img/big.png"' in hub  # latest image over the hub limit: relative link


class Served:
    """omega-progress on a free port with a scratch hub; the URL line is read with a timeout."""

    def __init__(self, tmp_path, *args, env=None):
        import threading
        full = {**os.environ, 'PROGRESS_HUB': str(tmp_path / 'hub'), 'PROGRESS_TOKEN': 'tok', **(env or {})}
        self.proc = subprocess.Popen([sys.executable, str(SCRIPT), '--serve', *(args or ('127.0.0.1:0',))],
                                     env=full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = []
        reader = threading.Thread(target=lambda: line.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(10)
        assert line and line[0].startswith('http://'), self.stop()
        self.url = line[0].strip()
        self.port = int(self.url.split(':')[2].split('/')[0])
        self.base = f'http://127.0.0.1:{self.port}'

    def get(self, path, method='GET', **headers):
        """(status, headers, body) without raising on HTTP errors."""
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request(method, urllib.parse.quote(path, safe='/%'), headers=headers)
        reply = conn.getresponse()
        result = reply.status, dict(reply.getheaders()), reply.read()
        conn.close()
        return result

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        return self.proc.wait(timeout=10)


def served_page(tmp_path):
    """Registered page with an inside image, a symlink escaping it, an outside image and stray files."""
    png = b'\x89PNG\r\n\x1a\n' + b'\0' * 30
    (tmp_path / 'page/img').mkdir(parents=True)
    (tmp_path / 'page/img/a.png').write_bytes(png)
    (tmp_path / 'page/img/v.mp4').write_bytes(bytes(range(256)) * 4)
    (tmp_path / 'page/.env').write_text('SECRET=1')
    (tmp_path / 'page/notes.txt').write_text('unlisted')
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'outside/o.png').write_bytes(png)
    (tmp_path / 'page/img/link.png').symlink_to(tmp_path / 'outside/o.png')
    state = {'title': 'T', 'steps': [], 'figures': [
        {'file': 'img/link.png', 'title': 'lien'}, {'file': '../outside/o.png', 'title': 'dehors'},
        {'file': 'img/v.mp4', 'title': 'vidéo'}, {'file': 'img/a.png', 'title': 'a'}]}
    assert render(tmp_path, state).returncode == 0
    return f'/tok/fs{tmp_path}/page/'


def test_serve_follows_relative_links_and_serves_only_listed_files(tmp_path):
    page = served_page(tmp_path)
    server = Served(tmp_path)
    try:
        status, headers, _ = server.get('/tok/')
        assert status == 302 and headers['Location'] == f'/tok/fs{tmp_path}/hub/index.html'
        hub = server.get(headers['Location'])[2].decode()
        assert 'file://' not in hub
        link = re.search(r'class="thumb" href="([^"#]+)', hub).group(1)
        assert not link.startswith(('/', 'file:'))  # relative: also what Orca previews resolve
        assert urllib.parse.urljoin(headers['Location'], link) == page + 'index.html'
        html_page = server.get(page + 'index.html')[2].decode()
        assert 'href="../hub/index.html"' in html_page
        # Served files link relatively; escaping ones are never embedded, so their bytes are not in the page.
        assert '<img src="img/a.png"' in html_page and 'data:image/png;base64,' not in html_page.split('<main', 1)[1]
        assert server.get(page + 'img/a.png')[0] == 200
        for path in (page + 'state.json', page + '.state.lock', page + '.env', page + 'notes.txt',
                     page + 'img/link.png', f'/tok/fs{tmp_path}/outside/o.png', f'/tok/fs{tmp_path}/hub/pages.json',
                     page + '../outside/o.png', page + '%2e%2e/outside/o.png', page + 'x%00.png',
                     '/tok/fs/etc/passwd', f'/other/fs{tmp_path}/page/index.html'):
            assert server.get(path)[0] == 404, path
        # The hub never shows a thumbnail the server refuses.
        assert 'o.png' not in hub
    finally:
        server.stop()


def test_serve_rejects_unknown_hosts_and_bad_requests(tmp_path):
    page = served_page(tmp_path)
    server = Served(tmp_path)
    try:
        assert server.get(page + 'index.html', Host='evil.example')[0] == 421  # DNS rebinding
        assert server.get(page + 'index.html', Host=f'localhost:{server.port}')[0] == 200
        status, _, body = server.get(page + 'img/v.mp4', Range='bytes=' + '9' * 5000 + '-')
        assert status == 200 and len(body) == 1024  # absurd range ignored, no crash
        status, headers, body = server.get(page + 'index.html', method='HEAD')
        assert status == 200 and body == b'' and int(headers['Content-Length']) > 0
    finally:
        assert server.stop() == 0  # SIGTERM: clean exit


def test_serve_streams_byte_ranges_and_revalidates(tmp_path):
    page = served_page(tmp_path)
    data = bytes(range(256)) * 4
    server = Served(tmp_path)
    try:
        status, headers, body = server.get(page + 'img/v.mp4')
        assert status == 200 and body == data and headers['Accept-Ranges'] == 'bytes'
        assert headers['Cache-Control'] == 'no-cache' and headers['ETag'] and headers['Last-Modified']
        assert server.get(page + 'img/v.mp4', **{'If-None-Match': headers['ETag']})[0] == 304
        assert server.get(page + 'img/v.mp4', **{'If-Modified-Since': headers['Last-Modified']})[0] == 304
        for spec, expected in (('bytes=0-9', data[:10]), ('bytes=1000-', data[1000:]),
                               ('bytes=-24', data[-24:]), ('bytes=1020-5000', data[1020:])):
            status, headers, body = server.get(page + 'img/v.mp4', Range=spec)
            assert status == 206 and body == expected, spec
            assert headers['Content-Range'].endswith('/1024')
        assert server.get(page + 'img/v.mp4', Range='bytes=1024-')[0] == 416
        assert server.get(page + 'img/v.mp4', Range='bytes=0-1,5-6')[0] == 200  # multi-range: whole file
        assert server.get(page + 'index.html')[1]['Cache-Control'] == 'no-store'
    finally:
        server.stop()


def test_serve_open_mode_has_no_token(tmp_path):
    served_page(tmp_path)
    server = Served(tmp_path, '127.0.0.1:0', '--open')
    try:
        assert server.url == f'http://127.0.0.1:{server.port}/'
        status, headers, _ = server.get('/')
        assert status == 302 and headers['Location'] == f'/fs{tmp_path}/hub/index.html'
        assert server.get(f'/fs{tmp_path}/page/index.html')[0] == 200
    finally:
        server.stop()


def test_serve_exits_when_the_mdns_name_is_lost_and_stops_its_publisher(tmp_path):
    served_page(tmp_path)
    fake = tmp_path / 'bin'
    fake.mkdir()
    pids = tmp_path / 'pids'
    # Fake avahi-publish: records its pid, then dies at once or keeps running.
    (fake / 'avahi-publish').write_text(f'#!/bin/sh\necho $$ >> {pids}\n[ -e {tmp_path}/die ] && exit 3\nexec sleep 60\n')
    (fake / 'avahi-publish').chmod(0o755)
    env = {'PATH': f'{fake}:{os.environ["PATH"]}'}
    # Port 0 and a token: nothing reachable beyond this test on the LAN.
    server = Served(tmp_path, '0.0.0.0:0', '--name', 'omega-test', env=env)
    assert server.url.startswith('http://omega-test.local:')
    assert server.stop() == 0
    pid = int(pids.read_text().split()[-1])
    assert not os.path.exists(f'/proc/{pid}') or 'zombie' in open(f'/proc/{pid}/status').read().lower()
    (tmp_path / 'die').touch()
    server = Served(tmp_path, '0.0.0.0:0', '--name', 'omega-test', env=env)
    assert server.proc.wait(timeout=15) == 1
    assert 'no longer published' in server.proc.stderr.read()


def test_serve_rejects_a_bad_port_and_loopback_names(tmp_path):
    for args in (('127.0.0.1:http',), ('localhost:8765', '--name', 'x'), ('[::1]:8765', '--name', 'x')):
        result = run(tmp_path, '--serve', *args)
        assert result.returncode == 2 and 'Traceback' not in result.stderr, args


def test_finished_page_of_an_active_task_shows_in_progress(tmp_path):
    state = {'title': 'Fini', 'task': 'suite', 'steps': [{'step': 'a', 'status': 'fait'}]}
    (tmp_path / 'tasks').mkdir()
    env = {**os.environ, 'PROGRESS_HUB': str(tmp_path / 'hub'), 'PROGRESS_TASKS': str(tmp_path / 'tasks')}
    page = tmp_path / 'page'
    page.mkdir()
    (page / 'state.json').write_text(json.dumps(state))
    go = lambda: subprocess.run([sys.executable, str(SCRIPT), str(page / 'state.json')], env=env, check=True,
                                capture_output=True)
    go()
    assert '<article class="task r3"' in (tmp_path / 'hub/index.html').read_text()
    (tmp_path / 'tasks/suite.md').write_text('# suite')  # the mission was resumed under the same task
    go()
    assert '<article class="task r2"' in (tmp_path / 'hub/index.html').read_text()


def test_follow_up_task_on_a_finished_page_must_bring_steps(tmp_path):
    (tmp_path / 'p').mkdir()
    state = str(tmp_path / 'p/state.json')
    (tmp_path / 'p/state.json').write_text(json.dumps({'title': 'T', 'task': 'old', 'steps': [{'step': 'a', 'status': 'fait'}]}))
    refused = run(tmp_path, state, '--task', 'new')
    assert refused.returncode == 1 and '--add-step' in refused.stderr
    assert json.loads((tmp_path / 'p/state.json').read_text())['task'] == 'old'
    assert run(tmp_path, state, '--task', 'new', '--add-step', 'b').returncode == 0
    saved = json.loads((tmp_path / 'p/state.json').read_text())
    assert saved['task'] == 'new' and saved['steps'][-1] == {'step': 'b', 'status': 'à faire'}


def env_for(tmp_path):
    return {**{k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_SESSION_ID'},
            'PROGRESS_HUB': str(tmp_path / 'hub'), 'PROGRESS_TASKS': str(tmp_path / 'tasks/active'),
            'PROGRESS_ACTIVITY': str(tmp_path / 'activity'), 'PROGRESS_SESSIONS': str(tmp_path / 'sessions')}


def make(tmp_path, name, state, age=0):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    path = tmp_path / name / 'state.json'
    path.write_text(json.dumps(state))
    if age:
        os.utime(path, (path.stat().st_atime - age, path.stat().st_mtime - age))
    return str(path)


def script(tmp_path, *args, stdin=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=env_for(tmp_path), input=stdin, check=False)


def test_closed_task_finishes_and_forgotten_closure_is_flagged(tmp_path):
    for d in ('active', 'completed'):
        (tmp_path / 'tasks' / d).mkdir(parents=True)
    (tmp_path / 'tasks/completed/closed.md').write_text('#')
    (tmp_path / 'tasks/active/open.md').write_text('#')
    closed = make(tmp_path, 'a', {'title': 'Close', 'task': 'closed', 'steps': [{'step': 'a', 'status': 'en cours'}]})
    forgot = make(tmp_path, 'b', {'title': 'Oubli', 'task': 'open', 'steps': [{'step': 'a', 'status': 'fait'}]},
                  age=2 * 86400)
    assert script(tmp_path, closed).returncode == 0 and script(tmp_path, forgot).returncode == 0
    hub = (tmp_path / 'hub/index.html').read_text()
    assert re.search(r'<article class="task r3"[^>]*>.*?Close', hub, re.S)
    assert 'À clôturer' in hub and hub.index('Oubli') < hub.index('id="done"')


def test_old_finished_and_long_missing_pages_leave_the_hub(tmp_path):
    old = make(tmp_path, 'old', {'title': 'Vieille', 'steps': [{'step': 'a', 'status': 'fait'}]}, age=8 * 86400)
    recent = make(tmp_path, 'new', {'title': 'Récente', 'steps': [{'step': 'a', 'status': 'fait'}]}, age=86400)
    ghost, lost = '/nonexistent-mount/a/suivi/state.json', '/nonexistent-mount/b/suivi/state.json'
    (tmp_path / 'hub').mkdir()
    (tmp_path / 'hub/pages.json').write_text(json.dumps([old, recent, ghost, lost]))
    (tmp_path / 'hub/missing.json').write_text(json.dumps({lost: 0}))  # missing since 1970
    assert script(tmp_path, '--all').returncode == 0
    assert json.loads((tmp_path / 'hub/pages.json').read_text()) == [recent, ghost]
    assert ghost in json.loads((tmp_path / 'hub/missing.json').read_text())
    assert Path(old).exists()  # only the hub forgets


def test_activity_hook_logs_events_and_hub_shows_agents(tmp_path):
    (tmp_path / 'sessions').mkdir()
    (tmp_path / 'sessions/1.json').write_text(json.dumps(
        {'pid': os.getpid(), 'sessionId': 'live', 'name': 'main — Aube', 'status': 'busy', 'statusUpdatedAt': 0}))
    make(tmp_path, 'p', {'title': 'Mission A', 'agent': 'Aube', 'steps': [{'step': 'a', 'status': 'en cours'}]})
    hook = lambda flag, **kw: script(tmp_path, flag, stdin=json.dumps({'session_id': kw.pop('sid', 'live'), **kw}))
    for event in ({'hook_event_name': 'SessionStart'}, {'hook_event_name': 'UserPromptSubmit', 'prompt': 'secret'},
                  {'hook_event_name': 'Notification', 'notification_type': 'idle_prompt'},
                  {'hook_event_name': 'Notification', 'notification_type': 'permission_prompt'},
                  {'hook_event_name': 'UserPromptSubmit', 'sid': 'dead'}):
        result = hook('--answered' if event['hook_event_name'] == 'UserPromptSubmit' else '--activity', **event)
        assert result.returncode == 0 and result.stdout == result.stderr == ''
    lines = [json.loads(l) for f in (tmp_path / 'activity').glob('*.jsonl') for l in f.read_text().splitlines()]
    assert [e['ev'] for e in lines] == ['SessionStart', 'UserPromptSubmit', 'Notification', 'UserPromptSubmit']
    assert 'secret' not in json.dumps(lines) and lines[0]['name'] == 'main — Aube'
    assert script(tmp_path, str(tmp_path / 'p/state.json')).returncode == 0
    hub = (tmp_path / 'hub/index.html').read_text()
    agents = hub[hub.index('id="agents"'):]
    assert 'main — Aube' in agents and 'attend ta réponse' in agents and 'Mission A' in agents
    assert 'perdue' in agents  # the dead session ended without SessionEnd
    assert hub.index('main — Aube') < hub.index('dead'[:8])
    bad = script(tmp_path, '--activity', stdin='not json')
    assert bad.returncode == 0 and bad.stdout == bad.stderr == ''


def test_broken_page_never_borrows_the_state_of_its_neighbour(tmp_path):
    good = make(tmp_path, 'good', {'title': 'Good', 'agent': 'alpha', 'steps': [{'step': 'a', 'status': 'en cours'}]})
    bad = make(tmp_path, 'bad', {'title': 'Bad', 'agent': 'beta', 'steps': None})
    for order in ([bad, good], [good, bad]):
        (tmp_path / 'hub').mkdir(exist_ok=True)
        (tmp_path / 'hub/pages.json').write_text(json.dumps(order))
        result = script(tmp_path, '--all')
        assert result.returncode == 0 and 'Traceback' not in result.stderr
        hub = (tmp_path / 'hub/index.html').read_text()
        assert 'Good' in hub and 'beta' not in hub
        assert hub.count('bad/index.html') <= 1  # listed as a missing card at most, never under alpha's title


def test_open_question_on_a_finished_page_without_task_does_not_crash(tmp_path):
    state = {'title': 'Q', 'ask': 'Valider ?', 'steps': [{'step': 'a', 'status': 'fait'}]}
    result = render(tmp_path, state)
    assert result.returncode == 0 and 'Traceback' not in result.stderr
    assert 'Valider ?' in (tmp_path / 'hub/index.html').read_text()


def test_figure_outside_the_page_is_not_embedded(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR\0\0\0\x02\0\0\0\x01" + b"\0" * 20
    (tmp_path / 'secret.png').write_bytes(png)
    state = {'title': 'T', 'steps': [], 'figures': [{'file': '../secret.png', 'title': 'x'}]}
    assert render(tmp_path, state).returncode == 0
    assert base64.b64encode(png).decode() not in (tmp_path / 'page/index.html').read_text()


def test_wrongly_typed_figure_and_section_fields_are_readable_errors(tmp_path):
    for key, entry in (('figures', {'file': 3, 'title': 't'}), ('figures', {'file': 'a.png', 'title': 't', 'full': 5}),
                       ('sections', {'title': 't', 'items': 5})):
        result = render(tmp_path, {'title': 'T', 'steps': [], key: [entry]})
        assert result.returncode == 1 and 'Traceback' not in result.stderr and key in result.stderr


def test_serve_skips_malformed_registered_states(tmp_path):
    page = served_page(tmp_path)
    for name, text in (('list', '[1]'), ('nofig', '{"figures": null}')):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'state.json').write_text(text)
    pages = tmp_path / 'hub/pages.json'
    pages.write_text(json.dumps([str(tmp_path / n / 'state.json') for n in ('list', 'nofig')] + json.loads(pages.read_text())))
    server = Served(tmp_path)
    try:
        assert server.get(page + 'index.html')[0] == 200
        assert server.get(f'/tok/fs{tmp_path}/hub/index.html')[0] == 200
    finally:
        server.stop()


def test_all_excludes_forget_and_show(tmp_path):
    for flag in ('--forget', '--show'):
        result = run(tmp_path, '--all', flag)
        assert result.returncode == 2 and 'Traceback' not in result.stderr and '--all excludes' in result.stderr


def test_section_table_renders_and_section_flag_replaces_by_title(tmp_path):
    state = {'title': 'T', 'steps': [], 'sections': [{'title': 'Notes', 'items': ['a']}]}
    assert render(tmp_path, state).returncode == 0
    table = json.dumps({'columns': ['seed', 'succès'], 'rows': [[1, '<b>90 %</b>'], [2, '85 %']]})
    for value in (table, '["x"]', table):
        result = render(tmp_path, json.loads((tmp_path / 'page/state.json').read_text()), '--section', f'Résultats::{value}')
        assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / 'page/state.json').read_text())
    assert [s['title'] for s in saved['sections']] == ['Notes', 'Résultats'] and 'items' not in saved['sections'][1]
    page = (tmp_path / 'page/index.html').read_text()
    assert '<th>succès</th>' in page and '<td>1</td>' in page and '&lt;b&gt;90 %&lt;/b&gt;' in page
    for value in ('{"columns": ["a"], "rows": [[1, 2]]}', 'nope', '3'):
        result = render(tmp_path, saved, '--section', f'R::{value}')
        assert result.returncode == 1 and 'Traceback' not in result.stderr and '--section' in result.stderr
    bad = {'title': 'T', 'steps': [], 'sections': [{'title': 'R', 'table': {'columns': ['a'], 'rows': [[]]}}]}
    result = render(tmp_path, bad)
    assert result.returncode == 1 and 'sections' in result.stderr
