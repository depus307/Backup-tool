"""Bounded browser jobs. Runs only in the archive's owning worker process."""
import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from html import escape
import os
import re
from pathlib import Path
import shutil
import ssl
import time
import traceback
from urllib.parse import urlsplit
import uuid
import zipfile

from .archive import Archive, export_reports, GOOD, LABELS
from .evidence import dated_pdf, original_path

MAX_BYTES = 100 * 1024 * 1024
DOCUMENTS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.rtf', '.odt', '.ods', '.odp'}
MIMES = {'application/pdf': '.pdf', 'application/msword': '.doc',
         'application/vnd.ms-excel': '.xls', 'application/vnd.ms-powerpoint': '.ppt',
         'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
         'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
         'application/rtf': '.rtf', 'text/rtf': '.rtf',
         'application/vnd.oasis.opendocument.text': '.odt',
         'application/vnd.oasis.opendocument.spreadsheet': '.ods'}


class NeedsHelp(Exception):
    pass


class HtmlDocument(Exception):
    """A document URL returned HTML which must be captured in the browser."""


@dataclass
class CaptureResult:
    extension: str
    final_url: str
    http_status: int | None
    checked_at: str
    reason: str = ''
    quality: str = ''
    original: Path | None = None


def capture_time():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def capture_reason(status, url, title, text, navigation_note=''):
    notes = []
    if status and status >= 400:
        notes.append(f'HTTP {status}: сохранена страница ошибки сайта')
    warning = blocked(url, title, text)
    if warning:
        notes.append(warning)
    if not text.strip():
        notes.append('Страница без текста; сохранено отображаемое состояние')
    if navigation_note:
        notes.append(navigation_note)
    return '; '.join(notes)


class HttpFailure(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        try:
            self.retry_after = min(10, max(0, float(retry_after)))
        except (ValueError, TypeError):
            self.retry_after = 0
        super().__init__(f'HTTP {code}')


def blocked(url, title, text):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if ('google.' in host and p.path.startswith('/sorry')):
        return 'Сохранена страница проверки Google /sorry'
    title, text = title.casefold(), text[:4000].casefold()
    signals = ('подтвердите, что вы человек', 'verify you are human', 'checking your browser',
               'just a moment', 'проверка браузера', 'unusual traffic', 'доступ запрещён',
               'access denied', 'robot check', 'attention required!')
    if any(signal in title or signal in text[:1500] for signal in signals):
        return 'Сохранена страница проверки или запрета доступа'
    if len(text) < 1500 and any(s in text for s in ('sign in to continue', 'войдите, чтобы продолжить')):
        return 'Сохранена страница с требованием входа на сайт'
    return ''


async def launch(playwright, channel='chrome', headed=False):
    channel = 'msedge' if channel == 'msedge' else 'chrome'
    try:
        return await playwright.chromium.launch(channel=channel, headless=not headed,
                                                args=['--disable-session-crashed-bubble'])
    except Exception as error:
        name = 'Microsoft Edge' if channel == 'msedge' else 'Google Chrome'
        detail = str(error).split('\n', 1)[0]
        raise RuntimeError(f'Не удалось запустить {name}. Проверьте, что браузер установлен. {detail}') from error


class ContextPool:
    def __init__(self, browser, limit):
        self.browser, self.limit = browser, limit
        self.items = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, origin):
        async with self.lock:
            if origin not in self.items:
                if len(self.items) >= self.limit:
                    idle = min((k for k, v in self.items.items() if v[1] == 0),
                               key=lambda k: self.items[k][2], default=None)
                    if idle is None:
                        raise RuntimeError('Нет свободного контекста браузера')
                    await self.items.pop(idle)[0].close()
                ctx = await self.browser.new_context(viewport={'width': 1440, 'height': 1000},
                                                      locale='ru-RU', accept_downloads=True)
                self.items[origin] = [ctx, 0, time.monotonic()]
            item = self.items[origin]
            item[1] += 1
        try:
            yield item[0]
        finally:
            async with self.lock:
                item[1] -= 1
                item[2] = time.monotonic()


