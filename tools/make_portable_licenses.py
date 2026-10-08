# -*- coding: utf-8 -*-
"""
make_portable_licenses.py — 为便携包生成字体许可证目录。

作用：解析便携包内每个字体文件的内嵌元数据，在便携包中生成
`FONT-LICENSES/` 目录，包含：
  - OFL.txt                  SIL OFL 1.1 全文（33 个字体共用）
  - FONT-NOTICES.txt         逐字体的版权声明（OFL 条件 2 要求）
  - README.txt               说明本目录的用途
  - MaruBuri 的处理            见 --drop-maruburi / --keep-maruburi

用法：
    python tools/make_portable_licenses.py <便携包目录>
    python tools/make_portable_licenses.py <便携包目录> --drop-maruburi
    python tools/make_portable_licenses.py <便携包目录> --dry-run

背景见仓库 FONTS.md。核心结论：
  - 34 个字体中 33 个为 SIL OFL-1.1（需随附许可证全文）
  - MaruBuri-Regular.ttf 未声明许可证，再分发授权不明
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_audit import read_name_table, pick  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OFL_SRC = os.path.join(HERE, "licenses", "OFL-1.1.txt")

FONT_SUBDIR = os.path.join("engine", "models", ".cache", "babeldoc", "fonts")
OUT_SUBDIR = "FONT-LICENSES"

OF_HEADER = """便携包字体许可证 / Font Licenses
=================================

本目录为便携包内 `engine\\models\\.cache\\babeldoc\\fonts\\` 所含字体文件的
许可证声明。

为什么需要这个目录
------------------
包内 34 个字体文件中，33 个采用 SIL Open Font License 1.1（OFL-1.1）。
OFL-1.1 的「许可与条件」第 2 条明确要求：

    Original or Modified Versions of the Font Software may be bundled,
    redistributed and/or sold with any software, provided that each copy
    contains the above copyright notice and this license.

即：再分发时必须随附**版权声明**与**许可证全文**。
`OFL.txt` 为许可证全文，`FONT-NOTICES.txt` 为逐字体的版权声明。

本包是否修改过字体
------------------
没有。字体文件按原样分发，未做任何修改。因此 OFL 条件 3
（保留字体名 / Reserved Font Name）不适用。

需注意 Adobe 思源系列（Source Han Sans / Serif）声明了保留字体名
'Source'；若日后重新打包或改造这些字体文件，必须遵守该限制。

