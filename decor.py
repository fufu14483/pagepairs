# -*- coding: utf-8 -*-
"""背景装饰层（斜排文字 / 水印）的"摘出 → 重画"支持。

问题背景
--------
水印/斜排大字的行框（bbox）斜跨大半页，在本引擎的横带模型里两头不讨好：

* 当作切带障碍 → `safe_cut` 被它一路顶到页尾，几十个正文块全折进同一条带、
  中文被堆到页面另一头（撕裂/碰撞/像素三条质检还全是 0）；
* 排除出障碍 → 切线就会从它身上切过去，**每切一刀，它在那一带里的那一段就
  跟着该带平移**，页面上于是出现一叠错位的碎片 —— 就是"水印被截断"。

对策
----
把它从"参与切带的源内容"里整个摘出去，切带结束后再**原样整条重画一次**，
位置就是它原本在纸面上的位置。三个部件：

* `collect(page)`：用 `get_texttrace` 找出斜排文字行，取文本、字号、颜色、
  透明度、方向、逐字原点与 bbox，并抽取内嵌字体（子集化过的，很小）；
* `clean_page(doc, pno, decors)`：做一份单页副本，把该行的绘制块置为
  **不可见文字**（在 BT 后插 `3 Tr`、ET 前插 `0 Tr`，只加不删），然后用
  **文本抽取自校验**（装饰文字消失、其余文字一字不差）。校验不过返回 None，
  调用方退回原页 —— 宁可有碎片，绝不误删正文；
* `draw_items(page, items, srcpage)`：`TextWriter` + `morph`(旋转) + `opacity`
  原样重画（旋转矩阵取 `get_texttrace` 的方向角，PyMuPDF 的 `Matrix(deg)`
  与其屏幕方向符号相反：dir=(0.707,-0.707) 对应 `Matrix(45)`）。

`frag_rects` 给出"原页里被切碎的那些残片"在输出页上的位置，供 `audit_build`
抹白参考页 —— 参考页必须也能复現"整条水印"，像素审计才不会被这些残片误报。
"""
import math
import re
from collections import Counter

import pymupdf as fitz

# BT ... ET 文本块（非贪婪、跨行）
_BTET = re.compile(rb"BT\b.*?\bET", re.S)
_MAT = re.compile(rb"([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+"
                  rb"([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+(Tm|cm)")
_TF = re.compile(rb"/([^\s/]+)\s+([-+0-9.]+)\s+Tf")

