# -*- coding: utf-8 -*-
"""同页对照「阅读质量」审查器。

与既有五道质检互补（判据全文见《同页对照评判标准.md》）：
  既有：qa_tears      A1 撕裂（格线/图形被切断）
        qa_synth      A2 压字（中文压住任何东西）
        qa_layout     B1 落点过远 / B2 顺序 / B3 页末堆积 / C1 整表复刻超页高
        audit_px      F4 像素差（内容丢失）
        audit_cn      F1 中文覆盖率
  本器补：
        A3 越界          CN 墨迹出正文窗/出页
        C2 表格空洞率    CN 复刻表的空格比例（信息缺失）
        C3 表格列数      CN 复刻表列数与同区源表列数是否一致
        D1 尾页家具      CN 落进源页页尾标题栏/修订表（模板家具不该译）
        E1 字号下限      CN 字号过小
        F2 乱码          控制字符/替换符残留
        F3 重复长文本    同一页同一长句渲染多次
        F5 疑似漏译      源页成句英文下方整页无中文（弱网）
        F6 残留英文      译文里混着未译英文（上游 mono 的问题）

用法: audit_read.py <src.pdf> <out.pdf>
输出: 每行 `P<sev> <code> p<page> <说明>`；末行 `SUMMARY P0=n P1=n P2=n`
退出码: 有 P0 → 1，否则 0
"""
import sys
import os
import re
import json
import collections
import pymupdf as fitz

CNF = "yahei"
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ufffd]")
# 需要翻译的"成句英文"：≥3 个含字母的词、≥18 字符、字母占比 ≥45%
_WORDY = re.compile(r"[A-Za-z][A-Za-z\-']{1,}")


def is_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def cn_lines(page):
    """(rect, text, maxsize) —— 合成中文字体（雅黑）的行"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            if any(CNF in s["font"].lower() for s in l["spans"]):
                t = "".join(s["text"] for s in l["spans"]).strip()
                if t:
                    out.append((fitz.Rect(l["bbox"]), t,
                                max(float(s["size"]) for s in l["spans"])))
    return out


def en_lines(page):
    """(rect, text, color) —— 非合成字体的行（源页英文 + 受控水印）"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            if any(CNF in s["font"].lower() for s in l["spans"]):
                continue
            t = "".join(s["text"] for s in l["spans"]).strip()
            if t:
                out.append((fitz.Rect(l["bbox"]), t,
                            int(l["spans"][0].get("color", 0))))
    return out


def is_red(col):
    return ((col >> 16) & 0xFF) >= 0x90 and ((col >> 8) & 0xFF) <= 0x70 \
        and (col & 0xFF) <= 0x70


def usable_words(t):
    return [w for w in _WORDY.findall(t) if len(w) >= 2]


def rules(page, minlen=18.0):
    """(横线 y 列表, 竖线 (x, y0, y1) 列表)"""
    hs, vs = [], []
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
            elif it[0] == "re":
                r0 = it[1]
                p1, p2 = fitz.Point(r0.x0, r0.y0), fitz.Point(r0.x1, r0.y1)
            else:
                continue
            R = fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y),
                          max(p1.x, p2.x), max(p1.y, p2.y))
            if R.height <= 2.5 and R.width >= minlen:
                hs.append(((R.y0 + R.y1) / 2, R.x0, R.x1))
            elif R.width <= 2.5 and R.height >= minlen:
                vs.append(((R.x0 + R.x1) / 2, R.y0, R.y1))
    return hs, vs


