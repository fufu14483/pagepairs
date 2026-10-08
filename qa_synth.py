# -*- coding: utf-8 -*-
"""QA for composited same-page dual: do synthesized YaHei CN lines collide with anything?

目录/行内就地替换（中文写在源行"标题—页码"之间的点导引区，且与源行同基线）
是设计好的同位对照，不算碰撞。判定不看几何相似度，直接读 geom 侧车里的
inline_dst（就地译文在输出页上的实际盒子），命中即豁免 —— 精确、不误伤。"""
import sys, os, json
import pymupdf as fitz

def cjk(s): return any('\u4e00' <= c <= '\u9fff' for c in s)
SYNFONT = "yahei"


def grey_col(col):
    """浅灰文字 = 底纹/水印类装饰（不是正文，不该参与压字判定）"""
    try:
        c = int(col)
    except (TypeError, ValueError):
        return False
    r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
    return abs(r - g) <= 8 and abs(g - b) <= 8 and 0x40 <= r <= 0xC8


def load_inline(out_f):
    """每页的就地译文盒子（输出坐标），略微外扩以吸收字形升部"""
    gf = out_f + ".geom.json"
    if not os.path.exists(gf):
        return {}
    try:
        geo = json.load(open(gf, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for i, g in enumerate(geo):
        if not isinstance(g, dict):
            continue
        boxes = []
        for b in (g.get("inline_dst") or []):
            try:
                boxes.append(fitz.Rect(b[0] - 1.5, b[1] - 1.5,
                                       b[2] + 1.5, b[3] + 1.5))
            except Exception:
                pass
        out[i] = boxes
    return out


def main():
  doc = fitz.open(sys.argv[1])
  src = fitz.open(sys.argv[2]) if len(sys.argv) > 2 else None
  inl = load_inline(sys.argv[1])
  coll = []
  n_syn = 0
  for pi, p in enumerate(doc):
      lines = []
      for b in p.get_text("dict")["blocks"]:
          if b.get("type") != 0: continue
          for l in b["lines"]:
              t = "".join(s["text"] for s in l["spans"])
              if t.strip():
                  syn = any(SYNFONT in s["font"].lower() for s in l["spans"])
                  ss = [s for s in l["spans"] if s["text"].strip()]
                  lines.append((fitz.Rect(l["bbox"]), t, syn,
                                max((s["size"] for s in ss), default=9.0),
                                any(grey_col(s.get("color", 0)) for s in ss)))
      ibox = inl.get(pi, [])
      for r1, t1, s1, _z1, _g1 in lines:
          if not s1: continue
          n_syn += 1
          cx = (r1.x0 + r1.x1) / 2.0
          cy = (r1.y0 + r1.y1) / 2.0
          if any(bx.x0 <= cx <= bx.x1 and bx.y0 <= cy <= bx.y1
                 for bx in ibox):
              continue                     # 就地对照行，设计如此
          for r2, t2, s2, z2, g2 in lines:
              if r2 is r1: continue
              if r2.contains(r1): continue          # containment of own line, fine
              # 斜排/水印装饰行：行框斜跨大半页、行高远大于字号，或浅灰底纹字
              # —— 既不是会被压的内容，也不该让落进它斜框/残片里的中文误报。
              if r2.height > 2.6 * max(4.0, z2) or g2: continue
              if s2 and abs(r1.x0 - r2.x0) < 1.2 and abs(r1.y0 - r2.y0) > 2 \
                      and abs(r1.y0 - r2.y0) < r1.height + 2:
                  continue                          # same-column CN wrap: stacked by design
              ix = fitz.Rect(r1); ix.intersect(r2)
              if not ix.is_valid or ix.is_empty: continue
              frac = ix.get_area() / max(1e-6, min(r1.get_area(), r2.get_area()))
              if frac > 0.30:
                  coll.append((pi + 1, t1[:30], t2[:30], round(frac, 2)))
  print("synthesized CN lines:", n_syn)
  print("collisions:", len(coll))
  for c in coll[:20]:
      print("  p%d  [%s]  x  [%s]  ov=%s" % c)
  if src:
      pi = min(1, len(doc) - 1, len(src) - 1)
      print("pages:", len(doc), "vs src", len(src),
            "| sizes:", doc[pi].rect, doc[pi].rect == src[pi].rect)
  return len(coll)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
