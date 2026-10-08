# -*- coding: utf-8 -*-
"""
dual_gui.py — 同页对照 PDF 工具的图形界面（可打包为免 Python 的独立 exe）

GUI:  双击运行，选原文/译文 -> 生成 -> 质检结果一目了然。
拖放: 把英文 PDF 直接拖到 exe 图标上 = 自动填入原文路径。
CLI:  python dual_gui.py --cli 原文.pdf [--mono 译文.pdf] [-o 输出.pdf] [--deep]
      （与图形界面同一套流程，供回归测试/脚本调用）
"""
import os
import re
import sys
import glob
import json
import queue
import contextlib
import threading
import traceback
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, HERE)

# Windows console defaults to GBK; the engine prints CJK + non-breaking
# hyphens from PDF text, which crash a --console build. Force UTF-8 output
# with replacement so an unhandled-encoding crash can never kill a run.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# LAZY engine import — deliberately NOT at module top. synth_reflow pulls in
# PyMuPDF; a PyInstaller process that has pymupdf loaded poisons the babeldoc
# grandchild spawned afterwards (onnxruntime_pybind11_state: DLL 初始化例程
# 失败), so the child-free online-translation stage must run BEFORE the
# engine exists in-process. Proven A/B: no-import parent OK 2/2, pymupdf
# parent FAIL 5/5 (each with 3 internal retries).
E = {}


def _load_engine():
    if "synth_reflow" not in E:
        import synth_reflow          # noqa: E402  (bundled engine)
        import qa_tears              # noqa: E402
        import qa_synth              # noqa: E402
        import qa_layout             # noqa: E402
        import audit_build           # noqa: E402
        import audit_px              # noqa: E402
        import audit_cn              # noqa: E402
        E.update(synth_reflow=synth_reflow, qa_tears=qa_tears,
                 qa_synth=qa_synth, qa_layout=qa_layout,
                 audit_build=audit_build,
                 audit_px=audit_px, audit_cn=audit_cn)
    return E

# 不写死机器路径：DUAL_P2ZH 环境变量可覆盖，否则走 find_p2zh() 探测链。
DEFAULT_P2ZH = os.environ.get("DUAL_P2ZH", "")
# 便携包布局：engine\python\python.exe 自带 pdf2zh_next + babeldoc，
# engine\models\.cache 预置模型/字体 → 换机零配置零下载（翻译本身仍需联网）。
ENGINE_PY = os.path.join(HERE, "engine", "python", "python.exe")
ENGINE_MODELS = os.path.join(HERE, "engine", "models")
P2ZH_BOOT = ("import sys;sys.argv=['pdf2zh_next']+sys.argv[1:];"
             "from pdf2zh_next.main import cli;cli()")
CFG_PATH = os.path.join(HERE, "同页对照工具.config.json")


