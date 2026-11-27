"""Local archive, source rows and recoverable publication of artifacts."""
import csv
from contextlib import suppress
import ctypes
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .delivery import is_network_path, publish_file
from .evidence import format_capture_time, original_path

LABELS = {'pending': 'В очереди', 'running': 'Обработка', 'ready': 'Готово',
          'document': 'Оригинал документа', 'needs_help': 'Нужна помощь',
          'error': 'Ошибка', 'saved_error': 'Сохранено как ошибка', 'interrupted': 'Прервано'}
GOOD = ('ready', 'document')
SAVED = (*GOOD, 'saved_error')


def result_label(row):
    label = LABELS.get(row['state'], row['state'])
    status = row.get('http_status')
    quality = row.get('quality', '')
    if row['state'] == 'saved_error' or (row['state'] in GOOD and (quality.startswith('access_')
                                                                 or row.get('reason') or (status or 0) >= 400)):
        return LABELS['saved_error'] + (f' · HTTP {status}' if (status or 0) >= 400 else '')
    if row['state'] in GOOD and quality == 'dated_document':
        return 'Документ сохранён'
    return label


def backup_name(company, inn, url):
    company = company.strip() or (urlsplit(url).hostname or 'site')
    company = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', '-', company).strip(' .-') or 'site'
    inn = re.sub(r'[^\w-]+', '-', inn).strip('-')
    company = company[:90].rstrip(' .-')
    return f'{inn[:30]}_{company}' if inn else company


def hide_service_file(path):
    if os.name == 'nt':
        kernel = ctypes.windll.kernel32
        attributes = kernel.GetFileAttributesW(str(path))
        if attributes != -1:
            kernel.SetFileAttributesW(str(path), attributes | 2)  # FILE_ATTRIBUTE_HIDDEN


def normalized_url(value):
    value = str(value).strip()
    if not value:
        raise ValueError('Пустой URL')
    if '://' not in value:
        value = 'https://' + value
    p = urlsplit(value)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Нужен адрес HTTP/HTTPS без логина и пароля')
    host = p.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    if port and (p.scheme.lower(), port) not in (('http', 80), ('https', 443)):
        host += ':' + str(port)
    return urlunsplit((p.scheme.lower(), host, p.path or '/', p.query, p.fragment))


def _value(value, fmt=''):
    if value is None:
        return ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(int(value)) if int(value) == value else str(value)
        if re.fullmatch('0+', fmt) and text.isdigit():
            text = text.zfill(len(fmt))
        return text
    return str(value)


def read_source(path):
    """Recognise a header or plain URL list; never collapse original rows."""
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            sheets = [(s.title, [[_value(c.value, c.number_format) for c in row] for row in s.iter_rows()])
                      for s in book.worksheets]
        finally:
            book.close()
    elif path.suffix.lower() in ('.csv', '.txt', '.tsv'):
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1251')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=';,\t')
            values = list(csv.reader(io.StringIO(text), dialect))
        except csv.Error:
            values = [[line] for line in text.splitlines()]
        sheets = [('CSV', values)]
    else:
        raise ValueError('Выберите XLSX, CSV, TSV или TXT')
    result = []
    for sheet, values in sheets:
        if not values:
            continue
        header, url_col, inn_col, name_col = None, None, None, None
        for index, row in enumerate(values[:20]):
            names = [v.strip().casefold() for v in row]
            url_indexes = [i for i, v in enumerate(names) if v in
                           ('url', 'urls', 'ссылка', 'ссылки', 'адрес', 'сайт', 'адрес сайта', 'website', 'link')]
            if url_indexes:
                header, url_col = index, url_indexes[0]
                inn_col = next((i for i, v in enumerate(names) if v in ('инн', 'inn', 'tax id')), None)
                name_col = next((i for i, v in enumerate(names) if v in
                                 ('компания', 'название', 'наименование', 'организация', 'company', 'name')), None)
                break
        if url_col is None:
            # Headerless lists: infer the URL column, retaining all non-empty rows.
            counts = {}
            for row in values[:30]:
                for col, val in enumerate(row):
                    if re.match(r'^https?://', val.strip(), re.I):
                        counts[col] = counts.get(col, 0) + 1
            if not counts:
                continue
            url_col = max(counts, key=counts.get)
        for index, row in enumerate(values):
            if (header is not None and index <= header) or not any(v.strip() for v in row):
                continue
            def get(col):
                return row[col].strip() if col is not None and col < len(row) else ''
            result.append({'sheet': sheet, 'number': index + 1, 'inn': get(inn_col),
                           'company': get(name_col), 'url': get(url_col), 'original': row})
    if not result:
        raise ValueError('Не найдены ссылки. Нужен столбец URL / Ссылка / Сайт или список HTTP/HTTPS адресов.')
    return result


