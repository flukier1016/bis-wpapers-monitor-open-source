"""去重 / 基线播种 / 简报流程测试（不触网，BisClient 全部打桩）。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest

from app import config, db, pipeline
from app.bis import PaperDetail, PaperStub


class FakeClient:
    """按预设脚本返回列表与详情，并记录被抓过哪些 path。"""

    def __init__(self, stubs: list[PaperStub], details: dict[str, PaperDetail] | None = None):
        self.stubs = stubs
        self.details = details or {}
        self.fetched: list[str] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def fetch_list(self) -> list[PaperStub]:
        return list(self.stubs)

    def fetch_detail(self, path: str, source_url: str = "") -> PaperDetail:
        self.fetched.append(path)
        return self.details.get(
            path,
            PaperDetail(
                focus="Focus text long enough to matter.",
                contribution="Contribution text.",
                findings="Findings text.",
                abstract="Abstract text about monetary policy and the yield curve.",
                jel_codes=["E52 - Monetary Policy"],
                keywords=["monetary policy"],
                pdf_pages=30,
                authors=["A. Author"],
            ),
        )

    def fetch_details(self, paths: Iterable[str]) -> dict[str, PaperDetail]:
        return {p: self.fetch_detail(p) for p in paths}


def make_stub(series: str, date: str = "2026-07-16") -> PaperStub:
    return PaperStub(
        path=f"/publ/work{series}",
        series=series,
        title=f"Paper {series} on exchange rate dynamics",
        authors=["A. Author"],
        pub_date=date,
    )


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    db.init(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def isolate_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """归档与基线标记写到 tmp，邮件默认关闭，护栏阈值下调到适合小样本。"""
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "BASELINE_MARKER", tmp_path / ".baselined")
    monkeypatch.setattr(config, "EMAIL_ENABLED", False)
    monkeypatch.setattr(config, "FETCH_DELAY_MS", 0)
    monkeypatch.setattr(config, "MIN_BASELINE_PAPERS", 1)
    monkeypatch.setattr(config, "MIN_LIST_RATIO", 0.9)


def use_client(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(pipeline, "BisClient", lambda *a, **k: client)


# ---------------------------------------------------------------- 基线播种
def test_first_run_seeds_baseline_without_reporting(conn, monkeypatch) -> None:
    client = FakeClient([make_stub("1369"), make_stub("1368"), make_stub("1367")])
    use_client(monkeypatch, client)

    result = pipeline.run_check(conn, trigger="test")

    assert result.is_baseline is True
    assert result.new_count == 0, "首次运行绝不能把历史论文当成新论文"
    assert result.listed_count == 3
    assert result.message.startswith("Baseline complete")
    # 基线论文全部入库且已标记为已报告
    assert len(db.known_paths(conn)) == 3
    assert db.unreported(conn) == []
    # 基线阶段不该逐篇抓详情（1300+ 篇会打爆 BIS）
    assert client.fetched == []


def test_second_run_with_no_changes_reports_nothing(conn, monkeypatch) -> None:
    stubs = [make_stub("1369"), make_stub("1368")]
    use_client(monkeypatch, FakeClient(stubs))
    pipeline.run_check(conn, trigger="baseline")

    use_client(monkeypatch, FakeClient(stubs))
    result = pipeline.run_check(conn, trigger="test")

    assert result.is_baseline is False
    assert result.new_count == 0
    assert result.message == "No new papers found."


# ---------------------------------------------------------------- 增量发现
def test_new_paper_is_detected_and_detailed(conn, monkeypatch) -> None:
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")

    client = FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")])
    use_client(monkeypatch, client)
    result = pipeline.run_check(conn, trigger="test")

    assert result.new_count == 1
    assert result.new_paths == ["/publ/work1370"]
    assert client.fetched == ["/publ/work1370"], "只应为新论文抓详情"

    row = db.get_paper(conn, "/publ/work1370")
    assert row["detail_fetched"] == 1
    assert row["findings"] == "Findings text."
    assert row["relevance"] > 0
    assert row["relevance_why"]


def test_same_paper_never_reported_twice(conn, monkeypatch) -> None:
    """需求 2 的核心保证：跑三次，同一篇只进一次简报。"""
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")

    new_list = [make_stub("1370", "2026-07-20"), make_stub("1369")]
    use_client(monkeypatch, FakeClient(new_list))
    first = pipeline.run_check(conn, trigger="test")

    use_client(monkeypatch, FakeClient(new_list))
    second = pipeline.run_check(conn, trigger="test")

    use_client(monkeypatch, FakeClient(new_list))
    third = pipeline.run_check(conn, trigger="test")

    assert first.new_count == 1
    assert second.new_count == 0
    assert third.new_count == 0
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is not None


def test_multiple_new_papers_sorted_by_relevance(conn, monkeypatch) -> None:
    use_client(monkeypatch, FakeClient([make_stub("1360")]))
    pipeline.run_check(conn, trigger="baseline")

    high = PaperDetail(
        focus="f", contribution="c", findings="fi",
        abstract="This paper studies the yield curve, term premium and foreign exchange.",
        jel_codes=["E43 - Interest Rates", "F31 - Foreign Exchange"],
        keywords=["yield curve", "foreign exchange"], pdf_pages=30,
    )
    low = PaperDetail(
        focus="f", contribution="c", findings="fi",
        abstract="This paper studies medieval agricultural land tenure.",
        jel_codes=["N53 - Economic History"], keywords=["land tenure"], pdf_pages=95,
    )
    client = FakeClient(
        [make_stub("1371", "2026-07-20"), make_stub("1372", "2026-07-20"), make_stub("1360")],
        details={"/publ/work1371": low, "/publ/work1372": high},
    )
    use_client(monkeypatch, client)
    result = pipeline.run_check(conn, trigger="test")

    assert result.new_count == 2
    # 高相关度的排前面
    assert result.new_paths[0] == "/publ/work1372"
    rows = db.unreported(conn)
    assert rows == []  # 已全部标记为已报告


# ---------------------------------------------------------------- 健壮性
def test_detail_failure_still_reports_paper(conn, monkeypatch) -> None:
    """抓详情失败不能导致论文被吞掉——标题和链接照样要报出去。"""
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")

    client = FakeClient(
        [make_stub("1370", "2026-07-20"), make_stub("1369")],
        details={"/publ/work1370": PaperDetail()},   # 全空 = 抓取失败
    )
    use_client(monkeypatch, client)
    result = pipeline.run_check(conn, trigger="test")

    assert result.new_count == 1
    row = db.get_paper(conn, "/publ/work1370")
    assert row["detail_fetched"] == 0, "空详情应保持未抓取状态，下次重试"
    assert row["reported_at"] is not None, "但论文本身仍应被报告"


def test_network_error_marks_run_failed(conn, monkeypatch) -> None:
    class Boom(FakeClient):
        def fetch_list(self):
            raise RuntimeError("BIS unreachable")

    use_client(monkeypatch, Boom([]))
    result = pipeline.run_check(conn, trigger="test")

    assert result.status == "error"
    assert "BIS unreachable" in result.error
    run = db.recent_runs(conn, 1)[0]
    assert run["status"] == "error"


def test_run_history_recorded(conn, monkeypatch) -> None:
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    pipeline.run_check(conn, trigger="schedule")

    runs = db.recent_runs(conn, 10)
    assert len(runs) == 2
    assert runs[0]["trigger"] == "schedule"
    assert runs[0]["new_count"] == 1
    assert runs[1]["is_baseline"] == 1


# ---------------------------------------------------------------- 投递正确性回归
# 以下每个测试都对应一个曾经真实存在的缺陷。核心不变式：
# "已报告"这个戳只有在确认送达之后才能打，否则论文会永久消失。
def _seed_then_new(conn, monkeypatch, **run_kwargs):
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    return pipeline.run_check(conn, trigger="test", **run_kwargs)


def test_smtp_failure_does_not_mark_reported(conn, monkeypatch) -> None:
    """发信失败必须保持未报告，下周重试 —— 否则一次密码过期就永久吞掉一篇论文。"""
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(
        pipeline.mailer, "send_digest",
        lambda papers: (False, "failed: SMTPAuthenticationError: 535"),
    )
    result = _seed_then_new(conn, monkeypatch)

    assert result.new_count == 1
    assert result.delivered is False
    assert "failed" in result.email_status
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is None
    # 下次运行必须重新报告它
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    monkeypatch.setattr(pipeline.mailer, "send_digest", lambda papers: (True, "sent"))
    retry = pipeline.run_check(conn, trigger="retry")
    assert retry.new_count == 1 and retry.delivered is True
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is not None


def test_dry_run_does_not_consume_papers(conn, monkeypatch) -> None:
    """演练模式不能把论文标记掉，否则演练一次就永远收不到那封简报。"""
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(pipeline.mailer, "send_digest", lambda p: (True, "sent"))
    result = _seed_then_new(conn, monkeypatch, send_email=False)

    assert result.new_count == 1
    assert result.delivered is False
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is None


def test_successful_send_marks_reported(conn, monkeypatch) -> None:
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(pipeline.mailer, "send_digest", lambda p: (True, "sent -> x"))
    result = _seed_then_new(conn, monkeypatch)

    assert result.delivered is True
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is not None
    rows = list(conn.execute("SELECT status FROM deliveries"))
    assert [r["status"] for r in rows] == ["sent"]


def test_crash_in_send_window_is_reconciled_not_resent(conn, monkeypatch) -> None:
    """进程在"已发信、未打戳"的窗口里被杀：重启对账应按已送达处理，不重复发送。"""
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)

    def send_then_die(papers):
        raise KeyboardInterrupt("进程被杀")

    monkeypatch.setattr(pipeline.mailer, "send_digest", send_then_die)
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_check(conn, trigger="test")

    # 崩溃后的残局：投递卡在 sending，论文仍未报告
    assert [r["status"] for r in conn.execute("SELECT status FROM deliveries")] == ["sending"]
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is None

    fixed = db.reconcile_interrupted(conn)
    assert fixed["papers"] == 1
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is not None
    assert [r["status"] for r in conn.execute("SELECT status FROM deliveries")] == ["assumed_sent"]

    # 对账之后再跑一次，不能再发同一篇
    sent: list[list[str]] = []
    monkeypatch.setattr(
        pipeline.mailer, "send_digest",
        lambda papers: (sent.append([p["path"] for p in papers]), (True, "sent"))[1],
    )
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    again = pipeline.run_check(conn, trigger="after-restart")
    assert again.new_count == 0
    assert sent == []


def test_stuck_external_delivery_is_treated_as_failed(conn) -> None:
    """外部代发（GitHub Actions）未回调确认时，必须判为未送达并重试。

    这与进程内发信的处理刚好相反：
      * 进程内发信卡在 sending -> 邮件很可能已经出去了，按已送达处理，避免重复轰炸
      * 外部代发卡在 sending   -> 代发方没回调，说明多半没发出去，判为失败下次重试
    偶尔重复一封，远好过静默漏掉一整周。
    """
    db.insert_stub(conn, make_stub("1370", "2026-07-20"), baseline=False)
    conn.commit()
    delivery_id = db.open_delivery(
        conn, 1, ["/publ/work1370"], channel="external"
    )

    fixed = db.reconcile_interrupted(conn)

    assert fixed["external_failed"] == 1
    assert fixed["papers"] == 0, "外部代发不应把论文标记为已报告"
    row = db.get_delivery(conn, delivery_id)
    assert row["status"] == "failed"
    assert db.get_paper(conn, "/publ/work1370")["reported_at"] is None
    assert [r["path"] for r in db.unreported(conn)] == ["/publ/work1370"]


def test_stuck_smtp_delivery_still_assumed_sent(conn) -> None:
    """进程内发信的语义保持不变，不能被外部代发的改动带偏。"""
    db.insert_stub(conn, make_stub("1371", "2026-07-20"), baseline=False)
    conn.commit()
    delivery_id = db.open_delivery(conn, 1, ["/publ/work1371"])   # 默认 smtp

    fixed = db.reconcile_interrupted(conn)

    assert fixed["papers"] == 1
    assert db.get_delivery(conn, delivery_id)["status"] == "assumed_sent"
    assert db.get_paper(conn, "/publ/work1371")["reported_at"] is not None


def test_confirmed_external_delivery_marks_reported(conn) -> None:
    db.insert_stub(conn, make_stub("1372", "2026-07-20"), baseline=False)
    conn.commit()
    delivery_id = db.open_delivery(
        conn, 1, ["/publ/work1372"], channel="external"
    )

    db.mark_reported(conn, ["/publ/work1372"])
    db.settle_delivery(conn, delivery_id, "sent", "由外部代发确认送达")

    assert db.get_delivery(conn, delivery_id)["status"] == "sent"
    assert db.get_paper(conn, "/publ/work1372")["reported_at"] is not None
    assert db.unreported(conn) == []


def test_interrupted_run_is_visible_to_last_run(conn, monkeypatch) -> None:
    """卡在 running 的运行会让 last_run 读到过期记录，进而误触发启动补跑。"""
    db.start_run(conn, "killed")
    conn.commit()
    assert db.last_run(conn) is None          # 修复前：running 被过滤掉，永远看不见

    db.reconcile_interrupted(conn)
    last = db.last_run(conn)
    assert last is not None and last["status"] == "interrupted"


# ---------------------------------------------------------------- 完整性护栏
def test_short_list_aborts_incremental_run(conn, monkeypatch) -> None:
    """BIS 返回残缺列表时必须中止，否则下次这些论文会被当成新论文重发一遍。"""
    monkeypatch.setattr(config, "MIN_BASELINE_PAPERS", 1)
    use_client(monkeypatch, FakeClient([make_stub(str(1300 + i)) for i in range(20)]))
    pipeline.run_check(conn, trigger="baseline")

    use_client(monkeypatch, FakeClient([make_stub("1300")]))   # 只回来 1 篇
    result = pipeline.run_check(conn, trigger="test")

    assert result.status == "error"
    assert "残缺响应" in result.error


def test_short_list_aborts_baseline(conn, monkeypatch) -> None:
    monkeypatch.setattr(config, "MIN_BASELINE_PAPERS", 500)
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    result = pipeline.run_check(conn, trigger="baseline")

    assert result.status == "error"
    assert "中止" in result.error
    assert db.known_paths(conn) == set()


# ---------------------------------------------------------------- 遗留论文
def test_carried_over_paper_gets_detail_on_next_run(conn, monkeypatch) -> None:
    """上次发信失败留下的论文，这次必须补齐摘要，不能带着空摘要被发出去。"""
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(pipeline.mailer, "send_digest", lambda p: (False, "failed"))

    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    # 第一次：详情抓取失败 + 发信失败，论文留在未报告队列里且没有摘要
    use_client(monkeypatch, FakeClient(
        [make_stub("1370", "2026-07-20"), make_stub("1369")],
        details={"/publ/work1370": PaperDetail()},
    ))
    pipeline.run_check(conn, trigger="first")
    assert db.get_paper(conn, "/publ/work1370")["detail_fetched"] == 0
    assert db.get_paper(conn, "/publ/work1370")["focus"] == ""

    # 第二次：列表里没有任何"新"论文，但遗留的那篇必须被补上摘要
    monkeypatch.setattr(pipeline.mailer, "send_digest", lambda p: (True, "sent"))
    client = FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")])
    use_client(monkeypatch, client)
    result = pipeline.run_check(conn, trigger="second")

    assert client.fetched == ["/publ/work1370"], "遗留论文没有被补抓详情"
    assert db.get_paper(conn, "/publ/work1370")["focus"] != ""
    assert result.delivered is True


def test_metadata_only_detail_stays_retryable(conn, monkeypatch) -> None:
    """只拿到作者/JEL 而没有摘要正文，不能算抓取成功，否则永久退出重试队列。"""
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    use_client(monkeypatch, FakeClient(
        [make_stub("1370", "2026-07-20"), make_stub("1369")],
        details={"/publ/work1370": PaperDetail(authors=["A"], jel_codes=["E52 - x"])},
    ))
    pipeline.run_check(conn, trigger="test")

    assert db.get_paper(conn, "/publ/work1370")["detail_fetched"] == 0
    assert "/publ/work1370" in db.needs_detail(conn, 10)


def test_corrected_title_is_refreshed(conn, monkeypatch) -> None:
    """BIS 事后更正标题时，库里应当跟着更新，但不能动已抓的摘要和报告状态。"""
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    before = db.get_paper(conn, "/publ/work1369")

    fixed = make_stub("1369")
    fixed.title = "Corrected title"
    use_client(monkeypatch, FakeClient([fixed]))
    pipeline.run_check(conn, trigger="test")

    after = db.get_paper(conn, "/publ/work1369")
    assert after["title"] == "Corrected title"
    assert after["reported_at"] == before["reported_at"]


def test_markdown_archive_written(conn, monkeypatch, tmp_path: Path) -> None:
    use_client(monkeypatch, FakeClient([make_stub("1369")]))
    pipeline.run_check(conn, trigger="baseline")
    use_client(monkeypatch, FakeClient([make_stub("1370", "2026-07-20"), make_stub("1369")]))
    result = pipeline.run_check(conn, trigger="test")

    archive = tmp_path / "archive"
    assert (archive / "README.md").exists()
    assert list((archive / "digests").glob("*.md")), "未写出简报归档"
    assert (archive / "papers" / "work1370.md").exists()
    assert Path(result.export_path).exists()
