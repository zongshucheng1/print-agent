"""Printer list and editor for the Windows management client."""
import tkinter as tk
from tkinter import font as tkfont, ttk

from .desktop_add import queue_connection_type
from .desktop_table import ClientTable
from .desktop_theme import BG, BLUE, BORDER, FONT, INK, MUTED, icon
from .desktop_widgets import AutoScrollbar


RUNTIME_STATES = {
    'STARTING': '正在连接', 'RUNNING': '运行中', 'PAUSED': '已暂停',
    'ATTENTION': '需要检查', 'FAILED': '运行异常', 'STOPPED': '服务未运行',
}


class DevicePage:
    def __init__(self, app, page):
        self.app = app
        self.page = page
        self.query = tk.StringVar(page)
        self.row_actions = {}
        self.action_layout_ticket = None
        self.column_widths = None
        self._styles()
        self.px = lambda value: max(1, round(value * page.winfo_fpixels('1i') / 96))
        canvas = app.device_scroll = tk.Canvas(page, background=BG, highlightthickness=0)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        bar = AutoScrollbar(page, orient='vertical', command=canvas.yview)
        bar.grid(row=0, column=1, sticky='ns')
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        self.body = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, anchor='nw', window=self.body)
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        self.body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        self._list()
        self._editor()
        app.root.bind('<MouseWheel>', self._scroll_page, add='+')
        self.query.trace_add('write', lambda *_: self.render(app.saved.get('printers', [])))

    def _styles(self):
        style = ttk.Style(self.page)
        self.body_font = tkfont.Font(self.page, family=FONT, size=12)
        self.name_font = tkfont.Font(self.page, family=FONT, size=12, weight='bold')
        self.meta_font = tkfont.Font(self.page, family=FONT, size=10)
        self.heading_font = tkfont.Font(self.page, family=FONT, size=11, weight='bold')
        self.cell_padding = max(9, self.body_font.measure('中') // 2)
        self.row_height = max(self.body_font.metrics('linespace') * 2 + self.cell_padding,
                              round(self.page.winfo_fpixels('44p')))
        style.layout('Devices.Treeview', [('Treeview.treearea', {'sticky': 'nsew'})])
        style.layout('Devices.Treeview.Heading', [
            ('Treeheading.cell', {'sticky': 'nsew'}),
            ('Treeheading.padding', {'sticky': 'nsew', 'children': [('Treeheading.text', {'sticky': 'ew'})]})])
        style.configure('Devices.Treeview', rowheight=self.row_height, font=self.body_font,
                        background='white', fieldbackground='white', borderwidth=0, relief='flat')
        style.configure('Devices.Treeview.Heading', font=self.heading_font,
                        borderwidth=0, relief='flat', padding=(8,7), background='#eaf0f7')
        style.map('Devices.Treeview', background=[('selected', '#e4eeff')],
                  foreground=[('selected', '#173563')])
        style.configure('DeviceHeading.TLabel', font=(FONT, 16, 'bold'),
                        foreground='#142945', background='white')
        style.configure('DeviceMuted.TLabel', font=(FONT, 11),
                        foreground='#536880', background='white')
        style.configure('DeviceField.TLabel', font=self.name_font, foreground=INK, background='white')
        style.configure('Search.TEntry', padding=(round(self.page.winfo_fpixels('27p')), 6, 8, 6))
        style.configure('DeviceSave.TButton', foreground=BLUE, font=(FONT, 12, 'bold'))
        for selected, background in (('', 'white'), ('Active', '#e5efff')):
            for kind, color in (('Edit', BLUE), ('Delete', '#bd3439')):
                name = f'{selected}Device{kind}.TButton'
                style.layout(name, [('Button.label', {'sticky': 'nsew'})])
                style.configure(name, foreground=color, background=background, anchor='center',
                                font=self.body_font, borderwidth=0, padding=(2, 1))
                style.map(name, foreground=[('disabled', '#8792a2')], background=[('active', '#dce9ff')])

    def _scroll_page(self, event):
        widget = event.widget
        while isinstance(widget, tk.Misc):
            if widget is self.app.devices:
                return
            if widget is self.body:
                self.app.device_scroll.yview_scroll(-int(event.delta / 120), 'units')
                return
            widget = widget.master

    def _list(self):
        app = self.app
        card = ttk.Frame(self.body, style='Card.TFrame', padding=self.px(16))
        card.pack(fill='x')
        title = ttk.Frame(card, style='Surface.TFrame')
        title.pack(fill='x')
        title.columnconfigure(1, weight=1)
        list_title = ttk.Label(title, text='设备列表', style='DeviceHeading.TLabel')
        list_title.grid(row=0, column=0, sticky='w', padx=(0, self.px(22)))
        search = ttk.Frame(title, style='Surface.TFrame')
        search.grid(row=0, column=1, sticky='ew')
        self.search_entry = ttk.Entry(search, style='Search.TEntry', textvariable=self.query, width=4, font=(FONT,12))
        self.search_entry.pack(fill='x')
        glyph = ttk.Label(search, image=icon('search', 16, MUTED, self.page), style='Card.TLabel')
        glyph.place(x=self.px(10), rely=.5, anchor='w')
        prompt = ttk.Label(search, text='搜索设备名称、位置或 IP', style='DeviceMuted.TLabel')
        def placeholder(*_):
            if self.query.get() or self.page.focus_get() == self.search_entry:
                prompt.place_forget()
            else:
                prompt.place(x=self.px(34), rely=.5, anchor='w')
        for hint in (glyph, prompt):
            hint.bind('<Button-1>', lambda e: self.search_entry.focus_set())
        for event in ('<FocusIn>', '<FocusOut>'):
            self.search_entry.bind(event, placeholder)
        self.query.trace_add('write', placeholder)
        placeholder()
        self.search_entry.bind('<Escape>', lambda _: self.query.set(''))
        sync = ttk.Button(title, text=' 同步打印机', width=0, compound='left',
                          image=icon('refresh', 18, INK, self.page), command=app.sync_devices)
        sync.grid(row=0, column=2, sticky='e', padx=(self.px(16), 0))
        app.buttons.append(sync)
        def layout_search(event):
            compact = event.width < list_title.winfo_reqwidth() + sync.winfo_reqwidth() + self.px(260)
            if compact != getattr(self, '_compact_search', None):
                self._compact_search = compact
                search.grid_configure(row=int(compact), column=0 if compact else 1,
                                      columnspan=3 if compact else 1, pady=(self.px(8), 0) if compact else 0)
        title.bind('<Configure>', layout_search)
        table = tk.Frame(card, background=BORDER, padx=self.px(1), pady=self.px(1))
        table.pack(fill='x', pady=(self.px(15), 0))
        table.columnconfigure(0, weight=1)
        columns = [('label', '设备名称'), ('link', '连接方式'),
                   ('queue', 'Windows 队列'), ('enabled', '本机领取'),
                   ('pending', '待补报'), ('actions', '操作')]
        tree = app.devices = ClientTable(table, columns=[item[0] for item in columns], paint_cell=self._paint_cell,
                                         show='headings', height=2, selectmode='browse', style='Devices.Treeview')
        for key, label in columns:
            width = self.heading_font.measure(label) + self.cell_padding * 2
            tree.heading(key, text=label, anchor='w' if key != 'actions' else 'center')
            tree.column(key, width=width, minwidth=width, stretch=False,
                        anchor='w' if key != 'pending' else 'center')
        tree.grid(row=0, column=0, sticky='nsew')
        ybar = AutoScrollbar(table, orient='vertical', command=tree.yview)
        ybar.grid(row=0, column=1, sticky='ns')
        xbar = AutoScrollbar(table, orient='horizontal', command=tree.xview)
        xbar.grid(row=1, column=0, sticky='ew')
        tree.configure(xscrollcommand=lambda a, b: self._scrolled(xbar, a, b),
                       yscrollcommand=lambda a, b: self._scrolled(ybar, a, b))
        tree.bind('<Configure>', lambda _: (self._fit_columns(), self._queue_action_layout()), add='+')
        tree.bind('<Map>', lambda _: self._queue_action_layout(), add='+')
        tree.bind('<<TreeviewSelect>>', self._selected, add='+')
        self.empty = ttk.Label(card, style='DeviceMuted.TLabel', text='尚未登记打印机，点击「新增打印机」开始。')
        self.empty.pack(anchor='w', pady=(9, 0))

    def _paint_cell(self, tree, canvas, key, column, value, x, y, width, height, selected):
        lines = value.split('\n')
        left, middle = x + self.cell_padding, y + height // 2
        if column == 'label':
            glyph = icon('printer', 23, '#677489', self.page)
            canvas.create_image(left, middle, image=glyph, anchor='w')
            text_x = left + glyph.width() + self.px(10)
            split = self.name_font.metrics('linespace') // 2 if len(lines) > 1 else 0
            canvas.create_text(text_x, middle-split, anchor='w', text=lines[0], font=self.name_font, fill=INK)
            if len(lines) > 1:
                canvas.create_text(text_x, middle+split, anchor='w', text=lines[1], font=self.meta_font, fill=MUTED)
        elif column == 'link':
            glyph_name = {'USB 直连': 'usb', '局域网': 'network'}.get(lines[0])
            if glyph_name is None:
                canvas.create_text(left, middle, text=lines[0], anchor='w', font=self.meta_font, fill=MUTED)
                return True
            glyph = icon(glyph_name, 15, '#374961', self.page)
            top = middle - self.px(12 if len(lines) == 1 else 22)
            right = left + glyph.width() + self.meta_font.measure(lines[0]) + self.px(21)
            bottom = top + self.px(25)
            rad = self.px(5)
            canvas.create_polygon(left+rad, top, right-rad, top, right, top, right, top+rad,
                right, bottom-rad, right, bottom, right-rad, bottom, left+rad, bottom, left, bottom,
                left, bottom-rad, left, top+rad, left, top, smooth=True,
                fill='#f8fbff', outline='#aab8cd', width=self.px(1))
            canvas.create_image(left+self.px(6), (top+bottom)//2, anchor='w', image=glyph)
            canvas.create_text(left+self.px(13)+glyph.width(), (top+bottom)//2, anchor='w',
                               text=lines[0], font=self.meta_font, fill=INK)
            if len(lines)>1:
                canvas.create_text(left, bottom+self.px(2), anchor='nw', text=lines[1], font=self.meta_font, fill=MUTED)
        elif column == 'enabled':
            r = self.px(5)
            canvas.create_oval(left, middle-r, left+2*r, middle+r, outline='',
                               fill='#17b877' if lines[0]=='允许' else '#eca51b')
            canvas.create_text(left+2*r+self.px(8), middle, anchor='w', text=lines[0], font=self.body_font, fill=INK)
        elif column == 'pending':
            canvas.create_text(x+width//2, middle, text=value, font=self.body_font, fill=INK)
        elif column != 'actions':
            return False
        return True

    def _editor(self):
        app = self.app
        self.editor = ttk.Frame(self.body, style='Card.TFrame', padding=self.px(18))
        head = ttk.Frame(self.editor, style='Surface.TFrame')
        head.pack(fill='x')
        self.editor_title = ttk.Label(head, text='设备设置', style='DeviceHeading.TLabel')
        self.editor_title.pack(side='left')
        self.toggle = app._button(head, '暂停领取', self._toggle, side='right')
        self.toggle.configure(width=0, compound='left')
        form = ttk.Frame(self.editor, style='Surface.TFrame')
        form.pack(fill='x', pady=(self.px(14), 0))
        form.columnconfigure(0, weight=1, uniform='device')
        form.columnconfigure(1, weight=1, uniform='device')
        app.device_entries = []
        for col, (key, name) in enumerate([('printerName', '设备名称'), ('location', '安装位置')]):
            ttk.Label(form, text=name, style='DeviceField.TLabel').grid(row=0, column=col, sticky='w')
            entry = ttk.Entry(form, width=10, textvariable=app.device_vars[key], font=(FONT,12))
            entry.grid(row=1, column=col, sticky='ew', pady=(self.px(5), self.px(16)),
                       padx=(0, self.px(20) if col == 0 else 0))
            app.device_entries.append(entry)
        ttk.Label(form, text='Windows 队列', style='DeviceField.TLabel').grid(row=2, column=0, sticky='w')
        queue_row = ttk.Frame(form, style='Surface.TFrame')
        queue_row.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(4, 0))
        queue_row.columnconfigure(0, weight=1, uniform='queue')
        queue_row.columnconfigure(1, weight=1, uniform='queue')
        app.device_queue = ttk.Combobox(queue_row, state='readonly', width=8, font=(FONT,12),
                                        textvariable=app.device_vars['queueName'])
        app.device_queue.grid(row=0, column=0, sticky='ew', padx=(0,self.px(16)))
        app.discovery_button = ttk.Button(queue_row, text='按 IP 添加队列', width=0, compound='left',
            command=app.open_network_discovery, image=icon('swap', 16, INK, self.page))
        app.discovery_button.grid(row=0, column=1, sticky='w')
        app.buttons.append(app.discovery_button)
        def fit_queue(event):
            compact = event.width < max(app.discovery_button.winfo_reqwidth(), app.device_queue.winfo_reqwidth()) * 2 + self.px(24)
            app.device_queue.grid_configure(columnspan=2 if compact else 1, padx=(0, 0 if compact else self.px(16)))
            app.discovery_button.grid_configure(row=1 if compact else 0, column=0 if compact else 1,
                columnspan=2 if compact else 1, pady=(self.px(6),0) if compact else 0)
        queue_row.bind('<Configure>', fit_queue)
        actions = app.device_actions = ttk.Frame(self.editor, style='Surface.TFrame')
        actions.pack(fill='x', pady=(self.px(56), 0))
        app.save_device_button = app._button(actions, '保存新增打印机',
            lambda: app.save_device(bool(app.devices.selection())), side='left')
        app.save_device_button.configure(style='DeviceSave.TButton', width=0)
        app._button(actions, '取消', self._cancel, side='left', padx=(self.px(12), 0)).configure(width=0)
        # Keep keyboard deletion and the existing selection guard on the same command as row deletion.
        app.delete_device_button = ttk.Button(self.editor, text='删除打印机', command=app.delete_device, state='disabled')
        app.devices.bind('<Delete>', lambda e: app.delete_device_button.invoke())
        self.tools = tk.Menu(self.editor, tearoff=False)
        self.tools.add_command(label='设备状态', state='disabled')
        self.tools.add_separator()
        self.tools.add_command(label='刷新可用队列', command=app.refresh_printers)
        self.tools.add_command(label='检查服务队列', command=app.service_check)
        self.tools.add_command(label='Windows 打印机设置', command=app.open_windows_printers)
        def queue_tools(event):
            if not app.busy:
                widget = event.widget
                self.tools.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
            return 'break'
        for widget in (app.device_queue, app.discovery_button):
            widget.bind('<Button-3>', queue_tools)
            widget.bind('<Shift-F10>', queue_tools)
        self.tools_link = ttk.Label(self.editor, text='队列工具 ▾', foreground=BLUE, style='Card.TLabel',
                                    cursor='hand2', takefocus=True)
        for event in ('<Button-1>', '<Return>', '<space>'):
            self.tools_link.bind(event, queue_tools)

    def show_editor(self, scroll=False):
        editing = self.app.editing_printer_id is not None
        self.editor_title.configure(text='编辑打印机' if editing else '登记打印机')
        self.editor.pack(fill='x', pady=(self.px(18), 0))
        self.update_controls()
        if scroll:
            self.app.device_scroll.update_idletasks()
            self.app.device_scroll.yview_moveto(1)

    def _cancel(self):
        self.app.new_device()
        self.editor.pack_forget()
        self.app.notice.set('已取消编辑。')

    def _selected(self, _event):
        if not self.app.busy and self.app.devices.selection() != (self.app.editing_printer_id,):
            self.app.select_device()

    def _choose(self, key, delete):
        app = self.app
        if app.busy or not app.devices.exists(key):
            return
        if app.devices.selection() != (key,):
            app.devices.selection_set(key)
        app.devices.focus(key)
        if app.editing_printer_id != key:
            app.select_device()
        if delete:
            app.delete_device()
        else:
            self.show_editor(scroll=True)
            app.device_entries[0].focus_set()

    def _toggle(self):
        row = self._selected_row()
        if row:
            self.app.toggle_device(not row.get('acceptNewJobs'))

    def _selected_row(self):
        selection = self.app.devices.selection()
        if selection:
            return next((r for r in self.app.saved.get('printers', []) if str(r['printerId']) == selection[0]), None)

    def update_controls(self):
        row = self._selected_row()
        busy = self.app.busy
        enabled = bool(row and row.get('acceptNewJobs'))
        self.toggle.configure(text=' 暂停领取' if enabled else ' 恢复领取',
                              image=icon('pause' if enabled else 'play', 16, INK, self.page),
                              state='normal' if row and not busy else 'disabled')
        self.search_entry.configure(state='disabled' if busy else 'normal')
        self.app.delete_device_button.configure(state='normal' if row and not busy and not row.get('acceptNewJobs') else 'disabled')
        selection = self.app.devices.selection()
        for key, (edit, delete, allowed) in self.row_actions.items():
            prefix = 'Active' if key in selection else ''
            edit.configure(state='disabled' if busy else 'normal', style=prefix+'DeviceEdit.TButton')
            delete.configure(state='disabled' if busy or allowed else 'normal', style=prefix+'DeviceDelete.TButton')
        runtime = self.app.last_local.get('runtime', {}).get('printers', [])
        state = next((item.get('state') for item in runtime if row and
                      str(item.get('printerId')) == str(row['printerId'])), None)
        if self.app.service.get('state') != 'RUNNING':
            state = 'STOPPED'
        self.tools.entryconfigure(0, label='设备状态 · ' + RUNTIME_STATES.get(state, '待检查'))
        needs_tools = row and (row['queueName'] not in self.app.service_queues or state in ('ATTENTION','FAILED'))
        if needs_tools:
            self.tools_link.pack(before=self.app.device_actions, anchor='w', pady=(self.px(6),0))
        else:
            self.tools_link.pack_forget()

    def _scrolled(self, bar, first, last):
        bar.set(first, last)
        self._queue_action_layout()

    def _queue_action_layout(self):
        if self.action_layout_ticket is None and not self.app.closed:
            self.action_layout_ticket = self.app.root.after_idle(self._place_actions)
            self.app.timers.add(self.action_layout_ticket)

    def _place_actions(self):
        self.app.timers.discard(self.action_layout_ticket)
        self.action_layout_ticket = None
        tree = self.app.devices
        for key, (edit, delete, _) in self.row_actions.items():
            box = tree.bbox(key, 'actions')
            if box and box[0] >= 0 and box[0] + box[2] <= tree.winfo_width():
                x, y, width, height = box
                for index, button in enumerate((edit, delete)):
                    half = width // 2
                    inset = max(4, self.cell_padding // 3)
                    button.place(x=x + index * half, y=y + inset, width=half, height=height - inset * 2)
            else:
                edit.place_forget()
                delete.place_forget()

    def _fit_columns(self):
        """Size in measured font pixels so headings and both text lines fit at any DPI."""
        tree = self.app.devices
        widths = []
        for key in tree.cget('columns'):
            content = [self.heading_font.measure(tree.heading(key, 'text'))]
            for row in tree.get_children():
                lines = tree.set(row, key).splitlines()
                if key == 'label' and lines:
                    content.append(self.px(33) + max(self.name_font.measure(lines[0]),
                                                      self.meta_font.measure(lines[-1])))
                elif key == 'link' and lines:
                    content.append(self.meta_font.measure(lines[0]) + self.px(38))
                    if len(lines)>1:
                        content.append(self.meta_font.measure(lines[1]))
                elif key == 'enabled' and lines:
                    content.append(self.body_font.measure(lines[0])+self.px(19))
                content.extend(self.body_font.measure(line) for line in lines)
            if key == 'actions':
                content.extend(edit.winfo_reqwidth() + delete.winfo_reqwidth()
                               for edit, delete, _ in self.row_actions.values())
            widths.append(max(content) + self.cell_padding * 2)
        viewport = max(0, tree.winfo_width() - self.px(4))
        spare = viewport - sum(widths)
        actual = list(widths)
        if spare > 0:
            shares = (.215, .175, .2, .135, .1, .175)
            needs = [max(0, viewport * share - minimum) for share, minimum in zip(shares, widths)]
            total = sum(needs)
            actual = [minimum + int(spare * need / total) for minimum, need in zip(widths, needs)]
            actual[-1] += viewport - sum(actual)
        if (widths, actual) != self.column_widths:
            self.column_widths = widths, actual
            for key, minimum, width in zip(tree.cget('columns'), widths, actual):
                tree.column(key, minwidth=minimum, width=width)

    def reveal(self, printer_id):
        if not self.app.devices.exists(str(printer_id)) and self.query.get():
            self.query.set('')

    def render(self, rows):
        app = self.app
        tree = app.devices
        local = {str(r['printerId']): r for r in app.last_local.get('printers', [])}
        term = self.query.get().strip().casefold()
        shown = set()
        for row in rows:
            key, queue = str(row['printerId']), row['queueName']
            name = row.get('printerName') or queue
            location = row.get('location') or ''
            info = app.queue_display_info.get(queue.casefold(), {})
            target = info.get('port_target') or {}
            address = str(target.get('address') or target.get('host') or '')
            if term and not any(term in value.casefold() for value in (name, location, queue, address, str(target.get('host') or ''))):
                continue
            shown.add(key)
            enabled = bool(row.get('acceptNewJobs'))
            connection = queue_connection_type(info)
            pending = local.get(key, {})
            values = (name + ('\n' + location if location else ''), connection + ('\n' + address if address else ''),
                      queue, '允许' if enabled else '已暂停',
                      pending.get('pendingReports', 0) + pending.get('pendingObservations', 0), '')
            if tree.exists(key):
                tree.item(key, values=values)
                edit, delete, _ = self.row_actions[key]
            else:
                tree.insert('', 'end', iid=key, values=values)
                edit = ttk.Button(tree, text='编辑', width=3, style='DeviceEdit.TButton',
                                  command=lambda k=key: self._choose(k, False))
                delete = ttk.Button(tree, text='删除', width=3, style='DeviceDelete.TButton',
                                    command=lambda k=key: self._choose(k, True))
            self.row_actions[key] = edit, delete, enabled
            tree.move(key, '', 'end')
        for old in set(tree.get_children()) - shown:
            edit, delete, _ = self.row_actions.pop(old)
            edit.destroy()
            delete.destroy()
            tree.delete(old)
        if app.editing_printer_id is not None and app.editing_printer_id not in shown:
            app.new_device()
            self.editor.pack_forget()
            if term:
                app.notice.set('请从搜索结果中选择要管理的打印机。')
        tree.configure(height=max(2, min(len(shown), 5)))
        self._fit_columns()
        if shown:
            self.empty.pack_forget()
        else:
            hint = '未找到匹配的打印机。' if term else '尚未登记打印机，点击「新增打印机」开始。'
            self.empty.configure(text=hint)
            self.empty.pack(anchor='w', pady=(9, 0))
        self.update_controls()
        self._queue_action_layout()
