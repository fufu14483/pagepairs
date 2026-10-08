# -*- coding: utf-8 -*-
"""Detect graphics/text torn by band cuts or side strips in the reflowed PDF.
Uses the source page + geom.json (window, cuts, rails)."""
import sys, json, pymupdf as fitz


def main():
  src_f, out_f = sys.argv[1], sys.argv[2]
  geom = json.load(open(out_f + ".geom.json", encoding="utf-8"))
  src = fitz.open(src_f)
  tot = 0
  for pno, g in enumerate(geom):
      W = src[pno].rect.width
      H = src[pno].rect.height
      bx0, bx1 = g.get("window", [0, W])
      cuts = [c for c in g.get("cuts", []) if 0.5 < c < H - 0.4]
      rails = g.get("rails", [])
      hrules = g.get("hrules", [])
      issues = []
      # -- drawings / images
      items = []
      for x in src[pno].get_drawings():
          r = fitz.Rect(x["rect"])
          if not r.is_valid:
              continue
          if r.is_empty:
              # 单线格线：get_drawings 给的是零宽/零高的 "l" 图元，is_empty 一过滤
              # 整条就消失 —— 结构竖线被切线切断也就永远查不出来（旧版正是这样
              # "假通过"的：DOC-A08 的标题栏、DOC-B05 的表格都有 10+ 条
              # ≥25pt 的竖线被切，撕裂却报 0）。按描边宽度膨胀后再判，与引擎
              # 切带障碍物的口径一致。
              sw = max(float(x.get("width") or 0.0), 0.35) * 0.5 + 0.25
              r = fitz.Rect(r.x0 - sw, r.y0 - sw, r.x1 + sw, r.y1 + sw)
          if r.get_area() < 1.5 or r.width > W - 3 or r.height > H - 3:
              continue
          items.append((r, "draw"))
      try:
          for im in src[pno].get_images(full=True):
              items += [(fitz.Rect(r), "img") for r in src[pno].get_image_rects(im[0])]
      except Exception:
          pass
      for r, kind in items:
          if r.get_area() < 1.5 or r.width > W - 3 or r.height > H - 3:
              continue
          israil = (r.width <= 4.5 and r.height >= 0.5 * H
                    and any(abs((r.x0 + r.x1) / 2 - x) < 2 for x in rails))
          if r.height <= 3.0 and r.width >= 30:          # horizontal rule
              # 整宽横向模板框线（height<=3, width>=0.5W）：与 rails 竖线对称，
              # 引擎已在 build 段跨窗口重画整宽（见 synth_reflow hrules），故像
              # rails 一样豁免 HRULE-SPLIT，避免"边框模板"页误报（DOC-A09）。
              ishrule = (r.width >= 0.5 * W
                         and any(abs((r.y0 + r.y1) / 2 - y) < 2 for y in hrules))
              if ishrule:
                  continue
              for xe in (bx0, bx1):
                  if r.x0 + 0.2 < xe < r.x1 - 0.2:
                      issues.append(("HRULE-SPLIT", kind, r, f"edge {xe:.1f}"))
                      break
          if r.width <= 3.0 and r.height >= 25 and not israil:
              for c in cuts:
                  if r.y0 < c - 0.4 < r.y1 and r.x0 >= bx0 - 0.6 and r.x1 <= bx1 + 0.6:
                      issues.append(("VCUT", kind, r, f"cut {c:.1f}"))
                      break
              else:
                  # NEARBEHIND 仅 overlay 模式有意义：reflow 下源图作为底图层
                  # 原样保留（输出页宽=整页 W，不裁边条），落在 [0,bx0]/[bx1,W]
                  # 边条里的源竖线（如边框模板左/右页边线）原样存在，并非被切带
                  # 撕掉；此处照报会误判（DOC-A09 p1 左页边线）。overlay 模式
                  # 才用边条压住源图、才需要这条告警。VCUT（竖线被切带切断）
                  # 在两种模式都成立，保留。
                  if g.get("overlay"):
                      if r.x1 <= bx0 and r.x0 > bx0 - 10 and r.y0 < H - 40:
                          issues.append(("NEARBEHIND-L", kind, r, ""))
                      elif r.x0 >= bx1 and r.x1 < bx1 + 10 and r.y0 < H - 40:
                          issues.append(("NEARBEHIND-R", kind, r, ""))
      # -- horizontal text lines crossing the window edges
      for b in src[pno].get_text("dict")["blocks"]:
          if b.get("type") != 0:
              continue
          for l in b["lines"]:
              lr = fitz.Rect(l["bbox"])
              t = "".join(s["text"] for s in l["spans"]).strip()
              if lr.height > 3 * max(1.0, lr.width):
                  continue                                # rotated
              if not lr.is_valid or lr.is_empty or lr.width < 3 or not t:
                  continue
              if lr.x0 + 0.2 < bx0 < lr.x1 - 0.2 or lr.x0 + 0.2 < bx1 < lr.x1 - 0.2:
                  issues.append(("TEXT-SPLIT", t[:22], lr, ""))
              elif (bx1 + 0.3 < lr.x0 < bx1 + 10 or
                    bx0 - 10 < lr.x1 < bx0 - 0.3) and 34 < lr.y0 < H - 58:
                  issues.append(("TEXT-BEHIND", t[:22], lr, ""))
      if issues:
          tot += len(issues)
          print(f"p{pno+1} ({len(issues)}):")
          for it in issues[:6]:
              k = it[0]
              r = it[2] if k.startswith(("HRULE", "VCUT", "NEAR")) else it[2]
              print("   ", k, it[1], [round(v, 1) for v in r], it[3])
  print("TOTAL tear issues:", tot)
  return tot


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
