"""SQLite 状态层。

设计要点
  * 主键是 BIS 的 path（/publ/work1369），天然保证同一篇论文不会被重复处理。
  * reported_at 为 NULL 表示"尚未在任何一期简报中出现过"——这就是"新论文"的定义。
  * is_baseline=1 是首次部署时播种的历史论文：入库但永不进简报，
    避免第一次运行就把 1300+ 篇历史论文当成新论文推送出去。
"""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import config
from .bis import PaperDetail, PaperStub

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    path            TEXT PRIMARY KEY,
    series          TEXT NOT NULL,
    title           TEXT NOT NULL,
    authors         TEXT NOT NULL DEFAULT '[]',
    pub_date        TEXT NOT NULL DEFAULT '',
    abstract_url    TEXT NOT NULL DEFAULT '',
    pdf_url         TEXT NOT NULL DEFAULT '',
    pdf_pages       INTEGER,
    pdf_size_kb     INTEGER,
    jel_codes       TEXT NOT NULL DEFAULT '[]',
    keywords        TEXT NOT NULL DEFAULT '[]',
    focus           TEXT NOT NULL DEFAULT '',
    contribution    TEXT NOT NULL DEFAULT '',
    findings        TEXT NOT NULL DEFAULT '',
    abstract        TEXT NOT NULL DEFAULT '',
    detail_fetched  INTEGER NOT NULL DEFAULT 0,
    relevance       INTEGER NOT NULL DEFAULT 0,
    relevance_why   TEXT NOT NULL DEFAULT '',
    first_seen_at   TEXT NOT NULL,
    reported_at     TEXT,
    is_baseline     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_papers_pub_date  ON papers(pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_papers_reported  ON papers(reported_at);
CREATE INDEX IF NOT EXISTS idx_papers_detail    ON papers(detail_fetched);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    trigger       TEXT NOT NULL DEFAULT 'schedule',
    listed_count  INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    is_baseline   INTEGER NOT NULL DEFAULT 0,
    email_status  TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    new_paths     TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

-- 投递账本：解决"邮件已发出、但标记未落库时进程被杀"导致的重复发信。
-- 协议是发信前先落一条 status='sending' 并提交；发完再改成 sent/failed。
-- 启动时若发现残留的 sending，说明上次是在发信窗口里被杀的，一律按"可能已送达"
-- 处理（宁可少发也不重复轰炸），同时留下告警。
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    created_at  TEXT NOT NULL,
    settled_at  TEXT,
    status      TEXT NOT NULL,      -- sending | sent | failed | skipped | assumed_sent
    detail      TEXT NOT NULL DEFAULT '',
    paths       TEXT NOT NULL DEFAULT '[]',
    channel     TEXT NOT NULL DEFAULT 'smtp'   -- smtp（进程内发） | external（外部代发）
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
"""

# 建表之后新增的列，靠 ensure_columns 补到老库上
DELIVERY_ADDED_COLUMNS = {"channel": "TEXT NOT NULL DEFAULT 'smtp'"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> list[str]:
    """给已存在的表补上新增的列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作 —— 它不会补列。
    所以每加一个字段，老库启动时就会以
    `no such column` / `table X has no column named Y` 崩掉。
    这里做最轻量的迁移：比对 PRAGMA table_info 后 ALTER TABLE ADD COLUMN。
    只增不改不删，因此重复执行是安全的。
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not existing:          # 表还不存在，由 CREATE TABLE 负责
        return []
    added: list[str] = []
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(name)
    if added:
        log.info("为表 %s 补充字段：%s", table, ", ".join(added))
    return added


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    ensure_columns(conn, "deliveries", DELIVERY_ADDED_COLUMNS)
    conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 写入
def known_paths(conn: sqlite3.Connection) -> set[str]:
    return {row["path"] for row in conn.execute("SELECT path FROM papers")}


def is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM papers LIMIT 1").fetchone() is None


def insert_stub(conn: sqlite3.Connection, stub: PaperStub, *, baseline: bool) -> None:
    """插入一条新发现的论文。

    已存在时只刷新列表级字段（标题/作者/日期）—— BIS 事后更正标题的情况确实存在，
    而抓来的摘要、评分、reported_at 等状态字段一律保持不动。
    """
    conn.execute(
        """
        INSERT INTO papers (path, series, title, authors, pub_date,
                            abstract_url, pdf_url, first_seen_at, is_baseline,
                            reported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            title    = excluded.title,
            authors  = CASE WHEN excluded.authors != '[]'
                            THEN excluded.authors ELSE papers.authors END,
            pub_date = excluded.pub_date,
            abstract_url = excluded.abstract_url
        """,
        (
            stub.path,
            stub.series,
            stub.title,
            json.dumps(stub.authors, ensure_ascii=False),
            stub.pub_date,
            stub.abstract_url,
            stub.pdf_url,
            utcnow(),
            1 if baseline else 0,
            # 基线论文视为"已报告"，这样永远不会出现在简报里
            utcnow() if baseline else None,
        ),
    )


def update_detail(
    conn: sqlite3.Connection,
    path: str,
    detail: PaperDetail,
    *,
    relevance: int = 0,
    relevance_why: str = "",
) -> None:
    conn.execute(
        """
        UPDATE papers SET
            focus = ?, contribution = ?, findings = ?, abstract = ?,
            jel_codes = ?, keywords = ?, pdf_pages = ?, pdf_size_kb = ?,
            authors = CASE WHEN ? != '[]' THEN ? ELSE authors END,
            relevance = ?, relevance_why = ?, pdf_url = CASE WHEN ? != '' THEN ? ELSE pdf_url END, detail_fetched = 1
        WHERE path = ?
        """,
        (
            detail.focus,
            detail.contribution,
            detail.findings,
            detail.abstract,
            json.dumps(detail.jel_codes, ensure_ascii=False),
            json.dumps(detail.keywords, ensure_ascii=False),
            detail.pdf_pages,
            detail.pdf_size_kb,
            json.dumps(detail.authors, ensure_ascii=False),
            json.dumps(detail.authors, ensure_ascii=False),
            relevance,
            relevance_why,
            detail.pdf_url,
            detail.pdf_url,
            path,
        ),
    )


def mark_reported(conn: sqlite3.Connection, paths: Sequence[str]) -> None:
    if not paths:
        return
    stamp = utcnow()
    conn.executemany(
        "UPDATE papers SET reported_at = ? WHERE path = ? AND reported_at IS NULL",
        [(stamp, p) for p in paths],
    )


# ---------------------------------------------------------------- 读取
def unreported(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM papers
            WHERE reported_at IS NULL AND is_baseline = 0
            ORDER BY relevance DESC, pub_date DESC, CAST(series AS INTEGER) DESC
            """
        )
    )


def needs_detail(conn: sqlite3.Connection, limit: int) -> list[str]:
    if limit <= 0:
        return []
    return [
        row["path"]
        for row in conn.execute(
            """
            SELECT path FROM papers
            WHERE detail_fetched = 0
            ORDER BY pub_date DESC, CAST(series AS INTEGER) DESC
            LIMIT ?
            """,
            (limit,),
        )
    ]


_SEARCH_COLUMNS = ("title", "authors", "abstract", "focus", "findings", "keywords")

# 中文总结所在的列。搜索必须一并覆盖，否则最贵的内容反而搜不到：
# No 1366 整篇都在讲拨备，搜「拨备」却返回 0 条——因为上面几列全是英文抓取字段。
# 用相关子查询而非 JOIN，是为了不影响 papers 的行数与既有排序。
_ZH_SEARCH_COLUMNS = (
    "title_zh", "research_question", "key_takeaway",
    "key_evidence", "limitations", "implications", "core_findings",
)


def _normalise(term: str) -> str:
    """规范化搜索词。

    从 PDF 或网页复制来的词条常带不可见空白（不换行空格 U+00A0、
    零宽空格 U+200B、全角空格 U+3000）。原样拼进 LIKE 的话会静默返回
    一个**看起来正常但错误**的结果数，用户完全无从察觉。
    """
    cleaned = term.replace("\u00a0", " ").replace("\u3000", " ")
    cleaned = re.sub(r"[\u200b-\u200f\ufeff]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _like(term: str) -> str:
    """转义 LIKE 的通配符，让 % 和 _ 按字面量匹配而不是当通配符。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filter(
    query: str, jel: str, *, scored: bool = False, zh_only: bool = False
) -> tuple[str, list[Any]]:
    # 先规范化：从 PDF 复制来的词常带不可见空白，不处理的话会静默少匹配
    query, jel = _normalise(query), _normalise(jel)
    sql = ""
    args: list[Any] = []

    if query:
        conditions = [f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_COLUMNS]
        args += [_like(query)] * len(_SEARCH_COLUMNS)

        # summaries 表可能还不存在（老库首次启动），用 try 兜住
        zh_conditions = " OR ".join(
            f"s.{col} LIKE ? ESCAPE '\\'" for col in _ZH_SEARCH_COLUMNS
        )
        conditions.append(
            "EXISTS (SELECT 1 FROM summaries s"
            f" WHERE s.series = papers.series AND ({zh_conditions}))"
        )
        args += [_like(query)] * len(_ZH_SEARCH_COLUMNS)

        sql += f" AND ({' OR '.join(conditions)})"

    if jel:
        sql += " AND jel_codes LIKE ? ESCAPE '\\'"
        args.append(_like(jel))
    if scored:
        sql += " AND detail_fetched = 1"
    if zh_only:
        sql += " AND EXISTS (SELECT 1 FROM summaries s WHERE s.series = papers.series)"
    return sql, args


_ORDERINGS = {
    "date": "pub_date DESC, CAST(series AS INTEGER) DESC",
    "relevance": "relevance DESC, pub_date DESC, CAST(series AS INTEGER) DESC",
}


def list_papers(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    jel: str = "",
    scored: bool = False,
    zh_only: bool = False,
    sort: str = "date",
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where, args = _filter(query, jel, scored=scored, zh_only=zh_only)
    order = _ORDERINGS.get(sort, _ORDERINGS["date"])
    sql = (
        "SELECT * FROM papers WHERE 1=1" + where
        + f" ORDER BY {order} LIMIT ? OFFSET ?"
    )
    return list(conn.execute(sql, [*args, limit, offset]))


def count_papers(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    jel: str = "",
    scored: bool = False,
    zh_only: bool = False,
) -> int:
    where, args = _filter(query, jel, scored=scored, zh_only=zh_only)
    sql = "SELECT COUNT(*) AS n FROM papers WHERE 1=1" + where
    return int(conn.execute(sql, args).fetchone()["n"])


def facet_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """筛选条上的三个数字。1369 篇里只有极少数有摘要，
    不给出数量的话用户不知道点进去会不会是空表。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(detail_fetched) AS scored,"
        " (SELECT COUNT(*) FROM summaries) AS zh"
        " FROM papers"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "scored": row["scored"] or 0,
        "zh": row["zh"] or 0,
    }


def get_paper(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE path = ?", (path,)).fetchone()


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(detail_fetched) AS with_detail,
               SUM(CASE WHEN is_baseline = 0 THEN 1 ELSE 0 END) AS discovered,
               MAX(pub_date) AS latest_date
        FROM papers
        """
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "with_detail": row["with_detail"] or 0,
        "discovered": row["discovered"] or 0,
        "latest_date": row["latest_date"] or "-",
    }


# ---------------------------------------------------------------- 运行记录
def start_run(conn: sqlite3.Connection, trigger: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (utcnow(), trigger)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    listed_count: int = 0,
    new_count: int = 0,
    is_baseline: bool = False,
    email_status: str = "",
    error: str = "",
    new_paths: Sequence[str] = (),
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, status = ?, listed_count = ?, new_count = ?,
                        is_baseline = ?, email_status = ?, error = ?, new_paths = ?
        WHERE id = ?
        """,
        (
            utcnow(),
            status,
            listed_count,
            new_count,
            1 if is_baseline else 0,
            email_status,
            error[:2000],
            json.dumps(list(new_paths), ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    )


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次已结束的运行。

    注意 'interrupted' 也算已结束 —— 否则一次被杀掉的运行会永远隐身，
    让启动补跑一直读到更早的那次记录，误判"距上次检查已超过 24 小时"。
    """
    return conn.execute(
        "SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ---------------------------------------------------------------- 投递账本
def open_delivery(
    conn: sqlite3.Connection,
    run_id: int,
    paths: Sequence[str],
    *,
    channel: str = "smtp",
) -> int:
    """发信前登记，并立刻提交 —— 提交动作本身就是崩溃恢复的依据。"""
    cur = conn.execute(
        "INSERT INTO deliveries (run_id, created_at, status, paths, channel)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            run_id,
            utcnow(),
            "sending",
            json.dumps(list(paths), ensure_ascii=False),
            channel,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_delivery(conn: sqlite3.Connection, delivery_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()


def settle_delivery(
    conn: sqlite3.Connection, delivery_id: int, status: str, detail: str = ""
) -> None:
    conn.execute(
        "UPDATE deliveries SET status = ?, detail = ?, settled_at = ? WHERE id = ?",
        (status, detail[:1000], utcnow(), delivery_id),
    )
    conn.commit()


def stuck_deliveries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM deliveries WHERE status = 'sending'"))


def repair_html_entities(conn: sqlite3.Connection) -> int:
    """把库里残留的 HTML 实体解码掉。

    抓取层早期没做 html.unescape，姓名和标题以 `Adam Ger&#353;l` 的形式落了库。
    页面上直接显示成实体码，导出的 BibTeX 更糟：& 和 # 是 LaTeX 保留字符，
    条目编译不过。抓取层已修，但已有的 1369 行要靠这里补救。

    幂等：解码后不含 `&#` 就不再改动，所以每次启动跑一遍没有副作用。
    """
    rows = conn.execute(
        "SELECT path, title, authors FROM papers"
        " WHERE title LIKE '%&#%' OR title LIKE '%&amp;%'"
        "    OR authors LIKE '%&#%' OR authors LIKE '%&amp;%'"
    ).fetchall()
    if not rows:
        return 0

    fixed = 0
    for row in rows:
        title = html.unescape(row["title"] or "")
        try:
            names = [html.unescape(n) for n in json.loads(row["authors"] or "[]")]
        except (json.JSONDecodeError, TypeError):
            names = []
        conn.execute(
            "UPDATE papers SET title = ?, authors = ? WHERE path = ?",
            (title, json.dumps(names, ensure_ascii=False), row["path"]),
        )
        fixed += 1
    conn.commit()
    log.info("已解码 %d 条记录里的 HTML 实体", fixed)
    return fixed


def reconcile_interrupted(conn: sqlite3.Connection) -> dict[str, int]:
    """启动时清理上次进程被杀留下的残局。

    1. 卡在 'running' 的运行 -> 'interrupted'，让 last_run 能看见它。
    2. 卡在 'sending' 的投递 -> 'assumed_sent'，并把其中的论文标记为已报告。
       宁可漏发一封，也不要在重启后把同一封简报重复轰炸给收件人；
       论文本身仍在仪表盘和 Markdown 归档里，内容不会丢。
    """
    runs_fixed = conn.execute(
        "UPDATE runs SET status = 'interrupted', finished_at = ?,"
        " error = CASE WHEN error = '' THEN '进程在运行期间被终止' ELSE error END"
        " WHERE status = 'running'",
        (utcnow(),),
    ).rowcount

    papers_settled = 0
    external_failed = 0
    for row in stuck_deliveries(conn):
        try:
            paths = json.loads(row["paths"] or "[]")
        except (json.JSONDecodeError, TypeError):
            paths = []

        if (row["channel"] or "smtp") == "external":
            # 外部代发（GitHub Actions）没有回调确认，说明它没发成功、
            # 或者发了但确认失败。这里判为失败让下周重试：
            # 偶尔重复一封，远好过静默漏掉一整周。
            # 这与进程内发信相反——那边邮件很可能已经出去了。
            external_failed += 1
            conn.execute(
                "UPDATE deliveries SET status = 'failed', settled_at = ?,"
                " detail = '外部代发未回调确认，判为未送达，下次运行会重试'"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            continue

        mark_reported(conn, paths)
        papers_settled += len(paths)
        conn.execute(
            "UPDATE deliveries SET status = 'assumed_sent', settled_at = ?,"
            " detail = '进程在发信窗口内被终止，按已送达处理以避免重复发送'"
            " WHERE id = ?",
            (utcnow(), row["id"]),
        )
    conn.commit()
    return {
        "runs": max(runs_fixed, 0),
        "papers": papers_settled,
        "external_failed": external_failed,
    }


# ---------------------------------------------------------------- 行 -> 字典
def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("authors", "jel_codes", "keywords"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except (json.JSONDecodeError, TypeError):
            data[key] = []
    return data
