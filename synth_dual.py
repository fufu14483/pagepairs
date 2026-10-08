# -*- coding: utf-8 -*-
"""
同页对照合成器 v4（仿 Google 文档翻译版式）
- 原文 PDF 文本/图形不动；中文（蓝色）来自 BabelDOC 中文版；
- TOC 行：点导引符位置就地换成中文（红action 仅清除点线），页码保留；
- 等行数的表格/行块：按行穿插到行下方空隙；
- 正文段落：整段中文置于块下方空白；
- 已放置区/行矩形/图形/图片都是障碍物；冲突宁缺勿叠。

用法: python synth_dual.py <original.pdf> <mono.pdf> <output.pdf>
"""
import sys, re, os
import pymupdf as fitz

BLUE = (0, 0, 1)
MAX_SIZE, MIN_SIZE = 8.0, 4.2
LH = 1.12
DOTS = re.compile(r"^[.\s·…⋯\_\-]{4,}$")

def norm(s):
    s = re.sub(r"[\x00-\x08\x0b-\x1f\u0591-\u05f4\x7f]", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()

# ---- 符号字体私用区（U+E000–U+F8FF）→ 标准 Unicode ----
# 源页常用 Wingdings/Webdings/Symbol 画勾选框、圆点、箭头等标记；babeldoc
# 把这些码位原样带进译文，而我们的中文字体（雅黑/思源/DroidSansFallback）
# **没有私用区字形** → 渲染空白、文本提取成 \x00（DOC-A06 p2 的变体表整列
# ☑ 变空白实测，252 处）。按"已知映射表 + 可见兜底标记"换成标准码位。
SYM_MAP = {
    "\uf0fe": "\u2611",   # Wingdings 0xFE 勾选框 ☑（本项目实测唯一出现者）
}
SYM_FB = "\u25a0"          # 未收录的私用区字符 → 实心方块（可见，好过空白）


def sym2uni(s):
    if not s:
        return s
    if not any("\ue000" <= c <= "\uf8ff" for c in s):
        return s
    return "".join(SYM_MAP.get(c, SYM_FB) if "\ue000" <= c <= "\uf8ff" else c
                   for c in s)


def has_cjk(s):
    return any('\u4e00' <= c <= '\u9fff' for c in s)

def tokens(s):
    return set(t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-/]{1,}", s or ""))

def tok_sim(a, b):
    A, B = tokens(a), tokens(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, min(len(A), len(B)))

class FontPool:
    def __init__(self):
        self.fonts = []
        self.bold = []
        try:
            with open(r"C:\Windows\Fonts\msyh.ttc", "rb") as fh:
                f = fitz.Font(fontbuffer=fh.read())
            if all(f.has_glyph(ord(c)) for c in "电路转换器说明归档模板版本"):
                self.fonts.append(f)
        except Exception:
            pass
        try:
            with open(r"C:\Windows\Fonts\msyhbd.ttc", "rb") as fh:
                f = fitz.Font(fontbuffer=fh.read())
            if all(f.has_glyph(ord(c)) for c in "电路转换器说明归档模板版本"):
                self.bold.append(f)
        except Exception:
            pass
        self.fonts.append(fitz.Font("china-s"))
        self.bold.append(self.fonts[-1])
        self._cache = {}

    def font_for(self, ch, bold=False):
        key = (ch, bold)
        f = self._cache.get(key)
        if f is None:
            fam = self.bold if bold else self.fonts
            f = next((c for c in fam if c.has_glyph(ord(ch))), fam[-1])
            self._cache[key] = f
        return f

    def runs(self, text, bold=False):
        out = []
        for ch in text:
            f = self.font_for(ch, bold)
            if out and out[-1][0] is f:
                out[-1][1] += ch
            else:
                out.append([f, ch])
        return out

    def width(self, text, size, bold=False):
        return sum(f.text_length(s, fontsize=size) for f, s in self.runs(text, bold))

def wrap(pool, text, size, width, maxlines=None, bold=False):
    parts = re.findall(r"[A-Za-z0-9@.*_+\-/=&#%'\(\)\[\]:,;!?\"”“]+|\s+|[^\sA-Za-z0-9]", text)
    lines, cur, cw = [], "", 0.0
    for w in parts:
        ww = pool.width(w, size, bold)
        if cur and cw + ww > width:
            lines.append(cur.strip())
            if maxlines and len(lines) >= maxlines:
                return None
            cur, cw = w.lstrip(), pool.width(w.lstrip(), size, bold)
        else:
            cur += w
            cw += ww
    if cur.strip():
        lines.append(cur.strip())
    return lines

def get_blocks(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        txt = norm(" ".join("".join(s["text"] for s in ln["spans"]) for ln in b["lines"]))
        if not txt:
            continue
        lines = []
        for ln in b["lines"]:
            spans = []
            for s in ln["spans"]:
                st = s["text"]
                if st.strip():
                    spans.append({"rect": fitz.Rect(s["bbox"]), "text": st,
                                  "origin": tuple(s["origin"]),
                                  "size": float(s.get("size", 9.0)),
                                  # 受控水印/图章是红色文字；synth_reflow 靠它
                                  # 把红线从表格格子里剔出去（这里原来丢掉
                                  # color，红线判定恒 False —— DOC-A09 的
                                  # BOM 表头格里混进过"、泄露或转让给第三方"）。
                                  "color": int(s.get("color", 0))})
            t = norm("".join(s["text"] for s in ln["spans"]))
            if t:
                lines.append({"rect": fitz.Rect(ln["bbox"]), "text": t,
                              "spans": spans, "dir": tuple(ln.get("dir", (1.0, 0.0)))})
        out.append({"rect": fitz.Rect(b["bbox"]), "text": txt, "lines": lines})
    out.sort(key=lambda r: (round(r["rect"].y0 / 2.0), r["rect"].x0))
    return out

def align(ob, mb):
    n, m = len(ob), len(mb)
    NEG = -9.0
    def sc(i, j):
        ro, rm = ob[i]["rect"], mb[j]["rect"]
        pos = (max(0.0, 1 - abs((rm.y0 + rm.y1) / 2 - (ro.y0 + ro.y1) / 2) / 26.0) *
               max(0.3, 1 - abs((rm.x0 + rm.x1) / 2 - (ro.x0 + ro.x1) / 2) / 90.0))
        return min(1.6, pos + 0.7 * tok_sim(ob[i]["text"], mb[j]["text"]))
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] - 0.06; bt[i][0] = 1
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] - 0.06; bt[0][j] = 2
    for i in range(1, n + 1):
        row, prow = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            s = sc(i - 1, j - 1)
            a = prow[j - 1] + (s if s >= 0.5 else NEG)
            b = prow[j] - 0.06
            c = row[j - 1] - 0.06
            if a >= b and a >= c: row[j], bt[i][j] = a, 0
            elif b >= c: row[j], bt[i][j] = b, 1
            else: row[j], bt[i][j] = c, 2
    pairs, i, j = [], n, m
    while i > 0 or j > 0:
        step = bt[i][j] if i > 0 and j > 0 else (1 if i > 0 else 2)
        if step == 0 and (dp[i][j] - dp[i - 1][j - 1]) > NEG / 2:
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif step == 0: i -= 1; j -= 1
        elif step == 1: i -= 1
        else: j -= 1
    return pairs[::-1]

