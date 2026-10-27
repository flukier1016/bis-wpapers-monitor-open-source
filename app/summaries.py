"""中文深度总结层。

与自动抓取的四段式摘要不同，这里的内容是**通读 PDF 全文之后写的**，
覆盖抓取拿不到的那三个维度（Key evidence、Limitations、Implications）。

存储方式刻意选了「仓库里的 JSON 文件 + 启动时载入数据库」：
  * 放仓库 -> 可版本管理、可 diff、可复查，卷丢了也不会丢总结
  * 载入库 -> 仪表盘搜索能覆盖到中文正文

写作规范（见 summaries/README.md）：
  * key_evidence 里的每个数字都必须能在 PDF 原文里逐字找到，
    并标注它出自哪张表/哪张图 —— 毕业论文引用错系数比没有系数糟糕得多。
  * 作者结论与二次分析分开标注，不把"我的推断"写成"作者发现"。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger(__name__)

SUMMARY_DIR = Path(__file__).resolve().parent.parent / "summaries"

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    series            TEXT PRIMARY KEY,
    title_zh          TEXT NOT NULL DEFAULT '',
    citation          TEXT NOT NULL DEFAULT '',
    theme             TEXT NOT NULL DEFAULT '',
    research_question TEXT NOT NULL DEFAULT '',
    core_findings     TEXT NOT NULL DEFAULT '[]',
    method_and_data   TEXT NOT NULL DEFAULT '',
    key_evidence      TEXT NOT NULL DEFAULT '[]',
    contribution      TEXT NOT NULL DEFAULT '',
    limitations       TEXT NOT NULL DEFAULT '',
    implications      TEXT NOT NULL DEFAULT '',
    key_takeaway      TEXT NOT NULL DEFAULT '',
    thesis            TEXT NOT NULL DEFAULT '{}',
    review            TEXT NOT NULL DEFAULT '{}',
    review_status     TEXT NOT NULL DEFAULT 'pending',
    generated_by      TEXT NOT NULL DEFAULT '',
    generated_at      TEXT NOT NULL DEFAULT '',
    source_chars      INTEGER NOT NULL DEFAULT 0,
    -- 印刷页码 -> PDF 页码的偏移，及该偏移适用到第几页（附录常重新编号）
    pdf_page_offset      INTEGER NOT NULL DEFAULT 0,
    pdf_page_valid_until INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_summaries_theme ON summaries(theme);
"""

# 质检状态。刻意让「未通过」在页面上显眼——首轮 13 篇全部没通过质检，
# 问题不是编造数字（一个都没有），而是把作者的单变量结论说成多变量结论、
# 跨 horizon 混用系数、比较关系写反这类归属错误。这些正是答辩时会被当场
# 问倒的东西，所以宁可显示警告，也不能让人以为通过了核验。
REVIEW_LABELS = {
    "passed": ("Reviewed", "Reviewed against the original PDF; verify before citing"),
    "issues": ("Review issues", "Unresolved issues; check the original before citing"),
    "pending": ("Pending review", "Not independently reviewed; check the original before citing"),
}

# 落库时需要序列化成 JSON 的字段
_JSON_FIELDS = ("core_findings", "key_evidence", "thesis", "review")
_TEXT_FIELDS = (
    "title_zh", "citation", "research_question", "method_and_data",
    "contribution", "limitations", "implications", "key_takeaway",
    "generated_by", "generated_at",
)

# 渲染顺序：中文标题 -> 七个维度。与用户最初提的七点一一对应。
SECTIONS: list[tuple[str, str]] = [
    ("research_question", "Research question"),
    ("core_findings", "Core findings"),
    ("method_and_data", "Method and data"),
    ("key_evidence", "Key evidence"),
    ("contribution", "Contribution"),
    ("limitations", "Limitations"),
    ("implications", "Implications"),
]


