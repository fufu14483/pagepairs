# -*- coding: utf-8 -*-
"""make_dual.py — 英文 PDF -> 英中同页对照 PDF（一条命令完成）

用法:
  python make_dual.py 输入.pdf                      # 自动找/做译文 -> 合成 -> 质检
  python make_dual.py 输入.pdf -o 输出.pdf
  python make_dual.py 输入.pdf --mono 已译mono.pdf  # 跳过翻译，直接合成
  python make_dual.py 输入.pdf --translate --p2zh "C:\\...\\pdf2zh_next.exe"
        [--engine siliconfree --apikey-env SILICONFLOW_API_KEY --base https://api.siliconflow.cn/v1]

已验证适用: 某电梯厂商 PRODUCTLINE 工作指导书 (DOC-A01 / DOC-B13 / DOC-B17 版式)；
PRODUCTLINE 技术描述 DOC-A05_2（目录页就地替换 + 密集规格表 + 斜排水印 + 红章）。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 机器相关的默认路径一律不写死：用环境变量覆盖，或靠
# pick_py()/find_p2zh() 的探测链自动定位。
#   DUAL_PY    带 pymupdf 的 Python
#   DUAL_P2ZH  pdf2zh_next 可执行文件
DEFAULT_PY = os.environ.get("DUAL_PY", "")
ROT_TAG = ".rotsrc"          # 整页竖排图纸的转正副本文件名标记
DEFAULT_P2ZH = os.environ.get("DUAL_P2ZH", "")


def find_p2zh():
    """便携包内置引擎优先，其次本机 zotero 安装，最后 PATH。"""
    import shutil
    for c in (os.path.join(HERE, "engine", "python", "python.exe"),
              DEFAULT_P2ZH,
              shutil.which("pdf2zh_next.exe") or shutil.which("pdf2zh_next")):
        if c and os.path.exists(c):
            return c
    return DEFAULT_P2ZH


def pick_py(pref):
    """first interpreter that actually has pymupdf (tool folder may be moved
    away from the venv it was built with)"""
    cands = [pref,
             os.path.join(HERE, "venv", "Scripts", "python.exe"),
             os.path.join(HERE, ".venv", "Scripts", "python.exe"),
             DEFAULT_PY, sys.executable, "python", "py"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            r = subprocess.run([c, "-c", "import pymupdf"],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.TimeoutExpired):
            pass
    sys.exit("找不到带 PyMuPDF 的 Python：pip install pymupdf 或用 --python 指定")


def find_mono(src):
    r"""existing mono donor: <src-dir>\translated\<stem>*.mono.pdf or <stem>*.mono.pdf"""
    stem = os.path.splitext(os.path.basename(src))[0]
    d = os.path.dirname(os.path.abspath(src))
    want_rot = ROT_TAG in stem
    pats = [
        os.path.join(d, "translated", stem + "*.mono*.pdf"),
        os.path.join(d, stem + "*.mono*.pdf"),
        os.path.join(d, "translated", stem + "*.zh-CN*.pdf"),
    ]
    for p in pats:
        hits = [h for h in glob.glob(p) if "no_watermark" in h] or glob.glob(p)
        hits = [h for h in hits if (ROT_TAG in os.path.basename(h)) == want_rot]
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def rotate_source(src, py):
    """整页竖排的图纸页先转正（子进程跑，本进程绝不 import pymupdf，
    否则 pdf2zh 孙进程的 onnxruntime 会崩）。返回新的工作源路径。"""
    d = os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    tdir = os.path.join(d, "translated")
    os.makedirs(tdir, exist_ok=True)
    rot = os.path.join(tdir, stem + ROT_TAG + ".pdf")
    if os.path.exists(rot) and os.path.getmtime(rot) >= os.path.getmtime(src):
        print(":: 沿用已转正的图纸副本", os.path.basename(rot))
        return rot
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([py, os.path.join(HERE, "rot_pages.py"), src, rot],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    try:
        import json
        info = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        print(":: 旋转预处理未生效（按原方向继续）:",
              ((r.stderr or r.stdout or "").strip()[-160:]))
        return src
    if not info.get("turned"):
        return src
    print(f":: 检测到 {len(info['turned'])}/{len(info['angles'])} 页整页竖排图纸，"
          "已转正后翻译/合成（输出为横版）")
    return rot


def translate(src, outdir, a):
    a.p2zh = a.p2zh or find_p2zh()
    if not os.path.exists(a.p2zh):
        sys.exit(f"未找到 pdf2zh 引擎：{a.p2zh}\n（用 --p2zh 指定，或放置便携 engine\\ 文件夹，"
                 "或先用 --mono 传入现成译文）")
    os.makedirs(outdir, exist_ok=True)
    mode = "py" if os.path.basename(a.p2zh).lower() == "python.exe" else "exe"
    head = [a.p2zh, "-c",
            "import sys;sys.argv=['pdf2zh_next']+sys.argv[1:];"
            "from pdf2zh_next.main import cli;cli()"] if mode == "py" else [a.p2zh]
    cmd = head + [src, "--lang-in", a.lang_in, "--lang-out", a.lang_out,
           "--output", outdir, "--no-dual",
           "--watermark-output-mode", "no_watermark",
           # 放开 babeldoc 的短段落门槛：图纸标签（Col./Date/1 : 1）也要译
           "--min-text-length", "1", "--split-short-lines",
           "--translate-table-text"]
    if a.engine == "siliconfree":
        cmd += ["--siliconflowfree"]
    else:                                    # openaicompatible / 其它
        cmd += ["--" + a.engine.replace("_", "-")]
        if a.base:
            cmd += ["--openai-base-url", a.base]
        if a.model:
            cmd += ["--openai-model", a.model]
        if a.apikey_env:
            key = os.environ.get(a.apikey_env, "")
            if not key:
                sys.exit(f"环境变量 {a.apikey_env} 未设置 API key，无法调用翻译引擎")
            cmd += ["--openai-api-key", key]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # insurance if this script is ever frozen into an exe: a PyInstaller
    # parent leaks its _MEIxxxx temp dir (app-home / Tcl / Tk vars) into
    # children, and babeldoc's onnxruntime grandchild then dies with
    # "DLL load failed / 初始化例程失败". PATH itself is needed — skip it.
    for _k in [k for k, v in list(env.items())
               if k != "PATH" and ("_MEI" in k or "_MEI" in str(v))]:
        env.pop(_k, None)
    eng_models = os.path.join(HERE, "engine", "models")
    if mode == "py" and os.path.isdir(eng_models):
        env["USERPROFILE"] = env["HOME"] = eng_models  # 预置模型/字体，首跑免下载
        print(":: 便携翻译引擎：模型随包，已重定向缓存 ->", eng_models)
    import time
    t0 = time.time() - 2
    print(":: 翻译中（pdf2zh_next）...", " ".join(cmd[:6]), "...")
    r = subprocess.run(cmd, check=False, env=env)
    hits = [f for f in (glob.glob(os.path.join(outdir, "**", "*.pdf"),
                                  recursive=True))
            if ".mono" in os.path.basename(f).lower()
            and os.path.getmtime(f) >= t0]
    if not hits:
        sys.exit(f"翻译未产出 mono PDF（pdf2zh_next 退出码 {r.returncode}）"
                 "—— 请检查上方其输出；引擎参数可先用 --mono 传入现成译文绕过此步")
    return max(hits, key=os.path.getmtime)


def run(py, script, *args, allow_fail=False, heal=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if heal is not None:
        env["SYNTH_HEAL"] = str(heal)
    r = subprocess.run([py, os.path.join(HERE, script), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       env=env)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not allow_fail:
        print(out)
        sys.exit(f"{script} 失败 (exit {r.returncode})")
    return out


def coverage_gate(mono, out):
    """合成链路硬闸门（问题 B）：防"空转通过"。

    原先四项质检（撕裂/碰撞/排版/像素）在成品 0 译文时必然全 0，却照常放行
    —— DOC-B05 评审 FAIL 即此：mono 翻译成功，但 make_dual 把 14 个 body
    块全丢，成品实质是未翻译原文件，四项质检却全绿。

    判据：mono 中文 >50 字，而成品用 yahei 合成字体承载的中文 < mono 的 5%
    → 判 FAIL、非零退出。只统计 yahei 合成中文，排除原文档自带的 watermark /
    DOC 受控框中文，避免误杀密集表单（这类页本就只能塞下少量块）。
    """
    def cjk(s):
        return sum(1 for c in s if "一" <= c <= "鿿")

    import pymupdf as fitz
    md, od = fitz.open(mono), fitz.open(out)
    mono_cn = sum(cjk(p.get_text()) for p in md)
    syn_cn = 0
    for p in od:
        for b in p.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                if any("yahei" in s["font"].lower() for s in l["spans"]):
                    syn_cn += cjk("".join(s["text"] for s in l["spans"]))
    # 【2026-09-30 修】分母必须扣掉**有意不译**的字数：闸门的目的是抓"静默
    # 丢块"，而清单/物料表表体（用户需求：只译表头）、页尾标题栏/修订表、
    # 家具词、网格外碎片都是**按设计**省掉的，synth_reflow 已把它们记进
    # geom 侧车的 skip_cn。不扣的话，一份 3 页 31 行 BOM 的图纸会因为
    # "只译表头"被判成丢块（DOC-A09 实测：mono 672 字里 585 字是清单表体，
    # 砍头后只剩 87 字有效，成品 20 字 = 23%；按原口径 20/672=3.0% 直接 FAIL）。
    skip_cn = 0
    try:
        with open(out + ".geom.json", encoding="utf-8") as fh:
            _g = json.load(fh)
        skip_cn = sum(cjk(t) for _pg in _g if isinstance(_pg, dict)
                      for t in (_pg.get("skip_cn") or []))
    except Exception:
        skip_cn = 0
    eff = max(0, mono_cn - skip_cn)
    if eff > 50 and syn_cn < 0.05 * eff:
        print(f":: ✘ 覆盖率硬闸门 FAIL：mono 中文 {mono_cn} 字（其中有意不译 "
              f"{skip_cn} 字 → 有效 {eff} 字），成品合成中文仅 {syn_cn} 字"
              f"（< 有效的 5%={int(0.05 * eff)}）。译文几乎未落版，"
              "疑似 synth_reflow 丢块（overlay 障碍/宿主误判/reflow 落点过远），"
              "请排查后再交付。")
        return False
    print(f":: 覆盖率闸门 OK：mono 中文 {mono_cn} 字 − 有意不译 {skip_cn} 字 = "
          f"有效 {eff} 字，成品合成中文 {syn_cn} 字"
          f"（占有效 {100 * syn_cn / max(1, eff):.1f}%）")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="英文 PDF -> 英中同页对照 PDF（合成 + 质检一键完成）")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--mono", default=None, help="已有的单语中文译文PDF（跳过翻译）")
    ap.add_argument("--translate", action="store_true",
                    help="没有译文时调用 pdf2zh_next 在线翻译")
    ap.add_argument("--lang-in", default="en")
    ap.add_argument("--lang-out", default="zh-CN")
    ap.add_argument("--engine", default="siliconfree")
    ap.add_argument("--base", default=None, help="OpenAI兼容接口 base url")
    ap.add_argument("--model", default=None)
    ap.add_argument("--apikey-env", default=None,
                    help="从此环境变量读 API key，如 SILICONFLOW_API_KEY")
    ap.add_argument("--p2zh", default=None)
    ap.add_argument("--python", dest="py", default=None,
                    help="带 PyMuPDF 的 python 解释器（默认自动探测）")
    ap.add_argument("--no-qa", action="store_true")
    ap.add_argument("--no-rotate", action="store_true",
                    help="不把整页竖排的图纸页转正（保留原纸张方向，竖排正文将无译文）")
    ap.add_argument("--deep-qa", action="store_true",
                    help="追加像素级0丢失 + 中文覆盖率深检（较慢，交付前用）")
    o = ap.parse_args()
    o.py = pick_py(o.py)

    src = os.path.abspath(o.src)
    # 成品命名：剥掉尾部限定符（_0 / _0(1) / .rotsrc），保留完整主名。
    # 旧逻辑按第一个 "_" 切断（re.split(r"[_\s]", ..., 1)[0]），对
    # "Z_45335259_Assembly_XLOPBx_Ae01" 这类下划线开头的图纸号会得出 "Z"，
    # 两份装配图互相覆盖成品（实测）。改为只剥尾部限定符：
    #   DOC-B05_0      -> DOC-B05
    #   DOC-B08_0(1)   -> DOC-B08
    #   DOC-A04_0.rotsrc -> DOC-A04
    #   Z_45335259_Assembly_XLOPBx_Ae01 -> 原样保留
    _stem = os.path.splitext(os.path.basename(src))[0]
    _stem = _stem.split(ROT_TAG)[0].rstrip(". ")
    _stem = re.sub(r"_\d+\(\d+\)$", "", _stem)
    _stem = re.sub(r"_\d+$", "", _stem)
    out = o.out or os.path.join(
        os.path.dirname(src), "translated", _stem + " 同页对照.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if not o.no_rotate:
        src = rotate_source(src, o.py)
    mono = o.mono or find_mono(src)
    if not mono:
        if o.translate:
            mono = translate(src, os.path.join(os.path.dirname(src), "translated"), o)
        else:
            sys.exit("未找到单语中文译文(*.mono*.pdf)。用 --mono <译文.pdf> 指定，"
                     "或加 --translate 在线翻译")
    print(f":: 原文 {src}\n:: 译文 {mono}")

    # preflight: a PDF viewer holding the output open makes MuPDF crash
    # deep inside the run (FzErrorSystem) after minutes of work — fail fast
    try:
        if os.path.exists(out):
            with open(out, "r+b"):
                pass
        else:
            open(out, "ab").close()
    except PermissionError:
        sys.exit(f"输出文件被占用：{out}\n请先关闭正在查看该 PDF 的程序"
                 "（Acrobat/浏览器标签页等），再重跑。")

    ok = True
    lvl = max(1, int(os.environ.get("SYNTH_HEAL", "1")))
    for attempt in range(4):                    # 合成→质检→自愈升级重试
        info = run(o.py, "synth_reflow.py", src, mono, out, heal=lvl)
        print("::" + info.strip().replace("\n", "\n::")[:400])
        if o.no_qa:
            return
        t = run(o.py, "qa_tears.py", src, out, allow_fail=True)
        c = run(o.py, "qa_synth.py", out, src, allow_fail=True)
        tn = re.search(r"TOTAL tear issues: (\d+)", t)
        cn_ = re.search(r"collisions: (\d+)", c)
        tv = int(tn.group(1)) if tn else 0
        cv = int(cn_.group(1)) if cn_ else 0
        print(f":: 质检 撕裂={tv} 碰撞={cv}")
        if not (tv or cv):
            if attempt:
                print(f":: ✔ 自动修复成功（第 {attempt} 轮，自愈级别 {lvl}）")
            break
        if lvl >= 3:
            ok = False
            print(t)
            print("\n".join(c.splitlines()[:10]))
            print(":: ✘ 自愈到最高级别仍不干净——请核查上方质检明细")
            break
        lvl += 1
        print(f":: ⚠ 告警（撕裂 {tv} / 碰撞 {cv}）→ 自动修复：升级自愈级别 {lvl}"
              "（加大切带边距 + 压字中文改排页末），重新合成 …")
    # 覆盖率硬闸门（问题 B）：mono 有中文而成品几乎无合成中文 → 判 FAIL、非零退出，
    # 防止"四项质检全绿但成品 0 译文"的空转通过（DOC-B05 评审 FAIL 即此）。
    if not coverage_gate(mono, out):
        sys.exit(1)
    # 排版可读性（单独一项）：撕裂/碰撞/像素只保证"没弄坏、没压字"，
    # 对"译文被堆到页面另一头"完全失明 —— 这里补上，并计入最终结论。
    ly = run(o.py, "qa_layout.py", out, allow_fail=True)
    lw = re.search(r"layout warnings: (\d+)", ly)
    if lw:
        n_lw = int(lw.group(1))
        if n_lw:
            ok = False
            print(":: ⚠ 排版可读性告警（译文落点离原文过远 / 堆在页末）：")
            for line in ly.splitlines():
                if "<== " in line:
                    print("::   " + line.strip())
        else:
            print(":: 排版可读性：OK（无译文落点过远、无页末堆积）")
    if o.deep_qa:
        # 像素级：按 geom 侧车重建"无中文"参考页，逐像素比对，任何
        # 非蓝色差异 = 源内容被移动/裁掉；覆盖率：mono 中文块是否都到了输出页
        import tempfile
        # 参考页路径必须**每个进程唯一**：多份成品并行重出时，固定名
        # `_dual_ref.pdf` 会被另一个进程覆盖 → audit_px 比的是别人的参考页，
        # 报出假的"未解释像素差"（实测 3 份并行时 DOC-B01 假报 1 页）。
        ref = os.path.join(tempfile.gettempdir(),
                           "_dual_ref_%d.pdf" % os.getpid())
        run(o.py, "audit_build.py", src, out, ref, allow_fail=True)
        px = run(o.py, "audit_px.py", ref, out, allow_fail=True)
        m = re.search(r"pixel diffs: (\d+)", px)
        print(f":: 深检 未解释像素差页={m and m.group(1)}")
        if m and m.group(1) != "0":
            ok = False
            print(px)
        cov = run(o.py, "audit_cn.py", src, mono, out, allow_fail=True)
        m2 = re.search(r"under-represented mono CN blocks: (\d+)", cov)
        n2 = int(m2.group(1)) if m2 else -1
        print(f":: 深检 中文缺项块={n2}（模板脚注/版权类属预期删减，"
              "正文出现请核查）")
    print((":: 完成 -> " if ok else ":: 完成(有质检告警) -> ") + out)


if __name__ == "__main__":
    main()
