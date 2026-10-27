"""BIS Working Papers 抓取层。

BIS 官网列表页是 JS 渲染的，但底层有一组稳定的 JSON 接口：

  列表   GET /api/document_lists/wppubls.json
         -> {"list": {"/publ/work1369": {short_title, authors, publication_start_date, ...}, ...}}
  元数据 GET /api/documents/publ/work1369.json
         -> {authors, jel_codes, pdf_pages, pdf_file_size, series_name, ...}
  摘要页 GET /publ/work1369.htm
         -> HTML 中嵌有 data-react-props JSON，其 content 字段是结构化正文：
            <h4>Focus</h4> ... <h4>Contribution</h4> ... <h4>Findings</h4> ... <h4>Abstract</h4>

因此无需无头浏览器，纯 HTTP 即可拿全部字段。
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from typing import Any, Iterable

import httpx

from . import config

log = logging.getLogger(__name__)

_PATH_RE = re.compile(r"^/publ/work(\d+)$")
_REACT_PROPS_RE = re.compile(r'data-react-props="([^"]*)"')
# 允许标题标签带属性/多余空白（<h4 class="x">），否则 BIS 一加 class 就会解析出 0 段。
# 同时接受 h3/h4：BIS 各期层级不完全一致。
_H4_SPLIT_RE = re.compile(r"<h[34](?:\s[^>]*)?>\s*(.*?)\s*</h[34]>", re.S | re.I)
_HAS_HEADING_RE = re.compile(r"<h[34](?:\s[^>]*)?>", re.I)
# JEL/Keywords 两行有时用 <br> 连在同一个 <p> 里，切段前先把 <br> 归一成段落分隔
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# content 尾部的两行元信息。
# BIS 各期用词不统一，实际见过 "JEL Codes:" / "JEL classification:" / "JEL Classifications:"，
# 三种都要认，否则会静默丢掉 JEL（进而影响优先级排序）。
# "JEL:" 的裸写法也要认，否则整行会漏进 Abstract 正文
_JEL_LINE_RE = re.compile(r"JEL(?:\s+(?:codes?|classifications?))?\s*:\s*(.+)", re.I)
_KEYWORDS_LINE_RE = re.compile(r"Key\s*words?\s*:\s*(.+)", re.I)

_SECTION_ALIASES = {
    "focus": "focus",
    "contribution": "contribution",
    "contributions": "contribution",
    "findings": "findings",
    "finding": "findings",
    "abstract": "abstract",
}


# ---------------------------------------------------------------- 数据结构
@dataclass
class PaperStub:
    """列表接口给出的轻量条目——用于"有没有新论文"的判断。"""

    path: str                       # /publ/work1369
    series: str                     # 1369
    title: str
    authors: list[str] = field(default_factory=list)
    pub_date: str = ""              # YYYY-MM-DD
    source_url: str = ""

    @property
    def abstract_url(self) -> str:
        return self.source_url or config.BIS_ABSTRACT_URL.format(path=self.path)

    @property
    def pdf_url(self) -> str:
        return "" if self.source_url else config.BIS_PDF_URL.format(path=self.path)


@dataclass
class PaperDetail:
    """摘要页 + 元数据接口合并后的完整记录。"""

    focus: str = ""
    contribution: str = ""
    findings: str = ""
    abstract: str = ""
    jel_codes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    pdf_pages: int | None = None
    pdf_size_kb: int | None = None
    pdf_url: str = ""
    authors: list[str] = field(default_factory=list)

    @property
    def has_summary(self) -> bool:
        return bool(self.focus or self.findings or self.abstract)


# ---------------------------------------------------------------- 纯函数：解析
def _clean(fragment: str) -> str:
    """HTML 片段 -> 干净纯文本。"""
    text = _TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    # BIS 正文里混有 BOM / 零宽字符
    text = text.replace("﻿", "").replace("​", "")
    return _WS_RE.sub(" ", text).strip()


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _plain(value: Any) -> str:
    """接口返回的字段统一解码 HTML 实体。

    BIS 的 JSON 里姓名和标题带着未解码的数字实体（Ger&#353;l 应为 Geršl，
    K&#305;sac&#305;ko&#287;lu 应为 Kısacıkoğlu）。原样落库的话，页面上直接
    显示成一串实体码，导出的 BibTeX 更糟——& 和 # 都是 LaTeX 保留字符，
    条目会编译不过，或者静默产出 'Ger353l' 这种乱码姓名进参考文献表。
    全库 1369 篇里有 45 篇受影响。
    """
    return html.unescape(str(value or "")).strip()


def _names(raw: Any) -> list[str]:
    """作者列表：解码实体、去空。

    注意顺序不可信 —— BIS 接口返回的顺序与 PDF 封面署名顺序常常不一致，
    引用格式请以总结 JSON 的 citation 字段为准（见 app/main.py 的
    _authors_from_citation）。
    """
    return [
        name
        for a in (raw or [])
        if isinstance(a, dict) and (name := _plain(a.get("name")))
    ]


def extract_react_content(page_html: str) -> str | None:
    """从摘要页 HTML 中取出 data-react-props 里的 content 字段。"""
    for match in _REACT_PROPS_RE.finditer(page_html):
        try:
            payload = json.loads(html.unescape(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
        found = _find_content(payload)
        if found:
            return found
    return None


def _find_content(node: Any, _depth: int = 0) -> str | None:
    """在任意嵌套的 JSON 里深搜正文 content 字段。

    优先取带小标题的（四段式俱全），但**退而求其次也接受没有小标题的正文** ——
    否则 BIS 换个排版就会把 Abstract / JEL / Keywords 一并丢掉。
    """
    best_fallback: str | None = None

    def walk(n: Any, depth: int) -> str | None:
        nonlocal best_fallback
        if depth > 12:
            return None
        if isinstance(n, dict):
            value = n.get("content")
            if isinstance(value, str) and value.strip():
                if _HAS_HEADING_RE.search(value):
                    return value
                if best_fallback is None and len(value) > 200:
                    best_fallback = value
            for child in n.values():
                hit = walk(child, depth + 1)
                if hit:
                    return hit
        elif isinstance(n, list):
            for child in n:
                hit = walk(child, depth + 1)
                if hit:
                    return hit
        return None

    return walk(node, _depth) or best_fallback


def _absorb_trailer(detail: PaperDetail, body: str) -> str:
    """从一段 HTML 里摘出 JEL / Keywords 两行，返回剩余的正文。

    BIS 有时把两行放在各自的 <p> 里，有时用 <br> 挤在同一个 <p> 里，
    所以先把 <br> 归一成段落分隔再逐段判断。
    """
    kept: list[str] = []
    for para in re.split(r"</p>|<p[^>]*>", _BR_RE.sub("</p><p>", body), flags=re.I):
        text = _clean(para)
        if not text:
            continue
        jel = _JEL_LINE_RE.match(text)
        kw = _KEYWORDS_LINE_RE.match(text)
        if jel:
            detail.jel_codes = _split_csv(jel.group(1))
        elif kw:
            detail.keywords = _split_csv(kw.group(1))
        else:
            kept.append(text)
    return " ".join(kept).strip()


def parse_sections(content_html: str) -> PaperDetail:
    """把 content HTML 切成 Focus / Contribution / Findings / Abstract。"""
    detail = PaperDetail()
    parts = _H4_SPLIT_RE.split(content_html)

    # 完全没有小标题：整段当 Abstract 处理，至少把摘要和 JEL/关键词救回来
    if len(parts) < 3:
        detail.abstract = _absorb_trailer(detail, content_html)
        if detail.abstract:
            log.info("正文没有小标题，已整体作为 Abstract 处理")
        return detail

    # parts = [前言, 标题1, 正文1, 标题2, 正文2, ...]
    for i in range(1, len(parts) - 1, 2):
        raw_name = _clean(parts[i]).lower().rstrip(":").strip()
        key = _SECTION_ALIASES.get(raw_name)
        body = parts[i + 1]
        if key is None:
            # BIS 改版新增/改名板块时要有声音，不能静默丢弃
            if raw_name and raw_name != "summary":
                log.warning("遇到未知板块「%s」，已跳过（BIS 可能改版了）", raw_name)
            continue

        if key == "abstract":
            detail.abstract = _absorb_trailer(detail, body)
        else:
            setattr(detail, key, _clean(body))

    # JEL/Keywords 偶尔落在 Abstract 板块之外，兜底再全局扫一遍
    if not detail.jel_codes or not detail.keywords:
        _absorb_trailer(detail, parts[-1])
    return detail


def parse_abstract_page(page_html: str) -> PaperDetail:
    content = extract_react_content(page_html)
    if not content:
        log.warning("摘要页中未找到 react content 字段")
        return PaperDetail()
    return parse_sections(content)


def parse_publications_page(page_html: str) -> tuple[list[PaperStub], int, int, int]:
    """Parse a BIS publications page, its advertised total and last page index."""
    soup = BeautifulSoup(page_html, "html.parser")
    total_text = soup.find(string=re.compile(r"Displaying\s+\d+\s*[-–]\s*\d+\s+of\s+\d+", re.I))
    if not total_text:
        raise ValueError("BIS listing does not contain a recognizable total")
    match = re.search(r"of\s+(\d+)", str(total_text), re.I)
    total = int(match.group(1))
    last = soup.select_one(".pager__item--last a")
    last_page = int(re.search(r"[?&]page=(\d+)", last.get("href", "")).group(1)) if last else 0
    stubs = []
    supplements = 0
    for card in soup.select(".card-wrapper"):
        heading = card.select_one(".card-heading")
        link = card.select_one("a.card-link[href]")
        if not heading or not link:
            continue
        badges = [x.get_text(" ", strip=True) for x in card.select(".card-header .badge")]
        if "BIS Working Paper" not in badges:
            continue
        number = next((re.fullmatch(r"No\s+(\d+)", x) for x in badges if re.fullmatch(r"No\s+(\d+)", x)), None)
        if not number:
            if re.search(r"\b(?:online )?appendix\b", heading.get_text(" ", strip=True), re.I):
                supplements += 1
                continue
            raise ValueError("Working paper listing is missing its series number")
        url = urljoin(config.BIS_BASE, link["href"])
        if urlparse(url).hostname != urlparse(config.BIS_BASE).hostname:
            raise ValueError("Unexpected listing host")
        date_node = card.select_one(".card-date")
        date = datetime.strptime(date_node.get_text(" ", strip=True), "%d %b %Y").date().isoformat() if date_node else ""
        authors = [a.get_text(" ", strip=True) for a in card.select(".card-author__link")]
        series = number.group(1)
        stubs.append(PaperStub(path=f"/publ/work{series}", series=series,
                               title=heading.get_text(" ", strip=True), authors=authors,
                               pub_date=date, source_url=url))
    if not stubs and total:
        raise ValueError("No working papers parsed from BIS listing page")
    return stubs, total, last_page, supplements


def parse_publication_detail(page_html: str) -> PaperDetail:
    soup = BeautifulSoup(page_html, "html.parser")
    for node in soup.select(".text__component"):
        if any(h.get_text(" ", strip=True).lower() in _SECTION_ALIASES for h in node.select("h3,h4")):
            detail = parse_sections(str(node))
            break
    else:
        detail = PaperDetail()
        meta = soup.select_one('meta[name="abstract"]')
        if meta:
            detail.abstract = meta.get("content", "").strip()
    detail.jel_codes = [a.get_text(" ", strip=True) for a in soup.select('a[href*="field_jel"]')]
    for a in soup.select('a[href$=".pdf"]'):
        url = urljoin(config.BIS_BASE, a["href"])
        if urlparse(url).hostname == urlparse(config.BIS_BASE).hostname:
            detail.pdf_url = url
            pages = re.search(r"PDF\s*\((\d+)\s+Pages?\)", a.get("title", ""), re.I)
            if pages: detail.pdf_pages = int(pages.group(1))
            break
    return detail


def parse_list_payload(payload: dict[str, Any]) -> list[PaperStub]:
    """document_lists/wppubls.json -> PaperStub 列表（按发布日期倒序）。"""
    raw = payload.get("list") or {}
    stubs: list[PaperStub] = []
    for path, item in raw.items():
        match = _PATH_RE.match(path or "")
        if not match:
            continue
        categories = item.get("recurse_category") or []
        if categories and "Working Papers" not in categories:
            continue
        stubs.append(
            PaperStub(
                path=path,
                series=match.group(1),
                title=_plain(item.get("short_title")),
                authors=_names(item.get("authors")),
                pub_date=(item.get("publication_start_date") or "")[:10],
            )
        )
    # 日期相同时用刊号做次级排序，保证顺序稳定
    stubs.sort(key=lambda s: (s.pub_date, int(s.series)), reverse=True)
    return stubs


def merge_doc_json(detail: PaperDetail, doc: dict[str, Any]) -> PaperDetail:
    """把 /api/documents/... 的元数据并入 detail（作者、带释义的 JEL、PDF 信息）。"""
    authors = _names(doc.get("authors"))
    if authors:
        detail.authors = authors
    jel = [c for c in (doc.get("jel_codes") or []) if isinstance(c, str) and c.strip()]
    if jel:
        # 接口给的是 "F34 - International Lending and Debt Problems"，信息更全，优先采用
        detail.jel_codes = jel
    if isinstance(doc.get("pdf_pages"), int):
        detail.pdf_pages = doc["pdf_pages"]
    if isinstance(doc.get("pdf_file_size"), int):
        detail.pdf_size_kb = doc["pdf_file_size"]
    return detail


# ---------------------------------------------------------------- IO：抓取
class BisClient:
    """对 BIS 的 HTTP 访问，串行 + 固定间隔，保持礼貌。"""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owns = client is None
        self._client = client or httpx.Client(
            timeout=config.HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
                "Referer": f"{config.BIS_BASE}/wpapers/index.htm",
            },
        )
        self._last_call = 0.0

    def __enter__(self) -> "BisClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def _throttle(self) -> None:
        gap = config.FETCH_DELAY_MS / 1000.0
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_call = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        self._throttle()
        response = self._client.get(url)
        response.raise_for_status()
        return response

    def fetch_list(self) -> list[PaperStub]:
        # The legacy JSON endpoint has been retired; walk the current BIS listing.
        base = config.BIS_BASE + "/publications?series%5BBIS%20Working%20Paper%5D=BIS%20Working%20Paper"
        first, total, last_page, supplements = parse_publications_page(self._get(base).text)
        stubs = first
        for page in range(1, last_page + 1):
            batch, page_total, _, page_supplements = parse_publications_page(self._get(f"{base}&page={page}").text)
            supplements += page_supplements
            if page_total != total:
                raise ValueError("BIS listing changed during pagination; retry later")
            stubs.extend(batch)
        if len(stubs) + supplements != total:
            raise ValueError(f"Incomplete BIS listing: {len(stubs)} papers and {supplements} appendices of {total} items")
        unique: dict[str, PaperStub] = {}
        for paper in stubs:
            previous = unique.get(paper.path)
            if previous:
                same_title = paper.title.casefold() == previous.title.casefold()
                same_authors = bool(paper.authors) and set(paper.authors) == set(previous.authors)
                if paper.pub_date != previous.pub_date or not (same_title or same_authors):
                    raise ValueError(f"Ambiguous BIS paper number: {paper.path}")
                # BIS sometimes lists two URLs for the same paper. Prefer the
                # unsuffixed URL; keep the first result for other variants.
                if previous.source_url.endswith("-0") and not paper.source_url.endswith("-0"):
                    unique[paper.path] = paper
            else:
                unique[paper.path] = paper
        log.info("BIS listing returned %d unique working papers (%d supplemental appendices)", len(unique), supplements)
        return list(unique.values())

    def fetch_detail(self, path: str, source_url: str = "") -> PaperDetail:
        if source_url and urlparse(source_url).hostname == urlparse(config.BIS_BASE).hostname and "/publications/" in urlparse(source_url).path:
            try:
                return parse_publication_detail(self._get(source_url).text)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Could not fetch BIS paper %s: %s", path, exc)
                return PaperDetail()
        detail = PaperDetail()
        try:
            detail = parse_abstract_page(self._get(config.BIS_ABSTRACT_URL.format(path=path)).text)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not fetch abstract %s: %s", path, exc)
        try:
            detail = merge_doc_json(detail, self._get(config.BIS_DOC_JSON.format(path=path)).json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not fetch metadata %s: %s", path, exc)
        return detail

    def fetch_details(self, paths: Iterable[str]) -> dict[str, PaperDetail]:
        return {path: self.fetch_detail(path) for path in paths}