def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_cfg(patch):
    c = load_cfg()
    c.update(patch)
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as fh:
            json.dump(c, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def find_p2zh():
    """解析翻译引擎：配置指定 > 自带便携引擎 > 本机 zotero 安装 > PATH。"""
    import shutil
    c = str(load_cfg().get("p2zh", "")).strip()
    if c and os.path.exists(c):
        return c
    if os.path.exists(ENGINE_PY):
        return ENGINE_PY
    if os.path.exists(DEFAULT_P2ZH):
        return DEFAULT_P2ZH
    return shutil.which("pdf2zh_next.exe") or shutil.which("pdf2zh_next") or ""


def p2zh_spec(path):
    """"python.exe" → 用 -c bootstrap 跑内置 pdf2zh_next；否则视为引擎 exe。"""
    mode = "py" if os.path.basename(path or "").lower() == "python.exe" else "exe"
    return mode, path


# ---------------------------------------------------------------- pipeline
ROT_TAG = ".rotsrc"          # 整页竖排图纸的转正副本文件名标记


def find_mono(src):
    """existing mono donor next to the source (same rules as make_dual.py)"""
    stem = os.path.splitext(os.path.basename(src))[0]
    d = os.path.dirname(os.path.abspath(src))
    want_rot = ROT_TAG in stem
    pats = [os.path.join(d, "translated", stem + "*.mono*.pdf"),
            os.path.join(d, stem + "*.mono*.pdf"),
            os.path.join(d, "translated", stem + "*.zh-CN*.pdf")]
    for p in pats:
        hits = [h for h in glob.glob(p) if "no_watermark" in h] or glob.glob(p)
        hits = [h for h in hits if os.path.abspath(h) != os.path.abspath(src)]
        # 转正副本的译文只能服务转正副本，普通文档也不能误捡它
        hits = [h for h in hits
                if (ROT_TAG in os.path.basename(h)) == want_rot]
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def _rot_argv():
    """跑 rot_pages 需要 PyMuPDF：便携 python > DualEngine.exe > 本解释器。
    一律走子进程，启动器进程本身绝不 import pymupdf（见文件头架构铁律）。"""
    rp = os.path.join(HERE, "rot_pages.py")
    if os.path.exists(ENGINE_PY):
        return [ENGINE_PY, rp]
    if _in_frozen_launcher():
        return [ENGINE_EXE, "--rotate"] if os.path.exists(ENGINE_EXE) else None
    if os.path.exists(rp):
        return [sys.executable, rp]
    return None


def rotate_source(src, log, enabled=True):
    """整页竖排的图纸页（PCB/机械图）先转正，翻译与合成才看得见正文。
    返回用于后续流程的 PDF 路径；无需旋转时原样返回 src。"""
    if not enabled:
        return src
    argv = _rot_argv()
    if not argv:
        log(":: 未找到可用的旋转预处理后端，按原方向处理")
        return src
    d = os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    tdir = os.path.join(d, "translated")
    os.makedirs(tdir, exist_ok=True)
    rot = os.path.join(tdir, stem + ROT_TAG + ".pdf")
    if os.path.exists(rot) and os.path.getmtime(rot) >= os.path.getmtime(src):
        log(":: 沿用已转正的图纸副本 " + os.path.basename(rot))
        return rot
    env, _ = _clean_env()
    try:
        r = subprocess.run(argv + [src, rot], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           creationflags=NO_WINDOW, timeout=600)
        info = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        log(":: 旋转预处理失败（按原方向继续）：" + str(e)[:120])
        return src
    if not info.get("turned"):
        return src
    log(f":: 检测到 {len(info['turned'])}/{len(info['angles'])} 页整页竖排图纸，"
        f"已转正后翻译/合成（输出为横版，正文不再竖着）")
    return rot



def default_out(src):
    stem = os.path.splitext(os.path.basename(src))[0]
    head = re.split(r"[_\s]", stem, 1)[0] or stem
    return os.path.join(os.path.dirname(os.path.abspath(src)),
                        "translated", head + " 同页对照.pdf")


def call_main(fn, *args):
    """run an argv-style script function in-process with patched sys.argv"""
    old = sys.argv
    sys.argv = ["dual_gui"] + [str(a) for a in args]
    try:
        return fn()
    except SystemExit as e:
        code = e.code
        if isinstance(code, str) and code.strip():
            print("!", code.strip())
        return code if isinstance(code, int) else 1
    finally:
        sys.argv = old


def _clean_env():
    """env for pdf2zh children: PyInstaller onefile leaks its temp _MEIxxxx
    dir (bundled python3xx.dll / VCRUNTIME140.dll / tcl tk …) into child
    processes — babeldoc's onnxruntime grandchild then dies with 'DLL load
    failed / 初始化例程失败'. Verified fix: drop every var whose NAME or VALUE
    still references a PyInstaller _MEIxxxx temp dir, plus the known Tcl/Tk
    and application-home leaks. Leave PATH alone (pdf2zh needs its toolchain)."""
    env = dict(os.environ)
    mei = getattr(sys, "_MEIPASS", "")
    dropped = []
    for k in ("_MEIPASS2", "MEI_PASS", "_PYI_APPLICATION_HOME_DIR",
              "TCL_LIBRARY", "TK_LIBRARY"):
        if env.pop(k, None) is not None:
            dropped.append(k)
    if getattr(sys, "frozen", False) and mei:
        # drop vars that POINT AT the extractor's own temp tree (they make the
        # child's onnxruntime load the wrong DLLs) — PATH is handled by segment
        for k in list(env):
            v = env[k]
            if k != "PATH" and isinstance(v, str) and mei in v:
                env.pop(k, None)
                dropped.append(k)
        parts = os.environ.get("PATH", "").split(os.pathsep)
        kept = [p for p in parts if p and mei not in p]  # mei nonempty here
        if len(kept) != len(parts):
            env["PATH"] = os.pathsep.join(kept)
            dropped.append("PATH:" + str(len(parts) - len(kept)) + " segs")
    if len(dropped) > 5:               # too aggressive ⇒ not the packer's doing
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        dropped = []
    env["PYTHONIOENCODING"] = "utf-8"
    return env, sorted(set(dropped))


# Windows: keep child consoles invisible when the parent is a windowed GUI
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def translate_with_p2zh(src, outdir, p2zh, log):
    import time
    p2zh = p2zh or find_p2zh()
    if not p2zh:
        raise RuntimeError("未找到 pdf2zh 翻译引擎：请用“pdf2zh路径…”选择，"
                           "或把便携 engine\\ 文件夹放进本工具目录，"
                           "或先用 --mono 传入现成译文。")
    mode, exe = p2zh_spec(p2zh)
    os.makedirs(outdir, exist_ok=True)
    args = [src, "--lang-in", "en", "--lang-out", "zh-CN",
            "--output", outdir, "--no-dual", "--siliconflowfree",
            "--watermark-output-mode", "no_watermark",
            # 图纸/表格里的短标签（Col. / Date / 1 : 1 …）默认被 babeldoc
            # 当噪声跳过；译文在本工具里只当"文字供体"，翻得越全越好，
            # 所以放开长度门槛并强制拆短行、尝试表格文字。
            "--min-text-length", "1", "--split-short-lines",
            "--translate-table-text"]
    cmd = ([exe, "-c", P2ZH_BOOT] if mode == "py" else [exe]) + args
    env, dropped = _clean_env()
    if mode == "py" and os.path.isdir(ENGINE_MODELS):
        # 便携包预置的 babeldoc 模型/字体缓存 → 首跑免下载
        env["USERPROFILE"] = env["HOME"] = ENGINE_MODELS
        log(":: 使用内置翻译引擎（便携模式，模型随包）")
    if dropped:
        log(":: 已隔离打包器环境变量: " + ", ".join(sorted(set(dropped))))
    dbg = os.path.join(outdir, "_翻译调试.log")
    last_tail = []
    if os.environ.get("DUAL_DUMP_ENV"):   # diagnostics: exact child env
        try:
            with open(os.path.join(outdir, "_child_env.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(repr(cmd) + "\n\n" + "\n".join(
                    f"{k}={v}" for k, v in sorted(env.items())))
        except OSError:
            pass
    for attempt in (1, 2, 3):
        t0 = time.time() - 2      # accept only files produced by THIS try
        log(f":: 在线翻译中（pdf2zh，第 {attempt} 次尝试，"
            f"可能需要几分钟）…")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           creationflags=NO_WINDOW)
        hits = []
        for f in glob.glob(os.path.join(outdir, "**", "*.pdf"),
                           recursive=True):
            try:
                fresh = os.path.getmtime(f) >= t0
            except OSError:
                fresh = False
            if fresh and ".mono" in os.path.basename(f).lower():
                hits.append(f)
        if hits:
            if attempt > 1:
                log(f":: 第 {attempt} 次尝试成功")
            return max(hits, key=os.path.getmtime)
        last_tail = ((r.stderr or "") + "\n" + (r.stdout or "")
                     ).strip().splitlines()
        try:
            with open(os.path.join(outdir, f"_p2zh_try{attempt}.log"),
                      "w", encoding="utf-8") as fh:
                fh.write("\n".join(last_tail))
        except OSError:
            pass
        why = ""
        joined = " ".join(last_tail)
        if "onnxruntime" in joined or "初始化例程" in joined or \
                "Visual C++" in joined:
            why = ("翻译引擎的版面识别组件（onnxruntime）DLL 初始化失败，"
                   "多为偶发，正在自动重试；持续出现请安装/修复 "
                   "Microsoft Visual C++ 运行库（vc_redist.x64.exe，"
                   "https://aka.ms/vs/17/release/vc_redist.x64.exe）后重启电脑。")
        for ln in last_tail[-15:]:
            log("  p2zh| " + ln)
        if why:
            log(":: " + why)
    try:
        with open(dbg, "w", encoding="utf-8") as fh:
            fh.write("cmd: " + " ".join(cmd) +
                     f"\nreturncode: {r.returncode}\n\n" +
                     "\n".join(last_tail))
    except OSError:
        dbg = "(调试日志无法写入)"
    raise RuntimeError(
        f"翻译未产出 mono PDF（pdf2zh 退出码 {r.returncode}，已重试 3 次）。"
        f"上方 p2zh| 为其输出末尾，完整记录见：{dbg}")


