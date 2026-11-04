"""Windows desktop launcher for the local paper learning service."""
from __future__ import annotations

import argparse
import json
import os
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
            detail = json.load(exc).get("error", str(exc))
        except (ValueError, OSError):
            detail = str(exc)
        raise LauncherError(detail) from exc


def health_for_profile(url, profile):
    try:
        info = request_json(url.rstrip("/") + "/health")
        if info.get("product") == PRODUCT and info.get("root") and _same_path(info["root"], profile):
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
        self.tk, self.ttk = tk, ttk
        self.root, self.controller = root, controller
        self.working = False
        self.closing = False
        self.buttons = []
        self.root.title("文献学习助手")
        self.root.geometry("780x590")
        self.root.minsize(700, 560)
        self.root.configure(bg="#f3f6fa")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), foreground="white",
                        background="#176c91", padding=(22, 12), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#125a79"), ("disabled", "#a8bfcd")])
        style.configure("Quiet.TButton", font=("Microsoft YaHei UI", 10), foreground="#314b60",
                        background="#e9eff4", padding=(14, 10), borderwidth=0)
        style.map("Quiet.TButton", background=[("active", "#dce6ee"), ("disabled", "#edf1f5")])
        style.configure("Small.TButton", font=("Microsoft YaHei UI", 9), foreground="#176c91",
                        background="#ffffff", padding=(10, 7), borderwidth=0)
        style.map("Small.TButton", background=[("active", "#edf4f8")])
        icon = APP_ROOT / "assets" / "app-icon.png"
        if icon.is_file():
            self.icon = tk.PhotoImage(file=str(icon))
            self.root.iconphoto(True, self.icon)
            if os.name == "nt" and (APP_ROOT / "assets" / "app-icon.ico").is_file():
                self.root.iconbitmap(str(APP_ROOT / "assets" / "app-icon.ico"))
        header = tk.Frame(root, bg="#132d43", height=132)
        header.pack(fill="x")
        header.pack_propagate(False)
        if hasattr(self, "icon"):
            self.header_icon = self.icon.subsample(4, 4)
            tk.Label(header, image=self.header_icon, bg="#132d43").place(x=30, y=30)
        tk.Label(header, text="文献学习助手", font=("Microsoft YaHei UI", 24, "bold"),
                 fg="#ffffff", bg="#132d43").place(x=110, y=26)
        tk.Label(header, text="从原文出发，让阅读与思考彼此连接", font=("Microsoft YaHei UI", 11),
                 fg="#b4cbdc", bg="#132d43").place(x=113, y=78)
        tk.Label(header, text="v" + VERSION, font=("Segoe UI", 10), fg="#92d8cc",
                 bg="#214459", padx=12, pady=5).place(relx=1.0, x=-28, y=35, anchor="ne")
        body = tk.Frame(root, bg="#f3f6fa", padx=28, pady=22)
        body.pack(fill="both", expand=True)
        status_card = tk.Frame(body, bg="white", padx=20, pady=16)
        status_card.pack(fill="x")
        self.status = tk.StringVar(value="准备启动")
        self.detail = tk.StringVar(value="正在准备本机阅读环境…")
        status_row = tk.Frame(status_card, bg="white")
        status_row.pack(fill="x")
        self.dot = tk.Label(status_row, text="●", font=("Segoe UI", 13), fg="#d99c2f", bg="white")
        self.dot.pack(side="left", padx=(0, 8))
        tk.Label(status_row, textvariable=self.status, font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#1b354a", bg="white").pack(side="left")
        self.endpoint = tk.StringVar(value="仅在本机访问")
        tk.Label(status_row, textvariable=self.endpoint, font=("Segoe UI", 10), fg="#6b8192",
                 bg="white").pack(side="right")
        tk.Label(status_card, textvariable=self.detail, font=("Microsoft YaHei UI", 10),
                 fg="#6a7f90", bg="white", anchor="w").pack(fill="x", pady=(5, 0))
        actions = tk.Frame(body, bg="#f3f6fa")
        actions.pack(fill="x", pady=(17, 20))
        self.button(actions, "打开阅读网页", self.open_reader, "Primary.TButton").pack(side="left")
        self.stop_button = self.button(actions, "停止后台", self.stop_service, "Quiet.TButton")
        self.stop_button.pack(side="left", padx=(12, 0))
        self.button(actions, "打开日志", self.open_log, "Quiet.TButton").pack(side="right")
        data_card = tk.Frame(body, bg="white", padx=20, pady=15)
        data_card.pack(fill="x")
        data_header = tk.Frame(data_card, bg="white")
        data_header.pack(fill="x")
        tk.Label(data_header, text="我的资料库", font=("Microsoft YaHei UI", 11, "bold"),
                 fg="#1b354a", bg="white").pack(side="left")
        self.button(data_header, "打开目录", lambda: self.local_action(self.controller.profile), "Small.TButton").pack(side="right")
        self.button(data_header, "更换目录", self.choose_profile, "Small.TButton").pack(side="right", padx=8)
        self.profile_text = tk.StringVar(value=str(controller.profile))
        tk.Label(data_card, textvariable=self.profile_text, font=("Segoe UI", 10), fg="#546f83",
                 bg="white", anchor="w", justify="left", wraplength=630).pack(fill="x", pady=(4, 7))
        tk.Label(data_card, text="论文、导读与学习记录保存在此目录。备份可带到另一台电脑。",
                 font=("Microsoft YaHei UI", 9), fg="#8495a3", bg="white", anchor="w").pack(fill="x")
        archive_row = tk.Frame(body, bg="#f3f6fa")
        archive_row.pack(fill="x", pady=(15, 0))
        self.button(archive_row, "备份资料库…", self.backup_profile, "Quiet.TButton").pack(side="left")
        self.button(archive_row, "从备份恢复…", self.restore_profile, "Quiet.TButton").pack(side="left", padx=12)
        tk.Label(body, text="关闭此窗口时，可选择让阅读服务继续在后台运行。",
                 font=("Microsoft YaHei UI", 9), fg="#8393a1", bg="#f3f6fa", anchor="w").pack(fill="x", pady=(16, 0))
        self.root.after(1500, self.refresh)
        if auto_start:
            self.root.after(150, lambda: self.action("正在启动", self.controller.start))

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
            self.root.after(0, finish, result, error)

        def finish(result, error):
            self.working = False
            for button in self.buttons:
                button.configure(state="normal")
            self.update_status()
            if error:
                self.show_error(error)
            elif complete:
                complete(result)

        threading.Thread(target=work, daemon=True).start()

    def update_status(self):
        info = self.controller.find_running()
        self.profile_text.set(str(self.controller.profile))
        if info:
            if info.get("version") != VERSION:
                self.status.set("其他版本正在运行")
                self.detail.set("请先从原启动入口退出后台，再使用当前版本打开资料库。")
                self.endpoint.set(self.controller.url or "仅在本机访问")
                self.dot.configure(fg="#d99c2f")
                self.stop_button.configure(state="disabled")
                return
            self.status.set("正在处理任务" if info.get("busy") else "已准备好阅读")
            self.detail.set("任务结束后可以安全停止或备份。" if info.get("busy") else "阅读服务已在本机运行，点击“打开阅读网页”开始阅读。")
            self.endpoint.set(self.controller.url or "仅在本机访问")
            self.dot.configure(fg="#1a9a83")
            self.stop_button.configure(state="normal")
        else:
            self.status.set("后台已停止")
            self.detail.set("点击“打开阅读网页”即可重新启动，已有资料不会改变。")
            self.endpoint.set("仅在本机访问")
            self.dot.configure(fg="#8b9aa6")
            self.stop_button.configure(state="disabled")

    def refresh(self):
        if not self.working:
            self.update_status()
        self.root.after(3000, self.refresh)

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

    def close(self):
        from tkinter import messagebox
        if self.working:
            messagebox.showinfo("操作进行中", "请等待当前操作完成后再关闭启动器。", parent=self.root)
            return
        if not self.controller.find_running():
            self.root.destroy()
            return
        choice = messagebox.askyesnocancel("关闭启动器", "是否同时安全停止后台服务？\n\n是：停止后台并退出\n否：只关闭窗口，网页可继续使用\n取消：返回启动器", parent=self.root)
        if choice is True:
            self.action("正在安全退出", self.controller.stop, lambda _: self.root.destroy())
        elif choice is False:
            self.root.destroy()


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
    root = tk.Tk()
    LauncherWindow(root, ServiceController(profile, parsed.port))
    root.mainloop()


if __name__ == "__main__":
    main()
