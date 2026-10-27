"""解析层测试：跑在真实抓下来的 BIS 摘要页快照上。"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from app import bis

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = sorted(FIXTURES.glob("p*.htm"))


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.stem)
def test_all_four_sections_parsed(path: Path) -> None:
    detail = bis.parse_abstract_page(path.read_text(encoding="utf-8", errors="replace"))
    assert detail.focus, f"{path.stem} 缺 Focus"
    assert detail.contribution, f"{path.stem} 缺 Contribution"
    assert detail.findings, f"{path.stem} 缺 Findings"
    assert detail.abstract, f"{path.stem} 缺 Abstract"
    assert detail.has_summary


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.stem)
def test_sections_are_clean_text(path: Path) -> None:
    detail = bis.parse_abstract_page(path.read_text(encoding="utf-8", errors="replace"))
    for field in (detail.focus, detail.contribution, detail.findings, detail.abstract):
        assert "<" not in field, "残留 HTML 标签"
        assert "&#" not in field and "&amp;" not in field, "残留 HTML 实体"
        assert "﻿" not in field, "残留 BOM"
        assert "  " not in field, "残留连续空白"
        assert len(field) > 50


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.stem)
def test_jel_and_keywords_lifted_out_of_abstract(path: Path) -> None:
    """JEL / Keywords 两行应被摘到独立字段，不应留在 Abstract 正文里。"""
    detail = bis.parse_abstract_page(path.read_text(encoding="utf-8", errors="replace"))
    assert detail.jel_codes, "未解析出 JEL"
    assert detail.keywords, "未解析出关键词"
    assert not detail.abstract.startswith("JEL")
    assert "Keywords:" not in detail.abstract
    assert "JEL Codes:" not in detail.abstract


def test_known_paper_exact_content() -> None:
    """对 work1369 做一次精确断言，锁住解析行为。"""
    page = (FIXTURES / "p1369.htm").read_text(encoding="utf-8", errors="replace")
    detail = bis.parse_abstract_page(page)
    assert detail.focus.startswith("Historically, banks' holdings of sovereign debt")
    assert "sovereign-bank-NBFI nexus" in detail.abstract
    assert detail.findings.startswith("We find that banks' direct sovereign exposures")
    assert "F34" in detail.jel_codes
    assert "nexus" in detail.keywords


def test_missing_content_degrades_gracefully() -> None:
    assert bis.parse_abstract_page("<html><body>nothing here</body></html>").focus == ""
    assert bis.parse_abstract_page("").has_summary is False


def test_malformed_react_props_is_skipped() -> None:
    """属性必须用双引号，否则正则根本不匹配，这个测试就成了假通过。"""
    broken = '<div data-react-props="{not json"></div>'
    assert bis.extract_react_content(broken) is None


def _wrap(content: str) -> str:
    """把一段 content 包成真实页面那样的 data-react-props 属性。"""
    payload = json.dumps({"document": {"content": content}})
    return f'<div data-react-props="{html.escape(payload, quote=True)}"></div>'


def test_heading_with_attributes_still_parses() -> None:
    """BIS 只要给 h4 加个 class，旧写法就会解析出 0 段 —— 必须容忍属性。"""
    page = _wrap(
        '<h4 class="hdr">Focus</h4><p>Focus body text here.</p>'
        '<h4 >Findings</h4><p>Findings body text here.</p>'
    )
    detail = bis.parse_abstract_page(page)
    assert detail.focus == "Focus body text here."
    assert detail.findings == "Findings body text here."


def test_h3_headings_also_parse() -> None:
    page = _wrap("<h3>Focus</h3><p>Body via h3.</p>")
    assert bis.parse_abstract_page(page).focus == "Body via h3."


def test_jel_and_keywords_joined_by_br() -> None:
    """两行挤在同一个 <p> 里用 <br> 分隔时，不能把关键词丢掉、也不能污染 JEL。"""
    page = _wrap(
        "<h4>Abstract</h4><p>The abstract body.</p>"
        "<p>JEL Codes: E52, F31<br>Keywords: carry, basis</p>"
    )
    detail = bis.parse_abstract_page(page)
    assert detail.jel_codes == ["E52", "F31"]
    assert detail.keywords == ["carry", "basis"]
    assert detail.abstract == "The abstract body."


def test_bare_jel_prefix_is_recognised() -> None:
    page = _wrap("<h4>Abstract</h4><p>Body.</p><p>JEL: G12, G15</p>")
    detail = bis.parse_abstract_page(page)
    assert detail.jel_codes == ["G12", "G15"]
    assert "JEL" not in detail.abstract


def test_content_without_headings_still_yields_abstract() -> None:
    """BIS 换成无小标题排版时，至少要把摘要和 JEL/关键词救回来，而不是整段丢弃。"""
    body = "This paper studies something important. " * 8
    page = _wrap(f"<p>{body}</p><p>JEL Codes: E43</p><p>Keywords: rates</p>")
    detail = bis.parse_abstract_page(page)
    assert body.strip() in detail.abstract
    assert detail.jel_codes == ["E43"]
    assert detail.keywords == ["rates"]
    assert detail.has_summary


def test_unknown_section_is_logged_not_silently_dropped(caplog) -> None:
    page = _wrap("<h4>Focus</h4><p>Body.</p><h4>Brand New Section</h4><p>x</p>")
    with caplog.at_level("WARNING"):
        bis.parse_abstract_page(page)
    assert any("brand new section" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------- 列表解析
LIST_PAYLOAD = {
    "list": {
        "/publ/work1369": {
            "path": "/publ/work1369",
            "short_title": "The evolving nexus",
            "publication_start_date": "2026-07-16",
            "authors": [{"id": 1, "name": "Stefan Avdjiev"}, {"id": 2, "name": "Bryan Hardy"}],
            "recurse_category": ["Working Papers", "Publications"],
        },
        "/publ/work1368": {
            "path": "/publ/work1368",
            "short_title": "Geopolitical risk",
            "publication_start_date": "2026-07-10",
            "authors": [{"id": 3, "name": "Someone"}],
            "recurse_category": ["Working Papers"],
        },
        # 同日不同刊号，用来验证次级排序
        "/publ/work1367": {
            "path": "/publ/work1367",
            "short_title": "Same day older number",
            "publication_start_date": "2026-07-16",
            "authors": [],
            "recurse_category": ["Working Papers"],
        },
        # 非 working paper，应被过滤
        "/publ/othr99": {
            "path": "/publ/othr99",
            "short_title": "Not a working paper",
            "publication_start_date": "2026-07-17",
            "recurse_category": ["Other"],
        },
    }
}


def test_list_parsing_filters_and_sorts() -> None:
    stubs = bis.parse_list_payload(LIST_PAYLOAD)
    assert [s.series for s in stubs] == ["1369", "1367", "1368"]
    assert all(s.path.startswith("/publ/work") for s in stubs)
    top = stubs[0]
    assert top.title == "The evolving nexus"
    assert top.authors == ["Stefan Avdjiev", "Bryan Hardy"]
    assert top.pub_date == "2026-07-16"
    assert top.abstract_url == "https://www.bis.org/publ/work1369.htm"
    assert top.pdf_url == "https://www.bis.org/publ/work1369.pdf"


def test_list_parsing_handles_empty() -> None:
    assert bis.parse_list_payload({}) == []
    assert bis.parse_list_payload({"list": {}}) == []


def test_merge_doc_json() -> None:
    detail = bis.PaperDetail(jel_codes=["F34"])
    merged = bis.merge_doc_json(
        detail,
        {
            "authors": [{"name": "A. Author"}, {"name": ""}],
            "jel_codes": ["F34 - International Lending and Debt Problems"],
            "pdf_pages": 37,
            "pdf_file_size": 687,
        },
    )
    assert merged.authors == ["A. Author"]
    assert merged.jel_codes == ["F34 - International Lending and Debt Problems"]
    assert merged.pdf_pages == 37
    assert merged.pdf_size_kb == 687


def test_current_listing_and_publication_details():
    from app.bis import parse_publications_page, parse_publication_detail
    listing = '''<h6>Displaying 1-1 of 1</h6><div class="card-wrapper">
      <a class="card-link" href="/publications/example"><div class="card-header">
      <span class="badge">BIS Working Paper</span><span class="badge">No 42</span>
      <span class="card-date">23 Oct 2026</span></div><h5 class="card-heading">Example</h5>
      <a class="card-author__link">A. Researcher</a></a></div>'''
    papers, total, last, supplements = parse_publications_page(listing)
    assert (total, last, len(papers), supplements) == (1, 0, 1, 0)
    assert papers[0].path == '/publ/work42'
    assert papers[0].source_url == 'https://www.bis.org/publications/example'
    assert papers[0].pub_date == '2026-10-23'
    detail = parse_publication_detail('''<div class="text__component"><h3>Focus</h3>
      <p>Question</p><h3>Findings</h3><p>Result</p></div>
      <a href="/publications/example.pdf" title="PDF (20 Pages)">PDF</a>
      <a href="/publications?field_jel%5B0%5D=E52">E52</a>''')
    assert detail.focus == 'Question' and detail.findings == 'Result'
    assert detail.pdf_url == 'https://www.bis.org/publications/example.pdf'
    assert detail.pdf_pages == 20 and detail.jel_codes == ['E52']


def test_current_listing_fails_closed_on_incomplete_pagination():
    import httpx
    import pytest
    from app.bis import BisClient

    listing = '''<h6>Displaying 1-1 of 2</h6><div class="card-wrapper">
      <a class="card-link" href="/publications/example"><div class="card-header">
      <span class="badge">BIS Working Paper</span><span class="badge">No 42</span>
      <span class="card-date">23 Oct 2026</span></div><h5 class="card-heading">Example</h5></a></div>'''
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=listing))
    with httpx.Client(transport=transport) as session:
        with BisClient(session) as client:
            with pytest.raises(ValueError, match='Incomplete'):
                client.fetch_list()


def test_current_listing_deduplicates_matching_papers_and_rejects_conflicts(monkeypatch):
    import httpx
    import pytest
    from app import bis
    from app.bis import BisClient, PaperStub
    monkeypatch.setattr(bis.config, 'FETCH_DELAY_MS', 0)
    one = PaperStub('/publ/work42', '42', 'Title', ['A'], '2026-01-01',
                    'https://www.bis.org/publications/paper-0')
    two = PaperStub('/publ/work42', '42', 'Title', ['A'], '2026-01-01',
                    'https://www.bis.org/publications/paper')
    monkeypatch.setattr(bis, 'parse_publications_page', lambda _: ([one, two], 2, 0, 0))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='fixture'))
    with httpx.Client(transport=transport) as session, BisClient(session) as client:
        papers = client.fetch_list()
        assert len(papers) == 1 and papers[0].source_url.endswith('/paper')
        two.title, two.authors = 'Unrelated', ['Different']
        with pytest.raises(ValueError, match='Ambiguous'):
            client.fetch_list()