def table_bands(hs, vs, src_h=1e9):
    """把横线按"x 范围基本一致 + y 相邻"聚成表带，再取跨越多行的竖线作列。

    返回 [(y0, y1, x0, x1, [列 x], [行 y])]
    """
    hs = sorted(hs, key=lambda z: z[0])
    used = [False] * len(hs)
    bands = []
    for i, (y, x0, x1) in enumerate(hs):
        if used[i]:
            continue
        grp = [(y, x0, x1)]
        used[i] = True
        for j in range(i + 1, len(hs)):
            if used[j]:
                continue
            y2, a2, b2 = hs[j]
            if y2 - grp[-1][0] > 34.0:
                break
            # x 范围必须与**首条**基本一致（±6pt）：只按 60% 重叠聚，
            # 会把整页多张不同宽度的表并成一条超长表带
            # （DOC-B02 p7 实测：并成 46 行 × 56 列 = 11 万格）
            if abs(a2 - x0) > 6.0 or abs(b2 - x1) > 6.0:
                continue
            grp.append((y2, a2, b2))
            used[j] = True
        if not (3 <= len(grp) <= 32):
            continue
        ys = [z[0] for z in grp]
        bx0 = min(z[1] for z in grp)
        bx1 = max(z[2] for z in grp)
        if ys[-1] - ys[0] > 0.92 * src_h:
            continue
        cols = [v[0] for v in vs
                if bx0 - 2 <= v[0] <= bx1 + 2
                and v[2] >= ys[0] - 2 and v[1] <= ys[-1] + 2]
        cols = sorted(set(round(c, 1) for c in cols))
        if not (2 <= len(cols) <= 16):
            continue
        bands.append((ys[0], ys[-1], bx0, bx1, cols, ys))
    return bands


