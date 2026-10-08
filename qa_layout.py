# -*- coding: utf-8 -*-
"""质检⑤：排版可读性（旧质检看不见的一类失败）。

撕裂/碰撞/像素三条只回答"原文有没有被弄坏、中文有没有压字"，
它们对"译文被丢到页面另一头"完全失明：一台 229/572 个中文块被
堆到页末的成品，三条全报 0，照样标"可交付"。

本项直接量三条可读性指标（全部读 <out>.geom.json 侧车 + 输出文本）：

  1. 落点距离  far   某个译文块实际落点距它自己的原文块 >120pt 的个数。
                     正常相邻插入 ≈ 0~2pt；密集表/大图把切带顶到页尾时，
                     几十个块会被折叠到同一条带、放到页面另一头。
  2. 页末堆积  appendix / app_cn 该页有多少译文块（及多少**中文字**）被改排到
                     页末附录，且**离它自己的原文很远**。只数块数会漏掉
                     "3 个块 = 整节 27 行译文"这种情况（DOC-B14 实测：三个块把
                     整节丢到页尾，旧口径报 0 告警），所以以**中文字数占比**为主
                     判据。主量取侧车的 `app_far_cn`（附录里的中文里，其原文距
                     "本页最深的一条原文下沿" >120pt 的那部分）——源页**最后一段**
                     的译文接在分隔线之下是正常续排（它的原文本来就是全页最深的
                     那条），不算堆积；老侧车没有这个键时退回"附录区里的全部中文"。
                     阈值 25%：实测正常页最高 21%（DOC-A05 p7，一个页首段落因
                     巨型表格把落点推到页底，属既有的"落点过远"族），严重案例
                     （整节译文丢页尾）≥34%，另有 300 字绝对上限兜底。
  3. 字号下限  tiny  输出里同步合成字体小于 4.2pt 的行数（肉眼不可读）。
  4. 编号对应  num   译文里带 "4.3" 这类编号的行，落点必须与原文同编号的那一条
                     对齐。撕裂/碰撞/排版/像素四项对"每条译文整体下移一行/整段
                     中文被插到该区域中部"完全失明 —— DOC-B01 第 2/3 节整体
                     错位一行、4.2–4.10 整段中文落在 4.6 之后，五项全 0。本项是
                     "逐条对照"唯一可机检的口径。
  5. 译文顺序  order  译文块的**先后顺序**是否与参考译文（mono）一致。撕裂/碰撞/
                     像素/落点距离、甚至第 4 项编号对应，全都只看"单块落位准不准"，
                     没有一项看"块的先后对不对"。mono 把一个段落的译文拆成多个行块
                     时，只有一块能被 1:1 对齐吃掉，其余走"挂靠"路径；两族切点取自
                     不同坐标系（源页 y vs mono y），于是互相穿插 —— 同页对照里
                     读起来就是"翻译顺序乱了"（DOC-B01 概述段实测：段落的第 2 行
                     插在正文第 3、4 行之间、第 1 行反而掉到段尾）。
  6. 表格复刻  grid  本页中文网格复刻的总高度（侧车 `tbl_rep`）。清单/数据表
                     （BOM 这类"填数据的表"）整表译成中文会把输出页撑到 2 页多、
                     长料号列被拆成一个字一行 —— 用户报的不可读形态（DOC-A09：
                     31×6 复刻 963~1755pt > 源页 842pt）。这类表应只译表头
                     （数据类型），复刻高度超过 0.9×源页高即告警（正常说明表格实测最高 0.56）。

任一页超标 → 退出码 1，且 make_dual 不再把结果标成"可交付"。
阈值取得宽松：只拦"明显没法读"，不打扰正常页。
"""
import re
import sys, os, json
import pymupdf as fitz

FAR_PT = 120.0          # 落点距离阈值（pt）
FAR_RATIO = 0.18        # 单页"落点过远"允许占比
APP_RATIO = 0.18        # 单页"页末堆积"允许占比（按块数，仅老侧车兜底）
APP_MIN = 4             # 页末堆积的绝对下限（小页允许少量）
PARK_RATIO = 0.25       # 被堆到页末**且远离自己原文**的中文字数占比上限
PARK_ABS = 300          # 同上，绝对上限（严重案例靠这条兜底）
MIN_SIZE = 4.2          # 可读字号下限
NUM_LINE = re.compile(r"^(\d+(?:[.．]\d+)+)[.．]?\s*")   # 行首编号
NUM_NEAR = 60.0         # 译文行与它那条英文行的最大间距（超出判不了，跳过）
# 译文里的"条目编号"：必须在行首，或紧跟在句末标点之后。放宽到"任意位置的
# 点分数字"会把 2.5mm / 27.6VDC / FW版本2.3 这类**数据**当成条目编号
# （DOC-A05/DOC-A06 实测 20+ 处假阳性）。
NUM_CN = re.compile(r"(?:(?<=[。；！？;])|^)(\d+(?:[.．]\d+)+)(?![0-9])")
NUM_ANY = re.compile(r"(?<![0-9.])(\d+(?:[.．]\d+)+)(?![0-9])")


