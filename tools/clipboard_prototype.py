"""Real Tkinter/Excel integration prototype for iteration 1.1."""
import json
import hashlib
from pathlib import Path
import tkinter as tk
from tkinter import font, ttk

from backuptool.clipboard import WindowsClipboard, clipboard_payload

ROOT = Path(__file__).resolve().parents[1]

HEADERS = ('ИНН', 'Компания', 'URL', 'Результат', 'Причина')
ROWS = (
    ('0123456789', 'ООО Кириллица', 'https://example.invalid/Очень/длинный/путь?query=' + 'abc123' * 25, 'Нужна помощь', 'Полная причина: требуется проверка страницы.\nВторая строка причины.'),
    ('000123456789', '=1+1', 'https://example.invalid/formula-equals', 'Ошибка', '=1+1'),
    ('0123456780', '+1+1', 'https://example.invalid/formula-plus', 'Ошибка', '+1+1'),
    ('0123456781', '-1+1', 'https://example.invalid/formula-minus', 'Ошибка', '-1+1'),
    ('0123456782', '@SUM(1;1)', 'https://example.invalid/formula-at', 'Ошибка', '@SUM(1;1)'),
    ('0123456783', 'Готовая компания', 'https://example.invalid/ready', 'Готово', 'Без замечаний'),
)


