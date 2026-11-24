"""Connection settings for the Windows print client."""
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from .desktop_theme import BG, BLUE, FONT, INK, MUTED, WHITE, icon
from .desktop_widgets import AutoScrollbar


def _checkbox_style(parent, dp):
    style = ttk.Style(parent)
    name = f'Connection{dp(100)}.indicator'
    if name not in style.element_names():
        size = dp(20)
        pictures = []
        for checked, disabled in ((False, False), (True, False), (False, True), (True, True)):
            side = size * 4
            image = Image.new('RGBA', (side, side))
            draw = ImageDraw.Draw(image)
            accent = '#96ade2' if disabled else BLUE
            draw.rounded_rectangle((2, 2, side-3, side-3), radius=side//6,
                                   fill=accent if checked else WHITE,
                                   outline=accent if checked else '#abb4c4' if disabled else '#8492a9',
                                   width=max(3, side//17))
            if checked:
                draw.line([(side*.24, side*.52), (side*.43, side*.70), (side*.77, side*.32)],
                          fill=WHITE, width=max(5, side//10), joint='curve')
            pictures.append(ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=parent))
        parent._checkbox_images = pictures
        style.element_create(name, 'image', pictures[0], ('disabled', 'selected', pictures[3]),
                             ('disabled', pictures[2]), ('selected', pictures[1]))
    style.layout('Config.TCheckbutton', [
        (name, {'side': 'left', 'sticky': ''}),
        ('Checkbutton.padding', {'side': 'left', 'sticky': 'nswe', 'children': [
            ('Checkbutton.focus', {'sticky': 'nswe', 'children': [('Checkbutton.label', {'sticky': 'w'})]})]})])
    style.configure('Config.TCheckbutton', padding=(dp(10), 0, 0, 0), font=(FONT, 12),
                    background=WHITE, foreground=INK)
    style.configure('Task.Config.TCheckbutton', font=(FONT, 12, 'bold'))
    style.map('Config.TCheckbutton', background=[('active', WHITE)], foreground=[('disabled', '#8190a5')])


def build_connection(app, parent):
    dp = lambda value: max(1, round(value * app.ui_scale))
    _checkbox_style(parent, dp)
    viewport = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0)
    bar = AutoScrollbar(parent, orient='vertical', command=viewport.yview)
    bar.pack(side='right', fill='y')
    viewport.pack(fill='both', expand=True)
    viewport.configure(yscrollcommand=bar.set)
    page = ttk.Frame(viewport)
    window = viewport.create_window(0, 0, anchor='nw', window=page)
    viewport.bind('<Configure>', lambda e: viewport.itemconfigure(window, width=e.width))
    page.bind('<Configure>', lambda _: viewport.configure(scrollregion=viewport.bbox('all')))

    server = ttk.Frame(page, style='Card.TFrame', padding=(dp(21), dp(15), dp(21), dp(18)))
    server.pack(fill='x')
    ttk.Label(server, text='后台连接', style='CardTitle.TLabel').pack(anchor='w', pady=(0, dp(16)))
    form = ttk.Frame(server, style='Surface.TFrame')
    form.pack(fill='x')
    form.columnconfigure(0, minsize=dp(147))
    form.columnconfigure(1, weight=1)
    labels = []
    for row, text in enumerate(('服务器地址', '机器接入密钥')):
        label = ttk.Label(form, text=text, style='Card.TLabel', font=(FONT, 12, 'bold'))
        label.grid(row=row, column=0, sticky='w', padx=(0, dp(8)), pady=(0, dp(13) if row == 0 else 0))
        labels.append(label)
    app.api_entry = ttk.Entry(form, width=12, textvariable=app.vars['api_url'],
                              style='ReadonlyAddress.TEntry', font=(FONT, 13))
    app.api_entry.grid(row=0, column=1, sticky='ew', pady=(0, dp(13)))
    app.switch_button = ttk.Button(form, text='切换服务器', width=13, command=app.begin_switch)
    app.switch_button.grid(row=0, column=2, sticky='ew', padx=(dp(15), 0), pady=(0, dp(13)))
    app.buttons.append(app.switch_button)
    secret = ttk.Frame(form, style='Surface.TFrame')
    secret.grid(row=1, column=1, columnspan=2, sticky='ew')
    style = ttk.Style(parent)
    style.configure('Credential.TEntry', padding=(dp(11), dp(7), dp(42), dp(7)))
    app.token_entry = ttk.Entry(secret, width=12, textvariable=app.vars['token'], font=(FONT, 13),
                                style='Credential.TEntry', show='●')
    app.token_entry.pack(fill='x')
    narrow_form = False
    def fit_fields(event):
        nonlocal narrow_form
        narrow = event.width < dp(510)
        if narrow_form == narrow:
            return
        narrow_form = narrow
        form.columnconfigure(0, minsize=0 if narrow else dp(147))
        app.switch_button.grid_configure(row=0, column=1 if narrow else 2, sticky='e',
                                         pady=(0, dp(5) if narrow else dp(13)))
        labels[0].grid_configure(pady=(0, dp(5) if narrow else dp(13)))
        app.api_entry.grid_configure(row=1 if narrow else 0, column=0 if narrow else 1,
                                     columnspan=2 if narrow else 1)
        labels[1].grid_configure(row=2 if narrow else 1, pady=(0, dp(5) if narrow else 0))
        secret.grid_configure(row=3 if narrow else 1, column=0 if narrow else 1, columnspan=2)
    form.bind('<Configure>', fit_fields)
    token_hint = tk.Label(secret, bg=WHITE, fg=MUTED, font=(FONT, 12), padx=0, pady=0, anchor='w',
                          highlightthickness=0, bd=0, cursor='xterm')
    token_hint.bind('<Button-1>', lambda _: app.token_entry.focus_set())
    eye = tk.Button(secret, bg=WHITE, activebackground=WHITE, relief='flat', bd=0, cursor='hand2',
                    highlightthickness=0, padx=dp(2), pady=0, takefocus=True,
                    command=lambda: app.show_token.set(not app.show_token.get()))
    eye.place(relx=1, x=-dp(10), rely=.5, anchor='e')
    app.buttons.append(eye)

    def mask(*_):
        visible = app.show_token.get()
        app.token_entry.configure(show='' if visible else '●')
        eye.configure(image=icon('eye-off' if visible else 'eye', 20, INK, eye))
    app.show_token.trace_add('write', mask)
    mask()

    def secret_hint(*_):
        if not app.vars['token'].get() and app.root.focus_get() is not app.token_entry:
            saved = app.saved.get('configured') and not app.switching
            token_hint.configure(text='已保存，留空沿用原密钥' if saved else '请输入机器接入密钥')
            token_hint.place(x=dp(11), rely=.5, relwidth=1, width=-dp(49), anchor='w')
            eye.lift()
        else:
            token_hint.place_forget()
    app.vars['token'].trace_add('write', secret_hint)
    app.api_hint.trace_add('write', secret_hint)
    for event in ('<FocusIn>', '<FocusOut>'):
        app.token_entry.bind(event, secret_hint)
    secret_hint()
    credential_help = ttk.Label(server, text='密钥由后台「电脑打印」配发。更换前请先停止打印服务。',
                                style='Card.TLabel', foreground=MUTED, font=(FONT, 12))
    credential_help.pack(anchor='w', pady=(dp(8), 0))
    server.bind('<Configure>', lambda e: credential_help.configure(wraplength=max(dp(130), e.width-dp(46))))

    tasks = ttk.Frame(page, style='Card.TFrame', padding=(dp(21), dp(13), dp(21), dp(16)))
    tasks.pack(fill='x', pady=(dp(21), 0))
    ttk.Label(tasks, text='打印任务', style='CardTitle.TLabel').pack(anchor='w', pady=(0, dp(13)))
    app.print_check = ttk.Checkbutton(tasks, text='允许领取并打印任务', variable=app.allow_print,
                                      style='Task.Config.TCheckbutton')
    app.print_check.pack(anchor='w')
    paper_note = ttk.Label(tasks, text='标签纸 80 × 80 mm，每个任务打印一份。', style='Card.TLabel',
                           foreground=MUTED, font=(FONT, 12), justify='left')
    paper_note.pack(anchor='w', padx=(dp(31), 0), pady=(dp(3), 0))
    tasks.bind('<Configure>', lambda e: paper_note.configure(wraplength=max(dp(80), e.width-dp(77))))

    advanced = ttk.Frame(page, style='Card.TFrame', padding=(dp(21), dp(13), dp(21), dp(19)))
    advanced.pack(fill='x', pady=(dp(19), 0))
    heading = ttk.Frame(advanced, style='Surface.TFrame')
    heading.pack(fill='x')
    title = ttk.Label(heading, text='高级设置', style='CardTitle.TLabel', cursor='hand2')
    title.pack(side='left')
    fields = ttk.Frame(advanced, style='Surface.TFrame')
    fields.columnconfigure(0, minsize=dp(147))
    fields.columnconfigure(1, weight=1)
    opened = tk.BooleanVar(advanced, True)
    def expand():
        if opened.get():
            fields.pack(fill='x', pady=(dp(15), 0))
        else:
            fields.pack_forget()
        arrow.configure(image=icon('chevron-up' if opened.get() else 'chevron', 18, INK, arrow))
    def toggle(_event=None):
        opened.set(not opened.get())
        expand()
    arrow = tk.Button(heading, bg=WHITE, activebackground=WHITE, bd=0, relief='flat', takefocus=True,
                      padx=dp(5), pady=dp(3), highlightthickness=0, cursor='hand2', command=toggle)
    arrow.pack(side='right')
    title.bind('<Button-1>', toggle)
    ttk.Label(fields, text='标签文件来源', style='Card.TLabel', font=(FONT, 12, 'bold')).grid(
        row=0, column=0, sticky='nw', pady=dp(6))
    # Reuse the rounded input border while keeping the normal multiline Text API.
    border = style.layout('TEntry')[0][0]
    style.layout('OriginField.TFrame', [(border, {'sticky': 'nsew'})])
    origin_field = ttk.Frame(fields, style='OriginField.TFrame', padding=(dp(11), dp(7)))
    origin_field.grid(row=0, column=1, sticky='ew')
    app.origins = tk.Text(origin_field, height=1, width=12, wrap='char', bd=0, highlightthickness=0,
                          bg=WHITE, fg=INK, insertbackground=INK, relief='flat', font=(FONT, 12),
                          padx=0, pady=0)
    app.origins.pack(fill='x')
    origin_hint = tk.Label(origin_field, text='与后台同源时留空', bg=WHITE, fg=MUTED, font=(FONT, 12),
                           bd=0, padx=0, pady=0, cursor='xterm')
    origin_hint.bind('<Button-1>', lambda _: app.origins.focus_set())
    def update_origins(_event=None):
        if app.origins.edit_modified():
            app.origins.edit_modified(False)
        raw = app.origins.get('1.0', 'end-1c')
        app.origins.configure(height=min(5, 1 + raw.count('\n')))
        if not raw and app.root.focus_get() is not app.origins:
            origin_hint.place(in_=app.origins, x=0, rely=.5, anchor='w')
        else:
            origin_hint.place_forget()
    for event in ('<<Modified>>', '<FocusIn>', '<FocusOut>'):
        app.origins.bind(event, update_origins)
    update_origins()
    app.http_check = ttk.Checkbutton(fields, text='允许本机开发 HTTP（仅 127.0.0.1 / ::1）',
                                     variable=app.allow_http, style='Http.Config.TCheckbutton')
    app.http_check.grid(row=1, column=0, columnspan=2, sticky='w', pady=(dp(15), 0))
    fields.bind('<Configure>', lambda e: style.configure('Http.Config.TCheckbutton',
                wraplength=max(dp(80), e.width-dp(38))))
    expand()

    footer = ttk.Frame(page)
    footer.pack(fill='x', pady=(dp(35), dp(5)))
    slots = [ttk.Frame(footer) for _ in range(3)]
    app.save_button = app._button(slots[0], '保存配置', app.save, primary=True, side='left')
    app.save_button.configure(width=11, padding=(dp(15), dp(9)))
    check_button = app._button(slots[1], '检查连接', app.check_server, side='left')
    check_button.configure(width=10, padding=(dp(14), dp(9)))
    app.cancel_switch_button = app._button(slots[2], '取消切换', app.cancel_switch, side='right')
    app.cancel_switch_button.pack_forget()
    # Separate slots preserve the controller's pack/forget contract for cancel.
    # Rows use the measured button widths at the current system font and DPI.
    def fit_actions(event):
        widths = [button.winfo_reqwidth() for button in (app.save_button, check_button, app.cancel_switch_button)]
        gap = dp(15)
        pair_fits = widths[0] + gap + widths[1] <= event.width
        all_fit = pair_fits and sum(widths) + gap*2 <= event.width
        slots[0].grid(row=0, column=0, sticky='w')
        slots[1].grid(row=0 if pair_fits else 1, column=1 if pair_fits else 0, sticky='w',
                      padx=(gap if pair_fits else 0, 0), pady=(0 if pair_fits else dp(8), 0))
        slots[2].grid(row=0 if all_fit else 1 if pair_fits else 2, column=2 if all_fit else 0,
                      columnspan=1 if all_fit or not pair_fits else 2, sticky='e' if all_fit else 'w',
                      pady=(0 if all_fit else dp(8), 0))
        footer.columnconfigure(2, weight=1 if all_fit else 0)
    footer.bind('<Configure>', fit_actions)
    app.adopt_button = app._button(app.header_actions, ' 打开配置…', app.adopt, side='right')
    app.adopt_button.configure(image=icon('folder', 21, INK, parent), compound='left')
    app.header_buttons[0] = app.adopt_button

    def scroll(event):
        if app.tabs.select() == str(parent) and event.widget is not app.origins:
            viewport.yview_scroll(-int(event.delta / 120), 'units')
    app.root.bind('<MouseWheel>', scroll, add='+')

    def show_focus(event):
        widget = event.widget
        if not str(widget).startswith(str(page) + '.') or not widget.winfo_ismapped():
            return
        top = widget.winfo_rooty() - viewport.winfo_rooty()
        bottom = top + widget.winfo_height() - viewport.winfo_height()
        offset = min(top, 0) if top < 0 else max(bottom, 0)
        if offset:
            viewport.yview_moveto((viewport.canvasy(0) + offset) / max(1, page.winfo_height()))
    app.root.bind('<FocusIn>', show_focus, add='+')