async def document(url, staged, context=None):
    """Only used for a document candidate/response, never to preflight HTML pages."""
    import httpx
    cookies = {}
    agent = 'BackupToolNext/1.0'
    if context:
        cookies = {c['name']: c['value'] for c in await context.cookies([url])}
        agent = await context.pages[0].evaluate('navigator.userAgent') if context.pages else agent
    # Chrome and the standalone downloader must both trust the Windows certificate store.
    verify = True if os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR') else ssl.create_default_context()
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, cookies=cookies, verify=verify,
                                headers={'User-Agent': agent}) as client:
        async with client.stream('GET', url) as response:
            mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
            if response.status_code >= 400:
                if mime in ('text/html', 'application/xhtml+xml'):
                    raise HtmlDocument(str(response.url))
                raise HttpFailure(response.status_code, response.headers.get('retry-after'))
            if response.status_code == 206:
                raise NeedsHelp('Сервер вернул только часть документа')
            suffix = Path(urlsplit(str(response.url)).path).suffix.lower()
            chunks = response.aiter_bytes(chunk_size=8192)
            first = await anext(chunks, b'')
            if first.startswith(b'%PDF-'):
                extension = '.pdf'
            elif mime in ('text/html', 'application/xhtml+xml'):
                raise HtmlDocument(str(response.url))
            else:
                extension = MIMES.get(mime, suffix if suffix in DOCUMENTS else '')
            if not extension:
                raise NeedsHelp('Ответ сервера не распознан как документ')
            size = len(first)
            with staged.open('wb') as stream:
                stream.write(first)
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise NeedsHelp('Документ больше 100 МБ; сохраните его вручную')
                    stream.write(chunk)
            if not size:
                raise ValueError('Получен пустой документ')
            if extension == '.pdf' and not first.startswith(b'%PDF-'):
                raise ValueError('Ответ сервера не содержит сигнатуру PDF')
            if extension in ('.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp'):
                with zipfile.ZipFile(staged) as z:
                    if '[Content_Types].xml' not in z.namelist() and 'mimetype' not in z.namelist():
                        raise ValueError('Неверная структура документа')
            elif extension in ('.doc', '.xls', '.ppt'):
                with staged.open('rb') as stream:
                    if stream.read(8) != bytes.fromhex('D0CF11E0A1B11AE1'):
                        raise ValueError('Неверная сигнатура документа Office')
            elif extension == '.rtf' and not staged.read_bytes()[:20].lstrip().startswith(b'{\\rtf'):
                raise ValueError('Неверная сигнатура RTF')
            checked_at = capture_time()
            final_url = str(response.url)
            if extension == '.pdf':
                return finish_pdf(staged, final_url, response.status_code, checked_at)
            return CaptureResult(extension, final_url, response.status_code, checked_at)


def finish_pdf(staged, url, status, checked_at):
    original = original_path(staged)
    os.replace(staged, original)
    dated_pdf(original, staged, url, checked_at, status)
    return CaptureResult('.pdf', url, status, checked_at, quality='dated_document', original=original)


async def browser_document(page, url, staged, pdf=False):
    import httpx

    try:
        return await document(url, staged, page.context)
    except httpx.RequestError:
        if not pdf:
            raise
    # Some servers send a chain accepted by Chrome but rejected by the separate
    # downloader. Retry using Chrome's own verified HTTPS connection and cookies.
    parts = urlsplit(url)
    helper = f'{parts.scheme}://{parts.netloc}/.backuptool-{uuid.uuid4().hex}'
    tab = await page.context.new_page()
    try:
        await tab.route(helper, lambda route: route.fulfill(content_type='text/html', body='<!doctype html>'))
        await tab.goto(helper, wait_until='domcontentloaded')
        async with asyncio.timeout(35), tab.expect_download(timeout=35000) as download_info:
            info = await tab.evaluate("""async ({url, limit}) => {
                const response = await fetch(url, {credentials: 'same-origin'});
                if (!response.ok || response.status === 206 || !response.body)
                    throw new Error('HTTP ' + response.status);
                const reader = response.body.getReader();
                const chunks = [];
                let size = 0;
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    size += value.byteLength;
                    if (size > limit) {
                        await reader.cancel();
                        throw new Error('PDF exceeds download limit');
                    }
                    chunks.push(value);
                }
                const blob = new Blob(chunks, {type: 'application/pdf'});
                if (await blob.slice(0, 5).text() !== '%PDF-')
                    throw new Error('The response is not a PDF');
                const link = document.createElement('a');
                link.href = URL.createObjectURL(blob);
                link.download = 'source.pdf';
                document.body.appendChild(link);
                link.click();
                return {url: response.url, status: response.status};
            }""", {'url': url, 'limit': MAX_BYTES})
            download = await download_info.value
            await download.save_as(staged)
        return finish_pdf(staged, info['url'], info['status'], capture_time())
    finally:
        await tab.close()


