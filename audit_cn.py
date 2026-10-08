# -*- coding: utf-8 -*-
"""Which mono-page CN blocks are NOT represented in the dual output?
A mono block counts as represented if >=60% of its CJK chars appear
(page-wide) in the synthesized (yahei) text of the same output page."""
import sys, re, os
import pymupdf as fitz
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synth_dual import get_blocks, has_cjk, norm

# 有意不译判据的**独立复核**：直接复用引擎的家具词表与 CJK 样板正则，但由
# 审查器**自己判定**，不依赖引擎把每个被丢的串都记进 skip_cn
# （引擎的单元路径在 skip_cn 初始化之前就丢了家具，记账覆盖不全 →
#  DOC-B05 p1 的 17 个"缺项"里 13 个其实是页首修订表/缩微胶卷/页面语言）。
try:
    from synth_reflow import is_furniture_cn as _is_furn, CJKBOIL as _BOIL
except Exception:
    def _is_furn(t, substr=False):
        return False
    _BOIL = re.compile(r"(?!x)x")


def syn_cjk(page):
    t = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            t.append("".join(s["text"] for s in l["spans"]))
    return "".join(t)


def _skip_text(out_f):
    """清单表"有意不译"的表体译文（引擎记在 geom sidecar 的 skip_cn）。
    这些块不参与覆盖率缺项统计 —— 不然每裁一张 BOM 就多一串假缺项。"""
    try:
        import json
        g = json.load(open(out_f + ".geom.json", encoding="utf-8"))
    except Exception:
        return ""
    out = []
    for x in g if isinstance(g, list) else []:
        if isinstance(x, dict) and x.get("skip_cn"):
            out.extend(t for t in x["skip_cn"] if t)
    return "".join(out)


def main(src_f, mono_f, out_f):
    src, mono, out = fitz.open(src_f), fitz.open(mono_f), fitz.open(out_f)
    skip = _skip_text(out_f)
    try:
        import json as _json
        _g = _json.load(open(out_f + ".geom.json", encoding="utf-8"))
    except Exception:
        _g = []
    skipped = missing = 0
    for pno in range(len(src)):
        mb = get_blocks(mono[pno])
        syn = syn_cjk(out[pno])
        band = None
        if pno < len(_g) and isinstance(_g[pno], dict):
            band = _g[pno].get("tail_band")
        for b in mb:
            txt = norm(b["text"])
            cjk = [c for c in txt if "\u4e00" <= c <= "\u9fff"]
            if len(cjk) < 3:
                continue
            # 页眉/页脚条、封面题录/修订表、家具词碎片、法务样板：有意不译
            r0 = fitz.Rect(b["rect"])
            ph0 = mono[pno].rect.height
            if r0.y1 < 34 or r0.y0 > ph0 - 58:
                skipped += 1
                continue
            if pno == 0 and r0.y0 > ph0 - 235:
                skipped += 1
                continue
            if _is_furn(txt) or _BOIL.search(re.sub(r"\s+", "", txt)):
                skipped += 1
                continue
            if band:
                r = fitz.Rect(b["rect"])
                ph = mono[pno].rect.height
                vh = min(r.y1, ph) - max(r.y0, float(band[0]))
                if (vh > 0 and vh >= 0.6 * max(r.height, 0.1)
                        and min(r.x1, float(band[2]))
                        - max(r.x0, float(band[1])) > -8.0):
                    skipped += 1
                    continue        # 页尾标题栏/修订表：有意不译
            if skip:
                k = sum(1 for c in cjk if c in skip) / len(cjk)
                if k >= 0.60:
                    skipped += 1
                    continue        # 清单表体：有意不译（见 synth_reflow）
            hit = sum(1 for c in cjk if c in syn) / len(cjk)
            if hit < 0.60:
                missing += 1
                print(f"p{pno+1} y={b['rect'].y0:6.1f} cov={hit:.0%} {txt[:66]!r}")
    if skipped:
        print(f"---- （清单表体有意不译，豁免 {skipped} 块）")
    print("---- under-represented mono CN blocks:", missing)
    return missing


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
