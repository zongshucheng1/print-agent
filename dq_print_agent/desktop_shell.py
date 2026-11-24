"""Navigation and service overview matching the approved client designs."""
import tkinter as tk
from tkinter import ttk

from . import __version__
from .desktop_theme import icon, printer_badge

NAVY = "#0d1c35"
BLUE = "#1556ed"
BG = "#eff5ff"
INK = "#111827"
MUTED = "#556176"
FONT = "Microsoft YaHei UI"
PAGES = {
    1: ("运行状态", "本机打印服务与任务记录。", "activity"),
    3: ("打印机", "管理 USB 直连和同一局域网内的打印机", "printer"),
    0: ("连接配置", "配置客户端访问业务后台的地址与凭据。", "settings"),
    2: ("客户端日志", "查看本机打印服务的最近运行记录。", "log"),
}


def build_shell(app):
    app.ui_scale = app.root.winfo_fpixels("1i") / 96
    px = lambda value: round(value * app.ui_scale)
    rail = tk.Frame(app.root, bg=NAVY, width=px(230))
    rail.pack(side="left", fill="y")
    rail.pack_propagate(False)
    brand = tk.Frame(rail, bg=NAVY)
    brand.pack(fill="x", padx=px(20), pady=(px(24), px(38)))
    ttk.Label(brand, image=printer_badge(app.root), background=NAVY).pack(side="left")
    brand_label = tk.Label(brand, text="德勤打印客户端", bg=NAVY, fg="white", font=(FONT, 14, "bold"))
    brand_label.pack(side="left", padx=px(9))
    content = ttk.Frame(app.root, padding=(px(26), px(35), px(26), px(8)))
    content.pack(side="left", fill="both", expand=True)
    header = ttk.Frame(content)
    header.pack(fill="x", pady=(0, px(26)))
    text = ttk.Frame(header)
    text.pack(side="left", fill="x", expand=True)
    breadcrumb = ttk.Label(text, text="打印机  /  新增打印机", font=(FONT, 11))
    title = ttk.Label(text, style="Title.TLabel")
    title.pack(anchor="w")
    subtitle = ttk.Label(text, style="Hint.TLabel", font=(FONT, 12))
    subtitle.pack(anchor="w", pady=(px(5), 0))
    app.header_actions = ttk.Frame(header)
    app.header_actions.pack(side="right", padx=(px(12), 0))
    app.header_buttons = {}
    notice = ttk.Label(content, textvariable=app.notice, style="Hint.TLabel", font=(FONT, 9))
    notice.pack(side="bottom", fill="x", pady=(px(8), 0))
    content.bind("<Configure>", lambda e: notice.configure(wraplength=max(160, e.width-px(52))))
    tabs = ttk.Notebook(content, style="Pages.TNotebook", takefocus=False)
    tabs.pack(fill="both", expand=True)
    buttons = {}
    for index, (name, _, glyph) in PAGES.items():
        button = ttk.Button(rail, text="  " + name, image=icon(glyph, 22, "white", app.root),
                            compound="left", style="Sidebar.TButton",
                            command=lambda index=index: tabs.select(index))
        button.pack(fill="x", padx=px(10), pady=px(6))
        buttons[index] = button
    version = tk.Label(rail, text=f"Windows  ·  v{__version__}", bg=NAVY, fg="#bdc8d7", font=(FONT, 10), anchor="w")
    version.pack(side="bottom", fill="x", padx=px(28), pady=(px(8), px(25)))
    guide = ttk.Button(rail, text="  使用说明", image=icon("book", 23, "white", app.root), compound="left",
                       command=app.open_guide, style="Sidebar.TButton")
    guide.pack(side="bottom", fill="x", padx=px(10))

    def navigate(_event=None):
        index = tabs.index("current")
        adding = index == 3 and getattr(app, "add_printer_panel", None) is not None
        if adding:
            target = app.add_printer_panel.queue_target
            heading = ("更换网络队列" if target else "配置网络队列") if target is not None else "新增打印机"
            title.configure(text=heading)
            breadcrumb.configure(text="打印机  /  " + heading)
            subtitle.pack_forget()
            breadcrumb.pack(before=title, anchor="w", pady=(0, px(12)))
        else:
            title.configure(text=PAGES[index][0])
            breadcrumb.pack_forget()
            subtitle.configure(text=PAGES[index][1])
            subtitle.pack(anchor="w", pady=(px(5), 0))
        for page, button in buttons.items():
            button.configure(style="SidebarSelected.TButton" if index == page else "Sidebar.TButton")
        for page, button in app.header_buttons.items():
            button.pack_forget()
            if index == page and not adding and not (page == 0 and app.switching):
                button.pack(side="right")
    def resize(_event=None):
        compact = app.root.winfo_width() < px(980)
        if compact == getattr(app, "_compact_shell", None):
            return
        app._compact_shell = compact
        rail.configure(width=px(64 if compact else 230))
        brand.pack_configure(padx=px(9 if compact else 20), pady=(px(24), px(38)))
        brand_label.pack_forget()
        version.pack_forget()
        if not compact:
            brand_label.pack(side="left", padx=px(9))
            version.pack(side="bottom", before=guide, fill="x", padx=px(28), pady=(px(8), px(25)))
        for index, button in buttons.items():
            button.configure(text="" if compact else "  " + PAGES[index][0])
        guide.configure(text="" if compact else "  使用说明")
        content.configure(padding=(px(12 if compact else 26), px(16 if compact else 35), px(12 if compact else 26), px(8)))
        app.header_actions.pack_forget()
        text.pack_forget()
        text.pack(side="top" if compact else "left", fill="x", expand=True)
        app.header_actions.pack(side="top" if compact else "right", anchor="e", pady=(px(8) if compact else 0, 0))
        subtitle.configure(wraplength=max(px(100), app.root.winfo_width()-rail.winfo_reqwidth()-px(60)))
    app.root.bind("<Configure>", lambda e: resize() if e.widget is app.root else None, add="+")
    tabs.bind("<<NotebookTabChanged>>", navigate)
    app.refresh_page_header = navigate
    return tabs