# 建表之后新增的字段。老库里没有这些列，靠 ensure_columns 补上 ——
# 否则加一个字段就会让已部署的实例在启动时崩掉。
ADDED_COLUMNS = {
    "review": "TEXT NOT NULL DEFAULT '{}'",
    "review_status": "TEXT NOT NULL DEFAULT 'pending'",
    "pdf_page_offset": "INTEGER NOT NULL DEFAULT 0",
    "pdf_page_valid_until": "INTEGER NOT NULL DEFAULT 0",
}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    db.ensure_columns(conn, "summaries", ADDED_COLUMNS)
    conn.commit()


def upsert(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    series = str(data.get("series", "")).strip()
    if not series:
        raise ValueError("总结缺少 series 字段")

    values: dict[str, Any] = {"series": series}
    for field in _TEXT_FIELDS:
        values[field] = str(data.get(field, "") or "")
    for field in _JSON_FIELDS:
        default: Any = {} if field in ("thesis", "review") else []
        values[field] = json.dumps(data.get(field, default), ensure_ascii=False)
    values["theme"] = str((data.get("thesis") or {}).get("theme", ""))
    values["source_chars"] = int(data.get("source_chars") or 0)
    values["pdf_page_offset"] = int(data.get("pdf_page_offset") or 0)
    values["pdf_page_valid_until"] = int(data.get("pdf_page_valid_until") or 0)

    status = str((data.get("review") or {}).get("status", "") or "pending")
    values["review_status"] = status if status in REVIEW_LABELS else "pending"

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != "series")
    conn.execute(
        f"INSERT INTO summaries ({columns}) VALUES ({placeholders})"
        f" ON CONFLICT(series) DO UPDATE SET {updates}",
        list(values.values()),
    )


def load_from_disk(conn: sqlite3.Connection, directory: Path | None = None) -> int:
    """把 summaries/*.json 全部载入数据库。启动时调用，幂等。"""
    root = Path(directory or SUMMARY_DIR)
    if not root.is_dir():
        return 0
    loaded = 0
    for path in sorted(root.glob("work*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            upsert(conn, data)
            loaded += 1
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            log.error("载入中文总结失败 %s：%s", path.name, exc)
    conn.commit()
    if loaded:
        log.info("已载入 %d 份中文深度总结", loaded)
    return loaded


def get(conn: sqlite3.Connection, series: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE series = ?", (str(series),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM summaries ORDER BY CAST(series AS INTEGER) DESC"
    )
    return [_row_to_dict(r) for r in rows]


def neighbours(
    conn: sqlite3.Connection, series: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """当前总结在「同主题优先、再按刊号」顺序下的上一篇与下一篇。

    写文献综述时是按主题成组读的，所以同主题的排在一起翻更顺手；
    读完一个主题会自然滑到下一个主题，不至于走到死胡同。
    """
    items = list_all(conn)
    if not items:
        return None, None
    items.sort(key=lambda x: (x.get("theme") or "", -int(x["series"])))
    index = next(
        (i for i, x in enumerate(items) if str(x["series"]) == str(series)), None
    )
    if index is None:
        return None, None
    prev_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index + 1 < len(items) else None
    return prev_item, next_item


def series_with_summary(conn: sqlite3.Connection) -> set[str]:
    return {r["series"] for r in conn.execute("SELECT series FROM summaries")}


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM summaries").fetchone()["n"])


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for field in _JSON_FIELDS:
        default: Any = {} if field in ("thesis", "review") else []
        try:
            data[field] = json.loads(data.get(field) or "null")
        except (json.JSONDecodeError, TypeError):
            data[field] = default
        if data[field] is None:
            data[field] = default

    status = data.get("review_status") or "pending"
    label, note = REVIEW_LABELS.get(status, REVIEW_LABELS["pending"])
    data["review_label"] = label
    data["review_note"] = note
    data["review_ok"] = status == "passed"
    return data


def review_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT review_status, COUNT(*) AS n FROM summaries GROUP BY review_status"
    )
    counts = {status: 0 for status in REVIEW_LABELS}
    for row in rows:
        counts[row["review_status"]] = row["n"]
    return counts
