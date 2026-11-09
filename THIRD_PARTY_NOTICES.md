# 第三方组件

应用自身使用 GNU Affero General Public License version 3（AGPL-3.0）。各第三方组件保留原作者版权与许可；这里的清单不改变它们的许可条款。

| 组件 | 使用方式 | 许可与来源 |
|---|---|---|
| PyMuPDF 1.28.0 / MuPDF | 本机 PDF 文字、页图及导入 | 本发行版采用 AGPL-3.0 路线；[官方许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)，[源码](https://github.com/pymupdf/PyMuPDF/tree/1.28.0) |
| PDF.js / pdfjs-dist 5.6.205（含官方修复回移） | 浏览器 PDF 阅读与选字 | Apache-2.0；[PDF.js 源码](https://github.com/mozilla/pdf.js)、[本地补丁记录](app/vendor/pdfjs/PATCHES.md)。已保留 `app/vendor/pdfjs/LICENSE` 及字体、CMaps、ICC、WASM 的附属许可文件 |
| CPython 3.12 | Windows 包内运行时 | Python Software Foundation License 及随附许可；[源码](https://www.python.org/downloads/source/) |
| Tcl/Tk 8.6 | Windows 原生启动界面 | Tcl/Tk 许可；[源码及许可](https://www.tcl.tk/software/tcltk/license.html) |
| OpenSSL 3.x | HTTPS 客户端运行时 | Apache-2.0；原始许可附于 `licenses/`；[官方源码](https://github.com/openssl/openssl) |
| PyInstaller 6.22.3 bootloader | 启动打包后的程序 | GPL-2.0-or-later with Bootloader Exception；[许可说明](https://pyinstaller.org/en/stable/license.html) |

Windows 包中的 `licenses/` 另附运行时与 PDF 引擎的原始许可文本；PDF.js 的附属许可随其目录保留。PyInstaller 的构建工具依赖列于 `requirements-build.txt`，它们不是应用功能依赖。

每次发布均提供本应用完整源码、构建脚本与依赖版本。PDF.js vendored 源码对应版本及来源见其 `package.json`；其可读模块与许可均随源码发行。未修改 PyMuPDF、MuPDF 或 CPython 的上游代码。
