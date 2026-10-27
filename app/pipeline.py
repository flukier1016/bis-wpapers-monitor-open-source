"""每周检查的主流程。

一次运行做的事：
  1. 拉取 BIS 全量列表（1 个请求），并做完整性护栏检查
  2. 与库里已知的 path 求差集 —— 差集就是新论文（需求 2：绝不重复报告）
  3. 首次运行走"基线播种"：全部入库并标记为已报告，不发简报
     （否则第一次就会把 1300+ 篇历史论文当成新论文推送）
  4. 非首次运行：为**所有尚未报告**的论文补详情、打分、生成简报、发邮件、归档
  5. 落 runs / deliveries 表留痕（需求 9）

投递协议（这部分是本文件最要紧的地方）
------------------------------------------------------------------
"已报告"这个戳一旦打上，论文就永远不会再出现在简报里。所以只有在**确认送达**
之后才能打戳：

    写归档 -> 登记 deliveries(sending) 并提交 -> 发信 -> 成功才 mark_reported

* 发信失败（SMTP 挂了、应用密码过期）-> 不打戳，下周自动重试。
* 演练模式（send_email=False）-> 不打戳。
* 完全没配 SMTP -> 打戳，因为此时 Markdown 归档 + 仪表盘就是投递渠道，
  否则未配邮箱的部署会无限累积"新论文"。
* 发信成功但进程在打戳前被杀 -> 启动时 reconcile_interrupted() 看到残留的
  sending 记录，按"可能已送达"处理，避免重启后重复轰炸收件人。
"""
from __future__ import annotations

import logging
import sqlite3
import traceback
from dataclasses import dataclass, field
from typing import Any

from . import config, db, exporter, mailer, scoring
from .bis import BisClient, PaperDetail, PaperStub

log = logging.getLogger(__name__)


class IncompleteListing(RuntimeError):
    """BIS 返回的列表明显残缺，中止本次运行而不是照单全收。"""


@dataclass
class RunResult:
    status: str = "ok"                  # ok | error | busy
    is_baseline: bool = False
    listed_count: int = 0
    new_count: int = 0
    new_paths: list[str] = field(default_factory=list)
    email_status: str = ""
    delivered: bool = False
    export_path: str = ""
    error: str = ""

    @property
    def message(self) -> str:
        if self.status == "busy":
            return "A check is already running."
        if self.status == "error":
            return f"Check failed: {self.error}"
        if self.is_baseline:
            return f"Baseline complete: {self.listed_count} existing papers indexed (no digest sent)."
        if self.new_count == 0:
            return "No new papers found."
        suffix = "" if self.delivered else " (delivery unconfirmed; retry on the next run)"
        return f"Found {self.new_count} new papers.{suffix}"


# ---------------------------------------------------------------- 详情抓取
def fetch_and_store_detail(
    conn: sqlite3.Connection, client: BisClient, path: str, title: str
) -> PaperDetail:
    """抓一篇论文的详情、打分并落库。

    只有真正拿到摘要正文才置 detail_fetched=1。仅有作者/JEL 这类元数据是不够的
    —— 那会让一篇解析失败的论文永久退出重试队列，摘要栏从此空白。
    """
    row = db.get_paper(conn, path)
    detail = client.fetch_detail(path, row["abstract_url"] if row else "")
    if not detail.has_summary:
        log.warning("未取到摘要正文，保持 detail_fetched=0 以便下次重试：%s", path)
        return detail
    score = scoring.score_paper(title, detail)
    db.update_detail(conn, path, detail, relevance=score.total, relevance_why=score.why)
    conn.commit()
    return detail


def backfill_details(conn: sqlite3.Connection, limit: int) -> int:
    """为仪表盘补齐历史论文的摘要（后台慢慢跑，不阻塞检查流程）。"""
    paths = db.needs_detail(conn, limit)
    if not paths:
        return 0
    done = 0
    with BisClient() as client:
        for path in paths:
            row = db.get_paper(conn, path)
            if row is None:
                continue
            try:
                if fetch_and_store_detail(conn, client, path, row["title"]).has_summary:
                    done += 1
            except Exception:  # 单篇失败不应中断整批
                log.exception("回填失败：%s", path)
    log.info("回填完成 %d/%d 篇", done, len(paths))
    return done


# ---------------------------------------------------------------- 护栏
def _guard_listing(conn: sqlite3.Connection, count: int, baseline: bool) -> None:
    """BIS 偶发的残缺响应必须挡住。

    放行一次残缺列表的后果是：大量已知论文这次"不在列表里"，下次又重新出现，
    届时会被当成新论文重发一遍 —— 收件箱直接被淹。
    """
    if baseline:
        if count < config.MIN_BASELINE_PAPERS:
            raise IncompleteListing(
                f"基线播种时列表只有 {count} 篇（阈值 {config.MIN_BASELINE_PAPERS}），"
                "判定为 BIS 返回残缺响应，已中止以免播种出错误的基线"
            )
        return

    previous = conn.execute(
        "SELECT MAX(listed_count) AS n FROM runs WHERE status = 'ok' AND listed_count > 0"
    ).fetchone()["n"]
    if previous and count < previous * config.MIN_LIST_RATIO:
        raise IncompleteListing(
            f"列表只返回 {count} 篇，明显少于历史最高的 {previous} 篇"
            f"（阈值 {config.MIN_LIST_RATIO:.0%}），判定为残缺响应，已中止"
        )


