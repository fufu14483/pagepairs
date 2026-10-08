# -*- coding: utf-8 -*-
"""按《同页对照评判标准.md》批量审查交付件，出汇总报告。

用法:
  python audit_report.py <工具目录> <被审 PDF 目录> <报告路径> [mono 根目录]
被审目录里文件名 = <主名>.pdf。

输出：表格化报告（每份一行：撕裂/压字/排版/顺序/像素/缺项/标准 P0P1P2），
并逐份保存 <报告目录>/<主名>.read.txt（audit_read 的明细）。
"""
import os
import re
import sys
import json
import subprocess

ROWS = [
    ("DOC-B05", "DOC-B05_0.pdf", "translated/DOC-B05_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-X01", "J45335262_0.pdf", "translated/J45335262_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B06", "DOC-B06_0.pdf", "translated/DOC-B06_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B07", "DOC-B07_0.pdf", "translated/DOC-B07_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B08", "DOC-B08_0(1).pdf", "translated/DOC-B08_0(1).no_watermark.zh-CN.mono.pdf"),
    ("DOC-B01", "DOC-B01_0.pdf", "translated/DOC-B01_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B09", "DOC-B09_0.pdf", "translated/DOC-B09_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B10", "DOC-B10_0.pdf", "translated/DOC-B10_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A07", "DOC-A07_0(1).pdf", "translated/DOC-A07_0(1).no_watermark.zh-CN.mono.pdf"),
    ("DOC-B11", "DOC-B11_1.pdf", "translated/DOC-B11_1.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B14", "DOC-B14_0.pdf", "translated/DOC-B14_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B15", "DOC-B15_0.pdf", "translated/DOC-B15_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A09", "DOC-A09_0.pdf", "translated/DOC-A09_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-B02", "DOC-B02_0.pdf", "translated/DOC-B02_0.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A05", "DOC-A05_2.pdf", "translated/DOC-A05_2.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A06", "DOC-A06_1.pdf", "translated/DOC-A06_1.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A08", "DOC-A08_0.pdf", "translated/translated/DOC-A08_0.rotsrc.no_watermark.zh-CN.mono.pdf"),
    ("DOC-A04", "DOC-A04_0.pdf", "translated/translated/DOC-A04_0.rotsrc.no_watermark.zh-CN.mono.pdf"),
    ("Z_45335259_Assembly_XLOPBx_Ae01", "Z_45335259_Assembly_XLOPBx_Ae01.pdf", "translated/translated/Z_45335259_Assembly_XLOPBx_Ae01.rotsrc.no_watermark.zh-CN.mono.pdf"),
    ("Z_45335269_Assembly_Drawing_XLOPCx_Ae01", "Z_45335269_Assembly_Drawing_XLOPCx_Ae01.pdf", "translated/translated/Z_45335269_Assembly_Drawing_XLOPCx_Ae01.rotsrc.no_watermark.zh-CN.mono.pdf"),
]


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace")
        return p.stdout or ""
    except Exception:
        return ""


def num(pat, txt, idx=0):
    m = re.findall(pat, txt)
    return m[idx] if idx < len(m) else "?"


def main():
    tool, outdir, rep, root = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    # 可选第 5 参：成品文件名模板（默认 "{nm}.pdf"；出厂成品是
    # "{nm} 同页对照.pdf"，此时直接审交付目录本身，不必再摆中转目录）
    pat = sys.argv[5] if len(sys.argv) > 5 else "{nm}.pdf"
    py = os.path.join(os.path.dirname(root.rstrip("/\\")),
                      "同页对照工具-便携包", "engine", "python", "python.exe")
    if not os.path.exists(py):
        py = r"C:/Users/LOCAL/Desktop/同页对照工具-便携包/engine/python/python.exe"
    lines = []
    lines.append("%-42s %5s %5s %5s %5s %5s %5s %6s %5s | %s"
                 % ("样本", "撕裂", "压字", "排版", "远抛", "像素", "缺项",
                    "P0", "P1", "备注"))
    tot = [0] * 8
    for nm, srel, mrel in ROWS:
        src = os.path.join(root, srel)
        # 竖排图纸的成品是用**转正副本**出的（make_dual 的 rot 分支）。质检必须
        # 拿同一份源，否则 qa_tears 会拿未转正的源去比 → Z_45335259 实测假报
        # 12 条撕裂（用转正源只有 2 条）。
        _rot = os.path.join(root, "translated",
                            os.path.basename(srel)[:-4] + ".rotsrc.pdf")
        if os.path.exists(_rot):
            src = _rot
        mp = os.path.join(root, mrel)
        op = os.path.join(outdir, pat.format(nm=nm))
        if not os.path.exists(op):
            lines.append("%-42s MISSING" % nm)
            continue
        s = json.load(open(op + ".geom.json", encoding="utf-8")) \
            if os.path.exists(op + ".geom.json") else []
        te = num(r"TOTAL tear issues: (\d+)",
                 run([py, tool + "/qa_tears.py", src, op]))
        co = num(r"collisions: (\d+)",
                 run([py, tool + "/qa_synth.py", op, src]))
        lo = run([py, tool + "/qa_layout.py", op])
        lay = num(r"layout warnings: (\d+)", lo)
        # qa_layout 的每页行：page nunits FAR ? ? maxjump ? ?
        far = 0
        for ln in lo.splitlines():
            f = ln.split()
            if len(f) >= 8 and f[0].isdigit() and f[1].isdigit():
                try:
                    far += int(f[2])
                except ValueError:
                    pass
        od = str(far)
        ref = os.path.join(os.path.dirname(rep), "ref_" + nm + ".pdf")
        run([py, tool + "/audit_build.py", src, op, ref])
        px = num(r"pages with unexplained pixel diffs: (\d+)",
                 run([py, tool + "/audit_px.py", ref, op]))
        cn = num(r"blocks: (\d+)",
                 run([py, tool + "/audit_cn.py", src, mp, op]))
        rd = run([py, tool + "/audit_read.py", src, op, mp])
        open(os.path.join(os.path.dirname(rep), nm + ".read.txt"),
             "w", encoding="utf-8").write(rd)
        p0, p1, p2 = (num(r"P0=(\d+)", rd), num(r"P1=(\d+)", rd),
                      num(r"P2=(\d+)", rd))
        note = ""
        try:
            n0 = int(p0)
        except ValueError:
            n0 = 0
        try:
            n1 = int(p1)
        except ValueError:
            n1 = 0
        try:
            n2 = int(p2)
        except ValueError:
            n2 = 0
        for i, v in enumerate([te, co, lay, od, px, cn, n0, n1]):
            try:
                tot[i] += int(v)
            except ValueError:
                pass
        if n0:
            bad = [l.split(None, 3)[1] for l in rd.splitlines()
                   if l.startswith("P0")][:4]
            note = "P0: " + ",".join(bad)
        elif n1:
            note = "P1: " + ",".join(
                sorted({l.split(None, 3)[1] for l in rd.splitlines()
                        if l.startswith("P1")}))
        lines.append("%-42s %5s %5s %5s %5s %5s %5s %5s %5s%s"
                     % (nm, te, co, lay, od, px, cn, p0, p1,
                        (" | " + note) if note else ""))
    lines.append("-" * 118)
    lines.append("合计  %s" % ("  ".join(str(t) for t in tot)))
    txt = "\n".join(lines) + "\n"
    open(rep, "w", encoding="utf-8").write(txt)
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