_FONTS = {}


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _line_texts(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            t = _norm("".join(s["text"] for s in l["spans"]))
            if t:
                out.append(t)
    return out


def font_for(srcpage, name):
    """按名字从源页里取内嵌字体（子集）；取不到就退回内置中文字体。
    缓存键用 (文档, 名字) 并**连带持有文档引用** —— 只存 id 的话，文档被关闭后
    新文档可能复用到同一个 id，就会拿到上一个文档的字体。"""
    doc = srcpage.parent if srcpage is not None else None
    key = (id(doc), name)
    hit = _FONTS.get(key)
    if hit is not None:
        return hit[1]
    f = None
    if srcpage is not None and name:
        try:
            for xref, _ext, _typ, base, _res, _enc, _ref in srcpage.get_fonts(full=True):
                if name.split("+")[-1].lower() in (base or "").lower():
                    _n, _e, _s, buf = srcpage.parent.extract_font(xref)
                    if buf:
                        f = fitz.Font(fontbuffer=buf)
                    break
        except Exception:
            f = None
    if f is None:
        for fb in ("msyh", "china-s"):
            try:
                f = fitz.Font(fb)
                break
            except Exception:
                continue
    _FONTS[key] = (doc, f)
    return f


def collect(page):
    """找出该页的斜排装饰文字行。"""
    if page.rotation:
        return []                     # 页面带 /Rotate 时 trace 坐标与版面不一致，宁可不碰
    try:
        trace = page.get_texttrace()
    except Exception:
        return []
    out = []
    for sp in trace:
        try:
            dx, dy = sp["dir"]
        except Exception:
            continue
        if abs(dy) <= 0.02:           # 水平行不是装饰
            continue
        x0, y0, x1, y1 = sp["bbox"]
        size = float(sp.get("size") or 0.0)
        if size < 8.0 or (x1 - x0) < 40.0 or (y1 - y0) < 40.0:
            continue                  # 竖排侧栏窄条（宽 10 余 pt）不算
        chars = []
        for c in sp.get("chars") or []:
            try:
                chars.append(tuple(float(v) for v in c[3]))
            except Exception:
                pass
        text = "".join(chr(c[0]) for c in (sp.get("chars") or [])
                       if isinstance(c[0], int))
        if not text.strip() or not chars:
            continue
        col = sp.get("color") or (0, 0, 0)
        out.append({
            "text": text,
            "size": size,
            "origin": tuple(float(v) for v in sp["chars"][0][2]),
            "angle": -math.degrees(math.atan2(dy, dx)),
            "color": tuple(float(v) for v in col),
            "opacity": float(sp.get("opacity") or 1.0),
            "fontname": (sp.get("font") or "").split("+")[-1],
            "chars": chars,
        })
    return out


def _block_is_decor(blk, sizes):
    mats = _MAT.findall(blk)
    if not mats:
        return False
    rot = False
    for m in mats:
        b_, c_ = abs(float(m[1])), abs(float(m[2]))
        if b_ > 0.2 and c_ > 0.2:      # 旋转矩阵：副对角非零
            rot = True
            break
    if not rot:
        return False
    mf = _TF.search(blk)
    if mf:
        try:
            sz = float(mf.group(2))
        except ValueError:
            return False
        if not any(abs(sz - s) < 0.8 for s in sizes):
            return False
    return True


def clean_page(doc, pno, decors):
    """返回 (干净单页文档, 页号) —— 装饰文字已置为不可见；失败返回 None。"""
    if not decors:
        return None
    try:
        tmp = fitz.open()
        tmp.insert_pdf(doc, from_page=pno, to_page=pno)
        pg = tmp[0]
        sizes = [d["size"] for d in decors]
        hit = 0
        for xref in list(pg.get_contents()):
            raw = tmp.xref_stream(xref)      # bytes：正则也是 bytes 模式
            if not raw:
                continue
            parts, pos, changed = [], 0, False
            for m in _BTET.finditer(raw):
                blk = m.group(0)
                if not _block_is_decor(blk, sizes):
                    continue
                # 整个文本块置空：`3 Tr` 只挡渲染、文字仍可被抽取，自校验
                # 就分不清"藏起来了"和"没动"，所以直接清空该块（该块只含
                # 这一条斜排文字；若误伤别的文字，下面的抽取自校验会拦下）。
                parts.append(raw[pos:m.start()])
                parts.append(b"BT ET")
                pos = m.end()
                changed = True
                hit += 1
            parts.append(raw[pos:])
            if changed:
                tmp.update_stream(xref, b"".join(parts))
        if not hit:
            return None
        pg = tmp.reload_page(pg)
        before = Counter(_line_texts(doc[pno]))
        after = Counter(_line_texts(pg))
        want = set(_norm(d["text"]) for d in decors)
        gone = before - after
        extra = after - before
        if extra or not gone or not set(gone) <= want or not want <= set(gone):
            return None                # 校验不过：可能伤到别处，宁可退回原页
        return tmp, 0
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        return None


def draw_items(page, items, srcpage=None):
    """把装饰文字原样重画到 page 上。"""
    for it in items:
        try:
            f = font_for(srcpage, it.get("fontname"))
            ox, oy = it["origin"]
            tw = fitz.TextWriter(page.rect)
            tw.append(fitz.Point(ox, oy), it["text"], font=f,
                      fontsize=it["size"])
            tw.write_text(page, color=tuple(it.get("color") or (0, 0, 0)),
                          opacity=float(it.get("opacity", 1.0)),
                          morph=(fitz.Point(ox, oy),
                                 fitz.Matrix(float(it.get("angle", 0.0)))),
                          overlay=True)
        except Exception:
            continue