def limit_below(y1, x0, x1, obstacles, page_y1):
    """Top y limit for a zone starting just below y1. Returns None if an
    obstacle rect vertically straddles the zone start (block/row spanning)."""
    limit = page_y1 - 16
    for r in obstacles:
        if r.x1 <= x0 + 1 or r.x0 >= x1 - 1:
            continue
        if r.y0 >= y1 - 0.3:
            if r.y0 < limit:
                if y1 - r.y1 <= 0.6:      # own underline flush at bottom
                    continue
                limit = r.y0
        elif r.y1 > y1 + 0.5:            # straddler: crosses the zone top
            return None
    return limit

def plan_below(pool, cn, x0, y0, width, avail, single=False, min_size=MIN_SIZE):
    size = MAX_SIZE
    while size >= min_size:
        lines = wrap(pool, cn, size, width, maxlines=1 if single else None)
        if lines and (not single or len(lines) == 1):
            if len(lines) * size * LH <= avail + 0.6:
                items, y = [], y0 + size * 0.95
                for ln in lines:
                    items.append((x0, y, size, ln))
                    y += size * LH
                return items
        size -= 0.3
    return None

def item_clear(pool, item, obstacles):
    """True if the placement rect avoids all obstacles."""
    x, y, size, ln = item
    r = fitz.Rect(x, y - size * 0.95, x + pool.width(ln, size) + 0.5, y + size * 0.28)
    for o in obstacles:
        if o.is_empty or not o.intersects(r):
            continue
        ix = fitz.Rect(r)
        ix.intersect(o)
        if not ix.is_valid or ix.is_empty:
            continue
        smaller = min(r.get_area(), o.get_area() or 1e-9)
        if ix.get_area() > 0.25 * smaller:
            return False
    return True

