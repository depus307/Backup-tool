"""Single-screen desktop application. Excel and browser work stay in the worker."""
from collections import Counter, deque
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import sqlite3
import sys
from threading import Thread
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlsplit

from . import __version__, updates
from .archive import GOOD, REPORT_HEADERS, SAVED, report_values, result_label
from .delivery import default_destination, is_network_path
from .evidence import format_capture_time, original_path
from .clipboard import WindowsClipboard, clipboard_payload
from .controller import WorkerSession
from .ui_widgets import ProgressTrack, button_layout

BG, PANEL, INK, MUTED = '#f5f7fb', '#ffffff', '#1e293b', '#52637b'
LINE, SOFT, BLUE = '#d5deec', '#e8eef7', '#2957d1'
STATE_COLORS = {'ready': '#14623d', 'document': '#14623d', 'error': '#a42a3e',
                'saved_error': '#805008', 'needs_help': '#805008', 'running': BLUE, 'pending': MUTED,
                'interrupted': MUTED}
RESULT_BACKGROUNDS = {'ready': '#eef9f2', 'document': '#eef9f2', 'error': '#fdf0f2',
                      'saved_error': '#fff0bf', 'needs_help': '#fff7e7'}
FILTERS = (('all', 'Все'), ('actions', 'Нужны действия'), ('ready', 'Готово'),
           ('error', 'Ошибки'), ('saved_error', 'Сохранено как ошибка'),
           ('needs_help', 'Нужна помощь'), ('pending', 'В очереди'),
           ('running', 'В работе'), ('interrupted', 'Прервано'))


def file_size(size):
    if not size:
        return '—'
    if size < 1024:
        return f'{size} Б'
    if size < 1024 ** 2:
        return f'{size / 1024:.1f} КБ'.replace('.', ',')
    return f'{size / 1024 ** 2:.1f} МБ'.replace('.', ',')


def release_notes():
    path = Path(__file__).resolve().parents[1] / 'CHANGELOG.md'
    if not path.is_file():
        return [(f'Версия {__version__}', 'Полный список изменений находится в CHANGELOG.md поставки.')]
    blocks, heading, body = [], '', []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith(('## ', '### ')):
            if heading:
                blocks.append((heading, '\n'.join(body).strip()))
            heading, body = line.lstrip('# ').strip(), []
        elif heading:
            body.append('• ' + line[2:] if line.startswith('- ') else line)
    if heading:
        blocks.append((heading, '\n'.join(body).strip()))
    return blocks


def tooltip(widget, text):
    timer = None
    popup = None

    def show():
        nonlocal timer, popup
        timer = None
        popup = tk.Toplevel(widget)
        popup.wm_overrideredirect(True)
        popup.wm_attributes('-topmost', True)
        ttk.Label(popup, text=text, padding=(9, 6), wraplength=300,
                  background='#fff6d9', foreground='#222').pack()
        popup.update_idletasks()
        x = min(widget.winfo_rootx(), widget.winfo_screenwidth() - popup.winfo_reqwidth())
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        if y + popup.winfo_reqheight() > widget.winfo_screenheight():
            y = widget.winfo_rooty() - popup.winfo_reqheight() - 4
        popup.geometry(f'+{max(0, x)}+{max(0, y)}')

    def hide(event):
        nonlocal timer, popup
        if timer is not None:
            widget.after_cancel(timer)
            timer = None
        if popup is not None:
            popup.destroy()
            popup = None

    def schedule(event):
        nonlocal timer
        hide(event)
        timer = widget.after(550, show)

    widget.bind('<Enter>', schedule, add='+')
    for event in ('<Leave>', '<ButtonPress>', '<Unmap>', '<Destroy>'):
        widget.bind(event, hide, add='+')