# --------------------------------------------------------------------------
# FROZEN ARCHITECTURE (root-caused by A/B probes, 2026-09):
#   A PyInstaller onefile process — even without importing PyMuPDF at runtime —
#   POISONS any pdf2zh/babeldoc child: its onnxruntime grandchild dies with
#   "DLL load failed / 初始化例程失败" (babeldoc misreports as missing VC++).
#   Evidence: launcher-bundle WITHOUT pymupdf → translate OK 3/3; WITH
#   pymupdf (hidden-import, never imported) → FAIL 3/3 + retried loops.
#   Env/cwd/cmd were byte-identical, so no env-sanitising can help.
#   ⇒ Ship two exes:
#       同页对照工具.exe / DualGuiCLI.exe  (NO pymupdf; runs pdf2zh translate,
#           must stay clean)  --spawns-->  DualEngine.exe (pymupdf inside,
#           synthesises + QA, spawns nothing).
#   Dev mode (plain python) keeps the in-process path — it never poisons.
ENGINE_EXE = os.path.join(HERE, "DualEngine.exe")


def _in_frozen_launcher():
    return getattr(sys, "frozen", False) and "dualengine" not in \
        os.path.basename(sys.executable).lower()


def _run_engine_subprocess(src, mono, out, deep, log):
    """Delegating synthesiser: run the (pymupdf-laden) engine in a separate
    process whose image name contains 'dualengine' so IT runs the pipeline
    in-process. Keeps pdf2zh children away from any pymupdf-poisoned tree."""
    if not os.path.exists(ENGINE_EXE):
        raise RuntimeError(
            f"缺少同页合成引擎：{ENGINE_EXE}\n"
            "请确认 DualEngine.exe 与主程序在同一文件夹。")
    env, _ = _clean_env()
    cmd = [ENGINE_EXE, "--engine-run", src, "--mono", mono, "-o", out]
    if deep:
        cmd.append("--deep")
    result = None
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=env,
                         creationflags=NO_WINDOW)
    for line in p.stdout:
        if line.startswith("__RESULT__"):
            try:
                result = json.loads(line[len("__RESULT__"):])
            except ValueError:
                pass
        else:
            log(line.rstrip())
    rc = p.wait()
    if result is None:
        raise RuntimeError(f"合成引擎异常退出（码 {rc}），见上方日志")
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def run_pipeline(src, mono, out, deep, log):
    """full flow; returns dict(tears, collisions, px, cov). Raises RuntimeError."""
    for f, what in ((src, "英文原文"), (mono, "中文译文")):
        if not f or not os.path.exists(f):
            raise RuntimeError(f"{what}文件不存在：{f}")
    try:
        if os.path.exists(out):
            with open(out, "r+b"):
                pass
        else:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            open(out, "ab").close()
    except PermissionError:
        raise RuntimeError(f"输出文件被占用：{out}\n请先关闭正在查看它的程序再重试。")

    if _in_frozen_launcher():
        return _run_engine_subprocess(src, mono, out, deep, log)

    log(f"原文  {src}\n译文  {mono}\n输出  {out}")
    eng = _load_engine()
    res = {"tears": 0, "collisions": 0, "layout": 0, "px": None, "cov": None,
           "healed": 0}
    lvl = max(1, int(os.environ.get("SYNTH_HEAL", "1")))
    _saved_heal = os.environ.get("SYNTH_HEAL")
    for attempt in range(4):                       # 合成→质检→自愈升级重试
        os.environ["SYNTH_HEAL"] = str(lvl)
        log(":: 合成中（切带重排 + 表格重建）"
            + (f"，自愈级别 {lvl} …" if lvl > 1 else " …"))
        r = call_main(eng["synth_reflow"].main, src, mono, out)
        if r not in (0, None):
            raise RuntimeError("合成失败，见上方日志")
        log(":: 质检① 图形/表格撕裂 …")
        tears = call_main(eng["qa_tears"].main, src, out) or 0
        log(":: 质检② 中文行碰撞 …")
        coll = call_main(eng["qa_synth"].main, out, src) or 0
        res.update(tears=int(tears), collisions=int(coll))
        if not (tears or coll):
            if attempt:
                log(f":: ✔ 自动修复成功（第 {attempt} 轮，自愈级别 {lvl}）")
                res["healed"] = attempt
            break
        if lvl >= 3:
            log(f":: ✘ 自愈到最高级别仍不干净：撕裂 {tears} / 碰撞 {coll}"
                " — 请查看上方质检明细，人工核查该页版式")
            break
        lvl += 1
        log(f":: ⚠ 告警（撕裂 {tears} / 碰撞 {coll}）→ 自动修复：升级自愈级别 "
            f"{lvl}（加大切带边距 + 压字中文改排页末），重新合成 …")
    if _saved_heal is None:               # don't leak escalation into
        os.environ.pop("SYNTH_HEAL", None)  # the next job in this process
    else:
        os.environ["SYNTH_HEAL"] = _saved_heal
    tears, coll = res["tears"], res["collisions"]
    if deep:
        log(":: 质检③ 像素级源内容 0 丢失 …")
        # 与 make_dual 同理：参考页名必须进程唯一，否则并发/多开时会互相覆盖
        ref = os.path.join(tempfile.gettempdir(),
                           "_dualgui_ref_%d.pdf" % os.getpid())
        call_main(eng["audit_build"].main, src, out, ref)
        geo = None
        try:
            import json
            geo = json.load(open(out + ".geom.json", encoding="utf-8"))
        except Exception:
            pass
        eng["audit_px"].audit(ref, out, geo)
        try:
            os.remove(ref)
        except OSError:
            pass
        if mono and os.path.exists(mono):
            log(":: 质检④ 中文覆盖率 …")
            res["cov"] = eng["audit_cn"].main(src, mono, out)
    # 质检⑤ 排版可读性：撕裂/碰撞/像素只保证"没弄坏、没压字"，对"译文被堆
    # 到页面另一头"完全失明；这一项单独把关并计入最终结论。
    log(":: 质检⑤ 排版可读性（译文落点距离 / 页末堆积）…")
    res["layout"] = int(call_main(eng["qa_layout"].main, out) or 0)
    if res["layout"]:
        log(f":: ⚠ 排版可读性告警 {res['layout']} 页 — "
            "有译文块的落点远离它自己的原文（读的时候对不上），见上方明细")
    log(f":: 结果 撕裂={tears} 碰撞={coll} 排版告警={res['layout']}"
        + (f"（缺项块 {res['cov']}，模板脚注类属预期删减）"
           if res.get("cov") is not None else ""))
    return res


