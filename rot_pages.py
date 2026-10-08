# -*- coding: utf-8 -*-
"""把"整页竖排"的工程图纸页真正转正，让 pdf2zh 和合成引擎都能处理。

背景：pdf2zh/BabelDOC 只重排水平文字，旋转 90° 的图纸正文（标题栏、
工艺说明、针脚表）会被整段丢弃不译；同时合成引擎的正文窗口按水平行
计算，竖排内容落在窗口外被左右竖条切开 —— 这就是"撕裂 + 漏译"。

用法: python rot_pages.py <in.pdf> <out.pdf>       # 打印每页决策
作为模块: angles_for(doc) -> [0|90|270, ...]
          apply_rotation(doc, angles) -> [页号]     # 原地转正（烧进内容流）
          restore_orientation(doc, angles, turned)  # 仅改 /Rotate 转回原显示方向
"""
import json
import os
import sys
from collections import defaultdict

import pymupdf as fitz

MIN_LEN = 6.0          # 短于此（pt，沿基线）的碎片不参与方向投票
ROT_VS_HOR = 1.2       # 竖排墨长需超过横排墨长的倍数
MIN_VLINES = 8         # 竖排行数下限：一条侧栏标题不算"整页竖排"


def _ink_len(bbox):
    x0, y0, x1, y1 = bbox
    return max(x1 - x0, y1 - y0)


def page_dir(page):
    """一页的文字方向统计：横/竖墨长、竖排行数、竖排主流朝向"""
    h = v = 0.0
    vlines = 0
    vsign = defaultdict(float)
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            if not "".join(s["text"] for s in l["spans"]).strip():
                continue
            dx, dy = l.get("dir", (1.0, 0.0))
            ln = _ink_len(l["bbox"])
            if ln < MIN_LEN:
                continue
            if abs(dy) > abs(dx):
                v += ln
                vlines += 1
                vsign[1 if dy > 0 else -1] += ln
            else:
                h += ln
    return {"h": h, "v": v, "vlines": vlines,
            "vsign": max(vsign, key=vsign.get) if vsign else 1}


def angle_for(page):
    """该页转正所需角度（0=不动）。语义同 Page.set_rotation。"""
    s = page_dir(page)
    if s["vlines"] < MIN_VLINES or s["v"] < ROT_VS_HOR * s["h"]:
        return 0
    return 270 if s["vsign"] > 0 else 90     # dir=(0,+1) 转 270° 后为 (1,0)


def angles_for(doc):
    return [angle_for(p) for p in doc]


def apply_rotation(doc, angles):
    """把需要转正的页真正旋转（写进内容流，MediaBox 随之变横）"""
    turned = []
    for pno, ang in enumerate(angles):
        if not ang:
            continue
        page = doc[pno]
        page.set_rotation((page.rotation + ang) % 360)
        page.remove_rotation()
        turned.append(pno)
    return turned


def restore_orientation(doc, angles, turned):
    """交付时转回原纸张方向：只改 /Rotate，视图仍是竖版 A4"""
    for pno in turned:
        doc[pno].set_rotation(angles[pno])
    return turned


def main():
    """rot_pages.py <in.pdf> [<out.pdf>]

    不带 <out.pdf>：只报告每页决策。
    带 <out.pdf>：确有整页竖排时写出转正副本 + <out>.rot.json，否则不写。
    无论哪种都在最后一行打印机器可读的 JSON。
    """
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else None
    doc = fitz.open(src)
    angles = angles_for(doc)
    turned = apply_rotation(doc, angles) if dst else []
    if dst and turned:
        doc.save(dst, garbage=3, deflate=True)
        try:
            with open(dst + ".rot.json", "w", encoding="utf-8") as fh:
                json.dump({"src": os.path.abspath(src), "angles": angles,
                           "turned": turned}, fh)
        except OSError:
            pass
    print(json.dumps({"angles": angles, "turned": turned,
                      "written": bool(dst and turned)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