与 AGPL 的关系
--------------
本工具整体以 AGPL-3.0 发布，其 PDF 引擎依赖 PyMuPDF（AGPL-3.0）。
字体采用 OFL-1.1，两者互不冲突：OFL 仅约束字体本身，且明确说明
「使用该字体创建的任何文档不受本许可证约束」。
"""


def gather(fonts_dir):
    """解析字体目录，返回记录列表。"""
    out = []
    for fn in sorted(os.listdir(fonts_dir)):
        if not fn.lower().endswith((".ttf", ".otf", ".ttc")):
            continue
        names, fstype, err = read_name_table(os.path.join(fonts_dir, fn))
        if err:
            out.append({"file": fn, "error": err})
            continue
        lic = pick(names, 13)
        out.append({
            "file": fn,
            "family": pick(names, 1),
            "copyright": pick(names, 0),
            "trademark": pick(names, 7),
            "license": lic,
            "license_url": pick(names, 14),
            "fstype": fstype,
            "is_ofl": "SIL Open Font License" in (lic or ""),
        })
    return out


def build_notices(records, dropped):
    """生成 FONT-NOTICES.txt 正文。"""
    ofl = [r for r in records if r.get("is_ofl")]
    other = [r for r in records if not r.get("is_ofl") and not r.get("error")]

    lines = []
    lines.append("便携包字体版权声明 / Font Copyright Notices")
    lines.append("=" * 60)
    lines.append("")
    lines.append("本文件列出便携包内每个字体文件的版权与许可证信息，")
    lines.append("数据直接取自各字体文件内嵌的 name 表（ID 0/1/7/13/14）。")
    lines.append("")
    lines.append("许可证全文见同目录 OFL.txt（SIL OFL 1.1）。")
    lines.append("")
    lines.append("-" * 60)
    lines.append("")

    # 按字体族分组，避免 34 条重复
    groups = {}
    for r in ofl:
        fam = (r.get("family") or "?").split("-")[0].strip()
        groups.setdefault(fam, {"files": [], "copyright": r.get("copyright", ""),
                                "trademark": r.get("trademark", "")})
        groups[fam]["files"].append(r["file"])

    lines.append("一、SIL Open Font License 1.1 字体（%d 个文件 / %d 个字体族）"
                 % (len(ofl), len(groups)))
    lines.append("")
    for fam in sorted(groups):
        g = groups[fam]
        lines.append("  %s" % fam)
        for f in sorted(g["files"]):
            lines.append("      %s" % f)
        if g["copyright"]:
            for cl in g["copyright"].splitlines():
                if cl.strip():
                    lines.append("      版权: %s" % cl.strip())
        if g["trademark"]:
            lines.append("      商标: %s" % g["trademark"].strip())
        lines.append("      许可: SIL Open Font License 1.1（见 OFL.txt）")
        lines.append("")

    if dropped:
        lines.append("-" * 60)
        lines.append("")
        lines.append("已从本包中移除的字体（%d 个）" % len(dropped))
        lines.append("")
        for d in dropped:
            lines.append("  %s" % d["file"])
            if d.get("copyright"):
                lines.append("      版权: %s" % d["copyright"].strip())
            lines.append("      原因: 字体未声明许可证，再分发授权不明，已移除以避免风险")
            lines.append("")

    if other:
        lines.append("-" * 60)
        lines.append("")
        lines.append("二、其他许可证状态的字体（%d 个）——需人工确认" % len(other))
        lines.append("")
        for r in other:
            lines.append("  %s" % r["file"])
            lines.append("      族名: %s" % r.get("family"))
            if r.get("copyright"):
                lines.append("      版权: %s" % r["copyright"].strip())
            lines.append("      许可: 未在字体元数据中声明")
            lines.append("")

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("portable", help="便携包目录")
    ap.add_argument("--drop-maruburi", action="store_true",
                    help="删除 MaruBuri-Regular.ttf（授权不明的字体）")
    ap.add_argument("--keep-maruburi", action="store_true",
                    help="保留 MaruBuri（仅在你已确认其授权后使用）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    a = ap.parse_args()

    root = os.path.abspath(a.portable)
    fonts_dir = os.path.join(root, FONT_SUBDIR)
    if not os.path.isdir(fonts_dir):
        sys.exit("找不到字体目录: %s" % fonts_dir)

    records = gather(fonts_dir)
    print("解析到 %d 个字体文件" % len(records))

    ofl = [r for r in records if r.get("is_ofl")]
    unknown = [r for r in records if not r.get("is_ofl") and not r.get("error")]
    print("  OFL-1.1 : %d" % len(ofl))
    print("  未声明  : %d %s" % (len(unknown),
                                [r["file"] for r in unknown] if unknown else ""))

    # ---- 未声明许可证的字体处置
    dropped = []
    if unknown and not a.keep_maruburi:
        if not a.drop_maruburi:
            print()
            print("!! 存在未声明许可证的字体：%s" % [r["file"] for r in unknown])
            print("!! 请明确选择其一：")
            print("!!   --drop-maruburi   删除（推荐，包内思源 KR 已覆盖韩文）")
            print("!!   --keep-maruburi   保留（仅在你已向权利人确认授权后）")
            sys.exit(3)
        for r in unknown:
            dropped.append(r)
            tgt = os.path.join(fonts_dir, r["file"])
            if a.dry_run:
                print("  [dry-run] 将删除 %s" % r["file"])
            else:
                os.remove(tgt)
                print("  已删除 %s" % r["file"])

    # ---- 写许可证目录
    out = os.path.join(root, OUT_SUBDIR)
    ofl_text = ""
    if os.path.exists(OFL_SRC):
        with open(OFL_SRC, encoding="utf-8") as fh:
            ofl_text = fh.read()

    if a.dry_run:
        print()
        print("[dry-run] 将生成 %s\\OFL.txt 与 %s\\FONT-NOTICES.txt"
              % (OUT_SUBDIR, OUT_SUBDIR))
        if dropped:
            print("[dry-run] 上述 %d 个字体将被移除" % len(dropped))
        return

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "OFL.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(ofl_text)
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(OF_HEADER)
    with open(os.path.join(out, "FONT-NOTICES.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(build_notices([r for r in records if r not in dropped], dropped))

    print()
    print("已生成 %s\\" % OUT_SUBDIR)
    for f in sorted(os.listdir(out)):
        sz = os.path.getsize(os.path.join(out, f))
        print("  %-20s %d bytes" % (f, sz))


if __name__ == "__main__":
    main()
