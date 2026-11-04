"""Build source, portable Windows and per-user setup artifacts from an explicit allowlist."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_version import VERSION


def release_files(root=ROOT):
    manifest = json.loads((root / "release-files.json").read_text(encoding="utf-8"))
    selected = [root / name for name in manifest["files"]]
    for name in manifest["directories"]:
        selected.extend(p for p in (root / name).rglob("*") if p.is_file()
                        and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"})
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"Invalid release input: {path.relative_to(root)}")
    return sorted(set(selected))


def zip_tree(source, target):
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(source).as_posix())


def collect_licenses(target):
    target.mkdir(parents=True)
    runtime_license = next((Path(sys.base_prefix) / name for name in ("LICENSE.txt", "LICENSE_PYTHON.txt", "LICENSE")
                            if (Path(sys.base_prefix) / name).is_file()), None)
    if runtime_license is None:
        raise RuntimeError("CPython license was not found")
    shutil.copyfile(runtime_license, target / "CPython-LICENSE.txt")
    for package in ("PyMuPDF", "PyInstaller"):
        count = 0
        distribution = importlib.metadata.distribution(package)
        for file in distribution.files or []:
            if any(term in Path(file).name.lower() for term in ("license", "copying")):
                original = Path(distribution.locate_file(file))
                if original.is_file():
                    output = target / package / Path(file)
                    if not output.resolve().is_relative_to(target.resolve()):
                        continue
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, output)
                    count += 1
        if not count:
            raise RuntimeError(f"{package} license files were not found")
    import tkinter
    tcl = Path(tkinter.Tcl().eval("info library")).parent
    for path in tcl.glob("*/license*"):
        destination = target / "TclTk" / path.parent.name / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    tcl_version = tkinter.Tcl().eval("info patchlevel")
    tcl_license = ROOT / "licenses" / f"Tcl-{tcl_version}-license.terms"
    if tcl_license.exists():
        shutil.copyfile(tcl_license, target / f"Tcl-{tcl_version}-license.terms")
    elif not list((target / "TclTk").glob("tcl*/license*")):
        raise RuntimeError(f"Tcl {tcl_version} license is required for this build")
    if not list((target / "TclTk").glob("tk*/license*")):
        raise RuntimeError("Tk license is required for this build")
    import ssl
    openssl_version = ssl.OPENSSL_VERSION.split()[1]
    openssl_license = ROOT / "licenses" / f"OpenSSL-{openssl_version}-LICENSE.txt"
    if not openssl_license.is_file() and openssl_version.startswith("3."):
        openssl_license = ROOT / "licenses" / "OpenSSL-3.x-LICENSE.txt"
    if not openssl_license.is_file():
        raise RuntimeError(f"OpenSSL {openssl_version} license is required for this build")
    shutil.copyfile(openssl_license, target / openssl_license.name)


def collect_sources(output, cache=None):
    artifacts = []
    sources = json.loads((ROOT / "release-sources.json").read_text(encoding="utf-8"))
    for item in sources:
        target = output / item["name"]
        cached = Path(cache) / item["name"] if cache else None
        if cached and cached.is_file():
            with cached.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise ValueError("Upstream cache checksum mismatch: " + item["name"])
            shutil.copyfile(cached, target)
        else:
            with urlopen(item["url"], timeout=60) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Upstream source checksum mismatch: " + item["name"])
        artifacts.append(target)
    return artifacts


def build(output, source_only=False, upstream_cache=None):
    output = output.resolve()
    if output.exists():
        raise ValueError("Build output already exists; choose a new output directory")
    files = release_files()
    stage = output / "source" / "paper-learning-assistant"
    stage.mkdir(parents=True)
    inventory = []
    for source in files:
        relative = source.relative_to(ROOT)
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        inventory.append({"path": relative.as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    (output / "source-inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    source_zip = output / f"paper-learning-assistant-{VERSION}-source.zip"
    zip_tree(output / "source", source_zip)
    artifacts = [source_zip]
    if not source_only:
        if sys.platform != "win32":
            raise RuntimeError("Build Windows binaries on Windows; use --source-only elsewhere")
        command = [sys.executable, "-m", "PyInstaller", "--onedir", "--windowed", "--noupx",
                   "--name", "PaperLearningAssistant", "--icon", str(stage / "assets/app-icon.ico"),
                   "--version-file", str(stage / "assets/windows-version.txt"),
                   "--distpath", str(output / "portable"), "--workpath", str(output / "build-app"),
                   "--specpath", str(output / "spec-app"),
                   "--add-data", f"{stage / 'app'}{os.pathsep}app",
                   "--add-data", f"{stage / 'assets'}{os.pathsep}assets",
                   "--add-data", f"{stage / 'resources'}{os.pathsep}resources",
                   "--collect-all", "pymupdf", str(stage / "launcher.py")]
        subprocess.run(command, cwd=stage, check=True)
        portable = output / "portable/PaperLearningAssistant"
        collect_licenses(portable / "licenses")
        for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "UPSTREAM_SOURCES.md", "CHANGELOG.md"):
            shutil.copyfile(stage / name, portable / name)
        portable_zip = output / f"paper-learning-assistant-{VERSION}-windows-x64.zip"
        zip_tree(portable, portable_zip)
        artifacts.append(portable_zip)
        subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--noupx",
                        "--name", f"paper-learning-assistant-{VERSION}-setup", "--icon", str(stage / "assets/app-icon.ico"),
                        "--version-file", str(stage / "assets/windows-version.txt"),
                        "--distpath", str(output / "setup"), "--workpath", str(output / "build-setup"),
                        "--specpath", str(output / "spec-setup"),
                        "--add-data", f"{portable_zip}{os.pathsep}.",
                        "--add-data", f"{stage / 'assets'}{os.pathsep}assets",
                        str(stage / "installer.py")], cwd=stage, check=True)
        setup = output / "setup" / f"paper-learning-assistant-{VERSION}-setup.exe"
        artifacts.append(setup)
        artifacts.extend(collect_sources(output, upstream_cache))
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name for path in artifacts]
    (output / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artifacts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--upstream-cache", type=Path, help="Optional checksum-verified upstream source cache")
    args = parser.parse_args()
    for artifact in build(args.output, args.source_only, args.upstream_cache):
        print(artifact)
