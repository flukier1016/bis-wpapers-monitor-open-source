#!/usr/bin/env python
"""BIS 论文 PDF 取全文，并提供数字核验。

用法：
    python tools/pdftext.py 1346                # 打印全文
    python tools/pdftext.py 1346 --pages 1-12   # 只看某几页
    python tools/pdftext.py 1346 --grep "credit loss"
    python tools/pdftext.py 1346 --verify 0.35 1.81 "2.66"

--verify 是给中文总结做事实核查用的：逐个检查这些数字是否真的出现在原文里。
毕业论文引用一个不存在的系数，比不引用糟糕得多，所以每个数字都必须过这一关。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from pypdf import PdfReader

CACHE = Path(__file__).resolve().parent.parent / ".pdfcache"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_text(series: str, pdf_url: str = "") -> str:
    """下载并抽取全文，本地缓存，避免反复打扰 BIS。"""
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"work{series}.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8")

    url = pdf_url
    if not url:
        from app import config, db
        if config.DB_PATH.exists():
            conn = db.connect()
            try:
                row = db.get_paper(conn, f"/publ/work{series}")
                url = row["pdf_url"] if row else ""
            finally:
                conn.close()
    if not url:
        raise ValueError("PDF URL unavailable. Fetch the paper's details first or pass --pdf-url.")
    response = httpx.get(url, timeout=120, follow_redirects=True,
                         headers={"User-Agent": UA})
    response.raise_for_status()
    reader = PdfReader(io.BytesIO(response.content))
    pages = []
    for index, page in enumerate(reader.pages, 1):
        pages.append(f"\n\n===== [PAGE {index}] =====\n\n{page.extract_text() or ''}")
    text = "".join(pages)
    cached.write_text(text, encoding="utf-8")
    return text


def slice_pages(text: str, spec: str) -> str:
    start, _, end = spec.partition("-")
    lo, hi = int(start), int(end or start)
    chunks = re.split(r"===== \[PAGE (\d+)\] =====", text)
    out = []
    for i in range(1, len(chunks) - 1, 2):
        if lo <= int(chunks[i]) <= hi:
            out.append(f"===== [PAGE {chunks[i]}] =====\n{chunks[i + 1]}")
    return "\n".join(out)


def normalise(text: str) -> str:
    """PDF 抽取常把字母间插入空格（'cr edit'），核验数字时先压掉空白。"""
    return re.sub(r"\s+", "", text)


def verify(text: str, needles: list[str]) -> int:
    flat = normalise(text)
    missing = 0
    for needle in needles:
        target = normalise(needle)
        if target and target in flat:
            # 给出上下文，方便人工确认语义是否被曲解
            index = flat.find(target)
            print(f"  ✅ {needle!r:14} 原文命中  …{flat[max(0, index-60):index+60]}…")
        else:
            print(f"  ❌ {needle!r:14} 原文中找不到")
            missing += 1
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("series")
    parser.add_argument("--pdf-url", default="", help="BIS PDF URL if not yet stored in the database")
    parser.add_argument("--pages", default="")
    parser.add_argument("--grep", default="")
    parser.add_argument("--context", type=int, default=300)
    parser.add_argument("--verify", nargs="*", default=None)
    args = parser.parse_args()

    text = fetch_text(args.series, args.pdf_url)

    if args.verify is not None:
        print(f"核验 work{args.series} 中的 {len(args.verify)} 个数字：")
        missing = verify(text, args.verify)
        print(f"\n结果：{len(args.verify) - missing} 命中 / {missing} 缺失")
        return 1 if missing else 0

    if args.grep:
        for match in re.finditer(re.escape(args.grep), text, re.I):
            lo = max(0, match.start() - args.context)
            print(f"--- @{match.start()} ---\n{text[lo:match.end() + args.context]}\n")
        return 0

    print(slice_pages(text, args.pages) if args.pages else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