async def settle(page):
    # Finite waits; no networkidle requirement on permanently busy sites.
    await page.wait_for_timeout(400)
    await page.evaluate('''async () => {
        await Promise.race([document.fonts.ready,new Promise(r=>setTimeout(r,2500))]);
        const height=Math.min(document.documentElement.scrollHeight,18000);
        for(let y=0;y<height;y+=900){window.scrollTo(0,y);await new Promise(r=>setTimeout(r,60));}
        window.scrollTo(0,0);
        await Promise.race([Promise.all([...document.images].map(i=>i.complete?Promise.resolve():
            new Promise(r=>{i.addEventListener('load',r,{once:true});i.addEventListener('error',r,{once:true});}))),
            new Promise(r=>setTimeout(r,2500))]);
    }''')


async def print_page(page, staged, checked_at, http_status, *, url=None):
    url = url or page.url
    checked_at = checked_at or capture_time()
    address = escape(url)
    visible = escape(url if len(url) <= 500 else url[:497] + '…')
    status = f' · HTTP {http_status}' if http_status else ''
    footer = ('<div style="margin:0 8mm;width:100%;font:9px sans-serif;overflow-wrap:anywhere">'
              f'Адрес: <a href="{address}">{visible}</a><br>'
              f'Дата и время снимка: {escape(checked_at)}{status}</div>')
    await page.emulate_media(media='screen')
    await page.pdf(path=str(staged), format='A4', print_background=True, display_header_footer=True,
                   header_template='<span></span>', footer_template=footer,
                   margin={'top': '10mm', 'bottom': '28mm', 'left': '8mm', 'right': '8mm'})
    return url


def access_code(detail):
    match = re.search(r'\bERR_[A-Z0-9_]+\b', detail)
    return match.group() if match else ('TIMEOUT' if 'timeout' in detail.lower() else 'Ошибка навигации')


async def save_access_error(page, staged, url, detail, status=None, *, wait_for_error=False):
    checked_at = capture_time()
    code = access_code(detail)
    screenshot = None
    try:
        if wait_for_error and page.url != 'chrome-error://chromewebdata/':
            await page.wait_for_url('chrome-error://chromewebdata/', wait_until='commit', timeout=900)
        if page.url == 'chrome-error://chromewebdata/':
            await page.locator('body').wait_for(state='visible', timeout=1000)
            screenshot = await page.screenshot(timeout=2000)
    except Exception:
        pass
    evidence = await page.context.new_page()
    try:
        heading = 'Экран ошибки браузера' if screenshot else 'Отчёт BackupTool о попытке доступа'
        picture = (f'<img alt="Экран ошибки Chrome" src="data:image/png;base64,{b64encode(screenshot).decode()}">'
                   if screenshot else '')
        await evidence.set_content(
            '<meta charset="utf-8"><style>body{font:15px Arial;color:#202124}'
            'img{display:block;width:100%;max-height:185mm;object-fit:contain}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            f'<h1>{heading}</h1><p>{escape(code)}</p>{picture}'
            f'<pre>{escape(detail[:1600])}</pre>')
        await print_page(evidence, staged, checked_at, status, url=url)
    finally:
        await evidence.close()
    return CaptureResult('.pdf', url, status, checked_at, f'{heading} · {code}',
                         'access_screenshot' if screenshot else 'access_report')


