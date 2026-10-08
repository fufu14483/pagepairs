# -*- coding: utf-8 -*-
"""
扩展式重排合成器 —— 英文原文在上、蓝色中文紧跟其下，原文像素零丢失：
- 大标题/小标题黑体加粗、字号分层；正文常规字号；
- 表格重建：源页矢量格线 -> 真实行列网格，中文逐格填回并画出蓝色
  表格线（术语|说明两列表同样重建；稀疏格线的大行保持一个合并单元格，
  格内按原文显示行断行）；无格线可用时退回"聚类折段"；
- 页眉页脚、版权/保密条款、封面题录栏等模板性内容不插中文；
- TOC 逐行插入；原文一字不删，页宽不变，页高按插入量加长。

调试：SYNTH_DBG=<页号0基> 环境变量打印该页窗口/簇/单元/切带信息。

用法: python synth_reflow.py <original.pdf> <mono.pdf> <output.pdf>
"""
import sys, os, re, math
import unicodedata
from collections import Counter
import pymupdf as fitz
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# GBK consoles crash on CJK / non-breaking-hyphen glyphs the engine prints;
# force UTF-8 with replacement so a stray char can never abort a run.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
from synth_dual import (norm, has_cjk, align, get_blocks, cluster_rows,
                        sym2uni,
                        split_cn_by_numbers, seg_number, find_dot_span,
                        clean_entry_cn, FontPool, wrap)
import decor as _decor

BLUE = (0, 0, 1)
LH_BODY = 1.18

# self-heal level (0=off): 1 = post-placement collision prediction parks
# offending CN strips at page bottom; 2/3 = additionally widen every
# safe_cut protection band (escalation ladder used by the pipeline retry).
HEAL = int(os.environ.get("SYNTH_HEAL", "1"))

# 表格判定调试开关：SYNTH_TBLDBG=1 打印每张重建表的 R/C/表体"值"占比/判定
# 结果（调 table_is_datalist 的门槛时用它先看分布）。
TBLDBG = os.environ.get("SYNTH_TBLDBG", "0") == "1"


def _dbg_pages():
    """SYNTH_DBG=<页号0基>，也接受逗号列表（'0,2,5'）或 'all'"""
    raw = (os.environ.get("SYNTH_DBG") or "").strip().lower()
    if not raw:
        return frozenset()
    if raw == "all":
        return "all"
    out = set()
    for t in raw.replace(" ", "").split(","):
        try:
            out.add(int(t))
        except ValueError:
            pass
    return frozenset(out)


DBGPAGES = _dbg_pages()


def DBG(pno):
    return DBGPAGES == "all" or pno in DBGPAGES


def _grey_col(col):
    """浅灰文字 = 底纹/水印类装饰（不是正文，不该参与压字判定）"""
    try:
        c = int(col)
    except (TypeError, ValueError):
        return False
    r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
    return abs(r - g) <= 8 and abs(g - b) <= 8 and 0x40 <= r <= 0xC8


# ---- 「数据表/清单」 vs 「说明表格」 ------------------------------------
# 用户报（DOC-A09 BOM）：这类"明显是填数据的清单"整表译成中文后不可读 ——
# 31 行 × 6 列全变中文，长料号列（"C3,C10,C13,…"）被拆成一个字一行、中文
# 复刻表比源页还高（998pt），输出页高翻到 2.19 页；而"插在正文里的说明表格"
# （DOC-B02 的引脚/功能表）格子是自然语言，必须整表译。
# 判据只看**表体**（去掉第 0 行表头）：格子装的是"值"（数字、料号、规格串）
# 还是"话"。保守设计 —— 拿不准一律按说明表格整表译（维持旧行为）。
_W4 = re.compile(r"[A-Za-z]{4,}")
_SENT = re.compile(r"[.;:!?]\s+[A-Za-z]")
_CJK1 = re.compile(r"[\u4e00-\u9fff]")
_LC = re.compile(r"\b[a-z][a-z\-']+\b")   # 小写起首的英文词


def cell_is_value(t):
    r"""格子里装的是"值"（数字/料号/规格串）而不是一句话。

    **喂源页的英文格子文本**（不是 mono 的中文译文 —— 译文里"1st 位置指示器行"
    带 ordinal 数字、"表面贴装电容器 220nF…"反而像规格串，两边都会判反；
    源页英文里"值"与"话"的界限清楚得多）。判据（20 份工作区样张 + 5 份 skill
    老样张实测调出）：
      · 小写起首的英文词 ≥2 个（`\b[a-z][a-z\-']+\b`）→ 话。规格串/代号是
        "Title Case + 全大写缩写 + 数字"（"SMD Capacitor 220nF 10% 50V X7R
        0603"、"TOP-CA-RL1457DS-B3"、"Blank Board 47*182.5*1.6mm 4layer"），
        一个小写词都没有；自然语言必有连着的小写词（"Top row of position
        indicator"、"Rotate potentiometer manually"、"2 landing call button
        inputs and acknowledgement outputs"）。序数词 1st/31st 不会误判
        （"1st" 与数字连写、词边界不落在 s 前）。
      · 有句读且后面还接字母 → 话
      · 有数字 → 值；无数字且 <2 个 4 字母长词 → 值（代号 "I/O"、"n.c."）
    含中文的源格子（盖章/受控水印）不算"值"——由调用方剔除。
    """
    t = (t or "").strip()
    if not t:
        return True                      # 空格子不参与判定
    if _CJK1.search(t):
        return False
    if _SENT.search(t):
        return False                     # 句读后还接字母 → 像句子
    if len(_LC.findall(t)) >= 2:
        return False                     # 连着的小写词 = 自然语言
    if re.search(r"\d", t):
        return True                      # 带数字 = 规格/数量/料号/坐标
    return len(_W4.findall(t)) < 2       # ≥2 个长英文词 → 像句子


def table_is_datalist(cells, R, C, scells=None, min_body=8, min_ratio=0.90,
                      min_val_col=0.65):
    """表体绝大多数格子是"值"，且**没有一列**混进成句的话 → 数据表/清单。

    只看表体（第 0 行是表头：正是要译的"数据类型"）。判定优先用 **源页英文
    格子**（`scells`，_grid_from_rules 从源页行提取；译文格子的"值/话"界限
    被翻译搅浑 —— "1st 位置指示器行"带数字、"表面贴装电容器 220nF…"像规格），
    没有源侧格子时退回中文格子（保守）。
    门槛取得保守：
      min_body=8       体行数 <8 的小表（说明性插表）一律整表译
      min_ratio=0.90   表体"值"格占比兜底
      min_val_col=0.65 单列"值"占比，且**一列都不许低于它**。实测分布：
                       BOM 的 Description 列 0.76~0.97（"Stand-alone
                       Bluetooth 5 lowenergy module…" 这类规格串里夹着
                       小写词），而说明表格的"话"列 ≤0.42（DOC-A06 p5
                       位定义表的"含义"列 ≈0、p2 PCBA 对比矩阵的首列
                       0.42）—— 0.65 居中，两边各留 1.2~1.8 倍余量
                       （用户明确：技术描述里插的说明表格要整表译）。
    """
    if R - 1 < min_body or C < 3:
        return False
    src = scells if scells else cells
    body = [(k, v) for k, v in src.items() if k[0] >= 1]
    if len(body) < C:
        return False
    vals = sum(1 for _k, (txt, _sp) in body if cell_is_value(txt))
    if vals / float(len(body)) < min_ratio:
        return False
    col_tot, col_val = [0] * C, [0] * C
    for (r, ci), (txt, sp) in src.items():
        if r < 1:
            continue
        ok = cell_is_value(txt)
        for j in range(ci, min(C, ci + max(1, sp))):
            col_tot[j] += 1
            if ok:
                col_val[j] += 1
    for j in range(C):
        if col_tot[j] and col_val[j] / float(col_tot[j]) < min_val_col:
            return False                # 有一列是"话" → 说明表格
    return True


def _row0_is_header(cells):
    """第 0 行是不是"表头"（数据类型名）：全是短标签、没有数字。

    BOM 这类清单常**跨页**：续页（DOC-A09 p2/p3）的表从页顶直接续数据，
    第 0 行是 "D14 / 430282 / SERI BAV99 SOT" 这样的数据行 —— 那种表没有
    表头可译，整表不译（数据类型行只在清单第一页出现一次）。
    """
    tot = lab = 0
    for (r, _ci), (txt, _sp) in cells.items():
        if r != 0:
            continue
        tot += 1
        t = txt.strip()
        if not t:
            lab += 1
            continue
        if re.search(r"\d", t) or len(t) > 30:
            return False                # 有数字/太长 → 是数据行
        lab += 1
    return tot >= 2 and lab == tot


def tbl_header_only(tbl, pool=None, force=False):
    """清单表：只译表头那一行"数据类型"，表体数据行不译（用户需求）。

    表头**加一行**（用户 2026-09-28 二次反馈：不要盖掉英文数据类型，
    直接在英文下面紧贴一行中文）—— 英文原样保留，中文按"格线底 -
    英文墨迹底"的剩余高度收缩字号（下限 3.4pt，用户同意适当改字号）
    写在英文正下方，走 toc_inplace 加法通道（audit_px 对浅底蓝字豁免，
    无需抹白）。英文下面实在放不下才退回白带覆盖式，再不行退回
    "表格下方一行图例"的旧样式。续页（表从页顶直接续数据、无表头行）
    整表不译。

    返回 (新 tbl, 保留的表头文本, 被丢弃的中文文本列表, 就地替换条目)。
    没有子表被判为清单时返回 (None, None, [], [])。
    """
    if os.environ.get("DUAL_NO_HDRONLY", "0") == "1":
        return None, None, [], []
    drop, kept, out, hit, inline = [], [], [], 0, []
    for blk in tbl["blocks"]:
        if not (force or table_is_datalist(
                blk.get("_cells_pre") or blk["cells"], blk["R"], blk["C"],
                blk.get("_scells"))):
            out.append(blk)
            continue
        hit += 1
        if not _row0_is_header(blk["cells"]):
            for (_r, _c), (txt, _sp) in blk["cells"].items():
                if txt.strip():
                    drop.append(txt.strip())
            continue                    # 续页：无表头 → 整表不译
        for (r, _c), (txt, _sp) in blk["cells"].items():
            if not txt.strip():
                continue
            if r == 0:
                kept.append(txt.strip())
            else:
                drop.append(txt.strip())
        # —— 表头加一行：英文保留，中文紧贴英文墨迹之下（不盖）——
        geo = blk.get("_geo")
        sr = blk.get("_srects") or {}
        cells_ok = bool(geo) and pool is not None
        items = []
        for (r, ci), (txt, sp) in sorted(blk["cells"].items()):
            if r != 0 or not txt.strip():
                continue
            base = sr.get((0, ci))
            if base is None:
                cells_ok = False        # 没有源矩形 → 没法原位
                break
            rx0, ry0, rx1, ry1 = base
            cn = norm(txt.replace("\n", " "))
            if not has_cjk(cn):
                continue                # 本来就是代号（ID/Qty）→ 原样保留
            w = max(12.0, rx1 - rx0 - 3.0)
            est_b = ry1 - 0.28 * (ry1 - ry0)      # 英文基线估计（desc 占 28%）
            _top = est_b + 0.5                    # 中文行顶：英文基线之下
            _bot = geo[1] + 3.6                   # 可跨过格线用数据行上留白
            size = min(5.6, _bot - _top - 0.3)
            lines = None
            if size >= 3.4:
                while size >= 3.2:
                    ls = wrap(pool, cn, size, w, maxlines=1)
                    if ls:
                        lines = ls
                        break
                    size -= 0.2
            if lines:
                y = _top + 0.80 * size
                items.append({"x": rx0 + 1.5, "y": y, "size": size,
                              "text": lines[0], "bold": True,
                              "yrow": (geo[0] + geo[1]) / 2.0,
                              "ers": []})
                continue
            # 兜底：英文下面实在没空间 → 白带覆盖式（盖英文、中文原位）
            size, lines = 7.6, None
            while size >= 5.0:
                ls = wrap(pool, cn, size, w, maxlines=1)
                if ls:
                    lines = ls
                    break
                size -= 0.3
            if not lines:
                cells_ok = False        # 一行放不下 → 退回图例样式
                break
            items.append({"x": rx0 + 2.0, "y": ry1 - size * 0.10,
                          "size": size, "text": lines[0], "bold": True,
                          "yrow": (ry0 + ry1) / 2.0,
                          "ers": [[rx0 - 0.6, ry0 - 0.6,
                                   rx1 + 0.6, ry1 + 0.6]]})
        if cells_ok and items:
            inline.extend(items)
            continue                    # 该子表整体转为就地替换，不再出图例
        nb = dict(blk)
        nb["cells"] = {k: v for k, v in blk["cells"].items() if k[0] == 0}
        nb["rh"] = blk["rh"][:1]
        nb["R"] = 1
        nb["h"] = sum(nb["rh"])
        out.append(nb)
    if not hit:
        return None, None, [], []
    h = max(0.0, sum(b["h"] for b in out) + 6.0 * (len(out) - 1))
    return ({"size": out[0]["size"] if out else 6.0, "blocks": out,
             "h": h}, kept, drop, inline)


# level: (max_size, bold, line-height)
LEVELS = {
    "h1":   (13.0, True,  1.30),
    "h2":   (11.0, True,  1.28),
    "h3":   (9.6,  True,  1.25),
    "toc":  (7.2,  False, 1.14),
    "body": (8.0,  False, 1.18),
}
MIN_SIZE = 5.0
PAD = 1.8