NUM_PREFIX = re.compile(r"^(\d+(?:[.．]\d+)*)[.．]?\s*")

def seg_number(line_text):
    m = NUM_PREFIX.match(line_text)
    return m.group(1) if m else None

def split_cn_by_numbers(row_prefixes, cn_text):
    """row_prefixes: list[str] numeric keys; split cn_text into same-order segments."""
    if not row_prefixes or len(row_prefixes) < 2:
        return None
    marks, cursor = [], 0
    for n in row_prefixes:
        m = re.search(r"(?<![0-9.])" + re.escape(n) + r"(?![0-9])", cn_text[cursor:])
        if not m:
            return None
        marks.append(cursor + m.start())
        cursor += m.end()
    segs = []
    for k, st in enumerate(marks):
        en = marks[k + 1] if k + 1 < len(marks) else len(cn_text)
        segs.append(norm(cn_text[st:en]))
    return segs

def cluster_rows(lines):
    """group block lines into visual rows by y0"""
    rows = []
    for l in lines:
        y = l["rect"].y0
        if rows and abs(y - rows[-1][0]) <= 2.5:
            rows[-1][1].append(l)
            rows[-1][0] = (rows[-1][0] * (len(rows[-1][1]) - 1) + y) / len(rows[-1][1])
        else:
            rows.append([y, [l]])
    return [r[1] for r in rows]

DOTTAIL = re.compile(r"([.\uFF0E\u00B7\u2026\u22EF]{6,})(?:\s*\d{1,3})?\s*$")

# Arial 与 Helvetica 度量一致：用真实字宽把"标题 + 点导引"混合 span 里的
# 点线起点算准。旧写法按"字符数比例"内插，遇到标题字宽远大于句点宽度时，
# 会偏差十几到二十 pt —— 抹掉点线时就把标题末尾几个字母一起抹了。
_HELV = fitz.Font("helv")


def find_dot_span(row_lines):
    """Locate trailing leader-dot region inside title spans (dots usually share
    the span with title text and a final page number). Returns {'rect','origin'}."""
    best = None
    for l in row_lines:
        for s in l["spans"]:
            t = s["text"]
            r = s["rect"]
            cand = None
            if DOTS.match(t):
                cand = (r.x0, r.x1, s["origin"][1])
            else:
                m = DOTTAIL.search(t)
                if m and r.width > 1:
                    sz = float(s.get("size", 9.0))
                    pre, dot = t[:m.start(1)], t[:m.end(1)]
                    try:
                        dx0 = r.x0 + _HELV.text_length(pre, fontsize=sz)
                        dx1 = r.x0 + _HELV.text_length(dot, fontsize=sz)
                    except Exception:
                        dx0 = dx1 = 0.0
                    if (dx0 <= r.x0 or dx1 - dx0 < 0.05 * r.width
                            or has_cjk(pre)):
                        # 度量失败/前缀异常 → 退回旧的按字符数比例估算
                        dx0 = r.x0 + r.width * (m.start(1) / len(t))
                        dx1 = r.x0 + r.width * (m.end(1) / len(t))
                    if dx0 - r.x0 > 8:   # real text before the dots
                        cand = (dx0, dx1, s["origin"][1])
            if cand and cand[1] - cand[0] > 40:
                item = {"rect": fitz.Rect(cand[0], r.y0, cand[1], r.y1),
                        "origin": (cand[0] + 1.5, cand[2])}
                if best is None or item["rect"].width > best["rect"].width:
                    best = item
    return best

def clean_entry_cn(cn):
    cn = re.sub(r"^[.．]?\s*\d+(?:[.．]\d+)*[.．]?\s*", "", cn).strip()
    cn = re.sub(r"[.．]\s*\d+\s*$", "", cn).strip()
    return norm(cn)