def _num_corr(page, bot=None):
    """编号对应检查：把整页文本按阅读顺序排成一条流，记住"最近一条以编号开头
    的英文行"的编号，然后要求每条带编号的中文行都等于它。英文行没有编号时
    （如 "66 to 70mA (DE2, all red LED ON)" 这种续行）不改判据，中文续行也放行。
    一行里出现 ≥3 个点分数字的行（目录/数据行）不判——那些编号不是条目编号。
    bot：页末附录区的起始 y。附录里的中文按设计远离它的原文（可填表单页/图纸页
    整页送附录），不参与本项判定。"""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            t = "".join(s["text"] for s in l["spans"]).strip()
            if not t:
                continue
            cn = any("yahei" in s["font"].lower() for s in l["spans"])
            lines.append((l["bbox"][1], l["bbox"][0], cn, t))
    lines.sort(key=lambda x: (round(x[0] / 2.0), x[1]))
    exp, exp_y, bad = None, None, []
    for y, x, cn, t in lines:
        if not cn:
            m = NUM_LINE.match(t)
            if m:
                exp, exp_y = m.group(1).replace("．", "."), y
            continue
        if bot is not None and y > bot:
            continue                      # 页末附录区（设计内远离原文）
        if len(NUM_ANY.findall(t)) >= 3:
            continue                      # 目录行/数据行
        m = NUM_CN.search(t)
        if not m or exp is None:
            continue
        if exp_y is not None and y - exp_y > NUM_NEAR:
            continue                      # 与那条英文行不相邻，判不了
        tok = m.group(1).replace("．", ".")
        if tok != exp:
            bad.append((round(y, 1), tok, exp, t[:34]))
    return bad


