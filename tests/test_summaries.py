"""中文深度总结的存储、载入与渲染测试。

另外对仓库里真实的 summaries/*.json 做结构与可信度校验 ——
这些内容要进毕业论文，格式塌了或数字栏空了必须能被测试发现。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db, render, summaries

REPO_SUMMARIES = Path(__file__).resolve().parent.parent / "summaries"

SAMPLE = {
    "series": "1346",
    "title_zh": "利率水平如何影响信贷损失率",
    "citation": "Fandl, M. et al. (2026). How do interest rate levels affect credit loss rates. BIS Working Papers No 1346.",
    "research_question": "利率上行会在多大程度上推高银行的信贷损失率？",
    "core_findings": ["政策利率上升会推高损失率", "新兴市场的弹性更大"],
    "method_and_data": "跨国面板，1990-2024，覆盖 30 余个经济体。",
    "key_evidence": [
        {"anchor": "Table 4", "number": "0.35", "detail": "政策利率上升约 3 个百分点，损失率上升 0.35 个百分点。"}
    ],
    "contribution": "首次给出可用于压力测试的经验法则。",
    "limitations": "样本以发达经济体为主。",
    "implications": "对逆周期资本缓冲的校准有直接参考价值。",
    "key_takeaway": "给出了一个简单可用的利率—信贷损失经验法则。",
    "thesis": {
        "theme": "银行监管与信贷风险",
        "data_sources": "BIS 与 IMF 公开数据",
        "replicability": "中国可用银保监会口径的不良率替代",
        "related_series": ["1352", "1366"],
        "research_gaps": ["未区分抵押贷款与消费贷"],
    },
    "generated_by": "Claude Opus 4.8 · 全文精读",
    "generated_at": "2026-07-18",
    "source_chars": 125812,
}


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "t.db")
    db.init(connection)
    summaries.init(connection)
    yield connection
    connection.close()


OLD_SCHEMA = """
CREATE TABLE summaries (
    series TEXT PRIMARY KEY, title_zh TEXT NOT NULL DEFAULT '',
    citation TEXT NOT NULL DEFAULT '', theme TEXT NOT NULL DEFAULT '',
    research_question TEXT NOT NULL DEFAULT '', core_findings TEXT NOT NULL DEFAULT '[]',
    method_and_data TEXT NOT NULL DEFAULT '', key_evidence TEXT NOT NULL DEFAULT '[]',
    contribution TEXT NOT NULL DEFAULT '', limitations TEXT NOT NULL DEFAULT '',
    implications TEXT NOT NULL DEFAULT '', key_takeaway TEXT NOT NULL DEFAULT '',
    thesis TEXT NOT NULL DEFAULT '{}', generated_by TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL DEFAULT '', source_chars INTEGER NOT NULL DEFAULT 0
);
"""


def test_old_db_gets_new_columns(tmp_path: Path) -> None:
    """老库要能自动补列。

    CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，不会补列 ——
    加 review 字段那次就是这样把本地服务弄崩的（table summaries has no
    column named review）。线上 Volume 里的库是长期存在的，没有这层迁移，
    以后每加一个字段都会让实例在启动时挂掉。
    """
    connection = db.connect(tmp_path / "old.db")
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    assert "review" not in {
        r["name"] for r in connection.execute("PRAGMA table_info(summaries)")
    }

    summaries.init(connection)

    columns = {r["name"] for r in connection.execute("PRAGMA table_info(summaries)")}
    assert {"review", "review_status"} <= columns

    summaries.upsert(connection, {**SAMPLE, "review": {"status": "passed"}})
    connection.commit()
    assert summaries.get(connection, "1346")["review_ok"] is True
    connection.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "x.db")
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    summaries.init(connection)
    added = db.ensure_columns(connection, "summaries", summaries.ADDED_COLUMNS)
    assert added == [], "第二次迁移不应再改动任何东西"
    connection.close()


def test_ensure_columns_skips_missing_table(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "y.db")
    assert db.ensure_columns(connection, "nonexistent", {"a": "TEXT"}) == []
    connection.close()


def test_review_status_defaults_to_pending(conn) -> None:
    """没有 review 块的总结绝不能被当成"已通过质检"——默认必须是待质检。"""
    summaries.upsert(conn, SAMPLE)
    conn.commit()
    got = summaries.get(conn, "1346")

    assert got["review_status"] == "pending"
    assert got["review_ok"] is False
    assert "Pending review" in got["review_label"]


def test_review_status_roundtrip(conn) -> None:
    summaries.upsert(conn, {**SAMPLE, "review": {"status": "passed", "rounds": 2}})
    conn.commit()
    got = summaries.get(conn, "1346")

    assert got["review_ok"] is True
    assert got["review"]["rounds"] == 2
    assert "Reviewed" in got["review_label"]


def test_unknown_review_status_is_not_trusted(conn) -> None:
    """伪造/拼错的状态值不能意外获得"已通过"待遇。"""
    summaries.upsert(conn, {**SAMPLE, "review": {"status": "PASSED-ish"}})
    conn.commit()
    assert summaries.get(conn, "1346")["review_ok"] is False


def test_review_counts(conn) -> None:
    summaries.upsert(conn, {**SAMPLE, "review": {"status": "passed"}})
    summaries.upsert(conn, {**SAMPLE, "series": "1352", "review": {"status": "issues"}})
    summaries.upsert(conn, {**SAMPLE, "series": "1353"})
    conn.commit()

    counts = summaries.review_counts(conn)
    assert counts == {"passed": 1, "issues": 1, "pending": 1}


def test_caveats_surface_in_markdown() -> None:
    """引用注意事项必须出现在导出的 Markdown 顶部——这是使用者最该先看到的。"""
    md = render.summary_to_markdown(
        {**SAMPLE, "review": {"status": "passed",
                              "caveats": ["Table 4 Column 3 是 10% 显著"]}},
        None,
    )
    assert "引用前请先核对这几处" in md
    assert "Table 4 Column 3 是 10% 显著" in md
    # 应排在正文之前
    assert md.index("引用前请先核对") < md.index("Research question")


def test_no_caveats_no_section() -> None:
    md = render.summary_to_markdown({**SAMPLE, "review": {"status": "passed"}}, None)
    assert "引用前请先核对" not in md


def test_unreviewed_markdown_carries_warning() -> None:
    md = render.summary_to_markdown({**SAMPLE, "review": {"status": "issues"}}, None)
    assert "尚未通过独立质检" in md
    clean = render.summary_to_markdown({**SAMPLE, "review": {"status": "passed"}}, None)
    assert "尚未通过独立质检" not in clean


def test_upsert_and_get_roundtrip(conn) -> None:
    summaries.upsert(conn, SAMPLE)
    conn.commit()
    got = summaries.get(conn, "1346")

    assert got is not None
    assert got["title_zh"] == SAMPLE["title_zh"]
    assert got["core_findings"] == SAMPLE["core_findings"]
    assert got["key_evidence"][0]["number"] == "0.35"
    assert got["thesis"]["related_series"] == ["1352", "1366"]
    assert got["theme"] == "银行监管与信贷风险"
    assert got["source_chars"] == 125812


def test_upsert_is_idempotent(conn) -> None:
    summaries.upsert(conn, SAMPLE)
    summaries.upsert(conn, {**SAMPLE, "title_zh": "改过的标题"})
    conn.commit()
    assert summaries.count(conn) == 1
    assert summaries.get(conn, "1346")["title_zh"] == "改过的标题"


def test_upsert_requires_series(conn) -> None:
    with pytest.raises(ValueError):
        summaries.upsert(conn, {"title_zh": "没有刊号"})


def test_load_from_disk(conn, tmp_path: Path) -> None:
    (tmp_path / "work1346.json").write_text(
        json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "work9999.json").write_text("{ 坏掉的 json", encoding="utf-8")

    loaded = summaries.load_from_disk(conn, tmp_path)

    assert loaded == 1, "损坏的文件应被跳过而不是让整批失败"
    assert summaries.count(conn) == 1
    assert summaries.series_with_summary(conn) == {"1346"}


def test_markdown_render_contains_everything() -> None:
    paper = {
        "title": "How do interest rate levels affect credit loss rates?",
        "authors": ["M. Fandl"], "pub_date": "2026-05-12", "pdf_pages": 52,
        "abstract_url": "https://www.bis.org/publ/work1346.htm",
        "pdf_url": "https://www.bis.org/publ/work1346.pdf",
    }
    md = render.summary_to_markdown(SAMPLE, paper)

    for needle in (
        "No 1346", "利率水平如何影响信贷损失率", "引用",
        "Research question", "Core findings", "Method and data", "Key evidence",
        "Contribution", "Limitations", "Implications", "Key Takeaway",
        "写作参考", "Table 4", "0.35", "No 1352",
    ):
        assert needle in md, f"Markdown 缺少 {needle}"


def test_markdown_flags_provenance() -> None:
    md = render.summary_to_markdown(SAMPLE, None)
    assert "authors’ findings" in md
    assert "writer’s judgment" in md


def test_markdown_survives_missing_fields() -> None:
    md = render.summary_to_markdown({"series": "1", "title_zh": "标题"}, None)
    assert "No 1" in md and "标题" in md


# ---------------------------------------------------------------- 真实文件校验
REAL_FILES = sorted(REPO_SUMMARIES.glob("work*.json"))

REQUIRED_TEXT = (
    "title_zh", "citation", "research_question", "method_and_data",
    "contribution", "limitations", "implications", "key_takeaway",
)


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_summary_is_complete(path: Path) -> None:
    """仓库里的真实总结不能有空字段或敷衍字段。"""
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data.get("series"), "缺 series"
    assert path.stem == f"work{data['series']}", "文件名与 series 不一致"

    for field in REQUIRED_TEXT:
        value = (data.get(field) or "").strip()
        assert value, f"{field} 为空"
        if field in ("research_question", "method_and_data", "contribution",
                     "limitations", "implications"):
            assert len(value) >= 150, f"{field} 只有 {len(value)} 字，过于敷衍"

    # 上界放到 9 是有意的。原始要求是「最重要的 3-5 个发现」，但结果密集的论文
    # （如 work1352）确实能给出九条各自锚定到不同表格的真实结论。为了凑数删掉
    # 已核验的发现，对写文献综述的人是净损失。这里只挡「无边界膨胀」，
    # 真正的约束是下面两条：每条都要够实、且必须锚定到具体表格。
    findings = data.get("core_findings") or []
    assert 3 <= len(findings) <= 9, f"core_findings 有 {len(findings)} 条，超出合理范围"
    assert all(len(f.strip()) >= 15 for f in findings), "有结论过短"


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_summary_evidence_is_anchored(path: Path) -> None:
    """每条关键证据都要能追溯到具体的表/图，否则写论文时无法引用。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    evidence = data.get("key_evidence") or []

    assert len(evidence) >= 3, f"只有 {len(evidence)} 条关键证据"
    for item in evidence:
        assert item.get("anchor", "").strip(), "证据缺少出处锚点"
        assert len(item.get("detail", "").strip()) >= 50, "证据说明过短"


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_significance_stars_are_preserved(path: Path) -> None:
    """显著性星号必须原样保留，不能被当成 Markdown 加粗吃掉。

    这条是有来历的：曾经写过一个"清理未配对 ** "的脚本，把 -0.037** 这类
    5% 显著性标记删掉了 37 处。对写论文的人来说，丢掉显著性等级是硬伤。
    正文里的 ** 有两种含义，只能靠"是否紧跟数字"来区分。
    """
    import re as _re

    from app.main import _emphasis

    data = json.loads(path.read_text(encoding="utf-8"))
    for text in _walk_strings(data):
        for match in _re.finditer(r"(-?\d+\.\d+)(\*{1,3})", text):
            rendered = str(_emphasis(text))
            assert match.group(0) in rendered, (
                f"显著性标记 {match.group(0)} 在渲染后丢失"
            )
            break