def red_line(l):
    """受控水印/图章行（红色文字）。表格里收行时必须剔掉，否则红字碎片
    会被当成格子内容渲染出来（DOC-A09 p3 实测多出"、泄露或转让给第三方"）。"""
    try:
        c = int(l["spans"][0].get("color", 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return ((c >> 16) & 0xFF) >= 0x90 \
        and ((c >> 8) & 0xFF) <= 0x70 and (c & 0xFF) <= 0x70


BOILER_RE = re.compile(
    r"archived according to|template version|copyright|all rights reserved|"
    r"confidential|proprietary|PRODUCTLINE AG, CH|KA No|KA Date|KA编号|"
    r"replaces|classification|lead office|prepared|reviewed|released|"
    r"modification|\bAe\s*\d|drawing no|no\. of pages|language|"
    r"\border\b|basis drawing|"
    r"technical manual|geheim|confidentiel|vertraulich", re.I)
HEAD_NUM = re.compile(r"^(\d+(?:[.．]\d+)*)[.．]?\s+\S")
CJKBOIL = re.compile(
    r"归档|模板版本|分类主管|版权所有|保留所有权利|工作指导|技术手册|"
    r"基础图纸|规范已检查|已审核|已发布|编制\s*\d|审核\s*\d|检查\s*\d|"
    r"页面\s*格式|转换\s*器\s*技术|KA\s*[编日号]|保密文件|含某电梯厂商集团|专有保密信息")

# 图纸家具标签：标题栏"Page 1 / Lang. E"类字段词。pdf2zh 常把它们译成
# "页 1"、"语言"、"<EN>目 录</EN>"（伪标签包裹语言代号）之类碎片；标题栏
# 格子挤不进去时被覆盖兜底/自愈改排堆到页末，读者看着莫名其妙（DOC-X01
# 实测页末出现"1 语言 目 录"）。这类纯家具词按设计有意跳过，不再堆页末。
_FURN_CN = frozenset("页语言目录")
_FURN_PHRASES = frozenset((
    "页面", "格式", "语言", "目录", "英文", "中文", "页码", "版本"))
_FURN_NOISE = re.compile(
    r"</?EN>|</?ZH(?:-[A-Z]+)?>|[\s0-9A-Za-z.\-–—:;,·|/()（）"
    r"\u3000、。，；：？！“”‘’]+")
# 标题栏家具英文锚点：源文小字格里含 Page/Lang./Form/No./Pg/Sheet/Contents 词，
# 或整块就是 ≤4 字符的纯短代号（EN、E、A4、1 …）
FURN_EN = re.compile(
    r"\b(?:page|lang|format|form|no|pg|sheet|contents)\b"
    r"|^[\s0-9A-Za-z.]{1,4}$", re.I)


def is_furniture_cn(t, substr=False):
    """纯"页/语言/目录/格式/页面"类标题栏家具碎片（可含数字/字母代号、
    伪标签、空白）→ True。substr=True 时放宽到"≤8 字且含家具短语"
    （如"无页码格式""页面语言"这类机翻杂凑）——仅限配对路径使用
    （那边还有源文家具词+小字号双重守卫），挂靠路径用精确匹配防误伤正文。"""
    t2 = _FURN_NOISE.sub("", t or "")
    if not t2 or len(t2) > 8:
        return False
    if t2 in _FURN_PHRASES or all(c in _FURN_CN for c in t2):
        return True
    return substr and any(p in t2 for p in ("页面", "页码", "格式", "语言"))


DESC = set("gjpqyQŸµ\u4e00-\u9fff…")


def cluster_rects(rects, gap=3.0):
    """merge drawing items that visually belong together (circuits, tables,
    frames): two rects within `gap` pt fuse, transitively."""
    cl = [fitz.Rect(r) for r in rects if r.is_valid and not r.is_empty]
    changed = True
    while changed:
        changed = False
        out = []
        for r in cl:
            hit = None
            for i, c in enumerate(out):
                if (c.x0 - gap < r.x1 and r.x0 - gap < c.x1
                        and c.y0 - gap < r.y1 and r.y0 - gap < c.y1):
                    hit = i
                    break
            if hit is None:
                out.append(r)
            else:
                out[hit] |= r
                changed = True
        cl = out
    return cl


def cut_key(y):
    """切带位置的规范化键：**向上**取整到 0.1pt。

    这里绝不能用 round()：切线与上方墨迹之间只留 0.4pt 安全间隙，四舍五入
    有时会把它压到 0.39pt，正好落在 qa_tears 的判定线上（该脚本用"切点左右
    0.4pt 内是否还压着竖线"来判撕裂），一根完好的竖线就被判成"被切线拦腰
    切断"。向上取整只会让间隙变大，永远不会变小。"""
    return math.ceil(y * 10.0 - 1e-9) / 10.0


def stroke_rects(page, W, H):
    """把一页的矢量图元拆成"真实墨迹矩形"清单（引擎各处障碍物的唯一口径）。

    · 直线段 → 按描边宽度膨胀成细长矩形（零宽/零高的 "l" 图元若不膨胀，
      `cluster_rects` 的 is_empty 会把它们整条丢掉）；
    · **空心**矩形（`fill is None` 的 re：手写章框、图框）→ 只贡献**四条边**。
      它的 bbox 是整整一片区域，当实心障碍会把章框覆盖的格子内部全部封死，
      中文在格内无处可放、只好飘到别的格（用户验收实测："可互换" 被挤到隔壁
      格右上角、"应用" 挤到章框下面）；
    · 实心图形（fill 非 None）→ 用它的外接矩形。"""
    out = []
    for p in page.get_drawings():
        fill = p.get("fill")
        sw = max(float(p.get("width") or 0.0), 0.35) * 0.5 + 0.25
        for it in p["items"]:
            kind = it[0]
            if kind == "re":
                r0 = fitz.Rect(it[1])
                if fill is None:
                    segs = [(r0.x0, r0.y0, r0.x1, r0.y0),
                            (r0.x0, r0.y1, r0.x1, r0.y1),
                            (r0.x0, r0.y0, r0.x0, r0.y1),
                            (r0.x1, r0.y0, r0.x1, r0.y1)]
                else:
                    segs = [(r0.x0, r0.y0, r0.x1, r0.y1)]
            elif kind == "l":
                a, b = it[1], it[2]
                segs = [(a.x, a.y, b.x, b.y)]
            elif kind == "qu":
                q = it[1]
                segs = [(q.ul.x, q.ul.y, q.lr.x, q.lr.y)]
            else:                                   # "c" 等曲线 → 外接矩形
                pts = [v for v in it[1:] if hasattr(v, "x")]
                if not pts:
                    continue
                segs = [(min(v.x for v in pts), min(v.y for v in pts),
                         max(v.x for v in pts), max(v.y for v in pts))]
            for x0, y0, x1, y1 in segs:
                r = fitz.Rect(min(x0, x1), min(y0, y1),
                              max(x0, x1), max(y0, y1))
                if fill is None:
                    r = fitz.Rect(r.x0 - sw, r.y0 - sw, r.x1 + sw, r.y1 + sw)
                if not r.is_valid or r.width > W - 2 or r.height > H - 2:
                    continue                        # 整页外框线走 rails，不当障碍
                out.append(r)
    return out


def table_rules(rects, W, H, frame=None):
    """从墨迹矩形里挑出**表格格线**的横坐标/纵坐标（排掉图纸外框线）。

    判据是"这条线是不是**落在内容框边界上**"（frame 传入内容框），而不是
    "够不够长"：表格自身的边框常常也有页宽的 70~90%，按长度一刀切会把整张
    表的行线全排掉（实测：DOC-B05 的横线只剩 4 条 → 单元格被撑到源文外
    42pt，14 块译文只落得下 3 块）。没有内容框时退化为"排掉近乎整页的线"——
    那种线是页边框，不是格线。"""
    hy, vx = [], []
    for r in rects:
        if r.height <= 2.5 and r.width >= 24:
            y = (r.y0 + r.y1) / 2
            if frame is not None:
                if abs(y - frame.y0) <= 2.5 or abs(y - frame.y1) <= 2.5:
                    continue                    # 落在内容框上/下边界上
            elif r.width >= 0.9 * W:
                continue
            hy.append(y)
        elif r.width <= 2.5 and r.height >= 12:
            x = (r.x0 + r.x1) / 2
            if frame is not None:
                if abs(x - frame.x0) <= 2.5 or abs(x - frame.x1) <= 2.5:
                    continue                    # 落在内容框左/右边界上
            elif r.height >= 0.9 * H:
                continue
            vx.append(x)

    def _uniq(vals, tol=2.5):
        out = []
        for v in sorted(vals):
            if out and v - out[-1] <= tol:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out

    return _uniq(hy), _uniq(vx)


def source_cell(rect, hy, vx, pad=42.0):
    """源文所在的"格"：由最近的格线围出（上/下最近的横线、左/右最近的竖线）。

    用户要求"翻译中文不要占用别的单元格"：落字只许在自己这格里，宁可放不下
    也不飘到别处。某一侧找不到格线时用 源框±pad 兜底，免得把整页当一格。"""
    R = fitz.Rect(rect)
    xl = [x for x in vx if x <= R.x0 + 1.0]
    xr = [x for x in vx if x >= R.x1 - 1.0]
    yt = [y for y in hy if y <= R.y0 + 1.0]
    yb = [y for y in hy if y >= R.y1 - 1.0]
    c = fitz.Rect(max(xl) if xl else R.x0 - pad,
                  max(yt) if yt else R.y0 - pad,
                  min(xr) if xr else R.x1 + pad,
                  min(yb) if yb else R.y1 + pad)
    c = fitz.Rect(max(c.x0, R.x0 - pad), max(c.y0, R.y0 - pad),
                  min(c.x1, R.x1 + pad), min(c.y1, R.y1 + pad))
    if c.width < 8.0 or c.height < 8.0:
        return None
    return c


def content_frame(rects, W, H):
    """图纸/表单的"内容框"：由**长横线、长竖线**围出的最内层包络。

    用户要求"中文不许出图框"：图框外面是页边的坐标带（1..8 / A..F）、页脚
    之类的图纸家具，译文落在那儿只会让人以为画错了；页末附录同理。返回
    None 表示这一页没有可用的长框线（普通正文页），不施加限制。"""
    hs, vs = [], []
    for r in rects:
        if r.height <= 2.5 and r.width >= 0.55 * W:
            hs.append(r)
        elif r.width <= 2.5 and r.height >= 0.55 * H:
            vs.append(r)
    if len(hs) < 2 or len(vs) < 2:
        return None
    lx = [(r.x0 + r.x1) / 2 for r in vs if (r.x0 + r.x1) / 2 < W / 2]
    rx = [(r.x0 + r.x1) / 2 for r in vs if (r.x0 + r.x1) / 2 >= W / 2]
    ty = [(r.y0 + r.y1) / 2 for r in hs if (r.y0 + r.y1) / 2 < H / 2]
    by = [(r.y0 + r.y1) / 2 for r in hs if (r.y0 + r.y1) / 2 >= H / 2]
    if not (lx and rx and ty and by):
        return None
    fr = fitz.Rect(max(lx), max(ty), min(rx), min(by))
    if fr.width < 0.4 * W or fr.height < 0.4 * H:
        return None
    return fr


def empty_form_cells(rects, W, H, text_rects, frame=None):
    """表单里"有格线围出、但没有任何文字"的空格子 —— 中文不许占用它们
    （那是留着将来填信息的）。只认够长的表格格线，免得把 PCB 图上的短虚线、
    零件符号当成格子。text_rects 传文字墨迹框（含页面上已有的中文）。"""
    hy, vx = table_rules(rects, W, H, frame)
    if len(hy) < 3 or len(vx) < 3 or (len(hy) - 1) * (len(vx) - 1) > 3000:
        return []
    out = []
    for i in range(len(hy) - 1):
        a, b = hy[i], hy[i + 1]
        if b - a < 4.0:
            continue
        for j in range(len(vx) - 1):
            c, d = vx[j], vx[j + 1]
            if d - c < 4.0:
                continue
            cell = fitz.Rect(c, a, d, b)
            if cell.get_area() > 0.25 * W * H:
                continue
            for t in text_rects:
                if (fitz.Rect(t) & cell).get_area() > 0.6:
                    break
            else:
                out.append(cell)
    return out


def _form_rules(page, rect):
    """rect 内所有细长轴对齐线段，按 (横线, 竖线) 返回。
    横线 = (y, x0, x1)，竖线 = (x, y0, y1)。"""
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
            if (a.y1 < rect.y0 - 2.0 or a.y0 > rect.y1 + 2.0 or
                    a.x1 < rect.x0 - 2.0 or a.x0 > rect.x1 + 2.0):
                continue
            if a.height <= 2.5 and a.width >= 2.0:
                hs.append(((a.y0 + a.y1) / 2, a.x0, a.x1))
            elif a.width <= 2.5 and a.height >= 2.0:
                vs.append(((a.x0 + a.x1) / 2, a.y0, a.y1))
    return hs, vs


def split_form_cluster(c, page, content):
    """把一个"整页大簇"按真实格线拆回叠在一起的各张子表。

    表单类文档（标题栏 / 修订表 / 历史表）常把好几张表画在**同一个连续外框**
    里：左右边框贯通，cluster_rects 于是把它们并成一个整页大的簇。后果是
    所有切线都被它吞掉（cuts<=2）—— 重排把整页译文折成一段流水账，overlay
    则把译文塞进随便哪张表的空格里（跨表乱插，看着像原文被复制了一遍）。
    两张表的分界正是"竖线不再连续"的那条横线：线上、线下的列支撑集是两套
    不同的列。返回 [] 表示该簇不是这种表单（单张表，或 PCB 这类图形）。"""
    hs, vs = _form_rules(page, c)
    hy = []
    for y, _x0, _x1 in sorted(hs):
        if hy and y - hy[-1] <= 3.0:
            hy[-1] = (hy[-1] + y) / 2
        else:
            hy.append(y)
    if len(hy) < 4 or len(vs) < 6:
        return []
    cw = c.width

    def cover(y):
        segs = sorted((x0, x1) for yy, x0, x1 in hs if abs(yy - y) <= 3.0)
        tot, cur = 0.0, None
        for x0, x1 in segs:
            if cur is None:
                cur = [x0, x1]
            elif x0 <= cur[1] + 1.0:
                cur[1] = max(cur[1], x1)
            else:
                tot += cur[1] - cur[0]
                cur = [x0, x1]
        if cur:
            tot += cur[1] - cur[0]
        return tot

    wide = [y for y in hy if cover(y) >= 0.7 * cw]
    if len(wide) < 4:
        return []                    # 不是"横线铺满外框"的表单

    def support(a, b):
        h = b - a
        if h <= 0:
            return frozenset()
        return frozenset(round(x, 1) for x, y0, y1 in vs
                         if min(y1, b) - max(y0, a) >= 0.6 * h)

    keep = []
    for i in range(1, len(hy) - 1):
        y = hy[i]
        if y not in wide:
            continue
        pa, nx = support(hy[i - 1], y), support(y, hy[i + 1])
        if len(pa) + len(nx) < 3 or max(len(pa), len(nx)) < 3:
            continue
        if len(pa & nx) / len(pa | nx) >= 0.5:
            continue                 # 竖线一路穿过 → 同一张表内部的行线
        # 注：表头是合并单元格时，表头那几列是表体列的子集，"变化很大"同样
        # 成立，于是这里会连表头一起拆开 —— 这是**想要**的：表头的中文就落
        # 在表头正下方，而不是被推到整张表下面（否则落点过远、排版告警）。
        # 反过来，一张表的内部行线上下列集完全一致，永远不会被拆。
        if keep and y - keep[-1] < 20.0:
            continue                 # 相邻两线属同一处分界，取靠上那条
        keep.append(y)
    if not keep:
        return []
    ys = [c.y0]
    for y in keep:
        if y - ys[-1] >= 24.0:
            ys.append(y)
    while len(ys) > 1 and c.y1 - ys[-1] < 24.0:
        ys.pop()
    ys.append(c.y1)
    if len(ys) < 3:
        return []
    # 每片的 bbox 取"分给它的那些**高**格线的外接矩形"，而不是直接取分界线：
    #   · 直接按分界线切，两片严丝合缝相接 → safe_cut 找不到落刀的空隙，切线
    #     被一路推回页底，等于没拆（这就是旧版整页折成一段的机制）；
    #   · 从分界线往里缩也不成 —— 竖线会伸进缩掉的那块，切线正好把它拦腰
    #     切断（质检①的 VCUT：竖线被切线穿过）；
    #   · 而两张表各自的竖线本来就不相接（中间那道表间距），拿竖线的外接
    #     矩形当 bbox，切线自然落进真实空隙，竖线一根也切不到。
    # 只认高 ≥6pt 的格线，是为了不让跨界的 0.5~5pt 边框短线把相邻两片又粘
    # 成一片（那些短线被切到也无所谓：质检①只查高 ≥25pt 的竖线）。
    own = [r for r in content
           if c.x0 - 1.0 <= (r.x0 + r.x1) / 2 <= c.x1 + 1.0
           and c.y0 - 1.0 <= (r.y0 + r.y1) / 2 <= c.y1 + 1.0]
    out = []
    for k in range(len(ys) - 1):
        sub = [r for r in own
               if ys[k] - 1.0 <= (r.y0 + r.y1) / 2 <= ys[k + 1] + 1.0]
        tall = [r for r in sub if r.height >= 6.0]
        sub = tall or sub
        if not sub:
            continue
        u = fitz.Rect(sub[0])
        for r in sub[1:]:
            u |= r
        if u.height >= 12.0:
            out.append(u)
    return out if len(out) > 1 else []


def line_ink(l):
    """conservative real-ink rect: never smaller than the actual glyphs,
    yet trims metric-inflated bbox padding (space-line height blowups)."""
    rect = l["rect"]
    top, bot, inked = 1e9, -1e9, False
    for s in l["spans"]:
        if not s["text"].strip():
            continue
        inked = True
        oy = s["origin"][1]
        sz = max(4.0, s.get("size", 9.0))
        sr = s["rect"]
        if set(s["text"].strip()) <= set("()[]{}|/\\'\"‘’“”"):
            top = min(top, sr.y0)          # tall punctuation: full ink box
            bot = max(bot, sr.y1)
            continue
        top = min(top, oy - sz * 0.78, sr.y0 + 0.14 * sz)
        pad = 0.28 if any(c in DESC for c in s["text"]) else 0.06
        bot = max(bot, oy + sz * pad, sr.y1 - 0.08 * sz)
    if not inked:
        return fitz.Rect(rect.x0, (rect.y0 + rect.y1) / 2,
                         rect.x1, (rect.y0 + rect.y1) / 2)
    if top > bot:
        top, bot = rect.y0, rect.y1
    return fitz.Rect(rect.x0 - 1, top, rect.x1 + 1, bot)


def max_span_size(block):
    m = 0.0
    for l in block["lines"]:
        for s in l["spans"]:
            m = max(m, s.get("size", 9.0))
    return m


def classify(en_text, size, nl):
    """heading level or body"""
    if size >= 10.2 and nl <= 2:
        m = HEAD_NUM.match(en_text)
        if m:
            depth = m.group(1).count(".") + m.group(1).count("．")
            return {0: "h1", 1: "h2"}.get(depth, "h3")
        core = re.sub(r"[^A-Za-z ]", "", en_text).strip()
        if core and core == core.upper() and len(core.split()) <= 6 and len(en_text) < 60:
            return "h2"
    return "body"


def _sanitize_cn(t):
    """写出前的最后防线：CJK 兼容表意字符（U+F900–U+FAD9，路/不/量 等）
    规范成标准码位——写出字体普遍没有兼容码位的字形（渲染空白、提取 \x00，
    DOC-B01/DOC-A07 实测）；控制字符剔除；NBSP 换普通空格。
    fix_cn 已对多数路径清洗，这里兜住表格等其余一切路径。"""
    if not t:
        return t
    if any("\uf900" <= c <= "\ufad9" for c in t) or "\xa0" in t \
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", t):
        out = []
        for c in t:
            if "\uf900" <= c <= "\ufad9":
                c = unicodedata.normalize("NFKC", c) or c
            elif c == "\xa0":
                c = " "
            out.append(c)
        t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "",
                   "".join(out))
    return t


def plan_cn(pool, cn, width, lvl):
    cn = _sanitize_cn(cn)
    size0, bold, lh = LEVELS[lvl]
    size = size0
    while size > MIN_SIZE:
        lines = wrap(pool, cn, size, width, bold=bold)
        if lines and max(pool.width(l, size, bold) for l in lines) <= width + 1.0:
            return size, lines, bold, lh
        size -= 0.4
    lines = wrap(pool, cn, MIN_SIZE, width, bold=bold)
    return MIN_SIZE, lines or [cn], bold, lh


def main():
    global HEAL
    import os
    HEAL = int(os.environ.get("SYNTH_HEAL", "1"))
    orig_f, mono_f, out_f = sys.argv[1], sys.argv[2], sys.argv[3]
    orig, mono = fitz.open(orig_f), fitz.open(mono_f)
    if len(orig) != len(mono):
        sys.exit(f"page mismatch {len(orig)} vs {len(mono)}")
    pool = FontPool()

    def cell_wrap(txt, size, w):
        """wrap that may hard-split over-wide latin tokens (cell rendering)"""
        parts = []
        for word in txt.split():
            ww = pool.width(word, size)
            if ww <= w or len(word) < 3:
                parts.append(word)
                continue
            n = int(ww // max(w, 6.0)) + 1
            k = -(-len(word) // n)
            parts.extend(word[i:i + k] for i in range(0, len(word), k))
        return wrap(pool, " ".join(parts), size, w) or [txt]

    def _wrap_cell(txt, size, w):
        """multi-line cell text ('\n' = one original display line)"""
        out = []
        for part in txt.split("\n"):
            part = part.strip()
            if part:
                out.extend(cell_wrap(part, size, w))
        return out or [txt.strip()]

    def boiler_rep(txt, nrep):
        """repeated long sentence without digits = legal boilerplate;
        repeated short labels / part-number rows are real diagram content"""
        return (nrep >= max(3, npages // 8) and len(txt) >= 45
                and not any(c.isdigit() for c in txt))

    gmap, cfix = {}, {}
    try:
        import json
        import os
        if getattr(sys, "frozen", False):        # PyInstaller exe: prefer a
            dirs = [os.path.dirname(sys.executable),   # user-editable copy
                    getattr(sys, "_MEIPASS", "")]      # next to the .exe
        else:
            dirs = [os.path.dirname(os.path.abspath(__file__))]
        gf = next((os.path.join(d, "heading_glossary.json") for d in dirs
                   if d and os.path.exists(os.path.join(d, "heading_glossary.json"))), None)
        if gf:
            with open(gf, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    gmap[re.sub(r"\s+", " ", norm(k)).strip()] = v
        # 中文纠错表：机翻把工程缩写译成金融词（"New Exch. Liq." →
        # "新交易所流动性"）这类错，源文那一块在 PDF 里常和整行表格粘成
        # 一个大块、按源文匹配的词表够不着 —— 直接用"中文→中文"的替换表
        # 纠正译文，验收端可以随时往 cn_fix.json 里加条目。
        cf = next((os.path.join(d, "cn_fix.json") for d in dirs
                   if d and os.path.exists(os.path.join(d, "cn_fix.json"))), None)
        if cf:
            with open(cf, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if k:
                        cfix[k] = v
    except Exception:
        pass

    # 大写一两字母的尖括号标记（<EN> </E> <B>…）= 机翻漏出来的元标签/占位符，
    # 不是内容。只认这种短标签，免得误删正文里 legit 的 "< Add-on module … >"。
    _META_TAG = re.compile(r"</?[A-Z][A-Za-z]?>")

    def fix_cn(t):
        """译文进入排版前的统一清洗：先剔元标签垃圾与控制字符，再套中文纠错表。"""
        if not t:
            return t
        if _META_TAG.search(t):
            t = re.sub(r"\s{2,}", " ", _META_TAG.sub("", t)).strip()
        # 控制字符（\x00 等）与 NBSP：NBSP 走子集字体写出时字形会丢成 \x00
        # （DOC-B01 页末碎片实测），统一换成普通空格。
        t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", t)
        t = t.replace("\xa0", " ")
        t = sym2uni(t)          # 符号字体私用区（Wingdings ☑ 等）→ 标准码位
        # CJK 兼容表意字符（U+F900–U+FAFF，路/行/量 等）：字形与路/行/量完全
        # 相同但码位不同，pdf2zh 整篇都用它们；写出的字体没有这些码位的字形
        # → 渲染空白、提取 \x00（DOC-B01 正文大面积缺字实测）。规范成标准码位。
        if any("\uf900" <= c <= "\ufaff" for c in t):
            t = "".join(
                unicodedata.normalize("NFKC", c)
                if "\uf900" <= c <= "\ufaff" else c
                for c in t)
        if not cfix:
            return t
        # 以 "=" 开头的键 = **整块精确匹配**（不是子串）。图纸表头这类单词块
        # （"Designation" 被机翻成孤零零一块"指定"）用子串替换必然误伤正文，
        # 精确匹配才敢进表（DOC-B05 同族表单实测）。命中即整块换成词表值。
        s = t.strip()
        for k, v in cfix.items():
            if k.startswith("="):
                if s == k[1:].strip():
                    return v
                continue
            if k in t:
                t = t.replace(k, v)
        return t
    npages = len(orig)
    rep_thresh = max(3, npages // 10) if npages >= 12 else 3

    # pass 1: repetition counts (boilerplate detection)
    pages_ob, pages_mb = [], []
    rep, rep_m = Counter(), Counter()
    for pno in range(npages):
        ob = get_blocks(orig[pno])
        pages_ob.append(ob)
        mbp = get_blocks(mono[pno])
        # mono 文本入口统一清洗：兼容表意字符规范成标准码位、控制字符剔除、
        # NBSP 换空格——表格收割等下游路径不再各自为政（DOC-A07 的 不→\x00
        # 实测：单元格写路径绕过了 fix_cn/plan_cn 两道清洗）
        for b in mbp:
            b["text"] = _sanitize_cn(b["text"])
            for l in b.get("lines", []):
                l["text"] = _sanitize_cn(l["text"])
        pages_mb.append(mbp)
        for b in ob:
            rep[b["text"][:120]] += 1
        for b in mbp:
            rep_m[b["text"][:120]] += 1

    nd = fitz.open()
    geom = []          # per-page band mapping for the pixel audit
    n_units = n_placed = n_app = n_rb = 0
    lvl_count = Counter()
    _spill_cn = []     # 跨页溢出译文（babeldoc 常把下一页开头的内容译到上一页；
                       # 上一页没落点的译文带到下一页开头 —— 用户 2026-09-29：
                       # 第 5 页尾页又堆了一堆，就是这类溢出被塞进上一页页尾）
    _spill_next = []   # 本页判为"属于下一页"的单元译文，翻页时并入 _spill_cn
    for pno in range(npages):
        _spill_cn.extend(_spill_next)
        _spill_next = []
        page = orig[pno]
        W, H = page.rect.width, page.rect.height

        def rotated(x):
            """vertical margin text via line direction matrix; parens etc.
            (dir horizontal, tall bbox) stay as ordinary text"""
            if isinstance(x, dict):
                if abs(x.get("dir", (1.0, 0.0))[0]) < 0.5:
                    return True
                rc = x["rect"]
            else:
                rc = x
            return rc.width < 12 and rc.height > 30

        def clean_blk(blocks):
            """strip rotated margin-column lines out of content blocks"""
            out = []
            for b in blocks:
                keep = [l for l in b["lines"] if not rotated(l)]
                if not keep:
                    continue
                if len(keep) != len(b["lines"]):
                    b = dict(b)
                    b["lines"] = keep
                    r = fitz.Rect(keep[0]["rect"])
                    for l in keep[1:]:
                        r |= l["rect"]
                    b["rect"] = r
                    b["text"] = "\n".join(l["text"] for l in keep)
                out.append(b)
            return out

        ob = clean_blk(pages_ob[pno])
        mb = clean_blk(pages_mb[pno])
        pairs = align(ob, mb)

        # 装饰行的 id 集合（斜排水印、竖排页边字：行高 ≫ 字号）。这类行既不
        # 算"会被压的内容"，也**绝不能充当别的译文的"宿主源块"**——它们
        # bbox 巨大（斜排水印斜跨大半页）且自带中文，一旦被选作宿主，落进它
        # 框内的中文块就会被误判成"宿主已有中文"而静默丢弃，最后整段堆到页末。
        # （DOC-B14 第 4 节 5 个中文块里有 3 个就是这样丢到页尾的。）
        dec_ids = set()
        for _b in ob:
            for _l in _b["lines"]:
                try:
                    _sz = max(_s.get("size", 9.0) for _s in _l["spans"])
                except ValueError:
                    continue
                if line_ink(_l).height > 2.6 * max(4.0, _sz):
                    dec_ids.add(id(_l))

        def is_decor_block(b):
            """整块行都是装饰行 → 不可当宿主源块。"""
            ls = [l for l in b["lines"] if l["text"].strip()]
            return bool(ls) and all(id(l) in dec_ids for l in ls)

        # 装饰行（斜排水印）必须同时排除出 src_line_rects：它的 bbox 斜跨
        # 大半页（DOC-B05 实测 438x438pt），被当障碍物后 overlay 落字在页
        # 面中部全军覆没（1982 次尝试 1235 次被它挡死 → 整页 14 个正文块
        # 只落 2 块）。装饰行已被 decor 摘除并在绘制阶段整条重画，其原 bbox
        # 在成品里就是空白地，中文落那里没问题（中文画在水印之上）。
        # 与 bobs 的排除（踩坑 #5）同一族 —— 别只修一处。
        src_line_rects = [line_ink(l) for b in ob for l in b["lines"]
                          if id(l) not in dec_ids]
        _hr0 = [line_ink(l) for b in ob for l in b["lines"] if not rotated(l)]
        if _hr0:
            wx0 = max(0.0, min(rr.x0 for rr in _hr0) - 0.5)
            wx1 = min(W, max(rr.x1 for rr in _hr0) + 0.5)
        else:
            wx0, wx1 = 0.0, W
        draw_rects = [fitz.Rect(p["rect"]) for p in page.get_drawings()
                      if p["rect"].is_valid and not p["rect"].is_empty
                      and p["rect"].width < W - 4 and p["rect"].height < H - 4]
        # 障碍物/格线用的"真实墨迹矩形"：单线按描边宽度膨胀、**空心矩形只算
        # 四条边**（手写章框、图框都是空心的，当成实心会把整片格子内部封死，
        # 中文在格内无处可放、只好飘到别的格）。与 overlay 路径同一套口径。
        ink_rects = stroke_rects(page, W, H)
        img_rects = []
        try:
            for im in page.get_images(full=True):
                img_rects += [fitz.Rect(r) for r in page.get_image_rects(im[0])]
        except Exception:
            pass
        obs = []

        # ---- 背景装饰层（斜排水印）：摘出切带、切完再整条重画 --------------
        # 斜排文字的 bbox 斜跨大半页：当切带障碍会把整页切线顶到页尾，不当
        # 障碍则每切一刀就把它在那一带里的片段平移一次 —— 页面上出现一叠错位
        # 碎片（"水印被截断"）。这里把它从参与切带的源内容里整个摘出去（见
        # decor.py：置空该文本块 + 文本抽取自校验），切带结束后按原位置整条
        # 重画。自校验不过则退回原页（宁可有碎片，绝不误删正文）。
        decors = _decor.collect(page)
        decor_items = []
        gsrc, gpno = orig, pno
        if decors:
            _c = _decor.clean_page(orig, pno, decors)
            if _c is not None:
                gsrc, gpno = _c
            else:
                decors = []          # 清理失败：按原样走（水印可能被切）
            for _d in decors:
                decor_items.append({k: _d[k] for k in
                                    ("text", "size", "origin", "angle",
                                     "color", "opacity", "fontname")})
            if DBG(pno):
                print(f"  DECOR p{pno + 1} n={len(decors)} cleaned="
                      f"{'Y' if gsrc is not orig else 'N'} "
                      f"{[d['text'][:14] for d in decors]}")

        def paint_decor(np_):
            if decor_items:
                _decor.draw_items(np_, decor_items, page)

        # ---- units with boilerplate filtering ----
        units = []
        pr = {o: m for o, m in pairs}
        strip = {}
        for i in range(len(ob)):
            if i in pr:
                continue
            b = ob[i]
            mx = max_span_size(b)
            t = norm(b["text"])
            if mx < 10.2 or len(t) > 80 or not HEAD_NUM.match(t):
                continue
            if BOILER_RE.search(t):
                continue
            r = b["rect"]
            if r.y1 < 34 or r.y0 > H - 58:
                continue
            num = HEAD_NUM.match(t).group(1)
            cnh = None
            nxt = min((o for o in pr if o > i), default=None)
            if nxt is not None:
                m = pr[nxt]
                mm = re.match(r"^(\d+(?:[.．]\d+)*)[.．]?\s+([\u4e00-\u9fff][^\s]{0,16})\s+",
                              mb[m]["text"])
                if mm and mm.group(1) == num:
                    cnh = mm.group(0).strip()
                    strip[m] = mm.group(0)      # paragraph loses the prefix
            if cnh is None:
                cnh = gmap.get(re.sub(r"\s+", " ", t).strip())
            cnh = fix_cn(cnh or "")
            if cnh and has_cjk(cnh):
                lvl = classify(t, mx, len(b["lines"]))
                if lvl != "body":
                    units.append({"rect": fitz.Rect(r), "cn": cnh, "mi": None,
                                  "oi": i, "en": b["text"], "org": "head",
                                  "ybot": max(line_ink(l).y1 for l in b["lines"]),
                                  "lvl": lvl})

        for oi, mi in pairs:
            src, cnb = ob[oi], mb[mi]
            cn = cnb["text"][len(strip.get(mi, "")):].strip() if mi in strip else cnb["text"]
            if CJKBOIL.search(re.sub(r"\s+", "", norm(cn))):
                continue                       # Chinese legal/copyright boilerplate
            if any(has_cjk(l["text"]) for l in src["lines"]):
                continue
            if not has_cjk(cn):
                mx = max_span_size(src)
                g = gmap.get(re.sub(r"\s+", " ", norm(src["text"])).strip())
                if g and classify(norm(src["text"]), mx, len(src["lines"])) != "body":
                    cn = g
                else:
                    continue
            cn = fix_cn(cn)
            if is_furniture_cn(cn, substr=True) and len(norm(src["text"])) <= 60 \
                    and FURN_EN.search(norm(src["text"])) \
                    and max_span_size(src) < 10.2:
                continue        # 标题栏家具（No.Pg/Form./Page/Lang. 小字格），
                # 译出"1 格式""页面""英 文"这类碎片只会让读者莫名其妙
                # （DOC-X01/DOC-B10 实测）；且这些单元的切点会落进标题栏
                # 内部、把外框竖线拦腰切开（撕裂质检 VCUT）
            r = src["rect"]
            if r.width < 8 or r.height > 3 * r.width:
                continue                       # rotated margin columns
            # zone filters
            if r.y1 < 34 or r.y0 > H - 58:
                continue                       # header / footer strips
            if boiler_rep(src["text"], rep[src["text"][:120]]):
                continue                       # repeated legal boilerplate
            if len(src["text"]) < 120 and BOILER_RE.search(src["text"]) \
                    and max_span_size(src) < 10.2:
                continue                       # title-block / revision-table cells
            if pno == 0 and r.y0 > H - 235 and BOILER_RE.search(src["text"]):
                continue                       # cover title/revision cluster
            units.append({"rect": fitz.Rect(r), "cn": cn, "mi": mi, "oi": oi,
                          "en": src["text"], "org": "pair",
                          # mono 侧的块矩形（"这块中文在参考译文里位于哪儿"）。
                          # 注意 u["rect"] 对 pair 是**源块**矩形、对 attach 是
                          # **mono** 矩形 —— 两族坐标系不同，凡是要跟 mono 比较
                          # 的地方一律用 _mrect，别用 rect（顺序机检口径要用它）。
                          "_mrect": fitz.Rect(cnb["rect"]),
                          "ybot": (line_ink(src["lines"][0]).y1
                                   if (len(src["lines"]) >= 2
                                       and len(norm(cn)) <= 42
                                       and seg_number(norm(
                                           src["lines"][0]["text"]))
                                       is not None)
                                   else max(line_ink(l).y1
                                            for l in src["lines"])),
                          "_hdr": (line_ink(src["lines"][0]).y1
                                   if (len(src["lines"]) >= 2
                                       and len(norm(cn)) <= 42
                                       and seg_number(norm(
                                           src["lines"][0]["text"]))
                                       is not None) else None),
                          "_srcy1": max(line_ink(l).y1 for l in src["lines"]),
                          "lvl": classify(src["text"], max_span_size(src),
                                          len(src["lines"]))})

        # ---- attach unconsumed CN blocks (tables/slices the 1:1 aligner
        #      could not pair): anchor them to the containing source block ----
        used_m = {m for o, m in pairs}

        _at_dbg_only = ("skip:host-has-cjk", "skip:boiler-rep", "skip:cjkboil",
                        "skip:cover-zone", "skip:outside-window",
                        "skip:furniture")

        def _at_dbg(m, cb, why):
            """只打有价值的两类：成功附着的、以及被"宿主已有中文"挡掉的。
            后者正是"整段译文被静默丢到页末"的病灶，必须可诊断。"""
            if not DBG(pno):
                return
            if why.startswith("OK") or why in _at_dbg_only:
                r = cb["rect"]
                print("   ATTACH m=%d y=%7.1f-%7.1f -> %s %r"
                      % (m, r.y0, r.y1, why, norm(cb["text"])[:44]))

        # N.N / N.N.N 形式的条目编号（"…步骤 4.3 至 4.7…" 里的也算，是否当锚点
        # 由 _split_by_numbers 的"句首"判据决定）
        _NUMTOK = re.compile(r"(?<![0-9.])\d+(?:\.\d+)+(?![0-9])")

        def _split_bullets(t):
            """把 mono 折成一整块的列表译文按条目分隔符切开，恢复逐条对照。
            只在分隔符总数 ≥2 时才动手 —— 普通句子里的一个破折号不会被误当
            条目边界（宁可整块，不可乱切）。圆点与" - "两族合并计数：真实列表
            常是"• 主条目 - 子条目"混排。"""
            s = norm(t)
            marks = []
            # 破折号要覆盖 U+2010–2015 / U+2212：mono 里条目前导出的常是
            # 不换行连字符 U+2011，只认 ASCII "-" 会漏掉整族列表。
            for pat in (r"[•·▪◦‣∙▶▪]", r"\s[-\u2010-\u2015\u2212]\s"):
                marks += list(re.finditer(pat, s))
            marks.sort(key=lambda m: m.start())
            if len(marks) < 2:
                return None
            segs, prev = [], 0
            for m2 in marks:
                segs.append(s[prev:m2.start()])
                prev = m2.end()
            segs.append(s[prev:])
            out = [x.strip(" \u3000\t-\u2010-\u2015\u2212•·▪◦‣∙")
                   for x in segs]
            out = [x for x in out if x]
            return out if len(out) >= 2 else None

        def _split_by_numbers(cn, pool, sub):
            """按"编号条目"拆中文块，并把每段锚定到它对应的源行。
            源行以 4.2 / 1.3.1 这类编号开头，而 mono 把编号也原样译了出来 ——
            于是可以拿"编号同时出现在源行与译文里"当锚点，比按字数比例猜落点准
            得多（DOC-B01 的 4.2–4.10 整段中文原本被当成一块插在 4.6 之后，
            4.4~4.10 底下一条中文都没有）。与 `_split_bullets` 的区别：
            · 锚点来自**内容**（编号）而非位置比例，所以 K 与 M 不等也不会错位；
            · 只在中文里该编号**位于句首**时才切 —— 串首，或前一字符是
              。；！？; —— 这样 "…并测试步骤 4.3 至 4.7 检查传输继电器。4.10 …"
              里嵌着的 4.3/4.7 不会被误当成条目起点；
            · 每个编号只认它第一次出现在句首的位置（正文里同号重复出现的少）。

            返回 [(段文本, 源行全局下标)]；锚点不足两处、或顺序与源行不一致时
            返回 None（宁可整块，不可乱切）。"""
            anch = []
            for ri in sub:
                t = " ".join(l["text"] for l in pool[ri])
                pn = seg_number(t)
                if pn:
                    anch.append((pn, ri))
            if len(anch) < 2:
                return None
            hits = []
            for tok, ri in anch:
                pat = re.compile(r"(?<![0-9.])" + re.escape(tok) + r"(?![0-9])")
                for m in pat.finditer(cn):
                    if m.start() == 0 or cn[m.start() - 1] in "。；！？;":
                        hits.append((m.start(), ri))
                        break
            hits.sort()
            if len(hits) < 2:
                return None
            if any(hits[i][1] >= hits[i + 1][1] for i in range(len(hits) - 1)):
                return None                       # 锚点顺序与源行顺序矛盾 → 弃用
            out = []
            if hits[0][0] > 0:
                # 首个编号之前的残句是该编号**上一行**的译文（mono 的换行点与
                # 原文不同，一句话常被拆到两个块里）
                prev = [ri for ri in sub if ri < hits[0][1]]
                head = cn[:hits[0][0]].strip()
                if prev and head:
                    out.append((head, prev[-1]))
            for i, (pos, ri) in enumerate(hits):
                en = hits[i + 1][0] if i + 1 < len(hits) else len(cn)
                seg = cn[pos:en].strip()
                if seg:
                    out.append((seg, ri))
            return out if len(out) >= 2 else None

        for m, cb in enumerate(mb):
            if m in used_m:
                _at_dbg(m, cb, "skip:used-by-pair")
                continue
            cn = fix_cn(norm(cb["text"]))
            if sum(1 for c in cn if "\u4e00" <= c <= "\u9fff") < 2:
                _at_dbg(m, cb, "skip:few-cjk")
                continue
            r = cb["rect"]
            if r.y1 < 34 or r.y0 > H - 58:
                continue
            # reject CN blocks whose centre sits outside the horizontal body
            # window: the mono engine scatters single CN chars along the
            # rotated sidebars, which otherwise overlap the legal text
            cxm = (r.x0 + r.x1) / 2.0
            if cxm < wx0 - 3 or cxm > wx1 + 3:
                _at_dbg(m, cb, "skip:outside-window")
                continue
            if boiler_rep(cn, rep_m[cn[:120]]):
                _at_dbg(m, cb, "skip:boiler-rep")
                continue
            if CJKBOIL.search(cn):
                _at_dbg(m, cb, "skip:cjkboil")
                continue
            if is_furniture_cn(cn):
                _at_dbg(m, cb, "skip:furniture")
                continue               # 标题栏家具碎片，有意不搬
            if pno == 0 and r.y0 > H - 235:
                _at_dbg(m, cb, "skip:cover-zone")
                continue                       # cover title/revision fields
            host, host_area = None, None
            for b in ob:
                if is_decor_block(b):
                    continue               # 斜排水印/页边竖排字：不是内容宿主
                sr = b["rect"]
                if (sr.y0 <= r.y0 + 4 and r.y1 <= sr.y1 + 14
                        and sr.x0 <= r.x0 + 14 and r.x1 <= sr.x1 + 14):
                    a = sr.get_area()
                    if host_area is None or a < host_area:
                        host, host_area = b, a     # 取最紧的那个宿主
            if host is None:
                cand = [b for b in ob if not is_decor_block(b)
                        and b["rect"].x0 <= r.x0 + 20
                        and r.x1 <= b["rect"].x1 + 20]
                if cand:
                    host = min(cand, key=lambda b: abs(b["rect"].y0 - r.y0))
            if host is None:
                host = {"rect": fitz.Rect(r), "lines": [], "text": ""}
            if host["lines"] and any(has_cjk(l["text"]) for l in host["lines"]):
                _at_dbg(m, cb, "skip:host-has-cjk")
                continue                       # CN already native there
            # NOTE: 不要在这里按宿主 BOILER_RE 过滤——内容表格（如 PCB History
            # 的宿主块含 "Modification"）会被整表误杀（DOC-A07 实测 placed
            # 24→3）。模板小字格由配对路径的行 775 守卫（BOILER_RE+小字号）
            # 与 cover-zone 负责就够了。
            _at_dbg(m, cb, "OK host=%s" % [round(v, 1)
                                           for v in host["rect"]])
            ybot = max([line_ink(l).y1 for l in cb["lines"]], default=r.y1)
            # mono 常把同一段落硬换行成多个行块，逐块各自挂靠会各自落位：
            # 一旦某块碰撞被自愈改排到页末，正文就出现内容断层、页末躺着
            # 一句没头没尾的残句（DOC-B01 实测：4.2 的后半句孤悬页末）。
            # 同一宿主且 y 连续的相邻行块合并成一个流动段落单元。
            prev = units[-1] if units else None
            if (prev is not None and prev.get("org") == "attach"
                    and prev.get("_host") is host
                    and abs(prev["rect"].y1 - r.y0) < 3.0):
                prev["cn"] += cn
                prev["rect"] |= fitz.Rect(r)
                prev["_mrect"] |= fitz.Rect(r)
                prev["ybot"] = max(prev["ybot"], ybot)
                prev["_srcy1"] = max(prev.get("_srcy1") or 0.0,
                                     max([line_ink(l).y1
                                          for l in host["lines"]],
                                         default=r.y1))
                _at_dbg(m, cb, "OK merged-into-prev host=%s"
                        % [round(v, 1) for v in host["rect"]])
                continue
            units.append({"rect": fitz.Rect(r), "cn": cn, "mi": None,
                          "oi": None, "en": norm(host["text"]),
                          "hlines": host["lines"], "org": "attach",
                          "_host": host, "_mrect": fitz.Rect(r),
                          # 这块译文对应的**源页**内容下沿（宿主源块最后一行墨迹的
                          # 底）。合并单元时取各成员的最大值 → "这段中文离它自己的
                          # 原文有多远"才量得准；`ybot` 不能兼任这个角色（合并后
                          # 它只代表其中一个成员，而且它还是切点依据）。
                          "_srcy1": max([line_ink(l).y1
                                         for l in host["lines"]], default=r.y1),
                          "ybot": ybot, "lvl": "body"})

        # ---- mono 把一个段落的译文拆成多个行块时，只有一块能被 1:1 对齐
        #      吃掉成 pair，其余落成 attach。两者必须**合成一个单元**：它们的
        #      ybot 取自两个不同坐标系（pair 用源页行底、attach 用 mono 块底），
        #      各自求切点就必然落在源段落内部 —— 译文被插到正文中间、前后颠倒
        #      （DOC-B01 实测：概述段的第 2 行插在正文第 3、4 行之间，第 1 行
        #      反而掉到了段尾；同理"4.2 的后半句孤悬页末"也是这一族）。
        #      合并按 **mono 块 y 序**拼接文本，落点沿用 pair 的源页 ybot。
        def _host_of(u):
            if u.get("org") == "pair" and u.get("oi") is not None:
                return ob[u["oi"]]
            return u.get("_host")

        def _mrect_of(u):
            """该单元在 **mono**（参考译文）里的矩形。attach 单元的 rect 本身就是
            mono 侧；pair 单元的 rect 是源块，得取 _mrect。"""
            return fitz.Rect(u["_mrect"])

        _hgrp = {}
        for u in units:
            if u.get("lvl") != "body":
                continue
            h = _host_of(u)
            if h is not None:
                _hgrp.setdefault(id(h), []).append(u)
        _drop = set()
        # DUAL_NO_HOSTMERGE=1 关掉本合并（回到"pair 与 attach 各自落位"的旧
        # 口径），供 A/B 与事后归因用。
        if os.environ.get("DUAL_NO_HOSTMERGE"):
            _hgrp = {}
        for grp in _hgrp.values():
            if len(grp) < 2 or not any(x.get("org") == "pair" for x in grp):
                continue                       # 纯 attach：交给上面的流水段合并
            grp = sorted(grp, key=lambda x: (_mrect_of(x).y0,
                                            _mrect_of(x).x0))
            # 相邻性校验：mono 块之间必须首尾相接（同栏、y 相邻），否则它们
            # 只是"恰好同一个宿主"的两段无关内容，不该并。
            ok = True
            for a2, b2 in zip(grp, grp[1:]):
                ra, rb = _mrect_of(a2), _mrect_of(b2)
                if rb.y0 - ra.y1 > 12.0:
                    ok = False
                    break
                if min(ra.x1, rb.x1) - max(ra.x0, rb.x0) < 8.0:
                    ok = False
                    break
            if not ok:
                continue
            base = next((x for x in grp if x.get("org") == "pair"), grp[0])
            base["cn"] = "".join(x["cn"] for x in grp)
            _mb2 = _mrect_of(base)
            _sy = [float(x.get("_srcy1") or 0.0) for x in grp] \
                + [float(x.get("ybot") or 0.0) for x in grp
                   if x.get("org") == "pair"]
            base["_srcy1"] = max(_sy or [0.0])
            for x in grp:
                _mb2 |= _mrect_of(x)
                if x.get("org") == "pair":
                    base["ybot"] = max(base["ybot"], x["ybot"])
                    base["rect"] |= fitz.Rect(x["rect"])
            base["_mrect"] = _mb2
            for x in grp:
                if x is not base:
                    _drop.add(id(x))
            if DBG(pno):
                print("   HOST-MERGE n=%d pair=%s -> %r"
                      % (len(grp), base.get("mi"), base["cn"][:40]))
        if _drop:
            units = [u for u in units if id(u) not in _drop]

        # 源页自有中文（红章/盖章/既有中文标注）不该被当译文重排：mono 行块
        # 与它们几何重叠时会被 babeldoc 并进行块（DOC-A07 实测：红章句子
        # 混进历史表数字行，蓝色重排一遍还带兼容字符 NUL）。译文单元的中文
        # 命中"源页中文片段"且占比 ≥30% → 整个单元剔除（章面原文源页 1:1
        # 保留，不需要第二遍）。注意别用 decor 判定做这事——会误杀正常文字。
        _srccn = []
        for b2 in ob:
            for l2 in b2["lines"]:
                t2 = norm(l2["text"])
                if len("".join(c for c in t2 if "\u4e00" <= c <= "\u9fff")) >= 6:
                    _srccn.append(t2)
        if _srccn:
            _keep = []
            for u in units:
                cj = "".join(c for c in u["cn"] if "\u4e00" <= c <= "\u9fff")
                hit = max((_t for _t in _srccn if _t and _t in u["cn"]),
                          key=lambda t: len("".join(
                              c for c in t if "\u4e00" <= c <= "\u9fff")),
                          default=None)
                if cj and hit:
                    fc = len("".join(c for c in hit
                                     if "\u4e00" <= c <= "\u9fff"))
                    if fc >= 0.3 * len(cj):
                        continue          # 源中文重排，剔除
                _keep.append(u)
            units = _keep
        # NOTE: 曾尝试"含装饰文字的单元整体剔除"（DOC-A07 红章文案混入历史表
        # 数字行），但 decor 判定会捞到正常文字、误杀大半单元（placed 24→3），
        # 已回滚。正确修法需在表格折叠路径里按宿主几何精确剔除，见任务队列。

        if not units:
            total = 0.0
            np_ = nd.new_page(width=W, height=H)
            np_.show_pdf_page(fitz.Rect(0, 0, W, H), gsrc, gpno)
            paint_decor(np_)
            geom.append({"bands": [[[0.0, 0.0, W, H], [0.0, 0.0, W, H]]],
                         "rails": [], "window": [0.0, W], "cuts": [],
                         "nunits": 0, "far": 0, "maxjump": 0.0,
                         "decor": {"items": decor_items}})
            continue

        # ---- TOC expansion ----
        # 目录页在 mono 里常被 babeldoc 切成"开头几行各成一块 + 其余一整块"，
        # DP 对齐只能把整块挂靠到宿主源块上，而挂靠单元 mi=None，旧代码直接
        # 跳过逐行展开 —— 整页目录于是被当成一个段落折进一条带，这正是"目录
        # 翻译可读性很差"的成因。这里改成两条：
        #   ① 只要"宿主源块"是目录（点导引行 + 编号行都占多数），挂靠单元也展开；
        #   ② 译文取用不再盯单个 mono 块，而是把与该目录块 y 区间相交、且以
        #      编号开头的所有 mono 块按 y 序拼成整页目录译文，再按源行编号序列
        #      切段 —— 无论 mono 怎么切块都能还原成逐行对照。
        def _toc_split_fuzzy(prefixes, cn_t):
            """mono 目录被 babeldoc 折腾坏时的兜底切分（DOC-B02 p1 实测：
            编号被粘成"11 . 目的"、受控印章整行插进条目中间、部分编号整个
            丢失、型号条目没有译文 —— 精确编号切分必然失败）。

            策略：编号锚点 + 窗口分摊 ——
            ① 按顺序贪心找能在 mono 文本里对上号的编号（能对上的少数锚点
              把整页目录切成若干窗口）；
            ② 每个窗口按"数字段"切开取候选（源页已有中文 = 红章/供应商名，
              是 babeldoc 从源页带进 mono 的杂物，按 CJK 核心词剔除；纯拉丁
              候选保留 —— 型号/LED 这类条目本来就没有译文）；
            ③ 窗口内第一个候选归锚行自己，其余按顺序分给窗口里没锚定的行，
              多余候选并入末行。非空段不足 60% 视为失败返回 None。"""
            kill = set()
            for b in ob:
                for l in b["lines"]:
                    t = norm(l["text"])
                    if has_cjk(t):
                        k = re.sub(r"[^\u4e00-\u9fff]", "", t)
                        if len(k) >= 4:
                            kill.add(k)

            def _keep(c):
                c = norm(c).rstrip(".．·")
                if not c:
                    return None
                core = re.sub(r"[^\u4e00-\u9fff]", "", c)
                if core and len(core) >= 2 and any(core in k or k in core
                                                   for k in kill):
                    return None            # 红章/源页已有中文的碎片
                if has_cjk(c):
                    return c
                if re.search(r"[A-Za-z]", c) and len(c) >= 2:
                    return c               # 型号/LED：无译文条目
                return None

            n = len(prefixes)
            anchors = []
            cursor = 0
            for i, pre in enumerate(prefixes):
                m = re.search(r"(?<![0-9.])" + re.escape(pre) + r"(?![0-9])",
                              cn_t[cursor:])
                if m:
                    anchors.append((i, cursor + m.start(), cursor + m.end()))
                    cursor += m.end()
            if not anchors:
                return None
            segs = [""] * n
            windows = []
            for w, (i, st, en) in enumerate(anchors):
                nxt = anchors[w + 1][1] if w + 1 < len(anchors) else len(cn_t)
                windows.append((0 if w == 0 else en, st if w == 0 else en,
                                i, nxt))
            # 重画窗口：锚行 i 的内容窗 = [en_i, 下一锚行 st)；页首未锚行
            # 共享 [0, st_首锚)；逐窗分配。
            segs_tmp = [""] * n
            for w, (i, st, en) in enumerate(anchors):
                lo = en
                hi = anchors[w + 1][1] if w + 1 < len(anchors) else len(cn_t)
                # 未锚行：锚行 i 之后、下一锚行之前
                unanch = [j for j in range(i + 1, n)
                          if j not in {a[0] for a in anchors}
                          and (j < (anchors[w + 1][0] if w + 1 < len(anchors)
                                    else n))]
                cands = [c for c in ( _keep(x) for x in
                         re.split(r"[0-9][0-9.\s]*", cn_t[lo:hi]) ) if c]
                if cands:
                    segs_tmp[i] = cands[0]
                    rest = cands[1:]
                else:
                    rest = []
                for j in unanch:
                    if rest:
                        segs_tmp[j] = rest.pop(0)
                if rest:                   # 多余候选并入最后一个未锚行/锚行
                    segs_tmp[unanch[-1] if unanch else i] += " " + " ".join(rest)
            # 页首未锚行（锚点之前的）
            first_i = anchors[0][0]
            head_rows = [j for j in range(first_i)
                         if j not in {a[0] for a in anchors}]
            if head_rows:
                cands = [c for c in ( _keep(x) for x in
                         re.split(r"[0-9][0-9.\s]*", cn_t[:anchors[0][1]]) )
                         if c]
                for j in head_rows:
                    if cands:
                        segs_tmp[j] = cands.pop(0)
                if cands and head_rows:
                    segs_tmp[head_rows[-1]] += " " + " ".join(cands)
            ok = sum(1 for t in segs_tmp if t.strip())
            if DBG(pno) and os.environ.get("DUAL_TOC_PROBE"):
                print("   TOCPROBE fuzzy anchors=%d nonempty=%d/%d"
                      % (len(anchors), ok, n))
            return segs_tmp if ok >= 0.6 * n else None

        def _toc_cn_all(srcrect):
            hits = []
            for b in mb:
                r = b["rect"]
                if r.y1 <= srcrect.y0 + 2 or r.y0 >= srcrect.y1 - 2:
                    continue
                t = norm(b["text"])
                if seg_number(t) is None:
                    continue            # 非目录条目（红章、题录栏等）不掺进来
                hits.append((round(r.y0, 1), round(r.x0, 1), t))
            hits.sort()
            return " ".join(t for _y, _x, t in hits)

        def _dot_bands(sp, own_ids):
            """把"标题—页码"之间的点导引带按被别的正文墨迹压住的部分切开，
            返回 [(x0, x1), ...] 若干可用子段（按 x 升序）。
            红章、表格竖线常和点线同行，直接整条抹白会连它们一起毁掉；
            切段后中文只落在空着的那一段里，源内容一字不动。斜排/水印
            这类装饰行（行高远大于字号）不计入占用。"""
            bl = sp["origin"][1]
            e0, e1 = sp["rect"].x0, sp["rect"].x1
            ey0, ey1 = bl - 3.2, bl + 0.6
            if e1 - e0 < 12 or e0 < 0:
                return []
            occ = []
            for b2 in ob:
                for l2 in b2["lines"]:
                    if id(l2) in own_ids:
                        continue
                    try:
                        sz2 = max(s2.get("size", 9.0) for s2 in l2["spans"])
                    except ValueError:
                        continue
                    lr2 = line_ink(l2)
                    if lr2.height > 2.6 * max(4.0, sz2):
                        continue                      # 斜排/水印装饰行
                    if (min(lr2.y1, ey1) - max(lr2.y0, ey0)
                            <= 0.45 * (ey1 - ey0)):
                        continue
                    occ.append((lr2.x0 - 1.6, lr2.x1 + 1.6))
            for dr in draw_rects:
                if dr.width > 0.9 * W or dr.height > 0.6 * H:
                    continue                          # 页框/整页底纹
                if (min(dr.y1, ey1) - max(dr.y0, ey0)
                        <= 0.5 * (ey1 - ey0)):
                    continue
                if dr.x1 <= e0 or dr.x0 >= e1:
                    continue
                occ.append((dr.x0 - 1.0, dr.x1 + 1.0))
            occ.sort()
            cut = []
            for a, b in occ:
                if cut and a <= cut[-1][1] + 0.5:
                    cut[-1][1] = max(cut[-1][1], b)
                else:
                    cut.append([a, b])
            bands, x = [], e0
            for a, b in cut:
                if a - x >= 10.0:
                    bands.append((x, min(a, e1)))
                x = max(x, b)
            if e1 - x >= 10.0:
                bands.append((x, e1))
            return bands

        expanded, done_toc = [], set()
        inline_dst = []       # 就地译文的输出坐标盒（供 qa_synth 豁免同行判定）
        _tail_drop = []       # 页尾标题栏/修订表被有意不译的中文（进 geom skip_cn）
        _furn_drop = []       # 有意删掉的模板家具译文（页眉页脚条/题录修订表/
                              # 逐页重复声明/家具词碎片）→ geom skip_cn，
                              # 供 audit_cn 豁免（否则覆盖率缺项里全是它们：
                              # DOC-B05 实测 17 项里 13 项是这类）
        _gtxt_drop = set()    # 落在表格行区间之外、被网格路径丢弃的译文
                              # （小节标题/表上注释；进 geom skip_cn 供 audit_cn
                              #  豁免——这些文本会由标题就地通道/切带另行落位）
        _tbl_fid = []         # 每张渲染出来的表：[R, C, 中文格数, 源侧格数, 总格数]
                              # （审查器 audit_read 的 C2「表格信息缺失」判据 ——
                              #  几何反推在交错版式上不可靠，改由引擎记账）
        _grid_txt = set()     # 已由网格渲染过的单元格译文
        _squeeze = lambda z: re.sub(r"[\s\u00a0]+", "", z)
        # （供单元去重：mono 把同一格拆成"格子译文 + 独立块"时，独立块
        #  会在网格旁边再排一遍 —— 用户 2026-09-29：4.1 的"指示：板载电源
        #  正常…"突兀地出现在表外）
        toc_inplace = []      # 就地替换点导引区的目录译文（不参与切带）
        # 页级行池：拆条与"编号条目锚定"必须能借到**宿主块之外**的源行 ——
        # mono 常把小节标题并进紧随其后的列表块（DOC-B01 的 m19 =
        # "2 外观测试 • 阻焊层 • … • 连接器引脚之间的铣削"），而宿主块只有那
        # 几条项目符号；只认宿主自己的行，条目数就比覆盖行数多 1，整段译文
        # 会系统性下移一行。行池按视觉行（y 距 ≤2.5pt）聚类，装饰行剔除。
        _plines = [l for b in ob if not is_decor_block(b) for l in b["lines"]]
        _plines.sort(key=lambda l: (l["rect"].y0, l["rect"].x0))
        _prows = cluster_rows(_plines)
        erased = []           # 被清除的源内容矩形（像素审计要一并抹白）
        for u in units:
            if u.get("mi") is not None:
                sblk = ob[u["oi"]]
            elif u.get("hlines"):
                sblk = {"lines": u["hlines"]}
            else:
                sblk = None
            if sblk is None or not sblk["lines"]:
                expanded.append(u)
                continue
            rows = cluster_rows(sblk["lines"])
            keyed = []
            for ri, row in enumerate(rows):
                for l in sorted(row, key=lambda x: x["rect"].x0):
                    pn = seg_number(l["text"])
                    if pn:
                        keyed.append((ri, pn))
                        break
            dot_rows = [i for i, row in enumerate(rows) if find_dot_span(row)]
            made = 0
            if DBG(pno) and os.environ.get("DUAL_TOC_PROBE"):
                print("   TOCPROBE rows=%d dot_rows=%d keyed=%d host=%r"
                      % (len(rows), len(dot_rows), len(keyed),
                         norm(sblk["lines"][0]["text"])[:30] if sblk.get("lines") else "?"))
            if len(rows) >= 3 and len(dot_rows) >= max(2, len(rows) // 2) \
                    and len(keyed) >= max(2, len(rows) // 2):
                sr = fitz.Rect(sblk["lines"][0]["rect"])
                for l in sblk["lines"][1:]:
                    sr |= l["rect"]
                cn_all = _toc_cn_all(sr)
                if id(sblk["lines"]) in done_toc:
                    # 同一目录源块已被展开过：若本单元译文已被并集覆盖就直接
                    # 丢弃（否则整页目录会重复一遍），否则保留原样兜底。
                    cj = [c for c in norm(u["cn"])
                          if "\u4e00" <= c <= "\u9fff"]
                    if sum(1 for c in cj if c in cn_all) >= 0.6 * len(cj):
                        continue
                    expanded.append(u)
                    continue
                segs = split_cn_by_numbers([p for _, p in keyed], cn_all)
                if not segs or len(segs) != len(keyed):
                    segs = _toc_split_fuzzy([p for _, p in keyed], cn_all)
                if DBG(pno) and os.environ.get("DUAL_TOC_PROBE"):
                    print("   TOCPROBE cn_all=%d chars segs=%d keyed=%d %s"
                          % (len(cn_all), len(segs) if segs else 0, len(keyed),
                             "OK" if segs and len(segs) == len(keyed) else "MISMATCH"))
                if segs and len(segs) == len(keyed):
                    for (ri, _pref), seg in zip(keyed, segs):
                        cn2 = clean_entry_cn(seg)
                        if not cn2 or not has_cjk(cn2):
                            continue
                        rb = fitz.Rect(rows[ri][0]["rect"])
                        for l in rows[ri][1:]:
                            rb |= l["rect"]
                        ybot = max(line_ink(l).y1 for l in rows[ri])
                        # 就地替换：中文写进该行"标题—页码"之间的点导引区，
                        # 只盖掉那一段点线的墨迹、页码与英文标题一字不动。
                        # 目录因此完全不需要切带 —— 页高不涨、水印/盖章不被
                        # 切碎，也不会把几十条目录挤成一条带里的一坨。
                        sp = find_dot_span(rows[ri])
                        done_inline = False
                        if sp:
                            bands = _dot_bands(sp, {id(l) for l in rows[ri]})
                            if bands:
                                bw, bxx = max((b - a, a) for a, b in bands)
                                size, txt = 7.6, None
                                while size >= 5.0:
                                    ls = wrap(pool, cn2, size, bw - 3.0,
                                              maxlines=1)
                                    if ls:
                                        txt = ls[0]
                                        break
                                    size -= 0.3
                                if txt:
                                    bl = sp["origin"][1]
                                    ers = [[a, bl - 3.2, b, bl + 0.6]
                                           for a, b in bands]
                                    erased.extend(ers)
                                    toc_inplace.append(
                                        {"x": bxx + 1.5,
                                         "y": bl - size * 0.06,
                                         "size": size, "text": txt,
                                         "bold": False, "yrow": ybot,
                                         "ers": ers})
                                    done_inline = True
                                    made += 1
                        if done_inline:
                            continue
                        expanded.append({"rect": rb, "cn": cn2, "toc": True,
                                         "lvl": "toc", "ybot": ybot})
                        made += 1
                    # 只要这一块已被"逐行消化"（就地替换或退化为行单元）就
                    # 绝不再把整块当单元加回去 —— 否则同一段译文既就地写了
                    # 一遍、又作为整块被排一遍（旧版 p2 的重复中文即此）。
                    if made >= 1:
                        done_toc.add(id(sblk["lines"]))
                        continue
            # ---- 多条目中文块：按条目行内拆条 ----
            # mono 常把整个列表折成一块（"• A • B • C"）。整块插在"覆盖到的
            # 最后一行英文"之后，读者得自己在流水账里找对应条目 —— 可读性
            # 很差（第 4 节 27 行条目被折成 5 大块的成因）。按条目分隔符把中文
            # 切成 K 段，按覆盖的英文行数 M 分摊到各自的行后，恢复逐条对照。
            # K 与 M 不等时按比例落位：段本身很短，仍远好于整块流水账。
            # 注意：这里只**记录拆条意图**（u["_pieces"]），真正落位放在切带
            # 赋值之后、MODE 判定之前 —— 拆条会凭空多出几条细切带，若先落位
            # 就会把"整页被巨型图形簇占据"的图纸页判成 reflow、丢掉 overlay。
            # 表格单元（tbl）不拆：拆条是给"段落/列表"用的。
            # DUAL_NO_SPLIT=1 可整体关掉拆条（A/B 对比用）。
            pieces = (None if os.environ.get("DUAL_NO_SPLIT")
                      else _split_bullets(u["cn"])) if not u.get("tbl") else None
            # 编号锚定的廉价前置判据：译文里至少得出现两个 N.N 形式的编号
            _ntok = (0 if u.get("tbl")
                     else len(_NUMTOK.findall(u["cn"])))
            # DUAL_OLD_SPLIT=1 → 回到改动前的口径（宿主行 + ceil 分摊 + 无编号
            # 锚定 + 不动 ybot），供 A/B 与事后归因用。
            _legacy = bool(os.environ.get("DUAL_OLD_SPLIT"))
            if (pieces or _ntok >= 2) and len(_prows) >= 2 and len(rows) >= 2 \
                    and not u.get("tbl"):
                ur = fitz.Rect(u["rect"])
                # ur 对附着单元是 mono 侧的块矩形、对配对单元却是**源块**矩形：
                # 覆盖窗口要用 mono 那一侧（块顶与源行顶对齐，能代表"这块中文译
                # 到了哪儿"），否则源块里夹带的无关行会把窗口撑开（blk31 里就
                # 夹着下一节的 "4 Electrical test"）。
                _mono = (fitz.Rect(u["_mrect"]) if u.get("_mrect")
                         else (fitz.Rect(mb[u["mi"]]["rect"])
                               if u.get("mi") is not None else ur))
                hrect = fitz.Rect(sblk["lines"][0]["rect"])
                for _hl in sblk["lines"][1:]:
                    hrect |= _hl["rect"]

                def _rspan(row):
                    return (min(line_ink(l).y0 for l in row),
                            max(line_ink(l).y1 for l in row))

                def _rrect(row):
                    rb = fitz.Rect(row[0]["rect"])
                    for _rl in row[1:]:
                        rb |= _rl["rect"]
                    return rb

                _rpool = rows if _legacy else _prows
                _rh = sorted(_rspan(r2)[1] - _rspan(r2)[0] for r2 in rows)
                _rh = _rh[len(_rh) // 2] if _rh else 12.0
                # 覆盖窗口：横跨 mono 块的竖直范围。上界最多容许越过宿主首行
                # 2.6 个行高 —— 那一档正是"mono 把小节标题并进了列表块"的签名
                # （DOC-B01 的 m19 vs 宿主 blk29），再往上就是别的内容了。
                if _legacy:
                    lo = ur.y0 - 3.0
                    _hi = ur.y1 + 3.0
                else:
                    lo = max(_mono.y0 - 3.0, hrect.y0 - 2.6 * _rh)
                    _hi = _mono.y1 + 3.0
                cover = [ri for ri in range(len(_rpool))
                         if _rspan(_rpool[ri])[1] >= lo
                         and _rspan(_rpool[ri])[0] <= _hi]
                if len(cover) < 2:
                    # 附着单元的 rect 来自 mono 侧，行距可能整体错位：
                    # 退回"与 rect 中心最近的一行 ±1"
                    mid = (ur.y0 + ur.y1) / 2.0
                    best = min(range(len(_rpool)),
                               key=lambda ri: abs(
                                   (_rspan(_rpool[ri])[0]
                                    + _rspan(_rpool[ri])[1]) / 2 - mid))
                    cover = [ri for ri in (best - 1, best)
                             if 0 <= ri < len(_rpool)]
                # mono 的行距比原文紧，一个中文块的 rect 常"短于"它实际译到的
                # 英文行数 → 另开一个下沿到宿主块底部的宽窗口，专供"编号锚定"
                # 去够那些行（4.3–4.10 的源行就在 mono 块底之后）。
                wide = [ri for ri in range(len(_rpool))
                        if _rspan(_rpool[ri])[1] >= lo
                        and _rspan(_rpool[ri])[0]
                        <= max(_mono.y1, hrect.y1) + 3.0]
                # 覆盖行数不足条目数时向下（必要时向上）补齐，否则多余的条目会
                # 全挤到最后一行、又攒回一小团流水账。补齐量封顶 4 行：K 远大于
                # M 时硬凑会把这批条目撒到无关的行上去。
                if pieces:
                    limit = min(len(pieces), len(cover) + 4)
                    while cover and len(cover) < limit:
                        if cover[-1] + 1 < len(_rpool):
                            cover.append(cover[-1] + 1)
                        elif cover[0] - 1 >= 0:
                            cover.insert(0, cover[0] - 1)
                        else:
                            break
                pl = []
                if pieces and (len(cover) >= 2 or _legacy):
                    # ① 条目符号拆条 → 按比例分摊到覆盖行。
                    # 分摊公式用 floor(i*M/K)：旧写法 ceil((i+1)*M/K)-1 在 M>K
                    # 时会**跳过第一行**（3 条译文铺 4 行时落成第 1/2/3 行），
                    # 每条整体下移一行 —— DOC-B01 第 3 节就是这么错的。
                    M, K = max(len(cover), 1), len(pieces)
                    for i, pc in enumerate(pieces):
                        j = (min(M - 1, -(-(i + 1) * M // K) - 1) if _legacy
                             else min(M - 1, (i * M) // K))
                        rw = _rpool[cover[max(0, j)]]
                        pl.append({"cn": pc, "rect": _rrect(rw),
                                   "ybot": _rspan(rw)[1]})
                    kind = "bullet"
                elif not _legacy:
                    # ② 编号条目锚定 → 每段直接落在它自己那一行（宽窗口优先，
                    # 锚点越多分得越准）
                    segs = _split_by_numbers(u["cn"], _rpool, wide) or \
                        _split_by_numbers(u["cn"], _rpool, cover)
                    if segs:
                        for txt, ri in segs:
                            rw = _rpool[ri]
                            pl.append({"cn": txt, "rect": _rrect(rw),
                                       "ybot": _rspan(rw)[1]})
                        kind = "number"
                if len(pl) >= 2:
                    u["_pieces"] = pl
                    if DBG(pno):
                        print("   SPLIT[%s] K=%d M=%d %r"
                              % (kind, len(pl), max(len(cover), 1),
                                 u["cn"][:46]))
                elif not _legacy:
                    # 没拆开、却明显跨了多行：mono 版式比原文紧凑，单元的 ybot
                    # 常远高于它译到的最后一行 —— 整块会被插到该区域中部
                    # （4.2–4.10 整段中文落在 4.6 之后的成因）。覆盖 ≥3 行且
                    # 高出半行以上时，把落点下移到"最后一个覆盖行的下沿"，
                    # 至少保证整块对应整段。DUAL_NO_PATHFIX=1 可关（A/B 用）。
                    if (len(cover) >= 3 and u.get("lvl") == "body"
                            and not os.environ.get("DUAL_NO_PATHFIX")):
                        _bot = _rspan(_rpool[cover[-1]])[1]
                        if _bot > u["ybot"] + 0.5 * _rh:
                            u["ybot"] = _bot
            expanded.append(u)
        units = expanded

        def emit_toc_inplace(np_, tw, cuts=None, merged=None):
            """画就地替换的目录译文；给定 cuts/merged 时按切带累计位移平移。
            先用白色实心矩形盖住点线墨迹（非破坏性，源文件一字不改），
            再把中文写在原位。"""
            del inline_dst[:]
            for itm in toc_inplace:
                # 位移基准用"覆盖带中心"，与 audit_build 抹白参考页时的
                # 带宽归属判据保持同一基准，否则审计会因半像素错位误报
                ers = itm.get("ers") or []
                yref = itm["yrow"]
                if ers:
                    yref = (ers[0][1] + ers[0][3]) / 2.0
                dy = 0.0
                if cuts and merged is not None:
                    for c in cuts:
                        if c < yref - 0.5:
                            dy += sum(u2["h"] for u2 in merged[c])
                for e0, e1, e2, e3 in ers:
                    np_.draw_rect(fitz.Rect(e0, e1 + dy, e2, e3 + dy),
                                  color=None, fill=(1, 1, 1), width=0)
                x = itm["x"]
                y = itm["y"] + dy
                x1 = x
                for f_, s_ in pool.runs(itm["text"], itm["bold"]):
                    tw.append((x1, y), s_, font=f_, fontsize=itm["size"])
                    x1 += f_.text_length(s_, fontsize=itm["size"])
                inline_dst.append([x - 1.0, y - itm["size"] * 1.05,
                                   x1 + 1.0, y + itm["size"] * 0.45])
        if not units:
            total = 0.0
            np_ = nd.new_page(width=W, height=H)
            np_.show_pdf_page(fitz.Rect(0, 0, W, H), gsrc, gpno)
            if toc_inplace:
                tw = fitz.TextWriter(np_.rect)
                emit_toc_inplace(np_, tw)
            paint_decor(np_)          # 必须在目录白带之后：白带只该盖点线
            if toc_inplace:
                tw.write_text(np_, color=BLUE, overlay=True)
            geom.append({"bands": [[[0.0, 0.0, W, H], [0.0, 0.0, W, H]]],
                         "rails": [], "window": [0.0, W], "cuts": [],
                         "nunits": 0, "far": 0, "maxjump": 0.0,
                         "decor": {"items": decor_items},
                         "erased": erased, "inline_dst": inline_dst})
            continue

        # body window must cover ALL horizontal text on the page (incl. the
        # title-block footer, which starts left of / ends right of the body
        # column), otherwise side strips slice tables and footers apart
        hr = [l["rect"] for b in ob for l in b["lines"]
              if not rotated(l)]
        if hr:
            bx0 = max(0.0, min(r.x0 for r in hr) - 0.5)
            bx1 = min(W, max(r.x1 for r in hr) + 0.5)
        else:
            bx0 = max(0.0, min(u["rect"].x0 for u in units) - 0.5)
            bx1 = min(W, max(u["rect"].x1 for u in units) + 0.5)
        # atomic graphic clusters inside the body column only; margin chains
        # and page frames can then never bridge into the content window
        content = [r for r in ink_rects + img_rects
                   if r.height < 0.60 * H and r.width < 0.92 * W
                   and r.x1 > bx0 and r.x0 < bx1]
        obs = cluster_rects(content, gap=3.0)
        for c in obs:
            for lr in src_line_rects:
                if rotated(lr):
                    continue          # rotated margin column, never absorb
                ov = min(c.x1, lr.x1) - max(c.x0, lr.x0)
                if ov > 0.6 * lr.width and lr.y0 < c.y1 and lr.y1 > c.y0:
                    c |= lr           # figure label rides with its graphic
        obs = cluster_rects(obs, gap=1.5)
        # ---- 表单页：一个连续外框下叠着好几张表 ------------------------
        # 这类页会被并成一个整页大的簇，把每一条切线都吞掉（见
        # split_form_cluster 的说明）。拆回子表后，各表各自成簇、各自
        # 重建网格，切线也回到"每张表下面插一段中文"的正常结构。
        _split, _precise = [], set()
        for c in obs:
            _ps = split_form_cluster(c, page, content)
            for p in _ps:
                _precise.add((round(p.x0, 1), round(p.y0, 1),
                              round(p.x1, 1), round(p.y1, 1)))
            _split.extend(_ps if _ps else [c])
        if len(_split) > len(obs):
            if DBG(pno):
                print("SPLIT-FORM p%d %d -> %d 张子表"
                      % (pno + 1, len(obs), len(_split)))
            obs = _split
        # paired body units riding inside a graphic cluster are table blobs
        # (2-col spec tables land as ONE block in the mono layout): explode
        # them into per-line units so the rules-grid seats every label /
        # content line in its true cell instead of folding a paragraph blob
        ex = []
        for u in units:
            cc = None
            if u["lvl"] == "body" and u.get("mi") is not None:
                r = fitz.Rect(u["rect"])
                for c_ in obs:
                    if (r & c_).get_area() >= 0.6 * max(r.get_area(), 1.0):
                        cc = c_
                        break
            done = False
            if cc is not None:
                # 【2026-09-30 修】散行收集原来只看**配对的那一个** mono 块。
                # 但 babeldoc 常把一张表切成十几个互相重叠的块，1:1 对齐只
                # 配得上其中一块，其余块经 HOST-MERGE 并进同一单元 —— 只按
                # mi 收行会把并进来的行**整段丢掉**（DOC-B02 p5 的 2.10.3
                # IO 分配表：19 行只重建出 10 行，表头与 1/2/3/29~38 行全部
                # 失踪）。改成按 _mrect（各成员 mono 矩形的并集）× 表簇收行。
                _mr = (fitz.Rect(u["_mrect"]) if u.get("_mrect")
                       else fitz.Rect(mb[u["mi"]]["rect"]))
                _mr = fitz.Rect(_mr.x0 - 2, _mr.y0 - 2, _mr.x1 + 2, _mr.y1 + 2)
                lines = []
                for _b2 in mb:
                    for l in _b2["lines"]:
                        t = norm(l["text"])
                        lr = fitz.Rect(l["rect"])
                        if not t or abs(l.get("dir", (1.0, 0.0))[0]) < 0.5:
                            continue
                        cm = fitz.Point((lr.x0 + lr.x1) / 2,
                                        (lr.y0 + lr.y1) / 2)
                        if not fitz.Rect(cc).contains(cm):
                            continue
                        if not _mr.contains(cm):
                            continue
                        if red_line(l):
                            continue        # 受控水印/图章行：不进格子
                        lines.append((lr, t))
                if len(lines) >= 2:
                    for lr, t in lines:
                        ex.append({"rect": fitz.Rect(lr), "cn": t,
                                   "mi": None, "oi": u["oi"], "en": "",
                                   "ybot": lr.y1, "lvl": "body"})
                    done = True
            if not done:
                ex.append(u)
        units = ex
        # grow the clip window so no rule/table edge is nibbled by a strip
        grow = True
        while grow:
            grow = False
            for c in obs:
                if bx1 < c.x1 <= min(W - 1.5, bx1 + 14) and c.x0 < bx1 + 6:
                    bx1 = c.x1 + 0.5
                    grow = True
                if 5 <= c.x0 < bx0 and bx0 - c.x0 <= 10 and c.x1 > bx0 - 6:
                    bx0 = c.x0 - 0.5
                    grow = True
            for r in draw_rects:
                if r.height <= 3.2 and r.width >= 120:      # long h-rules
                    if bx1 < r.x1 <= min(W - 1.5, bx1 + 14):
                        bx1 = r.x1 + 0.5
                        grow = True
                    if 5 <= r.x0 < bx0 and bx0 - r.x0 < 8:
                        bx0 = r.x0 - 0.5
                        grow = True
                # 细杆图形（表格/图框边线：一条边 ≤4.5pt、另一条边够长）
                # 横跨窗口边界时必须整体纳入窗口。窗口是**按横向文字**算出来的
                # （见上面的 hr/_hr0），图纸标题栏这类外框往往比文字宽出 1pt
                # 左右；窗口正好从外框上切过时，"窗内那截"随切带平移、"窗外
                # 那一丝"留在左右边条里不动 —— 于是正文区出现两条 1pt 宽、
                # 长达百 pt 的孤立竖线（用户报的"正文底部半框"两侧）。
                # 判据取窄（≤4.5pt 细杆 + 另一维 ≥12pt），避免把整块图形
                # （印章框、图框内部大矩形）也拖进窗口。
                if min(r.width, r.height) <= 4.5 and max(r.width, r.height) >= 12:
                    if 4 <= r.x0 < bx0 and bx0 - r.x0 <= 6 and r.x1 > bx0:
                        bx0 = max(0.0, r.x0 - 0.5)
                        grow = True
                    if bx1 < r.x1 <= min(W - 4, bx1 + 6) and r.x0 < bx1:
                        bx1 = r.x1 + 0.5
                        grow = True
            for lr in src_line_rects:                        # text tails
                if rotated(lr):
                    continue
                if bx1 < lr.x1 <= min(W - 1.5, bx1 + 14):
                    bx1 = lr.x1 + 0.5
                    grow = True
                elif 5 <= lr.x0 < bx0 and bx0 - lr.x0 <= 10:
                    bx0 = lr.x0 - 0.5
                    grow = True
        # ---- 竖排页边文字的保护（德/法版权声明这类 90° 侧栏）------------
        # 窗口边界**绝不能落进一条竖排文字的内部**：窗口 x 区间 [bx0,bx1] 一旦
        # 从它中间穿过，它的"窗内那半列"会被**每一段 y 与它相交的切带各引一
        # 次**，而各段的 dst 位移不同 —— 页边于是出现一叠错位碎片，原来那条
        # 竖排文字就散了（DOC-B14 实测：第 4 列 x45.8~55.0 被 bx0=51.5 从中间
        # 切开，右半列变成 7 段串在页面中部；用户报"最左侧的竖着英文没有了"）。
        # 也不能反过来"把 bx0 整条推到列外"：页脚修订表的左边框就在 x=52.0
        # （y 686~814），整条推过去会把**它**切出一丝留在页边（正是"半框"那族）。
        # 两者只在 y 上错开 → 按 y 段收窄窗口，并把被收窄掉的那一条用**原位**
        # （src==dst）单独补一次。见 `_emit_band`。
        rotL, rotR = [], []
        # 注意必须取**未清洗**的 pages_ob：`clean_blk` 早把竖排行从 ob 里摘掉了，
        # 拿 ob / src_line_rects 去找一条也找不到（实测 nrot=0）。
        for _b in pages_ob[pno]:
            for l in _b["lines"]:
                if not rotated(l):
                    continue
                # 取"文字矩形 ∪ 墨迹矩形"再各外扩 1pt：`line_ink` 的 y 比行矩形
                # 紧 3pt 上下（实测 471.2 vs 474.7），只按墨迹框补会把该列**顶端
                # 那 3pt 的字头**漏在两块之间的缝里（左右半列都覆盖不到）。
                lr = line_ink(l) | fitz.Rect(l["rect"])
                lr = fitz.Rect(lr.x0, lr.y0 - 1.0, lr.x1, lr.y1 + 1.0)
                if lr.x0 < bx0 < lr.x1:
                    rotL.append((lr.y0, lr.y1, lr.x1 + 0.5))
                if lr.x0 < bx1 < lr.x1:
                    rotR.append((lr.y0, lr.y1, lr.x0 - 0.5))
        if (rotL or rotR) and DBG(pno):
            print("   ROTGUARD L=%s R=%s"
                  % ([[round(v, 1) for v in t] for t in rotL],
                     [[round(v, 1) for v in t] for t in rotR]))
        # bobs = 切带边界不许碰到的"横向墨迹"。斜排/水印这类装饰文字行必须
        # 排除：它们的 bbox 斜跨大半个页面（本样张的水印 y191~630），一旦
        # 当障碍物，safe_cut 会把整页的切线一路顶到页尾 —— 于是几十个表格
        # 单元全折到同一条带、中文被堆到页面另一头。撕裂/碰撞/像素三条质检
        # 全是 0，却根本读不了。装饰行被切带切断属可接受的代价。
        _decids = dec_ids
        bobs = [line_ink(l) for b in ob for l in b["lines"]
                if id(l) not in _decids]
        bobs = [r for r in bobs if r.x1 > bx0 and r.x0 < bx1]
        bobs += [r for r in obs if r.x1 > bx0 and r.x0 < bx1]
        # page-frame vertical rails: excluded from obstacles (they will be
        # cut) but patched seamlessly across every insertion gap afterwards
        rails = []   # (cx, y0, y1) page-frame verticals to redraw stretched
        for it in page.get_drawings():
            r = it["rect"]
            if r.width <= 4.5 and r.height >= 0.5 * H:
                cx = (r.x0 + r.x1) / 2
                if all(abs(cx - y[0]) > 1.5 for y in rails):
                    rails.append((cx, r.y0, r.y1,
                                  it.get("color") or (0, 0, 0),
                                  it.get("width") or 0.7))
        rail_xs = [y[0] for y in rails]
        # 整宽横向模板框线：与 rails（竖线）对称。工作说明书/物料表的"边框
        # 模板"整页宽分隔横线（height<=3, width>=0.5W）在 reflow 下被窗口裁到
        # [bx0,bx1] 而残缺，qa_tears 据此记 HRULE-SPLIT。这里登记它们，build
        # 阶段跨窗口重画整宽、并在 geom 透传给 QA 以像 rails 一样豁免撕裂判定。
        hrules = []   # (yc, x0, x1, color, width) 整宽横向模板框线
        for it in page.get_drawings():
            r = it["rect"]
            if r.height <= 3.0 and r.width >= 0.5 * W:
                yc = (r.y0 + r.y1) / 2.0
                if all(abs(yc - y[0]) > 1.5 for y in hrules):
                    hrules.append((yc, r.x0, r.x1,
                                   it.get("color") or (0, 0, 0),
                                   it.get("width") or 0.7))
        # 排版可读性度量：每个译文块实际落点距它自己的原文块有多远。
        # 切带被推远（密集表/大图把 safe_cut 顶到页尾）时，几十个块会被折叠
        # 到同一条带里放到页面另一头 —— 撕裂/碰撞/像素全 0，却根本没法读。
        # 这是旧质检完全看不见的一类失败，故把它量化进 geom 与汇总。
        jumps = []
        if DBG(pno):
            print("WINDOW", round(bx0, 1), round(bx1, 1), "clusters:", len(obs))
            for r in sorted(obs, key=lambda c: -c.get_area())[:6]:
                print("  CLU", [round(v, 1) for v in r])

        def safe_cut(y):
            """band boundary: no text/cluster ink box may contain the cut.
            (old test had a 0.5pt escape window: an advance to y1+1.4 could
            land inside the NEXT line's ink top, slicing a hair off its
            caps — the phantom sliver lines then read as CN collisions)"""
            cut = y
            if HEAL < 1:                     # legacy formula (A/B regression probes)
                for r in bobs:
                    if r.y0 < cut - 0.5 and r.y1 > cut + 0.5:
                        cut = max(cut, r.y1 + 1.4)
                return cut
            sc = 1.0 if HEAL < 2 else (2.0 if HEAL == 2 else 3.0)
            top, bot, adv = 0.15 * sc, 0.25 * sc, 0.5 * sc
            for _ in range(80):
                moved = False
                for r in bobs:
                    if r.y1 <= r.y0:
                        continue
                    if r.y0 - top < cut <= r.y1 + bot:
                        cut = r.y1 + adv
                        moved = True
                if not moved:
                    return cut
            return cut

        def _free_at(v):
            for r in bobs:
                if r.y1 <= r.y0:
                    continue
                if r.y0 - 0.15 < v <= r.y1 + 0.25:
                    return False
            return True

        def free_cut(y, span=14.0):
            """就近取空切线（用户 2026-09-29）。旧式 safe_cut 只**向下**走：
            表格边框 + 标题栏文字行的墨迹盒在小页面上连成一条链时，切线被
            一路顶穿到标题栏下方 —— 2.1 表的译文于是排到了页尾表格下面、
            3.1 的段落整段落到页底（实测 req=764 被顶到 805.2）。改成先在
            [y, y+span] 内向下找空档，找不到再向上找最近空档；两头都找不到
            才退回 safe_cut。"""
            v = y
            while v <= y + span:
                if _free_at(v):
                    return v
                v += 0.5
            v = y - 0.5
            while v >= y - span:
                if _free_at(v):
                    return v
                v -= 0.5
            return safe_cut(y)

        # 标题落点回收到标题行下沿（用户 2026-09-29：2.9.2 标题译文没紧随、
        # 6.3 排到 6.4 之后 —— 都是"标题+正文"合并块被 PATHFIX 把 ybot 延到
        # 正文末尾所致；这里在 PATHFIX 之后把带编号标题的块收回来）
        for u in units:
            if u.get("_hdr"):
                u["ybot"] = min(u["ybot"], u["_hdr"])
        # ---- 编号标题就地跟随（用户 2026-09-29）----
        # 标题行的墨迹盒带行距、上下游相叠，切线在 ±14pt 内找不到空档 →
        # safe_cut 会一路走到段落末尾（2.9.2 的标题译文落到正文两行之后、
        # 6.3 落到 6.4 之后就是这个）。标题短、右侧多半有空白：把中文直接写
        # 在源页标题行右侧（toc_inplace 加法通道，英文原样保留），并把它从
        # 切带体系里摘出去 —— 标题永远贴着自己的源行。
        _hdrinl, _keep2 = [], []
        for u in units:
            _cu = norm(u.get("cn", ""))
            _ln = None
            if u.get("_hdr") and _cu and len(_cu) <= 46:
                _pre = seg_number(_cu)
                # 只认"标题样"的编号：带小数点的（1.1 / 2.9.2 / 6.3）直接算；
                # 裸数字（"1 概述"、"4 电气测试"）要求源块**不横跨整幅** ——
                # 表格数据行的译文也常以裸数字开头（"2 A1 DIP点阵…"），而数据行
                # 是横跨整幅的（源块宽 ≈ 正文窗宽 94%）。认错了就会把它当标题
                # 写到英文表行上、压住英文（DOC-A09 p1 实测：中文与英文表格行
                # 重叠）。标题则是左缘起的一条短行（DOC-B01 实测 13%~36% 窗宽）。
                if _pre and (re.search(r"\d[.．]\d", _pre)
                             or u["rect"].width <= 0.80 * (bx1 - bx0)):
                    for b2 in ob:
                        for l2 in b2["lines"]:
                            if (l2["rect"].x0 >= u["rect"].x0 - 6
                                    and l2["rect"].y0 >= u["rect"].y0 - 4
                                    and l2["rect"].y1 <= u["rect"].y1 + 4
                                    and seg_number(norm(l2["text"])) == _pre):
                                _ln = l2
                                break
                        if _ln is not None:
                            break
            if _ln is None:
                _keep2.append(u)
                continue
            _ir = line_ink(_ln)
            # 【2026-09-29 修】源页把标题拆成两条 line：编号（"1.1"）与标题词
            # （"PURPOSE"）同 y、不同 x。只拿编号那一段的右端定位，中文会正好
            # 压在标题词上（用户报"标题的翻译和原来标题中文重合"：实测
            # CN x=71~91 覆盖 EN 'INTRODUCTION' x=76~171）。这里把"同一视觉行"
            # 上的所有段并起来，中文写在整条标题的右边。
            _cy = (_ln["rect"].y0 + _ln["rect"].y1) / 2.0
            _obst = bx1
            # 同一视觉行上的段：按 x 排序后**链式合并**，相邻空隙 ≤26pt 就并
            # ——编号与标题词之间的空隙不固定（"1"/"INTRODUCTION" 是 14pt，
            # "5"/"ELECTRICAL DATA" 也是 14pt，旧阈值 12 会让中文压在标题词上；
            # 用户报"标题的翻译和原来标题中文重合"）。
            _row = []
            for b2 in ob:
                for l2 in b2["lines"]:
                    if l2 is _ln or not norm(l2["text"]):
                        continue
                    r2 = l2["rect"]
                    if abs((r2.y0 + r2.y1) / 2.0 - _cy) > 4.0:
                        continue
                    if r2.x0 < _ln["rect"].x0 - 1:
                        continue            # 编号左边的（缩进/项目符号）
                    _row.append((r2.x0, r2, line_ink(l2)))
            _row.sort(key=lambda z: z[0])
            for _x0, r2, ir2 in _row:
                if _x0 <= _ir.x1 + 26.0:
                    _ir = _ir | ir2           # 同一标题行的后续词
                elif r2.x0 < _obst:
                    _obst = r2.x0             # 右边的既有内容 → 上限
            _tx, _sz = None, 7.0
            while _sz >= 4.6:
                _w = _obst - (_ir.x1 + 6.0) - 2.0
                if _w >= 24.0:
                    _ls = wrap(pool, _cu, _sz, _w, maxlines=1)
                    if _ls:
                        _tx = _ls[0]
                        break
                _sz -= 0.3
            if _tx is None:
                # 右侧实在放不下 → 退到"贴右边界"的写法；还不行就交给切带
                _tx2 = None
                _sz2 = 7.0
                while _sz2 >= 4.6:
                    _w2 = bx1 - (_ir.x1 + 6.0) - 2.0
                    if _w2 >= 18.0:
                        _ls2 = wrap(pool, _cu, _sz2, _w2, maxlines=1)
                        if _ls2:
                            _tx2 = _ls2[0]
                            break
                    _sz2 -= 0.3
                if _tx2 is None:
                    _keep2.append(u)
                    continue
                _tx, _sz = _tx2, _sz2
            toc_inplace.append({"x": _ir.x1 + 6.0, "y": _ir.y1 - 1.0,
                                "size": _sz, "text": _tx, "bold": True,
                                "yrow": (_ir.y0 + _ir.y1) / 2.0, "ers": []})
        if len(_keep2) != len(units) and DBG(pno):
            print("   HDR-INLINE p%d n=%d" % (pno + 1,
                                              len(units) - len(_keep2)))
        units = _keep2
        if _grid_txt:
            _squeeze = lambda z: re.sub(r"[\s\u00a0]+", "", z)
            _gset = {_squeeze(z) for z in _grid_txt}
            _gjoin = "".join(_gset)

            def _sh(t2, k=4):
                return {t2[z:z + k] for z in range(max(0, len(t2) - k + 1))}

            _gsh = _sh(_gjoin)
            _k3 = []
            for u in units:
                _t3 = _squeeze(norm(u.get("cn", "")))
                if len(_t3) >= 12 and _gsh:
                    _u3 = _sh(_t3)
                    if (_u3 and len(_u3 & _gsh) / float(len(_u3)) >= 0.8):
                        # 【2026-09-29】这张表的（表头）译文内容**已经由网格
                        # 渲染过了**：mono 把表头整行合成一条、或该单元落在另一
                        # 条切带里没并进簇组 → 表外又排一遍（用户报"表格旁边的
                        # 提示不知道哪里来的"、6.6 表头上方那一串）。用 4-gram
                        # 覆盖率判：80% 以上的片段都能在网格文本里找到 → 重复。
                        continue
                # 只对"长文本"去重：短值（是/否/X/编号）在文档里天然重复，
                # 误删会丢内容（audit_cn 覆盖率会掉）
                if len(_t3) >= 24 and _t3 in _gset:
                    continue        # 这一格已由网格渲染，别在表外再排一遍
                if (len(_t3) >= 24 and _t3[:14] in _gjoin
                        and _t3[-12:] in _gjoin):
                    # 跨格拼接的碎片（首尾都能在网格文本里找到）同样是重复
                    continue
                _k3.append(u)
            if len(_k3) != len(units) and DBG(pno):
                print("   GRID-DEDUP p%d dropped=%d"
                      % (pno + 1, len(units) - len(_k3)))
            units = _k3

        # 【2026-09-29 修】页尾家具过滤必须排在"切点计算 + 跨页溢出"
        # **之前**：否则标题栏里那行的切点会被推到页底、被溢出规则当成
        # 下一页的内容送走；送走之后它已不在 units 里，页尾家具过滤器
        # 再也管不到它 —— 最后被 miss 兜底塞进下一页的空区（DOC-A09 p2
        # 页底凭空多一行、标题栏被顶下 14pt、页高 +11）。
        # ---- 页尾标题栏/修订表：模板家具，不译（用户 2026-09-28，
        # DOC-A09："尾页的表格不需要翻译" —— 其他文档的同类表格从来没译过，
        # 配对路径的家具体检只认 Page/Lang./Format 几个词，Date/Microfilmed/
        # Bill of material 漏网，而且 GRID 重建把整块标题栏译成中文网格）。
        # 判据用**簇顶 + 簇底**：簇顶落在页底 178pt 内、且簇底探到页底
        # 60pt 内 → 标题栏/修订表。实测分布（stroke_rects+cluster_rects
        # 口径）：标题栏簇顶 H-77.8±1（DOC-B02/60890/61783 三家的模板
        # 完全一致），DOC-A09/DOC-B14 的标题栏与上方修订表融成一簇
        # （簇顶 H-169/H-155）；而页底**正文的表**簇顶 ≤H-196 且簇底只到
        # H-85（DOC-A05 p3 的 CPU 规格表）—— y1>H-60 把它挡在外面。
        # 被丢的译文记进 skip_cn 供 audit_cn 豁免；missing 覆盖兜底同样
        # 跳过（不然又从页末附录冒回来）。
        _tailc = [cc for cc in obs if cc.y0 > H - 178.0 and cc.y1 > H - 60.0]

        def _in_tail(r):
            """页尾家具带 = 各标题栏簇的并集外接带（上沿取最顶的那个簇）。
            不能按"单元与簇的面积交"判 —— 标题栏的源块常常**横跨**左右两块
            簇（DOC-A09 p1 的 "PCBA XLOPDB 41.Q / ID Nr. of PCBA" 块
            x33~481，而 split 后的簇只从 x255 起，面积交只有 28%），按面积
            判永远漏。改判**竖直包含**：单元竖直方向 60% 以上落在带内、
            且 x 上与簇沾得到 → 家具。"""
            r = fitz.Rect(r)
            if not _tailc:
                return False
            vh = min(r.y1, H) - max(r.y0, _ty0)
            if vh <= 0 or vh < 0.6 * max(r.height, 0.1):
                return False
            return (min(r.x1, _tx1) - max(r.x0, _tx0)) > -8.0

        if _tailc:
            _ty0 = min(cc.y0 for cc in _tailc)
            _tx0, _tx1 = min(cc.x0 for cc in _tailc), max(cc.x1 for cc in _tailc)
        if DBG(pno) and _tailc:
            print("   TAILC p%d band_y0=%.1f x=%.0f..%.0f n=%d"
                  % (pno + 1, _ty0, _tx0, _tx1, len(_tailc)))
        _rl0 = [l for b in pages_ob[pno] for l in b["lines"] if rotated(l)]
        _hl0 = [l for b in pages_ob[pno] for l in b["lines"]
                if not rotated(l)]
        _ink0 = sum(max(l["rect"].width, l["rect"].height) for l in _rl0)
        _ink1 = sum(max(l["rect"].width, l["rect"].height) for l in _hl0)
        _drawpage = len(_rl0) >= 8 and _ink0 >= 1.2 * _ink1
        if _tailc and not _drawpage:
            _tu = [u for u in units if _in_tail(u["rect"])]
            # 少数派守卫：标题栏只是页尾的**少数**家具。带内单元超过全文
            # 单元的 20% 时，这条"带"多半是本页的主体（横版图纸整页只有
            # 标题栏 —— DOC-A08 转正后 25 个单元全在带里，全滤掉会把
            # overlay 判据的输入掏空、整页退回空页分支），此时不过滤。
            # 守卫豁免（用户 2026-09-28，DOC-B02 p9）：尾页整页几乎只有
            # 标题栏时带内单元占多数，少数派守卫会把过滤整个挡掉。带整体
            # 压在页底 120pt 内、且带内单元全部贴着页底（y0 > H-130）→
            # 就是纯标题栏页，照滤（横版图纸的巨型簇 y0 在页首，进不了
            # _tailc，不受影响；回归盯 DOC-A08/72）。
            _pure_tail = (_ty0 > H - 120.0
                          and all(fitz.Rect(u["rect"]).y0 > H - 130.0
                                  for u in _tu))
            if _tu and (len(_tu) <= 0.2 * max(1, len(units)) or _pure_tail):
                units = [u for u in units if not _in_tail(u["rect"])]
                _tail_drop.extend(norm(u["cn"])[:120] for u in _tu)
                if DBG(pno):
                    print("   TAIL-FURNITURE p%d dropped=%d %r"
                          % (pno + 1, len(_tu),
                             (norm(_tu[-1]["cn"])[:30] if _tu else "")))
        for u in units:
            x0 = max(0, min(u["rect"].x0, bx1 - 60))
            if u["lvl"] in ("h1", "h2", "h3"):
                width = min(bx1, x0 + max(u["rect"].width, 200)) - x0
            else:
                width = min(max(u["rect"].x1, x0 + 90), bx1) - x0
            size, lines, bold, lh = plan_cn(pool, u["cn"], width, u["lvl"])
            u["x0"], u["width"] = x0, width
            u["size"], u["lines"], u["bold"], u["lh"] = size, lines, bold, lh
            u["h"] = len(lines) * size * lh + PAD
            u["cut"] = min(free_cut(u["ybot"] + 0.4), H - 0.5)
        # 落点被推到页尾带的**长**单元：多为 attach 单元沿用了 mono 坐标
        # （mono 版式与源页不同步时 ybot 落在页底）→ 它不是本页的内容，
        # 判为跨页溢出，带到下一页开头（用户 2026-09-29：第 5 页尾页的
        # 蓝牙信标整段堆在页脚之后）
        if pno < npages - 1:
            _k5 = []
            for u in units:
                _cn_len = len(norm(u.get("cn", "")))
                if ((_cn_len >= 30 and u["cut"] > H - 60.0)
                        or (_cn_len >= 60 and u.get("mi") is None
                            and u["rect"].y0 > H - 150.0)):
                    _spill_next.append(norm(u["cn"]))
                    continue
                _k5.append(u)
            if len(_k5) != len(units) and DBG(pno):
                print("   SPILL-NEXT p%d n=%d" % (pno + 1,
                                                  len(units) - len(_k5)))
            units = _k5
        ord_bad = []

        def _ord_scan(us):
            """「译文顺序」机检口径。mono = 参考译文，它的块序就是正确顺序：给每个
            译文单元记下"它在 mono 里的纵向位置"与它最终的切点 `cut`，要求按 mono
            序排好后 cut 单调不减 —— 后一段的切点跑到前一段之上，就是这段中文被
            插到了它前面那段中文的位置之前，也就是"翻译顺序乱了"。
            （DOC-B01 概述段实测：第 2 行插在正文第 3、4 行之间、第 1 行掉到段尾。）

            为什么两族会互相穿插：mono 把一个段落的译文拆成多个行块时，只有一块
            能被 1:1 对齐吃掉成 pair（切点取自**源页** ybot），其余走"挂靠"路径
            （切点取自 **mono** 版式的 y）。源页行距与 mono 行距不同，两族坐标系
            混用，各自求切点就必然互相穿插。撕裂/碰撞/像素/落点距离四项对此全失明
            （它们只问"原文坏没坏、中文压没压字、落点远不远"）。
            须在**拆条落位之后**调用：拆条会把整段中文改成按行锚定，那是设计行为。
            """
            del ord_bad[:]
            grp = {}
            for u in us:
                if u.get("tbl"):
                    continue            # 已进网格表的单元
                rr = fitz.Rect(u["rect"])
                ar = max(rr.get_area(), 1.0)
                # 落进表格/图框原子簇的单元按格就位（cut 不参与排版），序由网格的
                # 行列决定 —— 拿 cut 比 mono 序必然假报（DOC-A05 p9 的表头格实测）。
                if any(ar < 0.45 * max(cc.get_area(), 1.0)
                       and (rr & cc).get_area() / ar >= 0.6 for cc in obs):
                    continue
                mr = u.get("_mrect")
                if mr is None and u.get("mi") is not None:
                    mr = mb[u["mi"]]["rect"]
                if mr is None:
                    continue
                g = grp.get(id(mr))
                if g is None:
                    grp[id(mr)] = [mr.y0, mr.x0, u["cut"], norm(u["cn"])[:22],
                                   mr.y1]
                else:
                    g[2] = min(g[2], u["cut"])
                    g[4] = max(g[4], mr.y1)
            seq = sorted(grp.values(), key=lambda t: (round(t[0], 1), t[1]))
            for i in range(len(seq) - 1):
                # 只有两段在 mono 里**纵向不重叠**时，先后顺序才是有定义的
                # （mono 会把相邻小段并成一个大块，块与标题在 y 上互相覆盖；
                # 那种情况下按 y0 排序是任意的，不能拿来判"顺序颠倒"）。
                if seq[i + 1][0] < seq[i][4] - 1.0:
                    continue
                if seq[i + 1][2] < seq[i][2] - 1.0:
                    ord_bad.append([round(seq[i][2], 1), round(seq[i + 1][2], 1),
                                    seq[i + 1][3]])


        # 拆条前快照：图纸页的 overlay 判据（MODE 处）必须用"没有拆条"的
        # 单元与切带 —— 拆条会凭空多出几条细切带，把"整页被巨型图形簇占据"
        # 的图纸页压成 reflow（DOC-A04_rotsrc 实测：overlay 13 块 → 只放下 4 块）。
        units_pre = list(units)
        import os
        if DBG(pno):
            for u in sorted(units_pre, key=lambda u: u["rect"].y0):
                if "org" not in u and os.environ.get("DUAL_ORGPROBE"):
                    print("   ORGPROBE keys=%s cn=%r rect=%s"
                          % (sorted(u.keys()), u.get("cn", "")[:30],
                             [round(v, 1) for v in u["rect"]]))
                print("UNIT", u.get("org", "?"), u["lvl"],
                      [round(v, 1) for v in u["rect"]],
                      "ybot", round(u["ybot"], 1), "cut",
                      round(u["cut"], 1), repr(u["cn"][:24]))
            for r in sorted(bobs, key=lambda r: r.y0):
                if r.y1 > 340:
                    print("BOB", [round(v, 1) for v in r])

        def _group(us):
            """cluster-aware grouping: short units inside one graphic cluster
            are table cells -> group them BY CLUSTER so each table folds to
            exactly ONE band below the cluster bottom (per-cut grouping used
            to split one table across adjacent bands whose grids then
            overlapped). 返回 (merged, cuts, clu_bot)。"""
            groups, clu = {}, {}
            clu_bot = {}
            for u in us:
                r = fitz.Rect(u["rect"])
                ar = max(r.get_area(), 1.0)
                best, bestf = None, 0.0
                # 带编号标题的单元不进簇：进了就会被"簇底切线"拉到表格/正文
                # 末尾（用户 2026-09-29：2.9.2 标题译文跑到正文两行之后、
                # 6.3 跑到 6.4 之后）——标题要贴着自己的源行
                if (u["lvl"] == "body" and not u.get("_hdr")
                        and r.height <= 45.0):
                    for cc in obs:
                        if ar >= 0.45 * max(cc.get_area(), 1.0):
                            continue
                        f = (r & cc).get_area() / ar
                        if f >= 0.6 and f > bestf:
                            best, bestf = cc, f
                if best is not None:
                    key = (round(best.x0, 1), round(best.y0, 1),
                           round(best.x1, 1), round(best.y1, 1))
                    clu.setdefault(key, [[], best])[0].append(u)
                    clu_bot[id(u)] = best.y1      # 该单元所属原子簇的底边
                else:
                    groups.setdefault(cut_key(u["cut"]), []).append(u)
            cuts = sorted(groups)
            merged, last = {}, None
            for c in cuts:
                if last is not None and c - last < 0.6:
                    merged[last] += groups[c]
                    continue
                merged.setdefault(c, []).extend(groups[c])
                last = c
            for k, (us2, cc) in sorted(clu.items(),
                                       key=lambda kv: kv[1][1].y0):
                if len(us2) < 2:
                    for u in us2:
                        merged.setdefault(cut_key(u["cut"]), []).append(u)
                    continue
                c = min(free_cut(max(max(u["ybot"] for u in us2), cc.y1) + 0.4),
                        H - 0.5)
                tgt = None
                for c2 in merged:
                    if abs(c2 - c) < 2.5:
                        tgt = c2
                        break
                merged.setdefault(
                    tgt if tgt is not None else cut_key(c), []).extend(us2)
            cuts = sorted(merged)
            # absorb singleton labels whose cut jittered 1-2pt off a scatter
            # group and whose rect sits inside that group's envelope (orphan
            # 'STRIPED'-style cells at the bottom edge of a table)
            # 注意：本循环会 `del merged[c2]`，所以两个遍历都必须**每轮重新取
            # 快照并查在不在**——否则后面再轮到那个已被删掉的键就直接 KeyError
            # （旧代码用 `for c in sorted(merged)`，快照固定，属于潜伏崩溃）。
            for c in list(merged):
                if c not in merged or len(merged[c]) < 2:
                    continue
                for c2 in list(merged):
                    if c2 == c or c2 not in merged or len(merged[c2]) != 1 \
                            or not (abs(c2 - c) < 2.5):
                        continue
                    u2 = merged[c2][0]
                    if u2["lvl"] != "body":
                        continue
                    env2 = fitz.Rect(merged[c][0]["rect"])
                    for u in merged[c][1:]:
                        env2 |= u["rect"]
                    r2 = fitz.Rect(u2["rect"])
                    if (r2 & env2).get_area() >= 0.6 * max(r2.get_area(), 1.0):
                        merged[c].append(u2)
                        del merged[c2]
            return merged, sorted(merged), clu_bot

        # 先按"拆条前"的单元分组，供 MODE 判定与覆盖兜底使用
        merged, cuts, clu_bot = _group(units_pre)

        # ---- 覆盖兜底（两种排版模式共用）：mono 里明明译好了、却没被任何
        # 单元带走的中文块。最常见成因是源段被切成一行一块、而 babeldoc 把
        # 它们合并成一个整段块，DP 对齐只能一一配对、其余整段被丢弃。宁可列
        # 到页末，也绝不让译文静默丢失。模板脚注（CJKBOIL）仍按设计有意删掉。
        # 注意：就地替换进目录点导引区的中文也是"已落地"，必须计入 have，
        # 否则整块目录译文会被判成漏项、又堆到页末（正是旧版的顽症）。
        have = ("".join(u["cn"] for u in units)
                + "".join(b["text"] for b in ob)
                + "".join(i["text"] for i in toc_inplace))
        missing = []
        for b in mb:
            t = fix_cn(norm(b["text"]))
            cj = [c for c in t if "\u4e00" <= c <= "\u9fff"]
            if len(cj) < 3 or CJKBOIL.search(re.sub(r"\s+", "", t)):
                continue
            if is_furniture_cn(t):
                _furn_drop.append(t)     # 有意删 → audit_cn 豁免
                continue                # 标题栏家具碎片（页/语言/目录），有意不搬
            if boiler_rep(t, rep_m[t[:120]]):
                _furn_drop.append(t)     # 逐页重复的模板/法律声明，有意删
                continue                    # 逐页重复的模板/法律声明译文
            if _tailc and _in_tail(b["rect"]):
                _tail_drop.append(t)        # 有意不译 → audit_cn 豁免
                continue                    # 页尾标题栏/修订表（同上，有意不译）
            rr = fitz.Rect(b["rect"])
            if rr.y1 < 34 or rr.y0 > H - 58:
                _furn_drop.append(t)     # 页眉/页脚条（盖章、页码带）
                continue                    # 页眉/页脚条（盖章、页码带），同单元级规则
            if pno == 0 and rr.y0 > H - 235:
                _furn_drop.append(t)     # 封面题录/修订表/版权条
                continue                    # 封面题录/修订表/版权条（同单元级规则）
            # 宿主是模板样板（题录栏/修订表）→ 同样不补。
            # 注意：**不能**用"宿主源块含中文"来判——配对路径有同一条件，
            # 两处叠加会把"英文正文块里混了一点中文（水印/盖章）"的整块
            # 译文静默丢掉（p11 的 MTBF 段就这样消失过）。
            host = None
            for b2 in ob:
                sr = b2["rect"]
                if (sr.y0 <= rr.y0 + 4 and rr.y1 <= sr.y1 + 14
                        and sr.x0 <= rr.x0 + 14 and rr.x1 <= sr.x1 + 14):
                    host = b2
                    break
            if host is not None and (len(host["text"]) < 120
                                     and BOILER_RE.search(host["text"])
                                     and max_span_size(host) < 10.2):
                continue
            if sum(1 for c in cj if c in have) / len(cj) >= 0.60:
                continue
            if os.environ.get("DUAL_MISS2DBG") and DBG(pno):
                print("   MISS-SRC p%d rect=%s %r"
                      % (pno + 1, [round(v, 1) for v in rr], t[:44]))
            have += t
            missing.append((t, fitz.Rect(b["rect"])))

        # ---- 图纸页专用：就地叠加中文（overlay），完全不切带 --------------
        # 一张 PCB/机械图往往是一个横跨大半个页面的巨型图形簇：所有切线都
        # 被 safe_cut 推到簇底，切带结构塌成 1~2 条，重排会把几十个标签挤成
        # 一坨。此时改成"原页一动不动 + 中文塞进最近的空白"，撕裂天然为 0。
        # 判据二：整页以竖排文字为主（图纸没被 rot_pages 转正、或用户勾掉
        # 了转正）——切带窗口只会按横排行算，必然把竖排内容拦腰切断。
        # _rl/_hl/_ink 与 drawing_page 已在页尾家具过滤处算过（那里必须
        # 先知道本页是不是图纸页 —— 图纸页不做家具过滤，见上），直接复用。
        drawing_page = _drawpage
        # 判据三：切带结构塌陷。原式 `cuts <= max(2, units//6)` 几乎恒为真
        # ——一张普通表格页的几十个格单元本来就折进同一条带，units 一大、
        # cuts 就显得"少"，于是所有密集页都被误判成图纸页，而 overlay 在密集
        # 页上找不到空白，只能把整页译文堆到页末（QA 却仍报 0/0/0）。真正的
        # 图纸是"全页被一个巨型图形簇占据、压根没有可用横带"，即塌成 1~2 条。
        cladom = max([c.get_area() for c in obs] or [0.0])
        # 条目拆条产生的细单元不计入本判据：它只是把同一段译文切细，与
        # "整页是否被巨型图形簇占据"与拆条无关：直接用拆条前的单元/切带判。
        # 注意：DOC-B05（超密集受控表单）曾试以"簇面积占优"替代本判据，
        # 但表单 cladom/page≈19% 被误判成表格→reflow，落点过远 57%（>120pt），
        # 远差于 overlay（decor 行排除后 far=0%、placed=5）。故表单类密集页
        # 仍走 overlay（固定网格塞中文），本判据保留。
        # 单元数上限：overlay 只适合"少标签"页面（表单 12~25 个）。行级文本
        # 页（DOC-A09 工作说明书 156 个单元）切带塌陷同样会触发 degen，但
        # 文本页排满了行、overlay 没有空白走廊可落字（placed 1/156），必须
        # 回 reflow 逐行插入。上限 100 兼顾 Z 系列图纸（96 行竖排，另有 rot
        # 转正判据兜底）。
        degenerate = (len(units_pre) >= 8 and len(cuts) <= 2
                      and len(units_pre) <= 100)
        # 判据四：可填表单页（用户要求，DOC-A07 实测）。要留白填数据的
        # 受控表单（PCB History 等）：大片"有格线没文字"的待填格子。
        # 这类页切带必把贯通框线切碎、GRID 重建又会画出蓝色复刻表毁掉
        # 可填性——唯一正解是 overlay：源页 1:1 不动，标签译文按三条规矩
        # 就地落字（空白待填格是障碍物，天然被保护）。
        _form_fill = False
        _eratio = 1.0          # 空白待填面积比（MODE 探针可能不算，见下）
        if os.environ.get("DUAL_NO_FORMFILL", "0") != "1" \
                and len(units_pre) >= 8:
            _fr_p = content_frame(ink_rects, W, H)
            _tk_p = list(src_line_rects) + \
                [line_ink(l) for b in mb for l in b["lines"]]
            _empty_p = empty_form_cells(ink_rects, W, H, _tk_p, _fr_p)
            # 数量 + 面积双条件：只有"大片的空白待填格"（总面积 ≥12% 页面）
            # 才是可填表单。正文文档的标题栏空格子小而零散，凑不够面积，
            # 不会把正文页误判成表单（DOC-B01 实测）。
            _earea = sum(fitz.Rect(c).get_area() for c in _empty_p)
            _eratio = _earea / max(1.0, (bx1 - bx0) * H)
            # 阈值 45%：可填表单（DOC-A07 历史 55%、DOC-B09/60/63/65/67
            # 56%）在其上；目录/文件名类文档（DOC-X01 38%、DOC-B10 37%）
            # 在其下——它们此前的逐行对照已验收，不走 overlay
            _form_fill = len(_empty_p) >= 6 and _eratio >= 0.45
            if DBG(pno):
                print(f"  FORMFILL-PROBE p{pno + 1}: empty={len(_empty_p)} "
                      f"area_ratio={_eratio:.1%}")
        # ---- 图纸注释模式（2026-09-30，用户点名 Z_45335259/269/DOC-A08/72）----
        # 装配图纸页用户要求："只译注释性文本、中文紧贴原文（写到英文块右侧
        # 空白并排）"；BOM 元件清单/标题栏/Ident 表/页边版权行一律保持英文。
        # 此前这些页走 reflow/overlay：BOM 译文成堆重复、注释译文被甩进文本
        # 堆（远抛）。本通路整页 1:1 复制，仅把"注释段落"的中文并排写入。
        # 自动判定（两个条件缺一不可）：
        #   ① 标题栏关键词 ≥3（Scale/Sheets/Assembly drawing/…）；
        #   ② **图框坐标带记号 ≥6**：页缘 28pt 内的单/双字符文本（CAD 图框
        #      的 A-D/1-8 分区字母数字，实测图纸 11 个）。①会误中受控表单
        #      （DOC-B05 关键词 6 个、还有贯通格线）——②是图纸独有的铁证
        #      （表单/文档实测 0~1 个）。DUAL_DRAW=1/0 强制开关。
        _drawnote = False
        if os.environ.get("DUAL_DRAW", "auto") != "0":
            _src_txt = page.get_text("text")
            _kw = len(re.findall(
                r"\bscale\b|\bsheets?\b|assembly drawing|drawing no|"
                r"modification|\bka no\b|auth\.?\s*group",
                _src_txt, re.I))
            _ticks = 0
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for l in b["lines"]:
                    t = "".join(s["text"] for s in l["spans"]).strip()
                    if 0 < len(t) <= 2 and (
                            l["bbox"][1] < 28.0 or l["bbox"][3] > H - 28.0
                            or l["bbox"][0] < 28.0
                            or l["bbox"][2] > W - 28.0):
                        _ticks += 1
            _drawnote = _kw >= 3 and _ticks >= 6
        if os.environ.get("DUAL_DRAW") == "1":
            _drawnote = True
        if DBG(pno):
            print(f"MODE p{pno + 1} units={len(units_pre)} cuts={len(cuts)} "
                  f"cladom={cladom:.0f}/{max(1.0, (bx1 - bx0) * H):.0f} "
                  f"rot={len(_rl0)} drawing={int(drawing_page)} "
                  f"degen={int(degenerate)} form_fill={int(_form_fill)} "
                  f"drawnote={int(_drawnote)} "
                  f"-> {'OVERLAY' if (drawing_page or degenerate or _form_fill) else 'reflow'}")
        if _drawnote:
            # ① 注释段落探测（通用）：≥2 行、字号 ≥6、左缘对齐、行距一致，
            #    且段落矩形内**没有格线穿过** —— 标题栏/表格内文本被格线排除；
            #    BOM 小字（sz<6）、Notice（sz 5.6）、单行视图名自然不入选。
            _pl = []
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for l in b["lines"]:
                    if abs(l.get("dir", (1.0, 0.0))[0]) < 0.5:
                        continue
                    t = "".join(s["text"] for s in l["spans"]).strip()
                    if not t:
                        continue
                    sz = max(float(s["size"]) for s in l["spans"])
                    if sz < 6.0:
                        continue
                    _pl.append((fitz.Rect(l["bbox"]), sz))
            # 按 y0 排序即可：x 对齐由分组内的 ±3pt 容差把关（按 x 分桶会因
            # 边界值把同一段落劈成两半 —— 159.1→桶27、158.6→桶26，实测劈开）
            _pl.sort(key=lambda v: v[0].y0)
            if DBG(pno) and os.environ.get("DUAL_DRAWDBG"):
                for _r, _s in _pl:
                    print("   DRAW-PL y=%.1f..%.1f x=%.1f sz=%.1f"
                          % (_r.y0, _r.y1, _r.x0, _s))
            _paras = []
            _used = [False] * len(_pl)
            for i in range(len(_pl)):
                if _used[i]:
                    continue
                _grp = [_pl[i]]
                _used[i] = True
                for j in range(i + 1, len(_pl)):
                    if _used[j]:
                        continue
                    _r2, _s2 = _pl[j]
                    if (abs(_r2.x0 - _grp[0][0].x0) <= 3.0
                            and 0.0 < _r2.y0 - _grp[-1][0].y1
                            <= 1.95 * max(_grp[-1][1], _s2)):
                        _grp.append(_pl[j])
                        _used[j] = True
                if len(_grp) < 2:
                    continue
                _pr = _grp[0][0]
                for _r2, _s2 in _grp[1:]:
                    _pr |= _r2
                # 格线穿过 → 表格/标题栏内文本，不译。用**精确**并集矩形
                # （不外扩：外扩 2pt 会把紧邻的涂覆符号边框算成"穿过"）；
                # 阈值 45%：真正的表格分格线在图纸上跨度≈整格宽（≥段宽），
                # 而**标题下划线**只有标题那么长（DOC-A08 "Process:" 下划线
                # 56pt vs 段宽 220pt=25%）—— 25% 阈值会把带下划线的标题块
                # 整块误杀（实测 Process 块因此没译）。
                _crossed = False
                for r in ink_rects:
                    ix = fitz.Rect(r) & _pr
                    if not ix.is_valid or ix.is_empty:
                        continue
                    if ix.get_area() >= 0.6 * _pr.get_area():
                        _crossed = True
                        break
                    if (ix.height <= 3.5
                            and ix.width >= 0.45 * _pr.width):
                        _crossed = True
                        break
                    if (ix.width <= 3.5
                            and ix.height >= 0.45 * _pr.height):
                        _crossed = True
                        break
                if not _crossed:
                    _paras.append(_pr)
            if DBG(pno):
                for _pr in _paras:
                    print("   DRAW-PARA %s" % [round(v, 1) for v in _pr])
            # ② 段落 → 中文：取锚定在段落里的单元，按阅读序、按句切开后
            #    重新换行；写到段落**右侧空白**（用户拍板：中文放英文右侧）
            _items = []
            _n_note = 0
            _skip = []
            for u in units_pre:
                if any("\u4e00" <= c <= "\u9fff" for c in (u.get("cn") or "")):
                    _skip.append(u["cn"])
            for t, _r in missing:
                _skip.append(t)
            for _pr in _paras:
                _seg = []
                # 直接从 **mono 页** 收集段落矩形内的中文行：注释块整体落在
                # 页尾家具带里时（本例 band_y0=460 < 注释 y494），未配对的行
                # 会被家具过滤器提前丢掉、配对行又挂在 attach 单元上 ——
                # units/missing 两边都凑不齐（实测 p1 只剩 1 句、p2 缺最后
                # 一句）。mono 行与源页同坐标系，按位置框定最可靠。
                for b in mb:
                    for l in b["lines"]:
                        # 过 fix_cn：兼容字归一 + cn_fix.json 中文纠错表
                        # （图纸注释里 mono 的错译——"焊膏钢网将由…"漏 not、
                        #  ": 不含…"漏"不涂覆"、"人口最多/最常用"——靠它纠正）
                        _cn = fix_cn(norm(l.get("text") or ""))
                        if not _cn or not any("\u4e00" <= c <= "\u9fff"
                                              for c in _cn):
                            continue
                        _c = fitz.Rect(l["rect"])
                        if (_pr.x0 - 8 <= (_c.x0 + _c.x1) / 2 <= _pr.x1 + 60
                                and _pr.y0 - 5 <= (_c.y0 + _c.y1) / 2
                                <= _pr.y1 + 5):
                            _seg.append((round(_c.y0, 1), round(_c.x0, 1),
                                         _cn))
                if not _seg:
                    continue
                _seg.sort()
                if DBG(pno):
                    print("   DRAW-SEG n=%d %s" % (
                        len(_seg), [(round(y, 1), c[:14])
                                    for y, x, c in _seg[:4]]))
                # 右邻障碍：竖线 / 源文块（标题栏左缘等）→ 可写宽度
                _tx1 = bx1 - 2.0
                for r in ink_rects:
                    if (r.x0 > _pr.x1 - 1.0 and r.width <= 3.5
                            and min(r.y1, _pr.y1) - max(r.y0, _pr.y0)
                            >= 0.5 * _pr.height):
                        _tx1 = min(_tx1, r.x0 - 2.0)
                for b2 in ob:
                    _r2 = fitz.Rect(b2["rect"])
                    if (_r2.x0 > _pr.x1 + 2.0
                            and min(_r2.y1, _pr.y1) - max(_r2.y0, _pr.y0)
                            >= 0.3 * _pr.height):
                        _tx1 = min(_tx1, _r2.x0 - 2.0)
                _tw = _tx1 - (_pr.x1 + 3.0)
                if _tw < 40.0:
                    if DBG(pno):
                        print("   DRAW-FIT tx1=%.1f tw=%.1f -> 太窄放弃"
                              % (_tx1, _tw))
                    continue
                _sents = []
                for _y, _x, _cn in _seg:
                    for _pc in _cn.replace("。", "。\n").split("\n"):
                        _pc = _pc.strip()
                        if _pc:
                            _sents.append(_pc)
                if DBG(pno):
                    print("   DRAW-FIT tx1=%.1f tw=%.1f sents=%d"
                          % (_tx1, _tw, len(_sents)))
                _got = None
                _cands = []
                for _sz in (6.8, 6.2, 5.6, 5.0, 4.6, 4.4):
                    _ls = []
                    for _pc in _sents:
                        _ls.extend(wrap(pool, _pc, _sz, _tw) or [])
                    if _ls and len(_ls) * _sz * 1.18 <= _pr.height + 8.0:
                        _cands.append((_sz, _ls))
                if _cands:
                    # 优先选"没有孤行标点"（末行只剩一个 。/、 之类）的字号
                    # —— 视觉上孤行标点比字号小半档难看得多
                    _got = next((_c for _c in _cands
                                 if all(len(ln.strip()) > 1
                                        for ln in _c[1])), _cands[0])
                if not _got:
                    continue
                _sz, _ls = _got
                _yy = _pr.y0 + 1.0
                for _ln in _ls:
                    _items.append((_pr.x1 + 3.0, _yy + _sz * 0.95, _sz, _ln))
                    _yy += _sz * 1.18
                _n_note += 1
            # ③ 渲染：整页 1:1 复制 + 注释中文（无附录、无自愈、页高不变）
            fh = H
            np_ = nd.new_page(width=W, height=fh)
            np_.show_pdf_page(fitz.Rect(0, 0, W, H), gsrc, gpno)
            tw = fitz.TextWriter(np_.rect)
            for x, y, size, ln in _items:
                xx = x
                for f, s in pool.runs(ln):
                    tw.append((xx, y), s, font=f, fontsize=size)
                    xx += f.text_length(s, fontsize=size)
            emit_toc_inplace(np_, tw)
            paint_decor(np_)
            tw.write_text(np_, color=BLUE, overlay=True)
            if DBG(pno):
                print(f"  DRAW-NOTE p{pno + 1} paras={len(_paras)} "
                      f"placed={_n_note} lines={len(_items)} "
                      f"skip_cn={len(_skip)}")
            lvl_count["overlay"] += _n_note
            all_u = list(units_pre)
            geom.append({"bands": [[[0.0, 0.0, W, H], [0.0, 0.0, W, H]]],
                         "rails": [], "window": [0.0, W], "cuts": [],
                         "overlay": 1, "page_h": fh,
                         "erased": erased, "inline_dst": inline_dst,
                         "decor": {"items": decor_items},
                         "nunits": len(units_pre),
                         "tbl_fid": _tbl_fid,
                         "far": 0,
                         "maxjump": 0.0,
                         "appendix": 0,
                         "ov_append": False,
                         "tail_y0": None,
                         "skipped": len(_skip),
                         "placed": _n_note,
                         "frame": None,
                         "app_cn": 0,
                         "all_cn": sum(1 for t in _skip
                                       for c in t
                                       if "\u4e00" <= c <= "\u9fff"),
                         "skip_cn": _skip})
            n_units += _n_note
            continue

        if drawing_page or degenerate or _form_fill:
            # 图纸页：不拆条，原样用拆条前的单元做就地叠加
            units = units_pre
            for u in units:
                jumps.append((id(u), u, u["cut"]))
                lvl_count[u["lvl"]] += 1
            # 图纸里的表格线/外框常是零高度的单线路径、或**空心**的章框/图框
            # （各自按描边宽度膨胀 / 只算四条边，见 stroke_rects）。不这么做
            # 中文压在细线上会被像素审计报 LOST，而空心章框更会把整片格子的
            # 内部封死、把中文挤到别的格去。
            fine = src_line_rects + ink_rects + img_rects

            def _ov_boxes(items):
                return [fitz.Rect(x, y - sz * 0.98,
                                  x + pool.width(ln, sz) + 0.4, y + sz * 0.36)
                        for x, y, sz, ln in items]

            # ---- 图纸/表单落字三条规矩（用户要求）------------------------
            # ① 中文不许出图框：图框外是页边坐标带/页脚，落在那儿只会让人
            #    以为画错了（页末附录同理，所以有图框时宁可这一块不译）。
            # ② 不许占用空白格子：表单里那些有格线、没文字的格子是留着填
            #    信息的，直接把整格当障碍物，落字自然避开。
            # ③ 没译出中文的（纯编号/日期/代号）不落字：本来就没翻译。
            _fr = content_frame(ink_rects, W, H)
            _tink = list(src_line_rects) + \
                [line_ink(l) for b in mb for l in b["lines"]]
            _empty = empty_form_cells(ink_rects, W, H, _tink, _fr)
            # 空格障碍只保护**真可填表单**（DOC-A07/60/70 等受控表单：空格
            # 全是矮格子，h_max≤38pt）。图纸页（DOC-A08/72 实测）的格线
            # lattice 会把图框坐标带、图内大片留白也围成 50~160pt 的"巨型空格"，
            # 全设障碍后整页封死（25 块译文只落得下 1 块，覆盖率硬闸门 FAIL）。
            # 判据：空格里出现 h>60pt 的巨型格（+页面有图框）→ 图纸页 →
            # 空格障碍全部不设（图纸留白本来就该落字）；矮格表单页维持原状。
            # DUAL_EMPTY_MODE=all/none 可强制（A/B 用）。
            _em_mode = os.environ.get("DUAL_EMPTY_MODE", "auto")
            _em_hmax = max((r.height for r in _empty), default=0.0)
            if _em_mode == "none" or (_em_mode == "auto"
                                      and _em_hmax > 60.0):
                _empty = []
            # 格线坐标：落字只许在自己那一格里（用户要求"不要占用别的单元格"）
            _hy, _vx = table_rules(ink_rects, W, H, _fr)
            if DBG(pno):
                print("  OVERLAY-FRAME p%d fr=%s empty_cells=%d rules=%dx%d"
                      % (pno + 1,
                         [round(v, 1) for v in _fr] if _fr else None,
                         len(_empty), len(_hy), len(_vx)))

            _ovdbg = os.environ.get("SYNTH_OVDBG", "")
            _ovdbg_hit = []

            def _ov_free(boxes, blocked, band=None):
                if _fr is not None:
                    for b in boxes:
                        if not _fr.contains(b):
                            if _ovdbg:
                                _ovdbg_hit.append(("frame", fitz.Rect(b)))
                            return False
                for b in boxes:
                    for o in (blocked if band is None else band):
                        ix = fitz.Rect(b)
                        ix.intersect(o)
                        if (ix.is_valid and not ix.is_empty
                                and ix.get_area() > 0.35):
                            if _ovdbg:
                                _ovdbg_hit.append(
                                    ("hit", fitz.Rect(b), fitz.Rect(o)))
                            return False
                return True

            # 稀疏索引：按 8pt 行桶存障碍物，走廊搜索只查所在行带
            _buck, _tall = {}, []

            def _ov_add(rr):
                if rr.height > 24.0:
                    _tall.append(rr)
                    return
                for k in range(int(rr.y0 // 8), int(rr.y1 // 8) + 1):
                    _buck.setdefault(k, []).append(rr)

            for rr in fine:
                _ov_add(rr)
            for rr in _empty:                 # ② 空格子整格设为障碍
                _ov_add(rr)

            def _ov_band(y0, y1):
                out = [r for r in _tall if r.y1 > y0 and r.y0 < y1]
                for k in range(int(y0 // 8) - 1, int(y1 // 8) + 2):
                    out.extend(_buck.get(k, ()))
                return out

            _wc = {}

            def _ov_wrap(u, size, w):
                key = (id(u), round(size, 1), int(w))
                v = _wc.get(key)
                if v is None:
                    v = wrap(pool, u["cn"], size, w) or []
                    _wc[key] = v
                return v

            def _ov_try(u, x, ytop, w, blocked, maxlines=4):
                """在 (x, ytop) 起、宽 w 的区域内排中文；成功返回行项目"""
                big = u["lvl"] in ("h1", "h2", "h3")
                band = _ov_band(ytop - 2.0, ytop + 4.0 + maxlines * 9.0)
                # 下限 5.0pt：与"可读字号下限"同一口径。以前一路缩到 4.4pt，
                # 挤进各种歪位置看着就是一团蚂蚁脚 —— 现在放不下宁可这块不译。
                for size in ((8.0, 7.2, 6.4) if big else
                             (6.8, 6.2, 5.6, 5.0)):
                    lines = _ov_wrap(u, size, w)
                    if not lines or len(lines) > maxlines:
                        continue
                    items, y = [], ytop + size * 0.95
                    for ln in lines:
                        items.append((x, y, size, ln))
                        y += size * 1.18
                    if _ov_free(_ov_boxes(items), blocked, band):
                        return items
                return None

            def _in_cell(got, cell):
                if cell is None:
                    return True
                for b in _ov_boxes(got):
                    if not cell.contains(b):
                        return False
                return True

            def _ov_place(u, blocked):
                """只在**源文自己那一格**里落字（用户要求：别占别的单元格、
                别飘到几十 pt 外）。顺序：同行右侧并排 → 本格内正下方 →
                正上方 → 本格其余空位；都不行就返回 None（宁可这块不译）。"""
                # 锚点与"自己那一格"都按**源文那一整块英文**算，而不是按每块
                # 中文碎片自己的矩形：碎片各不相同 → 格子各不相同 → 互相抢位、
                # 谁也放不下（实测：同一格里的"PCB名称/类型"和"指定"互相挡死，
                # 14 块只落得下 2 块）。按源块算，同一段英文的几行中文就能叠在
                # 一起、整齐落在自己格内。
                r = fitz.Rect(u["rect"])
                _oi = u.get("oi")
                if isinstance(_oi, int) and 0 <= _oi < len(ob):
                    r = fitz.Rect(ob[_oi]["rect"])
                cell = source_cell(r, _hy, _vx)
                w0 = max(r.width, 34.0)
                cy = (r.y0 + r.y1) / 2.0
                room_r = (cell.x1 - r.x1 - 3.0) if cell is not None else 30.0
                cands = []
                # ① 同行右侧："English 中文" 并排 —— 最好读，也最不容易跑位
                if room_r >= 26.0:
                    cands.append((r.x1 + 2.5, cy - 4.0, min(room_r, 170.0)))
                # ② 本格内多点扫描：正下方 / 正上方（各档 dy）× 格内几个 x 位
                #    （源文左边、格左边、格中线、格右边）。只扫源文左边缘一处
                #    不够 —— 格里的可用空白常常在源文旁边（如红章文字右侧）。
                #    `+1.3` 是给"文字正好从格线起排"的格留的：落字框左边与格线
                #    严丝合缝时，细线膨胀后必然重叠 → 该候选位必被挡。
                xs = [r.x0 + 1.3]
                if cell is not None:
                    wmax = max(24.0, cell.width - 2.4)
                    xs += [cell.x0 + 1.2,
                           max(cell.x0 + 1.2, (cell.x0 + cell.x1 - wmax) / 2.0),
                           max(cell.x0 + 1.2, cell.x1 - 1.2 - wmax)]
                for dy in (1.2, 3.2, 5.6, 9.0, 14.0, 21.0):
                    for x in xs:
                        cands.append((x, r.y1 + dy, w0))
                for dy in (1.2, 3.2, 5.6, 9.0):
                    for x in xs:
                        cands.append((x, r.y0 - dy - 6.0, w0))
                # ③ 本格整宽排在源文下方 / 源文左侧
                if cell is not None:
                    cands.append((cell.x0 + 1.2, r.y1 + 1.2,
                                  max(24.0, cell.width - 2.4)))
                    cands.append((cell.x0 + 1.2, cy - 4.0,
                                  max(24.0, r.x0 - cell.x0 - 3.0)))
                for x, ytop, w in cands:
                    if w < 22.0 or x < bx0 - 1 or ytop < 1.0:
                        continue
                    if x + w > bx1 + 1:
                        w = bx1 - x
                    got = _ov_try(u, x, ytop, w, blocked)
                    if got and _in_cell(got, cell):
                        return got
                    if _ovdbg and len(_ovdbg_hit) < 400:
                        _ovdbg_hit.append(
                            ("cand", round(x, 1), round(ytop, 1), round(w, 1),
                             round(r.width, 1),
                             [round(v, 1) for v in cell] if cell else None))
                # ④ 没有格线可依（图纸上的散标签）：沿本标签所在列小范围上下
                #    找空白走廊。**限 ±30pt** —— 老版本走 120pt，中文会飘到
                #    页面另一头，读者根本对不上原文。
                if cell is None:
                    for x, w in ((r.x0, min(max(w0 * 1.6, 90.0), bx1 - r.x0)),
                                 (r.x0, min(w0, bx1 - r.x0))):
                        if w < 40.0 or x < bx0 - 1:
                            continue
                        for step, sgn in ((2.0, 1), (-2.0, -1)):
                            y = r.y1 + 1.2 if sgn > 0 else r.y0 - 8.0
                            for _ in range(15):
                                ytop = y if sgn > 0 else y - 6.0
                                if ytop < 1.0 or ytop > H - 4.0:
                                    break
                                got = _ov_try(u, x, ytop, w, blocked,
                                              maxlines=6)
                                if got:
                                    return got
                                y += step
                return None

            blocked = list(fine)
            items, appendix = [], []

            def _emit(got):
                items.extend(got)
                for b in _ov_boxes(got):
                    blocked.append(b)
                    _ov_add(b)

            skipped = []
            n_ovplaced = 0
            for u in sorted(units, key=lambda u: (u["rect"].y0, u["rect"].x0)):
                if not any("\u4e00" <= c <= "\u9fff" for c in u["cn"]):
                    skipped.append(u)      # ③ 压根没译出中文（编号/日期/代号）
                    continue
                got = _ov_place(u, blocked)
                if got:
                    _emit(got)
                    n_placed += 1
                    n_ovplaced += 1
                else:
                    # 自己那格放不下就放弃：用户验收实测，飘到别的格/页末
                    # 只会让整页更乱、页脚更脏（附录区也一并取消）
                    skipped.append(u)
                    if _ovdbg:
                        print(f"  OVDBG y={u['rect'].y0:.1f} x={u['rect'].x0:.1f} "
                              f"rect={[round(v,1) for v in u['rect']]} "
                              f"cn={u['cn'][:20]!r}")
                        for h in _ovdbg_hit[-14:]:
                            print("    ", h)
            n_ovfail = len(units) - n_ovplaced
            # 图纸页（有巨型空格 → 非可填表单）落不下的译文送**页末附录**，
            # 不再静默丢弃 —— 单页图纸（DOC-A08/72 实测）overlay 只能落
            # 1/25，整页译文丢光会触发覆盖率硬闸门 FAIL。可填表单页维持原判
            # （用户验收：表单页飘去页末只会把页脚弄脏，宁缺勿滥）。
            for t, _r in missing:
                skipped.append({"cn": t, "rect": _r, "lvl": "body"})
            _ov_append = _em_hmax > 60.0
            ov_tail = []
            if _ov_append:
                for u in skipped:
                    if not any("\u4e00" <= c <= "\u9fff" for c in u["cn"]):
                        continue
                    _w = max(90.0, bx1 - bx0)
                    sz, ls, bd, lh = plan_cn(pool, u["cn"], _w, "body")
                    ov_tail.append({"cn": u["cn"], "x0": bx0, "width": _w,
                                    "size": sz, "lines": ls, "bold": bd,
                                    "lh": lh, "h": len(ls) * sz * lh + PAD})
            _ovapp_h = sum(t["h"] for t in ov_tail)
            ay = H
            if DBG(pno):
                print(f"  OVERLAY p{pno + 1} units={len(units)} "
                      f"placed={n_ovplaced} ovfail={n_ovfail} "
                      f"missing={len(missing)} parked={len(appendix)} "
                      f"skipped={len(skipped)}")
                for u in skipped[:40]:
                    print(f"  SKIP y={u['rect'].y0:6.1f} x={u['rect'].x0:6.1f} "
                          f"{u['cn'][:36]!r}")
            # 落字限定在源文自己那一格后，格太挤（标题栏行高只有 7~10pt）就
            # 只能少贴几块。**不因为"贴的少"退回切带重排**：实测这类表单页
            # 重排后 16 块落点过远（maxjump 404pt）、最高自愈级别仍有撕裂，
            # 比"少贴几块"糟得多。缺项一律如实记进 geom/质检④，不静默。
            lvl_count["overlay"] += n_ovplaced
            fh = H + _ovapp_h
            np_ = nd.new_page(width=W, height=fh)
            np_.show_pdf_page(fitz.Rect(0, 0, W, H), gsrc, gpno)
            tw = fitz.TextWriter(np_.rect)
            for x, y, size, ln in items:
                xx = x
                for f, s in pool.runs(ln):
                    tw.append((xx, y), s, font=f, fontsize=size)
                    xx += f.text_length(s, fontsize=size)
            if ov_tail:
                # 页末附录（同 reflow 的 tail 口径：分隔线 + 蓝字块）
                _ay = H
                np_.draw_line((bx0, _ay - 1.2), (bx1, _ay - 1.2),
                              color=(0.55, 0.55, 0.85), width=0.5)
                for t in ov_tail:
                    y = _ay + PAD * 0.6 + t["size"] * 0.95
                    for ln in t["lines"]:
                        xx = t["x0"]
                        for f, s in pool.runs(ln, t["bold"]):
                            tw.append((xx, y), s, font=f, fontsize=t["size"])
                            xx += f.text_length(s, fontsize=t["size"])
                        y += t["size"] * t["lh"]
                    _ay += t["h"]
            emit_toc_inplace(np_, tw)
            paint_decor(np_)
            tw.write_text(np_, color=BLUE, overlay=True)
            all_u = list(units) + list(skipped)
            geom.append({"bands": [[[0.0, 0.0, W, H], [0.0, 0.0, W, H]]],
                         "rails": [], "window": [0.0, W], "cuts": [],
                         "overlay": 1, "page_h": fh,
                         "erased": erased, "inline_dst": inline_dst,
                         "decor": {"items": decor_items},
                         "nunits": len(units),
                     "tbl_fid": _tbl_fid,
                         "far": 0,
                         "maxjump": 0.0,
                         "appendix": len(ov_tail),
                         "ov_append": bool(ov_tail),
                         "tail_y0": (H if ov_tail else None),
                         # 按"不出图框/不占空格/没译出中文就不落字"的规矩
                         # 主动放弃的块（仍会被质检④记为缺项，不静默）
                         "skipped": len(skipped),
                         "placed": n_ovplaced,
                         "frame": ([round(v, 1) for v in _fr]
                                   if _fr is not None else None),
                         "app_cn": sum(1 for t in ov_tail
                                       for c in t["cn"]
                                       if "\u4e00" <= c <= "\u9fff"),
                         "all_cn": sum(1 for u in all_u
                                       for c in u["cn"]
                                       if "\u4e00" <= c <= "\u9fff")})
            n_units += len(units)
            continue

        # ---- 落位条目拆条（仅 reflow 路径；overlay 页不拆，见 MODE 处）----
        # 拆条片段继承宿主的列位（x0/width），各自按"覆盖到的英文行"重取
        # 切带点；随后按拆条后的单元重新分组。jumps/lvl_count 在此统一记，
        # 避免原单元与片段重复计数。
        ex = []
        for u in units_pre:
            pl = u.pop("_pieces", None)
            if not pl:
                ex.append(u)
                continue
            for pi_ in pl:
                sz, ls, bd, lh = plan_cn(pool, pi_["cn"], u["width"], u["lvl"])
                pu = dict(u)
                pu.update(cn=pi_["cn"], rect=fitz.Rect(pi_["rect"]),
                          ybot=pi_["ybot"], org="split",
                          size=sz, lines=ls, bold=bd, lh=lh,
                          h=len(ls) * sz * lh + PAD,
                          cut=min(safe_cut(pi_["ybot"] + 0.4), H - 0.5))
                ex.append(pu)
        units = ex
        for u in units:
            jumps.append((id(u), u, u["cut"]))
            lvl_count[u["lvl"]] += 1
        _ord_scan(units)
        if ord_bad and DBG(pno):
            print("   ORDER-BAD n=%d %s" % (len(ord_bad), ord_bad[:2]))
        merged, cuts, clu_bot = _group(units)
        if DBG(pno) and len(units) != len(units_pre):
            print(f"   SPLIT-APPLIED units {len(units_pre)} -> {len(units)}, "
                  f"cuts {len(_group(units_pre)[1])} -> {len(cuts)}")

        # ---- fold multi-unit scatter groups (table/diagram labels pushed to

        # the same safe cut). When the fragments sit inside one graphic
        # cluster and form rows x columns, rebuild them as a real CN grid
        # table (cell texts come from the mono layout at the same positions);
        # otherwise fold them into one reading-order paragraph. ----
        _RULES = []      # 本页全部格线段（'h'/'v' + 坐标），一次算好。
                         # _grid_from_rules 原先每次都 page.get_drawings()；
                         # 现在单单元也要试网格（见 _fold_group 调用点），
                         # 不缓存的话每页要重算上百次。
        for _dr in page.get_drawings():
            for _it in _dr["items"]:
                if _it[0] == "l":
                    _p1, _p2 = _it[1], _it[2]
                elif _it[0] == "re":
                    _r0 = _it[1]
                    _p1 = fitz.Point(_r0.x0, _r0.y0)
                    _p2 = fitz.Point(_r0.x1, _r0.y1)
                else:
                    continue
                _a = fitz.Rect(min(_p1.x, _p2.x), min(_p1.y, _p2.y),
                               max(_p1.x, _p2.x), max(_p1.y, _p2.y))
                if _a.height <= 2.5 and _a.width >= 8:
                    _RULES.append(("h", (_a.y0 + _a.y1) / 2, _a.x0, _a.x1))
                elif _a.width <= 2.5 and _a.height >= 8:
                    _RULES.append(("v", (_a.x0 + _a.x1) / 2, _a.y0, _a.y1))

        def _grid_from_rules(cns, avail, rect, tol=4.0, sns=None, uns=None):
            """rebuild the table on the REAL grid rules of the source page:
            rule segments inside the cluster define row/col bounds; column
            widths are apportioned by the physical rule spans, so translated
            cell text lands in the same logical cell as in the original.
            tol: 收取格线的容差。拆出来的子表矩形是精确的，且与隔壁那张表
            只隔着一道 1~2pt 的表间距 —— 这时必须用 tol=1，否则邻表的竖线
            会被收进本表（列数 5 → 31，每格只剩一个字的宽，中文竖排成一条）。"""
            hs, vs = [], []
            for _k0, _v0, _a0, _a1 in _RULES:
                if _k0 == "h":
                    if (_v0 < rect.y0 - tol or _v0 > rect.y1 + tol
                            or _a1 < rect.x0 - tol or _a0 > rect.x1 + tol):
                        continue
                    hs.append((_v0, _a0, _a1))
                else:
                    if (_v0 < rect.x0 - tol or _v0 > rect.x1 + tol
                            or _a1 < rect.y0 - tol or _a0 > rect.y1 + tol):
                        continue
                    vs.append((_v0, _a0, _a1))

            def _grp(vals, tol=3.0):
                if not vals:
                    return []
                vals = sorted(vals)
                out = [vals[0]]
                for v in vals[1:]:
                    if v - out[-1] <= tol:
                        out[-1] = (out[-1] + v) / 2
                    else:
                        out.append(v)
                return out
            hb = [y for y in _grp([s[0] for s in hs])
                  if rect.y0 - tol <= y <= rect.y1 + tol]
            vb = [x for x in _grp([s[0] for s in vs])
                  if rect.x0 - tol <= x <= rect.x1 + tol]
            if len(hb) < 2 or len(vb) < 2:
                if DBG(pno):
                    print(f"   GRID-RULES few lines p{pno+1} hb={len(hb)} "
                          f"vb={len(vb)}")
                return None
            # corridor split: a vertical gap >=40pt with no rules or text
            # crossing it means side-by-side tables -> render stacked
            blocks, lo = [], 0
            for k in range(1, len(vb)):
                gap = vb[k] - vb[k - 1]
                if gap >= 40.0:
                    mid = (vb[k - 1] + vb[k]) / 2.0
                    cross = any(s[1] < mid - 3 and s[2] > mid + 3 for s in hs)
                    cross2 = any(s[1] < mid - 3 and s[2] > mid + 3 for s in vs)
                    cross3 = any((lr.x0 + lr.x1) / 2 - 6 < mid <
                                 (lr.x0 + lr.x1) / 2 + 6 for lr, _ in cns)
                    if not (cross or cross2 or cross3) and k - 1 - lo >= 2:
                        blocks.append((lo, k - 1))
                        lo = k - 1
            blocks.append((lo, len(vb) - 1))
            blocks = [b for b in blocks if b[1] - b[0] >= 2]
            if not blocks:
                return None

            def _band(bounds, v):
                for i in range(len(bounds) - 1):
                    if bounds[i] - 2 <= v < bounds[i + 1] - 2:
                        return i
                return len(bounds) - 2 if v >= bounds[-2] else 0

            out = []
            for bi, (j0, j1) in enumerate(blocks):
                C = j1 - j0
                phys = [vb[j + 1] - vb[j] for j in range(j0, j1)]
                cells = {}
                scells = {}
                srects = {}
                for lr, t in (sns or []):
                    _sc = (lr.y0 + lr.y1) / 2.0
                    if _sc < hb[0] - 1.5 or _sc > hb[-1] + 1.5:
                        # 【2026-09-29】表**上/下沿之外**的行不是表格内容，却会被
                        # `_band` 夹进行 0 / 末行：DOC-B02 p8 表上方的注释
                        # "Header, 1 X 3 pin, 2.54 mm pitch, not mounted"（y=134.2
                        # < 表顶 154.9）被并进了 scells[(0,3)]（"…TOLERANCES"），
                        # 英文词数从 1 变 14，词数锚直接失效；`table_is_datalist`
                        # 的数值列比与 `_srects` 几何也一并被污染。与 cns 侧同规则。
                        continue
                    i = _band(hb, _sc)
                    j = _band(vb, (lr.x0 + lr.x1) / 2)
                    if not (j0 <= j < j1):
                        continue
                    key = (i, j - j0)
                    scells[key] = [norm(scells[key][0] + " " + t)
                                   if key in scells else t, 1]
                    srects[key] = (lr | fitz.Rect(srects[key])) \
                        if key in srects else fitz.Rect(lr)
                # 【2026-09-29 结论：uns（用单元自身源坐标落格）**默认关闭**】
                # 当初引入它是怀疑 mono 版式与源页不同步 → cns 按 mono 坐标落格
                # 会串列。实测（DOC-B02）这个怀疑不成立：mono 的表格行与源页
                # 逐行对得上（1.3 表：mono y=348/382/418/428 → 源格 350.6/383.8/
                # 417.1/428.0，分毫不差）。而 uns 的两个副作用是灾难性的：
                #   ① `_uns_txt` 跳过逻辑会把 cns 里**绝大多数行**当成"已由单元
                #      落格"而丢掉（1.3 表 11 行只剩 3 行）；② uns 用赋值而非合并
                #      写格，格子数 = 单元数 —— 于是整表只剩几个格子（1.2 表
                #      55 格 → 5 格、1.4 表 27 → 2、2.10.3 表 37 → 6），用户看到
                #      的就是"信息大量缺失"。而且 `_use_uns` 的覆盖率判据是在
                #      `cells` 还空着的时候算的（len(cells)==0 恒真），等于永久
                #      开启，连"只在覆盖不足时启用"的闸门都没起作用。
                # 现在：cns 坐标落格为唯一通道；uns 只在显式请求时叠加
                # （DUAL_USE_UNS=1），并改成"只填空格"。
                _use_uns = os.environ.get("DUAL_USE_UNS") == "1"
                _uns_txt = set()
                for lr, t in cns:
                    _c2 = (lr.y0 + lr.y1) / 2.0
                    if _c2 < hb[0] - 1.5 or _c2 > hb[-1] + 1.5:
                        # 【2026-09-29】表格**上/下沿之外**的行不是表格内容：
                        # 小节标题（"1.3 定义、缩写和简称"紧跟表框上方）、表
                        # 上方的注释（用户报"6.5 表格上的注释直接进入了表格
                        # 中"）都会被 `_band` 夹进行 0 / 末行。直接丢弃。
                        _gtxt_drop.add(norm(t))
                        continue
                    i = _band(hb, _c2)
                    j = _band(vb, (lr.x0 + lr.x1) / 2)
                    if not (j0 <= j < j1):
                        continue
                    ja = _band(vb, lr.x0 + 1.5)
                    jb = _band(vb, lr.x1 - 1.5)
                    ja = max(j0, min(ja, j1 - 1))
                    jb = max(j0, min(jb, j1 - 1))
                    sp = max(1, jb - ja + 1)
                    # 【2026-09-29】mono 常把相邻几列的表头合成一条 line：
                    # "PIN NUMBER SIGNAL NAME" → "引脚编号 信号名称"（x97~171
                    # 横跨两列）、"TOLERANCES CURRENT SAFETY" → "公差 电流 安全"。
                    # 按中心落格会让整串塞进左起那一格、右边几格空着（用户报
                    # "没有保留原来的格式"）。这里按空格切成 N 段分给覆盖到的
                    # N 列；段数与列数不等（长句横跨两列）时不拆，维持原状。
                    # 【2026-09-29 方案A】表头"被压成一列"时按**源页英文词数**锚拆列。
                    # mono 把相邻几列的表头并成**一个段落**再翻译，几何上可能整个落进
                    # 最左那一列 —— "TOLERANCES CURRENT SAFETY RELEVANT" → "公差 电流 安全"，
                    # x321.7~379.8 宽 58pt < TOLERANCES 列宽 73pt，**纯几何无法判别**它跨了
                    # 3 列（"PCBA 名称" 37pt < 列宽 79pt 同样成立）。可行的额外信号只有
                    # 源页英文格的**词数**：从该列起累计词数，累计到 ≥ 中文段数即得跨度。
                    # 多重守卫（`DUAL_NO_HDRSPLIT=1` 可一键整体回退）：
                    #   ① 只对表头行（i==0）；② 段数 2~6、每段 ≤10 字；
                    #   ③ 中文宽度须 ≥ 本列宽 55% —— 真的"填满本列"才是被压的合并段，
                    #      短标签（"电压"）天然被挡；
                    #   ④ 锚必须吃得下（累计词数 ≥ 段数），跨度 2~4 列；
                    #   ⑤ 目标格已有内容只追加、语义重复则跳过，**绝不覆盖**。
                    _tk = [z for z in re.split(r"[\s\u00a0\x01]+", t) if z]
                    _M = 0
                    _ws = []
                    if (i == 0 and 2 <= len(_tk) <= 6
                            and all(len(z) <= 10 for z in _tk)
                            and os.environ.get("DUAL_NO_HDRSPLIT") != "1"
                            and lr.width >= 0.55 * (vb[ja + 1] - vb[ja])):
                        _cum = 0
                        for _m in range(4):
                            _jj = ja + _m
                            if _jj >= j1:
                                break
                            _en = norm((scells.get((i, _jj - j0))
                                        or ["", 0])[0])
                            _w = len(_en.split())
                            if _w <= 0:
                                break
                            _cum += _w
                            _ws.append(_w)
                            if _cum >= len(_tk):
                                break
                        if _cum >= len(_tk) and 2 <= len(_ws) <= 4:
                            _M = len(_ws)
                    if _M == 0 and sp >= 2 and len(_tk) == sp \
                            and all(len(z) <= 14 for z in _tk):
                        _M = sp
                    # 【风险守卫】只接受**一一对应**的情形：中文段数正好等于锚定
                    # 出来的列数才敢拆。段数 ≠ 列数时（如 mono 把未译的型号
                    # "VendorX ID NB" 与译文混成一条）按词数比例硬分会把词切错
                    # （实测 p3 会切出 "VendorX ID" / "NB 处理器类型 主要特性"），
                    # 这种宁可维持原状 —— 内容不丢，只是列位偏。
                    if _M >= 2 and _M == len(_tk):
                        _ptr = 0
                        for _m in range(_M):
                            _ky = (i, ja + _m - j0)
                            _seg = norm(_tk[_m])
                            if not _seg:
                                continue
                            _cur = cells.get(_ky)
                            if _cur and norm(_cur[0]):
                                if _seg in _cur[0] or _cur[0] in _seg:
                                    continue            # 已有同义内容 → 不重复
                                _cur[0] = _cur[0] + "\n" + _seg
                            else:
                                cells[_ky] = [_seg, 1]
                        if DBG(pno):
                            print("   HDR-SPLIT p%d row=%d col=%d..%d ws=%s %s"
                                  % (pno + 1, i, ja, ja + _M - 1, _ws,
                                     "|".join(_tk)))
                        continue
                    key = (i, ja - j0)
                    if key in cells:
                        cells[key][0] = cells[key][0] + "\n" + t
                        cells[key][1] = max(cells[key][1], sp)
                    else:
                        cells[key] = [t, sp]
                if len(cells) < 2:
                    continue
                rmin = min(r for r, _c in cells)
                if rmin > 0:
                    cells = {(r - rmin, c): v for (r, c), v in cells.items()}
                R = max(r for r, _c in cells) + 1
                if DBG(pno):
                    _ml = max((sum(1 for (rr, _cc), (tx, _s) in cells.items()
                                   if rr == r) for r in range(R)), default=0)
                    print(f"   GRID-RULES block p{pno+1} R={R} C={C} "
                          f"cells={len(cells)} maxcells_per_row={_ml} "
                          f"span={hb[0]:.0f}-{hb[-1]:.0f}")
                # ---- 单元格译文以**单元自身的源坐标**为准（用户 2026-09-29）----
                # mono 常把表格重新排版：同一个单元格的译文行在 mono 坐标系里
                # 可能整体位移、几列并成一行、或整行合成一条 —— 按 mono 行坐标
                # 落格会串列/丢行（DOC-B02 的 1.3、2.2、2.10.3 表）。而配对
                # 好的单元的 rect 就是**源页里的格子位置**，用它落格最稳。
                # mono 行只用来兜底（uns 没覆盖到的格子）。
                for _ur, _ut in (uns if _use_uns else []):
                    _i = _band(hb, (_ur.y0 + _ur.y1) / 2)
                    _j = _band(vb, _ur.x0 + 1.0)
                    if not (j0 <= _j < j1):
                        continue
                    _key = (_i, _j - j0)
                    if _key in cells and norm(cells[_key][0]):
                        continue            # 只填空格，不覆盖 cns 的结果
                    cells[_key] = [_ut, 1]
                    # 注意：**不要**把中文写进 scells —— scells 是"源侧格子文本"，
                    # 清单表判定（table_is_datalist）拿它算数值列比例；写中文进去
                    # 会让清单表被判成普通表、整表复刻（DOC-A05 p7 回归实测：
                    # 1244pt 复刻表 > 0.9 页高）
                    srects[_key] = [_ur.x0, _ur.y0, _ur.x1, _ur.y1]
                if _use_uns:
                    # uns 的行号取自完整 hb 带序，可能超出按 cns 算出的 R
                    # （格子行号 ≥ R 会让渲染段 ys[r_+1] 越界）——重算一次
                    _rmin = min(r for r, _c in cells)
                    if _rmin > 0:
                        cells = {(r - _rmin, c): v
                                 for (r, c), v in cells.items()}
                        scells = {(r - _rmin, c): v
                                  for (r, c), v in scells.items()}
                        srects = {(r - _rmin, c): v
                                  for (r, c), v in srects.items()}
                    R = max(r for r, _c in cells) + 1

                # ---- 行向合并（用户点名 DOC-A06 2.8.1 的合并大单元格）----
                # 源表某一列连续若干行之间**没有横线** → 那几行是一个合并大格
                # （2.8.1 第 4 列 Value definition：Bit 0~6 共用一个大格）。
                # 之前按"一格一行"渲染，合并格的译文只能落进它中点所在的那一
                # 行，形状与原文不符。这里按列探测行跨 → 跨内格内容并到顶格 →
                # 行高按跨分摊 → 渲染时文字在整段跨高内垂直居中。
                # 语义判定（清单表/数据表）必须用**合并前**的格子：行向合并只
                # 是渲染布局（一个格跨几行），不该改变"这张表是不是数据清单"
                # 的判断。用合并后的 cells 会让 DOC-A05 p7 的 19x4 表被误判
                # 成清单 → 整表只留表头（实测 p7 中文 92 行 → 27 行）。
                _cells_pre = {k: list(v) for k, v in cells.items()}
                try:
                    _roff = _rmin          # _use_uns 平移过行号时要有偏移
                except NameError:
                    _roff = 0
                _rsp = {}
                for _c in range(C):
                    _cx0, _cx1 = vb[j0 + _c], vb[j0 + _c + 1]

                    def _sep(_ii, _cx0=_cx0, _cx1=_cx1):
                        """第 _ii 行与下一行之间、在列 _c 的横向范围内是否有横线。"""
                        _kk = _roff + _ii + 1
                        if _kk >= len(hb):
                            return True
                        _yb = hb[_kk]
                        _cw = max(1.0, _cx1 - _cx0)
                        for (_hy, _hx0, _hx1) in hs:
                            if abs(_hy - _yb) > 3.0:
                                continue
                            # 源页横线常是**逐格画的小段**（不是贯通整行的长线），
                            # 所以不能要求"横线覆盖整列"—— 按与该列的**重叠比例**
                            # 判定（0.6 能认出逐格线，又不会错认邻列的线）。
                            if (min(_hx1, _cx1) - max(_hx0, _cx0)
                                    >= 0.6 * _cw):
                                return True
                        return False

                    _i = 0
                    while _i < R - 1:
                        if _sep(_i):
                            _i += 1
                            continue
                        _j2 = _i + 1
                        while _j2 < R - 1 and not _sep(_j2):
                            _j2 += 1
                        if _j2 > _i:
                            _rsp[(_i, _c)] = _j2 - _i + 1
                        _i = _j2 + 1
                if _rsp and os.environ.get("DUAL_NO_RSPAN", "0") == "1":
                    _rsp = {}          # 开关：关掉行向合并（A/B 与事后归因用）
                if _rsp:
                    # ---- mono 行 → 源行 的按序对齐（**只对出现行向合并的列**）----
                    # babeldoc 的 mono 表格行与源页行**可能整体错位 1 行**
                    # （DOC-A06 p4 第 4 列：源侧有内容的行 3/4/8/10/13，mono
                    # 侧是 3/4/7/10/12）。不修正的话，行向合并会把邻格的文本
                    # 吸进合并大格（"始终为0"、"0= 箭头关闭"被并进大格）。
                    # 守卫：两侧数量相等、≥2 行、每行偏移 ≤2 行。**只作用于有
                    # 合并格的列** —— 全表搬运会打乱本来对得很好的表
                    # （DOC-A05 p7 实测 92 行 → 27 行，等于把表拆成流水文字）。
                    for _c in sorted({_cc for (_rr, _cc) in _rsp}):
                        _sr = sorted(r for (r, cc) in scells if cc == _c
                                     and (scells[(r, cc)][0] or "").strip())
                        _mr = sorted(r for (r, cc) in cells if cc == _c
                                     and (cells[(r, cc)][0] or "").strip())
                        if len(_sr) != len(_mr) or len(_sr) < 2:
                            continue
                        if any(abs(_sr[i] - _mr[i]) > 2
                               for i in range(len(_sr))):
                            continue
                        if all(_sr[i] == _mr[i] for i in range(len(_sr))):
                            continue
                        if DBG(pno) and os.environ.get("DUAL_SPANDBG"):
                            print("      ROWALIGN col=%d src=%s mono=%s"
                                  % (_c, _sr, _mr))
                        _mv = {}
                        for r in _mr:
                            _mv[r] = cells.pop((r, _c))
                        for i, r in enumerate(_sr):
                            cells[(r, _c)] = _mv[_mr[i]]
                    # 合并：只收**源侧该行有内容**的格 —— 错位的邻格内容留在
                    # 原行，不会被吸进大格（宁可位置略偏，也不能丢内容）
                    for (_r0, _c), _k in sorted(_rsp.items()):
                        _acc = []
                        for _r in range(_r0, _r0 + _k):
                            if (_r, _c) not in cells:
                                continue
                            if not (scells.get((_r, _c)) or [""])[0].strip():
                                continue
                            _v = cells.pop((_r, _c))
                            if _v[0]:
                                _acc.append(_v[0])
                        if _acc:
                            cells[(_r0, _c)] = ["\n".join(_acc), 1]
                if DBG(pno) and _rsp and os.environ.get("DUAL_SPANDBG"):
                    print("   RSPAN p%d %s" % (pno + 1, sorted(_rsp.items())))
                    print("      hb=%s" % [round(v, 1) for v in hb])
                    print("      vb=%s j0=%d j1=%d R=%d C=%d"
                          % ([round(v, 1) for v in vb], j0, j1, R, C))
                    print("      cells=%s" % sorted(cells.keys()))
                tb = _fit_grid(cells, R, C, phys, avail, _rsp)
                if tb:
                    tb["_rspans"] = _rsp
                    tb["_cells_pre"] = _cells_pre
                    tb["hdr"] = bi
                    tb["_scells"] = {k: tuple(v) for k, v in scells.items()}
                    # 表头行（第 0 行）的源页几何：清单表就地替换"数据类型"
                    # 时要用（盖掉英文表头、中文写在原位）。
                    tb["_geo"] = (hb[0], min(hb[1], hb[0] + 40.0), vb, j0)
                    tb["_srects"] = {k: [round(v, 2) for v in r]
                                     for k, r in srects.items()}
                    out.append(tb)
            if not out:
                if DBG(pno):
                    print(f"   GRID-RULES out empty p{pno+1} blocks_tried="
                          f"{len(blocks)}")
                return None
            h = sum(t["h"] for t in out) + 6.0 * (len(out) - 1)
            for _t in out:
                _t["_span"] = (hb[0], hb[-1])   # 表格上下沿（源页坐标）
            return {"size": out[0]["size"], "blocks": out, "h": h,
                    "_span": (hb[0], hb[-1])}

        def _grid_collide(cells, cw, rh, size, rsp=None):
            """simulate the final rendered text-line boxes of a grid and
            report any physical overlap between two different cells' lines.
            A grid that collides is untrustworthy (bad span/row mapping on a
            form-style table) and must fall back to paragraph folding."""
            xs = [0.0]
            for w in cw:
                xs.append(xs[-1] + w)
            ys = [0.0]
            for h in rh:
                ys.append(ys[-1] + h)
            boxes = []
            for (r, ci), (txt, sp) in cells.items():
                if not txt or r + 1 >= len(ys):
                    continue
                wv = sum(cw[ci:ci + sp]) - 3.6
                _rb = min(r + (rsp or {}).get((r, ci), 1), len(ys) - 1)
                maxl = max(1, int((ys[_rb] - ys[r] - 2.4) // (size * 1.18)))
                for k, ln in enumerate(_wrap_cell(txt, size, wv)[:maxl]):
                    x0 = xs[ci] + 1.8
                    boxes.append((x0, x0 + pool.width(ln, size),
                                  ys[r] + k * size * 1.18,
                                  ys[r] + (k + 1) * size * 1.18))
            n = len(boxes)
            for i in range(n):
                ax0, ax1, ay0, ay1 = boxes[i]
                for j in range(i + 1, n):
                    bx0, bx1, by0, by1 = boxes[j]
                    ox = min(ax1, bx1) - max(ax0, bx0)
                    oy = min(ay1, by1) - max(ay0, by0)
                    if ox > 1.2 and oy > size * 0.35:
                        return True
            return False

        def _fit_grid(cells, R, C, phys, avail, rsp=None):
            """size/width pass: column widths proportional to the physical
            rule spans (clamped so one CN glyph always fits), row heights by
            the wrapped cell content; shrink text until the table fits."""
            tot_p = sum(phys) or 1
            CW = pool.width("国", 6.0)
            for size in (7.6, 7.2, 6.8, 6.4, 6.0):
                nat = [max(phys[j] / tot_p * avail, CW + 4.0)
                       for j in range(C)]
                cw = [min(v, avail - (CW + 4.0) * (C - 1)) for v in nat]
                s = sum(cw)
                if s > avail:
                    cw = [v * avail / s for v in cw]
                rh = []
                for r in range(R):
                    ml = 1
                    for (rr, ci), (txt, sp) in cells.items():
                        if rr != r or not txt:
                            continue
                        wv = sum(cw[ci:ci + sp]) - 3.6
                        try:
                            # 行向合并大格：文本铺在整段跨高上，**按跨分摊行数**
                            # —— 不分摊的话整段文本都算进首行，首行被撑成 3~4
                            # 行高（上一版实测的元凶，进而引发页末堆积）。
                            _k = (rsp or {}).get((rr, ci), 1)
                            _nl = len(_wrap_cell(txt, size, wv))
                            ml = max(ml, -(-_nl // max(_k, 1)))
                        except Exception:
                            pass
                    rh.append(ml * size * 1.18 + 3.0)
                h = sum(rh)
                # 高度判据：本引擎 band 机制会把输出页高延展为 H+cum 容纳中文
                # 网格（见 build 段 page_h），故网格本身不会溢出页面；此处只拦
                # 病理级超大表（>2.2 页高）。旧值 0.85H 过严：31 行 BOM 物料表
                # 译成中文约 1.15H、29 行规格表约 2.08H 被误拒、整表塌成一段
                # （DOC-A09 实测）。
                if h > 2.2 * H:
                    if DBG(pno):
                        print(f"   GRID-FIT too tall p{pno+1} size={size} "
                              f"h={h:.0f}")
                    return None
                if _grid_collide(cells, cw, rh, size, rsp):
                    if DBG(pno):
                        print(f"   GRID-FIT collide p{pno+1} size={size}")
                    continue
                return {"size": size, "cw": cw, "rh": rh, "R": R, "C": C,
                        "cells": {k: tuple(v) for k, v in cells.items()},
                        "h": h, "w": sum(cw), "_nc": len(cells)}
            return None

        def _grid_from_lines(cns, avail):
            if len(cns) < 4:
                return None
            cns = sorted(cns, key=lambda p: (p[0].y0, p[0].x0))
            rows = []
            for lr, t in cns:
                if rows and lr.y0 - rows[-1][0] <= 4.5:
                    rows[-1][1].append((lr, t))
                else:
                    rows.append((lr.y0, [(lr, t)]))
            starts = sorted({round(lr.x0, 1) for lr, _ in cns})
            colb = [starts[0]]
            for s in starts[1:]:
                if s - colb[-1] > 22.0:
                    colb.append(s)

            def col_of(x, lo):
                ci = 0
                for k, s in enumerate(colb):
                    if x >= s - (6.0 if lo else 8.0):
                        ci = k
                return ci
            R, C = len(rows), len(colb)
            if R < 2 or C < 2 or C > 12 or R * C > 320:
                return None
            cells = {}     # (r,c) -> [text, span]
            for r, (_ry, items) in enumerate(rows):
                for lr, t in items:
                    c0 = col_of(lr.x0, True)
                    c1 = col_of(lr.x1 - 0.01, False)
                    sp = max(1, min(C, c1 + 1) - c0)
                    key = (r, c0)
                    if key in cells:
                        cells[key][0] = norm(cells[key][0] + " " + t)
                        cells[key][1] = max(cells[key][1], sp)
                    else:
                        cells[key] = [t, sp]
            CW = pool.width("国", 6.0)
            for size in (8.0, 7.6, 7.2, 6.8, 6.4, 6.0):
                mn = [CW + 4.0] * C          # floor: one CN glyph + padding
                nat = [CW + 4.0] * C         # ideal width per column
                for (r, ci), (txt, sp) in cells.items():
                    if not txt:
                        continue
                    sp = max(sp, 1)
                    full = max(pool.width(p_, size) for p_ in txt.split("\n"))
                    full += 3.6
                    for k in range(ci, min(C, ci + sp)):
                        nat[k] = max(nat[k], full / sp)
                tot = sum(nat)
                cw = nat if tot <= avail else [
                    max(mn[k], avail * nat[k] / tot) for k in range(C)]
                if sum(cw) > avail + 0.5:
                    continue
                rh = []
                for r in range(R):
                    ml = 1
                    for (rr, ci), (txt, sp) in cells.items():
                        if rr != r or not txt:
                            continue
                        wv = sum(cw[ci:ci + sp]) - 3.6
                        try:
                            ml = max(ml, len(_wrap_cell(txt, size, wv)))
                        except Exception:
                            pass
                    rh.append(ml * size * 1.18 + 3.0)
                h = sum(rh)
                if h > 0.85 * H:
                    return None
                return {"size": size, "cw": cw, "rh": rh, "R": R, "C": C,
                        "cells": {k: tuple(v) for k, v in cells.items()},
                        "h": h, "w": sum(cw), "_nc": len(cells)}
            return None

        # 被判为"数据表/清单"而被有意**不译**的表体译文（用户需求：清单只译
        # 顶部的数据类型）。要记进 geom sidecar：audit_cn 的中文覆盖率闸门
        # 必须知道这是有意省略，否则会报成"缺项块"。
        _hdr_drop = []
        _hdr_n = [0]          # 计数器用 list：嵌套函数里不能赋值外层局部名

        def _fold_one(us, cc):
            """把一组单元折成一个块：优先按源页真实格线重建中文网格表，
            做不到就退回按阅读顺序折成一段。cc 为这一组所属的图形簇
            （宁可为 None，此时不做网格）。"""
            us = sorted(us, key=lambda u: (u["ybot"], u["rect"].x0))
            x0 = min(u["x0"] for u in us)
            width = max(90.0, min(max(u["rect"].x1 for u in us), bx1) - x0)
            prec = cc is not None and (round(cc.x0, 1), round(cc.y0, 1),
                                       round(cc.x1, 1), round(cc.y1, 1)
                                       ) in _precise
            if prec:
                # 拆出来的子表：可用宽度按**整张表**算，而不是按中文锚点的
                # 范围算 —— 中文只落在表格左半边时，后者会把网格挤成半宽，
                # 格子里的话被拆成一个字一行（表格左半边是标签、右半边是
                # 空白的长表格极常见）。
                x0 = min(x0, cc.x0)
                width = max(width, min(cc.x1, bx1) - x0)
            cn = norm(" ".join(u["cn"] for u in us))
            env = fitz.Rect(us[0]["rect"])
            for u in us[1:]:
                env |= u["rect"]
            env = fitz.Rect(env.x0 - 6, env.y0 - 6, env.x1 + 6, env.y1 + 6)
            tbl = None
            cns, sns = [], []
            # cl = 收取格线/文字的区域。cc=None（源块自带"一句话+一张表"、
            # 不在任何图形簇里）时用单元并集矩形 —— 否则整块折成流水段落，
            # 表格永远拿不到网格（DOC-B02 的 2.2 实测）。
            if cc is not None:
                _mm = 0.0 if prec else 2.0
                cl = fitz.Rect(cc.x0 - _mm, cc.y0 - _mm,
                               cc.x1 + _mm, cc.y1 + _mm)
            else:
                cl = fitz.Rect(env)

            if True:

                def _red(l):
                    """受控水印/图章行（红色）：不是表格内容，进格子只会把
                    表头弄脏（DOC-A09 BOM 的表头格里混进过"、泄露或转让
                    给第三方"）。"""
                    try:
                        c = int(l["spans"][0].get("color", 0))
                    except (KeyError, IndexError, TypeError, ValueError):
                        return False
                    return ((c >> 16) & 0xFF) >= 0x90 \
                        and ((c >> 8) & 0xFF) <= 0x70 and (c & 0xFF) <= 0x70

                for b in mb:
                    for l in b["lines"]:
                        t = sym2uni(norm(l["text"]))
                        lr = fitz.Rect(l["rect"])
                        if not t or abs(l.get("dir", (1.0, 0.0))[0]) < 0.5:
                            continue           # rotated artefact
                        if lr.width > 0.92 * width:
                            continue
                        cm = fitz.Point((lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2)
                        if env.contains(cm) and not _red(l):
                            cns.append((lr, t))
                for b in ob:
                    for l in b["lines"]:
                        t = norm(l["text"])
                        lr = fitz.Rect(l["rect"])
                        if not t or abs(l.get("dir", (1.0, 0.0))[0]) < 0.5:
                            continue
                        if lr.width > 0.92 * width:
                            continue
                        cm = fitz.Point((lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2)
                        if env.contains(cm) and not _red(l):
                            sns.append((lr, t))
                if cns:
                    # 拆出来的子表矩形本身就是"高格线的外接矩形"，位置是精确
                    # 的：不能再向外放宽，重收格线时也只给 1pt 容差，否则隔壁
                    # 那张表共边框的竖线会被当成自己的列（列数 5 → 31，每格
                    # 只剩一个字的宽度，中文竖着排成一条）。
                    # cl 已在上面按 cc / env 算好
                    uns = [(fitz.Rect(u2["rect"]), norm(u2.get("cn", "")))
                           for u2 in us if norm(u2.get("cn", ""))]
                    tbl = _grid_from_rules(cns, min(width, bx1 - x0), cl,
                                           1.0 if prec else 4.0, sns, uns)
                    if tbl is None and DBG(pno):
                        print(f"   GRID-RULES FAIL p{pno+1} cns={len(cns)} "
                              f"cl={[round(v,1) for v in cl]}")
                if (tbl is not None and cc is None
                        and sum(len(_b0["cells"])
                                for _b0 in tbl["blocks"]) < 4):
                    # cc=None（单单元 / 源块自带一句话+表）时，格子太少
                    # 的多半是正文正好落在某个边框里被误当表格 —— 不
                    # 采信，仍按段落走。
                    tbl = None
                if tbl is None and cns and cc is not None:
                    tb0 = _grid_from_lines(cns, min(width, bx1 - x0))
                    if tb0:
                        # 行网格没有源页行区间可记（cns 是 mono 坐标，与规则
                        # 网格的源页坐标不同一坐标系，不能拿来互相比较）——
                        # 用**图形簇矩形**当区域，去重时两边同源可比。
                        tbl = {"size": tb0["size"], "blocks": [tb0],
                               "h": tb0["h"],
                               "_region": (cc.x0, cc.y0, cc.x1, cc.y1)}
                if tbl is not None and "_region" not in tbl:
                    tbl["_region"] = (cl.x0, cl.y0, cl.x1, cl.y1)
            if tbl:
                for _blk0 in tbl["blocks"]:
                    _sc0 = _blk0.get("_scells") or {}
                    _sf0 = sum(1 for _v0 in _sc0.values()
                               if _v0 and _v0[0].strip())
                    _cf0 = sum(1 for _v0 in _blk0["cells"].values()
                               if _v0 and _v0[0].strip())
                    _tbl_fid.append([int(_blk0.get("R", 0)),
                                     int(_blk0.get("C", 0)), _cf0, _sf0,
                                     int(_blk0.get("R", 0))
                                     * int(_blk0.get("C", 0))])
                for _blk0 in tbl["blocks"]:
                    for _k0, (_tx0, _sp0) in _blk0["cells"].items():
                        _t0 = norm(_tx0)
                        if len(_t0) >= 2:
                            _grid_txt.add(_t0)
                if DBG(pno):
                    print(f"   FOLD p{pno + 1} n={len(us)} -> GRID "
                          f"{tbl['blocks'][0]['R']}x{tbl['blocks'][0]['C']} "
                          f"cells={len(tbl['blocks'][0]['cells'])} "
                          f"cc={[round(v, 1) for v in cc] if cc is not None else None}")
                if TBLDBG:
                    for _bi, _blk in enumerate(tbl["blocks"]):
                        _sc = _blk.get("_scells")
                        _bd = [(k, v) for k, v in (_sc or _blk["cells"]).items()
                               if k[0] >= 1]
                        _nv = sum(1 for _k, (t2, _s) in _bd
                                  if cell_is_value(t2))
                        _C = _blk["C"]
                        _ct, _cv = [0] * _C, [0] * _C
                        for (_r, _ci), (t2, _sp) in (_sc or _blk["cells"]).items():
                            if _r < 1:
                                continue
                            _ok = cell_is_value(t2)
                            for _j in range(_ci, min(_C, _ci + max(1, _sp))):
                                _ct[_j] += 1
                                if _ok:
                                    _cv[_j] += 1
                        print(f"   TBL p{pno + 1} b{_bi} "
                              f"{_blk['R']}x{_blk['C']} body={len(_bd)} "
                              f"val={_nv} ratio="
                              f"{(float(_nv) / len(_bd)) if _bd else -1:.2f} "
                              f"src={'Y' if _sc else 'N'} datalist="
                              f"{table_is_datalist(_blk.get('_cells_pre') or _blk['cells'], _blk['R'], _blk['C'], _sc)} "
                              f"colval={[round(_cv[j] / _ct[j], 2) if _ct[j] else None for j in range(_C)]} "
                              f"h={_blk['h']:.0f}")
                # 数据表/清单：只译表头那一行"数据类型"，表体数据行不译
                # （用户需求；判据见 table_is_datalist）。实测 BOM 31x6：
                # 整表复刻 998pt（比源页还高）→ 裁成表头后 12pt。
                nt, kept, drop, inl = tbl_header_only(tbl, pool)
                if nt is not None:
                    tbl = nt
                    _hdr_drop.extend(drop)
                    _hdr_n[0] += 1
                    if inl:
                        toc_inplace.extend(inl)
                        for _it in inl:
                            erased.extend(_it["ers"])
                    cn = norm(" ".join(kept))
                    if DBG(pno) or TBLDBG:
                        _h0 = tbl["blocks"][0] if tbl["blocks"] else None
                        print(f"   HDR-ONLY p{pno + 1} "
                              f"{'hdr' if _h0 else 'DROP-ALL'} "
                              f"dropped={len(drop)} "
                              f"chars={sum(len(d) for d in drop)} "
                              f"h={tbl['h']:.0f} "
                              f"hdr={[v[0][:14] for k, v in sorted(_h0['cells'].items())][:8] if _h0 else []}")
                mu = dict(us[0], cn=cn, x0=x0, width=width, tbl=tbl,
                          size=tbl["size"], lines=[cn], bold=False,
                          lh=LH_BODY, h=tbl["h"] + 1.6 * PAD)
                _res = [mu]
                # cc=None：源块自带"一句话 + 一张表"时，表格上下沿之外的
                # 译文（引子/结语）不能丢 —— 各自成段，按 ybot 排在表前/表后
                # （band 内按 ybot 排序）。
                if cc is None and tbl.get("_span"):
                    _s0, _s1 = tbl["_span"]
                    _pre = [t for (lr, t) in cns
                            if (lr.y0 + lr.y1) / 2 < _s0 - 1.5]
                    _post = [t for (lr, t) in cns
                             if (lr.y0 + lr.y1) / 2 > _s1 + 1.5]
                    for _pl, _yb in ((_pre, _s0 - 1.0), (_post, _s1 + 1.0)):
                        _pt = norm(" ".join(_pl))
                        if not _pt:
                            continue
                        _pz, _pls, _pbd, _plh = plan_cn(pool, _pt, width,
                                                        "body")
                        _res.append(dict(us[0], cn=_pt, x0=x0, width=width,
                                         size=_pz, lines=_pls, bold=_pbd,
                                         lh=_plh, tbl=None, ybot=_yb,
                                         h=len(_pls) * _pz * _plh + PAD))
                return _res
            if DBG(pno):
                print(f"   FOLD p{pno + 1} n={len(us)} cc="
                      f"{'Y' if cc is not None else 'N'} cns={len(cns)} "
                      f"-> PARAGRAPH ({len(cn)} chars)")
            if os.environ.get("DUAL_GRP_PROBE") and DBG(pno):
                for u2 in us:
                    _mi = u2.get("mi")
                    _mr = mb[_mi]["rect"] if (_mi is not None
                                              and _mi < len(mb)) else None
                    print("     GRPU src_y=%.1f srcx=%.0f mi=%s monoy=%s "
                          "cn=%r" % (u2["rect"].y0, u2["rect"].x0, _mi,
                                     ("%.1f" % _mr.y0) if _mr else "-",
                                     norm(u2.get("cn", ""))[:34]))
            if cc is not None and len(us) > 1:
                # 图形簇内的多单元组网格重建失败：不再把整组折成一段堆文
                # （用户 2026-09-28"信息堆到一起"，DOC-B02 p6×1/p7×3）。
                # 返回 None 让 _fold_group 逐单元各自成段 —— 译文顺序与源行
                # 一致、随各自切带落位，紧贴原文阅读顺序。
                return None
            size, lines, bold, lh = plan_cn(pool, cn, width, "body")
            return [dict(us[0], cn=cn, x0=x0, width=width, size=size,
                         lines=lines, bold=bold, lh=lh,
                         h=len(lines) * size * lh + PAD)]

        def _fold_group(us):
            """一组被同一条切线顶下来的单元。旧写法把整组当"一个块"处理，
            于是"两张相邻的表"合并成一组的并集既不属于表1也不属于表2，
            inside 判定失败 → 两张表一起退化成流水账段落（p3 两张规格表
            就是这样垮掉的）。改成先按所在图形簇拆组，每张表各自重建网格。"""
            if len(us) <= 1:
                # 【2026-09-29】单单元 band 也要查它落在哪个图形簇里：否则
                # cc=None → 表格里那一行被当普通段落排到表外（用户报"4.3 表格
                # 上面的 O16 接地端 0 V 否 不知哪来的"、"表格旁边的提示"）。
                _u0 = us[0]
                _r0 = (fitz.Rect(_u0["_host"]["rect"])
                       if (_u0.get("org") == "attach"
                           and _u0.get("_host") is not None)
                       else fitz.Rect(_u0["rect"]))
                _a0 = max(_r0.get_area(), 1.0)
                _b0, _bf = None, 0.0
                for _cc in obs:
                    _f0 = (_r0 & _cc).get_area() / _a0
                    if _f0 >= 0.3 and _f0 > _bf:
                        _b0, _bf = _cc, _f0
                return _fold_one(us, _b0) or []
            buckets, loose = {}, []
            for u in us:
                # 【2026-09-29 关键】挂靠(attach)单元的 `rect` 是 **mono** 侧
                # 坐标，而 `obs` 里的簇矩形是**源页**坐标 —— 拿 mono 矩形去和
                # 源页簇求交，恒为 0，单元全落进 loose、整条被当段落排到表外
                # （用户报"4.3 表格上面的 O16 接地端 0 V 否 不知哪来的"：
                # 实测那就是 6.1 表第 6 行的译文，ATTACH m=24 host 是 6.1 的
                # 源块）。归属判定一律用**源侧矩形**。
                r = (fitz.Rect(u["_host"]["rect"])
                     if (u.get("org") == "attach" and u.get("_host") is not None)
                     else fitz.Rect(u["rect"]))
                ar = max(r.get_area(), 1.0)
                best, bestf = None, 0.0
                # 阈值 0.5 → 0.3：单元跨了几行表体时，（单元∩表簇）/单元面积
                # 常只有 0.3~0.5，落到 loose 里就整条当段落排到表外（用户报
                # "4.3 表格上面的 O16 接地端 0 V 否 不知道是哪里来的" —— 那就是
                # 6.1 表第 6 行的译文）。0.3 以上归入该表，随网格落格。
                for cc in obs:
                    f = (r & cc).get_area() / ar
                    if f >= 0.3 and f > bestf:
                        best, bestf = cc, f
                if best is None:
                    loose.append(u)
                else:
                    k = (round(best.x0, 1), round(best.y0, 1),
                         round(best.x1, 1), round(best.y1, 1))
                    buckets.setdefault(k, [best, []])[1].append(u)
            out = []
            for _k, (cc, grp) in sorted(buckets.items(),
                                        key=lambda kv: kv[1][0].y0):
                _r = _fold_one(grp, cc)
                if _r is None:
                    # 网格重建失败的簇组：逐单元成段（保持源行顺序）
                    grp = sorted(grp, key=lambda u: (u["ybot"], u["rect"].x0))
                    for u in grp:
                        out.extend(_fold_one([u], None) or [])
                else:
                    out.extend(_r)
            if loose:
                _r = _fold_one(loose, None)
                if _r is None:
                    _r = _fold_one(sorted(loose, key=lambda u:
                                          (u["ybot"], u["rect"].x0))[0:1],
                                   None) or []
                out.extend(_r)
            return out
        # ---- 表格名称 → 标题旁空挡（用户 2026-09-28：表名翻译不要放
        # 表尾，直接放标题旁边的空挡，字号可适当缩小）。命中表名样式的
        # 单元（en 含 bill of material / parts list ...、译文短）不再进
        # 切带/附录：在标题行 右侧 → 正下方 → 左侧 找无遮挡空挡，把中文
        # 以 ≤7pt 写进去（toc_inplace 加法通道，不盖任何英文；audit_px
        # 对浅底蓝字豁免）。找不到空挡则维持旧行为（照常进切带）。
        _TTL_RE = re.compile(
            r"\b(bill\s+of\s+materials?|material\s+list|parts?\s+list|"
            r"component\s+list|spare\s+parts?\s+list|packing\s+list)\b",
            re.I)
        _ttl_all = ([l["rect"] for b in ob for l in b["lines"]]
                    + list(obs))

        def _ttl_fit(zx0, zy0, zx1, zy1, cn_t):
            if zx1 - zx0 < 14.0 or zy1 - zy0 < 4.6:
                return None
            st = min(7.0, (zy1 - zy0) * 0.85)
            while st >= 4.6:
                ls = wrap(pool, cn_t, st, zx1 - zx0 - 2.0, maxlines=1)
                if ls:
                    return {"x": zx0 + 1.0, "y": zy0 + st * 0.92,
                            "size": st, "text": ls[0], "bold": False,
                            "yrow": 0.0, "ers": []}
                st -= 0.25
            return None

        for c in cuts:
            for u in list(merged[c]):
                _en = u.get("en") or ""
                _cu = norm(u.get("cn", ""))
                if (not _TTL_RE.search(_en) or not has_cjk(_cu)
                        or len(_cu) > 48 or u.get("tbl")):
                    continue
                tr = fitz.Rect(u["rect"])
                # 同排障碍：与标题行竖向相叠 ≥40%，排除含标题的外框与标题自身
                ro = []
                for rr in _ttl_all:
                    rr = fitz.Rect(rr)
                    vo = min(rr.y1, tr.y1) - max(rr.y0, tr.y0)
                    if vo < 0.4 * max(tr.height, 0.1):
                        continue
                    if rr.x0 <= tr.x0 + 2 and rr.x1 >= tr.x1 - 2:
                        continue                    # 外框（含标题）
                    if (abs(rr.x0 - tr.x0) < 2
                            and abs(rr.x1 - tr.x1) < 2):
                        continue                    # 标题自身
                    ro.append(rr)
                # 正下方边界：横向与标题相叠 ≥15%、顶在标题之下的最近障碍
                _by = None
                for rr in _ttl_all:
                    rr = fitz.Rect(rr)
                    xo = min(rr.x1, tr.x1) - max(rr.x0, tr.x0)
                    if xo >= 0.15 * max(tr.width, 0.1) and rr.y0 >= tr.y1 - 1.0:
                        if _by is None or rr.y0 < _by:
                            _by = rr.y0
                cands = []
                if not any(r.x1 > tr.x1 + 2 for r in ro):
                    cands.append((tr.x1 + 5.0, tr.y0 + 0.4,
                                  bx1, tr.y1 - 0.4))          # 右侧
                _zy1 = tr.y1 + 14.0 if _by is None else min(
                    _by - 1.5, tr.y1 + 14.0)
                cands.append((tr.x0, tr.y1 + 1.5, tr.x1, _zy1))  # 正下方
                if not any(r.x0 < tr.x0 - 2 for r in ro):
                    cands.append((bx0, tr.y0 + 0.4,
                                  tr.x0 - 5.0, tr.y1 - 0.4))   # 左侧
                it = None
                for zx0, zy0, zx1, zy1 in cands:
                    it = _ttl_fit(zx0, zy0, min(zx1, bx1), zy1, _cu)
                    if it:
                        break
                if it:
                    it["yrow"] = (tr.y0 + tr.y1) / 2.0
                    toc_inplace.append(it)
                    merged[c].remove(u)
                    if DBG(pno):
                        print("   TBL-TITLE p%d %r -> inline @%.1f,%.1f "
                              "sz=%.1f" % (pno + 1, _en[:36],
                                           it["x"], it["y"], it["size"]))
                elif DBG(pno):
                    print("   TBL-TITLE p%d %r -> no gap, keep strip"
                          % (pno + 1, _en[:36]))

        for c in cuts:
            # 【2026-09-29】不再要求 >=2：单个单元的 band 也要过 _fold_group
            # ——源块自带"一句话 + 一张表"时（DOC-B02 的 2.2）单元只有一条，
            # 旧条件让它永不尝试网格，整块折成流水文字。
            merged[c] = _fold_group(merged[c])

        # ---- self-heal pass (escalation level >=2): simulate the final
        # output geometry and park offending CN units at the page bottom.
        # Two collision classes are predicted:
        #  (a) a cut that slices a source line: the clipped fragment still
        #      reports its FULL nominal extraction box in the band above the
        #      cut (exactly what qa_synth scores as a collision);
        #  (b) a CN unit riding over an EARLIER CN unit's line boxes (grid
        #      cells whose text spills into the neighbouring column, mono
        #      fragments split across cells, ...).
        # Un-sliced lines stay clear of the gaps by construction. ----
        def _plain(u):
            """de-grid a table-blob unit into plain wrapped paragraph"""
            u = dict(u)
            if u.get("tbl"):
                w = max(90.0, min(u["width"], bx1 - u["x0"]))
                size, lines, bold, lh = plan_cn(pool, u["cn"], w, "body")
                u.update(tbl=None, width=w, size=size, lines=lines,
                         bold=bold, lh=lh, h=len(lines) * size * lh + PAD)
            return u

        tail = []
        # ---- 覆盖兜底落位（用户 2026-09-28：没配上对的译文不要堆到页末，
        # DOC-B02 p2/p5/p7 的表格整块堆页末即此）。mono 里没配上对的块，
        # 其出处 = 源页里没被任何单元盖住的英文行；DP 配齐的部分两侧同时
        # 消掉，剩下的天然按阅读顺序单调对齐 —— 逐块取下一个空区，把译文
        # 插进该区下方最近的切带（紧贴原文）；找不到空区/切带的才进页末。
        _cov = [fitz.Rect(u["rect"]) for u in units]
        _zones = []
        for b in ob:
            for l in b["lines"]:
                t = norm(l["text"])
                if not t or has_cjk(t) or id(l) in _decids:
                    continue                # 空行/源页已有中文/装饰行
                R = line_ink(l)
                if R.y1 < 34 or R.y0 > H - 58:
                    continue                # 页眉/页脚，同 missing 规则
                if _tailc and _in_tail(R):
                    continue            # 页尾标题栏：有意不译区，不当落点
                if re.fullmatch(r"[\d.\s\-/()]+", t):
                    # 【2026-09-29 修】孤立的节号/纯数字（"4.3"、"12"、"(1)"）
                    # 不可能是某块译文的出处：DOC-B02 p7 顶部那行 stray
                    # "O1 6 接地端 0 V 否" 就是被锚到了源页左上角孤立的 "4.3"
                    # （x54-72, y39.9-49.9）—— 标题词 "Potentiometer" 有单元，
                    # 编号 "4.3" 是单独一条 line，于是成了"空区"里的第一个。
                    continue
                _cp = fitz.Point((R.x0 + R.x1) / 2, (R.y0 + R.y1) / 2)
                if any(r.contains(_cp) for r in _cov):
                    continue
                _zones.append(R)
        _zones.sort(key=lambda r: (round(r.y0, 1), r.x0))
        # 【2026-09-29 修】缺块 ↔ 空区不再"按顺序各取一个"：缺块是按 mono
        # 阅读序给出的，空区是按源页 y 排的，两边数量与分布都不一致，顺序配
        # 必然错位（p7 那行被配到 y=45，而它真正属于 y≈600 的表）。改用本页
        # **已配对单元**建 mono-y → 源页-y 的单调映射（同页同文档，y 单调），
        # 把缺块估到源页的近似位置，取**最近且未占用**的空区。没有配对可用时
        # 退回"顺序取"。
        _pairs = []
        for _up in units:
            _mi = _up.get("mi")
            if _mi is None or not (0 <= _mi < len(mb)):
                continue
            _mr = mb[_mi].get("rect")
            if _mr is None or _up.get("rect") is None:
                continue
            _pairs.append(((_mr.y0 + _mr.y1) / 2.0,
                           (_up["rect"].y0 + _up["rect"].y1) / 2.0))
        _pairs.sort()

        def _my2sy(ym):
            """mono 页内 y → 源页 y（分段线性，端点外夹住）。"""
            if not _pairs:
                return None
            if ym is None:
                return None
            if ym <= _pairs[0][0]:
                return _pairs[0][1]
            if ym >= _pairs[-1][0]:
                return _pairs[-1][1]
            for _k in range(len(_pairs) - 1):
                _m0, _s0 = _pairs[_k]
                _m1, _s1 = _pairs[_k + 1]
                if _m0 <= ym <= _m1:
                    if _m1 - _m0 < 1e-6:
                        return _s0
                    return _s0 + (ym - _m0) / (_m1 - _m0) * (_s1 - _s0)
            return _pairs[-1][1]

        _zused = set()

        def _pick_zone(ym):
            """取最近且未占用的空区；无映射（跨页溢出块 / 无配对）时取最上面的。"""
            if not _zones:
                return None
            _ys = _my2sy(ym)
            _best, _bd = None, None
            for _k, _z in enumerate(_zones):
                if _k in _zused:
                    continue
                _d = (abs((_z.y0 + _z.y1) / 2.0 - _ys)
                      if _ys is not None else float(_k))
                if _bd is None or _d < _bd:
                    _best, _bd = _k, _d
            if _best is None:
                return None
            _zused.add(_best)
            return _zones[_best]
        # 本页网格已渲染文本的**字符池**：短块（≤24 字）若 75% 以上的字符都
        # 能在网格里找到，就认为它已经由网格渲染过、不再在表外重复排一遍
        # （用户报"表格旁边的提示不知道哪里来的"）。长段落不适用本判据。
        _gpool = Counter("".join(_squeeze(z) for z in _grid_txt))
        _pending = []
        for _t0, _r0 in ([(t, None) for t in _spill_cn] + list(missing)):
            _q0 = _squeeze(norm(_t0))
            if (2 <= len(_q0) <= 24 and _gpool
                    and sum(1 for _ch in _q0 if _gpool.get(_ch)) >= 0.75 * len(_q0)):
                continue
            _pending.append((_t0, _r0))
        _spill_cn = []
        for t, _r in _pending:                # 覆盖兜底：先试贴带，再页末
            _z = _pick_zone((_r.y0 + _r.y1) / 2.0 if _r is not None else None)
            if (_z is not None and pno < npages - 1 and units
                    and (_z.y1 > max(u["ybot"] for u in units) + 2.0
                         or _z.y0 > H - 140.0)):
                # 空区落在本页所有译文之下 = 页尾残余（页脚/家具旁），不是
                # 这块译文的出处 → 判为跨页溢出，带到下一页开头（用户
                # 2026-09-29：第 5 页尾页又堆了一堆）
                _spill_cn.append(t)
                continue
            _c = None
            if _z is not None and cuts:
                _ct = safe_cut(_z.y1 + 0.4)
                _c = min(cuts, key=lambda k: abs(k - _ct))
            if _c is not None:
                x0 = max(bx0, min(_z.x0 - 4.0, bx1 - 90.0))
                w = max(90.0, bx1 - x0)
                sz, ls, bd, lh = plan_cn(pool, t, w, "body")
                merged.setdefault(_c, []).append(
                    {"cn": t, "x0": x0, "width": w, "size": sz,
                     "org": "miss", "lines": ls, "bold": bd, "lh": lh,
                     "h": len(ls) * sz * lh + PAD, "tbl": None,
                     "lvl": "body", "rect": _z, "ybot": _z.y1,
                     "cut": _c})
                continue
            if pno < npages - 1 and _r is None:
                _spill_cn.append(t)      # 上一页溢出的、本页也没落点 → 再带一页
                continue
            if pno < npages - 1 and not _zones:
                _spill_cn.append(t)      # 本页没有可用空区 → 带到下一页开头
                continue
            w = max(90.0, bx1 - bx0)
            sz, ls, bd, lh = plan_cn(pool, t, w, "body")
            tail.append({"cn": t, "x0": bx0, "width": w, "size": sz, "org": "tail",
                         "lines": ls, "bold": bd, "lh": lh,
                         "h": len(ls) * sz * lh + PAD, "tbl": None,
                         "lvl": "body", "rect": _r, "ybot": 0.0,
                         "cut": 0.0})

        # ---- 同一张表被两条以上切带各建一次网格 → 去重 ----
        # 【2026-09-29 用户报"6.4 没有保留原来格式，直接生成了两个表格"】
        # 一张源表的单元格单元常分落在两条切带里（表头行一条、表体一条），
        # 每条带各执一次 _fold_group → **同一张表复刻两遍**，页面上出现两个
        # 叠在一起的网格（p7 的 6.1/6.2/6.3、p8 的 6.5 实测：每张表都有一个
        # 29 格的全表网格 + 一个 5~6 格的"只有表头"网格）。判据用源页行区间
        # `_span`：重叠 ≥60% 视为同一张表，只保留格子多的那个。
        def _reg_of(u):
            """去重用的纵向区间：规则网格用源页行区间 `_span`（最准）；行网格
            没有源页行区间，退回它的簇矩形纵向跨度。两者都是源页坐标系。"""
            _t = u.get("tbl") or {}
            if _t.get("_span"):
                return _t["_span"]
            _rg = _t.get("_region")
            return (_rg[1], _rg[3]) if _rg else None

        _ti = []
        for _c in list(merged):
            for _u in merged[_c]:
                _rg = _reg_of(_u)
                if _rg:
                    _ti.append([_rg, sum(len(_b2["cells"])
                                         for _b2 in _u["tbl"]["blocks"]), _u])
        for _i in range(len(_ti)):
            for _j in range(_i + 1, len(_ti)):
                _A, _B = _ti[_i], _ti[_j]
                # 只看**纵向**重叠：同一张表被复刻两遍时两个网格纵向必然几乎
                # 重合（横向可能一个是整表、一个只有表头那几列，用 x 判会漏）。
                # 页面上两张不同的表在纵向不会重叠，所以按 y 判足够安全。
                _ovy = min(_A[0][1], _B[0][1]) - max(_A[0][0], _B[0][0])
                _mwy = max(1.0, min(_A[0][1] - _A[0][0], _B[0][1] - _B[0][0]))
                if _ovy >= 0.6 * _mwy:
                    (_A if _A[1] < _B[1] else _B)[2]["_tbldup"] = 1
        if any(_t[2].get("_tbldup") for _t in _ti):
            for _c in list(merged):
                merged[_c] = [u for u in merged[_c] if not u.get("_tbldup")]
            if DBG(pno):
                print("   TBL-DEDUP p%d dropped=%d"
                      % (pno + 1, sum(1 for _t in _ti if _t[2].get("_tbldup"))))

        # ---- 带内顺序 = 源行顺序（用户 2026-09-29：第 7 页 6.3 的译文排到了
        # 6.4 之后）。同一带内的单元来自不同批次插入（折叠、覆盖兜底），列表序
        # 不等于源序；统一按源页 ybot 重排后再落位。----
        for c in list(merged):
            merged[c] = sorted(merged[c],
                               key=lambda u: (u.get("ybot", 0.0),
                                              u["rect"].x0))

        # ---- 可填表单页模式（DOC-A07 用户需求）：要留白填数据的受控表单
        # （PCB History 等）不做网格重建、不往表区插译文/画蓝色复刻表——
        # 表格 1:1 零改动，全部译文送页末附录。判据：本页任一 GRID 重建的
        # 空单元格占比 ≥50% 且空格 ≥6（可填表单的典型特征：大片空白格）。
        # 注意阈值不能过松：BOM/物料清单类数据表天然有大量"不适用的空列"
        # （如 32×7 表填 154/224≈0.69），若按旧值 0.7 会把整页密集数据表误判
        # 成表单、把全部译文推到附录（DOC-A09 p2 实测）。真正留白表单的
        # 填充率通常 ≤0.5。DUAL_NO_FORMAPPEND=1 可关（A/B 用）。qa_layout
        # 据 geom 的 form_append 标志豁免"页末堆积"检查（此页堆积是设计内行为）。
        if os.environ.get("DUAL_NO_FORMAPPEND", "0") != "1":
            _form_append = False
            for c in cuts:
                for mu in merged[c]:
                    tb = mu.get("tbl")
                    if not tb:
                        continue
                    for blk in tb["blocks"]:
                        tot = blk["R"] * blk["C"]
                        # 用 **裁表头之前**的格子数：清单表裁成 1 行后
                        # cells 会只剩 C 个，按现数判会把整页误判成"可填表单"
                        # （空格子占比 0.83），把该页译文全推到页末附录。
                        _nc = blk.get("_nc", len(blk["cells"]))
                        # 面积比双保险（用户 2026-09-28，DOC-B02 p8 误判）：
                        # 只有"大片空白待填格"才是真表单。实测分布：真表单
                        # （DOC-A07 55%、DOC-B03 封面 36.6/41.7%）≥35%；
                        # 含空格的数据表远低于此（DOC-B02 p8 32.7%、
                        # DOC-A09 BOM 39.4% 且格填充率 0.69 本就不过计数关）
                        # —— 阈值 0.35 正好落在 32.7 与 36.6 的间隙里。
                        if (tot and _nc / tot <= 0.5 and tot - _nc >= 6
                                and _eratio >= 0.35):
                            _form_append = True
            if _form_append:
                if DBG(pno):
                    print(f"  FORM-APPEND p{pno + 1}: fillable form -> table "
                          f"1:1, all CN to appendix")
                for c in cuts:
                    for mu in merged[c]:
                        tail.append(_plain(mu))
                    merged[c] = []
        if HEAL >= 2 and cuts:
            thr = 0.10 if HEAL >= 3 else 0.30

            def _unit_boxes(u, yy):
                """predicted text-line rects of a unit rendered at strip
                position yy — mirrors the build-stage geometry"""
                bs = []
                tb = u.get("tbl")
                if tb:
                    yb = yy + PAD * 0.7
                    for blk in tb["blocks"]:
                        sz, cw, rh = blk["size"], blk["cw"], blk["rh"]
                        ys = [yb]
                        for h_ in rh:
                            ys.append(ys[-1] + h_)
                        xs = [u["x0"]]
                        for w_ in cw:
                            xs.append(xs[-1] + w_)
                        for (r_, ci), (txt, sp) in blk["cells"].items():
                            if not txt or r_ + 1 >= len(ys):
                                continue
                            wv = sum(cw[ci:ci + sp]) - 3.6
                            _rk = (blk.get("_rspans") or {}).get((r_, ci), 1)
                            _rb = min(r_ + _rk, len(ys) - 1)
                            maxl = max(1, int((ys[_rb] - ys[r_] - 2.4)
                                              // (sz * 1.18)))
                            _lns = _wrap_cell(txt, sz, wv)[:maxl]
                            _y0 = ys[r_] + (max(
                                0.0, (ys[_rb] - ys[r_]
                                      - len(_lns) * sz * 1.18) / 2.0)
                                if _rk > 1 else 0.0)
                            for k, ln in enumerate(_lns):
                                bs.append(fitz.Rect(
                                    xs[ci] + 1.8, _y0 + k * sz * 1.18,
                                    xs[ci] + 1.8 + pool.width(ln, sz),
                                    _y0 + (k + 1) * sz * 1.18))
                        yb = ys[-1] + 6.0
                else:
                    sz, lh = u["size"], u["lh"]
                    base = yy + PAD * 0.6 + sz * 0.95
                    for ln in u["lines"]:
                        try:
                            lw = pool.width(ln, sz, bold=u["bold"])
                        except Exception:
                            lw = u["width"]
                        bs.append(fitz.Rect(u["x0"], base - sz * 0.90,
                                            u["x0"] + min(lw, u["width"]) + 1.5,
                                            base + sz * 0.32))
                        base += sz * lh
                return bs

            def _ov(a, b):
                ix = fitz.Rect(a)
                ix.intersect(b)
                return (ix.is_valid and not ix.is_empty and
                        ix.get_area() > thr * min(a.get_area(), b.get_area()))

            for _pass in range(4):
                cut_off, cumi = {}, 0.0
                for c in cuts:
                    cut_off[c] = cumi
                    cumi += sum(u["h"] for u in merged[c])
                # sliced source lines -> nominal fragment boxes at the
                # band-above-cut position (as text extraction will report)
                sboxes = []
                for b in ob:
                    for l in b["lines"]:
                        if not l["text"].strip():
                            continue
                        if id(l) in _decids:
                            continue          # 水印/斜排装饰：不算会被压的内容
                        R = fitz.Rect(l["rect"])
                        if not R.is_valid or R.is_empty:
                            continue
                        for c in cuts:
                            if R.y0 + 0.2 < c < R.y1 - 0.2:
                                sboxes.append(
                                    fitz.Rect(R.x0, R.y0 + cut_off[c],
                                              R.x1, R.y1 + cut_off[c]))
                order = []
                for c in cuts:
                    yy = c + cut_off[c]
                    for u in sorted(merged[c],
                                    key=lambda u: (u["rect"].x0, u["rect"].y0)):
                        order.append((u, _unit_boxes(u, yy)))
                        yy += u["h"]
                bad = set()
                for i, (u, bs) in enumerate(order):
                    if any(_ov(bx, sr) for bx in bs for sr in sboxes):
                        bad.add(id(u))
                    elif any(_ov(bx, b2) for _, b2s in order[:i]
                             for b2 in b2s for bx in bs):
                        bad.add(id(u))
                    elif any(_ov(ba, bb) for a, ba in enumerate(bs)
                             for b2 in bs[a + 1:] for bb in [b2]):
                        # intra-unit: a grid cell whose (hard-split) text
                        # spills sideways onto a neighbouring cell
                        bad.add(id(u))
                if not bad:
                    break
                for c in cuts:
                    keep = []
                    for u in merged[c]:
                        if id(u) in bad:
                            if not is_furniture_cn(u["cn"]):
                                tail.append(_plain(u))
                            # 家具碎片：丢弃不补，源文英文原样保留
                        else:
                            keep.append(u)
                    merged[c] = keep
        # ---- build page, then SELF-CHECK it against the very truth QA
        # will see: re-extract text from the freshly built page and pair
        # lines with qa_synth's exact rule. Every real collision is mapped
        # back to the unit that drew it (run boxes recorded while painting)
        # and that unit is reparked at the page bottom; the page is then
        # rebuilt. Prediction can miss (font advance drift on hard-split
        # latin runs); extraction cannot lie. ----
        if os.environ.get("DUAL_MISSDBG") and DBG(pno):
            for _c in list(merged):
                for _u in merged[_c]:
                    if _u.get("org") == "miss":
                        print("   MISSDBG p%d cut=%.1f x0=%.1f ybot=%.1f "
                              "rect=%s %r"
                              % (pno + 1, _c, _u["x0"], _u.get("ybot", -1),
                                 [round(v, 1) for v in _u["rect"]],
                                 norm(_u["cn"])[:34]))

        if DBG(pno):
            print(f"  REFLOW p{pno + 1} units={len(units)} cuts={len(cuts)} "
                  f"tail={len(tail)} inline={len(toc_inplace)}")
        # ---- 【2026-09-30 修】重复译文单元剔除（DEDUP-TEXT）----
        # 病：mono 常把一段话切成多个首尾相接的块，1:1 对齐只配得上其中一个
        # （→ pair 单元），其余块经 HOST-MERGE / 切带折叠把译文并进同一单元；
        # 但**被并入的块自己也还是一个独立单元**，于是同一段译文在成品里渲染
        # 两遍（DOC-A06 p4 的 2.8.1：cut=577.6 一份 171 字、cut=763.2 一份
        # 228 字且包含前者；页尾那份因折行不同还少了几字 → 看着像"页尾堆叠"）。
        # 判据（只认"同锚点 + 内容覆盖"，不会误伤源文档固有的重复段落）：
        #   ① 只处理 CJK ≥10 字的长文本 —— 格子文本（「行1」「VOID」）不参与；
        #   ② 两个单元在**源页**的锚定矩形纵向重叠 ≥60%、横向重叠 ≥50%；
        #   ③ 一方正文（压掉空白后）是另一方的子串 → 丢弃较短的那个。
        def _anchor_rect(u):
            """单元在**源页**的锚定矩形（pair 用源块，attach 用宿主源块）。"""
            try:
                if u.get("org") == "pair" and u.get("oi") is not None:
                    return fitz.Rect(ob[u["oi"]]["rect"])
                h = u.get("_host")
                if h is not None:
                    return fitz.Rect(h["rect"])
            except Exception:
                pass
            return fitz.Rect(u["rect"])

        def _q(z):
            return re.sub(r"[\s\u00a0\u200b]+", "", norm(z or ""))

        _all = [(c, u) for c in cuts for u in merged.get(c, [])]
        _cand = []
        for _k, (_c, _u) in enumerate(_all):
            _t = _q(_u.get("cn"))
            if sum(1 for ch in _t if "\u4e00" <= ch <= "\u9fff") >= 10:
                _cand.append((_k, _t, _anchor_rect(_u)))
        _dd = set()
        for _a in range(len(_cand)):
            for _b2 in range(_a + 1, len(_cand)):
                k1, t1, r1 = _cand[_a]
                k2, t2, r2 = _cand[_b2]
                if k1 in _dd or k2 in _dd:
                    continue
                if (min(r1.y1, r2.y1) - max(r1.y0, r2.y0)
                        < 0.6 * min(r1.height, r2.height, 1e9)):
                    continue
                if (min(r1.x1, r2.x1) - max(r1.x0, r2.x0)
                        < 0.5 * min(r1.width, r2.width, 1e9)):
                    continue
                if t1 and t2 and (t1 in t2 or t2 in t1):
                    _dd.add(k1 if len(t1) <= len(t2) else k2)
        if _dd:
            if DBG(pno):
                for _k in sorted(_dd):
                    print("   DEDUP-TEXT 丢弃重复译文单元 %r"
                          % norm(_all[_k][1].get("cn") or "")[:44])
            for _k in _dd:
                _c, _u = _all[_k]
                merged[_c] = [x for x in merged[_c] if x is not _u]
            for itm in toc_inplace:
                print("   INLINE xy=(%.1f,%.1f) sz=%.1f ers=%s %r"
                      % (itm["x"], itm["y"], itm["size"],
                         [[round(v, 1) for v in e] for e in (itm.get("ers") or [])],
                         itm["text"]))
            for u2 in tail:
                print("   TAIL", repr(u2["cn"][:64]))
        laid = []
        for _rebuild in range(3):
            total = sum(u["h"] for us in merged.values() for u in us)
            total += sum(u["h"] for u in tail)
            laid = []
            np_ = nd.new_page(width=W, height=H + total)
            if _rebuild:
                # 先让新页就位、再删掉上一轮那一页。旧写法是"先删后建"，
                # 而被删的页既是 new_page 建的、又经过 show_pdf_page 嫁接、
                # 且是文档里唯一一页时，MuPDF 会抛
                # FzErrorFormat "kid not found in parent's kids array" 让整轮崩溃
                # （新建页已存在 → 被删页不再是唯一页 → 安全）。
                nd.delete_page(len(nd) - 2)
                # delete_page 会让此前取到的 Page 对象失去 parent（再拿它
                # show_pdf_page 就报 'NoneType' has no attribute 'is_pdf'），
                # 所以重新按索引取回本页的活对象。
                np_ = nd[len(nd) - 1]
            bands = []
            if bx0 > 0.5:
                np_.show_pdf_page(fitz.Rect(0, 0, bx0, H), gsrc, gpno,
                                  clip=fitz.Rect(0, 0, bx0, H))
                bands.append([[0.0, 0.0, bx0, H], [0.0, 0.0, bx0, H]])
            if bx1 < W - 0.5:
                np_.show_pdf_page(fitz.Rect(bx1, 0, W, H), gsrc, gpno,
                                  clip=fitz.Rect(bx1, 0, W, H))
                bands.append([[bx1, 0.0, W, H], [bx1, 0.0, W, H]])
            _rpatch = set()

            def _emit_band(sy0, sy1, dy):
                """把源页 [sy0,sy1] 这一横段贴到 dy 起（dy = 该段的 dst 起点）。
                窗口边界碰到竖排页边文字时（rotL/rotR），**只在文字所在的那几
                行**收窄窗口，并把被收窄掉的那一条用原位（src==dst）补一次 ——
                让整条竖排文字只出现一次、位置毫不动。每贴一片都记进 bands，
                `audit_build` 据此重拼参考页，自动与合成结果同步。"""
                if sy1 - sy0 <= 0.02:
                    return
                hits = []
                for gy0, gy1, gx in rotL:
                    if sy1 > gy0 and sy0 < gy1:
                        hits.append((max(sy0, gy0), min(sy1, gy1), gx, gy0, gy1))
                for gy0, gy1, gx in rotR:
                    if sy1 > gy0 and sy0 < gy1:
                        hits.append((max(sy0, gy0), min(sy1, gy1),
                                     bx1, gy0, gy1))
                edges = sorted({sy0, sy1} | {t[0] for t in hits}
                               | {t[1] for t in hits})
                for i in range(len(edges) - 1):
                    a, b = edges[i], edges[i + 1]
                    if b - a <= 0.02:
                        continue
                    xa, xb = bx0, bx1
                    for ga, gb, gx, gy0, gy1 in hits:
                        if (gx > bx0 and a >= gy0 - 1e-6
                                and b <= gy1 + 1e-6):
                            xa = max(xa, gx)
                        elif (gx < bx1 and a >= gy0 - 1e-6
                                and b <= gy1 + 1e-6):
                            xb = min(xb, gx)
                    if xa >= xb - 1.0:
                        continue
                    np_.show_pdf_page(fitz.Rect(xa, a + dy, xb, b + dy),
                                      gsrc, gpno, clip=fitz.Rect(xa, a, xb, b))
                    bands.append([[xa, a, xb, b], [xa, a + dy, xb, b + dy]])
                # 收窄掉的那一条原位补一次（同一列只补一遍）
                for gy0, gy1, gx in rotL:
                    k = ("L", gx, round(gy0, 1), round(gy1, 1))
                    if k in _rpatch or gx <= bx0 + 0.5:
                        continue
                    if sy1 > gy0 and sy0 < gy1:
                        _rpatch.add(k)
                        np_.show_pdf_page(fitz.Rect(bx0, gy0, gx, gy1),
                                          gsrc, gpno,
                                          clip=fitz.Rect(bx0, gy0, gx, gy1))
                        bands.append([[bx0, gy0, gx, gy1],
                                      [bx0, gy0, gx, gy1]])
                for gy0, gy1, gx in rotR:
                    k = ("R", gx, round(gy0, 1), round(gy1, 1))
                    if k in _rpatch or gx >= bx1 - 0.5:
                        continue
                    if sy1 > gy0 and sy0 < gy1:
                        _rpatch.add(k)
                        np_.show_pdf_page(fitz.Rect(gx, gy0, bx1, gy1),
                                          gsrc, gpno,
                                          clip=fitz.Rect(gx, gy0, bx1, gy1))
                        bands.append([[gx, gy0, bx1, gy1],
                                      [gx, gy0, bx1, gy1]])

            tw = fitz.TextWriter(np_.rect)
            cum, prev, n_pl = 0.0, 0.0, 0
            gaps = []
            for c in cuts:
                us = sorted(merged[c],
                            key=lambda u: (u["rect"].x0, u["rect"].y0))
                if c > prev + 0.05:
                    _emit_band(prev, c, cum)
                gaps.append((c + cum, c + cum + sum(u["h"] for u in us)))
                yc = c + cum
                for u in us:
                    tb = u.get("tbl")
                    if tb:
                        by = yc + PAD * 0.7
                        tx = u["x0"]
                        for blk in tb["blocks"]:
                            sz = blk["size"]
                            ys = [by]
                            for rh_ in blk["rh"]:
                                ys.append(ys[-1] + rh_)
                            xs = [tx]
                            for cw_ in blk["cw"]:
                                xs.append(xs[-1] + cw_)
                            for (r_, ci), (txt, sp) in sorted(blk["cells"].items()):
                                if not txt:
                                    continue
                                wv = sum(blk["cw"][ci:ci + sp]) - 3.6
                                # 行向合并大格：跨高取到 _rspans 给的末行，文字
                                # 在整段跨高内垂直居中（DOC-A06 2.8.1 第 4 列）
                                _rk = (blk.get("_rspans") or {}).get(
                                    (r_, ci), 1)
                                _rb = min(r_ + _rk, len(ys) - 1)
                                maxl = max(1, int((ys[_rb] - ys[r_] - 2.4)
                                                  // (sz * 1.18)))
                                _lns = _wrap_cell(txt, sz, wv)[:maxl]
                                # 居中**只对跨行合并格**生效：普通格一律顶部对齐
                                # （全表居中会把本来排好的表格整体推乱，实测
                                #  DOC-A05 p7 的 19x4 表因此退化成流水文字）
                                yv = ys[r_] + sz * 1.02 + (max(
                                    0.0, (ys[_rb] - ys[r_]
                                          - len(_lns) * sz * 1.18) / 2.0)
                                    if _rk > 1 else 0.0)
                                for ln in _lns:
                                    xx = xs[ci] + 1.8
                                    for f, s in pool.runs(ln, r_ == 0):
                                        tw.append((xx, yv), s, font=f,
                                                  fontsize=sz)
                                        if s.strip():
                                            laid.append((fitz.Rect(
                                                xx, yv - sz * 0.95,
                                                xx + f.text_length(
                                                    s, fontsize=sz),
                                                yv + sz * 0.40), id(u)))
                                        xx += f.text_length(s, fontsize=sz)
                                    yv += sz * 1.18
                            # ---- 网格线按**合并区域**裁剪（通用，2026-09-30）----
                            # 旧写法是朴素满网格：每条行界画通栏横线、每条列界画
                            # 通高竖线 —— 合并大格的内部被横/竖线穿过（DOC-A06
                            # 2.2.5：0x4BXX 大格被通栏线穿过、居中文本骑在线上；
                            # 2.8.1 的 Bit0~6 大格同样）。修法：先收集所有合并
                            # 区域（行向 _rspans × 列向 sp），画每条**内部**线时
                            # 减去穿过它的合并段 —— 表框/其余照画，线只会少不会多。
                            _regions = []
                            for (_rg0, _rgc), (_tx, _sp) in \
                                    blk["cells"].items():
                                _rgk = (blk.get("_rspans") or {}).get(
                                    (_rg0, _rgc), 1)
                                if _sp > 1 or _rgk > 1:
                                    _regions.append(
                                        (_rg0, _rg0 + _rgk, _rgc, _rgc + _sp))
                            for (_rg0, _rgc), _rgk in (
                                    blk.get("_rspans") or {}).items():
                                if _rgk > 1 and not any(
                                        rg[0] <= _rg0 < rg[1]
                                        and rg[2] <= _rgc < rg[3]
                                        for rg in _regions):
                                    _regions.append(
                                        (_rg0, _rg0 + _rgk, _rgc, _rgc + 1))

                            def _seg_sub(_a, _b, _cuts):
                                """[_a,_b] 减去 cuts 里的 (c,d) 段 → 剩余子段。"""
                                _out, _cur = [], _a
                                for _c, _d in sorted(_cuts):
                                    if _d <= _cur or _c >= _b:
                                        continue
                                    if _c > _cur:
                                        _out.append((_cur, min(_c, _b)))
                                    _cur = max(_cur, _d)
                                    if _cur >= _b:
                                        break
                                if _cur < _b:
                                    _out.append((_cur, _b))
                                return _out

                            for _j in range(len(ys)):
                                if _j == 0 or _j == len(ys) - 1:
                                    np_.draw_line((xs[0], ys[_j]),
                                                  (xs[-1], ys[_j]),
                                                  color=BLUE, width=0.45)
                                    continue
                                _cuts = [(xs[c0], xs[c1])
                                         for (r0, r1, c0, c1) in _regions
                                         if r0 < _j < r1]
                                for s0, s1 in _seg_sub(xs[0], xs[-1], _cuts):
                                    if s1 - s0 > 0.5:
                                        np_.draw_line((s0, ys[_j]),
                                                      (s1, ys[_j]),
                                                      color=BLUE, width=0.45)
                            for _i in range(len(xs)):
                                if _i == 0 or _i == len(xs) - 1:
                                    np_.draw_line((xs[_i], ys[0]),
                                                  (xs[_i], ys[-1]),
                                                  color=BLUE, width=0.45)
                                    continue
                                _cuts = [(ys[r0], ys[r1])
                                         for (r0, r1, c0, c1) in _regions
                                         if c0 < _i < c1]
                                for s0, s1 in _seg_sub(ys[0], ys[-1], _cuts):
                                    if s1 - s0 > 0.5:
                                        np_.draw_line((xs[_i], s0),
                                                      (xs[_i], s1),
                                                      color=BLUE, width=0.45)
                            by = ys[-1] + 6.0
                        yc += u["h"]
                        n_pl += 1
                        continue
                    y = yc + PAD * 0.6 + u["size"] * 0.95
                    # 中文不能压在竖排页边文字上：被保护的那几列（rotL/rotR）
                    # 只有 9pt 宽，中文块从窗口左界起排就会盖掉它大半个字身
                    # （DOC-B14 实测两条中文各压住 25pt²/98pt²）。与某列在 y 上
                    # 相交时整块右移让开；右移量以不超出窗口右界为限。
                    _ux = u["x0"]
                    _by0 = yc - u["size"] * 0.3
                    _by1 = _by0 + u["h"] + u["size"] * 0.6
                    for _gy0, _gy1, _gx in rotL + rotR:
                        if _by1 > _gy0 + 1.0 and _by0 < _gy1 - 1.0:
                            # 能往右让多少：到窗口右界（或右边条里那条被保护的
                            # 竖排列左沿）为止。让不开的极端情形（满宽中文块）
                            # 允许溢出到空白页边 —— 页边本来就没内容。
                            _lim = W - 2.0
                            for _ay0, _ay1, _agx in rotR:
                                if _by1 > _ay0 + 1.0 and _by0 < _ay1 - 1.0:
                                    _lim = min(_lim, _agx - 1.2)
                            _room = max(0.0, _lim - (u["x0"] + u["width"]))
                            _ux = max(_ux, min(_gx + 1.2, u["x0"] + _room))
                    for ln in u["lines"]:
                        xx = _ux
                        for f, s in pool.runs(ln, u["bold"]):
                            tw.append((xx, y), s, font=f, fontsize=u["size"])
                            if s.strip():
                                laid.append((fitz.Rect(
                                    xx, y - u["size"] * 0.95,
                                    xx + f.text_length(s, fontsize=u["size"]),
                                    y + u["size"] * 0.40), id(u)))
                            xx += f.text_length(s, fontsize=u["size"])
                        y += u["size"] * u["lh"]
                    yc += u["h"]
                    n_pl += 1
                cum += sum(u["h"] for u in us)
                prev = c
            if prev < H - 0.05:
                _emit_band(prev, H, cum)
            # page-bottom appendix: CN units the self-heal passes parked
            # away — always collision-free because nothing else lives here.
            if tail:
                ay = H + cum
                np_.draw_line((bx0, ay - 1.2), (bx1, ay - 1.2),
                              color=(0.55, 0.55, 0.85), width=0.5)
                for u in tail:
                    y = ay + PAD * 0.6 + u["size"] * 0.95
                    for ln in u["lines"]:
                        xx = u["x0"]
                        for f, s in pool.runs(ln, u["bold"]):
                            tw.append((xx, y), s, font=f, fontsize=u["size"])
                            if s.strip():
                                laid.append((fitz.Rect(
                                    xx, y - u["size"] * 0.95,
                                    xx + f.text_length(s, fontsize=u["size"]),
                                    y + u["size"] * 0.40), id(u)))
                            xx += f.text_length(s, fontsize=u["size"])
                        y += u["size"] * u["lh"]
                    ay += u["h"]
            for x, _ry0, _ry1, rail_col, rail_w in rails:
                for g0, g1 in gaps:
                    if g1 - g0 > 0.5:
                        np_.draw_line((x, g0 - 0.3), (x, g1 + 0.3),
                                      color=rail_col, width=rail_w)
            # 整宽横向模板框线：跨窗口重画整宽，避免被 [bx0,bx1] 裁残
            # （与 rails 竖线对称）。横线落在某条 band 内，按其 band 位移重算
            # 输出 y；gap 在横线下方、不会切到它，故直接整宽画即可。
            #
            # 坑：bands 里前两条是**左右边条**（src==dst、整页高），它们的 src
            # 区间同样"包含"任意 cy。旧写法按首个命中 break，边条先入列 →
            # 位移恒为 0 → 横线被留在原始 y 上。结果正文底部多出一条与已下移
            # 的表格错位的整宽横线，和边条残留的竖线薄片拼成"半框"（DOC-X01
            # 图框底边 y816.7 未随表格移到 y1014.5）。这里只在**切带**里找
            # （边条高度恒为整页高，据此排除），并取命中的最小高度区间。
            for cy, hx0, hx1, hcol, hw in hrules:
                out_y = cy
                cand = [b for b in bands
                        if 0.5 < (b[0][3] - b[0][1]) < H - 0.5
                        and b[0][1] - 0.5 <= cy <= b[0][3] + 0.5]
                if cand:
                    sb, db = min(cand, key=lambda b: b[0][3] - b[0][1])
                    out_y = cy + (db[1] - sb[1])
                # 只补**被窗口裁掉的两端**：窗内那一截本来就随切带搬到了
                # out_y，整条重画等于把黑线再叠一次 —— 压在红章/红框边上
                # 会把纯红压成暗红，audit_px 记账为"未解释像素差"
                # （DOC-X01：红框两条竖边 ×2 行 = 12px）。窗口长足以后
                # 本段通常为空，等于不画。
                segs = []
                if hx0 < bx0 - 0.05:
                    segs.append((hx0, min(hx1, bx0)))
                if hx1 > bx1 + 0.05:
                    segs.append((max(hx0, bx1), hx1))
                for s0, s1 in segs:
                    if s1 - s0 > 0.3:
                        np_.draw_line((s0, out_y), (s1, out_y),
                                      color=hcol, width=hw)
            emit_toc_inplace(np_, tw, cuts, merged)
            paint_decor(np_)
            tw.write_text(np_, color=BLUE, overlay=True)
            if HEAL < 1:               # legacy probe mode: no self-check
                break
            # ---- extraction truth check (qa_synth's exact rule) ----
            lines = []
            for b in np_.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for l in b["lines"]:
                    t = "".join(s["text"] for s in l["spans"])
                    if t.strip():
                        ss = [s for s in l["spans"] if s["text"].strip()]
                        lines.append((fitz.Rect(l["bbox"]),
                                      any("yahei" in s["font"].lower()
                                          for s in l["spans"]),
                                      max((s["size"] for s in ss), default=9.0),
                                      any(_grey_col(s.get("color", 0))
                                          for s in ss)))
            hit = set()
            for r1, s1, _z1, _g1 in lines:
                if not s1:
                    continue
                for r2, s2, z2, g2 in lines:
                    if r2 is r1 or r2.contains(r1):
                        continue
                    # 斜排/水印装饰行：行框斜跨大半页、行高远大于字号，
                    # 或本身是浅灰底纹字。它既不是切带障碍（见 bobs），
                    # 也不能算"会被压的内容"，否则任何落进它那个巨大的斜框
                    # 或灰色残片里的中文都会被误判为压字、无谓地挪到页末。
                    if r2.height > 2.6 * max(4.0, z2) or g2:
                        continue
                    # 目录就地替换：中文落在源行 x 范围内且基本同基线 = 设计内
                    if (not s2 and r1.x0 >= r2.x0 - 2.0
                            and r1.x1 <= r2.x1 + 2.0
                            and abs((r1.y0 + r1.y1) / 2 - (r2.y0 + r2.y1) / 2)
                            < 0.45 * max(r2.height, 1.0)):
                        continue
                    if s2 and abs(r1.x0 - r2.x0) < 1.2 \
                            and 2 < abs(r1.y0 - r2.y0) < r1.height + 2:
                        continue
                    ix = fitz.Rect(r1)
                    ix.intersect(r2)
                    if not (ix.is_valid and not ix.is_empty) \
                            or ix.get_area() <= 0.30 * min(r1.get_area(),
                                                           r2.get_area()):
                        continue
                    cx = (r1.x0 + r1.x1) / 2.0
                    cy = (r1.y0 + r1.y1) / 2.0
                    for lb, uid in laid:
                        if lb.x0 - 1.5 <= cx <= lb.x1 + 1.5 \
                                and lb.y0 - 1.5 <= cy <= lb.y1 + 1.5:
                            hit.add(uid)
                    break
            if not hit or _rebuild == 2:
                break
            for c in cuts:
                keep = []
                for u in merged[c]:
                    if id(u) in hit:
                        if not is_furniture_cn(u["cn"]):
                            tail.append(_plain(u))
                            n_rb += 1
                        # 家具碎片：丢弃不补，源文英文原样保留
                    else:
                        keep.append(u)
                merged[c] = keep
        # 页末堆积的"分量"：光数块数会漏掉"3 个块 = 整节 27 行译文"这种情况
        # （DOC-B14 第 4 节就是这样被丢到页尾而五项质检全报 0）。把被挪到
        # 页末附录的中文字数、以及本页中文总字数一并记进 geom，质检⑤据此
        # 按"占比"判定，而不是只看块数。
        _cjk = lambda s: sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
        _app_cn = sum(_cjk(u["cn"]) for u in tail)
        # "堆到页末**且离自己那块原文很远**"的中文字数。判据不能用"附录起点 −
        # ybot"（附录起点是输出坐标、含全部累积增量，恒差几百 pt），也不能用
        # "页底 H − ybot"（源页底部常是标题栏，正文早在 H-200 就结束了）。
        # 正确的参照是**本页所有译文单元里最深的那条原文下沿** `_src_deep`：
        # 源页最后一段的译文接在分隔线之下属正常续排（它的原文本来就是全页最
        # 深的一条，DOC-A05 p7 实测 21% 假堆积），而"整节译文被丢到页尾"的
        # 那一节，其原文距 `_src_deep` 有几百 pt，必然被记上。
        _src_deep = 0.0
        for us in merged.values():
            for u in us:
                _src_deep = max(_src_deep, float(u.get("_srcy1")
                                                or u.get("ybot") or 0.0))
        for u in tail:
            _src_deep = max(_src_deep, float(u.get("_srcy1")
                                             or u.get("ybot") or 0.0))
        _app_far = sum(_cjk(u["cn"]) for u in tail
                       if _src_deep - float(u.get("_srcy1")
                                            or u.get("ybot") or 0.0) > 120.0)
        _all_cn = sum(_cjk(u["cn"]) for us in merged.values() for u in us) \
            + _app_cn
        geom.append({"bands": bands, "rails": rail_xs, "hrules": [h[0] for h in hrules],
                     "window": [bx0, bx1], "cuts": sorted(cuts),
                     "page_h": H + total, "src_h": H,
                     "appendix": len(tail),
                     "form_append": _form_append,
                     # 页末附录的**真实起点**（= 最后一条切带的译文块之下、分隔线
                     # 所在处）。质检⑤的"页末堆积"必须用它当界：只按"最后一条
                     # 切带的 dst 底边"算，会把**每条切带本来就该有的那块译文**
                     # 也算成堆积，长段落页因此恒有 15~21% 的假堆积
                     # （DOC-A05 p7 实测：同一段落全在页底正常续排，却被记
                     # 21%）。老侧车没有这个键，质检走几何兜底。
                     "tail_y0": (H + cum) if tail else None,
                     "app_cn": _app_cn, "all_cn": _all_cn,
                     "app_far_cn": _app_far,
                     "decor": {"items": decor_items},
                     "erased": erased, "inline_dst": inline_dst,
                     "nunits": len(units),
                     # 清单表裁头（用户需求）：`datalist` = 被判为"数据表/
                     # 清单"的表格数（表体不译、只译表头）；`skip_cn` = 有意
                     # 不译的表体译文（audit_cn 据此豁免覆盖率闸门）；`tbl_rep`
                     # = 本页中文网格复刻的总高度（pt）—— 整表复刻一张 31×6
                     # BOM 要 ~1000pt（比源页还高，用户报的不可读形态），
                     # 质检⑤据此拦"整表复刻"。
                     "datalist": _hdr_n[0],
                     "skip_cn": (_hdr_drop + _tail_drop + _furn_drop
                                 + sorted(_gtxt_drop)),
                     # 页尾标题栏/修订表带（audit_cn 豁免用 —— 引擎各路径
                     # 漏记的家具块，只要落在带里就按有意不译处理）
                     "tail_band": ([round(_ty0, 1), round(_tx0, 1),
                                    round(_tx1, 1)] if _tailc else None),
                     "tbl_rep": round(sum(
                         u["h"] for us in merged.values() for u in us
                         if u.get("tbl")), 1),
                     "order": len(ord_bad),
                     "order_det": ord_bad[:4],
                     "far": sum(1 for uid, u2, ct in jumps
                                if ct - max(u2["ybot"],
                                            clu_bot.get(uid, 0.0)) > 120.0),
                     "maxjump": round(max(
                         [ct - max(u2["ybot"], clu_bot.get(uid, 0.0))
                          for uid, u2, ct in jumps] or [0.0]), 1)})
        n_units += len(units)
        n_placed += n_pl
        n_app += len(tail)
    # subset_fonts 会误删"嫁接页"里用到的字形轮廓：文字层还在，渲染却是空白
    # （本样张 p10 的弯引号就是这么丢的，像素审计报 LOST/MOVED）。但它同时
    # 是文件体积的大头 —— 关掉后同一份 22 页样张从 2.1MB 涨到 27MB（中文字
    # 体要整份嵌入）。因此默认仍然开启，需要"零字形丢失"时：
    #     set DUAL_NO_SUBSET=1
    # 换取体积（交付前深检若报"未解释像素差"，先试这个开关确认是否此因）。
    if os.environ.get("DUAL_NO_SUBSET"):
        print(":: 已禁用 subset_fonts（DUAL_NO_SUBSET=1）：零字形丢失，文件更大")
    else:
        try:
            nd.subset_fonts(verbose=False)
        except Exception:
            pass
    nd.save(out_f, garbage=4, clean=True, deflate=True)
    import json
    with open(out_f + ".geom.json", "w", encoding="utf-8") as f:
        json.dump(geom, f)
    print(f"OK -> {out_f} pages={len(nd)} units={n_units} placed={n_placed}")
    if n_app:
        extra = f"（其中 {n_rb} 个由页内真值自检发现）" if n_rb else ""
        print(f"自愈：{n_app} 个压字中文块已自动改排到所在页页末（零碰撞保证）{extra}")
    far = sum(int(g.get("far", 0) or 0) for g in geom if isinstance(g, dict))
    tot = sum(int(g.get("nunits", 0) or 0) for g in geom if isinstance(g, dict))
    if tot:
        print(f"排版距离：{tot} 个译文块中有 {far} 个落点距其原文 >120pt"
              f"（{far / tot:.0%}）")
    print("levels:", dict(lvl_count))


if __name__ == "__main__":
    main()
