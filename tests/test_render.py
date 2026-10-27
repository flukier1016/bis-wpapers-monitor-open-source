"""评分与渲染测试。"""
from __future__ import annotations

from app import render, scoring
from app.bis import PaperDetail

FIXED_INCOME = PaperDetail(
    focus="f", contribution="c", findings="fi",
    abstract="We study the yield curve and term premium in sovereign bond markets.",
    jel_codes=["E43 - Interest Rates", "F31 - Foreign Exchange", "H63 - Sovereign Debt"],
    keywords=["yield curve", "term premium", "bond"],
    pdf_pages=32,
)
OFF_TOPIC = PaperDetail(
    focus="f", contribution="c", findings="fi",
    abstract="We study medieval agricultural land tenure patterns.",
    jel_codes=["N53 - Economic History"], keywords=["land tenure"], pdf_pages=110,
)


def test_relevant_paper_outranks_off_topic() -> None:
    hot = scoring.score_paper("Yield curve dynamics and FX carry", FIXED_INCOME)
    cold = scoring.score_paper("Medieval land tenure", OFF_TOPIC)
    assert hot.total > cold.total
    assert scoring.tier(hot.total) == "High"
    assert scoring.tier(cold.total) == "Low"


def test_score_explains_itself() -> None:
    score = scoring.score_paper("Yield curve dynamics", FIXED_INCOME)
    assert "JEL matches" in score.why
    assert "Topics" in score.why
    assert score.total > 0


def test_off_topic_gets_honest_reason() -> None:
    score = scoring.score_paper("Medieval land tenure", OFF_TOPIC)
    assert "Limited fixed income" in score.why
    assert "long" in score.why


def test_same_jel_family_not_double_counted() -> None:
    """E43/E44 同属 E4 大类，只应按最高分计一次。"""
    many = PaperDetail(jel_codes=["E43 - x", "E44 - y"], pdf_pages=30)
    one = PaperDetail(jel_codes=["E43 - x"], pdf_pages=30)
    assert scoring.score_paper("t", many).total == scoring.score_paper("t", one).total


def test_jel_points_are_capped() -> None:
    """JEL 堆料不应无限加分，否则会压过主题相关性。"""
    stuffed = PaperDetail(
        jel_codes=["E43 - a", "E52 - b", "F31 - c", "G12 - d", "H63 - e", "G01 - f"],
        pdf_pages=50,
    )
    assert scoring.score_paper("t", stuffed).total <= scoring.MAX_JEL_POINTS
    assert len(scoring.score_paper("t", stuffed).why.split("/")) <= scoring.MAX_JEL_FAMILIES


def test_score_never_negative() -> None:
    assert scoring.score_paper("", PaperDetail(pdf_pages=500)).total >= 0


# ---------------------------------------------------------------- 渲染
PAPER = {
    "series": "1369", "title": "The evolving nexus: sovereigns, banks & NBFIs",
    "authors": ["Stefan Avdjiev", "Bryan Hardy"], "pub_date": "2026-07-16",
    "abstract_url": "https://www.bis.org/publ/work1369.htm",
    "pdf_url": "https://www.bis.org/publ/work1369.pdf",
    "pdf_pages": 37, "pdf_size_kb": 687,
    "jel_codes": ["F34 - International Lending"], "keywords": ["nexus", "banks"],
    "focus": "Focus paragraph.", "contribution": "Contribution paragraph.",
    "findings": "Findings paragraph.", "abstract": "Abstract paragraph.",
    "relevance": 55, "relevance_why": "JEL matches F3",
}


def test_markdown_contains_all_required_fields() -> None:
    md = render.to_markdown([PAPER])
    for needle in (
        "The evolving nexus", "Stefan Avdjiev, Bryan Hardy", "2026-07-16",
        "work1369.htm", "work1369.pdf", "Focus", "Findings",
        "Contribution", "Abstract", "Key Takeaway",
    ):
        assert needle in md, f"简报缺少 {needle}"


def test_markdown_labels_provenance() -> None:
    """需求 7：必须能分清哪些是作者原文、哪些是系统计算。"""
    md = render.to_markdown([PAPER])
    assert "BIS original: Focus" in md
    assert "BIS original: Findings" in md
    assert "not the authors" in md
    assert "not generated" in md


def test_summary_table_only_when_multiple() -> None:
    assert "Summary" not in render.to_markdown([PAPER])
    two = render.to_markdown([PAPER, {**PAPER, "series": "1368", "relevance": 20}])
    assert "Summary" in two
    assert "Ranking rationale" in two
    assert "Priority is calculated by the system" in two


def test_empty_digest_message() -> None:
    md = render.to_markdown([])
    assert "No new papers found in this check." in md
    assert "Summary" not in md


def test_html_escapes_user_content() -> None:
    """标题里的 & 和潜在的尖括号必须被转义。"""
    evil = {**PAPER, "title": "Bonds <script>alert(1)</script> & rates"}
    html_out = render.to_html([evil])
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "&amp; rates" in html_out


def test_html_has_structure() -> None:
    html_out = render.to_html([PAPER, {**PAPER, "series": "1368"}])
    assert html_out.startswith("<!doctype html>")
    assert "Summary" in html_out
    assert "Key Takeaway" in html_out
    assert html_out.count("</div>") >= html_out.count("<div") - 1


def test_digest_title() -> None:
    assert "no new papers found" in render.digest_title(0)
    assert "3 new papers" in render.digest_title(3)
