"""Windows desktop management surface; all printing stays in the durable service worker."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from .desktop_widgets import AutoScrollbar

from . import __version__
from .desktop_devices import DevicePage
from .desktop_model import DesktopError, DesktopModel
from .desktop_shell import BG, BLUE, INK, MUTED, build_service_overview, build_shell, render_service_overview
from .desktop_theme import install_theme, icon
from .desktop_table import ClientTable
from .desktop_connection import build_connection
from .desktop_logs import build_logs, render_logs
from .verification_display import observation_time, verification_text
from .windows_service import service_status

SERVICE_NAMES = {"NOT_INSTALLED": "尚未安装打印服务", "RUNNING": "打印服务运行中", "STOPPED": "打印服务已停止",
                 "START_PENDING": "正在启动", "STOP_PENDING": "正在停止", "PAUSED": "打印服务已暂停", "UNKNOWN": "服务状态暂不可用"}
STAGE_NAMES = {"PREPARED": "已领取", "INTENT": "待确认发送", "AUTHORIZED": "正在提交", "SUBMITTED": "已提交电脑队列",
               "UNKNOWN": "发送结果不明", "FAILED": "发送前失败", "COMPLETED": "已完成", "CANCELLED": "已取消", "RESOLVED": "服务端已处理"}


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve().parents[1] / "desktop_launcher.py")]


def elevated_service_action(action: str, config_path: Path) -> None:
    """One explicit UAC request for a service action; never place credentials in argv."""
    import win32api
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon
    command = launch_command() + ["service", "enable" if action == "start" else action, "--config", str(config_path.resolve())]
    handle = None
    try:
        response = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=command[0],
                                       lpParameters=subprocess.list2cmdline(command[1:]), nShow=win32con.SW_HIDE)
        handle = response["hProcess"]
        if win32event.WaitForSingleObject(handle, 120000) == win32event.WAIT_TIMEOUT:
            raise DesktopError("服务操作仍在进行，请刷新状态后核对，暂勿重复操作。")
        if win32process.GetExitCodeProcess(handle) != 0:
            raise DesktopError("服务操作未完成。请检查管理员授权、服务所属目录及客户端日志后刷新状态。")
    except DesktopError:
        raise
    except Exception as error:
        if getattr(error, "winerror", None) == 1223:
            raise DesktopError("已取消 Windows 管理员授权，服务未启动。") from None
        raise DesktopError("无法完成服务管理，请确认具有 Windows 管理员权限。") from None
    finally:
        if handle is not None:
            win32api.CloseHandle(handle)


class DesktopApp:
    def __init__(self, root: tk.Tk, model=None, *, status_reader=service_status, service_action=elevated_service_action,
                 dialogs=True, autoload=True):
        self.root = root
        self.model = model or DesktopModel()
        self.status_reader = status_reader
        self.service_action = service_action
        self.dialogs = dialogs
        self.autoload = autoload
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="printer-ui")
        self.results = queue.Queue()
        self.closed = False
        self.busy = False
        self.polling = False
        self.timers = set()
        self.generation = 0
        self.saved = {}
        self.discovery_window = None
        self.editing_printer_id = None
        self.service_queues = {}
        self.queue_display_info = {}
        self.switching = False
        self.service = {"installed": False, "owned": False, "state": "NOT_INSTALLED", "configPath": None}
        self.root.title("德勤打印客户端")
        display_scale = root.winfo_fpixels('1i') / 96
        width = min(round(1200 * display_scale), root.winfo_screenwidth() - 40)
        height = min(round(800 * display_scale), root.winfo_screenheight() - 80)
        self.root.geometry(f"{max(800, width)}x{max(680, height)}")
        self.root.minsize(800, 680)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            icon = Path(__file__).resolve().parents[1] / "assets" / "printer.ico"
            if icon.exists():
                root.iconbitmap(str(icon))
        except tk.TclError:
            pass
        self.vars = {name: tk.StringVar(root) for name in ("api_url", "token")}
        self.allow_http = tk.BooleanVar(root, False)
        self.allow_print = tk.BooleanVar(root, False)
        self.queue_filter = tk.StringVar(root, '全部打印机')
        self.device_vars = {name: tk.StringVar(root) for name in ('queueName', 'printerName', 'location')}
        self.last_local = {}
        self.show_token = tk.BooleanVar(root, False)
        self.notice = tk.StringVar(root, "先在后台登记打印电脑，然后填写服务器地址和接入密钥，启动服务后添加打印机。")
        self.status_text = tk.StringVar(root, "正在读取本机状态…")
        self.scope_text = tk.StringVar(root, "尚未保存连接配置")
        self.path_text = tk.StringVar(root, str(self.model.config_path))
        self.pending_text = tk.StringVar(root, "待补报回执：—")
        self.api_hint = tk.StringVar(root, "填写后端地址；例如 https://screen.deqin.info/prod-api。")
        self.buttons = []
        self._style()
        self._layout()
        self._schedule(80, self._drain)
        if autoload:
            self._async(self._initial_load, self._loaded, "正在读取配置与打印机…")
            self._schedule(2000, self._poll)

    def _schedule(self, delay, callback):
        def run():
            self.timers.discard(ticket)
            if not self.closed:
                callback()
        ticket = self.root.after(delay, run)
        self.timers.add(ticket)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background="white")
        style.configure("Hint.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground=INK)
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 8), background="white", foreground=INK,
                        bordercolor="#d5dfec", lightcolor="white", darkcolor="#d5dfec")
        style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", "#9cb4d9"), ("active", "#1955b7")])
        style.configure("TEntry", padding=7, font=("Microsoft YaHei UI", 10))
        style.configure("TCombobox", padding=7, font=("Microsoft YaHei UI", 10), fieldbackground="white", background="white")
        style.map("TCombobox", fieldbackground=[("readonly", "white"), ("disabled", "#edf1f7")])
        style.configure("TCheckbutton", background=BG, font=("Microsoft YaHei UI", 10))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.layout("Pages.TNotebook.Tab", [])
        style.configure("TNotebook.Tab", padding=(22, 11), font=("Microsoft YaHei UI", 10), background=BG, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Treeview", rowheight=max(34, round(self.root.winfo_fpixels("23p"))),
                        font=("Microsoft YaHei UI", 10), background="white", fieldbackground="white",
                        foreground=INK, borderwidth=0)
        style.map("Treeview", background=[("selected", "#dceaff")], foreground=[("selected", INK)])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"),
                        foreground=INK, background="#eaf0f7", relief="flat", padding=(4, 7))
        install_theme(self.root)
        style.layout("Tasks.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Tasks.Treeview", rowheight=round(self.root.winfo_fpixels("36p")))
        style.configure("Tasks.Treeview.Heading", padding=(8, 9), background="#eaf0f7",
                        font=("Microsoft YaHei UI", 11, "bold"), relief="flat")

    def _button(self, parent, text, command, *, primary=False, **pack):
        button = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")
        button.pack(**pack)
        self.buttons.append(button)
        return button

    def _layout(self):
        self.tabs = build_shell(self)
        connection_tab = ttk.Frame(self.tabs)
        running_tab = ttk.Frame(self.tabs)
        running_scroll = tk.Canvas(running_tab, background=BG, highlightthickness=0)
        running_bar = AutoScrollbar(running_tab, orient="vertical", command=running_scroll.yview)
        running_bar.pack(side="right", fill="y")
        running_scroll.pack(fill="both", expand=True)
        running_scroll.configure(yscrollcommand=running_bar.set)
        running = ttk.Frame(running_scroll)
        running_window = running_scroll.create_window(0, 0, window=running, anchor="nw")
        running.bind("<Configure>", lambda _: running_scroll.configure(scrollregion=running_scroll.bbox("all")))
        running_scroll.bind("<Configure>", lambda e: running_scroll.itemconfigure(running_window, width=e.width))
        def scroll_running(event):
            if self.tabs.select() == str(running_tab) and event.widget is not self.tasks:
                running_scroll.yview_scroll(-int(event.delta / 120), "units")
        self.root.bind("<MouseWheel>", scroll_running, add="+")
        logs = ttk.Frame(self.tabs)
        self.tabs.add(connection_tab, text="连接配置")
        self.tabs.add(running_tab, text="运行状态")
        self.tabs.add(logs, text="客户端日志")
        build_connection(self, connection_tab)
        build_service_overview(self, running)
        px = lambda value: round(value * self.ui_scale)
        self.header_buttons[1] = self._button(self.header_actions, " 刷新状态", self.refresh_status)
        self.header_buttons[1].configure(image=icon("refresh", 19, INK, self.root), compound="left")
        recent = ttk.Frame(running, style="Card.TFrame", padding=px(16))
        recent.pack(fill="both", expand=True)
        filters = ttk.Frame(recent, style="Surface.TFrame")
        filters.pack(fill="x", pady=(0, px(16)))
        ttk.Label(filters, text="最近任务", style="CardTitle.TLabel").pack(side="left")
        self.task_filter = ttk.Combobox(filters, textvariable=self.queue_filter, width=17, values=['全部打印机'], state='readonly')
        self.task_filter.pack(side="right")
        self.task_filter.bind('<<ComboboxSelected>>', lambda _: self._show_snapshot((self.service, self.last_local)))
        footer = ttk.Frame(recent, style="Surface.TFrame", padding=(0, px(20), 0, 0))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, image=icon("info", 17, MUTED, self.root), background="white").pack(side="left", padx=(0, px(8)))
        ttk.Label(footer, text="实际出纸请以现场确认为准。", style="Card.TLabel", foreground=MUTED,
                  font=("Microsoft YaHei UI", 11)).pack(side="left")
        task_frame = ttk.Frame(recent, style="Surface.TFrame")
        task_frame.pack(fill="both", expand=True)
        task_frame.rowconfigure(0, weight=1)
        task_frame.columnconfigure(0, weight=1)
        self.tasks = ClientTable(task_frame, columns=("job", "attempt", "stage", "spool", "verification", "observed", "queue"),
                                 show="headings", height=4, style="Tasks.Treeview")
        self.tasks.configure(displaycolumns=("job", "queue", "stage", "verification", "observed"))
        for column, title, width in (("job", "任务编号", 180), ("attempt", "尝试", 45), ("stage", "本地执行状态", 140),
                                     ("spool", "队列编号", 85), ("verification", "自动核验", 245), ("observed", "最近检查", 130), ('queue', '打印队列', 160)):
            self.tasks.heading(column, text=title)
            self.tasks.column(column, width=px(min(width, 165)), minwidth=px(min(width, 100)), anchor="w",
                              stretch=column in {"job", "queue", "verification"})
            self.tasks.heading(column, anchor="w")
        self.tasks.grid(row=0, column=0, sticky="nsew")
        task_y = AutoScrollbar(task_frame, orient="vertical", command=self.tasks.yview)
        task_y.grid(row=0, column=1, sticky="ns")
        task_x = AutoScrollbar(task_frame, orient="horizontal", command=self.tasks.xview)
        task_x.grid(row=1, column=0, sticky="ew")
        self.tasks.configure(yscrollcommand=task_y.set, xscrollcommand=task_x.set)
        self.tasks.bind("<<TableActivate>>", self._task_details)
        build_logs(self, logs)
        devices_tab = ttk.Frame(self.tabs)
        self.tabs.add(devices_tab, text='打印机')
        self.device_page = DevicePage(self, devices_tab)
        from .desktop_add import open_add_printer
        self.header_buttons[3] = self._button(self.header_actions, " 新增打印机",
            lambda: open_add_printer(self, devices_tab), primary=True)
        self.header_buttons[3].configure(image=icon("plus", 21, "white", self.root),
                                         compound="left", style="Header.Primary.TButton")
        self.refresh_page_header()

    def _fields(self):
        fields = {"api_url": self.vars["api_url"].get().strip(),
                "download_origins": [line.strip() for line in self.origins.get("1.0", "end").splitlines() if line.strip()],
                "allow_loopback_http": self.allow_http.get(), "physical_printing_enabled": self.allow_print.get()}
        if self.switching:
            fields["expected_current_api_url"] = self.saved.get("api_url")
            if self.saved.get("state_dir"):
                fields["expected_state_dir"] = self.saved["state_dir"]
        return fields

    def _task_details(self, _event=None):
        selection = self.tasks.selection()
        if selection and self.dialogs:
            values = self.tasks.item(selection[0], 'values')
            labels = ('任务编号', '尝试次数', '本地执行状态', '队列编号', '自动核验', '最近检查', '打印队列')
            messagebox.showinfo("任务详情", "\n".join(f"{label}  {value}" for label, value in zip(labels, values)),
                                parent=self.root)

    def _async(self, work, done, message):
        if self.busy or self.closed:
            return
        self.busy = True
        # A poll begun before this action cannot overwrite its resulting service state.
        self.generation += 1
        self.notice.set(message)
        self._button_states()
        generation = self.generation
        future = self.executor.submit(work)
        future.add_done_callback(lambda task: self.results.put(("action", generation, task, done)))

    def _drain(self):
        if self.closed:
            return
        while not self.results.empty():
            kind, generation, future, callback = self.results.get_nowait()
            if kind == "poll":
                self.polling = False
            else:
                self.busy = False
            if generation != self.generation:
                continue
            try:
                callback(future.result())
            except Exception as error:
                message = str(error) if isinstance(error, DesktopError) else "操作未完成，请检查配置和日志。错误类型：" + type(error).__name__
                self.notice.set(message)
                if kind != "poll" and self.dialogs:
                    messagebox.showerror("操作未完成", message, parent=self.root)
            self._button_states()
        self._schedule(80, self._drain)

    def _initial_load(self):
        current = self.status_reader()
        if current.get("owned") and current.get("configPath"):
            self.model = DesktopModel(Path(current["configPath"]))
        settings = self.model.load()
        try:
            printers = self.model.printers()
        except DesktopError:
            printers = []
            if settings.get('printers'):
                try:
                    printers = self.model.registered_queue_metadata(settings['printers'])
                except DesktopError:
                    pass  # The saved device list is still usable without spooler metadata.
        return settings, printers, current

    def _loaded(self, result):
        settings, printers, self.service = result
        self._apply_settings(settings)
        self._set_printers(printers)
        self._render_service()
        if settings.get("configured") and self.tabs.index("current") == 0 and self.root.focus_get() is not self.api_entry:
            self.tabs.select(1)
        if self.service.get("state") == "RUNNING":
            self.notice.set("打印服务正在运行。可在「打印机」中查看或登记设备。")
        elif settings.get("configured"):
            self.notice.set("已载入连接配置。请在「运行状态」启动打印服务。")
        else:
            self.notice.set("请先填写服务器地址和机器接入密钥，保存后启动打印服务。")

    def _apply_settings(self, settings):
        self.switching = False
        self.save_button.configure(text="保存配置")
        self.cancel_switch_button.pack_forget()
        self.refresh_page_header()
        self.saved = settings
        self.last_local = {}
        self.vars["api_url"].set(settings.get("api_url", ""))
        self.allow_http.set(settings.get("allow_loopback_http", False))
        self.allow_print.set(settings.get("physical_printing_enabled", False))
        self.origins.configure(state="normal")
        self.origins.delete("1.0", "end")
        self.origins.insert("1.0", "\n".join(settings.get("download_origins", [])))
        self.vars["token"].set("")
        self.show_token.set(False)
        self.token_entry.configure(show="●")
        self.path_text.set(str(self.model.config_path))
        configured = settings.get("configured", False)
        self.api_hint.set("已保存的地址请用“切换服务器”修改；先到“运行状态”停止服务。" if configured
                          else "填写后端地址；例如 https://screen.deqin.info/prod-api。")
        self.api_entry.configure(state="readonly" if configured else "normal")
        self.scope_text.set(('已配置 {} 台打印机　|　{}'.format(len(settings.get('printers', [])),
            '允许领取任务' if settings.get('physical_printing_enabled') else '总领取开关已关闭')) if configured else '尚未保存连接配置')
        self._render_devices(settings.get('printers', []))
        self._update_metrics()
        self.log_view.set_source(settings)

    def _set_printers(self, printers):
        self.service_queues = {item['name']: item for item in printers if item.get('serviceVerified') is True}
        self.queue_display_info = {item['name'].casefold(): item for item in printers if item.get('name')}
        self.queue_display_info.update({name.casefold(): item for name, item in self.service_queues.items()})
        self.device_queue.configure(values=list(self.service_queues))
        self._render_devices(self.saved.get('printers', []))

    def _button_states(self):
        for button in self.buttons:
            button.configure(state="disabled" if self.busy else "normal")
        self.token_entry.configure(state="disabled" if self.busy else "normal")
        self.origins.configure(state="disabled" if self.busy else "normal")
        self.api_entry.configure(state="disabled" if self.busy else "readonly" if self.saved.get("configured") and not self.switching else "normal")
        self.http_check.configure(state="disabled" if self.busy else "normal")
        self.print_check.configure(state="disabled" if self.busy or self.switching else "normal")
        self.switch_button.configure(state="disabled" if self.busy or self.switching or not self.saved.get("configured") else "normal")
        self.delete_device_button.configure(state='normal' if self.devices.selection() and not self.busy else 'disabled')
        self.devices.configure(selectmode='none' if self.busy else 'browse')
        self.device_queue.configure(state='disabled' if self.busy else 'readonly')
        for entry in self.device_entries:
            entry.configure(state='disabled' if self.busy else 'normal')
        self.device_page.update_controls()
        if self.busy:
            return
        conflict = self.service.get("installed") and not self.service.get("owned")
        active = self.service.get("state") in {"RUNNING", "START_PENDING", "STOP_PENDING"}
        self.start_button.configure(state="disabled" if conflict or active or self.switching or not self.saved.get("configured") else "normal")
        self.stop_button.configure(state="normal" if self.service.get("owned") and active else "disabled")

    def _render_service(self):
        if self.service.get("installed") and not self.service.get("owned"):
            self.status_text.set("检测到其他目录的同名服务，请先处理原服务")
        else:
            self.status_text.set(SERVICE_NAMES.get(self.service.get("state"), "服务状态暂不可用"))
        render_service_overview(self)

    def _update_metrics(self):
        rows = self.saved.get("printers", [])
        self.metric_values["registered"].set(f"{len(rows)} 台")
        enabled = sum(bool(p.get('acceptNewJobs')) for p in rows) if self.saved.get("physical_printing_enabled") else 0
        self.metric_values["enabled"].set(f"{enabled} 台")
        pending = self.last_local.get("pendingReports")
        self.metric_values["pending"].set("—" if pending is None else f"{pending} 条")

    def save(self):
        fields, token, model = self._fields(), self.vars["token"].get().strip(), self.model
        if self.service.get("state") in {"RUNNING", "START_PENDING", "STOP_PENDING"}:
            self.notice.set("请先停止服务，再保存配置或更换密钥。")
            return
        switching = self.switching
        def done(value):
            self._apply_settings(value)
            self.notice.set("服务器已切换，原打印记录已保留。请勾选允许领取任务并保存，再启动服务。" if switching
                            else "配置已保存，密钥已在本机加密。请检查打印机后启动服务。")
            if switching and value.get("preservedUnknownAttempts"):
                self.notice.set("服务器已切换，结果不明的旧记录已保留；切回原服务器运行服务后继续自动检查，切换不会重新打印。")
            elif switching and value.get("preservedPreparedAttempts"):
                self.notice.set("服务器已切换，旧服务器未发送的领取记录已保留。请核对旧任务，再按需启用当前服务。")
        self._async(lambda: model.switch_server(fields, token) if switching else model.save(fields, token), done,
                    "正在验证新服务器并切换，原任务记录将保留…" if switching else "正在校验机器身份并保存配置…")

    def begin_switch(self):
        if self.busy or self.switching or not self.saved.get("configured"):
            return
        def done(status):
            self.service = status
            self._render_service()
            if status.get("state") not in {"STOPPED", "NOT_INSTALLED"}:
                self.tabs.select(1)
                self.notice.set("请先点击“停止服务”，等状态变为已停止，再回到连接配置点击“切换服务器”。")
                return
            if status.get("installed") and (not status.get("owned") or
                    Path(status.get("configPath") or "").resolve() != self.model.config_path):
                self.notice.set("服务属于其他安装或配置，请先在原客户端核对；当前配置未修改。")
                return
            self.switching = True
            self.vars["token"].set("")
            self.show_token.set(False)
            self.token_entry.configure(show="●")
            self.allow_http.set(False)
            self.allow_print.set(False)
            self.origins.configure(state="normal")
            self.origins.delete("1.0", "end")
            self.api_hint.set("填写新的 HTTPS 地址和该服务器配发的密钥；原服务器记录会保留。")
            self.save_button.configure(text="验证并切换")
            self.adopt_button.pack_forget()
            self.cancel_switch_button.pack(side="right")
            self.tabs.select(0)
            self._button_states()
            self.api_entry.focus_set()
            self.api_entry.selection_range(0, "end")
            self.notice.set("请修改服务器地址并填写新密钥，点击“验证并切换”。切换后不会自动启动打印。")
        self._async(self.status_reader, done, "正在检查打印服务是否已停止…")

    def cancel_switch(self):
        if self.busy or not self.switching:
            return
        self._apply_settings(self.saved)
        self._button_states()
        self.notice.set("已取消切换，仍使用原服务器配置。")

    def check_server(self):
        fields, token, model = self._fields(), self.vars["token"].get().strip(), self.model
        switching = self.switching
        def done(value):
            printers = value.get('printers', [])
            text = "连接正常　工厂：{}　客户端：{}　已登记打印机：{} 台".format(value.get("factoryId", "—"), value.get("agentId", "—"), len(printers))
            self.notice.set(text)
            if self.dialogs:
                queues = '\n'.join(str(p.get('queueName', '')) for p in printers)
                messagebox.showinfo("服务器连接正常", text + "\n" + (queues or '启动服务后可在打印机页添加设备。'), parent=self.root)
        self._async(lambda: model.inspect_server_switch(fields, token) if switching else model.inspect_server(fields, token), done,
                    "正在检查服务器身份和打印队列绑定，不会出纸…")


    def refresh_printers(self):
        self._async(self.model.printers, lambda value: (self._set_printers(value), self.notice.set("打印机列表已更新。")), "正在读取 Windows 打印机…")

    def adopt(self):
        if self.busy:
            return
        if self.service.get("installed"):
            self.notice.set("已安装服务时使用服务原配置路径。更换配置前请先处理并卸载原服务，保留任务证据。")
            return
        path = filedialog.askopenfilename(title="选择客户端 config.json", filetypes=[("客户端配置", "*.json")], parent=self.root)
        if not path:
            return
        candidate = DesktopModel(Path(path))
        def work():
            value = candidate.load()
            if not value.get("configured"):
                raise DesktopError("所选文件不是有效的已有客户端配置。")
            return value
        def done(value):
            self.generation += 1
            self.model = candidate
            self._apply_settings(value)
            self.notice.set("已沿用原配置和任务记录，未复制或删除数据。")
        self._async(work, done, "正在检查已有配置…")

    def control(self, action):
        if action == "start" and self.switching:
            self.notice.set("请先完成或取消服务器切换，再启动打印服务。")
            return
        if action == "start" and not self.saved.get("configured"):
            self.notice.set("请先保存客户端连接配置，再启动服务。")
            return
        path = self.model.config_path
        def work():
            self.service_action(action, path)
            return self.status_reader()
        def done(value):
            self.service = value
            self._render_service()
            self.notice.set("服务状态已更新。" + ("领取任务已关闭，运行时仅自动检查已有任务并补报回执。"
                if not self.saved.get("physical_printing_enabled") else "打印任务会自动检查并同步后台。"))
            if action == "start" and value.get("owned") and value.get("state") == "RUNNING":
                self.refresh_printers()
        self._async(work, done, "等待 Windows 管理员授权并更新打印服务…")

    def _state_snapshot(self):
        return self.status_reader(), self.model.local_status()

    def _show_snapshot(self, result):
        self.service, local = result
        self.last_local = local
        self._update_metrics()
        self._render_devices(self.saved.get('printers', []))
        self._render_service()
        self.pending_text.set("待补报执行回执：{}　|　待同步自动核验：{}".format(local.get("pendingReports", 0), local.get("pendingObservations", 0)))
        self.tasks.delete(*self.tasks.get_children())
        names = sorted({item.get('queueName') for item in local.get('recent', []) if item.get('queueName')})
        self.task_filter.configure(values=['全部打印机', *names])
        for item in local.get("recent", []):
            if self.queue_filter.get() != '全部打印机' and item.get('queueName') != self.queue_filter.get():
                continue
            observation = item.get("auto_verification")
            self.tasks.insert("", "end", values=(item.get("job_id"), item.get("attempt"), STAGE_NAMES.get(item.get("stage"), item.get("stage")),
                item.get("spool_job_id") or "—", verification_text(observation) if item.get("stage") in {"SUBMITTED", "UNKNOWN", "RESOLVED"} or observation else "—",
                observation_time(observation), item.get('queueName') or '—'))

    def _render_devices(self, rows):
        self.device_page.render(rows)

    def new_device(self):
        self.devices.selection_remove(*self.devices.selection())
        self.editing_printer_id = None
        for field in self.device_vars.values():
            field.set('')
        self.save_device_button.configure(text='保存新增打印机')
        self.discovery_button.configure(text='按 IP 添加队列')
        self.delete_device_button.configure(state='disabled')
        self.notice.set('请选择已有 Windows 队列，或点击“按 IP 添加队列”。')

    def select_device(self, _event=None):
        selection = self.devices.selection()
        if not selection:
            return
        row = next((p for p in self.saved.get('printers', []) if str(p['printerId']) == selection[0]), None)
        if row:
            self.editing_printer_id = selection[0]
            self.save_device_button.configure(text='保存修改')
            self.discovery_button.configure(text='按 IP 更换队列')
            self.delete_device_button.configure(state='disabled' if self.busy else 'normal')
            self.device_vars['queueName'].set(row['queueName'])
            self.device_vars['printerName'].set(row.get('printerName') or row['queueName'])
            self.device_vars['location'].set(row.get('location') or '')
            self.device_page.show_editor()

    def delete_device(self):
        selection = self.devices.selection()
        if self.busy or not selection:
            return
        target = selection[0]
        row = next((p for p in self.saved.get('printers', []) if str(p['printerId']) == target), None)
        if row is None:
            return
        if self.dialogs and not messagebox.askyesno('删除打印机',
                f"删除当前客户端登记的打印机「{row.get('printerName') or row['queueName']}」？\n"
                f"Windows 队列：{row['queueName']}\n"
                "请先在后台处理启用中的仓库绑定，并等待在途任务结束。\n"
                "删除后可重新添加该队列，历史打印记录会保留。",
                parent=self.root):
            return
        def done(value):
            self._apply_settings(value)
            self.notice.set('已删除打印机登记。')
        self._async(lambda: self.model.delete_printer(target), done, '正在删除打印机登记…')

    def sync_devices(self):
        self._async(self.model.sync_printers, self._apply_settings, '正在同步当前电脑的打印机，保留原任务日志…')

    def save_device(self, editing):
        selection = self.devices.selection()
        if editing and not selection:
            self.notice.set('请先选择要修改的打印机。')
            return
        fields = {key: value.get().strip() for key, value in self.device_vars.items()}
        if not fields['queueName']:
            self.notice.set('请先选择 Windows 队列，或使用队列右侧的 IP 按钮。')
            return
        printer_id = selection[0] if editing else None
        def done(value):
            self._apply_settings(value)
            saved = next((row for row in value.get('printers', []) if row['queueName'] == fields['queueName']), None)
            if saved:
                self.device_page.reveal(saved['printerId'])
                self.devices.selection_set(str(saved['printerId']))
                self.select_device()
            self.notice.set('打印机已保存。新增或更换队列后，请核对仓库绑定、恢复领取并试打确认。')
        self._async(lambda: self.model.save_printer(fields, printer_id), done, '正在通过服务账户检查并保存打印机，不会出纸…')

    def toggle_device(self, enabled):
        selection = self.devices.selection()
        if not selection:
            self.notice.set('请先选择一台打印机。')
            return
        def done(value):
            self._apply_settings(value)
            self.notice.set('已恢复该打印机领取；后台仓库必须完成测试确认。' if enabled else '已暂停该打印机领取；已进入 Windows 队列的任务仍可能继续。')
        self._async(lambda: self.model.set_printer_enabled(selection[0], enabled), done, '正在更新所选打印机的领取开关…')

    def service_check(self):
        name = self.device_vars['queueName'].get().strip()
        self._async(lambda: self.model.inspect_service_printer(name),
                    lambda _: self.notice.set('服务账户检查通过，未发送纸张。'),
                    '正在检查服务账户的队列访问权限与纸张…')


    def open_windows_printers(self):
        os.startfile('ms-settings:printers')

    def open_network_discovery(self):
        if self.closed:
            return
        if self.discovery_window is not None and not self.discovery_window.closed:
            self.discovery_window.window.lift()
            return
        from .desktop_add import open_add_printer
        open_add_printer(self, self.device_page.page, queue_target=self.devices.selection())

    def _network_queue_ready(self, result, target):
        if self.closed:
            return
        # Discovery runs in the interactive account. Never append its queues to
        # the serviceVerified selector, or register/enable a backend printer.
        name = result['queueName']
        if not self.saved.get('configured') or self.service.get('state') != 'RUNNING' or self.busy:
            self.notice.set(f'Windows 队列「{name}」已就绪。启动打印服务后，请刷新可用队列并选择服务账户可见的队列。')
            return
        def done(queues):
            self._set_printers(queues)
            if name in self.service_queues and self.devices.selection() == target:
                self.device_vars['queueName'].set(name)
                button = '保存修改' if target else '保存新增打印机'
                self.notice.set(f'已选中「{name}」。请点击“{button}”。')
            elif name in self.service_queues:
                self.notice.set(f'Windows 队列「{name}」已就绪。请选择要配置的打印机和队列。')
            else:
                self.notice.set(f'Windows 队列「{name}」已就绪。请核对服务账户权限，刷新可用队列并选择。')
        self._async(self.model.printers, done, '正在读取服务账户可用的队列…')

    def refresh_status(self):
        self._async(self._state_snapshot, self._show_snapshot, "正在刷新服务与本地任务状态…")

    def _poll(self):
        if self.closed:
            return
        if not self.busy and not self.polling:
            self.polling = True
            generation = self.generation
            future = self.executor.submit(self._state_snapshot)
            future.add_done_callback(lambda task: self.results.put(("poll", generation, task, self._show_snapshot)))
        self._schedule(2500, self._poll)

    def refresh_log(self):
        def done(value):
            render_logs(self, value)
            self.notice.set("日志已刷新。")
        self._async(self.model.log_tail, done, "正在读取客户端日志…")

    def open_directory(self):
        directory = Path(self.saved.get("state_dir") or self.model.config_path.parent)
        if not directory.is_dir():
            self.notice.set("数据目录尚未创建，请先保存配置。")
            return
        os.startfile(str(directory))

    def open_guide(self):
        path = Path(__file__).resolve().parents[1] / "desktop-guide.txt"
        if path.is_file():
            os.startfile(str(path))
        else:
            self.notice.set("使用说明文件缺失，请重新安装完整客户端。")

    def close(self):
        panel = getattr(self, 'add_printer_panel', None)
        if panel and panel.creating_queue:
            self.notice.set('正在添加或复用 Windows 队列，请等待操作完成后关闭。')
            return
        if self.discovery_window is not None:
            if not self.discovery_window.close():
                self.notice.set('正在添加或复用 Windows 网络队列，请等待操作结果后关闭。')
                return
        self.closed = True
        self.vars["token"].set("")
        for timer in self.timers:
            self.root.after_cancel(timer)
        self.timers.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run_gui(config_path=None) -> int:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    DesktopApp(root, DesktopModel(config_path))
    root.mainloop()
    return 0


def smoke_ui(report_path=None) -> int:
    """Bounded packaged UI check with isolated local state. No network/service/print actions."""
    with tempfile.TemporaryDirectory(prefix="dq-desktop-check-") as folder:
        root = tk.Tk()
        app = DesktopApp(root, DesktopModel(Path(folder) / "config.json"), dialogs=False, autoload=False,
                         status_reader=lambda: {"installed": False, "owned": False, "state": "NOT_INSTALLED", "configPath": None})
        app._apply_settings(app.model.load())
        app._set_printers([{"name": "离线界面检查（不打印）"}])
        app._render_service()
        app._button_states()
        root.update()
        assert "disabled" in app.start_button.state()
        app.vars["token"].set("SYNTHETIC-CHECK-NOT-A-CREDENTIAL")
        assert app.token_entry.cget("show") == "●"
        app._apply_settings(app.model.load())
        assert not app.vars["token"].get()
        demo = {**app.model.load(), "configured": True, "api_url": "http://127.0.0.1:8080"}
        app._apply_settings(demo)
        app._button_states()
        app.begin_switch()
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert app.switching and str(app.api_entry.cget("state")) == "normal"
        assert "disabled" in app.start_button.state()
        app.vars["api_url"].set("https://screen.deqin.info/prod-api")
        root.update()
        result = {"ok": True, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
                  "physicalPrinting": False, "networkUsed": False, "serviceChanged": False,
                  "checks": ["window-created", "unconfigured-start-disabled", "credential-masked", "credential-cleared",
                             "server-switch-address-editable", "switch-does-not-start-service"]}
        def capture(suffix):
            if not report_path:
                return
            try:
                from PIL import ImageGrab
                import win32gui
                window = win32gui.GetAncestor(root.winfo_id(), 2)
                destination = Path(report_path)
                ImageGrab.grab(window=window).save(destination.with_name(destination.stem + suffix + ".png"))
            except (ImportError, OSError):
                pass
        capture("")
        app.cancel_switch()
        assert app.vars["api_url"].get() == "http://127.0.0.1:8080"
        app._apply_settings({**demo, 'api_url': 'https://print.example.com/prod-api',
                             'credential_saved': True, 'physical_printing_enabled': True,
                             'allow_loopback_http': False})
        app.api_entry.selection_clear()
        app.notice.set('界面验收预览 · 示例配置')
        root.focus_set()
        app.tabs.select(0)
        root.update()
        capture("-connection")
        render_logs(app, "\n".join([
            "2026-11-19 14:26:10,000 INFO Windows 已接收 job=1026 spool=34",
            "2026-11-19 14:26:12,000 INFO 回执已确认 job=1026 attempt=1 status=SUBMITTED",
            "2026-11-19 14:30:05,000 WARNING 自动核验结果待补报 job=1026 code=NETWORK_ERROR",
            "2026-11-19 14:31:44,000 INFO Windows 已接收 job=1027 spool=35",
            "2026-11-19 14:31:46,000 INFO 回执已确认 job=1027 attempt=1 status=SUBMITTED",
            "2026-11-19 14:32:08,000 INFO Windows 已接收 job=1028 spool=36",
        ]))
        app.tabs.select(2)
        app.notice.set('界面验收预览 · 示例日志')
        root.update()
        log_rows = [key for key in app.log_view.tree.get_children() if key in app.log_view.raw_rows]
        assert [app.log_view.tree.item(key, 'values')[0] for key in log_rows] == [
            '14:32:08', '14:31:46', '14:31:44', '14:30:05', '14:26:12', '14:26:10']
        assert app.log_view.tree.heading('time', 'text') == '时间 ↓'
        assert app.log_view.tree.yview()[0] == 0
        result['checks'].append('log-descending-time')
        capture("-logs")
        app._set_printers([
            {'name': 'USB-GE540', 'port': 'USB001', 'driver': 'Godex GE540', 'serviceVerified': True},
            {'name': 'LAN-GE540', 'port': 'IP_192.0.2.28', 'driver': 'Godex GE540', 'serviceVerified': True,
             'port_target': {'kind': 'standard-tcpip', 'address': '192.0.2.28'}},
        ])
        app._apply_settings({**demo, 'schema_version': 2, 'multi_printer': True,
            'physical_printing_enabled': True, 'printers': [
                {'printerId': '101', 'printerName': '原木标签机', 'queueName': 'USB-GE540', 'location': '原木仓', 'acceptNewJobs': True},
                {'printerId': '102', 'printerName': '烘干标签机', 'queueName': 'LAN-GE540', 'location': '烘干仓', 'acceptNewJobs': False},
                {'printerId': '103', 'printerName': '成品仓标签机', 'queueName': 'LAN-Queue-C', 'location': '成品仓', 'acceptNewJobs': True}]})
        assert len(app.devices.get_children()) == 3
        app.device_page.query.set('192.0.2.28')
        assert app.devices.get_children() == ('102',)
        app.device_page.query.set('')
        app._apply_settings({**app.saved, 'printers': app.saved['printers'][:2]})
        app.devices.selection_set('102')
        app.select_device()
        app._show_snapshot(({'state': 'RUNNING', 'owned': True}, {'pendingReports': 0, 'recent': [
            {'job_id': '301', 'attempt': 1, 'stage': 'SUBMITTED', 'spool_job_id': 7, 'queueName': 'USB-GE540',
             'auto_verification': {'state': 'SPOOLER_REPORTED_PRINTED', 'observedAtMs': 1795069928000}},
            {'job_id': '302', 'attempt': 1, 'stage': 'SUBMITTED', 'spool_job_id': 7, 'queueName': 'LAN-GE540'},
            {'job_id': '303', 'attempt': 1, 'stage': 'SUBMITTED', 'spool_job_id': 8, 'queueName': 'USB-GE540',
             'auto_verification': {'state': 'SPOOLER_REPORTED_PRINTED', 'observedAtMs': 1795069568000}}]}))
        app.queue_filter.set('LAN-GE540')
        app._show_snapshot((app.service, app.last_local))
        assert len(app.tasks.get_children()) == 1
        assert str(app.tasks.item(app.tasks.get_children()[0])['values'][0]) == '302'
        app.tabs.select(3)
        app.notice.set('界面验收预览 · 示例设备')
        root.update()
        app.device_scroll.yview_moveto(1)
        root.update()
        assert app.device_actions.winfo_rooty() + app.device_actions.winfo_height() <= app.device_scroll.winfo_rooty() + app.device_scroll.winfo_height()
        capture("-printers")
        from .desktop_add import open_add_printer
        panel = open_add_printer(app, app.device_page.page)
        root.update()
        assert tuple(panel.queue_box.cget("values")) == ("USB-GE540",)
        capture("-usb")
        app.model.network_inventory = lambda: {'drivers': ['Godex GE540'], 'interfaces': [], 'queues': []}
        panel.choose_mode('lan')
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        panel.ip.set('192.0.2.28')
        panel.driver.set('Godex GE540')
        panel.name.set('LAN-GE540')
        root.update()
        capture("-lan")
        panel.choose_mode('usb')
        panel.advance()
        assert app.device_vars["queueName"].get() == "USB-GE540" and app.editing_printer_id is None
        app.queue_filter.set('全部打印机')
        app._show_snapshot((app.service, app.last_local))
        app._button_states()
        app.notice.set('离线界面检查：示例任务和设备状态。')
        app.tabs.select(1)
        root.update()
        capture("-status")
        result['checks'].extend(['three-printer-list', 'select-independent-printer', 'tasks-filter-by-queue',
                                 'printer-editor-actions-reachable', 'filter-by-ip', 'usb-queue-selection',
                                 'connection-page', 'log-levels-and-dates', 'lan-queue-form'])
        if report_path:
            Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        app.close()
    return 0
