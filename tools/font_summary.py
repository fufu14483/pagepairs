# -*- coding: utf-8 -*-
"""
font_summary.py — 汇总 font_audit.py 产出的 JSON。

用法：
    python tools/font_audit.py <字体目录> <输出.json>
    python tools/font_summary.py <输出.json>
"""
import json
import re
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)

    print("总字体数: %d" % len(data))

    print()
    print("=== 许可证分布 ===")
    counts = {}
    for r in data:
        lic = (r.get("license") or "").strip()
        if "SIL Open Font License" in lic:
            key = "OFL-1.1"
        elif not lic:
            key = "未声明许可证"
        else:
            key = "其他: " + lic[:40]
        counts[key] = counts.get(key, 0) + 1
    for k in sorted(counts, key=lambda x: -counts[x]):
        print("  %-24s %d" % (k, counts[k]))

    print()
    print("=== 未声明许可证的字体（需人工确认）===")
    found = False
    for r in data:
        if not (r.get("license") or "").strip():
            found = True
            print("  %s" % r["file"])
            print("      family    : %s" % r.get("family"))
            print("      copyright : %s" % r.get("copyright"))
            fs = r.get("fstype")
            if fs is not None:
                print("      fsType    : 0x%04X" % fs)
    if not found:
        print("  （无）")

    print()
    print("=== Reserved Font Name 声明 ===")
    seen = set()
    for r in data:
        c = r.get("copyright") or ""
        if "Reserved Font Name" in c:
            for m in re.findall(r"Reserved Font Name '?([A-Za-z]+)'?", c):
                if m not in seen:
                    seen.add(m)
                    print("  '%s'   (例: %s)" % (m, r["file"]))
    if not seen:
        print("  （无）")

    print()
    print("=== 字体族 ===")
    fam = {}
    for r in data:
        f = (r.get("family") or "?").split("-")[0].strip()
        fam[f] = fam.get(f, 0) + 1
    for k in sorted(fam):
        print("  %-22s %d" % (k, fam[k]))

    print()
    print("=== fsType 非 0（含嵌入限制位）===")
    any_r = False
    for r in data:
        fs = r.get("fstype")
        if fs:
            any_r = True
            print("  %-32s 0x%04X" % (r["file"], fs))
    if not any_r:
        print("  （全部为 0x0000 installable）")


if __name__ == "__main__":
    main()
