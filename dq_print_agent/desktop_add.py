"""Choose a Windows queue before registering a printer on the device page."""
import re
import tkinter as tk
from tkinter import font as tkfont, ttk
from PIL import Image, ImageDraw, ImageTk

from .desktop_theme import BG, BLUE, BORDER, FONT, INK, MUTED, WHITE, icon
from .desktop_widgets import AutoScrollbar

from .desktop_discovery import DiscoveryDialog


def queue_connection_type(queue_info):
    port = str(queue_info.get('port') or '').strip()
    if (queue_info.get('port_target') or {}).get('kind') == 'standard-tcpip':
        return '局域网'
    if re.fullmatch(r'USB\d+:?', port, re.IGNORECASE):
        return 'USB 直连'
    return '待识别'


def open_add_printer(app, page, *, queue_target=None):
    if app.closed or app.busy:
        return
    if app.discovery_window and not app.discovery_window.closed:
        app.discovery_window.window.lift()
        return
    existing = getattr(app, 'add_printer_panel', None)
    if existing is not None:
        existing.frame.lift()
        return existing
    panel = AddPrinterPanel(app, page, queue_target=queue_target)
    app.add_printer_panel = panel
    app.refresh_page_header()
    return panel


class ConnectionTile(tk.Canvas):
    """Keyboard-operable radio tile with the two lines and icon in the design."""
    def __init__(self, parent, mode, title, caption, on_choose, dp):
        super().__init__(parent, width=1, height=dp(108), background=WHITE,
                         highlightthickness=0, borderwidth=0, takefocus=True, cursor='hand2')
        self.mode = mode
        self.title = title
        self.caption = caption
        self.selected = False
        self.choose = on_choose
        self.dp = dp
        self.title_font = tkfont.Font(root=self, family=FONT, size=15, weight='bold')
        self.caption_font = tkfont.Font(root=self, family=FONT, size=11)
        self.bind('<Configure>', self.paint)
        self.bind('<FocusIn>', self.paint)
        self.bind('<FocusOut>', self.paint)
        self.bind('<Button-1>', self.activate)
        self.bind('<space>', self.activate)
        self.bind('<Return>', self.activate)

    def activate(self, _event):
        self.focus_set()
        self.choose(self.mode)
        return 'break'

    def paint(self, _event=None):
        self.delete('all')
        w, h, dp = self.winfo_width(), self.winfo_height(), self.dp
        if w <= 1 or h <= 1:
            return
        outline = BLUE if self.selected else '#8292a9' if self.focus_get() is self else BORDER
        key = (w, h, self.selected, outline)
        if getattr(self, '_background_key', None) != key:
            img = Image.new('RGB', (w*3, h*3), WHITE)
            ImageDraw.Draw(img).rounded_rectangle((2, 2, w*3-3, h*3-3), radius=dp(8)*3,
                fill='#e9f1ff' if self.selected else WHITE, outline=outline,
                width=dp(2 if self.selected else 1)*3)
            self.background_image = ImageTk.PhotoImage(img.resize((w, h), Image.Resampling.LANCZOS), master=self)
            self._background_key = key
        self.create_image(0, 0, anchor='nw', image=self.background_image)
        center = h / 2
        # Keep the caption legible in narrow windows by giving text more space.
        tight = w < dp(360)
        glyph_x = dp(30 if tight else 46)
        divider = dp(57 if tight else 87)
        text_x = divider + dp(12 if tight else 18)
        trailing = w - dp(21 if tight else 31)
        text_width = max(dp(75), trailing - dp(18) - text_x)
        caption_rows = 2 if self.caption_font.measure(self.caption) > text_width else 1
        title_h = self.title_font.metrics('linespace')
        line_h = self.caption_font.metrics('linespace')
        y = center - (title_h + dp(3) + line_h*caption_rows)/2
        self.create_image(glyph_x, center, image=icon('usb' if self.mode == 'usb' else 'network',
                          32 if tight else 43, BLUE if self.selected else MUTED, self))
        self.create_line(divider, center-dp(22), divider, center+dp(22), fill='#a6b5c9')
        self.create_text(text_x, y, anchor='nw', text=self.title, fill=INK, font=self.title_font)
        self.create_text(text_x, y+title_h+dp(3), anchor='nw', text=self.caption, width=text_width,
                         fill=MUTED, font=self.caption_font)
        ring_y, radius = center-dp(8), dp(10)
        self.create_oval(trailing-radius, ring_y-radius, trailing+radius, ring_y+radius,
                         fill=BLUE if self.selected else WHITE,
                         outline=BLUE if self.selected else '#8793a6', width=dp(1.5))
        if self.selected:
            dot = dp(3.3)
            self.create_oval(trailing-dot, ring_y-dot, trailing+dot, ring_y+dot, fill=WHITE, outline='')
        if self.focus_get() is self:
            self.create_line(text_x, y+title_h-1, text_x+self.title_font.measure(self.title), y+title_h-1,
                             fill=BLUE, dash=(2, 2))