def main():
    orig_f, mono_f, out_f = sys.argv[1], sys.argv[2], sys.argv[3]
    orig, mono = fitz.open(orig_f), fitz.open(mono_f)
    if len(orig) != len(mono):
        sys.exit(f"page mismatch {len(orig)} vs {len(mono)}")
    pool = FontPool()
    placed = skipped = 0
    skips = []
    for pno in range(len(orig)):
        page = orig[pno]
        ob, mb = get_blocks(page), get_blocks(mono[pno])
        pairs = align(ob, mb)
        src_line_rects = [l["rect"] for b in ob for l in b["lines"]]
        obs = [fitz.Rect(p["rect"]) for p in page.get_drawings()
               if p["rect"].is_valid and not p["rect"].is_empty]
        try:
            for im in page.get_images(full=True):
                obs += [fitz.Rect(r) for r in page.get_image_rects(im[0])]
        except Exception:
            pass
        placed_rects = []
        tw = fitz.TextWriter(page.rect)
        used = False
        skip_next = set()
        obstacles = src_line_rects + obs

        def emit(items):
            nonlocal used
            for x, y, size, ln in items:
                xw = x
                for f, s in pool.runs(ln):
                    tw.append((xw, y), s, font=f, fontsize=size)
                    xw += f.text_length(s, fontsize=size)
                placed_rects.append(fitz.Rect(x, y - size * 0.95, xw + 0.5, y + size * 0.28))
            used = True

        for oi, mi in pairs:
            src, cn_block = ob[oi], mb[mi]
            cn = cn_block["text"]
            rect = src["rect"]
            src_lines = src["lines"]
            line_texts = [l["text"] for l in src_lines]
            if not has_cjk(cn) or any(has_cjk(t) for t in line_texts):
                continue

            # ---------- A) TOC / dot-leader rows ----------
            rows = cluster_rows(src_lines)
            keyed = []  # (row_idx, numeric prefix)
            for ri, row in enumerate(rows):
                for l in sorted(row, key=lambda x: x["rect"].x0):
                    pn = seg_number(l["text"])
                    if pn:
                        keyed.append((ri, pn))
                        break
            dot_rows = [i for i, row in enumerate(rows) if find_dot_span(row)]
            if len(rows) >= 3 and len(dot_rows) >= 2 and len(keyed) >= max(2, len(rows) // 2):
                segs = split_cn_by_numbers([p for _, p in keyed], cn)
                if segs and len(segs) == len(keyed):
                    done_any = False
                    for (ri, _pref), seg in zip(keyed, segs):
                        cn2 = clean_entry_cn(seg)
                        if not cn2 or not has_cjk(cn2):
                            continue
                        sp = find_dot_span(rows[ri])
                        if not sp:
                            continue
                        w = sp["rect"].width - 3
                        size = MAX_SIZE
                        line = None
                        while size >= 5.0:
                            ls = wrap(pool, cn2, size, w, maxlines=1)
                            if ls:
                                line = ls[0]; break
                            size -= 0.3
                        if line is None:
                            continue
                        bx = sp["origin"][0]
                        by = sp["origin"][1] - size * 0.06
                        page.add_redact_annot(fitz.Rect(sp["rect"].x0, sp["rect"].y0 + 1,
                                                        bx + pool.width(line, size) + 0.5,
                                                        sp["rect"].y1 - 1))
                        items = [(bx, by, size, line)]
                        emit(items)
                        placed_rects.pop()  # 行内占位不挡后续（点区已清除）
                        done_any = True; placed += 1
                    if done_any:
                        continue

            # ---------- B) block placement below the block ----------
            ob_all = obstacles + placed_rects
            bx0 = rect.x0
            bx1 = min(max(rect.x1, bx0 + 60), page.rect.x1 - 12)
            by1 = rect.y1
            cands = [(bx0, bx1)]
            for o in ob_all:
                if o.y0 < by1 - 0.3 and o.y1 > by1 + 0.5 and o.x1 > bx0 + 20 and o.x0 < bx1 - 20:
                    cands.append((bx0, o.x0 - 1))
                    cands.append((o.x1 + 1, bx1))
            ytop = by1 + 1.2
            best = None
            for (a, b) in cands:
                if b - a < 60 or a < bx0 - 1 or b > page.rect.x1 - 10:
                    continue
                lim = limit_below(by1, a, b, ob_all, page.rect.y1)
                if lim is None or lim - ytop < 5.5:
                    continue
                items = plan_below(pool, cn, a, ytop, b - a, lim - ytop, min_size=5.0)
                if items and all(item_clear(pool, it, ob_all) for it in items):
                    score = (b - a) * (lim - ytop)
                    if best is None or score > best[0]:
                        best = (score, items)
            if best:
                emit(best[1]); placed += 1
            else:
                skipped += 1; skips.append((pno + 1, rect.y0, cn[:40]))

        if used:
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            except Exception:
                page.apply_redactions()
            tw.write_text(page, color=BLUE, overlay=True)
    orig.save(out_f, garbage=3, deflate=True)
    print(f"OK -> {out_f}  placed={placed} skipped={skipped} font={pool.fonts[0].name}")
    for pg, y, t in skips:
        print(f"   skip p{pg} y={y:.0f}: {t}")

if __name__ == "__main__":
    main()
