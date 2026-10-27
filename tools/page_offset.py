#!/usr/bin/env python
"""计算 BIS 论文「印刷页码 → PDF 页码」的偏移量。

为什么需要这个
--------------
中文总结里大量引用「印刷第16页」。BIS 的 PDF 前面有封面、免责声明等数页，
所以印刷页 1 通常落在 PDF 的第 3 或第 4 页。直接把印刷页码当 PDF 页码跳转，
会跳到早几页的位置——读者对不上内容，很可能误判成总结写错了。

怎么算
------
逐页找页眉页脚里的独立数字（就是印刷页码），与该页的 PDF 页码作差。
正常情况下几十页会给出完全一致的差值。只有在**绝大多数页达成一致**时
才采信，否则返回 None，让调用方退回「不生成跳转链接」——
宁可不给链接，也不能给一个跳错地方的链接。

用法
----
    python tools/page_offset.py 1366              # 只看某一篇
    python tools/page_offset.py --all             # 全部总结
    python tools/page_offset.py --all --write     # 算完写回 summaries/*.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.pdftext import fetch_text  # noqa: E402

SUMMARY_DIR = Path(__file__).resolve().parent.parent / "summaries"
_PAGE_SPLIT = re.compile(r"===== \[PAGE (\d+)\] =====")
_PURE_NUMBER = re.compile(r"\d{1,3}")

# 至少这么多页给出同一个差值，才认为偏移可信
MIN_AGREEING_PAGES = 8
# 一致率下限：达成共识的页数 / 找到页码的页数
MIN_CONSENSUS = 0.85


def detect_offset(series: str) -> tuple[int | None, int, dict[str, object]]:
    """返回 (偏移量, 该偏移适用的最大印刷页码, 诊断信息)。

    为什么要带一个「适用到第几页」：部分论文的附录会重新从第 1 页编号
    （实测 work1354 就是这样，正文偏移 +3、附录偏移 +47）。若只取众数，
    引用附录页码时会跳错几十页。这里取**从正文开头连续成立**的那一段，
    范围之外一律不生成链接——宁可不给链接，也不能给跳错地方的链接。
    """
    text = fetch_text(series)
    parts = _PAGE_SPLIT.split(text)

    # (印刷页码, 偏移量)，按 PDF 页顺序
    observed: list[tuple[int, int]] = []
    for i in range(1, len(parts) - 1, 2):
        pdf_page = int(parts[i])
        lines = [ln.strip() for ln in parts[i + 1].strip().splitlines() if ln.strip()]
        if not lines:
            continue
        for line in lines[:2] + lines[-3:]:
            if _PURE_NUMBER.fullmatch(line):
                observed.append((int(line), pdf_page - int(line)))
                break

    if not observed:
        return None, 0, {"reason": "没有找到任何印刷页码", "samples": 0}

    counter = Counter(off for _, off in observed)
    best, count = counter.most_common(1)[0]

    # 从头开始，众数偏移连续成立到哪一页为止（允许零星几页抽取失败）
    valid_until = 0
    misses = 0
    for printed, off in observed:
        if off == best:
            valid_until = max(valid_until, printed)
            misses = 0
        else:
            misses += 1
            if misses >= 2:      # 连续两页对不上，判定为进入了另一套编号
                break

    info = {
        "samples": len(observed),
        "agreeing": count,
        "consensus": round(count / len(observed), 3),
        "distribution": dict(counter.most_common(4)),
        "valid_until": valid_until,
    }
    if count < MIN_AGREEING_PAGES:
        info["reason"] = "一致的页数太少，不采信"
        return None, 0, info
    if count < len(observed) * MIN_CONSENSUS:
        info["note"] = "存在第二套页码（多半是附录重新编号），只对正文段生效"
    return best, valid_until, info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("series", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--write", action="store_true", help="写回 summaries/*.json")
    args = parser.parse_args()

    if args.all:
        targets = sorted(
            p.stem.replace("work", "") for p in SUMMARY_DIR.glob("work*.json")
        )
    elif args.series:
        targets = [args.series]
    else:
        parser.error("给一个刊号，或用 --all")

    written = 0
    for series in targets:
        try:
            offset, valid_until, info = detect_offset(series)
        except Exception as exc:            # 网络或解析问题不该中断整批
            print(f"  work{series}: 失败 {type(exc).__name__}: {exc}")
            continue

        if offset is None:
            print(f"  work{series}: 无法确定偏移 · {info}")
        else:
            note = f" · {info['note']}" if info.get("note") else ""
            print(
                f"  work{series}: 偏移 +{offset}"
                f"（{info['agreeing']}/{info['samples']} 页一致，"
                f"{info['consensus']:.0%}）"
                f" → 印刷第 1 页 = PDF 第 {1 + offset} 页"
                f"，适用到印刷第 {valid_until} 页{note}"
            )

        if args.write:
            path = SUMMARY_DIR / f"work{series}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            # 确定不了就写 0，渲染层据此不生成跳转链接
            data["pdf_page_offset"] = offset if offset is not None else 0
            data["pdf_page_valid_until"] = valid_until if offset is not None else 0
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written += 1

    if args.write:
        print(f"\n已写回 {written} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
