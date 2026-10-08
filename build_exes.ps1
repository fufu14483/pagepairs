# 重建三个 exe（需联网装 pip 包；产出复制回本目录）
# 架构铁律：启动器 exe 的包里【绝不能】含 PyMuPDF——被污染的包会让 pdf2zh 孙进程
# 的 onnxruntime 崩溃（见 README 踩坑表）。合成（要用 pymupdf、不开子进程）隔离进
# DualEngine.exe。在工具目录同级找/建一个一次性构建 venv。
$ErrorActionPreference = "Stop"
# pip / PyInstaller 是原生命令：非零退出默认**不会**中止脚本，于是构建早就失败了，
# 却要等到回收产物那步才炸出"找不到文件"，把真实原因完全掩盖。
# Windows PowerShell 5.1 没有 $PSNativeCommandUseErrorActionPreference（7.3+ 才有），
# 所以这里用显式退出码检查，5.1 与 7 行为一致。
function Step-Ok([string]$what) {
  if ($LASTEXITCODE -ne 0) { throw "$what 失败，退出码 $LASTEXITCODE" }
  Write-Host "$what ... OK"
}
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
Step-Ok "建构建 venv"
# 版本必须钉死：exe 里 bundle 的 pymupdf 与"便携包 engine/python"必须是同一版本，
# 否则两者的文本分块结果不同 —— 用便携包 python 验证"全绿"的版本，用户跑 exe
# 会得到完全不同的排版（曾因此把整节译文并成一个巨型单元丢到页末）。
$PMV = "1.28.2"
& "$b\venv\Scripts\python.exe" -m pip install --quiet "pymupdf==$PMV" pyinstaller
Step-Ok "安装 pymupdf==$PMV + pyinstaller"

# 1) 启动器（GUI + CLI）：构建目录只放 dual_gui.py + 词表，并排除 pymupdf
Copy-Item "$T\dual_gui.py","$T\heading_glossary.json","$T\cn_fix.json" "$b\gui" -Force
Set-Location "$b\gui"
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed `
  --name "同页对照工具" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." `
  --exclude-module pymupdf --exclude-module numpy --exclude-module matplotlib `
  --exclude-module PIL --exclude-module scipy dual_gui.py
Step-Ok "打包 GUI 启动器（同页对照工具.exe）"
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console `
  --name "DualGuiCLI" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." `
  --exclude-module pymupdf --exclude-module numpy --exclude-module matplotlib `
  --exclude-module PIL --exclude-module scipy dual_gui.py
Step-Ok "打包 CLI 启动器（DualGuiCLI.exe）"

# 2) 引擎：完整构建目录（允许 pymupdf），文件名必须含 DualEngine
Copy-Item "$T\*.py","$T\heading_glossary.json","$T\cn_fix.json" "$b\eng" -Force
Set-Location "$b\eng"
& "$b\venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console `
  --name "DualEngine" --add-data "heading_glossary.json;." --add-data "cn_fix.json;." dual_gui.py
Step-Ok "打包合成引擎（DualEngine.exe）"

# 3) 回收产物（先停掉正在运行的旧 GUI，否则文件被占用）
Get-Process "同页对照工具","DualGuiCLI","DualEngine" -EA SilentlyContinue | Stop-Process -Force
$built = @(
  (Join-Path "$b\gui\dist" "同页对照工具.exe"),
  (Join-Path "$b\gui\dist" "DualGuiCLI.exe"),
  (Join-Path "$b\eng\dist" "DualEngine.exe")
)
# 三个 --name 都含中文/大小写敏感的文件名。若某天脚本被按错误代码页读取（例如用
# Windows PowerShell 5.1 打开无 BOM 的本文件），PyInstaller 会产出一个乱码名的 exe，
# 这里必须在复制前就报出来，否则错误会漂到 CI 的下一步才炸、看不出根因。
foreach ($f in $built) {
  if (-not (Test-Path -LiteralPath $f)) {
    $got = (Get-ChildItem (Split-Path $f) -Filter *.exe -EA SilentlyContinue | ForEach-Object Name) -join ", "
    throw "缺少构建产物 $f（该目录下实际有: ${got}）——若名字是乱码，说明本脚本被按非 UTF-8 代码页读取，请用 pwsh 7 或确认文件带 UTF-8 BOM"
  }
}
Copy-Item $built $T -Force
Remove-Item $b -Recurse -EA SilentlyContinue
Write-Host "OK -> $T（三个 exe 已更新）"