# ------------------------------------------------------------------- CLI
def cli_main(argv):
    args = [a for a in argv if a != "--cli"]
    flags = [a for a in args if a.startswith("--")]
    src = next((a for a in args if not a.startswith("-")), None)
    def opt(name):
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return None
    mono = opt("--mono")
    out = opt("-o")
    deep = "--deep" in flags
    rotate = "--no-rotate" not in flags
    if not src:
        print("用法: dual_gui --cli 原文.pdf [--mono 译文.pdf] [-o 输出.pdf] [--deep] "
              "[--translate] [--no-rotate]")
        return 2
    out = out or default_out(src)
    src = rotate_source(src, print, rotate)
    mono = mono or find_mono(src)
    if not mono and "--translate" in flags:
        p2zh = opt("--p2zh") or find_p2zh()
        mono = translate_with_p2zh(
            src, os.path.join(os.path.dirname(src), "translated"), p2zh, print)
    if not mono:
        print("未找到译文：请用 --mono 指定，或加 --translate 在线翻译")
        return 2
    try:
        r = run_pipeline(src, mono, out, deep, print)
        return 0 if (not r["tears"] and not r["collisions"]
                     and not r.get("layout")) else 1
    except RuntimeError as e:
        print("!", e)
        return 2


# ------------------------------------------------------------------- GUI
def gui_main(prefill=None):
    import tkinter as tk
    from tkinter import filedialog, ttk, scrolledtext
    from queue import Empty

    root = tk.Tk()
    root.title("同页对照 PDF 生成器")
    root.geometry("760x560")
    root.minsize(640, 460)

    var_src, var_mono, var_out = tk.StringVar(), tk.StringVar(), tk.StringVar()
    var_p2zh = tk.StringVar(value=find_p2zh())
    var_deep = tk.BooleanVar(value=True)
    var_auto_tr = tk.BooleanVar(value=True)
    var_rot = tk.BooleanVar(value=True)

    def auto_out(*_):
        if var_src.get():
            var_out.set(default_out(var_src.get()))

    def auto_mono(*_):
        m = find_mono(var_src.get()) if var_src.get() else None
        if m:
            var_mono.set(m)

    if prefill and prefill.lower().endswith(".pdf"):
        var_src.set(prefill)
        auto_out()
        auto_mono()

    def pick(var, save=False):
        if var is var_p2zh:
            f, _ = filedialog.askopenfilename(
                title="选择 pdf2zh_next.exe（或便携 python.exe）",
                filetypes=[("可执行文件", "*.exe"), ("全部", "*.*")])
            if f:
                var.set(f)
                save_cfg({"p2zh": f})
            return
        f = (filedialog.asksaveasfilename(defaultextension=".pdf") if save
             else filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")]))
        if f:
            var.set(f)
            if var is var_src:
                auto_out()
                auto_mono()
            if save and var_out.get():
                os.makedirs(os.path.dirname(var_out.get()), exist_ok=True)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    def row(r, label, var, save=False):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm, text="浏览…", width=7,
                   command=lambda: pick(var, save)).grid(row=r, column=2, pady=4)

    row(0, "英文原文 PDF：", var_src)
    row(1, "中文译文 PDF：", var_mono)
    row(2, "输出对照 PDF：", var_out, save=True)
    ttk.Label(frm, text="（译文留空：先在原文同目录 translated\\ 里自动找，找不到则在线翻译）")\
        .grid(row=3, column=1, sticky="w")

    opt = ttk.Frame(frm)
    opt.grid(row=4, column=0, columnspan=3, sticky="w", pady=6)
    ttk.Checkbutton(opt, text="交付级深检（像素0丢失+覆盖率，稍慢）",
                    variable=var_deep).pack(side="left")
    ttk.Checkbutton(opt, text="缺译文时自动在线翻译",
                    variable=var_auto_tr).pack(side="left", padx=12)
    ttk.Checkbutton(opt, text="竖排图纸转正",
                    variable=var_rot).pack(side="left")
    ttk.Button(opt, text="pdf2zh 路径…", width=13,
               command=lambda: pick(var_p2zh)).pack(side="left")

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
    btn_go = ttk.Button(btns, text="▶ 开始生成", width=16)
    btn_go.pack(side="left")
    ttk.Button(btns, text="打开成品", command=lambda: _open(var_out.get())
               ).pack(side="left", padx=8)
    ttk.Button(btns, text="打开所在文件夹", command=lambda: _folder(var_out.get())
               ).pack(side="left")

    logw = scrolledtext.ScrolledText(frm, height=18, state="disabled",
                                     font=("Microsoft YaHei UI", 9))
    logw.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=8)
    frm.rowconfigure(6, weight=1)

    status = ttk.Label(frm, text="就绪", foreground="#063")
    status.grid(row=7, column=0, columnspan=3, sticky="w")

    q = queue.Queue()

    def log(msg):
        for line in str(msg).splitlines() or [""]:
            q.put(line)

    class _Tee:
        """route in-process print() from the engine modules into the log box
        (windowed exe has no console, so stdout would otherwise vanish)"""
        def write(self, s):
            if s.strip("\n"):
                log(s.rstrip("\n"))
        def flush(self):
            pass

    def _open(p):
        if p and os.path.exists(p):
            os.startfile(p)

    def _folder(p):
        d = os.path.dirname(p) if p else None
        if d and os.path.exists(d):
            os.startfile(d)

    def worker(src, mono, out, deep, auto_tr, p2zh, rotate):
        try:
            with contextlib.redirect_stdout(_Tee()):
                r_src = rotate_source(src, log, rotate)
                if r_src != src and mono and ROT_TAG not in os.path.basename(mono):
                    log(":: 原文已转正，所选译文不适用，改为重新查找/在线翻译")
                    mono = None
                src = r_src
                mono = mono or find_mono(src)
                if not mono:
                    if not auto_tr:
                        raise RuntimeError("未找到译文且未启用在线翻译")
                    if not p2zh or not os.path.exists(p2zh):
                        raise RuntimeError("未找到译文，也没有可用的 pdf2zh_next.exe")
                    mono = translate_with_p2zh(
                        src, os.path.join(os.path.dirname(src), "translated"),
                        p2zh, log)
                    log(":: 译文 " + mono)
                q.put(("__out__", mono))
                r = run_pipeline(src, mono, out, deep, log)
                ok = (not r["tears"] and not r["collisions"]
                      and not r.get("layout"))
                q.put(("__done__", (ok, r)))
        except Exception as e:
            tb = "" if isinstance(e, RuntimeError) else traceback.format_exc(limit=3)
            q.put(("__err__", str(e) + ("\n" + tb if tb else "")))

    def start():
        src = var_src.get().strip().strip('"')
        if not src:
            log("请先选择英文原文 PDF")
            return
        mono = var_mono.get().strip().strip('"') or None
        out = var_out.get().strip().strip('"') or default_out(src)
        var_out.set(out)
        btn_go.config(state="disabled")
        status.config(text="● 生成中…", foreground="#06c")
        threading.Thread(target=worker, daemon=True,
                         args=(src, mono, out, bool(var_deep.get()),
                               bool(var_auto_tr.get()), var_p2zh.get().strip(),
                               bool(var_rot.get()))
                         ).start()

    btn_go.config(command=start)

    def poll():
        try:
            while True:
                item = q.get_nowait()
                tag, payload = item if isinstance(item, tuple) else ("__line__", item)
                if tag == "__line__":
                    logw.config(state="normal")
                    logw.insert("end", payload + "\n")
                    logw.see("end")
                    logw.config(state="disabled")
                elif tag == "__out__":
                    var_mono.set(payload)
                elif tag == "__err__":
                    logw.config(state="normal")
                    logw.insert("end", "\n✘ " + payload + "\n")
                    logw.config(state="disabled")
                    status.config(text="✘ 失败 — 见日志", foreground="#c00")
                    btn_go.config(state="normal")
                elif tag == "__done__":
                    ok, r = payload
                    healed = r.get("healed") or 0
                    lay = r.get("layout") or 0
                    if ok and lay:
                        status.config(
                            text=f"⚠ 译文落点/页末堆积告警 {lay} 页 — "
                                 "原文与译文对不上，见日志明细", foreground="#c60")
                    elif ok:
                        status.config(text="✔ 完成：撕裂 0 / 碰撞 0 / 排版 OK"
                                       + (f"（自动修复 {healed} 轮）" if healed else "")
                                       + " — 可交付", foreground="#063")
                    else:
                        status.config(text=f"⚠ 自动修复后仍有告警：撕裂 {r['tears']} / "
                                           f"碰撞 {r['collisions']} / 排版 {lay} — 见日志明细",
                                      foreground="#c60")
                    btn_go.config(state="normal")
        except Empty:
            pass
        root.after(120, poll)

    root.after(120, poll)
    root.mainloop()
    return 0


