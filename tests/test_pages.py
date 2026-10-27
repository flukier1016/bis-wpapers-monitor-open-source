"""页面渲染测试。

模板错误在单元测试里很容易漏掉——改个变量名、少个 endif，本地不点进去就发现
不了，直到线上 500。这里把每个页面都渲染一遍并做结构校验。
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, summaries

REAL_FILES = sorted(
    (Path(__file__).resolve().parent.parent / "summaries").glob("work*.json")
)
from app.bis import PaperDetail, PaperStub


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "STRICT_DATA_DIR", False)
    monkeypatch.setattr(config, "RUN_ON_STARTUP", False)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-token")

    conn = db.connect(tmp_path / "t.db")
    db.init(conn)
    summaries.init(conn)
    stub = PaperStub(
        path="/publ/work1366", series="1366",
        title="Assessing the effects of recent provisioning rules",
        authors=["F. Gamboa", "C. Sanchez-Quinto"], pub_date="2026-07-09",
    )
    db.insert_stub(conn, stub, baseline=False)
    db.update_detail(
        conn, "/publ/work1366",
        PaperDetail(
            focus="Focus text long enough.", contribution="Contribution text.",
            findings="Findings text.", abstract="Abstract text.",
            jel_codes=["G21 - Banks"], keywords=["credit risk"],
            pdf_pages=43, authors=["F. Gamboa"],
        ),
        relevance=37, relevance_why="JEL 命中 G2，主题词 credit",
    )
    summaries.upsert(conn, {
        "series": "1366",
        "title_zh": "期限差异化拨备新规的影响",
        "citation": "Gamboa, F. (2026). Assessing... BIS Working Papers No 1366.",
        "research_question": "研究问题正文。" * 12,
        "core_findings": ["结论一，足够长的一句话。", "结论二，同样够长。", "结论三，也够长。"],
        "method_and_data": "方法与数据正文。" * 12,
        "key_evidence": [
            {"anchor": "Table 3, Column 6", "number": "0.86115",
             "detail": "这条证据说明了什么，写得足够长以通过完整性校验。" * 2},
            {"anchor": "Table 8", "number": "-0.23403",
             "detail": "第二条证据的说明文字，同样足够长以通过校验。" * 2},
            {"anchor": "Table 9", "number": "-0.08885",
             "detail": "第三条证据的说明文字，同样足够长以通过校验。" * 2},
        ],
        "contribution": "创新与贡献正文。" * 12,
        "limitations": "局限性正文。" * 12,
        "implications": "实际意义正文。" * 12,
        "key_takeaway": "三到五句话的总括。",
        "thesis": {
            "theme": "银行监管与信贷风险",
            "data_sources": "数据来源说明，需要至少三十个字才能通过完整性校验。",
            "replicability": "在中国复现的可行性说明。",
            "related_series": ["1346", "1352"],
            "research_gaps": ["缺口一", "缺口二"],
        },
        "review": {"status": "passed", "rounds": 3,
                   "caveats": ["引用 Table 3 时请注意口径问题，这条注意事项足够长。"]},
        "generated_by": "Claude Opus 4.8 · 全文精读",
        "generated_at": "2026-07-19",
    })
    conn.commit()
    conn.close()

    from app.main import app

    with TestClient(app) as c:
        yield c


PAGES = ["/", "/thesis", "/paper/1366", "/runs", "/admin"]


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path} 渲染失败：{r.text[:300]}"
    assert len(r.text) > 500


@pytest.mark.parametrize("path", PAGES)
def test_no_unrendered_jinja(client, path: str) -> None:
    """页面上不该出现没渲染掉的模板语法。

    BibTeX 块要排除掉：`title = {{...}}` 的双花括号是 BibTeX 用来保护
    专有名词大小写的合法语法（没有它，plain.bst 会把 LCR 转成 Lcr），
    与 Jinja 的 {{ }} 只是长得像。
    """
    body = client.get(path).text
    body = re.sub(r'<pre id="bibtex-text".*?</pre>', "", body, flags=re.S)
    assert "{{" not in body and "{%" not in body, f"{path} 有未渲染的模板标记"


@pytest.mark.parametrize("path", PAGES)
def test_no_undefined_css_vars(client, path: str) -> None:
    """内联样式里引用的 CSS 变量必须在样式表里定义过。

    改名重构时最容易留下这种问题——页面不报错，只是颜色悄悄变成透明。
    """
    css = (Path(__file__).parent.parent / "app/static/style.css").read_text(
        encoding="utf-8"
    )
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", client.get(path).text))
    missing = used - defined
    assert not missing, f"{path} 用了未定义的变量：{missing}"


def test_paper_page_has_toc_and_copy(client) -> None:
    """详情页有 19,000 字，没目录就只能一路滚到底。"""
    body = client.get("/paper/1366").text
    assert 'class="toc"' in body
    for key in ("research_question", "key_evidence", "limitations"):
        assert f'href="#s-{key}"' in body, f"目录缺少 {key}"
        assert f'id="s-{key}"' in body, f"正文缺少 {key} 锚点"
    # 写论文最高频的动作是复制引用与摘录证据
    assert 'data-copy="#citation-text"' in body
    assert 'data-copy="#zh-body"' in body


def test_paper_page_shows_evidence_numbers(client) -> None:
    body = client.get("/paper/1366").text
    assert "0.86115" in body and "Table 3, Column 6" in body
    assert 'class="evidence-num"' in body


def test_caveats_come_before_body(client) -> None:
    """引用注意事项必须排在正文之前——这是使用者最该先看到的。"""
    body = client.get("/paper/1366").text
    assert body.index('id="caveats"') < body.index('id="s-research_question"')


def test_thesis_index_takeaway_is_visible_but_clamped(client) -> None:
    """索引页的摘要要截断显示，但**必须看得见**。

    原先把 .clamp 套在 <details> 里，折叠状态下是 0 行而不是 2 行 ——
    整个索引页看不到任何摘要内容，等于白截断。takeaway 中位数 637 字，
    13 篇全展开又比文章还长，所以是「截断显示 + 读全文链接」。
    """
    body = client.get("/thesis").text
    assert "clamp" in body and "takeaway" in body

    # 摘要不能被包在 details 里
    for block in re.findall(r"<details.*?</details>", body, re.S):
        assert "takeaway" not in block, "摘要又被折叠进 details 了"

    # 每篇都要有可点进去读全文的入口
    assert body.count(">Read notes</a>") >= 13


def test_mobile_table_labels_present(client) -> None:
    """小屏幕下表头会被隐藏，每个单元格得自带标签才看得懂。"""
    body = client.get("/").text
    for label in ("Priority", "Number", "Title", "Authors"):
        assert f'data-label="{label}"' in body, f"表格单元格缺少 {label} 标签"


def test_nav_marks_current_page(client) -> None:
    assert 'aria-current="page"' in client.get("/").text
    assert 'aria-current="page"' in client.get("/thesis").text


def test_accessibility_basics(client) -> None:
    body = client.get("/").text
    assert 'class="skip"' in body, "缺少跳到主内容的链接"
    assert 'id="main"' in body
    assert 'scope="col"' in body, "表头缺少 scope"
    assert 'lang="en"' in body


def test_external_links_are_safe(client) -> None:
    """所有 target=_blank 都要带 rel=noopener。"""
    for path in PAGES:
        body = client.get(path).text
        for tag in re.findall(r"<a\b[^>]*target=['\"]_blank['\"][^>]*>", body):
            assert "noopener" in tag, f"{path} 有外链缺 rel=noopener：{tag[:90]}"


def test_paper_without_summary_still_renders(client) -> None:
    """没有中文总结的论文也要能正常打开。"""
    conn = db.connect()
    db.insert_stub(
        conn,
        PaperStub(path="/publ/work999", series="999", title="No summary paper",
                  authors=[], pub_date="2020-01-01"),
        baseline=True,
    )
    conn.commit()
    conn.close()

    r = client.get("/paper/999")
    assert r.status_code == 200
    assert 'class="toc"' not in r.text        # 没总结就不该有目录


def test_search_covers_chinese_summaries(client) -> None:
    """搜中文词必须能命中中文总结。

    修复前：/?q=拨备 返回 0 条，而 No 1366 整篇都在讲拨备——搜索只覆盖
    英文抓取字段，站点最贵的 13 篇内容反而搜不到。她脑子里的词是中文。
    """
    body = client.get("/", params={"q": "期限差异化拨备"}).text
    assert "work1366" in body or "1366" in body
    assert "共 0 条" not in body


def test_search_still_covers_english(client) -> None:
    body = client.get("/", params={"q": "provisioning"}).text
    assert "1366" in body


def test_search_no_match_is_clean(client) -> None:
    body = client.get("/", params={"q": "zzz-不存在的词-zzz"}).text
    assert "No matching papers" in body


def test_view_filters(client) -> None:
    """1369 篇里绝大多数只有标题，翻页会落进空表，所以要能只看有内容的。"""
    assert client.get("/", params={"view": "zh"}).status_code == 200
    assert client.get("/", params={"view": "scored"}).status_code == 200
    assert "Chinese notes 1" in client.get("/").text        # 筛选条带数量


def test_sort_by_relevance(client) -> None:
    assert client.get("/", params={"sort": "relevance"}).status_code == 200


def test_filters_survive_pagination(client) -> None:
    """翻页时不能把筛选和排序丢掉。"""
    body = client.get("/", params={"q": "credit", "view": "zh", "sort": "relevance"}).text
    if "下一页" in body:
        assert "view=zh" in body and "sort=relevance" in body


def test_mobile_toc_keeps_all_sections(client) -> None:
    """手机上目录不能把七个维度藏掉。

    曾经给所有维度链接加了 class="sub"，而 CSS 在 ≤900px 下
    .toc .sub{display:none} —— 结果手机上目录只剩四个外围条目，
    「关键证据」（摘系数的地方）从目录里彻底消失。
    """
    body = client.get("/paper/1366").text
    toc = body[body.index('class="toc"'):body.index("</aside>")]
    assert 'class="sub"' not in toc, "目录链接不该带会被移动端隐藏的 class"
    for key in ("research_question", "core_findings", "key_evidence",
                "method_and_data", "contribution", "limitations", "implications"):
        assert f'href="#s-{key}"' in toc, f"移动端目录缺少 {key}"

    css = (Path(__file__).parent.parent / "app/static/style.css").read_text(encoding="utf-8")
    assert ".toc .sub { display: none; }" not in css


# ---------------------------------------------------------------- 页码链接
def test_pdf_page_links(client) -> None:
    """「PDF 第 N 页」应变成能直接跳转的链接。"""
    from app.main import _link_pdf_pages

    out = _link_pdf_pages("见 PDF 第 19 页的表格", "https://x/w.pdf", 3, 40)
    assert 'href="https://x/w.pdf#page=19"' in out


def test_printed_page_links_apply_offset(client) -> None:
    """印刷页码必须加偏移。

    BIS 的 PDF 前面有封面和免责声明，印刷第 1 页通常落在 PDF 第 3-4 页。
    不加偏移会跳错几页，读者对不上内容，很可能误判成总结写错了。
    """
    from app.main import _link_pdf_pages

    out = _link_pdf_pages("见印刷第 16 页", "https://x/w.pdf", offset=3, valid_until=40)
    assert "#page=19" in out, "印刷第 16 页 + 偏移 3 应指向 PDF 第 19 页"

    out = _link_pdf_pages("见印刷页 p.16", "https://x/w.pdf", offset=3, valid_until=40)
    assert "#page=19" in out


def test_no_link_when_offset_unknown(client) -> None:
    """偏移未知时宁可不给链接，也不能给一个跳错地方的。"""
    from app.main import _link_pdf_pages

    out = _link_pdf_pages("见印刷第 16 页", "https://x/w.pdf", offset=0, valid_until=0)
    assert "<a" not in out


def test_no_link_beyond_valid_range(client) -> None:
    """附录常常重新从第 1 页编号，超出适用范围就不给链接。

    实测 work1354 正文偏移 +3、附录偏移 +47，只取众数会让附录页码跳错几十页。
    """
    from app.main import _link_pdf_pages

    out = _link_pdf_pages("见印刷第 50 页", "https://x/w.pdf", offset=3, valid_until=43)
    assert "<a" not in out


def test_page_links_appear_on_real_page(client) -> None:
    body = client.get("/paper/1366").text
    assert "pageref" in body or "印刷" not in body


def test_real_summaries_have_page_offsets(client) -> None:
    """仓库里的总结都应带偏移量，否则页码链接一个都生成不出来。"""
    import json

    root = Path(__file__).parent.parent / "summaries"
    for path in sorted(root.glob("work*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "pdf_page_offset" in data, f"{path.stem} 缺 pdf_page_offset"
        assert data["pdf_page_offset"] >= 0


# ---------------------------------------------------------------- BibTeX
def test_bibtex_generated(client) -> None:
    body = client.get("/paper/1366").text
    assert "@techreport" in body
    assert "Bank for International Settlements" in body
    assert 'data-copy="#bibtex-text"' in body


def test_bibtex_fields(client) -> None:
    from app.main import _bibtex

    entry = _bibtex(
        {"series": "1366"},
        {
            "series": "1366", "title": "Assessing the effects",
            "authors": ["Fredy Gamboa", "Camilo Sanchez-Quinto"],
            "pub_date": "2026-07-09",
            "pdf_url": "https://www.bis.org/publ/work1366.pdf",
        },
    )
    assert entry.startswith("@techreport{bis1366gamboa,")
    assert "Fredy Gamboa and Camilo Sanchez-Quinto" in entry
    assert "number      = {1366}" in entry
    assert "year        = {2026}" in entry
    assert entry.rstrip().endswith("}")


def test_bibtex_survives_missing_authors(client) -> None:
    from app.main import _bibtex

    entry = _bibtex({}, {"series": "1", "title": "T", "pub_date": "2020-01-01"})
    assert "@techreport{bis1," in entry


# ---------------------------------------------------------------- 证据括注
def test_evidence_citation_button(client) -> None:
    """结构化系数要能一键复制成可直接粘进正文的括注。"""
    body = client.get("/paper/1366").text
    assert "evidence-cite" in body or "coef" not in body


def test_real_summaries_structured_coefficients() -> None:
    """coef 必须是能逐字回原文核验的数值，不能是中文转述或换算值。"""
    import json
    import re as _re

    root = Path(__file__).parent.parent / "summaries"
    bad = []
    for path in sorted(root.glob("work*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for i, ev in enumerate(data.get("key_evidence", [])):
            coef = ev.get("coef")
            if coef is None:
                continue
            # 允许纯数字、带 %、以及原文里的英文/短划线区间
            if not _re.fullmatch(
                r"-?\d+(\.\d+)?%?(\s*(to|[–-])\s*-?\d+(\.\d+)?%?)?", str(coef)
            ):
                bad.append((path.stem, i, coef))
    assert not bad, f"coef 不是可核验的数值：{bad}"


def test_page_range_is_linked_whole(client) -> None:
    """页码区间要整体成为链接，不能截成「印刷第1」后面挂着「–2页」。"""
    from app.main import _link_pdf_pages

    out = _link_pdf_pages("原文（印刷第1–2页）", "https://x/w.pdf", 3, 40)
    assert ">印刷第1–2页</a>" in out, out
    assert "#page=4" in out          # 区间取起始页

    out = _link_pdf_pages("见 PDF 第 19-20 页", "https://x/w.pdf", 3, 40)
    assert ">PDF 第 19-20 页</a>" in out
    assert "#page=19" in out


def test_review_note_is_visible_not_only_tooltip(client) -> None:
    """质检状态说明不能只放在 title 属性里——手机上没有 hover。

    这条决定她敢不敢引用一篇总结，藏在 tooltip 里等于没有。
    """
    import json

    conn = db.connect()
    summaries.upsert(conn, {
        "series": "1366", "title_zh": "有问题的一篇",
        "thesis": {"theme": "银行监管与信贷风险"},
        "key_takeaway": "摘要。",
        "review": {"status": "issues"},
    })
    conn.commit()
    conn.close()

    body = client.get("/thesis").text
    assert "Review issues" in body
    # 说明文字必须作为正文出现，而不是只在 title="..." 里
    stripped = re.sub(r'title="[^"]*"', "", body)
    assert "check the original before citing" in stripped


def test_back_link_degrades_without_js(client) -> None:
    """返回链接在没有 JS 时也要能用，所以 href 必须是真地址。"""
    body = client.get("/paper/1366").text
    assert 'href="/" data-back' in body


# ---------------------------------------------------------------- BibTeX 正确性
class TestBibtexAuthors:
    """作者顺序必须来自 APA 引用，不能用 BIS 接口返回的顺序。

    终检发现 13 篇里有 10 篇的 BibTeX 首作者与 PDF 封面署名不符：
    work1364 接口给 Zampolli 打头，封面是 Gorea。复核 agent 实际编译了
    LaTeX，证实正文会印成「Zampolli et al. (2026)」——参考文献表也跟着错，
    答辩时一翻原文封面当场对不上。
    """

    def test_order_follows_citation_not_api(self) -> None:
        from app.main import _bibtex

        entry = _bibtex(
            {"citation": "Gorea, D., Ng, D. X., & Zampolli, F. (2026). T. BIS Working Papers No 1364."},
            # 接口返回的顺序是错的，必须被忽略
            {"series": "1364", "title": "T", "pub_date": "2026-06-28",
             "authors": ["Fabrizio Zampolli", "Denis Gorea", "Ding Xuan Ng"]},
        )
        assert entry.startswith("@techreport{bis1364gorea,")
        assert "{Gorea, D. and Ng, D. X. and Zampolli, F.}" in entry
        assert "zampolli," not in entry.splitlines()[0]

    def test_compound_surname_kept_whole(self) -> None:
        """"Di Stefano" 不能被截成 "Stefano"。"""
        from app.main import _authors_from_citation, _bibtex

        assert _authors_from_citation(
            "Di Stefano, A., Lengwiler, Y., & Rishabh, K. (2026)."
        ) == ["Di Stefano, A.", "Lengwiler, Y.", "Rishabh, K."]
        entry = _bibtex(
            {"citation": "Di Stefano, A., Lengwiler, Y. (2026)."},
            {"series": "1356", "title": "T", "pub_date": "2026-06-05"},
        )
        assert entry.startswith("@techreport{bis1356distefano,")

    def test_diacritics_folded_not_dropped(self) -> None:
        """Hördahl 的 key 应是 hordahl，不是 hrdahl。"""
        from app.main import _bibtex

        entry = _bibtex(
            {"citation": "Hördahl, P., Kısacıkoğlu, B. (2026)."},
            {"series": "1361", "title": "T", "pub_date": "2026-06-16"},
        )
        assert entry.startswith("@techreport{bis1361hordahl,")
        assert "Hördahl, P. and Kısacıkoğlu, B." in entry

    def test_falls_back_to_api_authors_without_citation(self) -> None:
        from app.main import _bibtex

        entry = _bibtex({}, {"series": "1", "title": "T",
                             "pub_date": "2020-01-01", "authors": ["Jane Roe"]})
        assert "@techreport{bis1roe," in entry
        assert "{Jane Roe}" in entry


class TestBibtexSyntax:
    """条目必须能真的编译。"""

    def test_latex_reserved_chars_escaped(self) -> None:
        from app.main import _bibtex

        entry = _bibtex(
            {}, {"series": "9", "title": "Cost & risk: 50% of #1 funding",
                 "pub_date": "2026-01-01", "authors": ["A B"]},
        )
        assert r"\&" in entry and r"\%" in entry and r"\#" in entry
        # 转义后不应再有裸的保留字符（url 行除外）
        for line in entry.splitlines():
            if "url" in line:
                continue
            for ch in ("&", "%", "#"):
                for i, c in enumerate(line):
                    if c == ch:
                        assert line[i - 1] == "\\", f"{ch} 未转义: {line}"

    def test_html_entities_decoded(self) -> None:
        """Ger&#353;l 里的 & 和 # 都是 LaTeX 保留字符，不解码就编译不过。"""
        from app.main import _bibtex

        entry = _bibtex(
            {}, {"series": "9", "title": "T", "pub_date": "2026-01-01",
                 "authors": ["Adam Ger&#353;l"]},
        )
        assert "Geršl" in entry
        assert "&#" not in entry

    def test_month_is_macro_not_braced(self) -> None:
        """month = jul（宏），不是 month = {jul}（字面量）。"""
        from app.main import _bibtex

        entry = _bibtex({}, {"series": "9", "title": "T", "pub_date": "2026-07-09"})
        assert "month       = jul" in entry
        assert "{jul}" not in entry

    def test_title_case_protected(self) -> None:
        from app.main import _bibtex

        entry = _bibtex({}, {"series": "9", "title": "LCR and NBFI risk",
                             "pub_date": "2026-01-01"})
        assert "title       = {{LCR and NBFI risk}}" in entry

    def test_braces_balanced(self) -> None:
        from app.main import _bibtex

        entry = _bibtex(
            {"citation": "Di Stefano, A. (2026)."},
            {"series": "9", "title": "A & B", "pub_date": "2026-01-01"},
        )
        assert entry.count("{") == entry.count("}")


