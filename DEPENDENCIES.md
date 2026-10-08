# 依赖与许可证

本项目的合成引擎直接依赖 **PyMuPDF**，在线翻译路径依赖 **pdf2zh_next / babeldoc**。
这三者均为 **AGPL-3.0**（PyMuPDF 为 AGPL-3.0 与商业双许可），因此本项目整体
以 **AGPL-3.0** 发布。若你需要闭源分发，必须自行向 Artifex 购买 PyMuPDF 商业许可。

## 运行期依赖

| 包 | 用途 | 许可 |
|---|---|---|
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | PDF 解析与写入（核心引擎） | AGPL-3.0 或 Artifex 商业许可 |
| [pdf2zh_next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) | 在线翻译后端（可选） | AGPL-3.0 |
| [babeldoc](https://github.com/funstory-ai/BabelDOC) | 版面分析与翻译（pdf2zh 的依赖） | AGPL-3.0 |

## 构建期依赖

| 包 | 用途 | 许可 |
|---|---|---|
| PyInstaller | 打包单文件 exe | GPL-2.0-or-later，附有例外条款，允许打包专有程序 |

## 版本钉死说明

本项目对 PyMuPDF 版本敏感：不同版本 `get_text("dict")` 的**分块规则不同**，
会导致同一份 PDF 得到完全不同的对齐与排版结果。详见 README 踩坑表
「同一份文档，用便携包的 python 跑"全绿"，用户双击 exe 却排版完全不同」。

因此构建脚本（`build_exes.ps1`）中 `$PMV` 变量钉死了版本，且**必须与便携包
`engine\python` 中的 PyMuPDF 版本一致**。升级前请先确认：

```powershell
python -c "import pymupdf; print(pymupdf.__version__)"
```

## 本仓库不含的内容

为控制仓库体积，以下内容**不在版本库中**，通过 GitHub Releases 分发：

- 三个预编译 exe（`同页对照工具.exe` / `DualGuiCLI.exe` / `DualEngine.exe`）
- 便携包（内置便携 Python + 预置 babeldoc 模型与字体，约 1 GB）

按 AGPL-3.0 要求，对应的**完整源码即本仓库**。