def run():
    root = tk.Tk()
    root.title('BackupTool — прототип копирования 1.1')
    root.geometry('1180x720')
    root.minsize(900, 620)
    clipboard = WindowsClipboard(root.winfo_id())
    initial = tuple(ROWS)
    status = tk.StringVar(value='Выберите строки. Ctrl+A — весь фильтр, Ctrl+C — копировать.')
    selected_column = tk.StringVar(value='URL')
    filter_value = tk.StringVar(value='Все строки')
    log = []
    version = {name:hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
               for name in ('backuptool/clipboard.py','tools/clipboard_prototype.py')}
    selection_anchor = [None]
    report_dir = ROOT / 'reports/iterations/1.1'
    report_dir.mkdir(parents=True, exist_ok=True)
    outer = ttk.Frame(root, padding=16)
    outer.pack(fill='both', expand=True)
    outer.rowconfigure(2, weight=1)
    outer.columnconfigure(0, weight=1)
    top = ttk.Frame(outer)
    top.grid(row=0, column=0, sticky='ew')
    title_label = ttk.Label(top, text='Прототип Tkinter / Excel', font=('Segoe UI', 14))
    title_label.pack(side='left')
    filter_box = ttk.Combobox(top, textvariable=filter_value, values=('Все строки','Нужны действия'), state='readonly', width=22)
    filter_box.pack(side='left', padx=16)
    ttk.Label(outer, textvariable=status, wraplength=1100).grid(row=1, column=0, sticky='w', pady=12)
    table_frame = ttk.Frame(outer)
    table_frame.grid(row=2, column=0, sticky='nsew')
    table_frame.rowconfigure(0, weight=1)
    table_frame.columnconfigure(0, weight=1)
    tree = ttk.Treeview(table_frame, columns=HEADERS, show='headings', selectmode='extended')
    for name, width in zip(HEADERS, (130,180,320,130,280)):
        tree.heading(name, text=name)
        tree.column(name, width=width, minwidth=80, stretch=False)
    tree.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(table_frame, orient='horizontal', command=tree.xview)
    scrollbar.grid(row=1, column=0, sticky='ew')
    tree.configure(xscrollcommand=scrollbar.set)
    vertical = ttk.Scrollbar(table_frame, orient='vertical', command=tree.yview)
    vertical.grid(row=0, column=1, sticky='ns')
    tree.configure(yscrollcommand=vertical.set)
    reason = tk.Text(outer, height=4, wrap='word', font=('Segoe UI', 10))
    reason.grid(row=3, column=0, sticky='ew', pady=10)
    bar = ttk.Frame(outer)
    bar.grid(row=4, column=0, sticky='ew')

    def visible_ids():
        return tuple(tree.get_children())

    def selected_ids():
        selected = set(tree.selection())
        return tuple(i for i in visible_ids() if i in selected)

    def copy(kind='rows'):
        ids = selected_ids()
        if kind == 'filter':
            ids = visible_ids()
        if not ids:
            status.set('Сначала выберите строку.')
            return 'break'
        if kind in ('rows','filter'):
            data = (HEADERS,) + tuple(ROWS[int(i)] for i in ids)
            text_columns = (0,)
        else:
            column = {'url':2,'inn':0,'reason':4}.get(kind, HEADERS.index(selected_column.get()))
            focused = tree.focus()
            ids = (focused if focused in ids else ids[0],)
            data = ((ROWS[int(ids[0])][column],),)
            text_columns = (0,) if column == 0 else ()
        try:
            payload = clipboard_payload(data, text_columns)
            clipboard.write(payload)
            status.set(f'Скопировано: {kind}, строк данных {len(ids)}. Выбрано: {len(selected_ids())}.')
            log.append({'kind':kind, 'ids':ids, 'rows':data, 'scale':'100%',
                        'code_sha256':version,
                        'selection_after':selected_ids(), 'model_unchanged':tuple(ROWS) == initial,
                        'html_roundtrip':clipboard.read('html') == payload['html'],
                        'unicode_roundtrip':clipboard.read() == payload['text']})
            (report_dir / 'prototype-actions.json').write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as error:
            status.set(f'Не удалось скопировать: {error}')
        return 'break'

    def select_all(_=None):
        tree.selection_set(visible_ids())
        status.set(f'Выбрано: {len(selected_ids())} строк текущего фильтра.')
        return 'break'

    def extend_selection(delta):
        ids = visible_ids()
        current = tree.focus()
        if current not in ids:
            return 'break'
        if selection_anchor[0] not in ids:
            selection_anchor[0] = current
        target = ids[min(len(ids)-1,max(0,ids.index(current) + delta))]
        left,right = sorted((ids.index(selection_anchor[0]),ids.index(target)))
        tree.selection_set(ids[left:right+1])
        tree.focus(target)
        tree.see(target)
        return 'break'

    def set_anchor(event):
        row = tree.identify_row(event.y)
        if row:
            selection_anchor[0] = row

    def selection(_=None):
        ids = selected_ids()
        reason.delete('1.0','end')
        if ids:
            reason.insert('1.0', ROWS[int(ids[0])][4])

    def populate(_=None):
        tree.delete(*tree.get_children())
        for i,row in enumerate(ROWS):
            if filter_value.get() == 'Нужны действия' and row[3] == 'Готово':
                continue
            tree.insert('', 'end', iid=str(i), values=row)
        tree.selection_set('0')
        tree.focus('0')
        selection_anchor[0] = '0'
        tree.focus_set()

    def configure_style():
        for name in ('TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont'):
            font.nametofont(name).configure(size=-14)
        ttk.Style().configure('Treeview', rowheight=42)
        reason.configure(font=('Segoe UI',-14))
        title_label.configure(font=('Segoe UI',-19))
        for name,width in zip(HEADERS,(150,190,330,150,300)):
            tree.column(name,width=width,stretch=False)

    def context(event):
        row = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not row:
            return
        if row not in tree.selection():
            tree.selection_set(row)
        tree.focus(row)
        if column and column != '#0':
            selected_column.set(HEADERS[int(column[1:]) - 1])
        menu.tk_popup(event.x_root, event.y_root)

    menu = tk.Menu(root, tearoff=False)
    for title,kind,shortcut in [('Выбранные строки','rows','Ctrl+C'),('Ячейку','cell','Alt+C'),('URL','url','Alt+U'),('ИНН','inn','Alt+I'),('Причину','reason','Alt+R'),('Весь фильтр','filter','')]:
        menu.add_command(label=title, accelerator=shortcut, command=lambda value=kind:copy(value))
    ttk.Menubutton(bar, text='Копировать', menu=menu, direction='above').pack(side='left')
    ttk.Label(bar, text='Ctrl+C — выбранные строки. Остальные команды — в меню и по правой кнопке мыши.').pack(side='left', padx=12)
    tree.bind('<Control-a>', select_all)
    tree.bind('<Shift-Down>', lambda _:extend_selection(1))
    tree.bind('<Shift-Up>', lambda _:extend_selection(-1))
    tree.bind('<Button-1>', set_anchor, add=True)
    tree.bind('<Control-c>', lambda _:copy())
    tree.bind('<Button-3>', context)
    tree.bind('<Shift-F10>', lambda _:menu.tk_popup(tree.winfo_rootx()+60, tree.winfo_rooty()+60))
    tree.bind('<<TreeviewSelect>>', selection)
    def copy_reason_selection(_):
        try:
            value = reason.get('sel.first','sel.last')
        except tk.TclError:
            return 'break'
        payload = clipboard_payload(((value,),))
        clipboard.write(payload)
        log.append({'kind':'reason_selection','rows':((value,),),'scale':'100%',
                    'code_sha256':version,
                    'selection_after':selected_ids(),'model_unchanged':tuple(ROWS) == initial,
                    'html_roundtrip':clipboard.read('html') == payload['html'],
                    'unicode_roundtrip':clipboard.read() == payload['text']})
        (report_dir / 'prototype-actions.json').write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
        return 'break'
    reason.bind('<Control-c>', copy_reason_selection)
    for key,kind in [('u','url'),('i','inn'),('r','reason'),('c','cell')]:
        root.bind(f'<Alt-{key}>',lambda _,value=kind:copy(value))
    filter_box.bind('<<ComboboxSelected>>', populate)
    populate()
    configure_style()
    root.mainloop()


if __name__ == '__main__':
    run()
