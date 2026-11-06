"""Windows desktop launcher for the local paper learning service."""
from __future__ import annotations

import argparse
import json
import os
import queue
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser
from app_version import PRODUCT, VERSION

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 18766
LOCAL_HTTP = build_opener(ProxyHandler({}))


class LauncherError(RuntimeError):
    """An actionable service lifecycle failure."""


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def request_json(url, *, token=None, timeout=1.0):
    headers = {"Accept": "application/json"}
    data = None
    if token is not None:
        headers.update({"Content-Type": "application/json", "X-Learning-Token": token})
        data = b"{}"
    request = Request(url, data=data, headers=headers)
    try:
        with LOCAL_HTTP.open(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
            detail = body.get("error", str(exc)) if isinstance(body, dict) else str(exc)
        except (ValueError, OSError):
            detail = str(exc)
        raise LauncherError(detail) from exc


def health_for_profile(url, profile):
    try:
        info = request_json(url.rstrip("/") + "/health")
        if isinstance(info, dict) and info.get("product") == PRODUCT and info.get("root") and _same_path(info["root"], profile):
            return info
    except (OSError, URLError, ValueError, TypeError, LauncherError):
        pass
    return None


def server_command(profile, port, *, frozen=None, executable=None, app_root=APP_ROOT):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    command = [str(executable or sys.executable)]
    command += ["--serve"] if frozen else [str(Path(app_root) / "server.py")]
    return command + ["--data-dir", str(Path(profile).resolve()), "--port", str(port)]


def available_port(preferred):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceController:
    def __init__(self, profile, preferred_port=DEFAULT_PORT, app_root=APP_ROOT):
        self.profile = Path(profile).resolve()
        self.preferred_port = preferred_port
        self.app_root = Path(app_root)
        self.url = None
        self.process = None

    @property
    def log_path(self):
        return self.profile / "logs" / "server.log"

    def find_running(self):
        from profile_store import read_service_info
        candidates = [self.url] if self.url else []
        try:
            service = read_service_info(self.profile) or {}
            port = int(service.get("port", 0))
            if 0 < port < 65536:
                candidates.append(f"http://127.0.0.1:{port}")
        except (OSError, ValueError, TypeError):
            pass
        candidates.append(f"http://127.0.0.1:{self.preferred_port}")
        for url in dict.fromkeys(candidates):
            info = health_for_profile(url, self.profile)
            if info:
                self.url = url
                return info
        return None

    def start(self):
        info = self.find_running()
        if info:
            if info.get("version") != VERSION:
                raise LauncherError("此资料库正由其他版本的服务打开。请先从原启动入口退出后台，再使用当前版本打开。")
            return info
        port = available_port(self.preferred_port)
        self.url = f"http://127.0.0.1:{port}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        options = {"cwd": str(self.app_root), "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options["startupinfo"] = startup
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(server_command(self.profile, port, app_root=self.app_root),
                                            stdout=log, stderr=subprocess.STDOUT, **options)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = self.find_running()
            if info:
                return info
            if self.process.poll() is not None:
                # Another launcher may have acquired the same profile while this one started.
                time.sleep(0.3)
                info = self.find_running()
                if info:
                    return info
                raise LauncherError(f"后台未能启动。请打开日志查看原因：\n{self.log_path}")
            time.sleep(0.2)
        raise LauncherError(f"后台启动等待超时。请查看日志：\n{self.log_path}")

    def stop(self):
        from profile_store import ProfileLock, ProfileInUseError
        info = self.find_running()
        if not info:
            return
        if info.get("version") != VERSION:
            raise LauncherError("当前资料库由其他版本运行，请从原启动入口安全停止。")
        if info.get("busy"):
            raise LauncherError("正在处理问答或整理任务，请等待任务结束后再停止后台。")
        library = request_json(self.url + "/api/library", timeout=3)
        token = library.get("token")
        if not token:
            raise LauncherError("后台未返回停止凭据，请保留后台并查看日志。")
        request_json(self.url + "/api/shutdown", token=token, timeout=5)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not health_for_profile(self.url, self.profile):
                # HTTP can close a moment before SQLite and the profile lock are released.
                try:
                    with ProfileLock(self.profile):
                        pass
                except ProfileInUseError:
                    time.sleep(0.15)
                    continue
                if self.process:
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                return
            time.sleep(0.15)
        raise LauncherError("后台尚未确认停止。未强制结束进程，请稍后再试。")

    def backup(self, destination):
        from backup import create_backup
        was_running = bool(self.find_running())
        if was_running:
            self.stop()
        result = None
        failure = None
        try:
            result = create_backup(self.profile, destination)
        except Exception as exc:
            failure = exc
        if was_running:
            try:
                self.start()
            except Exception as exc:
                prefix = "备份已保存，但后台未能重新启动。" if result else f"备份失败：{failure}\n后台也未能重新启动。"
                raise LauncherError(prefix + f"\n{exc}") from exc
        if failure:
            raise failure
        return result


def open_local(path):
    path = Path(path)
    if not path.exists():
        raise LauncherError(f"路径尚不存在：\n{path}")
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class LauncherWindow:
    def __init__(self, root, controller, *, auto_start=True):
        import tkinter as tk
        from tkinter import ttk
        from desktop_ui import DesktopTheme
        self.tk, self.ttk = tk, ttk
        self.root, self.controller = root, controller
        self.working = False
        self.closed = False
        self.probing = False
        self.generation = 0
        self.results = queue.SimpleQueue()
        self.buttons = []
        self.theme = DesktopTheme(root, APP_ROOT / "assets")
        p = self.theme.px
        background, ink, muted = "#f4f7f5", "#203d3d", "#687d7a"
        self.root.title("文献学习助手")
        self.root.geometry(f"{p(760)}x{p(650)}")
        self.root.minsize(p(680), p(640))
        self.root.configure(bg=background)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        icon = APP_ROOT / "assets/app-icon.png"
        if icon.is_file():
            self.icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self.icon)
            if os.name == "nt" and (APP_ROOT / "assets/app-icon.ico").is_file():
                self.root.iconbitmap(str(APP_ROOT / "assets/app-icon.ico"))

        header = tk.Frame(root, bg=background, padx=p(32), pady=p(26))
        header.pack(fill="x")
        tk.Label(header, image=self.theme.logo, bg=background).pack(side="left", padx=(0, p(14)))
        heading = tk.Frame(header, bg=background)
        heading.pack(side="left")
        tk.Label(heading, text="文献学习助手", font=("Microsoft YaHei UI", 20, "bold"),
                 fg=ink, bg=background, anchor="w").pack(anchor="w")
        tk.Label(heading, text="从原文出发，让阅读与思考彼此连接", font=("Microsoft YaHei UI", 10),
                 fg=muted, bg=background).pack(anchor="w", pady=(p(5), 0))
        tk.Label(header, text="本地工作区", font=("Microsoft YaHei UI", 9),
                 fg="#52766c", bg="#e6f0e9", padx=p(12), pady=p(6)).pack(side="right", anchor="n", pady=p(5))

        body = tk.Frame(root, bg=background, padx=p(32))
        body.pack(fill="both", expand=True)
        status_card = tk.Frame(body, bg="white", padx=p(24), pady=p(22),
                               highlightbackground="#e1e9e4", highlightthickness=1)
        status_card.pack(fill="x")
        status_row = tk.Frame(status_card, bg="white")
        status_row.pack(fill="x")
        self.dot = tk.Label(status_row, text="●", font=("Segoe UI", 10), fg="#c38c28", bg="white")
        self.dot.pack(side="left", padx=(0, p(8)))
        tk.Label(status_row, text="阅读服务", font=("Microsoft YaHei UI", 9), fg=muted,
                 bg="white").pack(side="left")
        self.endpoint = tk.StringVar(value="仅在本机访问")
        tk.Label(status_row, textvariable=self.endpoint, font=("Segoe UI", 9), fg=muted,
                 bg="white").pack(side="right")
        self.status = tk.StringVar(value="准备启动")
        self.detail = tk.StringVar(value="正在准备本机阅读环境…")
        tk.Label(status_card, textvariable=self.status, font=("Microsoft YaHei UI", 22, "bold"),
                 fg=ink, bg="white", anchor="w").pack(fill="x", pady=(p(12), p(5)))
        self.detail_label = tk.Label(status_card, textvariable=self.detail, font=("Microsoft YaHei UI", 10),
                 fg=muted, bg="white", anchor="w", justify="left")
        self.detail_label.pack(fill="x")
        actions = tk.Frame(status_card, bg="white")
        actions.pack(fill="x", pady=(p(22), 0))
        self.button(actions, "打开阅读网页  →", self.open_reader, "Primary.TButton").pack(side="left")
        self.stop_button = self.button(actions, "停止后台", self.stop_service, "Quiet.TButton")
        self.stop_button.pack(side="left", padx=(p(10), 0))
        self.button(actions, "查看日志", self.open_log, "Small.TButton").pack(side="right")

        data_card = tk.Frame(body, bg="white", padx=p(24), pady=p(18),
                             highlightbackground="#e1e9e4", highlightthickness=1)
        data_card.pack(fill="x", pady=(p(16), 0))
        data_header = tk.Frame(data_card, bg="white")
        data_header.pack(fill="x")
        tk.Label(data_header, text="我的资料库", font=("Microsoft YaHei UI", 12, "bold"),
                 fg=ink, bg="white").pack(side="left")
        self.button(data_header, "打开目录", lambda: self.local_action(self.controller.profile), "Small.TButton").pack(side="right")
        self.button(data_header, "更换目录", self.choose_profile, "Small.TButton").pack(side="right", padx=p(5))
        self.profile_text = tk.StringVar(value=str(controller.profile))
        self.path_label = tk.Label(data_card, textvariable=self.profile_text, font=("Segoe UI", 10),
                 fg="#4d6661", bg="white", anchor="w", justify="left")
        self.path_label.pack(fill="x", pady=(p(10), p(6)))
        self.library_hint = tk.Label(data_card, text="论文、导读与学习记录保存在这里。", font=("Microsoft YaHei UI", 9),
                 fg=muted, bg="white", anchor="w", justify="left")
        self.library_hint.pack(fill="x")
        tk.Frame(data_card, bg="#edf1ed", height=1).pack(fill="x", pady=(p(17), p(12)))
        archive_row = tk.Frame(data_card, bg="white")
        archive_row.pack(fill="x")
        self.button(archive_row, "备份资料库…", self.backup_profile, "Small.TButton").pack(side="left")
        self.button(archive_row, "从备份恢复…", self.restore_profile, "Small.TButton").pack(side="left", padx=p(12))
        tk.Label(archive_row, text="恢复时另存为新目录", font=("Microsoft YaHei UI", 9),
                 fg=muted, bg="white").pack(side="right")
        footer = tk.Frame(body, bg=background)
        footer.pack(fill="x", pady=(p(20), p(16)))
        tk.Label(footer, text="关闭窗口时，可以保留后台继续阅读。", font=("Microsoft YaHei UI", 9),
                 fg=muted, bg=background).pack(side="left")
        tk.Label(footer, text="v" + VERSION, font=("Segoe UI", 9), fg=muted,
                 bg=background).pack(side="right")
        status_card.bind("<Configure>", lambda event: self.detail_label.configure(wraplength=max(1, event.width-p(50))))
        data_card.bind("<Configure>", lambda event: self.path_label.configure(wraplength=max(1, event.width-p(50))))
        self.result_timer = self.root.after(60, self.drain_results)
        self.poll_timer = self.root.after(1500, self.refresh)
        self.start_timer = None
        if auto_start:
            self.start_timer = self.root.after(150, lambda: self.action("正在启动", self.controller.start))

    def drain_results(self):
        while True:
            try:
                callback, result, error = self.results.get_nowait()
            except queue.Empty:
                break
            callback(result, error)
            if self.closed:
                return
        self.result_timer = self.root.after(60, self.drain_results)

    def button(self, parent, label, command, style):
        button = self.ttk.Button(parent, text=label, command=command, style=style)
        self.buttons.append(button)
        return button

    def show_error(self, exc):
        from tkinter import messagebox
        messagebox.showerror("文献学习助手", str(exc), parent=self.root)

    def action(self, label, function, complete=None):
        if self.working:
            return
        self.working = True
        self.generation += 1
        self.status.set(label)
        self.detail.set("请稍候，完成后会自动更新状态。")
        self.dot.configure(fg="#d99c2f")
        for button in self.buttons:
            button.configure(state="disabled")

        def work():
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, exc
            self.results.put((finish, result, error))

        def finish(result, error):
            self.working = False
            for button in self.buttons:
                button.configure(state="normal")
            self.request_status()
            if error:
                self.show_error(error)
            elif complete:
                complete(result)

        threading.Thread(target=work, daemon=True).start()

    def update_status(self, info, url):
        self.profile_text.set(str(self.controller.profile))
        if info:
            if info.get("version") != VERSION:
                self.status.set("其他版本正在运行")
                self.detail.set("请先从原启动入口退出后台，再使用当前版本打开资料库。")
                self.endpoint.set(url or "仅在本机访问")
                self.dot.configure(fg="#d99c2f")
                self.stop_button.configure(state="disabled")
                return
            self.status.set("正在处理任务" if info.get("busy") else "已准备好阅读")
            self.detail.set("任务结束后可以安全停止或备份。" if info.get("busy") else "阅读服务已在本机运行，点击“打开阅读网页”开始阅读。")
            self.endpoint.set(url or "仅在本机访问")
            self.dot.configure(fg="#1a9a83")
            self.stop_button.configure(state="disabled" if info.get("busy") else "normal")
        else:
            self.status.set("后台已停止")
            self.detail.set("点击“打开阅读网页”即可重新启动，已有资料不会改变。")
            self.endpoint.set("仅在本机访问")
            self.dot.configure(fg="#8b9aa6")
            self.stop_button.configure(state="disabled")

    def request_status(self):
        if self.working or self.probing:
            return
        self.probing = True
        generation = self.generation
        probe = ServiceController(self.controller.profile, self.controller.preferred_port, self.controller.app_root)
        probe.url = self.controller.url

        def receive(result, error):
            self.probing = False
            if generation != self.generation:
                self.request_status()
                return
            if error:
                self.status.set("暂时无法确认后台状态")
                self.detail.set("可稍后重试，或查看日志了解详情。")
                self.dot.configure(fg="#c38c28")
            else:
                self.update_status(result, probe.url)

        def work():
            try:
                result, error = probe.find_running(), None
            except Exception as exc:
                result, error = None, exc
            self.results.put((receive, result, error))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.request_status()
        self.poll_timer = self.root.after(3000, self.refresh)

    def open_reader(self):
        self.action("正在打开阅读网页", self.controller.start,
                    lambda _: webbrowser.open((self.controller.url or "") + "/"))

    def stop_service(self):
        self.action("正在安全停止", self.controller.stop)

    def local_action(self, path):
        try:
            open_local(path)
        except Exception as exc:
            self.show_error(exc)

    def open_log(self):
        self.local_action(self.controller.log_path)

    def switch_profile(self, target):
        def switch():
            self.controller.stop()
            self.controller = ServiceController(target, self.controller.preferred_port)
            return self.controller.start()
        self.action("正在打开资料库", switch)

    def choose_profile(self):
        from tkinter import filedialog, messagebox
        target = filedialog.askdirectory(title="选择资料库目录（空目录会建立新资料库）", parent=self.root)
        if target and not _same_path(target, self.controller.profile):
            if messagebox.askyesno("更换资料库", "切换会安全停止当前资料库的后台服务，并打开所选目录。\n\n现有资料保留在原目录。", parent=self.root):
                self.switch_profile(target)

    def backup_profile(self):
        from tkinter import filedialog, messagebox
        name = "文献学习助手备份-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
        destination = filedialog.asksaveasfilename(title="保存资料库备份", initialfile=name,
            defaultextension=".zip", filetypes=[("ZIP 备份", "*.zip")], parent=self.root)
        if destination:
            self.action("正在备份资料库", lambda: self.controller.backup(destination),
                lambda result: messagebox.showinfo("备份完成", f"已备份 {result['article_count']} 篇文章。\n\n{result['archive']}", parent=self.root))

    def restore_profile(self):
        from tkinter import filedialog
        from backup import inspect_backup
        archive = filedialog.askopenfilename(title="选择资料库备份", filetypes=[("ZIP 备份", "*.zip")], parent=self.root)
        if not archive:
            return
        self.action("正在检查备份", lambda: inspect_backup(archive),
                    lambda summary: self.restore_inspected(archive, summary))

    def restore_inspected(self, archive, summary):
        from tkinter import filedialog, messagebox, simpledialog
        from backup import restore_backup
        parent = filedialog.askdirectory(title=f"备份含 {summary['article_count']} 篇文章：选择恢复位置的上级目录", parent=self.root)
        if not parent:
            return
        name = simpledialog.askstring("恢复到新目录", "新资料库目录名称：", initialvalue="文献资料库-" + time.strftime("%Y%m%d-%H%M%S"), parent=self.root)
        if not name:
            return
        if Path(name).name != name or name in {".", ".."} or any(c in name for c in '<>:"/\\|?*'):
            self.show_error(LauncherError("请输入普通目录名称，不要包含路径或特殊字符。"))
            return
        destination = Path(parent) / name

        def done(result):
            if messagebox.askyesno("恢复完成", f"已恢复 {result['article_count']} 篇文章到新目录。\n\n{destination}\n\n现在打开这个资料库？", parent=self.root):
                self.switch_profile(destination)
        self.action("正在恢复资料库", lambda: restore_backup(archive, destination), done)

    def destroy(self):
        self.closed = True
        for timer in (self.result_timer, self.poll_timer, self.start_timer):
            if timer:
                self.root.after_cancel(timer)
        self.root.destroy()

    def close(self):
        from tkinter import messagebox
        if self.working:
            messagebox.showinfo("操作进行中", "请等待当前操作完成后再关闭启动器。", parent=self.root)
            return
        self.action("正在检查后台", self.controller.find_running, self.confirm_close)

    def confirm_close(self, info):
        from tkinter import messagebox
        if not info:
            self.destroy()
            return
        choice = messagebox.askyesnocancel("关闭启动器", "是否同时安全停止后台服务？\n\n是：停止后台并退出\n否：只关闭窗口，网页可继续使用\n取消：返回启动器", parent=self.root)
        if choice is True:
            self.action("正在安全退出", self.controller.stop, lambda _: self.destroy())
        elif choice is False:
            self.destroy()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--serve" in args:
        args.remove("--serve")
        from server import main as serve_main
        try:
            return serve_main(args)
        except (OSError, RuntimeError, ValueError) as exc:
            if sys.stderr is not None:
                print(f"后台启动失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from None
    parser = argparse.ArgumentParser(description="文献学习助手启动器")
    parser.add_argument("--data-dir", type=Path, help="完整资料库目录")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parsed = parser.parse_args(args)
    if not 1 <= parsed.port <= 65535:
        parser.error("端口必须介于 1 与 65535 之间")
    from profile_store import resolve_profile_root
    import tkinter as tk
    profile_base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_ROOT
    profile = resolve_profile_root(profile_base, parsed.data_dir)
    from desktop_ui import enable_high_dpi
    enable_high_dpi()
    root = tk.Tk()
    LauncherWindow(root, ServiceController(profile, parsed.port))
    root.mainloop()


if __name__ == "__main__":
    main()
