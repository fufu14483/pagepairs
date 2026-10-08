# 重建三个 exe（需联网装 pip 包；产出复制回本目录）
# 架构铁律：启动器 exe 的包里【绝不能】含 PyMuPDF——被污染的包会让 pdf2zh 孙进程
# 的 onnxruntime 崩溃（见 README 踩坑表）。合成（要用 pymupdf、不开子进程）隔离进
# DualEngine.exe。在工具目录同级找/建一个一次性构建 venv。
$ErrorActionPreference = "Stop"
$T   = $PSScriptRoot
$b   = Join-Path $env:TEMP "dualbuild"
# 构建用 Python：优先 $env:DUAL_BUILD_PY，否则取 PATH 上的 python。
# 建议 3.12（与便携包 engine\python 保持一致）。
$py = $env:DUAL_BUILD_PY
if (-not $py) { $py = (Get-Command python -EA SilentlyContinue).Source }
if (-not $py -or -not (Test-Path $py)) {
  throw "找不到构建用 Python：请设 `$env:DUAL_BUILD_PY，或把 python 加进 PATH。"
}
Remove-Item $b -Recurse -EA SilentlyContinue
New-Item -ItemType Directory "$b\gui", "$b\eng" | Out-Null
& $py -m venv "$b\venv"
# 版本必须钉死：exe 里 bundle 的 pymupdf 与"便携包 engine/python"必须是同一版本，
# 否则两者的文本分块结果不同 —— 用便携包 python 验证"全绿"的版本，用户跑 exe
# 会得到完全不同的排版（曾因此把整节译文并成一个巨型单元丢到页末）。
$PMV = "1.28.2"
& "$b\venv\Scripts\python.exe" -m pip install --quiet "pymupdf==$PMV" pyinstaller

# 1) 启动器（GUI + CLI）：构建目录只放 dual_gui.py + 词表，并排除 pymupdf
Copy-Item "$T\dual_gui.py","$T\heading_glossary.json","$T\cn_fix.json" "$b\gui" -Force
Set-Location "$b\gui"
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed `
  --name "同页对照工具" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." `
  --exclude-module pymupdf --exclude-module numpy --exclude-module matplotlib `
  --exclude-module PIL --exclude-module scipy dual_gui.py
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console `
  --name "DualGuiCLI" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." `
  --exclude-module pymupdf --exclude-module numpy --exclude-module matplotlib `
  --exclude-module PIL --exclude-module scipy dual_gui.py

# 2) 引擎：完整构建目录（允许 pymupdf），文件名必须含 DualEngine
Copy-Item "$T\*.py","$T\heading_glossary.json","$T\cn_fix.json" "$b\eng" -Force
Set-Location "$b\eng"
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console `
  --name "DualEngine" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." dual_gui.py

# 3) 回收产物（先停掉正在运行的旧 GUI，否则文件被占用）
Get-Process "同页对照工具","DualGuiCLI","DualEngine" -EA SilentlyContinue | Stop-Process -Force
Copy-Item "$b\gui\dist\同页对照工具.exe","$b\gui\dist\DualGuiCLI.exe","$b\eng\dist\DualEngine.exe" $T -Force
Remove-Item $b -Recurse -EA SilentlyContinue
Write-Host "OK -> $T（三个 exe 已更新）"