def build_service_overview(app, parent):
    px = lambda value: round(value * app.ui_scale)
    panel = ttk.Frame(parent, style="Service.TFrame", padding=px(22))
    panel.pack(fill="x", pady=(0, px(20)))
    controls = ttk.Frame(panel, style="ServiceSurface.TFrame")
    panel.columnconfigure(2, weight=1)
    controls.grid(row=0, column=3, sticky="e", padx=(px(12), 0))
    app.start_button = app._button(controls, "启动打印服务", lambda: app.control("start"), primary=True, side="left")
    app.stop_button = app._button(controls, "停止服务", lambda: app.control("stop"), side="left")
    badge = ttk.Label(panel, image=printer_badge(app.root, 48), background="#e3efff")
    badge.grid(row=0, column=0, padx=(0, px(20)))
    divider = tk.Frame(panel, width=px(1), height=px(46), bg="#a8bad4")
    divider.grid(row=0, column=1, padx=(0, px(18)))
    detail = ttk.Frame(panel, style="ServiceSurface.TFrame")
    detail.grid(row=0, column=2, sticky="ew")
    line = ttk.Frame(detail, style="ServiceSurface.TFrame")
    line.pack(fill="x")
    service_title = ttk.Label(line, text="打印服务", font=(FONT, 16, "bold"), background="#e3efff")
    service_title.grid(row=0, column=0, sticky="w")
    status = ttk.Frame(line, style="ServiceSurface.TFrame")
    status.grid(row=0, column=1, sticky="w", padx=(px(16), 0))
    app.service_dot = tk.Label(status, text="●", font=(FONT, 12), bg="#e3efff", fg=MUTED)
    app.service_dot.pack(side="left", padx=(0, px(7)))
    app.service_badge = tk.Label(status, text="待配置", font=(FONT, 11), bg="#e3efff", fg=MUTED, padx=px(8))
    app.service_badge.pack(side="left")
    description = ttk.Label(detail, text="关闭窗口后，服务将继续运行。", font=(FONT, 11),
                            background="#e3efff", foreground=MUTED)
    description.pack(anchor="w", pady=(px(5), 0))
    def fit_status(_event=None):
        inline = line.winfo_width() >= service_title.winfo_reqwidth() + status.winfo_reqwidth() + px(16)
        status.grid_configure(row=0 if inline else 1, column=1 if inline else 0,
                              padx=(px(16) if inline else 0, 0), pady=(0 if inline else px(4), 0))
    line.bind("<Configure>", fit_status)
    status.bind("<Configure>", fit_status)
    detail.bind("<Configure>", lambda event: description.configure(wraplength=max(1, event.width)))
    def arrange_service(event):
        stacked = event.width < px(505)
        controls.grid_configure(row=1 if stacked else 0, column=0 if stacked else 3,
                                columnspan=3 if stacked else 1, sticky="e",
                                padx=(0 if stacked else px(12), 0), pady=(px(12) if stacked else 0, 0))
    panel.bind("<Configure>", arrange_service)
    cards = ttk.Frame(parent)
    cards.pack(fill="x", pady=(0, px(20)))
    app.metric_values = {}
    metrics = []
    for i, (key, label) in enumerate((("registered", "已登记打印机"), ("enabled", "允许领取"), ("pending", "待补报回执"))):
        card = ttk.Frame(cards, padding=(px(24), px(16)), style="Card.TFrame")
        cards.columnconfigure(i, weight=1, uniform="metrics")
        card.grid(row=0, column=i, padx=(0 if i == 0 else px(16), 0), sticky="ew")
        caption = ttk.Label(card, text=label, style="Card.TLabel", foreground=MUTED, font=(FONT, 12))
        caption.pack(anchor="w")
        metrics.append((card, caption))
        value = tk.StringVar(parent, "—")
        app.metric_values[key] = value
        ttk.Label(card, textvariable=value, style="Card.TLabel", font=(FONT, 23, "bold")).pack(anchor="w", pady=(px(8), 0))

    def arrange_metrics(event):
        label_width = max(caption.winfo_reqwidth() for _, caption in metrics)
        columns = 3 if event.width >= 3 * (label_width + px(48)) + px(32) else (
            2 if event.width >= 2 * (label_width + px(28)) + px(12) else 1)
        for i, (card, _) in enumerate(metrics):
            cards.columnconfigure(i, weight=int(i < columns), uniform="metrics" if i < columns else "")
            row, col = divmod(i, columns)
            card.configure(padding=(px(24 if columns == 3 else 14), px(16 if columns == 3 else 10)))
            card.grid_configure(row=row, column=col, columnspan=2 if columns == 2 and i == 2 else 1,
                                padx=(px(16 if columns == 3 else 12) if col else 0, 0),
                                pady=(px(10) if row else 0, 0))
    cards.bind("<Configure>", arrange_metrics)


def render_service_overview(app):
    state = app.service.get("state")
    labels = {"RUNNING": "运行中", "STOPPED": "已停止", "NOT_INSTALLED": "待配置",
              "START_PENDING": "正在启动", "STOP_PENDING": "正在停止", "PAUSED": "已暂停"}
    conflict = app.service.get("installed") and not app.service.get("owned")
    label = "服务归属异常" if conflict else labels.get(state, "待检查")
    healthy = state == "RUNNING" and not conflict
    app.service_dot.configure(fg="#08bd4c" if healthy else "#d49a18")
    app.service_badge.configure(text=label, fg="#158237" if healthy else "#79602a",
                                bg="#cdf5dc" if healthy else "#f9edcc")
    app.start_button.pack_forget()
    app.stop_button.pack_forget()
    active = state in {"RUNNING", "START_PENDING", "STOP_PENDING"}
    (app.stop_button if active else app.start_button).pack(side="right")