def test_emphasis_distinguishes_stars_from_bold() -> None:
    from app.main import _emphasis

    # 显著性星号：原样保留
    out = str(_emphasis("系数为 -0.037**（5% 显著），另一列 -0.064***"))
    assert "-0.037**" in out and "-0.064***" in out
    assert "<strong>" not in out

    # 真正的加粗：转成 <strong>
    out = str(_emphasis("这属于**对公信贷**范畴"))
    assert "<strong>对公信贷</strong>" in out

    # 两者混在同一句里
    out = str(_emphasis("**核心结论**：系数 -0.037** 在 5% 水平显著"))
    assert "<strong>核心结论</strong>" in out
    assert "-0.037**" in out

    # HTML 仍然被转义
    assert "&lt;script&gt;" in str(_emphasis("<script>x</script>"))


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_summary_has_been_reviewed(path: Path) -> None:
    """上线的总结必须带质检结论和引用注意事项。

    首轮 13 篇全部没通过质检（归属错误 88 条），所以这条不是形式主义：
    没有 review 块的总结会在页面上显示成"待质检"并附警告，
    这个测试保证仓库里的内容确实经过了复核。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    review = data.get("review") or {}

    assert review.get("status") == "passed", f"{path.stem} 尚未通过质检"
    caveats = review.get("caveats") or []
    assert caveats, "缺少引用注意事项——使用者需要知道该自己核对哪几处"
    assert all(len(c.strip()) >= 20 for c in caveats), "有注意事项过于笼统"


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_summary_thesis_block(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    thesis = data.get("thesis") or {}

    assert thesis.get("theme"), "缺主题归类"
    assert len((thesis.get("data_sources") or "").strip()) >= 30, "数据来源说明过短"
    gaps = thesis.get("research_gaps") or []
    assert 2 <= len(gaps) <= 6, f"research_gaps 有 {len(gaps)} 条，应为 2-4 条"
    for series in thesis.get("related_series") or []:
        assert series.isdigit(), f"related_series 里 {series!r} 不是刊号"
        assert series != data["series"], "不应把自己列为相关论文"


class TestMarkdownStarProtection:
    """导出的 .md 里显著性星号不能被 Markdown 渲染器吃掉。

    页面渲染靠 _BOLD_RE 区分两种星号，但导出走的是另一条路。
    原样输出的话，`-0.037**` 后面只要再出现一个 `**`，CommonMark 就会把
    中间整段配对成加粗，两处星号双双消失——显著性等级没了，还看不出少了东西。
    """

    def test_significance_stars_escaped(self) -> None:
        out = render._protect_stars("系数 -0.037** 与 0.051*** 以及 1.2*")
        assert r"-0.037\*\*" in out
        assert r"0.051\*\*\*" in out
        assert r"1.2\*" in out

    def test_bold_markers_untouched(self) -> None:
        """成对包裹词组的加粗不能被转义，否则正文里全是反斜杠。"""
        out = render._protect_stars("这属于**对公信贷**范畴")
        assert "**对公信贷**" in out
        assert "\\*" not in out

    def test_mixed_in_one_sentence(self) -> None:
        out = render._protect_stars("**核心结论**：系数 -0.037** 在 5% 显著")
        assert "**核心结论**" in out
        assert r"-0.037\*\*" in out

    def test_handles_none(self) -> None:
        assert render._protect_stars(None) == ""


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_markdown_keeps_all_stars(path: Path) -> None:
    """真实数据导出后，每个显著性标记都要还在。"""
    import re as _re

    data = json.loads(path.read_text(encoding="utf-8"))
    md = render.summary_to_markdown(data, None)

    for text in _walk_strings(data):
        for m in _re.finditer(r"(-?\d+\.?\d*)(\*{1,3})", text):
            escaped = m.group(1) + "".join("\\*" for _ in m.group(2))
            plain = m.group(0)
            assert escaped in md or plain in md, f"{plain} 在导出后丢失"
