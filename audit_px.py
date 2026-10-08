# -*- coding: utf-8 -*-
"""
audit stage 2: pixel-exact comparison  out vs noCN reference.
Every differing pixel must be explained: blue CN glyph (ref side near-white)
or anti-aliasing. Anything else = moved/clipped/lost source content.
Row-wise fast path keeps it quick.
"""
import sys
import pymupdf as fitz

SC = 2.0


def classify_diff(r, o):
    """r=(R,G,B) reference (noCN), o=(R,G,B) output. Return tag or None."""
    # blue CN ink over light background（215 太紧：中文叠在浅灰水印/底纹上时
    # 参考像素只有 ~0xcd，会被误报成 OVERWRITE。放宽到"接近白"即可，
    # 深灰及更暗的源内容仍然照旧报错。）
    # 198 也太紧：DOC-B18 图纸页的底纹水印实测 0xc3(195)，中文蓝字压上去
    # 的 AA 光晕被记成 4px OVERWRITE。判据再放宽到 176，并**要求参考像素是
    # 中性浅灰**（max-min<40，水印/底纹/淡格线的特征）；深灰及彩色源内容
    # （红章、彩色图线）不受影响，照旧报错。
    blue = o[2] - max(o[0], o[1]) > 12 and o[2] > 90
    light_r = min(r) > 176 and max(r[0] - r[2], r[1] - r[2]) < 40
    if blue and light_r:
        return None
    # faint blue AA halo over white
    if o[2] - max(o[0], o[1]) > 6 and min(r) > 235 and (max(r) < 252
                                                        or min(o) > 200):
        return None
    if max(abs(int(r[k]) - int(o[k])) for k in range(3)) <= 12:
        return None  # tiny raster jitter
    return "LOST/MOVED" if min(r) < 128 else "OVERWRITE"


def audit(ref_f, out_f, rails=None):
    a, b = fitz.open(ref_f), fitz.open(out_f)
    railcols = {}
    exbox = {}   # 引擎有意落笔区（toc_inplace 的 inline_dst 框）：加法式中文
                 # 压在源页格线上时，格线深灰不满足"浅底"豁免，按框豁免。
    if rails:
        for pno, g in enumerate(rails):
            if not isinstance(g, dict):
                continue
            rx = g.get("rails", [])
            railcols[pno] = [int(x * SC) for x in rx]
            exbox[pno] = [[int(v * SC) for v in b]
                          for b in (g.get("inline_dst") or [])]
    if len(a) != len(b):
        print("PAGE COUNT MISMATCH", len(a), len(b))
    total_bad = 0
    for pno in range(max(len(a), len(b))):
        pa, pb = a[pno], b[pno]
        ra, rb = pa.rect, pb.rect
        if abs(ra.height - rb.height) > 1.0 or abs(ra.width - rb.width) > 1.0:
            print(f"p{pno+1}: SIZE {ra.width}x{ra.height} vs {rb.width}x{rb.height}")
            total_bad += 1
            continue
        m = fitz.Matrix(SC, SC)
        # 关键：MuPDF 对"由 show_pdf_page 拼接出来的页"做光栅化时，结果依赖
        # 渲染顺序 —— 同一页在不同顺序下逐位不同（实测同一页四种顺序四个
        # CRC）。复用同一个 Document 逐页渲染，会让成片像素凭空对不上，
        # 于是审计把"渲染抖动"报成"源内容丢失"（旧产物恒有 2~4 页、本样张
        # 甚至 8 页"丢失"，全是假阳性）。改成每页都从全新句柄只渲染这一页，
        # 结果才确定、可比。
        _da, _db = fitz.open(ref_f), fitz.open(out_f)
        x = _da[pno].get_pixmap(matrix=m, alpha=0)
        y = _db[pno].get_pixmap(matrix=m, alpha=0)
        if (x.width, x.height) != (y.width, y.height):
            print(f"p{pno+1}: raster dims differ {x.width}x{x.height} {y.width}x{y.height}")
            total_bad += 1
            continue
        w, h, n = x.width, x.height, x.n
        sa, sb = x.samples, y.samples
        rowb = w * n
        bad_rows = 0
        bad_px = 0
        unexplained = None
        for yy in range(h):
            o0 = yy * rowb
            o1 = o0 + rowb
            ra_ = sa[o0:o1]
            rb_ = sb[o0:o1]
            if ra_ == rb_:
                continue
            bad_rows += 1
            rp = railcols.get(pno, [])
            exb = exbox.get(pno, [])
            for xx in range(w):
                i = xx * n
                if any(abs(xx - cx) <= 6 for cx in rp):
                    continue
                r3 = ra_[i:i+3]
                o3 = rb_[i:i+3]
                if r3 == o3:
                    continue
                if any(b0 <= xx <= b2 and b1 <= yy <= b3
                       for b0, b1, b2, b3 in exb):
                    continue            # 引擎有意落笔（就地/加法中文）
                t = classify_diff(r3, o3)
                if t:
                    bad_px += 1
                    if unexplained is None:
                        unexplained = (yy, xx, bytes(r3), bytes(o3), t)
        if bad_px:
            total_bad += 1
            uy, ux, r3, o3, tag = unexplained
            print(f"p{pno+1}: {bad_px} unexplained px in {bad_rows} rows; "
                  f"first@({ux/SC:.1f},{uy/SC:.1f}) src={r3.hex()} out={o3.hex()} {tag}")
            if os.environ.get("AUDIT_DBG"):
                print(f"   DBG dims={w}x{h} n={n} bad_rows={bad_rows} "
                      f"bad_px={bad_px} railcols={len(railcols.get(pno, []))}"
                      f" rect={ra.width:.1f}x{ra.height:.1f}/{rb.width:.1f}x{rb.height:.1f}")
        elif bad_rows:
            pass
    print("pages with unexplained pixel diffs:", total_bad)


import json, os


if __name__ == "__main__":
    geo = None
    gf = sys.argv[2] + ".geom.json"
    if os.path.exists(gf):
        geo = json.load(open(gf, encoding="utf-8"))
    audit(sys.argv[1], sys.argv[2], geo)