class AddPrinterPanel:
    def __init__(self, app, page, *, queue_target=None):
        self.app = app
        self.queue_target = queue_target
        self.existing_network_queue = None
        self.known_ipp = set()
        self.frame = ttk.Frame(page, padding=(0, 2))
        self.frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.frame.lift()
        self.mode = tk.StringVar(self.frame, 'usb' if queue_target is None else 'lan')
        self.show_all = tk.BooleanVar(self.frame)
        self.queue = tk.StringVar(self.frame)
        self.ip = tk.StringVar(self.frame)
        self.driver = tk.StringVar(self.frame)
        self.name = tk.StringVar(self.frame)
        self.hint = tk.StringVar(self.frame)
        self.drivers_loaded = False
        self.creating_queue = False
        self.discovery = None
        self._layout()
        self.ip.trace_add('write', self._target_changed)
        if queue_target is not None:
            current = app.service_queues.get(app.device_vars['queueName'].get(), {})
            self.driver.set(current.get('driver') or '')
        self.show_mode()
        self.bind_scroll(self.body)

    def _layout(self):
        factor = self.frame.winfo_fpixels('1i') / 96
        self.dp = dp = lambda value: max(1, round(value * factor))
        style = ttk.Style(self.frame)
        for name in ('Wizard.TButton', 'Wizard.Primary.TButton'):
            style.configure(name, font=(FONT, 13), padding=(dp(18), dp(9)))
        for name in ('Wizard.TEntry', 'Wizard.TCombobox'):
            style.configure(name, padding=(dp(11), dp(9)))
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.frame, bg=BG, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        bar = AutoScrollbar(self.frame, orient='vertical', command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(yscrollcommand=bar.set)
        self.body = ttk.Frame(self.canvas)
        window = self.canvas.create_window(0, 0, anchor='nw', window=self.body)
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.body.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))

        options = ttk.Frame(self.body, style='Card.TFrame', padding=(dp(19), dp(21)))
        if self.queue_target is None:
            options.pack(fill='x')
        ttk.Label(options, text='连接方式', style='CardTitle.TLabel').pack(anchor='w', pady=(0, dp(21)))
        chooser = ttk.Frame(options, style='Surface.TFrame')
        chooser.pack(fill='x')
        self.tiles = {
            'usb': ConnectionTile(chooser, 'usb', 'USB 直连', '选择本机已安装的 USB 打印队列', self.choose_mode, dp),
            'lan': ConnectionTile(chooser, 'lan', '局域网打印机', '通过 IP 地址或局域网搜索添加', self.choose_mode, dp),
        }
        chooser.columnconfigure((0, 1), weight=1, uniform='tiles')
        def place_tiles(event):
            # At high DPI or narrow widths, both full descriptions remain readable.
            stacked = event.width < dp(535)
            for n, tile in enumerate(self.tiles.values()):
                tile.grid(row=n if stacked else 0, column=0 if stacked else n,
                          columnspan=2 if stacked else 1, sticky='ew',
                          padx=(0, 0 if stacked else dp(8)) if n == 0 else (0 if stacked else dp(8), 0),
                          pady=(dp(6) if stacked and n else 0, 0))
        chooser.bind('<Configure>', place_tiles)
        for n, tile in enumerate(self.tiles.values()):
            tile.grid(row=0, column=n, sticky='ew')
            for key in ('<Left>', '<Right>', '<Up>', '<Down>'):
                other = self.tiles['lan' if tile.mode == 'usb' else 'usb']
                tile.bind(key, other.activate)

        self.box = ttk.Frame(self.body, style='Card.TFrame', padding=(dp(23), dp(16), dp(23), dp(24)))
        self.box.pack(fill='x', pady=(dp(15), 0))
        header = ttk.Frame(self.box, style='Surface.TFrame')
        header.pack(fill='x')
        self.heading = ttk.Label(header, style='CardTitle.TLabel')
        self.heading.pack(side='left')
        self.more = tk.Menubutton(header, text='···', font=(FONT, 13, 'bold'), fg=MUTED, bg=WHITE,
                                  activeforeground=BLUE, activebackground='#eaf2ff', bd=0, relief='flat',
                                  takefocus=True, cursor='hand2', padx=dp(8), pady=0)
        self.more.pack(side='right')
        self.menu = tk.Menu(self.more, tearoff=False, font=(FONT, 11))
        self.more.configure(menu=self.menu)
        self.more.bind('<Return>', lambda _: self.menu.tk_popup(
            self.more.winfo_rootx(), self.more.winfo_rooty()+self.more.winfo_height()))

        self.forms = ttk.Frame(self.box, style='Surface.TFrame')
        self.forms.pack(fill='x', pady=(dp(25), 0))
        self.queue_form = ttk.Frame(self.forms, style='Surface.TFrame')
        self.network_form = ttk.Frame(self.forms, style='Surface.TFrame')
        self._queue_fields()
        self._network_fields()

        note = ttk.Frame(self.box, style='Surface.TFrame')
        note.pack(fill='x', pady=(dp(19), 0))
        ttk.Label(note, image=icon('info', 22, MUTED, note), style='Card.TLabel').pack(side='left', anchor='n')
        message = ttk.Label(note, textvariable=self.hint, style='Card.TLabel', foreground=MUTED,
                            justify='left', font=(FONT, 12))
        message.pack(side='left', anchor='w', fill='x', expand=True, padx=(dp(9), 0))
        note.bind('<Configure>', lambda e: message.configure(wraplength=max(dp(100), e.width-dp(37))))
        self.windows_link = tk.Button(self.box, text='打开 Windows 打印机设置', command=self.app.open_windows_printers,
                                      font=(FONT, 12), fg=BLUE, bg=WHITE, activeforeground=BLUE,
                                      activebackground=WHITE, bd=0, highlightthickness=0, relief='flat',
                                      padx=0, pady=0, cursor='hand2')
        self.actions = ttk.Frame(self.body)
        self.actions.pack(fill='x', pady=(dp(30), dp(8)))
        self.next_button = ttk.Button(self.actions, text='下一步', width=10, style='Wizard.Primary.TButton', command=self.advance)
        if self.queue_target is not None:
            self.next_button.configure(text='使用此队列')
        self.next_button.pack(side='left')
        ttk.Button(self.actions, text='取消', width=8, style='Wizard.TButton',
                   command=self.cancel).pack(side='left', padx=(dp(15), 0))

    def _queue_fields(self):
        form, dp = self.queue_form, self.dp
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='Windows 队列', style='Card.TLabel', font=(FONT, 12, 'bold')).grid(row=0, column=0, sticky='w')
        form.columnconfigure(0, minsize=dp(173))
        self.queue_box = ttk.Combobox(form, textvariable=self.queue, state='readonly', width=12,
                                      font=(FONT, 13), style='Wizard.TCombobox')
        self.queue_box.grid(row=0, column=1, sticky='ew')
        self.queue_box.bind('<<ComboboxSelected>>', lambda _: self.show_details())
        ttk.Button(form, text=' 刷新队列', image=icon('refresh', 22, INK, form), compound='left',
                   style='Wizard.TButton', command=self.refresh_queues).grid(row=0, column=2, sticky='ew', padx=(dp(13), 0))
        meta = tk.Frame(form, bg='#edf1f6', padx=dp(14), pady=dp(10))
        meta.grid(row=1, column=1, sticky='ew', pady=(dp(9), 0))
        self.driver_text = tk.StringVar(form)
        self.port_text = tk.StringVar(form)
        for col, (label, variable) in enumerate((('驱动', self.driver_text), ('端口', self.port_text))):
            pos = 3 * col
            if col:
                tk.Frame(meta, bg='#acb9ca', width=1).grid(row=0, column=pos-1, sticky='ns', padx=dp(9))
            tk.Label(meta, text=label, bg='#edf1f6', fg=MUTED, font=(FONT, 10, 'bold')).grid(row=0, column=pos)
            tk.Label(meta, textvariable=variable, bg='#edf1f6', fg=INK, anchor='w', width=1,
                     font=(FONT, 11)).grid(row=0, column=pos+1, sticky='ew', padx=(dp(7), 0))
            meta.columnconfigure(pos+1, weight=3 if col == 0 else 2)
        def fit(event):
            # On a narrow viewport the metadata band can also use the action column.
            narrow = event.width < dp(570)
            form.columnconfigure(0, minsize=dp(110 if narrow else 173))
            form.columnconfigure(2, minsize=0 if narrow else dp(160))
            meta.grid_configure(column=0 if narrow else 1, columnspan=3 if narrow else 1)
        form.bind('<Configure>', fit)

    def _network_fields(self):
        form, dp = self.network_form, self.dp
        form.columnconfigure(1, weight=1)
        for row, title in enumerate(('打印机 IP', 'Windows 驱动', '队列名称（可选）')):
            label = ttk.Label(form, text=title, style='Card.TLabel', font=(FONT, 12, 'bold'))
            label.grid(row=row, column=0, sticky='w', padx=(0, dp(10)), pady=(0, dp(16) if row < 2 else 0))
        options = dict(width=12, font=(FONT, 13))
        self.driver_box = ttk.Combobox(form, textvariable=self.driver, state='readonly', style='Wizard.TCombobox', **options)
        self.name_entry = ttk.Entry(form, textvariable=self.name, style='Wizard.TEntry', **options)
        fields = (ttk.Entry(form, textvariable=self.ip, style='Wizard.TEntry', **options), self.driver_box, self.name_entry)
        for row, field in enumerate(fields):
            field.grid(row=row, column=1, sticky='ew', pady=(0, dp(16) if row < 2 else 0))
        search = ttk.Button(form, text=' 搜索局域网打印机', image=icon('search', 22, INK, form),
                            compound='left', style='Wizard.TButton', command=self.search)
        search.grid(row=0, column=2, sticky='ew', padx=(dp(12), 0), pady=(0, dp(16)))
        def fit(event):
            narrow = event.width < dp(540)
            form.columnconfigure(0, minsize=dp(110 if narrow else 173))
            form.columnconfigure(2, minsize=0 if narrow else dp(237))
            search.grid_configure(row=3 if narrow else 0, column=0 if narrow else 2, columnspan=2 if narrow else 1,
                                  padx=(0 if narrow else dp(12), 0),
                                  pady=(dp(8), 0) if narrow else (0, dp(16)), sticky='w' if narrow else 'ew')
        form.bind('<Configure>', fit)

    def choose_mode(self, mode):
        if not self.creating_queue and self.mode.get() != mode:
            self.mode.set(mode)
            self.show_mode()

    def bind_scroll(self, widget):
        if not isinstance(widget, ttk.Combobox):
            widget.bind('<MouseWheel>', self.scroll)
        widget.bind('<FocusIn>', self.reveal_focus, add='+')
        for child in widget.winfo_children():
            self.bind_scroll(child)

    def scroll(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-int(event.delta / 120), 'units')
        return 'break'

    def reveal_focus(self, event):
        widget = event.widget
        if not widget.winfo_ismapped() or self.canvas.yview() == (0.0, 1.0):
            return
        top = widget.winfo_rooty() - self.canvas.winfo_rooty()
        bottom = top + widget.winfo_height() - self.canvas.winfo_height()
        offset = min(top, 0) if top < 0 else max(bottom, 0)
        if offset:
            self.canvas.yview_moveto((self.canvas.canvasy(0) + offset) / max(1, self.body.winfo_height()))

    def show_mode(self, *, existing=False):
        self.queue_form.pack_forget()
        self.network_form.pack_forget()
        self.windows_link.pack_forget()
        self.menu.delete(0, 'end')
        self.use_queue = self.mode.get() == 'usb' or existing
        for mode, tile in self.tiles.items():
            tile.selected = self.mode.get() == mode
            tile.paint()
        if self.mode.get() == 'lan':
            self.menu.add_command(label='按 IP 配置队列' if existing else '选择已安装的网络队列',
                                  command=self.toggle_existing)
            if not existing:
                self.menu.add_command(label='刷新 Windows 驱动', command=self.load_drivers)
                self.menu.add_command(label='打开 Windows 打印机设置', command=self.app.open_windows_printers)
        if self.use_queue:
            self.menu.add_checkbutton(label='显示全部已安装队列', variable=self.show_all, command=self.populate_queues)
            self.windows_link.pack(anchor='w', pady=(self.dp(19), self.dp(15)))
        if self.use_queue:
            self.heading.configure(text='选择本机打印队列' if self.mode.get() == 'usb' else '选择已安装的网络队列')
            self.queue_form.pack(fill='x')
            self.show_all.set(False)
            self.populate_queues()
            if not self.app.service_queues and self.service_running() and not self.app.busy:
                self.refresh_queues()
        else:
            self.heading.configure(text='配置网络队列')
            self.network_form.pack(fill='x')
            self.hint.set('相同 IP、端口和驱动的已有队列会自动复用。')
            if not self.drivers_loaded:
                self.load_drivers()

    def toggle_existing(self):
        self.show_mode(existing=not self.use_queue)

    def service_running(self):
        return self.app.saved.get('configured') and self.app.service.get('state') == 'RUNNING'

    def populate_queues(self):
        connection = 'USB 直连' if self.mode.get() == 'usb' else '局域网'
        queues = [name for name, info in self.app.service_queues.items()
                  if self.show_all.get() or queue_connection_type(info) == connection]
        self.queue_box.configure(values=queues)
        if self.queue.get() not in queues:
            self.queue.set(queues[0] if len(queues) == 1 else '')
        self.show_details()
        if not self.service_running():
            self.hint.set('请先保存连接配置，并在“运行状态”启动当前打印服务，再刷新队列。')
        elif not queues:
            self.hint.set('未找到匹配的队列。请安装驱动后刷新，或在右上角菜单中选择“显示全部已安装队列”。')
        else:
            self.hint.set('找不到打印机？请检查 USB 连接，并在 Windows 中安装对应驱动后刷新。' if self.mode.get() == 'usb' else '请核对所选 Windows 队列的驱动和端口。')

    def show_details(self):
        info = self.app.service_queues.get(self.queue.get(), {})
        self.driver_text.set(info.get('driver') or '—')
        self.port_text.set(info.get('port') or '—')

    def refresh_queues(self):
        def done(rows):
            self.app._set_printers(rows)
            self.app.notice.set('服务账户可用的打印队列已刷新。')
            if self.frame.winfo_exists():
                if self.use_queue:
                    self.populate_queues()
                elif not self.drivers_loaded:
                    self.load_drivers()
        self.app._async(self.app.model.printers, done, '正在读取服务账户可用的 Windows 队列…')

    def load_drivers(self):
        def done(inventory):
            if not self.frame.winfo_exists():
                return
            drivers = inventory.get('drivers', [])
            self.driver_box.configure(values=drivers)
            if self.driver.get() not in drivers:
                self.driver.set('')
            self.drivers_loaded = True
            if not drivers and not self.use_queue:
                self.hint.set('请先安装与打印机型号匹配的 Windows 驱动，再刷新驱动。')
            self.app.notice.set('Windows 驱动列表已刷新。')
        self.app._async(self.app.model.network_inventory, done, '正在读取本机已安装的打印驱动…')

    def search(self):
        if self.app.busy:
            return
        active = self.app.discovery_window
        if active and not active.closed:
            active.window.lift()
            return
        def closed():
            for device in self.discovery.devices.values():
                if 9100 in device.get('ports', []):
                    self.known_ipp.discard(device['ip'])
                else:
                    self.known_ipp.add(device['ip'])
            self.app.discovery_window = None
            self.discovery = None
        self.discovery = DiscoveryDialog(self.app.root, self.app.model, dialogs=self.app.dialogs,
            initial_ip=self.ip.get().strip(), on_selected=self.select_network_target, on_close=closed)
        self.app.discovery_window = self.discovery

    def _target_changed(self, *_):
        if self.existing_network_queue and self.ip.get().strip() != self.existing_network_queue['ip']:
            self.existing_network_queue = None
            self.driver.set('')
            self.name.set('')
            self.driver_box.configure(state='readonly')
            self.name_entry.configure(state='normal')
            self.hint.set('相同 IP、端口和驱动的已有队列会自动复用。')

    def select_network_target(self, result):
        address = result['ip']
        changed = self.ip.get().strip() != address
        previous_queue = self.existing_network_queue
        self.ip.set(address)
        drivers = result['drivers']
        self.drivers_loaded = True
        self.driver_box.configure(values=drivers)
        existing = result['existingQueue']
        self.existing_network_queue = existing
        if existing:
            self.driver.set(existing['driver'])
            self.name.set(existing['name'])
            self.driver_box.configure(state='disabled')
            self.name_entry.configure(state='disabled')
            self.hint.set('已选中现有 Windows 队列，继续后将核对并复用该队列。')
        else:
            self.driver_box.configure(state='readonly')
            self.name_entry.configure(state='normal')
            if changed or previous_queue or self.driver.get() not in drivers:
                self.driver.set('')
            if changed or previous_queue:
                self.name.set('')
            self.hint.set('请确认或选择与这台打印机型号匹配的 Windows 驱动。')

    def advance(self):
        if self.app.busy:
            return
        if self.use_queue:
            name = self.queue.get()
            if not self.service_running() or name not in self.app.service_queues:
                self.hint.set('请启动当前打印服务，刷新并选择服务账户可访问的 Windows 队列。')
                return
            if self.queue_target is not None and self.app.devices.selection() != self.queue_target:
                self.hint.set('正在编辑的设备已变更，请取消后重新选择打印机。')
                return
            if self.queue_target is None:
                self.app.new_device()
            self.app.device_vars['queueName'].set(name)
            if self.queue_target is None:
                self.app.device_vars['printerName'].set(name)
            self.app.notice.set('请核对设备名称、安装位置和队列，再保存打印机。')
            self.close()
            self.app.device_page.show_editor(scroll=True)
            return
        ip = self.ip.get().strip()
        driver = self.driver.get()
        if not ip or not driver:
            self.hint.set('请填写打印机 IP 并选择已安装的对应驱动，也可以搜索局域网打印机。')
            return
        if ip in self.known_ipp:
            self.hint.set('此地址仅发现 IPP 端口，请通过 Windows 添加打印机。')
            return
        if driver not in self.driver_box.cget('values'):
            self.hint.set('所选驱动不在已安装列表中，请刷新 Windows 驱动。')
            return
        fields = {'ip': ip, 'port': 9100, 'driverName': driver,
                  'queueName': self.name.get().strip() or f'{driver} {ip}'}
        def submit():
            try:
                return self.app.model.add_network_queue(fields), None
            except Exception as error:
                return None, error
        def submitted(outcome):
            self.creating_queue = False
            result, error = outcome
            if error is not None:
                self.hint.set('网络队列添加未完成，请核对 IP 和驱动后重试。')
                raise error
            self.network_ready(result)
        self.creating_queue = True
        self.app._async(submit, submitted, '正在添加或复用 Windows 网络队列…')

    def network_ready(self, result):
        self.creating_queue = False
        self.close()
        if self.queue_target is None:
            self.app.new_device()
        self.app.device_page.show_editor(scroll=True)
        self.app._network_queue_ready(result, self.queue_target or ())

    def cancel(self):
        if not self.app.busy:
            self.close()

    def close(self):
        self.app.add_printer_panel = None
        self.frame.destroy()
        self.app.refresh_page_header()