def document_response(headers):
    mime = headers.get('content-type', '').split(';')[0].strip().lower()
    disposition = headers.get('content-disposition', '').lower()
    return mime in MIMES or 'attachment' in disposition or mime == 'application/octet-stream'


def pdf_response(headers, url):
    mime = headers.get('content-type', '').split(';')[0].strip().lower()
    return mime == 'application/pdf' or Path(urlsplit(url).path).suffix.lower() == '.pdf'


async def pdf_viewer(page):
    return await page.evaluate("""() => document.contentType === 'application/pdf' ||
        (document.body?.children.length === 1 && !!document.body.querySelector(
            'embed[type="application/pdf"], embed[type="application/x-google-chrome-pdf"]'))""")


async def capture(pool, url, staged, pdf_lock):
    import httpx
    p = urlsplit(url)
    if Path(p.path).suffix.lower() in DOCUMENTS:
        try:
            return await document(url, staged)
        except (HtmlDocument, HttpFailure, httpx.RequestError):
            pass
    origin = (p.scheme, p.netloc)
    async with pool.acquire(origin) as context:
        page = await context.new_page()
        main = {}

        def observe(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                main.update(url=response.url, headers=response.headers, status=response.status)

        page.on('response', observe)
        failed = {}

        def observe_failure(request):
            if request.is_navigation_request() and request.frame == page.main_frame:
                failed.update(url=request.url, detail=request.failure or '')

        page.on('requestfailed', observe_failure)
        try:
            navigation_note = ''
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as error:
                if main and main['status'] < 400 and document_response(main['headers']):
                    return await browser_document(page, main['url'], staged, pdf_response(main['headers'], main['url']))
                if (not main or not page.url.startswith(('http://', 'https://'))
                        or main.get('status') in (204, 205)):
                    async with pdf_lock:
                        target = failed.get('url') or main.get('url') or url
                        detail = str(error)
                        status = main.get('status') if main.get('url') == target else None
                        return await save_access_error(page, staged, target, detail, status, wait_for_error=True)
                navigation_note = f'Загрузка завершилась не полностью: {type(error).__name__}'
            if main and main['status'] < 400:
                headers, final = main['headers'], main['url']
                if document_response(headers):
                    return await browser_document(page, final, staged, pdf_response(headers, final))
                if await pdf_viewer(page):
                    return await browser_document(page, final, staged, pdf=True)
            if not main:
                async with pdf_lock:
                    return await save_access_error(page, staged, url, 'Сервер не вернул страницу для сохранения')
            await settle(page)
            title = await page.title()
            text = await page.locator('body').inner_text(timeout=5000)
            status = main.get('status')
            reason = capture_reason(status, page.url, title, text, navigation_note)
            async with pdf_lock:
                checked_at = capture_time()
                final_url = await print_page(page, staged, checked_at, status)
            return CaptureResult('.pdf', final_url, status, checked_at, reason)
        finally:
            await page.close()


class Runner:
    def __init__(self, connection, session_id, payload):
        self.connection, self.session, self.payload = connection, session_id, payload
        self.cancelled = asyncio.Event()
        self.last_progress = 0
        self.done = 0

    def send(self, kind, **values):
        self.connection.send({'type': kind, 'session_id': self.session, **values})

    def log(self, state, url, reason=''):
        text = f'{LABELS[state]} · {url}'
        if reason.strip():
            text += f' — {reason.strip().splitlines()[0]}'
        self.send('log', at=time.time(), text=text)

    def progress(self, total, text='', force=False):
        if force or time.monotonic() - self.last_progress >= .25:
            self.send('progress', done=self.done, total=total, text=text)
            self.last_progress = time.monotonic()

    async def watch_cancel(self):
        while True:
            if self.connection.poll():
                message = self.connection.recv()
                if message.get('session_id') == self.session and message.get('type') == 'cancel':
                    self.cancelled.set()
                    return
            await asyncio.sleep(.1)

    async def batch(self, archive, list_id):
        from playwright.async_api import async_playwright
        run = uuid.uuid4().hex
        archive.db.execute('INSERT INTO runs(id,list_id,started) VALUES(?,?,?)', (run, list_id, time.time()))
        archive.db.commit()
        refresh = self.payload.get('refresh', False)
        only_actions = self.payload.get('only_actions', False)
        queue = []
        for resource in archive.resources(list_id):
            if resource['url'].startswith('invalid:'):
                continue
            if archive.reusable(resource) and (not refresh or only_actions):
                continue
            queue.append(resource)
        total = len(queue)
        self.progress(total, f'К обработке ресурсов: {total}', True)
        outcome = 'error'
        try:
            if not queue:
                outcome = 'success'
                return outcome
            limit = 2 if self.payload.get('gentle') else 4
            host_limit = 1 if self.payload.get('gentle') else 2
            work = asyncio.Queue()
            for item in queue:
                work.put_nowait(item)
            host_locks = {}
            pdf_lock = asyncio.Semaphore(1)
            async with async_playwright() as playwright:
                browser = await launch(playwright, self.payload.get('browser', 'chrome'))
                archive.db.execute('UPDATE runs SET browser=? WHERE id=?', (browser.version, run))
                archive.db.commit()
                pool = ContextPool(browser, limit)
                work_dir = archive.work_dir(run)

                async def consume():
                    while not work.empty() and not self.cancelled.is_set():
                        resource = work.get_nowait()
                        host = urlsplit(resource['url']).netloc
                        lock = host_locks.setdefault(host, asyncio.Semaphore(host_limit))
                        async with lock:
                            if self.cancelled.is_set():
                                return
                            attempt = archive.begin(resource['id'], run)
                            self.progress(total, resource['url'])
                            staged = work_dir / (attempt + '.part')
                            try:
                                async with asyncio.timeout(90):
                                    for retry in range(2):
                                        try:
                                            result = await capture(pool, resource['url'], staged, pdf_lock)
                                            break
                                        except Exception as error:
                                            transient = (isinstance(error, HttpFailure) and error.code in (429, 502, 503, 504))
                                            transient |= 'Timeout' in type(error).__name__
                                            if retry or not transient:
                                                raise
                                            await asyncio.sleep(max(1, getattr(error, 'retry_after', 0)))
                                    owner = archive.db.execute('SELECT inn,company FROM input_rows WHERE resource_id=? '
                                                               'ORDER BY id LIMIT 1', (resource['id'],)).fetchone()
                                    name = '_'.join(v for v in owner if v) if owner else host
                                    archive.publish(attempt, staged, name or host, result.extension,
                                                    result.quality or ('automatic' if result.extension == '.pdf' else 'original'),
                                                    result.final_url, result.reason, result.http_status, result.checked_at,
                                                    result.original)
                            except asyncio.CancelledError:
                                archive.fail(attempt, 'interrupted', 'Остановлено пользователем')
                                self.log('interrupted', resource['url'])
                                raise
                            except NeedsHelp as error:
                                archive.fail(attempt, 'needs_help', str(error))
                                self.log('needs_help', resource['url'], str(error))
                            except Exception as error:
                                if self.payload.get('_diagnostics'):
                                    self.send('diagnostic', traceback=traceback.format_exc())
                                reason = f'{type(error).__name__}: {str(error)[:1600]}'
                                archive.fail(attempt, 'error', reason)
                                self.log('error', resource['url'], reason)
                            else:
                                self.log('ready' if result.extension == '.pdf' else 'document',
                                         resource['url'], result.reason)
                            finally:
                                # Only this attempt's registered staging file is removed.
                                staged.unlink(missing_ok=True)
                                original_path(staged).unlink(missing_ok=True)
                            self.done += 1
                            self.progress(total, resource['url'])

                tasks = [asyncio.create_task(consume()) for _ in range(limit)]
                cancel = asyncio.create_task(self.watch_cancel())
                group = asyncio.gather(*tasks)
                try:
                    await asyncio.wait([group, cancel], return_when=asyncio.FIRST_COMPLETED)
                    if self.cancelled.is_set():
                        outcome = 'cancelled'
                        group.cancel()
                    await asyncio.gather(group, return_exceptions=False)
                    if not self.cancelled.is_set():
                        outcome = 'success'
                except asyncio.CancelledError:
                    outcome = 'cancelled'
                finally:
                    cancel.cancel()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await browser.close()
            return outcome
        finally:
            archive.db.execute('UPDATE runs SET ended=?,outcome=? WHERE id=?', (time.time(), outcome, run))
            archive.db.commit()
            self.progress(total, 'Остановлено' if outcome == 'cancelled' else 'Обработка завершена', True)

    async def manual(self, archive, list_id):
        from playwright.async_api import async_playwright
        row = next((r for r in archive.rows(list_id) if r['id'] == self.payload['row_id']), None)
        if not row:
            raise ValueError('Строка отсутствует в выбранном списке')
        resource = archive.db.execute('SELECT url FROM resources WHERE id=?', (row['resource_id'],)).fetchone()
        if not resource or resource['url'].startswith('invalid:'):
            raise ValueError('Исправьте исходный URL и загрузите список заново')
        run = uuid.uuid4().hex
        archive.db.execute('INSERT INTO runs(id,list_id,started) VALUES(?,?,?)', (run, list_id, time.time()))
        archive.db.commit()
        outcome = 'cancelled'
        async with async_playwright() as playwright:
            browser = await launch(playwright, self.payload.get('browser', 'chrome'), headed=True)
            context = await browser.new_context(accept_downloads=True, viewport=None)
            responses = {}
            failures = {}

            def observe(response):
                if response.request.is_navigation_request() and response.frame == response.frame.page.main_frame:
                    browser_page = response.frame.page
                    responses[browser_page] = (response.url, response.status, response.headers)
                    failures.pop(browser_page, None)

            def observe_failure(request):
                if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
                    failures[request.frame.page] = (request.url, request.failure or '')

            context.on('response', observe)
            context.on('requestfailed', observe_failure)
            page = await context.new_page()
            try:
                try:
                    await page.goto(resource['url'], wait_until='domcontentloaded', timeout=30000)
                except Exception as error:
                    failures.setdefault(page, (resource['url'], str(error)))
                    self.send('notice', text=f'Браузер открыт. Навигация: {str(error)[:300]}')
                self.send('manual_ready')
                last_tabs = 0
                while browser.is_connected() and context.pages:
                    if time.monotonic() - last_tabs > 1:
                        self.send('tabs', tabs=[{'index': i, 'url': failures.get(p, (p.url,))[0]
                                                if p.url.startswith('chrome-error:') else p.url}
                                               for i, p in enumerate(context.pages)])
                        last_tabs = time.monotonic()
                    if self.connection.poll():
                        command = self.connection.recv()
                        if command.get('session_id') != self.session:
                            continue
                        if command.get('type') == 'cancel':
                            break
                        if command.get('type') == 'save_manual':
                            pages = context.pages
                            index = command.get('tab', 0)
                            if not 0 <= index < len(pages):
                                self.send('notice', text='Вкладка закрыта; выберите открытую вкладку')
                                continue
                            selected = pages[index]
                            attempt = archive.begin(row['resource_id'], run)
                            staged = archive.work_dir(run) / (attempt + '.part')
                            try:
                                async with asyncio.timeout(45):
                                    result = await self.save_tab(selected, staged, responses.get(selected),
                                                                 failures.get(selected))
                                    archive.publish(attempt, staged, row['company'] or row['inn'],
                                                    extension=result.extension, quality=result.quality or 'manual',
                                                    final_url=result.final_url, reason=result.reason,
                                                    http_status=result.http_status, checked_at=result.checked_at,
                                                    original=result.original)
                            except Exception as error:
                                archive.fail(attempt, 'needs_help', f'Ручное сохранение: {error}')
                                self.send('notice', text=f'Не сохранено: {error}. Можно повторить или прикрепить PDF.')
                            else:
                                outcome = 'success'
                                self.log('ready' if result.extension == '.pdf' else 'document',
                                         result.final_url, result.reason)
                                break
                            finally:
                                staged.unlink(missing_ok=True)
                                original_path(staged).unlink(missing_ok=True)
                    await asyncio.sleep(.15)
            finally:
                await browser.close()
                archive.db.execute('UPDATE runs SET ended=?,outcome=? WHERE id=?', (time.time(), outcome, run))
                archive.db.commit()
        return outcome

    async def save_tab(self, selected, staged, response, failure):
        url = selected.url
        observed_url, status, headers = response or ('', None, {})
        if failure and (url.startswith('chrome-error:') or url == 'about:blank'):
            target, detail = failure
            if 'ERR_ABORTED' in detail and observed_url == target and document_response(headers):
                return await browser_document(selected, target, staged, pdf_response(headers, target))
            return await save_access_error(selected, staged, target, detail,
                                           status if observed_url == target else None, wait_for_error=True)
        if not url.startswith(('http://', 'https://')):
            raise NeedsHelp('Для вкладки отсутствует адрес проверки')
        if observed_url.split('#')[0] != url.split('#')[0]:
            status, headers = None, {}
        if status is None or status < 400:
            if document_response(headers):
                return await browser_document(selected, url, staged, pdf_response(headers, url))
            if await pdf_viewer(selected):
                return await browser_document(selected, url, staged, pdf=True)
        text = await selected.locator('body').inner_text(timeout=5000)
        reason = capture_reason(status, url, await selected.title(), text)
        checked_at = capture_time()
        final_url = await print_page(selected, staged, checked_at, status)
        return CaptureResult('.pdf', final_url, status, checked_at, reason)

    async def execute(self):
        archive = Archive(self.payload['archive'])
        try:
            operation = self.payload.get('operation', 'batch')
            list_id = self.payload.get('list_id')
            if self.payload.get('source'):
                self.send('notice', text='Чтение списка…')
                list_id = archive.import_source(self.payload['source'])
            if not list_id:
                row = archive.db.execute('SELECT id FROM lists ORDER BY created DESC LIMIT 1').fetchone()
                if not row:
                    raise ValueError('Сначала загрузите список XLSX/CSV')
                list_id = row['id']
            self.send('loaded', list_id=list_id)
            if operation == 'import':
                return {'outcome': 'success', 'list_id': list_id}
            if operation == 'manual':
                outcome = await self.manual(archive, list_id)
            elif operation == 'attach':
                row = next(r for r in archive.rows(list_id) if r['id'] == self.payload['row_id'])
                run = uuid.uuid4().hex
                attempt = archive.begin(row['resource_id'], run)
                staged = archive.work_dir(run) / (attempt + '.part')
                try:
                    shutil.copyfile(self.payload['pdf'], staged)
                    archive.publish(attempt, staged, row['company'] or row['inn'], quality='manual_attachment',
                                    final_url=row['url'])
                except Exception as error:
                    archive.fail(attempt, 'needs_help', f'Прикрепление: {error}')
                    raise
                finally:
                    staged.unlink(missing_ok=True)
                self.log('ready', row['url'])
                outcome = 'success'
            elif operation == 'verify':
                resources = archive.resources(list_id)
                for item in resources:
                    if item['state'] in GOOD and not archive.reusable(item, full=True):
                        with archive.db:
                            archive.db.execute("UPDATE resources SET state='error',reason=? WHERE id=?",
                                               ('Файл отсутствует, повреждён или изменён; нужен повтор', item['id']))
                outcome = 'success'
            elif operation == 'export':
                outcome = 'success'
            else:
                outcome = await self.batch(archive, list_id)
            try:
                report = export_reports(archive, list_id)
                self.send('report', path=report)
                report_error = ''
            except Exception as error:
                report_error = str(error)
                self.send('notice', text=f'PDF учтены в архиве, но отчёт не записан: {error}')
            counts = {}
            for row in archive.rows(list_id):
                counts[row['state']] = counts.get(row['state'], 0) + 1
            return {'outcome': outcome, 'list_id': list_id, 'counts': counts, 'report_error': report_error}
        finally:
            archive.close()


def run_job(connection, session_id, payload):
    # Windows job object is owned by this worker: forced closure also stops only its browser tree.
    global _worker_job
    if os.name == 'nt':
        from .job import own_process_tree
        _worker_job = own_process_tree()
    return asyncio.run(Runner(connection, session_id, payload).execute())