@pytest.mark.skipif(not REAL_FILES, reason="尚未生成中文总结")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_bibtex_first_author_matches_citation(path: Path) -> None:
    """仓库里每一篇的 BibTeX 首作者都要与 APA 引用一致。"""
    import json as _json

    from app.main import _authors_from_citation, _bibtex

    data = _json.loads(path.read_text(encoding="utf-8"))
    parsed = _authors_from_citation(data["citation"])
    assert parsed, f"{path.stem} 的 citation 解析不出作者"

    entry = _bibtex(data, {"series": data["series"], "title": "T",
                           "pub_date": "2026-01-01"})
    surname = parsed[0].split(",")[0]
    folded = unicodedata.normalize("NFKD", surname)
    expect = re.sub(r"[^A-Za-z]", "", folded).lower()
    assert entry.startswith(f"@techreport{{bis{data['series']}{expect},")


class TestErrorPages:
    """浏览器里的错误不该是一段裸 JSON。

    终检：整个应用没注册 exception_handler，页面路由上的 404/422 一律
    以 JSON 返回，浏览器里是个没有导航、没有返回入口的死胡同。
    API 路径仍应保持 JSON。
    """

    def test_html_404_is_a_page(self, client) -> None:
        r = client.get("/paper/9999")
        assert r.status_code == 404
        assert "text/html" in r.headers["content-type"]
        assert "Back to papers" in r.text
        assert "<nav" in r.text          # 有导航，不是死胡同

    def test_html_422_is_a_page(self, client) -> None:
        r = client.get("/", params={"q": "x" * 400})
        assert r.status_code == 422
        assert "text/html" in r.headers["content-type"]
        assert "Back to papers" in r.text

    def test_page_title_length_is_searchable(self, client) -> None:
        """站点自己展示的最长标题，必须能整段贴进搜索框。

        原先 q 限 120 字符、jel 限 20，而页面上展示的标题最长 160、
        JEL 标签最长 85 —— 把看到的内容复制进搜索框必定撞 422。
        """
        long_title = "Assessing the effects of recent provisioning rules on " * 3
        assert len(long_title) > 120
        assert client.get("/", params={"q": long_title}).status_code == 200

        long_jel = "G21 - Banks; Depository Institutions; Micro Finance Institutions; Mortgages"
        assert len(long_jel) > 20
        assert client.get("/", params={"jel": long_jel}).status_code == 200

    def test_api_404_stays_json(self, client) -> None:
        r = client.get("/thesis/9999.md")
        assert r.status_code == 404
        assert "application/json" in r.headers["content-type"]

    def test_api_422_stays_json(self, client) -> None:
        r = client.get("/api/papers.json", params={"limit": 99999})
        assert r.status_code == 422
        assert "application/json" in r.headers["content-type"]

    def test_admin_errors_stay_json(self, client) -> None:
        r = client.post("/admin/run")
        assert r.status_code in (401, 503)
        assert "application/json" in r.headers["content-type"]