def main(out_f=None):
    # 入口同时兼容两种调用：`qa_layout.py <out.pdf>`（子进程）与
    # `call_main(qa_layout.main, out)` 的 argv 风格（GUI/exe 内进程调用）。
    if out_f is None:
        out_f = sys.argv[1] if len(sys.argv) > 1 else None
    if not out_f:
        print("qa_layout: 未给出输出 PDF 路径")
        return 0
    gf = out_f + ".geom.json"
    if not os.path.exists(gf):
        print("qa_layout: 缺少 geom 侧车，跳过（旧产物请重跑合成）")
        return 0
    geo = json.load(open(gf, encoding="utf-8"))

    def _parked(g, page):
        """量这一页被堆在**页末附录区**的中文字数（与引擎版本无关，直接读
        输出 PDF）。附录区起点优先用侧车的 `tail_y0`（引擎记的**真实**分隔线
        位置）；老侧车没有这个键，退回"最后一条切带的 dst 底边"——注意后者会
        把"每条切带本来就该在它下面的那块译文"也算进去，长段落页因此恒有
        15~21% 的假堆积，只宜作兜底。"""
        bot = None
        ty = g.get("tail_y0")
        if ty is not None:
            try:
                bot = float(ty)
            except (TypeError, ValueError):
                bot = None
        if bot is None:
            bands = g.get("bands") or []
            bot = 0.0
            for pair in bands:
                try:
                    bot = max(bot, float(pair[1][3]))
                except (IndexError, TypeError, ValueError):
                    continue
        ph = float(g.get("page_h") or page.rect.height)
        if ph - bot < 6.0:
            return 0, 0                    # 没有附录区
        park = tot = 0
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                t = "".join(s["text"] for s in l["spans"])
                k = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
                if not k:
                    continue
                tot += k
                if (l["bbox"][1] + l["bbox"][3]) / 2.0 > bot + 1.0:
                    park += k
        return park, tot

    doc = fitz.open(out_f)
    bad_pages, rows = [], []
    for i, g in enumerate(geo):
        if not isinstance(g, dict):
            continue
        n = int(g.get("nunits", 0) or 0)
        far = int(g.get("far", 0) or 0)
        app = int(g.get("appendix", 0) or 0)
        mj = float(g.get("maxjump", 0.0) or 0.0)
        aps, tot = _parked(g, doc[i]) if i < len(doc) else (0, 0)
        # 判据用的"堆积分量"：侧车给了 `app_far_cn`（被挪到附录、且离自己原文
        # >120pt 的中文字数）就用它；老侧车退回"附录区里的全部中文"。
        if g.get("app_far_cn") is not None:
            aps_j = int(g.get("app_far_cn") or 0)
        else:
            aps_j = aps
        ordn = int(g.get("order", 0) or 0)
        rep = float(g.get("tbl_rep", 0) or 0)
        denom = max(1, n)
        over = []
        # 可填表单页（geom.form_append）：表格 1:1 零改动、译文全部按设计
        # 送页末附录——页末堆积是该页的设计内行为，豁免检查。
        # 图纸 overlay 页（geom.ov_append）：图框内落不下的译文按设计送页末
        # 附录（DOC-A08/72：图框内只能落 1/25，丢光则整页无译文），同豁免。
        if g.get("form_append") or g.get("ov_append"):
            rows.append((i + 1, n, far, app, aps, tot, mj, ordn, rep,
                         ["form_append 豁免" if g.get("form_append")
                          else "overlay 附录豁免"]))
            continue
        if ordn:
            det = ""
            d0 = g.get("order_det") or []
            if d0:
                det = "（如 cut %.0f 的译文排到了 cut %.0f 那段之后：%s…）" \
                    % (float(d0[0][1]), float(d0[0][0]), d0[0][2])
            over.append("译文顺序颠倒 %d 处%s" % (ordn, det))
        if n >= 8 and far > max(3, FAR_RATIO * denom):
            over.append(f"落点过远 {far}/{n}")
        # 主判据：被挪到页末**且远离自己原文**的中文**字数**（及占比）。只数块数
        # 会漏掉"3 个块 = 整节译文"；只看"在附录区里"会把源页最后一段的正常续排
        # 也记进去（DOC-A05 p7 实测 21% 假堆积）。
        if aps_j >= 40 and (aps_j > PARK_ABS
                            or (tot > 0 and aps_j > PARK_RATIO * tot)):
            over.append("页末堆积 %d字/%d字(%.0f%%)"
                        % (aps_j, tot, 100.0 * aps_j / max(1, tot)))
        elif aps_j == 0 and n >= 8 \
                and app > max(APP_MIN, APP_RATIO * (n + app)):
            over.append(f"页末堆积 {app}/{n + app}")   # 兜底（按块数）
        # 表格整表复刻：清单/数据表（BOM）整表译会把输出页撑到 2 页多、
        # 长料号列被拆成一个字一行。阈值 0.9×源页高 —— 实测正常说明表格的
        # 复刻高度最高 0.56×源页高（25 张表里没有超过 0.6 的）。
        src_h = float(g.get("src_h", 0) or 0)
        if src_h > 0 and rep > 0.90 * src_h:
            over.append("表格整表复刻 %.0fpt > 0.9×源页高 %.0fpt"
                        "（清单类应只译表头）" % (rep, src_h))
        rows.append((i + 1, n, far, app, aps, tot, mj, ordn, rep, over))
        if over:
            bad_pages.append((i + 1, over))

    tiny = []
    for pi, p in enumerate(doc):
        for b in p.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                if not any("yahei" in s["font"].lower() for s in l["spans"]):
                    continue
                sz = min((s["size"] for s in l["spans"] if s["text"].strip()),
                         default=99.0)
                if sz < MIN_SIZE:
                    tiny.append((pi + 1, round(sz, 2),
                                 "".join(s["text"] for s in l["spans"])[:24]))
    if len(tiny) > max(3, 0.02 * len(doc)):
        bad_pages.append((0, [f"过小字号 {len(tiny)} 行"]))

    # 编号对应（逐条对照的机检口径）。阈值：单页允许 1 处——mono 的换行点与
    # 原文不同，一句话被拆到两个块时首尾会各带一次邻条编号，属可接受噪声；
    # 成片出现才是"整体错位/整段中文插错位置"。
    numbad = []
    for pi, p in enumerate(doc):
        g = geo[pi] if pi < len(geo) and isinstance(geo[pi], dict) else {}
        bands = g.get("bands") or []
        _bot = 0.0
        for pair in bands:
            try:
                _bot = max(_bot, float(pair[1][3]))
            except (IndexError, TypeError, ValueError):
                continue
        bad = _num_corr(p, _bot or None)
        if len(bad) > 1:
            bad_pages.append((pi + 1, [f"编号对应 {len(bad)} 处"]))
            numbad.append((pi + 1, bad))

    print("%-5s %-7s %-7s %-9s %-10s %-8s %-6s %s"
          % ("page", "units", "far", "appendix", "parked_cn", "maxjump",
             "order", "grid"))
    for pg, n, far, app, aps, tot, mj, ordn, rep, over in rows:
        flag = "  <== " + "; ".join(over) if over else ""
        tag = f"{aps}/{tot}" if tot else "0"
        print("%-5d %-7d %-7d %-9d %-10s %-8.1f %-6d %-6.0f%s"
              % (pg, n, far, app, tag, mj, ordn, rep, flag))
    for pg, sz, t in tiny[:10]:
        print(f"  过小字号 p{pg} {sz}pt {t!r}")
    for pg, bad in numbad[:4]:
        for y, got, want, t in bad[:4]:
            print(f"  编号错位 p{pg} y={y} 译文 {got} vs 原文 {want} {t!r}")
    print("layout warnings:", len(bad_pages))
    return 1 if bad_pages else 0


if __name__ == "__main__":
    sys.exit(main())