class Application:
    def __init__(self, root):
        self.root = root
        resources = Path(__file__).resolve().parents[1]
        self.executable = Path(sys.executable) if getattr(sys, 'frozen', False) else None
        distribution = self.executable.parent if self.executable else resources
        self.home = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'BackupTool'
        icon = resources / 'BackupToolNext.ico'
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        self.settings_path = self.home / 'settings.json'
        try:
            settings_file = self.settings_path if self.settings_path.is_file() else distribution / 'settings.json'
            settings = json.loads(settings_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        previous_archive = settings.get('archive', str(distribution / 'archive'))
        self.archive_root = Path(settings.get('archive_root', str(self.home / 'archive')))
        destination = settings.get('destination') or default_destination(
            distribution / 'delivery.local.json', resources / 'delivery.json')
        if 'archive_root' not in settings and previous_archive and not is_network_path(previous_archive):
            if (Path(previous_archive) / 'archive.sqlite3').is_file():
                self.archive_root = Path(previous_archive).resolve()
        self.onboarding_seen = settings.get('onboarding_seen', False)
        root.title('BackupTool')
        root.geometry('1160x770')
        root.minsize(820, 670)
        root.tk.call('tk', 'scaling', 96 / 72)
        self.session = WorkerSession()
        self.source = tk.StringVar(value=settings.get('source', ''))
        self.destination = tk.StringVar(value=destination)
        self.destination_warning = tk.StringVar()
        self.pending_delivery_label = tk.StringVar()
        self.gentle = tk.BooleanVar(value=settings.get('gentle', False))
        self.refresh = tk.BooleanVar()
        self.filter_key = 'all'
        self.search = tk.StringVar()
        self.phase = tk.StringVar(value='Подготовка списка')
        self.progress_text = tk.StringVar(value='0 из 0')
        self.status = tk.StringVar(value='Выберите список и папку архива. Затем нажмите «Начать обработку».')
        self.counts = tk.StringVar(value='Список не загружен')
        self.page_info = tk.StringVar(value='0 строк')
        self.detail_caption = tk.StringVar(value='Выбранная строка')
        self.detail_parts = None
        self.selection_info = tk.StringVar()
        self.list_name = tk.StringVar()
        self.tab = tk.StringVar()
        self.manual_saving = False
        self.list_id = settings.get('list_id')
        self.list_ids = []
        self.rows = {}
        self.offset = 0
        self.filtered_count = 0
        self.all_selected = False
        self.active_column = 'url'
        self.report = None
        self.operation = None
        self.next_read = 0
        self.dirty = True
        self.closing = False
        self.stop_deadline = None
        self.log_lines = deque(maxlen=1500)
        self.log_window = None
        self.about_window = None
        self.update_events = SimpleQueue()
        self.available_update = None
        self.update_task = None
        self.update_label = tk.StringVar(value='Проверить обновления')
        self.update_status = tk.StringVar()
        self.build()
        self.source.trace_add('write', self.source_changed)
        self.destination.trace_add('write', self.destination_changed)
        self.destination_changed()
        self.refresh.trace_add('write', self.mark_dirty)
        self.clipboard = WindowsClipboard(root.winfo_id())
        self.load_lists()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(150, self.poll)
        if self.executable:
            result = updates.previous_result(distribution)
            if result:
                if result['success']:
                    self.update_status.set(f'BackupTool обновлён до {result["version"]}.')
                else:
                    self.set_status('Ошибка обновления: ' + result['error'])
        if not self.onboarding_seen:
            root.after_idle(lambda: self.show_about('Инструкция'))

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')
        self.root.configure(background=BG)
        style.configure('.', background=BG, foreground=INK, font=('Segoe UI', 10))
        style.configure('TLabel', background=BG)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Warning.TLabel', foreground='#805008')
        style.configure('Title.TLabel', font=('Segoe UI', 19, 'bold'))
        style.configure('Section.TLabel', font=('Segoe UI', 11, 'bold'))
        style.configure('Panel.TFrame', background=PANEL, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.configure('Rule.TFrame', background=LINE)
        style.configure('Brand.TLabel', foreground=BLUE, font=('Segoe UI', 17, 'bold'))
        style.configure('Card.TLabel', background=PANEL, foreground=MUTED)
        self.button_images = []
        disabled = ('#edf1f6', '#e1e7f0')
        for name, foreground, palette in (
            ('TButton', INK, ((PANEL, LINE), disabled, ('#e3ebf8', '#9db3d8'), ('#f0f5ff', '#b5c6e3'))),
            ('Primary.TButton', PANEL, ((BLUE, BLUE), disabled, ('#19388f', '#19388f'), ('#2049b7', '#2049b7'))),
            ('Ready.TButton', PANEL, (('#1646ca', '#1646ca'), disabled, ('#142f86', '#142f86'),
                                      ('#193bab', '#193bab'))),
        ):
            self.button_images.extend(button_layout(style, name, palette))
            style.configure(name, foreground=foreground, focuscolor=foreground, focusthickness=1,
                            width=0, padding=(12, 5))
            style.map(name, foreground=[('disabled', MUTED)])
        style.configure('Ready.TButton', font=('Segoe UI', 10, 'bold'), padding=(15, 6))
        style.configure('Ready.TLabel', foreground=BLUE, font=('Segoe UI', 9, 'bold'))
        style.configure('About.TNotebook', background=BG, bordercolor=LINE, tabmargins=(0, 0, 0, 0))
        style.layout('About.TNotebook.Tab', [
            ('Notebook.padding', {'sticky': 'nsew', 'children': [('Notebook.label', {'sticky': 'nsew'})]}),
        ])
        style.configure('About.TNotebook.Tab', padding=(12, 8), foreground=MUTED)
        style.map('About.TNotebook.Tab', padding=[('selected', (12, 8))], foreground=[('selected', BLUE)])
        for name in ('TEntry', 'TCombobox', 'TMenubutton'):
            style.configure(name, fieldbackground=PANEL, background=PANEL, bordercolor=LINE, padding=(7, 3))
        style.map('TCombobox', fieldbackground=[('readonly', PANEL)], selectbackground=[('readonly', PANEL)],
                  selectforeground=[('readonly', INK)])
        style.configure('Treeview', rowheight=42, background=PANEL, fieldbackground=PANEL,
                        foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.configure('Compact.Treeview', rowheight=37)
        style.configure('Treeview.Heading', background=SOFT, foreground=MUTED, padding=(7, 6),
                        bordercolor=LINE, font=('Segoe UI', 9, 'bold'))
        style.map('Treeview', background=[('selected', '#dce8ff')], foreground=[])
        style.configure('Vertical.TScrollbar', background=SOFT, troughcolor=PANEL, bordercolor=LINE,
                        lightcolor=SOFT, darkcolor=SOFT)

        ttk.Label(self.root, text='BackupTool', style='Brand.TLabel').pack(anchor='w', padx=18, pady=(6, 1))
        outer = ttk.Frame(self.root, padding=(18, 3, 18, 8))
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        setup = ttk.Frame(outer)
        setup.grid(row=0, column=0, sticky='ew')
        for column in (0, 1):
            setup.columnconfigure(column, weight=1, uniform='path')
        (self.source_entry, self.choose_source) = self.path_field(
            setup, 0, 'Список сайтов', self.source, self.browse_source)
        (self.archive_entry, self.choose_archive) = self.path_field(
            setup, 1, 'Сохранять в', self.destination, self.browse_archive)
        ttk.Label(setup, text='Сохраняйте бэкапы на общий сетевой диск', style='Muted.TLabel').grid(
            row=2, column=1, sticky='w', pady=(4, 0))
        warning = ttk.Label(setup, textvariable=self.destination_warning, style='Warning.TLabel', wraplength=300)
        warning.grid(row=3, column=1, sticky='ew')
        self.archive_entry.master.bind('<Configure>', lambda event: warning.configure(wraplength=event.width))
        ttk.Label(setup, text='Нагрузка', style='Muted.TLabel').grid(row=0, column=2, sticky='w', pady=(0, 4))
        self.mode = ttk.Combobox(setup, values=('Обычная · до 4 страниц', 'Щадящая · до 2 страниц'),
                                 width=25, state='readonly')
        self.mode.current(int(self.gentle.get()))
        self.mode.bind('<<ComboboxSelected>>', lambda event: self.gentle.set(self.mode.current() == 1))
        self.mode.grid(row=1, column=2, sticky='ew')

        controls = ttk.Frame(outer)
        controls.grid(row=1, column=0, sticky='ew', pady=(10, 10))
        self.start_button = ttk.Button(controls, text='▶  Начать обработку', style='Primary.TButton',
                                       command=lambda: self.start('batch'))
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(controls, text='■  Остановить', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=7)
        self.start_hint = ttk.Label(controls, text='Список загружен', style='Ready.TLabel')
        self.open_report_button = ttk.Button(controls, text='Отчёт Excel', command=self.open_report)
        self.open_report_button.pack(side='right')
        self.options = ttk.Menubutton(controls, text='Действия')
        self.options.pack(side='right', padx=7)
        operations = tk.Menu(self.options, tearoff=False)
        operations.add_command(label='Загрузить список', command=lambda: self.start('import'))
        operations.add_checkbutton(label='Обновить готовые', variable=self.refresh)
        operations.add_command(label='Проверить файлы списка', command=lambda: self.start('verify'))
        operations.add_command(label='Выгрузить XLSX + CSV', command=lambda: self.start('export'))
        self.options.configure(menu=operations)

        progress = ttk.Frame(outer)
        progress.grid(row=2, column=0, sticky='ew', pady=(0, 9))
        progress.columnconfigure(0, weight=1)
        ttk.Frame(progress, style='Rule.TFrame', height=1).grid(row=0, column=0, columnspan=2,
                                                               sticky='ew', pady=(0, 5))
        ttk.Label(progress, textvariable=self.phase, style='Section.TLabel').grid(row=1, column=0, sticky='w')
        ttk.Label(progress, textvariable=self.progress_text, style='Muted.TLabel').grid(row=1, column=1, sticky='e')
        self.progress = ProgressTrack(progress, background=BG, track=SOFT, fill=BLUE, highlight='#719cff')
        self.progress.grid(row=2, column=0, columnspan=2, sticky='ew', pady=5)
        ttk.Label(progress, textvariable=self.counts, style='Muted.TLabel').grid(row=3, column=0, sticky='w')
        self.status_label = ttk.Label(progress, textvariable=self.status, style='Muted.TLabel')
        self.status_label.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(2, 0))
        ttk.Frame(progress, style='Rule.TFrame', height=1).grid(row=5, column=0, columnspan=2,
                                                               sticky='ew', pady=(5, 0))

        self.body = ttk.Frame(outer)
        self.body.grid(row=3, column=0, sticky='nsew')
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        table = ttk.Frame(self.body)
        table.grid(row=0, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        filters = ttk.Frame(table)
        filters.grid(row=0, column=0, sticky='ew', pady=(0, 7))
        self.filter_combo = ttk.Combobox(filters, state='readonly', width=22)
        self.filter_combo.pack(side='left')
        self.filter_combo.bind('<<ComboboxSelected>>', self.select_filter)
        self.filter_counts({})
        self.list_combo = ttk.Combobox(filters, textvariable=self.list_name, state='readonly', width=20)
        self.list_combo.bind('<<ComboboxSelected>>', self.select_list)
        search = ttk.Entry(filters, textvariable=self.search, width=17)
        search.pack(side='right', fill='x', expand=True, padx=(5, 0))
        self.search.trace_add('write', lambda *args: self.filter_changed())
        ttk.Label(filters, text='Поиск', style='Muted.TLabel').pack(side='right', padx=(8, 0))

        table_grid = ttk.Frame(table)
        table_grid.grid(row=1, column=0, sticky='nsew')
        table_grid.rowconfigure(0, weight=1)
        table_grid.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_grid, columns=('company', 'url', 'state', 'size'),
                                 show='headings', selectmode='extended', height=4)
        for name, label, width, minimum in (('company', 'Компания / ИНН', 220, 128),
                                            ('url', 'Адрес', 305, 130),
                                            ('state', 'Результат', 155, 115),
                                            ('size', 'Размер', 75, 65)):
            self.tree.heading(name, text=label, anchor='e' if name == 'size' else 'w')
            self.tree.column(name, width=width, minwidth=minimum, stretch=name in ('company', 'url'),
                             anchor='e' if name == 'size' else 'w')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table_grid, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table_grid, orient='horizontal', command=self.tree.xview)

        def horizontal_view(first, last):
            horizontal.set(first, last)
            if float(first) == 0 and float(last) == 1:
                horizontal.grid_remove()
            else:
                horizontal.grid(row=1, column=0, sticky='ew')

        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal_view)
        self.empty_table = ttk.Label(table_grid, text='Загрузите список сайтов', style='Card.TLabel')
        for state, color in STATE_COLORS.items():
            colors = {'foreground': color}
            if state in RESULT_BACKGROUNDS:
                colors['background'] = RESULT_BACKGROUNDS[state]
            self.tree.tag_configure(state, **colors)
        self.tree.tag_configure('alternate', background='#f8fafd')
        self.tree.bind('<<TreeviewSelect>>', self.selected)
        self.tree.bind('<Button-1>', self.click_row)
        self.tree.bind('<Button-3>', self.context)
        self.tree.bind('<Control-a>', self.select_all)
        self.tree.bind('<Control-c>', lambda event: self.copy('rows'))
        self.tree.bind('<Shift-Down>', lambda event: self.extend_selection(1))
        self.tree.bind('<Shift-Up>', lambda event: self.extend_selection(-1))
        for key, field in [('u', 'url'), ('i', 'inn'), ('r', 'reason'), ('c', 'cell')]:
            self.root.bind('<Alt-' + key + '>', lambda event, f=field: self.copy(f))

        copy_bar = ttk.Frame(table)
        copy_bar.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        self.next_page = ttk.Button(copy_bar, text='→', width=3, command=lambda: self.page(1))
        self.next_page.pack(side='right')
        self.previous_page = ttk.Button(copy_bar, text='←', width=3, command=lambda: self.page(-1))
        self.previous_page.pack(side='right', padx=4)
        ttk.Label(copy_bar, textvariable=self.page_info, style='Muted.TLabel').pack(side='right', padx=(3, 5))
        copy = ttk.Menubutton(copy_bar, text='Копировать')
        copy_menu = tk.Menu(copy, tearoff=False)
        self.populate_copy(copy_menu)
        copy.configure(menu=copy_menu)
        copy.pack(side='left')
        self.copy_filter = ttk.Button(copy_bar, text='Весь фильтр', command=lambda: self.copy('filter'))
        self.copy_filter.pack(side='left', padx=7)
        ttk.Label(copy_bar, textvariable=self.selection_info, style='Muted.TLabel').pack(side='left')

        self.card = ttk.Frame(self.body, style='Panel.TFrame', padding=9, relief='solid', borderwidth=1)
        self.card.rowconfigure(1, weight=1)
        self.card.columnconfigure(0, weight=1)
        ttk.Label(self.card, textvariable=self.detail_caption, style='Card.TLabel').grid(row=0, column=0, sticky='w')
        self.detail = ScrolledText(self.card, wrap='word', height=4, width=29, font=('Segoe UI', 10),
                                   relief='flat', background=PANEL, foreground=INK, pady=4, padx=0,
                                   state='disabled')
        self.detail.grid(row=1, column=0, sticky='nsew')
        self.detail.tag_configure('title', font=('Segoe UI', 12, 'bold'), spacing3=3)
        self.detail.tag_configure('muted', foreground=MUTED)
        self.detail.tag_configure('url', foreground=BLUE)
        self.detail.tag_configure('reason', background=SOFT, lmargin1=6, lmargin2=6, rmargin=6, spacing1=4,
                                  spacing3=4)
        for state, color in STATE_COLORS.items():
            self.detail.tag_configure(state, foreground=color, font=('Segoe UI', 10, 'bold'), spacing3=3)
        self.detail.bind('<Control-c>', self.copy_detail)
        self.card_actions = ttk.Frame(self.card, style='Panel.TFrame')
        self.open_file_button = ttk.Button(self.card_actions, text='Открыть файл', command=self.open_artifact)
        self.manual_button = ttk.Button(self.card_actions, text='Помочь открыть сайт', command=self.manual)
        self.tabs = ttk.Combobox(self.card_actions, textvariable=self.tab, state='disabled', width=20)
        self.save_button = ttk.Button(self.card_actions, text='2  Сохранить PDF вкладки', command=self.save_manual,
                                      style='Ready.TButton', state='disabled')
        self.attach_button = ttk.Button(self.card_actions, text='Прикрепить PDF…', command=self.attach)
        self.copy_url_button = ttk.Button(self.card_actions, text='Копировать ссылку', command=lambda: self.copy('url'))
        self.history_button = ttk.Button(self.card_actions, text='История попыток', command=self.show_history)

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky='ew', pady=(7, 0))
        ttk.Frame(footer, style='Rule.TFrame', height=1).pack(fill='x', pady=(0, 5))
        open_archive = ttk.Button(footer, text='Открыть архив', command=self.open_archive)
        open_archive.pack(side='left')
        journal = ttk.Button(footer, text='Журнал', command=self.show_log)
        journal.pack(side='left', padx=7)
        self.delivery_button = ttk.Button(footer, textvariable=self.pending_delivery_label,
                                          command=self.show_pending_delivery)
        about_button = ttk.Button(footer, text='О приложении', command=self.show_about)
        self.update_button = ttk.Button(footer, textvariable=self.update_label, command=self.check_or_update)
        self.update_button.pack(side='right')
        self.update_status_label = ttk.Label(footer, textvariable=self.update_status, style='Muted.TLabel',
                                            justify='right')
        self.update_status_label.pack(side='right', padx=(0, 8))
        about_button.pack(side='right', padx=(0, 7))

        for widget, tip in (
            (self.choose_source, 'Выберите XLSX, CSV, TSV или TXT со ссылками.'),
            (self.choose_archive, 'Выберите папку назначения для снимков и оригиналов.'),
            (self.mode, 'Щадящий режим уменьшает число одновременно открытых страниц.'),
            (self.start_button, 'Сохраняет сайты из списка. Готовые файлы при продолжении пропускаются.'),
            (self.stop_button, 'Останавливает обработку. Готовые файлы сохраняются.'),
            (self.options, 'Загрузить список, обновить готовые, проверить файлы или выгрузить XLSX и CSV.'),
            (self.filter_combo, 'Показывает строки с выбранным результатом.'),
            (self.list_combo, 'Переключает списки, загруженные в этот архив.'),
            (search, 'Поиск по ИНН, компании или адресу.'),
            (copy, 'Копирует строки или поля. Вставка в Excel сохраняет ведущие нули ИНН.'),
            (self.copy_filter, 'Копирует все строки текущего фильтра, включая другие страницы.'),
            (self.next_page, 'Следующие 500 строк текущего фильтра.'),
            (self.previous_page, 'Предыдущие 500 строк текущего фильтра.'),
            (self.manual_button, 'Открывает сайт в отдельном окне Chrome для входа или проверки.'),
            (self.tabs, 'Выберите вкладку браузера, которую нужно сохранить.'),
            (self.save_button, 'Печатает выбранную вкладку в PDF с адресом страницы.'),
            (self.attach_button, 'Проверяет ваш PDF и сохраняет копию в архиве для выбранной строки.'),
            (self.copy_url_button, 'Копирует исходную ссылку выбранной строки.'),
            (self.history_button, 'Показывает попытки сохранения с датой и результатом.'),
            (self.open_file_button, 'Открывает файл выбранной строки.'),
            (self.open_report_button, 'Открывает последний XLSX выбранного списка.'),
            (open_archive, 'Открывает папку назначения в Проводнике.'),
            (journal, 'Показывает события текущего сеанса с местным временем.'),
            (about_button, 'Версия, инструкция, список изменений и контакт разработчика.'),
            (self.update_button, 'Проверяет наличие новой стабильной версии на GitHub.'),
        ):
            tooltip(widget, tip)
        self.root.bind('<Configure>', self.layout, add='+')
        self.root.after_idle(self.layout)

    def path_field(self, setup, column, title, variable, choose):
        ttk.Label(setup, text=title, style='Muted.TLabel').grid(row=0, column=column, sticky='w', pady=(0, 4))
        field = ttk.Frame(setup, style='Panel.TFrame', padding=1, relief='solid', borderwidth=1)
        field.grid(row=1, column=column, sticky='ew', padx=(0, 12))
        entry = ttk.Entry(field, textvariable=variable, width=20)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Configure>', lambda event: entry.xview_moveto(1) if self.root.focus_get() != entry else None)
        button = ttk.Button(field, text='Обзор…', command=choose)
        button.pack(side='right')
        return entry, button

    def show_about(self, section='О приложении'):
        if self.about_window is not None:
            self.about_window.lift()
            return
        window = self.about_window = tk.Toplevel(self.root)
        window.title('О приложении — BackupTool')
        window.transient(self.root)
        window.configure(background=BG)
        window.geometry('680x590')
        window.minsize(525, 380)
        tab_animation = None

        def close():
            if tab_animation is not None:
                window.after_cancel(tab_animation)
            self.about_window = None
            window.destroy()

        window.protocol('WM_DELETE_WINDOW', close)
        window.bind('<Escape>', lambda event: close())
        content = ttk.Frame(window, padding=(22, 17, 22, 14))
        content.pack(fill='both', expand=True)
        ttk.Label(content, text='BackupTool', style='Title.TLabel').pack(anchor='w')
        ttk.Label(content, text=f'Версия {__version__} · Архив сайтов', style='Muted.TLabel').pack(
            anchor='w', pady=(2, 15))
        notebook = ttk.Notebook(content, style='About.TNotebook')
        notebook.pack(fill='both', expand=True)
        notebook.enable_traversal()

        def add_tab(label, blocks):
            page = ttk.Frame(notebook, style='Panel.TFrame')
            notebook.add(page, text=label)
            article = ScrolledText(page, wrap='word', font=('Segoe UI', 10), relief='flat',
                                   background=PANEL, foreground=INK, padx=17, pady=10)
            article.pack(fill='both', expand=True)
            article.tag_configure('heading', foreground=BLUE, font=('Segoe UI', 11, 'bold'),
                                  spacing1=10, spacing3=5)
            article.tag_configure('body', spacing2=3, spacing3=6)
            for heading, body in blocks:
                article.insert('end', heading + '\n', 'heading')
                article.insert('end', body + '\n', 'body')
            article.configure(state='disabled')
            if section == label:
                notebook.select(page)

        add_tab('О приложении', (
            ('Архив сайтов',
             'BackupTool сохраняет веб-страницы в PDF и скачивает документы по прямым ссылкам. '
             'В архиве хранятся файлы, результаты обработки и история попыток. '
             'По каждому списку можно открыть отчёт Excel.'),
            ('Требования', 'Windows 11 и установленный Google Chrome.'),
        ))
        add_tab('Инструкция', (
            ('1. Подготовьте список',
             'Нажмите «Обзор…» рядом с полем «Список сайтов». Подойдут XLSX, CSV и TSV со столбцом «URL», '
             '«Ссылка», «Адрес» или «Сайт». В TXT укажите по одной ссылке на строку. '
             'Столбцы «ИНН» и «Компания» помогут найти нужную строку.'),
            ('2. Выберите папку и запустите обработку',
             'В поле «Сохранять в» проверьте путь к общей папке. Нажмите «Начать обработку». '
             'Чтобы сначала посмотреть список, выберите «Действия → Загрузить список». '
             'Ход работы виден над таблицей. После остановки можно продолжить с оставшихся сайтов.'),
            ('3. Проверьте результаты',
             'Страницы сохраняются как есть, включая заглушки, ошибки HTTP и проверки доступа. '
             'Внизу PDF указаны адрес, дата и время снимка с часовым поясом и HTTP-статус. '
             'Зелёные строки сохранены, красные завершились ошибкой, жёлтым отмечены сайты, которым нужна помощь. '
             'Выберите строку, чтобы увидеть подробности или открыть файл. «Отчёт Excel» открывает сводку по списку.'),
            ('4. Если сайт требует входа или проверки',
             'Выберите строку и нажмите «Помочь открыть сайт». В открывшемся Chrome войдите или пройдите проверку. '
             'Вернитесь в BackupTool, выберите вкладку и нажмите «Сохранить PDF вкладки». '
             'Если PDF уже есть, нажмите «Прикрепить PDF…».'),
        ))
        add_tab('Changelog', release_notes())
        add_tab('Разработчик', (
            ('Никита Климов', 'Вопросы и предложения можно отправить на nikita.klimov@tedo.ru.'),
        ))

        style = ttk.Style(window)
        inactive, active = (tuple(channel // 257 for channel in window.winfo_rgb(color)) for color in (MUTED, BLUE))
        style.map('About.TNotebook.Tab', foreground=[('selected', BLUE)])

        def select_tab(event):
            nonlocal tab_animation
            if tab_animation is not None:
                window.after_cancel(tab_animation)
            started = time.monotonic()

            def fade():
                nonlocal tab_animation
                elapsed = min(1., (time.monotonic() - started) / .18)
                fraction = 1 - (1 - elapsed) ** 3
                rgb = ''.join(f'{round(a + (b - a) * fraction):02x}' for a, b in zip(inactive, active))
                style.map('About.TNotebook.Tab', foreground=[('selected', '#' + rgb)])
                tab_animation = window.after(16, fade) if elapsed < 1 else None

            fade()

        notebook.bind('<<NotebookTabChanged>>', select_tab)
        close_button = ttk.Button(content, text='Закрыть', style='Primary.TButton', command=close)
        close_button.pack(before=notebook, side='bottom', anchor='e', pady=(10, 0))
        close_button.focus_set()
        if not self.onboarding_seen:
            self.onboarding_seen = True
            self.save_settings()

    def show_log(self):
        if self.log_window is None:
            self.log_window = tk.Toplevel(self.root)
            self.log_window.title('Журнал обработки')
            self.log_window.geometry('820x360')
            self.log_window.protocol('WM_DELETE_WINDOW', self.log_window.withdraw)
            self.log_text = ScrolledText(self.log_window, wrap='word', font=('Consolas', 10), padx=10, pady=10)
            self.log_text.pack(fill='both', expand=True)
            self.log_text.insert('end', ''.join(self.log_lines))
            self.log_text.configure(state='disabled')
        self.log_window.deiconify()
        self.log_window.lift()
        self.log_text.see('end')

    def log(self, text, at=None):
        stamp = time.strftime('%H:%M:%S', time.localtime(at))
        line = f'{stamp}  {" ".join(text.splitlines())}\n'
        full = len(self.log_lines) == self.log_lines.maxlen
        self.log_lines.append(line)
        if self.log_window is not None:
            following = self.log_text.yview()[1] >= 0.999
            self.log_text.configure(state='normal')
            if full:
                self.log_text.delete('1.0', '2.0')
            self.log_text.insert('end', line)
            self.log_text.configure(state='disabled')
            if following:
                self.log_text.see('end')

    def set_status(self, text):
        self.status.set(text)
        self.log(text)

    def layout(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        width = self.root.winfo_width()
        self.status_label.configure(wraplength=max(450, width - 40))
        self.update_status_label.configure(wraplength=max(160, width - 635))
        narrow = width < 1010
        self.tree.configure(style='Compact.Treeview' if narrow else 'Treeview')
        if narrow:
            self.card.grid(row=1, column=0, sticky='ew', padx=0, pady=(10, 0))
            self.body.columnconfigure(1, weight=0, minsize=0)
            self.card_actions.grid(row=1, column=1, sticky='ne', padx=(12, 0))
        else:
            self.card.grid(row=0, column=1, sticky='nsew', padx=(14, 0), pady=0)
            self.body.columnconfigure(1, weight=0, minsize=287)
            self.card_actions.grid(row=2, column=0, sticky='ew', padx=0)
        actions = (self.open_file_button, self.manual_button, self.copy_url_button,
                   self.attach_button, self.history_button, self.tabs, self.save_button)
        cells = ((0, 0, 1), (0, 1, 1), (0, 2, 1), (1, 0, 1), (1, 1, 2), (2, 0, 2), (2, 2, 1)) if narrow else (
            (0, 0, 2), (1, 0, 2), (2, 0, 1), (2, 1, 1), (3, 0, 2), (4, 0, 2), (5, 0, 2))
        for column in range(3):
            self.card_actions.columnconfigure(column, weight=int(narrow or column < 2))
        for widget, (row, column, span) in zip(actions, cells):
            widget.grid(row=row, column=column, columnspan=span, sticky='ew', padx=2, pady=(3, 0))
        self.manual_controls()

    def manual_controls(self):
        for widget in (self.tabs, self.save_button):
            if self.operation == 'manual':
                widget.grid()
            else:
                widget.grid_remove()

    def update_manual_save(self):
        has_tabs = bool(self.tabs['values']) and self.stop_deadline is None
        self.tabs.configure(state='readonly' if has_tabs else 'disabled')
        self.save_button.configure(state='normal' if has_tabs and not self.manual_saving else 'disabled')

    def filter_counts(self, counts):
        totals = dict(counts)
        totals['all'] = sum(counts.values())
        totals['ready'] = sum(counts.get(state, 0) for state in GOOD)
        totals['actions'] = totals['all'] - totals['ready']
        self.filter_combo.configure(values=[f'{title} · {totals.get(key, 0)}' for key, title in FILTERS])
        self.filter_combo.current(next(i for i, (key, _) in enumerate(FILTERS) if key == self.filter_key))

    def select_filter(self, event=None):
        index = self.filter_combo.current()
        if index >= 0:
            self.filter_key = FILTERS[index][0]
            self.filter_changed()

    def read_db(self):
        path = self.archive_root / 'archive.sqlite3'
        if not path.is_file():
            return None
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)
        db.row_factory = sqlite3.Row
        db.create_function('casefold', 1, lambda value: str(value or '').casefold())
        return db

    def load_lists(self):
        self.list_combo.pack_forget()
        db = self.read_db()
        if db is None:
            self.list_ids = []
            self.list_combo.configure(values=[])
            self.list_name.set('')
            return
        try:
            lists = db.execute('SELECT * FROM lists ORDER BY created DESC, id').fetchall()
            self.list_ids = [r['id'] for r in lists]
            versions = Counter(r['name'].casefold() for r in lists)
            repeated = {name for name, count in versions.items() if count > 1}
            names = []
            for row in lists:
                name = row['name']
                key = name.casefold()
                names.append(f'{name} · версия {versions[key]}' if key in repeated else name)
                versions[key] -= 1
            self.list_combo.configure(values=names)
            if self.list_ids:
                if self.list_id not in self.list_ids:
                    self.list_id = self.list_ids[0]
                self.list_combo.current(self.list_ids.index(self.list_id))
            if len(self.list_ids) > 1:
                self.list_combo.pack(side='left', after=self.filter_combo, padx=(7, 0))
        except sqlite3.Error:
            pass
        finally:
            db.close()

    def select_list(self, event=None):
        index = self.list_combo.current()
        if 0 <= index < len(self.list_ids):
            self.list_id = self.list_ids[index]
            self.report = None
            self.filter_changed()

    def query(self, all_rows=False):
        db = self.read_db()
        if db is None or not self.list_id:
            if db:
                db.close()
            return [], 0, {}
        try:
            base = ' FROM input_rows i JOIN resources r ON r.id=i.resource_id'
            has_delivery = db.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone()
            if has_delivery:
                base += (" LEFT JOIN deliveries d ON d.source=r.path"
                         " LEFT JOIN deliveries o ON o.attempt_id=d.attempt_id AND o.kind='original'")
            base += ' WHERE i.list_id=?'
            params = [self.list_id]
            counts = {r['state']: r['n'] for r in db.execute('SELECT r.state,count(*) n' + base + ' GROUP BY r.state', params)}
            if self.filter_key == 'actions':
                base += " AND r.state NOT IN ('ready','document')"
            elif self.filter_key == 'ready':
                base += " AND r.state IN ('ready','document')"
            elif self.filter_key != 'all':
                base += ' AND r.state=?'
                params.append(self.filter_key)
            if self.search.get().strip():
                base += " AND (instr(casefold(i.inn),casefold(?)) OR instr(casefold(i.company),casefold(?)) OR instr(casefold(i.url),casefold(?)))"
                params += [self.search.get().strip()] * 3
            total = db.execute('SELECT count(*)' + base, params).fetchone()[0]
            # Read old archives before the worker has performed the additive migration.
            columns = {r['name'] for r in db.execute('PRAGMA table_info(resources)')}
            metadata = ''.join(f',r.{name}' if name in columns else f',NULL AS {name}'
                               for name in ('http_status', 'checked_at'))
            if has_delivery:
                metadata += ',d.directory,d.candidate,d.delivered,o.candidate AS original_candidate,o.delivered AS original_delivered'
            sql = ('SELECT i.*,r.state,r.reason,r.final_url,r.path,r.quality,r.updated,r.size'
                   + metadata + base + ' ORDER BY i.id')
            if not all_rows:
                sql += ' LIMIT 500 OFFSET ?'
                params.append(self.offset)
            return [dict(r) for r in db.execute(sql, params)], total, counts
        finally:
            db.close()

    def refresh_table(self):
        try:
            rows, total, counts = self.query()
            if not rows and total and self.offset:
                self.offset = ((total - 1) // 500) * 500
                rows, total, counts = self.query()
        except sqlite3.Error:
            return
        selection, focus = self.tree.selection(), self.tree.focus()
        position = self.tree.yview()
        self.filtered_count = total
        self.rows = {str(r['id']): r for r in rows}
        desired = list(self.rows)
        for iid in self.tree.get_children():
            if iid not in self.rows:
                self.tree.delete(iid)
        for i, row in enumerate(rows):
            iid = str(row['id'])
            company = ' '.join(row['company'].split()) or 'Без названия'
            inn = f'\nИНН {row["inn"]}' if row['inn'] else ''
            address = row['url']
            try:
                url = urlsplit(address if '://' in address else '//' + address)
                if url.hostname:
                    path = url.path or '/'
                    if url.query:
                        path += '?' + url.query
                    address = f'{url.netloc}\n{path}'
            except ValueError:
                pass
            state = row['state']
            label = result_label(row).replace(' · ', '\n')
            values = (company + inn, address, '●  ' + label, file_size(row['size']))
            tags = (state, 'alternate') if i % 2 and state not in RESULT_BACKGROUNDS else (state,)
            if self.tree.exists(iid):
                if self.tree.item(iid, 'values') != values or self.tree.item(iid, 'tags') != tags:
                    self.tree.item(iid, values=values, tags=tags)
                self.tree.move(iid, '', i)
            else:
                self.tree.insert('', 'end', iid=iid, values=values, tags=tags)
        self.tree.selection_set(desired if self.all_selected else [s for s in selection if s in self.rows])
        if focus in self.rows:
            self.tree.focus(focus)
        if position:
            self.tree.yview_moveto(position[0])
        ready = sum(counts.get(key, 0) for key in GOOD)
        all_count = sum(counts.values())
        waiting = counts.get('pending', 0)
        running = counts.get('running', 0)
        self.counts.set(f'{ready} готово   ·   {all_count - ready} нужны действия'
                        f'   ·   {waiting} в очереди   ·   {running} в работе' if all_count else 'Список не загружен')
        self.filter_counts(counts)
        if not self.busy():
            unfinished = waiting + running + counts.get('interrupted', 0)
            done = all_count - unfinished
            saved = ready + counts.get('saved_error', 0)
            self.update_progress(done, all_count)
            if self.refresh.get():
                action = '↻  Обновить снимки'
            elif done and unfinished:
                action = '▶  Продолжить'
            elif done and saved < all_count:
                action = '↻  Повторить ошибки'
            else:
                action = '▶  Начать обработку'
            ready_to_run = all_count and (unfinished or self.refresh.get() or saved < all_count)
            self.start_button.configure(text=action, style='Ready.TButton' if ready_to_run else 'Primary.TButton',
                                        state='normal' if all_count or self.source.get().strip() else 'disabled')
            if unfinished:
                self.start_hint.pack(side='left', padx=(3, 0))
            else:
                self.start_hint.pack_forget()
            if all_count and self.phase.get() == 'Подготовка списка':
                self.phase.set('Результаты в архиве' if done else 'Список готов к обработке')
        self.page_info.set(f'{self.offset + 1}–{min(self.offset + 500, total)} из {total}' if total else '0 строк')
        self.previous_page.configure(state='normal' if self.offset else 'disabled')
        self.next_page.configure(state='normal' if self.offset + 500 < total else 'disabled')
        self.copy_filter.configure(text=f'Весь фильтр · {total}', state='normal' if total else 'disabled')
        if rows:
            self.empty_table.place_forget()
        else:
            self.empty_table.configure(text='Нет строк по этому фильтру' if all_count else 'Загрузите список сайтов')
            self.empty_table.place(relx=.5, rely=.5, anchor='center')
        self.selected()

    def update_progress(self, done, total):
        self.progress.set(done, total)
        self.progress_text.set(f'{done} из {total}')

    def filter_changed(self):
        self.offset = 0
        self.all_selected = False
        self.dirty = True

    def mark_dirty(self, *args):
        self.dirty = True

    def page(self, direction):
        self.offset = max(0, min(max(0, ((self.filtered_count-1)//500)*500), self.offset + direction*500))
        self.all_selected = False
        self.dirty = True

    def click_row(self, event):
        if self.tree.identify_row(event.y):
            self.all_selected = False
            col = self.tree.identify_column(event.x)
            if col and col != '#0':
                self.active_column = self.tree.column(col, 'id')

    def select_all(self, event=None):
        self.all_selected = True
        self.tree.selection_set(self.tree.get_children())
        self.selected()
        return 'break'

    def extend_selection(self, direction):
        children = self.tree.get_children()
        current = self.tree.focus()
        if current in children:
            index = max(0, min(len(children)-1, children.index(current) + direction))
            self.tree.selection_add(children[index])
            self.tree.focus(children[index])
            self.tree.see(children[index])
        return 'break'

    def selected_row(self):
        focus = self.tree.focus()
        selection = self.tree.selection()
        return self.rows.get(focus if focus in selection else (selection[0] if selection else ''))

    def selected(self, event=None):
        count = self.filtered_count if self.all_selected else len(self.tree.selection())
        self.selection_info.set(f'Выбрано: {count}' + (' (весь фильтр)' if self.all_selected else ''))
        row = self.selected_row()
        self.detail_caption.set(f'Выбранная строка · {row["number"]}' if row else 'Выбранная строка')
        self.update_row_actions(row)
        parts = [('Выберите строку в таблице.\n', 'title'),
                 ('Здесь появятся адрес, результат и действия для сайта.', 'muted')]
        if row:
            state = row['state']
            parts = [(f'{row["company"] or "Без названия"}\n', 'title'),
                     (f'ИНН {row["inn"] or "—"} · {row["sheet"]}, строка {row["number"]}\n\n', 'muted'),
                     (f'●  {result_label(row)}\n', state),
                     ('Исходная ссылка\n', 'muted'), (f'{row["url"]}\n', 'url')]
            if row.get('checked_at'):
                stamp = format_capture_time(row['checked_at'])
                parts.extend([('Дата и время снимка\n', 'muted'), (f'{stamp}\n', '')])
            if row.get('http_status'):
                parts.extend([('HTTP-статус\n', 'muted'), (f'{row["http_status"]}\n', '')])
            if row['final_url'] and row['final_url'] != row['url']:
                parts.extend([('Конечный адрес\n', 'muted'), (f'{row["final_url"]}\n', 'url')])
            if row['reason']:
                caption = 'Состояние сайта' if state in SAVED else 'Причина'
                parts.extend([(f'\n{caption}\n', 'muted'), (f'{row["reason"]}\n', 'reason')])
            if row['path']:
                label = 'Сохранённый файл' if state in SAVED else 'Файл предыдущей попытки'
                parts.extend([(f'\n{label} · {file_size(row["size"])}\n', 'muted'),
                              (row['candidate'] if row.get('delivered') else Path(row['path']).name, '')])
                if row['quality'] == 'dated_document':
                    parts.extend([('\nИсходный PDF\n', 'muted'),
                                  (row['original_candidate'] if row.get('original_delivered') else
                                   original_path(Path(row['path'])).name, '')])
                if row.get('directory') and (not row.get('delivered') or row.get('original_delivered') == 0):
                    parts.append(('\nЕсть файлы, ожидающие отправки\n', 'reason'))
        if self.detail_parts != parts:
            self.detail_parts = parts
            self.detail.configure(state='normal')
            self.detail.delete('1.0', 'end')
            for text, tag in parts:
                self.detail.insert('end', text, tag)
            self.detail.configure(state='disabled')

    def update_row_actions(self, row):
        has_file = bool(row and row['path'])
        good = bool(row and row['state'] in GOOD and has_file)
        self.open_file_button.configure(
            text='Открыть PDF' if has_file and row['path'].lower().endswith('.pdf') else 'Открыть файл',
            state='normal' if has_file else 'disabled', style='Primary.TButton' if good else 'TButton')
        guided = row and (not good or self.operation == 'manual')
        self.manual_button.configure(text='1  Помочь открыть сайт' if guided else 'Помочь открыть сайт',
                                     state='normal' if row and not self.busy() else 'disabled',
                                     style='Ready.TButton' if guided else 'TButton')
        self.attach_button.configure(state='normal' if row and not self.busy() else 'disabled')
        for widget in (self.copy_url_button, self.history_button):
            widget.configure(state='normal' if row else 'disabled')

    def show_history(self):
        row = self.selected_row()
        db = self.read_db() if row else None
        if db is None:
            return
        try:
            attempts = db.execute('''SELECT * FROM attempts
                WHERE resource_id=? ORDER BY started DESC LIMIT 200''', (row['resource_id'],)).fetchall()
            deliveries = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone():
                for item in db.execute('''SELECT d.* FROM deliveries d JOIN attempts a ON a.id=d.attempt_id
                                          WHERE a.resource_id=?''', (row['resource_id'],)):
                    deliveries[item['attempt_id'], item['kind']] = item
        except sqlite3.Error as error:
            return self.error(error)
        finally:
            db.close()
        dialog = tk.Toplevel(self.root)
        dialog.title('История попыток')
        dialog.transient(self.root)
        dialog.geometry('620x340')
        panel = ttk.Frame(dialog, padding=14)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=row['company'] or row['url'], style='Section.TLabel', wraplength=570).pack(anchor='w')
        history = ScrolledText(panel, wrap='word', background=PANEL, foreground=INK, relief='flat', padx=8, pady=8)
        history.pack(fill='both', expand=True, pady=8)
        for index, attempt in enumerate(attempts):
            stamp = time.strftime('%d.%m.%Y %H:%M', time.localtime(attempt['ended'] or attempt['started']))
            history.insert('end', f'{stamp}   {result_label(dict(attempt))}\n')
            if 'checked_at' in attempt.keys() and attempt['checked_at']:
                stamp = format_capture_time(attempt['checked_at'])
                history.insert('end', f'Дата и время снимка: {stamp}\n')
            for key in ('reason', 'final_url'):
                if attempt[key]:
                    history.insert('end', attempt[key] + '\n')
            if attempt['state'] in SAVED and attempt['target']:
                target = self.archive_root / attempt['target']
                files = [('backup', 'Открыть файл попытки', target)]
                if 'original_sha' in attempt.keys() and attempt['original_sha']:
                    files.append(('original', 'Открыть исходный PDF', original_path(target)))
                for kind, label, local in files:
                    delivery = deliveries.get((attempt['id'], kind))
                    path = local
                    if delivery:
                        if delivery['delivered']:
                            path = Path(delivery['directory']) / delivery['candidate']
                        else:
                            history.insert('end', f'Ожидает отправки: {delivery["name"]}\n')
                    tag = f'file-{index}-{kind}'
                    history.insert('end', f'{label} · {path.name}\n', tag)
                    history.tag_configure(tag, foreground=BLUE, underline=True)
                    history.tag_bind(tag, '<Button-1>',
                                     lambda event, file=path, fallback=local: self.open_backup(file, fallback))
            history.insert('end', '\n')
        if not attempts:
            history.insert('end', 'Попыток сохранения пока нет.')
        history.configure(state='disabled')
        ttk.Button(panel, text='Закрыть', command=dialog.destroy).pack(anchor='e')
        dialog.bind('<Escape>', lambda event: dialog.destroy())

    def populate_copy(self, menu):
        for title, kind in [('Выбранные строки   Ctrl+C', 'rows'), ('Весь фильтр', 'filter'),
                            ('Ячейка   Alt+C', 'cell'), ('URL   Alt+U', 'url'), ('ИНН   Alt+I', 'inn'),
                            ('Причина   Alt+R', 'reason')]:
            menu.add_command(label=title, command=lambda k=kind: self.copy(k))

    def context(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.all_selected = False
                self.tree.selection_set(iid)
            self.tree.focus(iid)
        col = self.tree.identify_column(event.x)
        if col and col != '#0':
            self.active_column = self.tree.column(col, 'id')
        menu = tk.Menu(self.tree, tearoff=False)
        self.populate_copy(menu)
        menu.tk_popup(event.x_root, event.y_root)

    def copy(self, kind):
        try:
            if kind == 'filter' or (kind == 'rows' and self.all_selected):
                rows = self.query(all_rows=True)[0]
            elif kind == 'rows':
                rows = [self.rows[s] for s in self.tree.selection() if s in self.rows]
            else:
                row = self.selected_row()
                rows = [row] if row else []
            if not rows:
                return 'break'
            if kind in ('rows', 'filter'):
                values = [REPORT_HEADERS] + [report_values(r, self.archive_root) for r in rows]
            else:
                key = self.active_column if kind == 'cell' else kind
                value = rows[0][key]
                if key == 'state':
                    value = result_label(rows[0])
                elif key == 'size':
                    value = file_size(value)
                values = [[value]]
            self.clipboard.write(clipboard_payload(values, text_columns=(0,)))
            self.status.set(f'Скопировано: {len(rows)} строк' if kind in ('rows', 'filter') else 'Скопировано')
        except Exception as error:
            self.error(error)
        return 'break'

    def copy_detail(self, event=None):
        try:
            text = self.detail.get('sel.first', 'sel.last')
            self.clipboard.write(clipboard_payload([[text]]))
        except tk.TclError:
            pass
        return 'break'

    def browse_source(self):
        path = filedialog.askopenfilename(title='Список сайтов', filetypes=[('Списки', '*.xlsx *.csv *.tsv *.txt')])
        if path:
            self.source.set(path)
            self.source_entry.xview_moveto(1)

    def source_changed(self, *args):
        self.list_id = None
        self.report = None
        self.phase.set('Подготовка списка')
        self.status.set('Нажмите «Начать обработку» или загрузите список через меню «Действия».')
        self.filter_changed()

    def browse_archive(self):
        path = filedialog.askdirectory(title='Папка для бэкапов')
        if path:
            self.destination.set(path)
            self.archive_entry.xview_moveto(1)
            self.save_settings()

    def destination_changed(self, *args):
        path = self.destination.get().strip()
        if not path:
            warning = 'Укажите путь к общей папке.'
        elif is_network_path(path):
            warning = ''
        else:
            warning = ('Эта папка находится на локальном диске. Выберите общую сетевую папку, '
                       'чтобы коллеги могли открыть бэкапы')
        self.destination_warning.set(warning)

    def refresh_pending_delivery(self):
        db = self.read_db()
        count = 0
        if db is not None:
            try:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone():
                    count = db.execute('SELECT count(*) FROM deliveries WHERE delivered=0').fetchone()[0]
            except sqlite3.Error:
                return
            finally:
                db.close()
        if count:
            self.pending_delivery_label.set(f'Не отправлено: {count}')
            self.delivery_button.pack(side='left')
        else:
            self.delivery_button.pack_forget()

    def show_pending_delivery(self):
        db = self.read_db()
        if db is None:
            return
        try:
            files = db.execute('''SELECT d.*, b.candidate AS backup_candidate, b.delivered AS backup_delivered
                FROM deliveries d LEFT JOIN deliveries b ON b.attempt_id=d.attempt_id AND b.kind='backup'
                WHERE d.delivered=0 ORDER BY d.directory,d.name''').fetchall()
        except sqlite3.Error as error:
            return self.error(error)
        finally:
            db.close()
        if not files:
            self.refresh_pending_delivery()
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Неотправленные файлы')
        dialog.transient(self.root)
        dialog.geometry('720x400')
        panel = ttk.Frame(dialog, padding=14)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=f'Ожидают отправки: {len(files)}', style='Section.TLabel').pack(anchor='w')
        contents = ScrolledText(panel, wrap='word', background=PANEL, foreground=INK, relief='flat', padx=8, pady=8)
        contents.pack(fill='both', expand=True, pady=8)
        for item in files:
            name = item['candidate'] or item['name']
            if item['kind'] == 'original' and not item['candidate'] and item['backup_delivered']:
                name = original_path(Path(item['backup_candidate'])).name
            reason = item['error'] or 'Ожидает отправки'
            contents.insert('end', f'{Path(item["directory"]) / name}\n{reason}\n\n')
        contents.configure(state='disabled')

        def retry():
            dialog.destroy()
            self.start('retry_delivery')

        ttk.Button(panel, text='Повторить отправку', command=retry).pack(side='left')
        ttk.Button(panel, text='Закрыть', command=dialog.destroy).pack(side='right')
        dialog.bind('<Escape>', lambda event: dialog.destroy())

    def busy(self):
        return self.session.state in ('starting', 'running', 'stopping')

    def set_busy(self, active):
        for widget in (self.start_button, self.choose_source, self.choose_archive, self.options,
                       self.source_entry, self.archive_entry, self.delivery_button):
            widget.configure(state='disabled' if active else 'normal')
        for widget in (self.list_combo, self.mode):
            widget.configure(state='disabled' if active else 'readonly')
        self.stop_button.configure(state='normal' if active else 'disabled')
        self.save_button.configure(state='disabled')
        self.tabs.configure(state='disabled', values=())
        self.tab.set('')
        self.manual_saving = False
        self.start_hint.pack_forget()
        self.progress.animate(active and self.operation == 'batch')
        self.update_row_actions(self.selected_row())
        self.manual_controls()

    def start(self, operation, **extra):
        if self.update_task == 'download':
            self.set_status('Дождитесь окончания загрузки обновления.')
            return
        if self.busy():
            return
        destination = self.destination.get().strip()
        if not destination and operation in ('batch', 'manual', 'attach'):
            return self.error('Укажите папку назначения для бэкапов')
        source = self.source.get().strip()
        if operation == 'import' and not source:
            return self.error('Выберите список')
        payload = {'operation': operation, 'archive': str(self.archive_root), 'destination': destination,
                   'list_id': self.list_id,
                   'gentle': self.gentle.get(), 'refresh': self.refresh.get(), **extra}
        if operation == 'import' or (operation == 'batch' and not self.list_id):
            if not source:
                return self.error('Сначала выберите и загрузите список')
            payload['source'] = source
        try:
            self.session.start(payload=payload)
            self.operation = operation
            self.stop_deadline = None
            self.set_busy(True)
            self.phase.set({'batch': 'Автоматическая обработка', 'import': 'Загрузка списка',
                            'manual': 'Ручная помощь', 'attach': 'Прикрепление PDF',
                            'verify': 'Проверка файлов', 'export': 'Выгрузка отчёта',
                            'retry_delivery': 'Повторная отправка файлов'}[operation])
            self.set_status('Запуск…')
            self.update_progress(0, 0)
            self.save_settings()
        except Exception as error:
            self.error(error)

    def manual(self):
        row = self.selected_row()
        if row:
            self.start('manual', row_id=row['id'])
        else:
            self.status.set('Сначала выберите строку для ручной помощи')

    def save_manual(self):
        if self.busy() and self.operation == 'manual' and not self.save_button.instate(['disabled']):
            self.session.send('save_manual', tab=self.tabs.current())
            self.manual_saving = True
            self.save_button.configure(state='disabled')
            self.set_status('Сохранение выбранной вкладки…')

    def attach(self):
        row = self.selected_row()
        if not row:
            return
        path = filedialog.askopenfilename(title='Прикрепить PDF к выбранной строке', filetypes=[('PDF', '*.pdf')])
        if path:
            self.start('attach', row_id=row['id'], pdf=path)

    def stop(self):
        self.session.cancel()
        self.stop_deadline = time.monotonic() + 8
        self.stop_button.configure(state='disabled')
        self.save_button.configure(state='disabled')
        self.tabs.configure(state='disabled')
        self.phase.set('Остановка обработки')
        self.set_status('Остановка и сохранение состояния…')

    def poll(self):
        if self.poll_update():
            return
        for event in self.session.poll():
            kind = event['type']
            if kind == 'loaded':
                self.list_id = event['list_id']
                self.load_lists()
                self.dirty = True
            elif kind == 'log':
                self.log(event['text'], event['at'])
            elif kind == 'progress':
                text = event.get('text', '')
                self.status.set(text if len(text) <= 150 else text[:147] + '…')
                self.update_progress(event['done'], event['total'])
                self.dirty = True
            elif kind == 'notice':
                self.set_status(event.get('text', ''))
                if self.operation == 'manual':
                    self.manual_saving = False
                    self.update_manual_save()
            elif kind == 'manual_ready':
                self.set_status('Завершите вход или проверку в Chrome. Затем выберите вкладку и нажмите «Сохранить PDF вкладки».')
            elif kind == 'tabs':
                selected = self.tabs.current()
                self.tabs.configure(values=[f'{t["index"]+1}: {t["url"]}' for t in event['tabs']])
                if event['tabs']:
                    self.tabs.current(min(max(0, selected), len(event['tabs'])-1))
                else:
                    self.tab.set('')
                self.update_manual_save()
            elif kind == 'report':
                self.report = event['path']
                self.log(f'Отчёт сохранён · {self.report}')
            elif kind == 'delivery':
                self.phase.set('Отправка файлов в папку назначения')
                self.set_status('Проверка доступа и копирование…')
            elif kind == 'final':
                self.dirty = True
                finished = {'batch': 'Автоматическая обработка завершена', 'import': 'Список готов к обработке',
                            'manual': 'PDF сохранён', 'attach': 'PDF прикреплён', 'verify': 'Проверка завершена',
                            'export': 'Отчёт готов'}
                self.phase.set({'success': finished.get(self.operation, 'Операция завершена'),
                                'cancelled': 'Обработка остановлена',
                                'error': 'Не удалось завершить операцию'}.get(event['outcome'], event['outcome']))
                self.set_status({'success': 'Завершено. Готовые файлы и нерешённые строки учтены в архиве.',
                                 'cancelled': 'Результаты сохранены. Можно продолжить позже.',
                                 'error': 'Ошибка: ' + event.get('reason', '')}.get(event['outcome'], event['outcome']))
                if event.get('report_error'):
                    self.set_status('Файлы учтены. Ошибка выгрузки отчёта: ' + event['report_error'])
                if event.get('pending_delivery'):
                    self.phase.set('Остались неотправленные файлы')
                    self.set_status(f'Не отправлено файлов: {event["pending_delivery"]}. '
                                    'Откройте список неотправленных файлов и повторите отправку.')
                elif self.operation == 'retry_delivery' and event['outcome'] == 'success':
                    self.set_status('Все файлы отправлены в папки назначения.')
                if self.operation == 'import' and event['outcome'] == 'success' and not event.get('report_error'):
                    self.set_status('Список загружен. Нажмите «Начать обработку».')
        if self.session.state == 'finished' and self.operation:
            self.operation = None
            self.set_busy(False)
            self.dirty = True
            self.stop_deadline = None
            self.save_settings()
        if self.stop_deadline and time.monotonic() > self.stop_deadline:
            self.session.close(timeout=.1)
            self.set_status('Рабочий процесс остановлен. Незавершённые строки восстановятся при продолжении.')
            self.dirty = True
        if self.dirty and time.monotonic() >= self.next_read:
            self.refresh_table()
            self.refresh_pending_delivery()
            self.dirty = False
            self.next_read = time.monotonic() + .5
        if self.closing and not self.busy():
            self.root.destroy()
            return
        self.root.after(150, self.poll)

    def save_settings(self):
        data = {'source': self.source.get(), 'archive_root': str(self.archive_root),
                'destination': self.destination.get().strip(),
                'gentle': self.gentle.get(), 'list_id': self.list_id, 'onboarding_seen': self.onboarding_seen}
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass

    def open_archive(self):
        path = self.destination.get().strip()
        if path:
            try:
                os.startfile(path)
            except OSError as error:
                self.error(error)

    def open_artifact(self):
        row = self.selected_row()
        if row and row['path']:
            local = self.archive_root / row['path']
            path = Path(row['directory']) / row['candidate'] if row.get('delivered') else local
            self.open_backup(path, local)

    def open_backup(self, path, local):
        try:
            os.startfile(path)
        except OSError:
            try:
                os.startfile(local)
            except OSError as error:
                self.error(error)

    def open_report(self):
        if self.report and Path(self.report).is_file():
            os.startfile(self.report)
            return
        if self.list_id:
            path = self.archive_root / 'exports' / self.list_id[:12]
            reports = sorted(path.glob('report-*.xlsx'), reverse=True)
            if reports:
                os.startfile(reports[0])
                return
        self.status.set('Отчёт появится после обработки. Его можно создать через меню «Действия».')

    def error(self, error):
        self.set_status(str(error))
        messagebox.showerror('BackupTool', str(error), parent=self.root)

    def check_or_update(self):
        if self.update_task:
            return
        if self.available_update:
            if not self.executable:
                return self.error('Обновление устанавливается из собранной версии BackupTool для Windows.')
            if self.busy():
                self.set_status('Установка доступна после завершения обработки.')
                return
            update = self.available_update
            notes = f'\n\n{update.notes}' if update.notes else ''
            if not messagebox.askyesno('Обновление BackupTool',
                                       f'Установить версию {update.version} и перезапустить BackupTool?{notes}',
                                       parent=self.root):
                return
            self.begin_update('download', updates.prepare, update, self.executable)
        else:
            self.begin_update('check', updates.latest_update, __version__)

    def begin_update(self, task, action, *args):
        self.update_task = task
        self.update_status.set('')
        self.update_button.configure(state='disabled')
        self.update_label.set('Проверка обновлений…' if task == 'check' else 'Загрузка обновления…')

        def run():
            try:
                result = action(*args)
            except Exception as error:
                self.update_events.put((False, str(error)))
            else:
                self.update_events.put((True, result))

        Thread(target=run, daemon=True).start()

    def poll_update(self):
        try:
            success, result = self.update_events.get_nowait()
        except Empty:
            return False
        task, self.update_task = self.update_task, None
        self.update_button.configure(state='normal')
        if success and task == 'check':
            self.available_update = result
            if result is None:
                self.update_status.set(f'Доступных обновлений для BackupTool {__version__} нет.')
        self.update_label.set(f'Обновить до {self.available_update.version}' if self.available_update
                              else 'Проверить обновления')
        if not success:
            self.error('Ошибка обновления: ' + result)
        elif task == 'download':
            try:
                self.save_settings()
                updates.install(result)
            except OSError as error:
                self.error(error)
            else:
                self.root.destroy()
                return True
        return False

    def close(self):
        if self.update_task == 'download':
            self.set_status('Дождитесь окончания загрузки обновления.')
            return
        self.save_settings()
        if self.busy():
            self.closing = True
            self.stop()
            self.status.set('Завершение рабочего процесса…')
        else:
            self.root.destroy()


def run_shell(report=None, scenario='normal'):
    if report:
        from .probe_ui import run_shell as probe
        return probe(report, scenario)
    root = tk.Tk()
    Application(root)
    root.mainloop()
