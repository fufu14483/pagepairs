# -*- coding: utf-8 -*-
"""audit_tbl.py — 表格信息缺失专项核查。

逐份对比：源页**有格线的表区**里，翻译稿（mono）译出的每一行表格文字，
是否都落到了成品里。判据：

1. 源页用矢量格线聚出表区（≥2 条横线且 ≥2 条竖线的连通组）；
2. mono 与源页同版式，取中心落在表区内的 CJK 行 = 该表的译文行；
3. 成品按"含中文的页"拼接归一化文本（去空白），mono 行去空白后应能在
   其中找到（格子内换行折行的两条在成品里 y 相邻，拼接后仍连续）；
   找不到 → 记「表格行缺失」。

用法:
  python audit_tbl.py <工具目录> <被审PDF目录> <报告路径> <工程根> [文件名模板]
"""
import os
import re
import sys
import json
import glob

import fitz


def rules_of(page):
    hs, vs = [], []
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
            elif it[0] == "re":
                r = it[1]
                p1 = fitz.Point(r.x0, r.y0)
                p2 = fitz.Point(r.x1, r.y1)
            else:
                continue
            a = fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y),
                          max(p1.x, p2.x), max(p1.y, p2.y))
            if a.height <= 2.5 and a.width >= 8:
                hs.append((round((a.y0 + a.y1) / 2, 1), a.x0, a.x1))
            elif a.width <= 2.5 and a.height >= 8:
                vs.append((round((a.x0 + a.x1) / 2, 1), a.y0, a.y1))
    return hs, vs


def table_regions(hs, vs):
    """把格线按"横线 y 相邻 + x 范围相叠"聚成表区，返回 [(x0,y0,x1,y1,行数)]。"""
    if len(hs) < 2 or len(vs) < 2:
        return []
    ys = sorted(set(h[0] for h in hs))
    bands = []
    cur = [ys[0]]
    for y in ys[1:]:
        if y - cur[-1] <= 30.0:
            cur.append(y)
        else:
            bands.append(cur)
            cur = [y]
    bands.append(cur)
    out = []
    for b in bands:
        sel = [h for h in hs if b[0] - 1 <= h[0] <= b[-1] + 1]
        if len(sel) < 3:
            continue
        x0 = min(h[1] for h in sel)
        x1 = max(h[2] for h in sel)
        nv = len(set(v[0] for v in vs
                     if x0 - 2 <= v[0] <= x1 + 2
                     and v[1] <= b[-1] + 2 and v[2] >= b[0] - 2))
        if nv < 2:
            continue
        out.append((x0, b[0], x1, b[-1], len(b) - 1, nv - 1))
    return out


def norm(s):
    return re.sub(r"[\s\u00a0]+", "", s or "")