def test_api_offset_upper_bound(client) -> None:
    """offset 只限下界不限上界时，超大值会让 sqlite3 抛 OverflowError -> 500。"""
    r = client.get("/api/papers.json", params={"offset": 2**63})
    assert r.status_code == 422, "应被参数校验挡下，而不是 500"


def test_search_normalises_invisible_whitespace(client) -> None:
    """从 PDF 复制来的词常带不可见空白，不规范化就静默搜不到。

    这类失败最难察觉：不报错，只是结果数悄悄变成 0 或变少。
    """
    plain = client.get("/", params={"q": "拨备"})
    for weird in [" 拨备 ", "拨​备", "　拨备　", "  拨备  "]:
        r = client.get("/", params={"q": weird})
        assert r.status_code == 200
        n_plain = re.search(r"(\d+) results", plain.text)
        n_weird = re.search(r"(\d+) results", r.text)
        assert n_plain and n_weird and n_plain.group(1) == n_weird.group(1), (
            f"{weird!r} 的结果数与干净词不一致"
        )


def test_admin_archive_uses_header_not_query_token(client):
    assert client.get('/admin/export.zip?token=test-token').status_code == 401
    response = client.get('/admin/export.zip', headers={'X-Admin-Token': 'test-token'})
    assert response.status_code == 200
    html = client.get('/admin').text
    assert "'X-Admin-Token': token()" in html
    assert '?token=' not in html


def test_public_runs_hide_recipient_and_error_details(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, 'MAIL_TO', ['private-recipient@example.com'])
    conn = db.connect()
    conn.execute("INSERT INTO runs (trigger, started_at, status, error) VALUES (?, ?, ?, ?)",
                 ('test', '2026-01-01', 'error', 'private-recipient@example.com'))
    conn.commit()
    conn.close()
    body = client.get('/runs').text
    assert 'private-recipient@example.com' not in body
    assert 'An error occurred; check private server logs.' in body
