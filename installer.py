"""Per-user Windows installation into a new version directory; never edits libraries."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import zipfile

from app_version import VERSION


def bundled(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def install_bundle(archive, destination):
    destination = Path(destination).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("安装位置已有文件。请选择新的空目录；原版本会保留。")
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        entries = bundle.infolist()
        if len(entries) > 20000 or sum(item.file_size for item in entries) > 2 * 1024**3:
            raise ValueError("安装包大小超出预期。")
        for item in entries:
            path = PurePosixPath(item.filename)
            name = item.filename.casefold()
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename
                    or name in seen or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                raise ValueError("安装包包含无效路径。")
            if any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                   or any(ord(c) < 32 or c in '<>"|?*' for c in part) for part in path.parts):
                raise ValueError("安装包包含不兼容的文件名。")
            seen.add(name)
        if "paperlearningassistant.exe" not in seen:
            raise ValueError("安装包缺少主程序。")
        if bundle.testzip() is not None:
            raise ValueError("安装包损坏，请重新下载。")
        destination.mkdir(parents=True, exist_ok=True)
        # Never replace an existing file, even if another process writes during installation.
        for item in entries:
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
    return destination / "PaperLearningAssistant.exe"


def create_shortcuts(executable):
    """Create versioned shortcuts without replacing an existing user shortcut."""
    script = r'''
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
foreach ($folder in @($desktop, $programs)) {
  $linkPath = Join-Path $folder ('文献学习助手 ' + $env:PLA_INSTALL_VERSION + '.lnk')
  if (Test-Path -LiteralPath $linkPath) { continue }
  $shortcut = $shell.CreateShortcut($linkPath)
  $shortcut.TargetPath = $env:PLA_INSTALL_EXE
  $shortcut.WorkingDirectory = Split-Path -Parent $env:PLA_INSTALL_EXE
  $shortcut.IconLocation = $env:PLA_INSTALL_EXE + ',0'
  $shortcut.Description = '文献学习助手：本地 PDF 阅读与学习'
  $shortcut.Save()
}
'''
    env = dict(os.environ, PLA_INSTALL_EXE=str(executable), PLA_INSTALL_VERSION=VERSION)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


class Installer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("安装文献学习助手")
        self.root.geometry("650x470")
        self.root.resizable(False, False)
        self.root.configure(bg="#f3f6f8")
        self.busy = False
        self.executable = None
        icon = bundled("assets/app-icon.ico")
        if icon.is_file():
            self.root.iconbitmap(str(icon))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(14, 9))
        style.configure("Accent.TButton", foreground="white", background="#256c68")
        style.map("Accent.TButton", background=[("disabled", "#ced9dc"), ("active", "#19554f")],
                  foreground=[("disabled", "#536c78"), ("active", "white")])
        header = tk.Frame(self.root, bg="#153e4b", padx=30, pady=25)
        header.pack(fill="x")
        tk.Label(header, text="文献学习助手", font=("Microsoft YaHei UI", 23, "bold"), bg="#153e4b", fg="white").pack(anchor="w")
        tk.Label(header, text=f"本地阅读 · 有据可循  /  {VERSION} 预发布版", font=("Microsoft YaHei UI", 10), bg="#153e4b", fg="#bad9da").pack(anchor="w", pady=(7, 0))
        body = tk.Frame(self.root, bg="#f3f6f8", padx=30, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="安装程序文件，个人资料另存于你的用户目录。", bg="#f3f6f8", fg="#304855", font=("Microsoft YaHei UI", 11)).pack(anchor="w")
        tk.Label(body, text="原版本和已有文库都会保留。无需管理员权限。", bg="#f3f6f8", fg="#627986", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(6, 16))
        default = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs/PaperLearningAssistant" / VERSION
        self.path = tk.StringVar(value=str(default))
        row = tk.Frame(body, bg="#f3f6f8")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.path, width=55).pack(side="left", fill="x", expand=True)
        self.choose = ttk.Button(row, text="选择…", command=self.select)
        self.choose.pack(side="left", padx=(8, 0))
        self.shortcuts = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="创建桌面和开始菜单快捷方式", variable=self.shortcuts).pack(anchor="w", pady=(12, 0))
        self.status = tk.StringVar(value="准备就绪")
        tk.Label(body, textvariable=self.status, bg="#f3f6f8", fg="#536c78", font=("Microsoft YaHei UI", 10), wraplength=575, justify="left").pack(anchor="w", pady=16)
        actions = tk.Frame(body, bg="#f3f6f8")
        actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="关闭", command=self.close).pack(side="right")
        self.install_button = ttk.Button(actions, text="开始安装", style="Accent.TButton", command=self.install)
        self.install_button.pack(side="right", padx=10)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def select(self):
        chosen = filedialog.askdirectory(title="选择新的空安装目录")
        if chosen:
            self.path.set(chosen)

    def close(self):
        if not self.busy:
            self.root.destroy()

    def install(self):
        if self.executable:
            subprocess.Popen([str(self.executable)], creationflags=subprocess.CREATE_NO_WINDOW)
            self.root.destroy()
            return
        destination = self.path.get()
        shortcuts = self.shortcuts.get()
        self.busy = True
        self.install_button.state(["disabled"])
        self.choose.state(["disabled"])
        self.status.set("正在安装程序文件…")
        def worker():
            try:
                executable = install_bundle(bundled(f"paper-learning-assistant-{VERSION}-windows-x64.zip"), destination)
                shortcut_error = False
                if shortcuts:
                    try:
                        create_shortcuts(executable)
                    except (OSError, subprocess.SubprocessError):
                        shortcut_error = True
                self.root.after(0, lambda: self.done(executable, shortcut_error, shortcuts))
            except Exception as exc:
                detail = f"{exc}\n若目录中留有部分文件，请保留并选择另一个新目录。"
                self.root.after(0, lambda: self.failed(detail))
        threading.Thread(target=worker, daemon=True).start()

    def done(self, executable, shortcut_error, shortcuts):
        self.busy = False
        self.executable = executable
        self.status.set("安装完成。" + ("可从安装目录打开程序。" if shortcut_error or not shortcuts else "桌面与开始菜单入口已准备；同名旧入口会保留。"))
        self.install_button.configure(text="打开文献学习助手")
        self.install_button.state(["!disabled"])

    def failed(self, detail):
        self.busy = False
        self.status.set("安装未完成，已有资料保持原样。")
        self.install_button.state(["!disabled"])
        self.choose.state(["!disabled"])
        messagebox.showerror("安装未完成", detail, parent=self.root)


if __name__ == "__main__":
    Installer().root.mainloop()
