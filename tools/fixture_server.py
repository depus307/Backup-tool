"""Offline loopback scenarios. No external resources or application logic."""
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).resolve().parents[1] / 'fixtures' / 'assets'


def run_worker(payload):
    from backuptool.controller import WorkerSession

    session = WorkerSession()
    session.start(payload=payload)
    events = []
    deadline = time.monotonic() + 150
    try:
        while session.state != 'finished':
            events.extend(session.poll())
            if time.monotonic() > deadline:
                raise TimeoutError('Worker did not finish')
            time.sleep(.05)
        assert session.final['outcome'] == 'success', session.final
        return session.final, events
    finally:
        session.close()


def html(body, script=''):
    return ('''<!doctype html><html lang="ru"><meta charset="utf-8"><title>BackupTool fixture</title>
<style>@font-face{font-family:Fixture;src:url('/assets/fixture.ttf')}body{font:18px Fixture,Arial;margin:40px;color:#17342f}table{border-collapse:collapse}td,th{border:1px solid #467;padding:12px}img{max-width:100%}</style>
<body>''' + body + '<script>' + script + '</script></body></html>').encode('utf-8')


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, sleep=time.sleep):
        super().__init__(('127.0.0.1', port), Handler)
        self.sleep = sleep
        self.requests = Counter()
        self.guard = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        with self.server.guard:
            self.server.requests[path] += 1
        status, mime, headers = 200, 'text/html; charset=utf-8', {}
        if path == '/closed':
            self.close_connection = True
            return
        if path == '/timeout':
            self.server.sleep(35)
        if path in ('/', '/simple'):
            body = html('<h1>Контрольная компания</h1><p>SIMPLE-READY: полезная страница</p><table><tr><th>ИНН</th><th>Название</th></tr><tr><td>0123456789</td><td>Тест</td></tr></table>')
        elif path == '/typography':
            body = html('<h1>Шрифты и изображения</h1><p>FONT-READY: Кириллица Ёжик</p><img src="/assets/image.svg" alt="Контрольное изображение">')
        elif path == '/long':
            body = html('<h1>Длинный документ</h1>' + ''.join(f'<p style="height:100px">Строка {i}: LONG-{i:03d}</p>' for i in range(40)))
        elif path == '/spa':
            body = html('<main id="app">INITIAL-PLACEHOLDER</main>', "setTimeout(()=>{document.querySelector('#app').textContent='SPA-READY: динамическое содержимое'},150)")
        elif path == '/lazy':
            body = html('<h1>LAZY-READY</h1><div style="height:1500px">Прокрутите вниз</div><img id="lazy" loading="lazy" data-src="/assets/image.svg">', "new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)e.target.src=e.target.dataset.src})).observe(document.querySelector('#lazy'))")
        elif path in ('/challenge', '/sorry/index'):
            body = html('<main id="challenge"><h1>Подтвердите, что вы человек</h1><p>CHALLENGE-PENDING</p><button onclick="document.querySelector(\'#challenge\').innerHTML=\'<h1>MANUAL-READY</h1><p>Открытая информация компании</p>\'">Подтвердить на тестовом стенде</button></main>')
        elif path == '/article':
            body = html('<h1>Обычная статья</h1><p>Тема статьи: CAPTCHA. ARTICLE-READY</p>')
        elif path == '/js-redirect':
            body = html('<p>JS-REDIRECT</p>', "location.replace('/simple?via=js')")
        elif path == '/redirect':
            status, headers, body = 302, {'Location': '/simple?via=http'}, b''
        elif path.startswith('/status/') and path.rsplit('/', 1)[1] in (
                '200', '400', '402', '403', '404', '429', '500', '502', '503', '504'):
            status = int(path.rsplit('/', 1)[1])
            headers = {'Retry-After': '2'} if status in (429, 503) else {}
            body = html(f'<h1>HTTP {status}</h1>')
        elif path in ('/expired', '/expired.pdf'):
            status = 402
            body = html('<h1>402 Please renew your subscription</h1><p>TILDA-EXPIRED</p>')
        elif path == '/redirect-error':
            status, headers, body = 302, {'Location': '/expired'}, b''
        elif path == '/login':
            body = html('<h1>Войдите, чтобы продолжить</h1>')
        elif path == '/blank':
            body = html('')
        elif path == '/blank-styled':
            body = html('<style>html{background:white} h1{visibility:hidden}</style><h1>HIDDEN</h1>')
        elif path == '/image-only':
            body = html('<img src="/assets/image.svg" alt="">')
        elif path == '/delay':
            delay = min(2.0, max(0.0, float(query.get('seconds', ['0.2'])[0])))
            self.server.sleep(delay)
            body = html('<h1>DELAY-READY</h1>')
        elif path in ('/inline.pdf', '/attachment.pdf', '/no-extension', '/original.docx', '/fake.pdf', '/truncated.pdf'):
            if path == '/fake.pdf':
                body, mime = html('<h1>Доступ запрещён</h1>'), 'application/pdf'
            elif path == '/original.docx':
                body = (ASSETS / 'original.docx').read_bytes()
                mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                headers = {'Content-Disposition': 'attachment; filename="../../unsafe.docx"'}
            else:
                body, mime = (ASSETS / 'useful.pdf').read_bytes(), 'application/pdf'
                if path == '/attachment.pdf':
                    headers = {'Content-Disposition': 'attachment; filename="original.pdf"'}
                if path == '/truncated.pdf':
                    headers['Content-Length'] = str(len(body) + 100)
        elif path.startswith('/assets/') and path.removeprefix('/assets/') in ('image.svg', 'fixture.ttf'):
            name = path.removeprefix('/assets/')
            body = (ASSETS / name).read_bytes()
            mime = 'image/svg+xml' if name.endswith('.svg') else 'font/ttf'
        else:
            status, body = 404, html('<h1>HTTP 404</h1>')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', headers.pop('Content-Length', str(len(body))))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if path == '/truncated.pdf':
            self.close_connection = True


@contextmanager
def serving(port=0, sleep=time.sleep):
    server = FixtureServer(port, sleep)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with serving(args.port) as (_, base):
        print(base, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
