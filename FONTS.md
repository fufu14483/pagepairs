# 便携包字体许可证核实

本文件记录便携包 `engine\models\.cache\babeldoc\fonts\` 中 34 个字体文件的**内嵌授权信息**。

核实方法：直接解析每个字体文件的 `name` 表（ID 0/1/7/13/14）与 `OS/2` 表的 `fsType` 位，
**不依赖任何外部检索或人工推测**。原始数据可用脚本重新生成。

## 结论摘要

| 字体族 | 文件数 | 许可证 | 来源 |
|---|---|---|---|
| Noto Sans | 4 | **OFL-1.1** | Google / Noto Project |
| Noto Serif | 4 | **OFL-1.1** | Google / Noto Project |
| Go Noto Kurrent | 2 | **OFL-1.1** | Noto Project |
| Source Han Sans（思源黑体，CN/HK/JP/KR/TW） | 10 | **OFL-1.1** | Adobe，保留字体名 `Source` |
| Source Han Serif（思源宋体，CN/HK/JP/KR/TW） | 10 | **OFL-1.1** | Adobe，保留字体名 `Source` |
| LXGW WenKai GB（霞鹜文楷） | 1 | **OFL-1.1** | LXGW（基于 Klee） |
| 霞鶩文楷 TC | 1 | **OFL-1.1** | LXGW |
| Klee One | 1 | **OFL-1.1** | Fontworks Inc. |
| **MaruBuri Regular** | 1 | ⚠️ **未声明许可证** | © NAVER Corp. |

**33/34 明确为 SIL OFL 1.1，内嵌许可证声明可直接取证。**

## ⚠️ 需要你决策的一项：MaruBuri

`MaruBuri-Regular.ttf` 是唯一异常项：

- `name` 表**没有** License Description（ID 13）与 License URL（ID 14）字段
- 仅有版权声明：`© NAVER Corp. © NAVER Cultural Foundation Corp.`
- `fsType = 0x0008` —— **Editable embedding**（允许嵌入编辑，但该位是字体嵌入权限，
  **不等于**授予再分发权）

即：**这个字体是否允许再分发，无法从文件本身判定。** 需要到 NAVER 官方渠道
（Naver 字体页面）确认其授权条款。

处理建议（三选一）：

1. **从便携包中删除该字体** —— 最省事。它只用于韩文渲染，
   而包内已有 Source Han Sans/Serif 的 KR 字重覆盖韩文
2. **确认授权后保留**，并在便携包内附上其官方许可证全文
3. **替换为 Noto Sans KR**（OFL-1.1，可直接再分发）

## OFL-1.1 的合规要求

OFL 允许免费使用、修改、再分发（含商业用途），但要求：

1. **随附许可证全文** —— 当前便携包内**没有任何字体许可证文件**，
   这是必须补的首要动作
2. **保留版权声明** —— 不得删除字体内的 copyright / license 字段
3. **保留字体名（Reserved Font Name）** —— 若修改字体，不得继续使用保留名。
   本包中需注意：**`Source`（Adobe 思源系列）**

> 本项目只是**嵌入字体到 PDF 输出**、并未修改字体文件本身，因此第 3 条一般不触发。
> 但如需重新打包字体文件，务必遵守。

## 待办

- [ ] 为 33 个 OFL 字体补齐 `OFL.txt` 全文（随便携包分发）
- [ ] 就 MaruBuri 做出决策（删 / 换 / 确认授权）
- [ ] 便携包内加 `FONT-LICENSES\` 目录存放上述文件

---

生成方式：`_font_audit.py`（解析 `name`/`OS/2` 表）+ `_font_summary.py`（汇总）。
数据来源为字体文件自身元数据，可复现。