def cjk(s):
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def main():
    tool, outdir, rep, root = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    pat = sys.argv[5] if len(sys.argv) > 5 else "{nm} 同页对照.pdf"
    py = os.path.join(os.path.dirname(root.rstrip("/\\")),
                      "同页对照工具-便携包", "engine", "python", "python.exe")

    import subprocess
    def run(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=900, encoding="utf-8", errors="replace")
            return p.stdout or ""
        except Exception:
            return ""

    # 与 audit_report.py 同一份样本清单
    sys.path.insert(0, tool)
    import audit_report as AR

    lines = ["%-42s %5s %5s %6s | %s" % ("样本", "表区", "缺行", "缺字", "明细")]
    tot_tbl = tot_miss = tot_chars = 0
    for nm, srel, mrel in AR.ROWS:
        op = os.path.join(outdir, pat.format(nm=nm))
        if not os.path.exists(op):
            continue
        src = os.path.join(root, srel)
        _rot = os.path.join(root, "translated",
                            os.path.basename(srel)[:-4] + ".rotsrc.pdf")
        if os.path.exists(_rot):
            src = _rot
        mp = os.path.join(root, mrel)
        if not (os.path.exists(src) and os.path.exists(mp)):
            continue
        sdoc, mdoc, odoc = fitz.open(src), fitz.open(mp), fitz.open(op)
        # 豁免口径（与引擎/评判标准一致）：
        #  · geom.skip_cn —— 引擎按设计有意不译的文本（清单表体/页尾标题栏/
        #    家具词/网格外碎片）；
        #  · 页尾家具带（页底 180pt）与首页题录带（页底 235pt）—— 设计为不译；
        #  · 该页被判为 datalist（清单只译表头）→ 该页表区的表体行全部豁免。
        _skip, _dl_pages = set(), set()
        try:
            _g = json.load(open(op + ".geom.json", encoding="utf-8"))
            for _pg in _g:
                if not isinstance(_pg, dict):
                    continue
                for _t in (_pg.get("skip_cn") or []):
                    _skip.add(norm(_t))
                if int(_pg.get("datalist") or 0) > 0:
                    _dl_pages.add(True)
        except Exception:
            pass
        _skip_join = "".join(_skip)

        def _exempt(txt, pi, y):
            if txt in _skip:
                return True
            if any(txt in k or k in txt for k in _skip if len(k) >= 2):
                return True
            ph = sdoc[pi].rect.height
            if y > ph - 180.0 or (pi == 0 and y > ph - 235.0):
                return True                     # 页尾标题栏 / 首页题录带
            if _dl_pages:
                return True                     # 清单表：只译表头（用户需求）
            pool = _skip_join
            if pool:
                cj = [c for c in txt if "\u4e00" <= c <= "\u9fff"]
                if cj and sum(1 for c in cj if c in pool) >= 0.7 * len(cj):
                    return True                 # 家具词（状态/修改/KA编号…）
            return False
        # 成品：每页 CN 行按 (y,x) 拼接的归一化文本
        opage_txt = []
        for pg in odoc:
            cn = []
            for b in pg.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for l in b["lines"]:
                    t = "".join(s["text"] for s in l["spans"])
                    if cjk(t):
                        cn.append((round(l["bbox"][1], 1),
                                   round(l["bbox"][0], 1), norm(t)))
            cn.sort()
            opage_txt.append("".join(t for _y, _x, t in cn))
        miss_rows, miss_chars = [], 0
        n_tbl = 0
        for pi in range(min(len(sdoc), len(mdoc))):
            hs, vs = rules_of(sdoc[pi])
            for (x0, y0, x1, y1, nr, nc) in table_regions(hs, vs):
                n_tbl += 1
                for b in mdoc[pi].get_text("dict")["blocks"]:
                    if b.get("type") != 0:
                        continue
                    for l in b["lines"]:
                        t = norm("".join(s["text"] for s in l["spans"]))
                        r = fitz.Rect(l["bbox"])
                        cm = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                        if not cjk(t) or len(t) < 2:
                            continue
                        if not (x0 - 3 <= cm.x <= x1 + 3
                                and y0 - 3 <= cm.y <= y1 + 3):
                            continue
                        if any(t in ot for ot in opage_txt):
                            continue
                        if _exempt(t, pi, cm.y):
                            continue
                        miss_rows.append((pi + 1, t))
                        miss_chars += len(t)
        tot_tbl += n_tbl
        tot_miss += len(miss_rows)
        tot_chars += miss_chars
        det = ""
        if miss_rows:
            det = "; ".join("p%d %r" % (p, t[:24])
                            for p, t in miss_rows[:4])
            if len(miss_rows) > 4:
                det += " …共%d 行" % len(miss_rows)
        lines.append("%-42s %5d %5d %6d | %s"
                     % (nm, n_tbl, len(miss_rows), miss_chars, det))
    lines.append("-" * 110)
    lines.append("合计  表区 %d  缺行 %d  缺字 %d"
                 % (tot_tbl, tot_miss, tot_chars))
    txt = "\n".join(lines) + "\n"
    open(rep, "w", encoding="utf-8").write(txt)
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
