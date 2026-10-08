# -*- coding: utf-8 -*-
"""
font_audit.py — 读字体文件内嵌的 name 表，核实授权信息。

不依赖联网：直接解析 TrueType/OpenType 的 name 表，取出
  ID 0  = Copyright
  ID 1  = Font Family
  ID 7  = Trademark
  ID 13 = License Description
  ID 14 = License URL
以及 OS/2 / head 表的 fsType（嵌入限制位）。
"""
import os
import struct
import sys

# Windows 控制台默认 GBK，字体元数据含 ©/乱码字符会炸；强制 UTF-8 替换输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------- name ids
NAME_IDS = {
    0: "Copyright",
    1: "Family",
    3: "UniqueID",
    7: "Trademark",
    8: "Manufacturer",
    9: "Designer",
    11: "VendorURL",
    12: "DesignerURL",
    13: "LicenseDescription",
    14: "LicenseURL",
}

# fsType 位含义（OS/2 表）
FSTYPE_BITS = [
    (0x0002, "Restricted License embedding"),
    (0x0004, "Preview & Print embedding"),
    (0x0008, "Editable embedding"),
    (0x0100, "No subsetting"),
    (0x0200, "Bitmap embedding only"),
]


def read_name_table(fp):
    """解析 name 表，返回 {nameID: [(platformID, text), ...]}。"""
    with open(fp, "rb") as fh:
        data = fh.read()

    if len(data) < 12:
        return None, None, "文件过小"

    tag = data[:4]
    num_tables = struct.unpack(">H", data[4:6])[0]

    tables = {}
    for i in range(num_tables):
        off = 12 + i * 16
        if off + 16 > len(data):
            break
        t = data[off:off + 4]
        o, l = struct.unpack(">II", data[off + 8:off + 16])
        tables[t] = (o, l)

    # ---- name 表
    names = {}
    if b"name" in tables:
        no, nl = tables[b"name"]
        seg = data[no:no + nl]
        if len(seg) >= 6:
            count, str_off = struct.unpack(">HH", seg[2:6])
            for i in range(count):
                rec = 6 + i * 12
                if rec + 12 > len(seg):
                    break
                pid, eid, lid, nid, ln, off = struct.unpack(">HHHHHH", seg[rec:rec + 12])
                raw = seg[str_off + off:str_off + off + ln]
                try:
                    if pid == 3 or pid == 0:       # Windows / Unicode
                        txt = raw.decode("utf-16-be", "replace")
                    elif pid == 1:                  # Mac
                        txt = raw.decode("mac-roman", "replace")
                    else:
                        txt = raw.decode("utf-8", "replace")
                except Exception:
                    txt = repr(raw)
                txt = txt.strip().strip("\x00")
                if txt:
                    names.setdefault(nid, []).append((pid, txt))

    # ---- OS/2 的 fsType
    fstype = None
    if b"OS/2" in tables:
        oo, ol = tables[b"OS/2"]
        if ol >= 10:
            fstype = struct.unpack(">H", data[oo + 8:oo + 10])[0]

    return names, fstype, None


def pick(names, nid):
    """取某 name ID 的可读文本（优先 Windows 平台记录）。"""
    if nid not in names:
        return ""
    recs = names[nid]
    for pid, txt in recs:
        if pid == 3:
            return txt
    return recs[0][1]


def main():
    fonts_dir = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None

    files = sorted(
        f for f in os.listdir(fonts_dir)
        if f.lower().endswith((".ttf", ".otf", ".ttc"))
    )

    results = []
    for fn in files:
        fp = os.path.join(fonts_dir, fn)
        names, fstype, err = read_name_table(fp)
        if err:
            results.append({"file": fn, "error": err})
            continue
        results.append({
            "file": fn,
            "family": pick(names, 1),
            "copyright": pick(names, 0),
            "trademark": pick(names, 7),
            "license": pick(names, 13),
            "license_url": pick(names, 14),
            "manufacturer": pick(names, 8),
            "fstype": fstype,
        })

    # 控制台摘要
    for r in results:
        if r.get("error"):
            print("!! %s : %s" % (r["file"], r["error"]))
            continue
        print("=" * 78)
        print("%s" % r["file"])
        print("  family      : %s" % r["family"])
        print("  copyright   : %s" % (r["copyright"][:150] or "(none)"))
        print("  license     : %s" % (r["license"][:200] or "(none)"))
        print("  licenseURL  : %s" % (r["license_url"] or "(none)"))
        if r["trademark"]:
            print("  trademark   : %s" % r["trademark"][:100])
        fs = r["fstype"]
        if fs is not None:
            flags = [lab for bit, lab in FSTYPE_BITS if fs & bit]
            print("  fsType      : 0x%04X %s" % (fs, (" <- " + ", ".join(flags)) if flags else "(installable)"))

    if out_path:
        import json
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print("\nsaved JSON -> %s" % out_path)


if __name__ == "__main__":
    main()
