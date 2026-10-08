@echo off
rem 把英文PDF拖到本文件上 => 旁边 translated\<名> 同页对照.pdf
rem 若已有 *.mono*.pdf 译文则直接合成；否则用内置/本机 pdf2zh 在线翻译后合成。
setlocal
set TOOL=%~dp0
set PYTHONIOENCODING=utf-8
if exist "%TOOL%DualGuiCLI.exe" (
  "%TOOL%DualGuiCLI.exe" --cli %* --translate
) else (
  rem DUAL_PY 可指定带 pymupdf 的 Python；未设则用 PATH 上的 python。
  set PY=%DUAL_PY%
  if not defined PY set PY=python
  "%PY%" "%TOOL%make_dual.py" %* --translate
)
pause