def main():
    src_f, out_f = sys.argv[1], sys.argv[2]
    mono_f = sys.argv[3] if len(sys.argv) > 3 and os.path.exists(sys.argv[3]) else None
    src, out = fitz.open(src_f), fitz.open(out_f)
    mono = fitz.open(mono_f) if mono_f else None
    try:
        geom = json.load(open(out_f + ".geom.json", encoding="utf-8"))
    except Exception:
        geom = []
    hits = []

    def add(sev, code, pno, msg):
        hits.append((sev, code, pno, msg))

    for pno in range(min(len(src), len(out))):
        sp, op = src[pno], out[pno]
        H = sp.rect.height
        g = geom[pno] if pno < len(geom) and isinstance(geom[pno], dict) else {}
        bx0, bx1 = (g.get("window") or [0.0, op.rect.width])[:2]
        cns, ens = cn_lines(op), en_lines(op)

        # ---- A3 越界：CN 墨迹出正文窗 / 出页 ----
        # 分档：出页或越窗 >12pt → P0；6~12pt → P1（首字悬挂/项目符号的
        # 正常微出界不算问题，DOC-B16 实测只有 0.5pt 越界却报 P0）。
        for r, t, _z in cns:
            if (r.x0 < -1 or r.x1 > op.rect.width + 1
                    or r.y1 > op.rect.height + 1):
                add(0, "A3", pno, "CN 出页 x=%.1f..%.1f %r"
                    % (r.x0, r.x1, t[:22]))
                continue
            ov = max(bx0 - r.x0, r.x1 - bx1, 0.0)
            if ov > 12.0:
                add(0, "A3", pno, "CN 越窗 %.1fpt x=%.1f..%.1f (窗 %.1f..%.1f) %r"
                    % (ov, r.x0, r.x1, bx0, bx1, t[:20]))
            elif ov > 6.0:
                add(1, "A3", pno, "CN 微越窗 %.1fpt %r" % (ov, t[:20]))

        # ---- E1 字号下限 ----
        for r, t, z in cns:
            if z < 4.4:
                add(1, "E1", pno, "字号 %.2fpt < 4.4 %r" % (z, t[:22]))

        # ---- E2 页高失控（提示） ----
        if op.rect.height > 3.0 * H:
            add(2, "E2", pno, "页高 %.0f = 源页 %.0f 的 %.1f 倍"
                % (op.rect.height, H, op.rect.height / max(1.0, H)))

        # ---- F2 乱码 / 占位符 ----
        for r, t, _z in cns:
            if _CTRL.search(t) or "\\\\\\\\" in t:
                add(0, "F2", pno, "乱码/占位符 %r" % t[:26])

        # ---- F3 重复长文本（**以翻译稿为基准**：超出 mono 的次数才算我们重复） ----
        # 源文档本身常有成表重复（DOC-A05 p3 的定义表里 4 个型号共用同一段
        # 描述文字，源页就有 4 份）—— 那种重复是忠实的，不是缺陷。
        _sq = lambda z: re.sub(r"[\s\u00a0]+", "", z)
        dup = collections.Counter(_sq(t) for _r, t, _z in cns
                                  if len(_sq(t)) >= 26)
        mcnt = collections.Counter()
        if mono is not None and pno < len(mono):
            for _r2, _t2, _z2 in cn_lines(mono[pno]):
                if len(_sq(_t2)) >= 26:
                    mcnt[_sq(_t2)] += 1
        for k, cnt in dup.items():
            if cnt >= 2 and cnt > mcnt.get(k, 0):
                add(2, "F3", pno, "同页重复 %d 次（翻译稿 %d 次）%r"
                    % (cnt, mcnt.get(k, 0), k[:26]))

        # ---- C2 表格信息缺失（读引擎记账；几何反推在交错版式上不可靠） ----
        # geom.tbl_fid 每张渲染表一条 [R, C, 中文格数, 源侧格数, 总格数]。
        # 判据：源侧填充 ≥4 格且总格 ≥6 时，中文填充不得低于源侧的 80%
        # ——「信息大量缺失」= 源表有内容的格子，中文复刻里空着。
        src_h = float(g.get("src_h") or H)
        for fid in (g.get("tbl_fid") or []):
            try:
                R, C, cf, sf, tot = (int(fid[0]), int(fid[1]), int(fid[2]),
                                     int(fid[3]), int(fid[4]))
            except (IndexError, TypeError, ValueError):
                continue
            if tot < 6 or sf < 4:
                continue
            if cf < 0.80 * min(sf, tot):
                add(0 if cf < 0.60 * min(sf, tot) else 1, "C2", pno,
                    "表格信息缺失：中文 %d/%d 格，源侧有内容 %d 格（%dx%d）"
                    % (cf, tot, sf, R, C))

        # ---- D1 尾页标题栏/修订表被译 ----
        # 输出页是**分段下移的重排**：源页 y 与输出 y 不同基准。用 geom 的
        # `bands`（源页→输出 的完整分块映射）把源页页尾家具带投影到输出坐标，
        # 再判译文是否落进去（竖直包含 ≥60% + x 沾得到，同引擎 `_in_tail`）。
        tb = g.get("tail_band")
        bands = g.get("bands") or []

        def _s2o(y, xa=None, xb=None):
            """源页 y → 输出 y。取**含 y、与 [xa,xb] 横向重叠、且高度最小**的
            分块 —— 页边条带（src y 跨整页）会命中任意 y，必须排除。"""
            best = None
            for b in bands:
                try:
                    sx0, sy0, sx1, sy1 = (float(b[0][0]), float(b[0][1]),
                                          float(b[0][2]), float(b[0][3]))
                    dy0 = float(b[1][1])
                except (IndexError, TypeError, ValueError):
                    continue
                if not (sy0 <= y <= sy1):
                    continue
                if xa is not None:
                    if min(sx1, xb) - max(sx0, xa) < 0.5 * max(
                            1.0, min(sx1 - sx0, xb - xa)):
                        continue
                h = sy1 - sy0
                if best is None or h < best[0]:
                    best = (h, dy0 + (y - sy0))
            return best[1] if best else y

        if tb and len(tb) >= 3 and bands:
            ty0, tx0, tx1 = float(tb[0]), float(tb[1]), float(tb[2])
            fy0, fy1 = _s2o(ty0, tx0, tx1), _s2o(src_h, tx0, tx1)
            if abs(fy0 - ty0) < 0.5 and abs(fy1 - src_h) < 0.5 \
                    and len(bands) > 3:
                # 映射没生效（多半命中了整页高的页边条带）→ 放弃本页判定
                fy0 = fy1 = None
            if fy0 is None:
                continue

            # 把源页页尾家具的**实际文字字形**（y >= ty0）投影到输出坐标。
            # 只有译文矩形**真的压到某个页尾家具字形**才判 D1 —— 否则会把
            # "表最后一行因重排下移、恰好落在页尾带投影区"误判（DOC-A06 p4
            # 的箭头行：它本就是表尾最后一行，与源布局一致，只是行向合并改变
            # 行高让它下移进投影带；它与页脚文字 'Page/Format/4' 之间有 13pt
            # 间距、且位于列 4 的空隙，并非真的进了页尾标题栏）。
            def _srect(sr):
                sx0, sy0, sx1, sy1 = sr
                best = None
                for b in bands:
                    try:
                        bx0, by0, bx1, by1 = b[0]
                        ox0, oy0, ox1, oy1 = b[1]
                    except (IndexError, TypeError, ValueError):
                        continue
                    if not (by0 - 1.0 <= sy0 and sy1 <= by1 + 1.0):
                        continue
                    h = by1 - by0
                    if best is None or h < best[0]:
                        sc = (ox1 - ox0) / max(1e-6, bx1 - bx0)
                        best = (h, ox0 + (sx0 - bx0) * sc,
                                oy0 + (sy0 - by0),
                                ox0 + (sx1 - bx0) * sc,
                                oy0 + (sy1 - by0))
                return fitz.Rect(best[1], best[2], best[3], best[4]) if best \
                    else None

            _foot = []
            for _b in sp.get_text("dict")["blocks"]:
                if _b.get("type") != 0:
                    continue
                for _l in _b["lines"]:
                    for _s in _l["spans"]:
                        if _s["bbox"][1] < ty0 - 2.0:
                            continue
                        _r = _srect(fitz.Rect(_s["bbox"]))
                        if _r:
                            _foot.append(_r)
            for r, t, _z in cns:
                cy = (r.y0 + r.y1) / 2.0
                if not (fy0 - 2.0 <= cy <= fy1 + 2.0):
                    continue
                if (min(r.x1, tx1) - max(r.x0, tx0)) <= -8.0:
                    continue
                if any(r.intersects(fr) for fr in _foot):
                    add(0, "D1", pno, "译文进了页尾标题栏 %r" % t[:22])

        # ---- F5 疑似漏译：成句英文附近找不到中文 ----
        # 源页底部的英文，其译文可能被排到输出页的"追加区"（y > src_h），
        # 这时不能按 ±46pt 的邻域判，凡追加区内有同主题中文即视为已译。
        tail_cn = [r for r, _t, _z in cns if r.y0 > src_h - 30.0]

        def _o2s(y):
            """输出 y → 源页 y（bands 的逆向映射）。"""
            best = None
            for b in bands:
                try:
                    sy0, sy1 = float(b[0][1]), float(b[0][3])
                    dy0, dy1 = float(b[1][1]), float(b[1][3])
                except (IndexError, TypeError, ValueError):
                    continue
                if dy0 <= y <= dy1:
                    h = dy1 - dy0
                    if best is None or h < best[0]:
                        best = (h, sy0 + (y - dy0))
            return best[1] if best else y

        # 源页表带（输出坐标）——英文表**有意保留**，其译文复刻在别处，
        # 不能按"附近没中文"判漏译
        tb_x = None
        if tb and len(tb) >= 3 and bands:
            tb_x = (float(tb[1]), float(tb[2]))
        # 源页的表带（用**源页**规则检测，再经 bands 映射到输出坐标）——
        # 输出页混着中文表的格线，直接在输出页检测会漏
        _shs, _svs = rules(sp)
        bands_tbl = [(_s2o(y0, tx0_, tx1_), _s2o(y1, tx0_, tx1_), tx0_, tx1_,
                      _c, _ys)
                     for (y0, y1, tx0_, tx1_, _c, _ys)
                     in table_bands(_shs, _svs, H)]
        if not bands_tbl:
            bands_tbl = []
        miss = []
        for r, t, col in ens:
            if is_red(col) or r.y1 < 34 or r.y0 > H - 58:
                continue                    # 受控水印 / 页眉页脚
            _sy = _o2s((r.y0 + r.y1) / 2.0)
            if (tb_x and bands and tb_x[0] - 3 <= r.x0
                    and r.x1 <= tb_x[1] + 3 and _sy >= float(tb[0]) - 2):
                continue                    # 页尾标题栏/修订表：设计为不译
            if any(y0 - 1 <= (r.y0 + r.y1) / 2 <= y1 + 1
                   and tx0 - 3 <= r.x0 and r.x1 <= tx1 + 3
                   for (y0, y1, tx0, tx1, _c, _ys) in bands_tbl):
                continue                    # 英文表的格：其译文复刻在别处
            if len(t) < 18 or len(usable_words(t)) < 3:
                continue
            letters = sum(1 for c in t if c.isascii() and c.isalpha())
            if letters < 0.45 * len(t):
                continue
            if is_cjk(t):
                continue
            # **弱网**：译文常被放到该段之后的第一条切带处，源表越大偏移越远
            # （DOC-A06 p5 的 39 行大表：译文在源行下方 450pt 的追加区里）。
            # 只问"该行下方本页还有没有同 x 范围的中文"，因此只能抓"整页/整节
            # 无译文"这种量级的问题，不作 P1 计（降为 P2 提示）。
            near = any(r2.x0 < r.x1 + 6 and r2.x1 > r.x0 - 6
                       and r.y0 - 14 <= r2.y1 for r2, _t2, _z2 in cns)
            if not near and r.y0 > H - 140.0 and tail_cn:
                near = True                 # 页底英文 → 追加区承载其译文
            if not near:
                miss.append(t)
        if miss:
            add(2, "F5", pno, "该行下方整页无中文 %d 行，如 %r"
                % (len(miss), miss[0][:40]))

        # ---- F6 译文里残留未译英文（上游翻译稿的问题，非本工具缺陷） ----
        # DOC-A06 p5 实测：中文表里 "Row5/Row6/Row7"、"2nd 位置指示器行"、
        # "1st 指示灯" —— mono 本身没译全，我们只是忠实复刻。列出来供决策。
        _en_words = {
            "row", "rows", "lamp", "bit", "bits", "name", "value", "values",
            "top", "bottom", "left", "right", "first", "second", "third",
            "no", "yes", "on", "off", "open", "closed", "used", "not", "all",
            "each", "from", "with", "without", "and", "the", "for", "of",
            "indicator", "position", "description", "remark", "note", "notes",
            "type", "version", "date", "page", "format", "lang", "input",
            "output", "switch", "button", "colour", "color", "state",
        }
        _ord = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I)
        # 词边界：前后不能是字母/数字/下划线/点/斜杠/连字符的一部分 —— 否则
        # "lay_top.gdo" 里的 "top"、"xxx.gdo" 的 "gdo" 会假报（实测 F6 假报
        # 里绝大多数是文件名）；但允许**后面跟数字**（"Lamp1" 要能抓到）
        _lw = re.compile(r"(?<![A-Za-z0-9_./\-])([A-Za-z][A-Za-z]{2,})"
                         r"(?![A-Za-z_./\-])")
        for r, t, _z in cns:
            if not is_cjk(t):
                continue
            toks = _lw.findall(t)
            bad = [w for w in toks if w.lower() in _en_words]
            if bad or _ord.search(t):
                add(2, "F6", pno, "译文残留英文 %r（%s）"
                    % (t[:26], ",".join((bad or [])[:3]) or "序数"))

    p0 = sum(1 for h in hits if h[0] == 0)
    p1 = sum(1 for h in hits if h[0] == 1)
    p2 = sum(1 for h in hits if h[0] == 2)
    for sev, code, pno, msg in hits[:80]:
        print("P%d %-3s p%-3d %s" % (sev, code, pno + 1, msg))
    if len(hits) > 80:
        print("... 另有 %d 条" % (len(hits) - 80))
    print("SUMMARY P0=%d P1=%d P2=%d" % (p0, p1, p2))
    return 1 if p0 else 0


if __name__ == "__main__":
    sys.exit(main())
