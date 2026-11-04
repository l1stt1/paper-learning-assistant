# 二进制对应源码

每个 Windows 二进制版本旁同时提供本应用的 `source.zip`、PyMuPDF 1.28.0 的 `pymupdf-1.28.0.tar.gz` 和 MuPDF 1.28.0 的 `mupdf-1.28.0-source.tar.gz`，均可在 [同一发布页](https://github.com/l1stt1/paper-learning-assistant/releases) 免费下载，无需购买服务。SHA256SUMS.txt 包含这些发行文件的摘要。

本应用源码包含全部程序、资源、测试及构建脚本；`requirements-build.txt` 固定构建依赖。运行 `python scripts/build_release.py --output <新目录>` 在 Windows 构建。它不修改上游 PDF 引擎，直接打包公开 PyMuPDF 1.28.0 Windows wheel。

上游原始源码的精确下载地址与摘要见 `release-sources.json`。PyMuPDF 的 `setup.py`、`pyproject.toml` 和随包文档包含构建配置，默认 MuPDF 版本同为 1.28.0。希望自行重建 PDF 引擎时可解压两份源码，按照 [PyMuPDF 对应版本的构建说明](https://github.com/pymupdf/PyMuPDF/blob/1.28.0/setup.py) 设置 `PYMUPDF_SETUP_MUPDF_BUILD` 指向 MuPDF 源码目录，然后从 PyMuPDF 源码构建 wheel。上游构建需其声明的 C/C++ 工具链。

PDF.js 5.6.205 的可读发行模块与许可随应用源码提供；上游项目为 [mozilla/pdf.js](https://github.com/mozilla/pdf.js)，准确包版本见 `app/vendor/pdfjs/package.json`。CPython 与 Tcl/Tk 的原始许可随 Windows 包的 `licenses/` 目录提供。本应用许可见 LICENSE，各组件许可见 THIRD_PARTY_NOTICES.md。