def gui_check():
    """smoke test: tkinter (+ engine when bundled) usable in this build"""
    import tkinter as tk
    r = tk.Tk()
    r.title("check")
    r.after(120, r.destroy)
    r.mainloop()
    if _in_frozen_launcher():           # clean launcher: engine lives outside
        if os.path.exists(ENGINE_EXE):
            q = subprocess.run([ENGINE_EXE, "--check"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            print("engine:", "OK" if q.returncode == 0
                  else "异常 " + (q.stdout + q.stderr)[-200:])
            return q.returncode
        print("engine: DualEngine.exe 缺失！")
        return 1
    eng = _load_engine()
    # 别用 assert len(eng) == 6：_load_engine 现在装入 7 个模块（多了 audit_cn），
    # 这个数字一过期，`DualEngine.exe --check` 就必然抛 AssertionError →
    # 启动器自检把引擎报成"异常"。改成按名单查缺失，加模块时不会失效。
    _need = ("synth_reflow", "qa_tears", "qa_synth", "qa_layout",
             "audit_build", "audit_px", "audit_cn")
    _miss = [k for k in _need if k not in eng]
    assert not _miss, f"引擎模块缺失: {_miss}"
    print("engine: OK (in-process)")
    return 0


def engine_main(argv):
    """DualEngine.exe entry: run pipeline IN-process, report __RESULT__ json."""
    src = next((a for a in argv[1:] if not a.startswith("-")), None)
    def opt(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None
    mono, out = opt("--mono"), opt("-o")
    deep = "--deep" in argv
    try:
        r = run_pipeline(src, mono, out, deep, print)
        print("__RESULT__" + json.dumps(r, ensure_ascii=False))
        return 0
    except Exception as e:
        print("__RESULT__" + json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2


def main():
    args = sys.argv[1:]
    if args and args[0] == "--engine-run":
        return engine_main(args)
    if args and args[0] == "--rotate":     # 启动器委托：转正整页竖排的图纸页
        import rot_pages
        sys.argv = ["dual_gui"] + args[1:]     # rot_pages 自己读 sys.argv
        return rot_pages.main()
    if args and args[0] == "--cli":
        return cli_main(args)
    if args and args[0] == "--env":       # diagnostics: leak check for frozen builds
        print("frozen:", getattr(sys, "frozen", False),
              "meipass:", getattr(sys, "_MEIPASS", "-"))
        print("leaky vars:", [k for k, v in os.environ.items()
                              if "_MEI" in str(v) or k.startswith("_MEI")])
        print("PATH _MEI entries:", [p for p in os.environ.get("PATH", "").split(os.pathsep)
                                     if "_MEI" in p])
        print("clean PATH _MEI entries:", [p for p in _clean_env()[0].get("PATH", "").split(os.pathsep)
                                           if "_MEI" in p])
        return 0
    if args and args[0] == "--check":
        return gui_check()
    pdf = next((a for a in args if a.lower().endswith(".pdf")), None)
    return gui_main(prefill=pdf)


if __name__ == "__main__":
    sys.exit(main())