def valid_pdf(path):
    from pypdf import PdfReader
    with Path(path).open('rb') as stream:
        if stream.read(5) != b'%PDF-':
            raise ValueError('Файл не имеет сигнатуры PDF')
        stream.seek(0)
        pdf = PdfReader(stream, strict=False)
        if pdf.is_encrypted:
            raise ValueError('PDF зашифрован: требуется ручная проверка')
        if not len(pdf.pages):
            raise ValueError('В PDF нет страниц')
        return len(pdf.pages)


class Archive:
    def __init__(self, folder, destination=''):
        if is_network_path(folder):
            raise ValueError('Рабочий архив должен находиться на локальном диске')
        self.root = Path(folder).resolve()
        if is_network_path(self.root):
            raise ValueError('Рабочий архив должен находиться на локальном диске')
        self.destination = str(Path(destination).absolute()) if destination else ''
        if self.destination and Path(self.destination) == self.root / 'captures':
            raise ValueError('Выберите папку назначения вне рабочего архива снимков')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / '.writer.lock').open('a+b')
        hide_service_file(self.root / '.writer.lock')
        try:
            import msvcrt
            self.lock.seek(0)
            if not self.lock.read(1):
                self.lock.write(b'0')
                self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.lock.close()
            raise RuntimeError('Этот архив уже открыт другим рабочим процессом') from None
        self.db = sqlite3.connect(self.root / 'archive.sqlite3', timeout=10)
        hide_service_file(self.root / 'archive.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS lists(id TEXT PRIMARY KEY, name TEXT, source TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, url TEXT UNIQUE, state TEXT,
                reason TEXT DEFAULT '', final_url TEXT DEFAULT '', path TEXT DEFAULT '', sha TEXT DEFAULT '',
                size INTEGER DEFAULT 0, mtime INTEGER DEFAULT 0, quality TEXT DEFAULT '', updated REAL);
            CREATE TABLE IF NOT EXISTS input_rows(id INTEGER PRIMARY KEY, list_id TEXT REFERENCES lists(id),
                resource_id TEXT REFERENCES resources(id), sheet TEXT, number INTEGER, inn TEXT,
                company TEXT, url TEXT, original TEXT);
            CREATE INDEX IF NOT EXISTS rows_list ON input_rows(list_id, id);
            CREATE INDEX IF NOT EXISTS rows_resource ON input_rows(resource_id);
            CREATE INDEX IF NOT EXISTS rows_inn ON input_rows(list_id, inn);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, list_id TEXT, started REAL, ended REAL,
                outcome TEXT, browser TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, resource_id TEXT, run_id TEXT,
                started REAL, ended REAL, state TEXT, reason TEXT DEFAULT '', staged TEXT DEFAULT '',
                target TEXT DEFAULT '', sha TEXT DEFAULT '', quality TEXT DEFAULT '', final_url TEXT DEFAULT '');
            CREATE INDEX IF NOT EXISTS attempts_resource ON attempts(resource_id, started);
            CREATE INDEX IF NOT EXISTS attempts_target ON attempts(target);
            CREATE TABLE IF NOT EXISTS deliveries(
                attempt_id TEXT REFERENCES attempts(id), kind TEXT, source TEXT UNIQUE,
                directory TEXT, name TEXT, candidate TEXT DEFAULT '', delivered INTEGER DEFAULT 0,
                error TEXT DEFAULT '', PRIMARY KEY (attempt_id, kind));
            CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(delivered);
        ''')
        # Additive migration: existing archives and their earlier captures stay readable.
        with self.db:
            for table in ('resources', 'attempts'):
                columns = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
                metadata = [('http_status', 'INTEGER'), ('checked_at', "TEXT DEFAULT ''"),
                            ('original_sha', "TEXT DEFAULT ''")]
                if table == 'attempts':
                    metadata += [('destination', "TEXT DEFAULT ''"), ('filename', "TEXT DEFAULT ''")]
                for name, definition in metadata:
                    if name not in columns:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        self.recover()
        with self.db:
            for table in ('resources', 'attempts'):
                self.db.execute(f"""UPDATE {table} SET state='saved_error'
                    WHERE state IN ('ready','document') AND
                    (reason <> '' OR quality LIKE 'access_%' OR http_status >= 400)""")
            self.db.execute("""UPDATE resources SET state='error',path='',size=0,checked_at='',
                reason='Капча Google не пройдена. Откройте сайт и пройдите проверку вручную.'
                WHERE state='saved_error' AND reason LIKE '%проверки Google /sorry%'""")

    def close(self):
        try:
            self.clean_work_dirs()
        finally:
            self.db.close()
            self.lock.close()

    def work_dir(self, run):
        work = self.root / '.work'
        folder = work / run
        folder.mkdir(parents=True, exist_ok=True)
        hide_service_file(work)
        return folder

    def clean_work_dirs(self):
        work = self.root / '.work'
        with suppress(OSError):
            for folder in work.iterdir():
                if re.fullmatch(r'[0-9a-f]{32}', folder.name):
                    with suppress(OSError):
                        folder.rmdir()
            work.rmdir()

    def recover(self):
        for attempt in self.db.execute("SELECT * FROM attempts WHERE state IN ('running','publishing')").fetchall():
            try:
                target = self.root / attempt['target']
                if attempt['state'] == 'publishing' and attempt['target']:
                    if not target.exists() and attempt['staged']:
                        staged = self.root / attempt['staged']
                        if staged.exists() and hashlib.sha256(staged.read_bytes()).hexdigest() == attempt['sha']:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(staged, target)
                    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == attempt['sha']:
                        self._publish_original(attempt, target)
                        if target.suffix == '.pdf':
                            valid_pdf(target)
                        self._finish_publication(attempt, target)
                        continue
                self.fail(attempt['id'], 'interrupted', 'Предыдущий запуск был прерван; можно продолжить')
            except Exception as error:
                self.fail(attempt['id'], 'interrupted', f'Восстановление: {error}')
        self.db.execute("UPDATE runs SET ended=?,outcome='interrupted' WHERE ended IS NULL", (time.time(),))
        self.db.commit()

    def import_source(self, path):
        rows = read_source(path)
        fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO lists VALUES(?,?,?,?)',
                            (fingerprint, Path(path).name, str(Path(path).resolve()), time.time()))
            if self.db.execute('SELECT 1 FROM input_rows WHERE list_id=? LIMIT 1', (fingerprint,)).fetchone():
                return fingerprint
            for row in rows:
                try:
                    url, state, reason = normalized_url(row['url']), 'pending', ''
                except ValueError as error:
                    url, state, reason = 'invalid:' + row['url'], 'error', str(error)
                resource = hashlib.sha256(url.encode()).hexdigest()[:24]
                self.db.execute('INSERT OR IGNORE INTO resources(id,url,state,reason,updated) VALUES(?,?,?,?,?)',
                                (resource, url, state, reason, time.time()))
                self.db.execute('''INSERT INTO input_rows(list_id,resource_id,sheet,number,inn,company,url,original)
                                   VALUES(?,?,?,?,?,?,?,?)''', (fingerprint, resource, row['sheet'], row['number'],
                                   row['inn'], row['company'], row['url'], json.dumps(row['original'], ensure_ascii=False)))
        return fingerprint

    def rows(self, list_id):
        return [dict(r) for r in self.db.execute('''SELECT i.*,r.state,r.reason,r.final_url,r.path,r.quality,r.updated,
            r.http_status,r.checked_at,r.original_sha,d.directory,d.candidate,d.delivered,o.candidate AS original_candidate,
            o.delivered AS original_delivered
            FROM input_rows i JOIN resources r ON r.id=i.resource_id
            LEFT JOIN deliveries d ON d.source=r.path
            LEFT JOIN deliveries o ON o.attempt_id=d.attempt_id AND o.kind='original'
            WHERE i.list_id=? ORDER BY i.id''', (list_id,))]

    def begin(self, resource, run):
        attempt = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO attempts(id,resource_id,run_id,started,state) VALUES(?,?,?,?,?)',
                            (attempt, resource, run, time.time(), 'running'))
            self.db.execute("UPDATE resources SET state='running',reason='',http_status=NULL,checked_at='',updated=? WHERE id=?",
                            (time.time(), resource))
        return attempt

    def fail(self, attempt, state, reason, final_url='', http_status=None):
        with self.db:
            self.db.execute('UPDATE attempts SET ended=?,state=?,reason=?,final_url=?,http_status=? WHERE id=?',
                            (time.time(), state, reason, final_url, http_status, attempt))
            self.db.execute('''UPDATE resources SET state=?,reason=?,final_url=?,http_status=?,updated=?
                               WHERE id=(SELECT resource_id FROM attempts WHERE id=?)''',
                            (state, reason, final_url, http_status, time.time(), attempt))

    def publish(self, attempt, staged, name, extension='.pdf', quality='automatic', final_url='',
                reason='', http_status=None, checked_at='', original=None):
        staged = Path(staged)
        if extension == '.pdf':
            valid_pdf(staged)
        sha = hashlib.sha256(staged.read_bytes()).hexdigest()
        original_sha = hashlib.sha256(original.read_bytes()).hexdigest() if original else ''
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', name).strip(' .')[:120].rstrip(' .') or 'site'
        if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', safe.split('.')[0], re.I):
            safe = '_' + safe
        captured = datetime.fromisoformat(checked_at) if checked_at else datetime.now().astimezone()
        safe += '_' + captured.date().isoformat()
        folder = self.root / 'captures'
        target = folder / (safe + extension)
        copy_number = 0
        while (target.exists() or original_path(target).exists() or self.db.execute(
                'SELECT 1 FROM attempts WHERE target=? LIMIT 1', (str(target.relative_to(self.root)),)).fetchone()):
            copy_number += 1
            target = folder / f'{safe}_{copy_number}{extension}'
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.db:
            self.db.execute('''UPDATE attempts SET state='publishing',staged=?,target=?,sha=?,quality=?,final_url=?,
                               reason=?,http_status=?,checked_at=?,original_sha=?,destination=?,filename=?
                               WHERE id=?''', (str(staged.relative_to(self.root)), str(target.relative_to(self.root)),
                                              sha, quality, final_url, reason, http_status, checked_at, original_sha,
                                              self.destination, safe + extension, attempt))
        item = self.db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
        self._publish_original(item, target)
        os.replace(staged, target)
        self._finish_publication(item, target)
        return str(target)

    def _publish_original(self, item, target):
        if not item['original_sha']:
            return
        destination = original_path(target)
        source = destination if destination.is_file() else original_path(self.root / item['staged'])
        if hashlib.sha256(source.read_bytes()).hexdigest() != item['original_sha']:
            raise ValueError('Исходный PDF повреждён')
        if source != destination:
            os.replace(source, destination)

    def _finish_publication(self, item, target):
        stat = target.stat()
        state = ('saved_error' if item['reason'] or item['quality'].startswith('access_')
                 or (item['http_status'] or 0) >= 400 else
                 'ready' if target.suffix == '.pdf' else 'document')
        with self.db:
            local_path = target.relative_to(self.root)
            if item['destination']:
                self.db.execute('INSERT INTO deliveries(attempt_id,kind,source,directory,name) VALUES(?,?,?,?,?)',
                                (item['id'], 'backup', str(local_path), item['destination'], item['filename']))
                if item['original_sha']:
                    self.db.execute('INSERT INTO deliveries(attempt_id,kind,source,directory,name) VALUES(?,?,?,?,?)',
                                    (item['id'], 'original', str(original_path(local_path)), item['destination'],
                                     original_path(Path(item['filename'])).name))
            self.db.execute('UPDATE attempts SET state=?,ended=? WHERE id=?', (state, time.time(), item['id']))
            self.db.execute('''UPDATE resources SET state=?,reason=?,path=?,sha=?,size=?,mtime=?,quality=?,
                final_url=?,http_status=?,checked_at=?,original_sha=?,updated=? WHERE id=?''',
                (state, item['reason'], str(local_path), item['sha'],
                 stat.st_size, stat.st_mtime_ns, item['quality'], item['final_url'], item['http_status'],
                 item['checked_at'], item['original_sha'], time.time(), item['resource_id']))

    def send_pending(self, cancelled, notify, *, after=0):
        pending = self.db.execute('''SELECT d.* FROM deliveries d JOIN attempts a ON a.id=d.attempt_id
            WHERE d.delivered=0 AND d.rowid>? ORDER BY a.started, d.attempt_id, d.kind''', (after,)).fetchall()
        for item in pending:
            if cancelled():
                break
            key = (item['attempt_id'], item['kind'])
            name = item['name']
            if item['kind'] == 'original':
                backup = self.db.execute("SELECT candidate,delivered FROM deliveries WHERE attempt_id=? AND kind='backup'",
                                         (item['attempt_id'],)).fetchone()
                if not backup['delivered']:
                    continue
                name = original_path(Path(backup['candidate'])).name

            def remember(candidate):
                with self.db:
                    self.db.execute('UPDATE deliveries SET candidate=? WHERE attempt_id=? AND kind=?', (candidate, *key))

            try:
                target = publish_file(self.root / item['source'], Path(item['directory']), name,
                                      f'.backuptool-{item["attempt_id"]}-{item["kind"]}.part', item['candidate'], remember)
            except OSError as error:
                with self.db:
                    self.db.execute('UPDATE deliveries SET error=? WHERE attempt_id=? AND kind=?', (str(error), *key))
                notify(f'Не отправлен {name} · {item["directory"]} · {error}')
            else:
                with self.db:
                    self.db.execute("UPDATE deliveries SET delivered=1,error='' WHERE attempt_id=? AND kind=?", key)
                notify(f'Отправлен {target}')
        return self.db.execute('SELECT count(*) FROM deliveries WHERE delivered=0').fetchone()[0]

    def reusable(self, resource, full=False):
        if resource['state'] not in SAVED or not resource['path']:
            return False
        path = self.root / resource['path']
        try:
            stat = path.stat()
            if resource['original_sha']:
                original = original_path(path)
                if not original.is_file() or (full and hashlib.sha256(original.read_bytes()).hexdigest()
                                              != resource['original_sha']):
                    return False
            if full or stat.st_size != resource['size'] or stat.st_mtime_ns != resource['mtime']:
                if hashlib.sha256(path.read_bytes()).hexdigest() != resource['sha']:
                    return False
                if path.suffix == '.pdf':
                    valid_pdf(path)
                with self.db:
                    self.db.execute('UPDATE resources SET size=?,mtime=? WHERE id=?',
                                    (stat.st_size, stat.st_mtime_ns, resource['id']))
            return True
        except Exception:
            return False

    def resources(self, list_id):
        return [dict(r) for r in self.db.execute('''SELECT DISTINCT r.* FROM resources r
            JOIN input_rows i ON i.resource_id=r.id WHERE i.list_id=? ORDER BY r.id''', (list_id,))]


REPORT_FIELDS = ('inn', 'company', 'url', 'state', 'reason', 'final_url', 'path', 'quality', 'sheet', 'number')
REPORT_HEADERS = ('ИНН', 'Компания', 'URL', 'Результат', 'Причина', 'Конечный URL', 'Файл', 'Проверка', 'Лист', 'Строка')
EXPORT_HEADERS = REPORT_HEADERS + ('Время обработки', 'HTTP-статус', 'Дата и время снимка',
                                   'Исходный PDF', 'SHA-256 исходного PDF', 'Отправка в папку назначения')


def report_values(row, root):
    values = [str(row.get(key, '')) for key in REPORT_FIELDS]
    values[3] = result_label(row)
    if row.get('path'):
        values[6] = str(Path(row['directory']) / row['candidate'] if row.get('delivered')
                        else Path(root) / row['path'])
    return values


def export_reports(archive, list_id):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import PatternFill
    rows = archive.rows(list_id)
    def export_values(row):
        stamp = (datetime.fromtimestamp(row['updated']).astimezone().isoformat(timespec='seconds')
                 if row['updated'] and row['state'] != 'pending' else '')
        original = str(original_path(archive.root / row['path'])) if row['original_sha'] and row['path'] else ''
        if row['original_delivered']:
            original = str(Path(row['directory']) / row['original_candidate'])
        delivery = ''
        if row['directory']:
            delivery = ('Отправлено' if row['delivered'] and row['original_delivered'] != 0
                        else 'Ожидает отправки')
        return report_values(row, archive.root) + [stamp, str(row['http_status'] or ''), format_capture_time(row['checked_at']),
                                                    original, row['original_sha'], delivery]

    folder = archive.root / 'exports' / list_id[:12]
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4]
    target = folder / f'report-{stamp}.xlsx'
    temp = target.with_suffix('.part.xlsx')
    book = Workbook(write_only=True)
    fills = {'saved_error': PatternFill('solid', fgColor='FFF0BF'),
             **{state: PatternFill('solid', fgColor='EEF9F2') for state in GOOD}}
    for title, subset in [('Все результаты', rows),
                          ('Нужны действия', [r for r in rows if r['state'] not in GOOD]),
                          ('Снимки с предупреждениями', [r for r in rows if r['state'] == 'saved_error'])]:
        sheet = book.create_sheet(title)
        sheet.freeze_panes = 'A2'
        for key, width in zip('ABCDEFGHIJKLMNOP', [18, 35, 60, 36, 70, 60, 60, 20, 20, 12, 30, 16, 36, 65, 65, 32]):
            sheet.column_dimensions[key].width = width
        sheet.append(EXPORT_HEADERS)
        for row in subset:
            cells = []
            for index, value in enumerate(export_values(row)):
                cell = WriteOnlyCell(sheet, value=value[:32767])
                cell.data_type = 's'
                cell.number_format = '@'
                if index == 3 and row['state'] in fills:
                    cell.fill = fills[row['state']]
                cells.append(cell)
            sheet.append(cells)
    book.save(temp)
    os.replace(temp, target)
    csv_path = target.with_suffix('.csv')
    csv_temp = csv_path.with_suffix('.part.csv')
    with csv_temp.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=';')
        writer.writerow(EXPORT_HEADERS)
        for row in rows:
            values = export_values(row)
            writer.writerow(["'" + v if v.lstrip().startswith(('=', '+', '-', '@')) or
                             (i == 0 and v.isdigit()) else v for i, v in enumerate(values)])
    os.replace(csv_temp, csv_path)
    return str(target)
