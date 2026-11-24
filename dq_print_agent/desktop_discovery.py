"""Read-only LAN printer picker for the add and edit workflows."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .desktop_model import DesktopError
from .desktop_table import ClientTable
from .desktop_theme import BLUE, BORDER, FONT, INK, MUTED, WHITE, icon
from .desktop_widgets import AutoScrollbar


_PICK_HINT = '选择设备后，返回上一页确认 Windows 驱动和队列名称。'
_IPP_HINT = '该设备仅提供 IPP 端口，请通过 Windows 添加打印机。'


class DiscoveryDialog:
    def __init__(self, parent, model, *, on_selected=None, on_close=None,
                 initial_ip='', dialogs=True, autoload=True):
        self.model = model
        self.on_selected = on_selected
        self.on_close = on_close
        self.dialogs = dialogs
        self.closed = False
        self.generation = 0
        self.active_generation = None
        self.active_kind = None
        self.worker = None
        self.cancel_event = None
        self.results = queue.Queue()
        self.timer = None
        self.interfaces = []
        self.devices = {}
        self.searched = False
        self.mode = 'subnet'
        self.shade = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('搜索局域网打印机')
        self.window.configure(bg=WHITE)
        self.window.transient(parent)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda _: self.close())
        self.selected_interface = tk.StringVar(self.window)
        self.network = tk.StringVar(self.window)
        self.direct_ip = tk.StringVar(self.window, initial_ip)
        self.status = tk.StringVar(self.window)
        self.count_text = tk.StringVar(self.window)
        self.detail = tk.StringVar(self.window, _PICK_HINT)
        self.selected_text = tk.StringVar(self.window, '尚未选择设备')
        self._layout()
        for query in (self.direct_ip, self.network):
            query.trace_add('write', self._query_changed)
        self._position(parent)
        self._schedule()
        if autoload:
            self.refresh_inventory()

    @staticmethod
    def _work_area(parent):
        try:
            import win32api
            import pywintypes
        except ImportError:
            pass
        else:
            try:
                monitor = win32api.MonitorFromWindow(parent.winfo_id(), 2)
                return win32api.GetMonitorInfo(monitor)['Work']
            except pywintypes.error:
                pass
        x, y = parent.winfo_vrootx(), parent.winfo_vrooty()
        return x, y, x + parent.winfo_screenwidth(), y + parent.winfo_screenheight()

    def _position(self, parent):
        self.window.update_idletasks()
        dp = self.dp
        left, top, right, bottom = self._work_area(parent)
        width = min(dp(1000), right - left - dp(12))
        visible = parent.winfo_viewable()
        if visible:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            width = min(width, max(dp(610), pw - dp(28)))
            self.shade = tk.Toplevel(parent, background='#08162c', takefocus=False)
            self.shade.withdraw()
            self.shade.overrideredirect(True)
            self.shade.transient(parent)
            self.shade.attributes('-alpha', 0.30)
            self.shade.geometry(f'{pw}x{ph}+{px}+{py}')
            self.shade.deiconify()
        self._fit_controls(width - dp(52))
        self.window.update_idletasks()
        natural_h = self.body.winfo_reqheight() + self.footer.winfo_reqheight() + dp(44)
        height = min(max(dp(535), natural_h), bottom - top - dp(12))
        if visible:
            x, y = px + (pw-width)//2, py + (ph-height)//2
        else:
            x, y = (left+right-width)//2, (top+bottom-height)//2
        x = min(max(left, x), right - width)
        y = min(max(top, y), bottom - height)
        self.window.overrideredirect(True)
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        self.window.deiconify()
        self.window.lift()
        if visible:
            self.window.grab_set()
            self.interface_box.focus_set()

    def _layout(self):
        scale = self.window.winfo_fpixels('1i') / 96
        self.dp = dp = lambda value: max(1, round(scale * value))
        style = ttk.Style(self.window)
        style.layout('Discovery.Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure('Discovery.Treeview', rowheight=dp(42), font=(FONT, 12),
                        background=WHITE, fieldbackground=WHITE, borderwidth=0)
        style.configure('Discovery.Treeview.Heading', font=(FONT, 11, 'bold'), background='#eaf0f7',
                        padding=(dp(6), dp(9)), foreground=INK, relief='flat')
        shell = ttk.Frame(self.window, style='Card.TFrame', padding=(dp(25), dp(16)))
        shell.pack(fill='both', expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(shell, bg=WHITE, highlightthickness=0, bd=0, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar = AutoScrollbar(shell, orient='vertical', command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = body = ttk.Frame(self.canvas, style='Surface.TFrame')
        self.content_id = self.canvas.create_window(0, 0, window=body, anchor='nw')
        self.canvas.bind('<Configure>', self._fit_content)
        body.bind('<Configure>', self._fit_content)
        self.window.bind('<MouseWheel>', self._scroll_page)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        def surface():
            return ttk.Frame(body, style='Surface.TFrame')
        header = surface()
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text='  搜索局域网打印机', image=icon('network', 35, BLUE, header),
                  compound='left', style='CardTitle.TLabel', font=(FONT, 18, 'bold')).pack(side='left')
        tk.Button(header, text='×', font=(FONT, 17), fg=INK, bg=WHITE, relief='flat',
                  bd=0, padx=dp(7), pady=0, cursor='hand2', command=self.close).pack(side='right')
        ttk.Label(body, text='选择本机网卡，查找同一网段内的打印机。', style='Card.TLabel',
                  foreground=MUTED).grid(row=1, column=0, sticky='w', pady=(dp(3), dp(10)))

        tabs = surface()
        tabs.grid(row=2, column=0, sticky='ew')
        self.tab_buttons = {}
        self.tab_lines = {}
        for col, (mode, title) in enumerate((('subnet', '网段搜索'), ('ip', '按 IP 检查'))):
            tab = tk.Button(tabs, text=title, font=(FONT, 12, 'bold'), fg=INK, bg=WHITE,
                            activebackground='#edf3ff', relief='flat', bd=0, padx=dp(16),
                            pady=dp(4), cursor='hand2', command=lambda m=mode: self.set_mode(m))
            tab.grid(row=0, column=col)
            self.tab_buttons[mode] = tab
            line = tk.Frame(tabs, height=dp(3))
            line.grid(row=1, column=col, sticky='ew')
            self.tab_lines[mode] = line
        tk.Frame(tabs, bg=BORDER, height=1).grid(row=1, column=2, sticky='sew')
        tabs.columnconfigure(2, weight=1)

        controls = surface()
        controls.grid(row=3, column=0, sticky='ew', pady=dp(14))
        controls.columnconfigure(1, weight=2)
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text='本地网卡', style='Card.TLabel').grid(row=0, column=0, sticky='w', padx=(0, dp(7)))
        self.interface_box = ttk.Combobox(controls, textvariable=self.selected_interface, state='readonly', width=18)
        self.interface_box.grid(row=0, column=1, sticky='ew')
        self.interface_box.bind('<<ComboboxSelected>>', self.interface_changed)
        self.query_label = ttk.Label(controls, style='Card.TLabel')
        self.query_label.grid(row=0, column=2, padx=dp(8))
        self.query_entry = ttk.Entry(controls, width=14)
        self.query_entry.grid(row=0, column=3, sticky='ew')
        self.query_entry.bind('<Return>', lambda _: self.search())
        self.refresh_button = ttk.Button(controls, text=' 刷新网卡', image=icon('refresh', 16, INK, controls),
                                        compound='left', command=self.refresh_inventory)
        self.refresh_button.grid(row=0, column=4, padx=(dp(8), 0))
        self.search_button = ttk.Button(controls, compound='left', command=self.search_or_cancel)
        self.search_button.grid(row=0, column=5, padx=(dp(8), 0))
        controls.bind('<Configure>', lambda event: self._fit_controls(event.width))

        caption = surface()
        caption.grid(row=4, column=0, sticky='ew', pady=(0, dp(6)))
        ttk.Label(caption, text='搜索结果', style='CardTitle.TLabel').pack(side='left')
        ttk.Label(caption, textvariable=self.count_text, style='Card.TLabel', foreground=MUTED).pack(side='right')
        listing = tk.Frame(body, bg=WHITE, highlightthickness=1, highlightbackground=BORDER)
        listing.grid(row=5, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.table = ClientTable(listing, columns=('select','ip','ports','device','mac','queues'),
                                 style='Discovery.Treeview', show='headings', height=3, selectmode='browse',
                                 paint_cell=self._paint_radio)
        for key, title, width in (('select', '', 34), ('ip', 'IP 地址', 133), ('ports', '端口 / 协议', 112),
                                  ('device', '设备信息', 165), ('mac', 'MAC 地址', 155), ('queues', '已安装队列', 147)):
            self.table.heading(key, text=title, anchor='w')
            self.table.column(key, width=dp(width), minwidth=dp(width if key != 'device' else 110),
                              stretch=key in ('device', 'queues'), anchor='w')
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical = AutoScrollbar(listing, orient='vertical', command=self.table.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = AutoScrollbar(listing, orient='horizontal', command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.bind('<<TreeviewSelect>>', self.select_device, add='+')
        self.empty = ttk.Label(listing, style='Card.TLabel', foreground=MUTED)
        self.empty.place(relx=.5, rely=.62, anchor='center')

        info = surface()
        info.grid(row=6, column=0, sticky='ew', pady=dp(7))
        self.progress = ttk.Progressbar(info, mode='indeterminate', length=dp(80))
        self.message = ttk.Label(info, textvariable=self.detail, style='Card.TLabel', foreground=MUTED,
                                 font=(FONT, 10), justify='left')
        self.message.pack(side='left', fill='x', expand=True)
        info.bind('<Configure>', lambda e: self.message.configure(wraplength=max(dp(150), e.width-dp(88))))
        ttk.Separator(shell).grid(row=1, column=0, columnspan=2, sticky='ew')
        self.footer = footer = ttk.Frame(shell, style='Surface.TFrame')
        footer.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(dp(10), 0))
        footer.columnconfigure(0, weight=1)
        selection = ttk.Label(footer, textvariable=self.selected_text, style='Card.TLabel')
        selection.grid(row=0, column=0, sticky='w')
        self.use_button = ttk.Button(footer, text='使用所选设备', style='Primary.TButton', command=self.use_selected)
        self.use_button.grid(row=0, column=2)
        cancel = ttk.Button(footer, text='取消', command=self.close)
        cancel.grid(row=0, column=1, padx=dp(9))
        def fit_footer(event):
            row = 1 if event.width < dp(470) else 0
            self.use_button.grid_configure(row=row)
            cancel.grid_configure(row=row)
            selection.grid_configure(columnspan=3 if row else 1)
        footer.bind('<Configure>', fit_footer)
        self.window.bind('<FocusIn>', self._reveal_focus)
        self.set_mode('subnet')

    def _fit_content(self, _event=None):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        total = max(h, self.body.winfo_reqheight())
        self.canvas.itemconfigure(self.content_id, width=w, height=total)
        self.canvas.configure(scrollregion=(0, 0, w, total))

    def _scroll_page(self, event):
        if self.canvas.yview() == (0.0, 1.0) or isinstance(event.widget, ttk.Combobox):
            return
        if str(event.widget).startswith(str(self.table)) and self.table.yview() != (0.0, 1.0):
            return
        self.canvas.yview_scroll(-int(event.delta/120), 'units')

    def _reveal_focus(self, event):
        if not str(event.widget).startswith(str(self.body)) or self.canvas.yview() == (0.0, 1.0):
            return
        y = event.widget.winfo_rooty() - self.canvas.winfo_rooty()
        delta = min(0, y) if y < 0 else max(0, y + event.widget.winfo_height() - self.canvas.winfo_height())
        if delta:
            self.canvas.yview_moveto((self.canvas.canvasy(0) + delta) / max(1, self.body.winfo_height()))

    def _fit_controls(self, width):
        narrow = width < self.dp(805)
        if getattr(self, '_compact', None) == narrow:
            return
        self._compact = narrow
        dp = self.dp
        self.query_label.grid_configure(row=1 if narrow else 0, column=0 if narrow else 2,
                                       sticky='w', padx=(0, dp(7)) if narrow else dp(8))
        self.query_entry.grid_configure(row=1 if narrow else 0, column=1 if narrow else 3,
                                        pady=(dp(5), 0) if narrow else 0)
        self.refresh_button.grid_configure(column=2 if narrow else 4)
        self.search_button.grid_configure(row=1 if narrow else 0, column=2 if narrow else 5,
                                         pady=(dp(5), 0) if narrow else 0)
        self.query_entry.master.columnconfigure(3, weight=0 if narrow else 1)

    def _paint_radio(self, tree, canvas, key, column, value, x, y, width, height, selected):
        if column != 'select':
            return False
        cx, cy, radius = x+width/2, y+height/2, self.dp(7)
        canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, width=self.dp(1.5),
                           fill=BLUE if selected else WHITE, outline=BLUE if selected else MUTED)
        if selected:
            dot = self.dp(2)
            canvas.create_oval(cx-dot, cy-dot, cx+dot, cy+dot, fill=WHITE, outline='')
        return True

    def set_mode(self, mode):
        if self.active_kind:
            return
        self.mode = mode
        for name, tab in self.tab_buttons.items():
            tab.configure(fg=BLUE if name == mode else MUTED)
            self.tab_lines[name].configure(bg=BLUE if name == mode else BORDER)
        direct = mode == 'ip'
        self.query_label.configure(text='打印机 IP' if direct else '搜索网段')
        self.query_entry.configure(textvariable=self.direct_ip if direct else self.network)
        self._clear_devices()

    def _query_changed(self, *_):
        if not self.active_kind:
            self._clear_devices()

    def _schedule(self):
        self.timer = self.window.after(70, self._drain)

    def _submit(self, kind, work):
        if self.closed or self.active_kind:
            return
        self.generation += 1
        generation, output = self.generation, self.results
        self.active_kind, self.active_generation = kind, generation
        def run():
            try:
                value, error = work(), None
            except Exception as caught:
                value = None
                error = str(caught) if isinstance(caught, DesktopError) else '操作未完成，请刷新后重试或检查本机权限。'
            output.put((generation, kind, value, error))
        self.worker = threading.Thread(target=run, name='printer-discovery-ui', daemon=True)
        self.worker.start()
        self.progress.pack(side='right')
        self.progress.start(15)
        self._button_states()

    def _drain(self):
        self.timer = None
        if self.closed:
            return
        while True:
            try:
                generation, kind, value, error = self.results.get_nowait()
            except queue.Empty:
                break
            if generation != self.active_generation:
                continue
            self.active_kind = self.active_generation = None
            self.progress.stop()
            self.progress.pack_forget()
            if generation != self.generation:
                self.status.set('搜索已取消。')
            elif error:
                self.status.set(error)
                if self.dialogs:
                    messagebox.showerror('操作未完成', error, parent=self.window)
            elif kind == 'inventory':
                self._inventory_loaded(value)
            else:
                self._scan_loaded(value)
            self._button_states()
        self._schedule()

    def refresh_inventory(self):
        if self.active_kind:
            return
        self._clear_devices()
        self.status.set('正在读取本机网卡…')
        self._submit('inventory', self.model.network_inventory)

    @staticmethod
    def _interface_label(item):
        return '{} · {}'.format(item.get('name', '网卡'), item['address'])

    def _inventory_loaded(self, value):
        self.interfaces = list(value.get('interfaces', []))
        labels = [self._interface_label(item) for item in self.interfaces]
        self.interface_box.configure(values=labels)
        if self.selected_interface.get() not in labels:
            self.selected_interface.set(labels[0] if labels else '')
            self.interface_changed()
        self.status.set('' if labels else '未找到可用的局域网 IPv4 网卡，请先连接网络。')
        self._button_states()

    def _interface(self):
        return next((item for item in self.interfaces if self._interface_label(item) == self.selected_interface.get()), None)

    def interface_changed(self, _event=None):
        item = self._interface()
        self.network.set(item.get('scanNetwork', item.get('network', '')) if item else '')
        self._clear_devices()

    def _clear_devices(self):
        self.searched = False
        self.status.set('')
        self.devices.clear()
        self.table.delete(*self.table.get_children())
        self._button_states()

    def search_or_cancel(self):
        if self.active_kind == 'scan':
            self.cancel_scan()
        else:
            self.search()

    def search(self):
        if self.closed or self.active_kind:
            return
        interface = self._interface()
        direct = self.mode == 'ip'
        target = self.direct_ip.get().strip() if direct else None
        if not interface or (direct and not target):
            self.status.set('请先选择本地网卡。' if not interface else '请填写需要检查的 IP 地址。')
            self._button_states()
            return
        model, network, interface = self.model, self.network.get().strip(), dict(interface)
        cancellation = threading.Event()
        self.cancel_event = cancellation
        self._clear_devices()
        self.status.set('正在检查指定 IP…' if direct else '正在搜索所选网段…')
        self._submit('scan', lambda: model.discover_network(interface, network, cancellation, ip=target))

    def cancel_scan(self):
        if self.active_kind != 'scan' or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.generation += 1
        self.status.set('正在取消搜索…')
        self._button_states()

    @staticmethod
    def _device_label(device):
        model = str(device.get('model') or '').strip()
        label = str(device.get('displayName') or '').strip()
        if model:
            return model
        return label + '（型号待确认）' if label and '型号待确认' not in label else label or '网络打印端口（型号待确认）'

    def _scan_loaded(self, value):
        self._clear_devices()
        self.searched = True
        self.scan_drivers = value.get('drivers', [])
        for index, device in enumerate(value.get('devices', [])):
            key = str(index)
            self.devices[key] = device
            ports = ', '.join({9100: '9100 / RAW', 631: '631 / IPP'}.get(p, str(p)) for p in device.get('ports', []))
            self.table.insert('', 'end', iid=key, values=('', device['ip'], ports, self._device_label(device),
                              device.get('mac') or '—', '、'.join(device.get('queueNames', [])) or '—'))
            if self.mode == 'ip' and device['ip'] == self.direct_ip.get().strip():
                self.table.selection_set(key)
        if value.get('timedOut') or value.get('cancelled'):
            prefix = '达到搜索时限' if value.get('timedOut') else '搜索已取消'
            self.status.set(f"{prefix}，检查 {value.get('scanned', 0)} / {value.get('total', 0)} 个地址，仅显示已检查结果。")
        self._button_states()

    def _selected_device(self):
        selected = self.table.selection()
        return self.devices.get(selected[0]) if selected else None

    def select_device(self, _event=None):
        self._button_states()

    def use_selected(self):
        device = self._selected_device()
        if self.closed or self.active_kind or not device or 9100 not in device.get('ports', []):
            return
        choice = {'ip': device['ip'], 'existingQueue': device.get('rawQueue'), 'drivers': self.scan_drivers}
        self.close()
        if self.on_selected:
            self.on_selected(choice)

    def _button_states(self):
        if self.closed:
            return
        busy = self.active_kind is not None
        scan = self.active_kind == 'scan'
        for tab in self.tab_buttons.values():
            tab.configure(state='disabled' if busy else 'normal')
        self.table.configure(selectmode='none' if busy else 'browse')
        self.interface_box.configure(state='disabled' if busy else 'readonly')
        self.query_entry.configure(state='disabled' if busy else 'normal')
        self.refresh_button.configure(state='disabled' if busy else 'normal')
        scanning_text = '取消搜索' if scan else '检查 IP' if self.mode == 'ip' else '重新搜索' if self.searched else '开始搜索'
        can_search = bool(self._interface() and not busy)
        can_cancel = scan and not self.cancel_event.is_set()
        self.search_button.configure(text=' '+scanning_text, image=icon('search', 16, INK, self.window),
                                     state='normal' if can_search or can_cancel else 'disabled')
        device = self._selected_device()
        supported = bool(device and 9100 in device.get('ports', []))
        self.use_button.configure(state='normal' if supported and not busy else 'disabled')
        self.selected_text.set('已选择 ' + device['ip'] if device else '尚未选择设备')
        self.count_text.set(f'发现 {len(self.devices)} 台设备' if self.searched else '')
        detail = self.status.get()
        if device and not supported:
            detail = (detail + ' ' + _IPP_HINT).strip()
        self.detail.set(detail or _PICK_HINT)
        if self.devices or busy:
            self.empty.place_forget()
        else:
            self.empty.configure(text='未发现设备，可检查网卡或按 IP 检查。' if self.searched else '请选择本地网卡并开始搜索。')
            self.empty.place(relx=.5, rely=.62, anchor='center')

    def close(self):
        if self.closed:
            return True
        self.closed = True
        self.generation += 1
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.progress.stop()
        self.window.destroy()
        if self.shade:
            self.shade.destroy()
        if self.on_close:
            self.on_close()
        return True


def smoke_discovery_ui(report_path=None):
    """Render the actual selection dialog with offline fixtures and exercise its handoff."""
    import json
    from pathlib import Path
    import sys
    from . import __version__
    from .desktop_theme import install_theme

    root = tk.Tk()
    root.withdraw()
    ttk.Style(root).theme_use('clam')
    install_theme(root)
    selections = []
    dialog = DiscoveryDialog(root, None, on_selected=selections.append, dialogs=False, autoload=False)
    dialog.window.transient('')
    try:
        root.update()
        interface = {'id': '7', 'name': 'WLAN 4', 'address': '192.168.137.1',
                     'prefixLength': 24, 'scanNetwork': '192.168.137.0/24'}
        driver = 'GoDEX GE540'
        existing = {'name': 'GE540-02', 'driver': driver, 'port': 9100, 'ip': '192.168.137.92'}
        dialog._inventory_loaded({'interfaces': [interface], 'drivers': [driver], 'queues': [existing]})
        assert dialog.network.get() == '192.168.137.0/24'
        first = {'ip': '192.168.137.86', 'ports': [9100], 'model': driver,
                 'mac': '02:00:00:00:00:86', 'queueNames': [], 'rawQueue': None}
        second = {'ip': existing['ip'], 'ports': [9100], 'model': driver, 'mac': '02:00:00:00:00:92',
                  'queueNames': [existing['name']], 'rawQueue': existing}
        dialog._scan_loaded({'devices': [first, second], 'drivers': [driver]})
        dialog.table.selection_set('0')
        dialog.select_device()
        dialog.window.deiconify()
        dialog.window.lift()
        root.update()
        assert dialog.table.get_children() == ('0', '1')
        assert dialog.selected_text.get() == '已选择 192.168.137.86'
        checks = ['discovery-window-created', 'bounded-default-subnet', 'candidates-rendered', 'selection-shown']
        if report_path:
            from PIL import ImageGrab
            import win32gui
            target = Path(report_path)
            handle = win32gui.GetAncestor(dialog.window.winfo_id(), 2)
            ImageGrab.grab(window=handle).save(target.with_suffix('.png'))
        dialog.set_mode('ip')
        assert not dialog.table.get_children() and 'disabled' in dialog.use_button.state()
        dialog.direct_ip.set(existing['ip'])
        dialog._scan_loaded({'devices': [second], 'drivers': [driver]})
        assert dialog.table.selection() == ('0',)
        dialog.use_button.invoke()
        assert dialog.closed and selections == [{'ip': existing['ip'], 'existingQueue': existing, 'drivers': [driver]}]
        checks.extend(['ip-check-tab', 'stale-selection-cleared', 'existing-queue-handoff', 'readonly-selection'])
        report = {'ok': True, 'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)),
                  'checks': checks, 'networkUsed': False, 'queuesChanged': False,
                  'serviceChanged': False, 'physicalPrinting': False}
        if report_path:
            report['screenshot'] = str(target.with_suffix('.png'))
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    finally:
        dialog.close()
        root.destroy()
