"""Present the already redacted tail of the Windows print-service log."""

import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont, ttk

from .desktop_table import ClientTable
from .desktop_theme import BG, BORDER, FONT, INK, MUTED, WHITE, icon
from .desktop_widgets import AutoScrollbar
from .log_records import latest_log_entries


_LEVEL_COLORS = {
    'INFO': ('#dce9ff', '#164db6'),
    'DEBUG': ('#e9edf3', '#495970'),
    'WARNING': ('#fff0c5', '#9c4e00'),
    'WARN': ('#fff0c5', '#9c4e00'),
    'ERROR': ('#ffe2e4', '#ac2937'),
    'CRITICAL': ('#ffe2e4', '#ac2937'),
    'FATAL': ('#ffe2e4', '#ac2937'),
}


def build_logs(app, parent):
    app.log_view = _LogView(app, parent)


def render_logs(app, text):
    app.log_view.render(text)


class _LogView:
    def __init__(self, app, page):
        self.app = app
        self.page = page
        self.raw_rows = {}
        self.last_text = None
        self.source = None
        self.waiting_for_load = False
        self.detail = None
        self.dp = lambda value: round(value * app.ui_scale)
        dp = self.dp
        actions = ttk.Frame(app.header_actions)
        app.header_buttons[2] = actions
        action_buttons = []
        for label, glyph, callback, primary in (
            ('刷新日志', 'refresh', app.refresh_log, True),
            ('打开数据目录', 'folder', app.open_directory, False),
        ):
            button = app._button(actions, '  ' + label, callback, primary=primary, side='left',
                                 padx=(0 if primary else dp(12), 0))
            image = icon(glyph, 21, WHITE if primary else INK, page)
            button.configure(image=image, compound='left')
            if primary:
                button.configure(style='Header.Primary.TButton')
            action_buttons.append((button, label, image))
        preferred_width = sum(b.winfo_reqwidth() for b, _, _ in action_buttons) + dp(12)
        compact_actions = False
        def fit_actions(event):
            nonlocal compact_actions
            compact = event.width < preferred_width
            if compact != compact_actions:
                compact_actions = compact
                for button, label, image in action_buttons:
                    button.configure(image='' if compact else image, text=label if compact else '  ' + label,
                                     padding=(dp(6 if compact else 14), dp(7)))
        page.bind('<Configure>', fit_actions, add='+')

        self.viewport = tk.Canvas(page, bg=BG, bd=0, highlightthickness=0, yscrollincrement=dp(16))
        bar = AutoScrollbar(page, command=self.viewport.yview)
        bar.pack(side='right', fill='y')
        self.viewport.pack(fill='both', expand=True)
        self.viewport.configure(yscrollcommand=bar.set)
        body = ttk.Frame(self.viewport)
        window = self.viewport.create_window(0, 0, anchor='nw', window=body)
        body.bind('<Configure>', lambda _: self.viewport.configure(scrollregion=self.viewport.bbox(window)))
        card = ttk.Frame(body, style='Card.TFrame', padding=(dp(18), dp(19)))
        card.pack(fill='x', pady=(0, dp(8)))
        header = ttk.Frame(card, style='Surface.TFrame')
        header.pack(fill='x', pady=(0, dp(19)))
        heading = ttk.Label(header, text='  运行日志', image=icon('log', 27, INK, page),
                            compound='left', style='CardTitle.TLabel')
        filename = ttk.Label(header, text='agent.log', style='Card.TLabel', foreground=MUTED, font=('Consolas', 12))
        limit = ttk.Label(header, text='最近 100 行', style='Card.TLabel', foreground=MUTED)
        header.columnconfigure(2, weight=1)
        heading.grid(row=0, column=0, sticky='w')
        filename.grid(row=0, column=1, sticky='w', padx=dp(14))
        limit.grid(row=0, column=2, sticky='e')
        header_width = heading.winfo_reqwidth() + filename.winfo_reqwidth() + limit.winfo_reqwidth() + dp(28)
        def fit_heading(event):
            narrow = event.width < header_width
            heading.grid_configure(columnspan=3 if narrow else 1)
            filename.grid_configure(row=int(narrow), column=0 if narrow else 1, padx=0 if narrow else dp(14))
            limit.grid_configure(row=int(narrow))
        header.bind('<Configure>', fit_heading)

        footer = ttk.Frame(card, style='Surface.TFrame')
        footer.pack(side='bottom', fill='x', pady=(dp(16), 0))
        tk.Frame(footer, bg=BORDER, height=dp(1)).pack(fill='x')
        ttk.Label(footer, text='  含凭据的日志已隐藏。', style='Card.TLabel', foreground=MUTED,
                  font=(FONT, 11), image=icon('info', 19, MUTED, page), compound='left'
                  ).pack(side='left', pady=(dp(10), 0))
        self.body_font = tkfont.Font(page, family=FONT, size=12)
        self.day_font = tkfont.Font(page, family=FONT, size=12, weight='bold')
        self.badge_font = tkfont.Font(page, family=FONT, size=10)
        style = ttk.Style(page)
        style.layout('LogRows.Treeview', [('Treeview.treearea', {'sticky': 'nsew'})])
        normal_row = max(dp(49), self.body_font.metrics('linespace') + dp(15))
        style.configure('LogRows.Treeview', background=WHITE, fieldbackground=WHITE, borderwidth=0,
                        font=self.body_font, rowheight=normal_row)
        style.configure('LogRows.Treeview.Heading', background='#e7edf6', foreground=INK,
                        font=self.day_font, padding=(dp(12), dp(11)), relief='flat')
        dense = False
        def fit_viewport(event):
            nonlocal dense
            self.viewport.itemconfigure(window, width=event.width)
            short = event.height < 2 * normal_row + self.day_font.metrics('linespace') + dp(24)
            if short != dense:
                dense = short
                height = self.body_font.metrics('linespace') + dp(8) if short else normal_row
                style.configure('LogRows.Treeview', rowheight=height)
                style.configure('LogRows.Treeview.Heading', padding=(dp(12), dp(5 if short else 11)))
                self.tree.redraw()
        self.viewport.bind('<Configure>', fit_viewport)
        table = tk.Frame(card, bg=BORDER, borderwidth=0, padx=dp(1), pady=dp(1))
        table.pack(fill='both', expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ClientTable(table, columns=('time', 'level', 'message'), show='headings',
                                style='LogRows.Treeview', paint_cell=self._paint, height=2)
        for key, label, width, minimum in (
            ('time', '时间 ↓', 162, 112), ('level', '级别', 142, 84), ('message', '日志内容', 340, 135),
        ):
            self.tree.heading(key, text=label, anchor='w')
            self.tree.column(key, width=dp(width), minwidth=dp(minimum), stretch=key=='message')
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical = AutoScrollbar(table, command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = AutoScrollbar(table, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        def outer_wheel(event):
            region = str(event.widget)
            if region == str(self.viewport) or region.startswith(str(body) + '.') or region == str(body):
                if region != str(table) and not region.startswith(str(table) + '.'):
                    units = max(1, abs(event.delta)//120) * (2 if event.delta < 0 else -2)
                    self.viewport.yview_scroll(units, 'units')
                    return 'break'
        app.root.bind('<MouseWheel>', outer_wheel, add='+')
        self.tree.bind('<<TableActivate>>', self._show_detail)
        self.tree.bind('<Control-c>', self._copy)
        self.empty = ttk.Label(self.tree, text='暂无运行日志。', style='Card.TLabel', foreground=MUTED)
        self.empty.place(relx=.5, rely=.5, anchor='center')
        page.bind('<Map>', self._initial_read, add='+')

    def set_source(self, settings):
        config = Path(settings.get('config_path') or self.app.model.config_path)
        source = (config, Path(settings.get('state_dir') or config.parent),
                  (settings.get('api_url') or '').rstrip('/'))
        previous, self.source = self.source, source
        if previous is None or previous == source:
            return
        if self.detail is not None and self.detail.winfo_exists():
            self.detail.destroy()
        self.detail = None
        self.render('')
        self.last_text = None
        if self.app.autoload and self.page.winfo_ismapped():
            self.app._schedule(0, lambda: self._initial_read(None))

    def _initial_read(self, event):
        if event is not None:
            self.viewport.yview_moveto(0)
            self.tree.yview_moveto(0)
        if self.last_text is not None or not self.app.autoload or not self.page.winfo_ismapped():
            return
        if self.app.busy:
            if not self.waiting_for_load:
                def retry():
                    self.waiting_for_load = False
                    self._initial_read(None)
                self.waiting_for_load = True
                self.app._schedule(160, retry)
            return
        self.app.refresh_log()

    def render(self, text):
        text = text or ''
        self.viewport.yview_moveto(0)
        self.tree.yview_moveto(0)
        if self.last_text == text:
            return
        self.last_text = text
        self.raw_rows.clear()
        self.tree.delete(*self.tree.get_children())
        if not text or text == '尚无客户端运行日志。':
            self.tree.configure(height=2)
            self.empty.place(relx=.5, rely=.5, anchor='center')
            return
        self.empty.place_forget()
        current_day = None
        for date, time, level, message, raw in latest_log_entries(text):
            if date != current_day:
                self.tree.insert('', 'end', values=(date or '时间未知', '', ''), tags=('day',))
                current_day = date
            key = self.tree.insert('', 'end', values=(time, level, message))
            self.raw_rows[key] = raw
        self.tree.configure(height=max(2, min(len(self.tree.get_children()), 8)))

    def _paint(self, tree, canvas, key, column, value, x, y, width, height, selected):
        dp = self.dp
        left = x + dp(16)
        if key not in self.raw_rows:
            canvas.create_rectangle(x, y, x+width, y+height, fill='#edf2f9', outline='')
            if column == 'time':
                canvas.create_text(left, y+height//2, anchor='w', text=value, font=self.day_font, fill=INK)
            return True
        if column == 'level' and value:
            fill, ink = _LEVEL_COLORS.get(value, ('#e9edf3', MUTED))
            right = left + max(dp(62), self.badge_font.measure(value) + dp(21))
            badge_height = self.badge_font.metrics('linespace') + dp(5)
            top = y + (height - badge_height)//2
            bottom = top + badge_height
            radius = dp(4)
            canvas.create_polygon(left+radius, top, right-radius, top, right, top, right, top+radius,
                                  right, bottom-radius, right, bottom, right-radius, bottom,
                                  left+radius, bottom, left, bottom, left, bottom-radius,
                                  left, top+radius, left, top, smooth=True, fill=fill, outline='')
            canvas.create_text((left+right)/2, y+height//2, text=value, font=self.badge_font, fill=ink)
        elif value:
            canvas.create_text(left, y+height//2, anchor='w', fill=INK, font=self.body_font,
                               text=tree._fit_text(value, width-dp(22)))
        return True

    def _copy(self, _event):
        selection = [self.raw_rows[k] for k in self.tree.selection() if k in self.raw_rows]
        if selection:
            self.page.clipboard_clear()
            self.page.clipboard_append('\n'.join(selection))
        return 'break'

    def _show_detail(self, event):
        key = self.tree.identify_row(event.y)
        if key not in self.raw_rows:
            return
        if self.detail is None or not self.detail.winfo_exists():
            self.detail = tk.Toplevel(self.page)
            self.detail.title('日志内容')
            self.detail.transient(self.app.root)
            self.detail.geometry(f'{self.dp(620)}x{self.dp(260)}')
            self.detail_text = tk.Text(self.detail, wrap='word', font=(FONT, 12), foreground=INK,
                                       background=WHITE, borderwidth=0, padx=self.dp(16), pady=self.dp(16))
            scroll = AutoScrollbar(self.detail, command=self.detail_text.yview)
            scroll.pack(side='right', fill='y')
            self.detail_text.pack(fill='both', expand=True)
            self.detail_text.configure(yscrollcommand=scroll.set)
            self.detail.bind('<Escape>', lambda _: self.detail.destroy())
        self.detail_text.configure(state='normal')
        self.detail_text.delete('1.0', 'end')
        self.detail_text.insert('1.0', self.raw_rows[key])
        self.detail_text.configure(state='disabled')
        self.detail.lift()
