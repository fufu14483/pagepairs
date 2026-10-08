# -*- coding: utf-8 -*-
"""audit stage 1: rebuild 'noCN' reference — same bands, no synthesized text."""
import sys, os, json
import pymupdf as fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decor as _decor          # noqa: E402  （背景装饰层的重画支持）


def main():
  src_f, out_f, ref_f = sys.argv[1], sys.argv[2], sys.argv[3]
  src = fitz.open(src_f)
  geom = json.load(open(out_f + ".geom.json", encoding="utf-8"))
  nd = fitz.open()
  for pno, g in enumerate(geom):
      bands = g["bands"] if isinstance(g, dict) else g
      W = src[pno].rect.width
      page = src[pno]
      # page height: from geom when known (pages with a page-bottom
      # appendix have taller output than any band dst), else derive
      H = max([d[3] for _, d in bands] +
              [float(g.get("page_h", 0.0)) if isinstance(g, dict) else 0.0])
      np_ = nd.new_page(width=W, height=H)
      # 背景装饰层（斜排水印）：成品是"把装饰文字从切带内容里摘出去、最后整条
      # 重画"。参考页必须走同一条路 —— 也拿"清洁页"（装饰文字已置空）来拼带，
      # 再按同一份规格重画。若改用"抹白原页碎片"，会连碎片底下的正文一起抹掉，
      # 反而把正常内容报成丢失。
      dec = g.get("decor") if isinstance(g, dict) else None
      gsrc, gpno = src, pno
      if dec and dec.get("items"):
          _c = _decor.clean_page(src, pno, _decor.collect(page))
          if _c is not None:
              gsrc, gpno = _c
      for srect, drect in bands:
          np_.show_pdf_page(fitz.Rect(drect), gsrc, gpno, clip=fitz.Rect(srect))
      # 目录就地替换会清掉被顶替的点导引，参考页必须同样抹白那一块，
      # 否则像素审计会把"有意删除的点线"误报成源内容丢失。
      for e in ((g.get("erased") or []) if isinstance(g, dict) else []):
          er = fitz.Rect(e)
          cy = (er.y0 + er.y1) / 2.0
          for srect, drect in bands:
              sr = fitz.Rect(srect)
              if sr.y0 - 0.5 <= cy <= sr.y1 + 0.5:
                  dx, dy = drect[0] - sr.x0, drect[1] - sr.y0
                  np_.draw_rect(fitz.Rect(er.x0 + dx, er.y0 + dy,
                                          er.x1 + dx, er.y1 + dy),
                                color=None, fill=(1, 1, 1), width=0)
                  break
      if dec and dec.get("items"):
          _decor.draw_items(np_, dec["items"], page)
  nd.save(ref_f, garbage=4, clean=True, deflate=True)
  print("ref pages:", len(nd))


if __name__ == "__main__":
    main()
