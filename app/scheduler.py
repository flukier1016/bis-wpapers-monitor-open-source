"""APScheduler 定时器：默认每周一 09:00（Asia/Shanghai）跑一次检查。

用应用内调度而非 Railway Cron，是因为仪表盘本来就要常驻运行，
放在同一个进程里可以少开一个服务、共用同一块 Volume。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, db, pipeline

log = logging.getLogger(__name__)

# 同一时刻只允许一次检查在跑（定时任务与手动触发共用）
_run_lock = threading.Lock()
# 只读的"是否在跑"信号。刻意与 _run_lock 分开：如果用试探性 acquire 来查状态，
# 每次有人刷新仪表盘都可能恰好抢走锁，把当周的定时检查顶成 busy 而整周跳过。
_running = threading.Event()
_scheduler: BackgroundScheduler | None = None


def run_locked(trigger: str, *, send_email: bool = True) -> pipeline.RunResult:
    """带互斥的检查入口。已有任务在跑时立即返回，不排队。"""
    if not _run_lock.acquire(blocking=False):
        log.warning("已有检查在运行，跳过本次 %s 触发", trigger)
        return pipeline.RunResult(status="busy", error="已有一次检查正在运行")
    _running.set()
    try:
        with db.session() as conn:
            result = pipeline.run_check(conn, trigger=trigger, send_email=send_email)
            # 基线播种后顺带回填最近若干篇详情，让仪表盘一上线就有内容
            if result.is_baseline and config.BACKFILL_RECENT > 0:
                pipeline.backfill_details(conn, config.BACKFILL_RECENT)
            return result
    finally:
        _running.clear()
        _run_lock.release()


def is_running() -> bool:
    """纯只读查询，不碰 _run_lock。"""
    return _running.is_set()


def _job() -> None:
    result = run_locked("schedule")
    log.info("定时检查结束：%s", result.message)


def start(on_startup: Callable[[], Any] | None = None) -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

    if config.EXTERNAL_MAILER:
        # 由 GitHub Actions 驱动，进程内不再自己定时跑，避免两个调度器抢同一批论文
        scheduler.start()
        _scheduler = scheduler
        log.info("EXTERNAL_MAILER=true：进程内每周定时器已停用，改由外部代发驱动")
        if on_startup:
            threading.Thread(target=on_startup, daemon=True, name="startup-run").start()
        return scheduler

    scheduler.add_job(
        _job,
        CronTrigger(
            day_of_week=config.CRON_DAY_OF_WEEK,
            hour=config.CRON_HOUR,
            minute=config.CRON_MINUTE,
            timezone=config.TIMEZONE,
        ),
        id="weekly-bis-check",
        name="BIS Working Papers 每周检查",
        replace_existing=True,
        misfire_grace_time=3600,   # 容器重启错过时间点时，1 小时内仍补跑
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "调度已启动：%s %02d:%02d (%s)",
        config.CRON_DAY_OF_WEEK, config.CRON_HOUR, config.CRON_MINUTE, config.TIMEZONE,
    )

    if on_startup:
        threading.Thread(target=on_startup, daemon=True, name="startup-run").start()
    return scheduler


def shutdown(timeout: float = 25.0) -> None:
    """停机时**等待**在途的检查结束。

    不等的话，进程会在"邮件已发出、reported_at 还没提交"的窗口里被砍掉，
    重启后就会把同一封简报再发一遍（启动对账能兜住，但代价是那期简报只能
    按"可能已送达"处理）。等一会儿远比事后对账干净。
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None

    if _running.is_set():
        log.info("等待在途的检查完成（最多 %.0f 秒）…", timeout)
        deadline = time.monotonic() + timeout
        while _running.is_set() and time.monotonic() < deadline:
            time.sleep(0.2)
        if _running.is_set():
            log.warning("在途检查未能在超时内结束，启动时将由对账逻辑处理")


def next_run_time() -> str:
    if config.EXTERNAL_MAILER:
        return "Driven by GitHub Actions (see workflow schedule)"
    if not _scheduler:
        return "Not started"
    job = _scheduler.get_job("weekly-bis-check")
    if not job or not job.next_run_time:
        return "Not scheduled"
    return job.next_run_time.strftime("%Y-%m-%d %H:%M %Z")