# ---------------------------------------------------------------- 主流程
def run_check(
    conn: sqlite3.Connection,
    *,
    trigger: str = "schedule",
    send_email: bool = True,
) -> RunResult:
    run_id = db.start_run(conn, trigger)
    result = RunResult()
    try:
        baseline = db.is_empty(conn)
        result.is_baseline = baseline

        if baseline and config.BASELINE_MARKER.exists():
            # 数据目录还在、但表空了：卷被换掉或数据库被删。只能重新播种，
            # 但必须留下醒目的记录 —— 这中间发布的论文会被静默吞掉。
            log.critical(
                "检测到基线标记文件存在但论文表为空：历史状态已丢失，"
                "本次将重新播种基线，其间发布的论文不会被报告。"
            )

        with BisClient() as client:
            stubs: list[PaperStub] = client.fetch_list()
            result.listed_count = len(stubs)
            _guard_listing(conn, len(stubs), baseline)

            known = db.known_paths(conn)
            fresh = [s for s in stubs if s.path not in known]

            for stub in stubs:
                # 对已知论文也走一遍：顺带刷新 BIS 事后更正过的标题/作者
                db.insert_stub(conn, stub, baseline=baseline)
            conn.commit()

            if baseline:
                log.info("首次运行：已播种 %d 篇历史论文", len(fresh))
                config.BASELINE_MARKER.parent.mkdir(parents=True, exist_ok=True)
                config.BASELINE_MARKER.write_text(db.utcnow(), encoding="utf-8")
                db.finish_run(
                    conn, run_id, status="ok", listed_count=len(stubs),
                    new_count=0, is_baseline=True,
                    email_status="skipped (baseline; no email)",
                )
                result.email_status = "skipped (baseline; no email)"
                return result

            # 为**所有**未报告的论文补详情，而不只是本次新插入的那批。
            # 上周抓详情失败、或上周发信失败留下来的论文，这次必须一并补齐，
            # 否则它们会带着空摘要被发出去。
            pending = [r["path"] for r in db.unreported(conn)]
            titles = {p: (db.get_paper(conn, p) or {})["title"] for p in pending}
            for path in pending[: config.MAX_DETAIL_FETCH]:
                if (db.get_paper(conn, path) or {})["detail_fetched"]:
                    continue
                try:
                    fetch_and_store_detail(conn, client, path, titles.get(path, ""))
                except Exception:
                    log.exception("抓取详情失败：%s", path)

        # 重新从库里读，拿到打分后的完整记录并按优先级排序
        rows = db.unreported(conn)
        result.new_count = len(rows)
        result.new_paths = [r["path"] for r in rows]

        if not rows:
            if send_email and config.EMAIL_ENABLED and config.MAIL_ON_EMPTY:
                _, result.email_status = mailer.send_empty_notice()
            else:
                result.email_status = "skipped (no new papers)"
            db.finish_run(
                conn, run_id, status="ok", listed_count=result.listed_count,
                new_count=0, email_status=result.email_status,
            )
            return result

        papers = [db.row_to_dict(r) for r in rows]

        # 1) 先落归档。归档失败就不打戳 —— 内容没留下来就不算送达。
        try:
            result.export_path = str(exporter.write_digest(papers))
        except Exception as exc:
            log.exception("Markdown 归档失败")
            result.export_path = f"failed: {exc}"
            result.email_status = "aborted (archive failed; retry next run)"
            db.finish_run(
                conn, run_id, status="error", listed_count=result.listed_count,
                new_count=result.new_count, email_status=result.email_status,
                error=f"Archive failed: {exc}", new_paths=result.new_paths,
            )
            result.status = "error"
            result.error = f"Archive failed: {exc}"
            return result

        # 2) 发信。登记 -> 提交 -> 发送 -> 结算，崩溃也能被启动时的对账捞回来。
        if not send_email:
            result.delivered = False
            result.email_status = "skipped (no email; papers remain unreported)"
        elif not config.EMAIL_ENABLED:
            # 未配邮箱：归档 + 仪表盘即为投递渠道，允许打戳
            result.delivered = True
            result.email_status = "skipped (SMTP not configured; archive is the delivery channel)"
        else:
            delivery_id = db.open_delivery(conn, run_id, result.new_paths)
            ok, status = mailer.send_digest(papers)
            db.settle_delivery(conn, delivery_id, "sent" if ok else "failed", status)
            result.delivered = ok
            result.email_status = status

        # 3) 只有确认送达才打戳
        if result.delivered:
            db.mark_reported(conn, result.new_paths)
            conn.commit()
        else:
            log.warning(
                "未确认送达（%s），%d 篇论文保持未报告状态，下次运行会重试",
                result.email_status, len(result.new_paths),
            )

        db.finish_run(
            conn, run_id, status="ok", listed_count=result.listed_count,
            new_count=result.new_count, is_baseline=False,
            email_status=result.email_status, new_paths=result.new_paths,
        )
        return result

    except Exception as exc:
        log.exception("run_check 失败")
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        db.finish_run(
            conn, run_id, status="error", listed_count=result.listed_count,
            error=traceback.format_exc(),
        )
        return result
