"""FastAPI 应用：只读仪表盘 + 受保护的管理端点。"""
from __future__ import annotations

import logging
import math
import re
import secrets
import unicodedata
from contextlib import asynccontextmanager
from html import escape as html_escape, unescape as html_unescape
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import config, db, exporter, mailer, render, scheduler, scoring, summaries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bis")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
PAGE_SIZE = 25

# 中文总结里的 ** 有两种完全不同的含义，必须区分：
#   1. 计量显著性星号 —— 紧跟在数字后面，如 -0.037** 表示 5% 水平显著
#   2. Markdown 加粗   —— 成对包裹词组，如 **对公信贷**
# 全库有 56 处前者、15 处后者。把前者当加粗处理会吃掉显著性信息
# （对写论文的人来说这是硬伤），把后者原样输出则会在页面上看到字面星号。
# 判别依据：显著性星号一定紧跟数字，加粗的开标记前面是句首或空白/标点。
_BOLD_RE = re.compile(
    # 开标记前不能是数字、字母或星号。排除星号很关键：-0.047*** 里从第二个
    # 星号起也会构成 **，若只挡数字，它就会被当成开标记，把后面一整段吞掉。
    r"(?<![0-9A-Za-z*])"
    r"\*\*"
    r"(?=\S)([^*\n]{1,200}?)(?<=\S)"   # 内容不跨行、不含星号、两端非空白
    r"\*\*"
    r"(?![0-9A-Za-z*])"      # 闭标记后同理
)


def _emphasis(text: Any) -> Markup:
    """把中文总结里的 **加粗** 渲染成 <strong>，同时保留显著性星号原样。

    先做 HTML 转义再替换，所以正文里的尖括号仍然安全。
    """
    escaped = html_escape(str(text if text is not None else ""))
    return Markup(_BOLD_RE.sub(r"<strong>\1</strong>", escaped))


# 总结里引用页码有两种写法，且常常并列出现（「印刷页 p.16 / PDF 第 19 页」）。
# PDF 页码可以直接跳；印刷页码要加偏移——BIS 的 PDF 前面有封面和免责声明，
# 印刷第 1 页通常落在 PDF 第 3 或第 4 页，直接当 PDF 页码跳会差好几页，
# 读者对不上内容，很容易误判成总结写错了。
# 页码可能写成区间（「印刷第1–2页」）。不把区间一并吃掉的话，链接文字会
# 截成「印刷第1」，后面孤零零挂着「–2页」。
_RANGE = r"(?:\s*[–—－-]\s*\d+)?"
_PDF_PAGE_RE = re.compile(rf"PDF\s*第\s*(\d+){_RANGE}\s*页")
_PRINT_PAGE_RE = re.compile(rf"印刷(?:页\s*p\.?\s*|第\s*)(\d+){_RANGE}(?:\s*页)?")


def _link_pdf_pages(
    html: str, pdf_url: str, offset: int = 0, valid_until: int = 0
) -> str:
    """把正文里的页码引用变成能直接跳到 PDF 对应页的链接。"""
    if not pdf_url:
        return html

    def anchor(label: str, pdf_page: int) -> str:
        return (
            f'<a class="pageref" href="{html_escape(pdf_url)}#page={pdf_page}"'
            f' target="_blank" rel="noopener"'
            f' title="Go to PDF page {pdf_page}">{label}</a>'
        )

    def by_pdf(match: re.Match[str]) -> str:
        return anchor(match.group(0), int(match.group(1)))

    def by_print(match: re.Match[str]) -> str:
        printed = int(match.group(1))
        # 偏移未知，或页码超出偏移适用范围（附录常常重新编号），就不给链接
        if offset <= 0 or (valid_until and printed > valid_until):
            return match.group(0)
        return anchor(match.group(0), printed + offset)

    return _PRINT_PAGE_RE.sub(by_print, _PDF_PAGE_RE.sub(by_pdf, html))


def _zh_text(text: Any, zh: dict[str, Any] | None = None,
             pdf_url: str = "") -> Markup:
    """中文总结正文的渲染：转义 -> 加粗 -> 页码链接 -> 分段。

    分段这步不能省：JSON 里的长字段用空行分了段，塞进一个 <p> 的话
    HTML 会把空行全压成空格，页面上变成一整坨几百字不换行的文本，
    而同一份数据导出成 .md 时是分好段的 —— 两边观感差得很远。
    """
    rendered = str(_emphasis(text))
    if zh:
        rendered = _link_pdf_pages(
            rendered,
            pdf_url,
            int(zh.get("pdf_page_offset") or 0),
            int(zh.get("pdf_page_valid_until") or 0),
        )
    paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(rendered) if p.strip()]
    if len(paragraphs) <= 1:
        return Markup(rendered)
    return Markup("</p><p>".join(paragraphs))


# 一个及以上空行视为分段（兼容 \r\n）
_PARA_SPLIT_RE = re.compile(r"(?:\r?\n){2,}")


# APA 引用里的一位作者：「姓, 首字母.」
# 姓可以含空格与连字符（Di Stefano、Gamboa-Estrada），首字母允许变音大写（Í.）
_APA_AUTHOR_RE = re.compile(r"([^,&()]+?),\s*((?:[A-ZÀ-ÖØ-Þ]\.\s*)+)")
_ASCII_RE = re.compile(r"[^A-Za-z]")
# BibTeX/LaTeX 保留字符
_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _latex_escape(text: str) -> str:
    """转义 LaTeX 保留字符。

    BIS 接口返回的姓名里带 HTML 实体（Ger&#353;l），未解码就进 BibTeX 的话，
    & 和 # 都是 LaTeX 保留字符，条目直接编译不过（Misplaced alignment tab character &），
    或者静默产出 'Ger353l' 这种乱码姓名进参考文献表。
    """
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in text)


def _authors_from_citation(citation: str) -> list[str]:
    """从 APA 引用里取出作者，保持**封面署名顺序**。

    不能直接用 BIS 接口返回的 authors —— 实测 13 篇里有 10 篇的顺序与 PDF
    封面不一致（work1364 接口给 Zampolli 打头，封面是 Gorea）。BibTeX 用错顺序，
    正文里就会印成「Zampolli et al.」，参考文献表也跟着错，答辩时一翻原文封面就对不上。

    总结 JSON 里的 citation 字段是逐篇对着封面核过的，所以以它为准。
    返回「姓, 首字母.」形式，这也顺带解决了复合姓的问题：
    BibTeX 看到 "Di Stefano, A." 才知道姓是 "Di Stefano"，
    换成 "Alessandro Di Stefano" 会被当成姓 "Stefano"。
    """
    head = citation.split("(")[0] if "(" in citation else citation
    authors = [
        f"{surname.strip()}, {initials.strip()}"
        for surname, initials in _APA_AUTHOR_RE.findall(head)
        if surname.strip()
    ]
    return authors


# coef 不总是回归系数。IV 诊断的偏 F、图上读出的脉冲响应幅度都可能填在这儿，
# 一律标成「β =」会把非系数的量伪装成回归结果。
#
# 出处是图而非表，基本可以断定不是回归系数 —— 只看 anchor，
# 因为 detail 里顺带提一句「另见 Figure 3」不该改变判定。
_NON_BETA_ANCHORS = ("Figure", "Graph", "Chart", "图")
# 这几个词无论出现在哪里都说明不是回归系数
_NON_BETA_ANYWHERE = ("F 统计", "F统计", "偏 F", "第一阶段", "IV 诊断", "IV诊断")
# 注意「半弹性」不在此列：对数因变量回归的系数本来就是半弹性，它仍是 β


def _evidence_citation(e: dict[str, Any]) -> str:
    """把一条关键证据拼成可直接粘进正文的括注。

    三处刻意的处理，都是终检暴露出来的：

    * **量纲要带上**。53 条证据存了 unit 却从不输出，复制出来是裸数字 ——
      0.35 到底是百分点还是基点，抄进论文就说不清了。
    * **显著性缺失要明说**。20 条只有系数没有 sig，静默省略会让读者
      以为"没有显著性信息"，实则是我们的数据没记。改为写明"显著性见原表"。
    * **不是回归系数就别叫 β**。IV 的偏 F、脉冲响应幅度也存在 coef 字段里，
      标成 β 会把它们伪装成回归结果。
    """
    coef = str(e.get("coef") or "").strip()
    if not coef:
        return ""

    where = ", ".join(x for x in (e.get("table"), e.get("column")) if x)
    if not where:
        where = str(e.get("anchor") or "").strip()

    anchor = str(e.get("anchor") or "")
    everywhere = f"{anchor} {e.get('detail', '')}"
    is_beta = not (
        any(h in anchor for h in _NON_BETA_ANCHORS)
        or any(h in everywhere for h in _NON_BETA_ANYWHERE)
    )

    parts = [f"{'β' if is_beta else '值'} = {coef}"]
    if se := str(e.get("se") or "").strip():
        parts.append(f"SE = {se}")

    sig = str(e.get("sig") or "").strip()
    if sig == "不显著":
        parts.append("不显著")
    elif sig:
        parts.append(f"{sig} 水平显著")
    else:
        # 静默省略会让人以为"没有显著性"，而实际是我们的数据没记
        parts.append("显著性见原表")

    body = "，".join(parts)
    # 量纲另起一节，不塞进系数后面的括号 —— 单位本身常含括号
    # （「对数利率（半弹性）」），套起来会变成 β = -0.075（对数利率（半弹性））
    if unit := str(e.get("unit") or "").strip():
        body += f"；单位为{unit}"

    return f"（{where}：{body}）" if where else f"（{body}）"


def _evidence_badge(e: dict[str, Any]) -> str:
    """徽标上显示的数字。

    有 coef 时一律显示 coef —— 徽标和「复制括注」必须是同一个数。
    work1364 的第一条证据里 number 是偏 F 统计量 27.03、coef 是系数 0.00994，
    徽标显示前者、复制给出后者，读者会以为复制错了。
    """
    return str(e.get("coef") or e.get("number") or "").strip()


def _bibtex(zh: dict[str, Any], paper: dict[str, Any]) -> str:
    """生成 BibTeX 条目。

    只有 APA 一种格式的话，投期刊或用 Zotero 时还得手动重排。
    BIS 工作论文属于机构报告，用 @techreport。
    """
    series = str(paper.get("series") or zh.get("series") or "")

    # 优先用 APA 引用里的顺序（对着封面核过），接口返回的顺序不可信
    authors = _authors_from_citation(zh.get("citation") or "")
    if not authors:
        authors = [html_unescape(a) for a in (paper.get("authors") or [])]

    first_surname = ""
    if authors:
        first = authors[0]
        first_surname = first.split(",")[0] if "," in first else first.split()[-1]
    # 变音符号先做兼容分解再滤掉非 ASCII：Hördahl -> hordahl 而不是 hrdahl
    folded = unicodedata.normalize("NFKD", first_surname)
    key = f"bis{series}{_ASCII_RE.sub('', folded).lower()}"
    year = (paper.get("pub_date") or "")[:4] or "2026"
    month = (paper.get("pub_date") or "")[5:7].lstrip("0")

    # 值先转义，再决定怎么包裹 —— 顺序反了的话，标题的保护花括号自己会被转义掉
    braced = [
        ("author", _latex_escape(" and ".join(authors))),
        # 标题多包一层花括号，护住 LCR、NBFI 这类专有名词的大小写，
        # 否则 plain.bst 之类的样式会把整个标题转成句首大写
        ("title", "{" + _latex_escape(html_unescape(paper.get("title", ""))) + "}"),
        ("institution", "Bank for International Settlements"),
        ("type", "BIS Working Papers"),
        ("number", series),
        ("year", year),
        # url 里的 ~ _ # 若转义反而打不开，交给 \url 处理，这里保持原样
        ("url", paper.get("pdf_url", "")),
    ]
    lines = [f"  {name:<12}= {{{value}}}" for name, value in braced if value]

    # month 用的是 BibTeX 内建宏（jul），不能加花括号，否则退化成字面量
    if month in _BIBTEX_MONTHS:
        lines.insert(-1, f"  {'month':<12}= {_BIBTEX_MONTHS[month]}")

    return "@techreport{" + key + ",\n" + ",\n".join(lines) + "\n}"


# BibTeX 的标准月份宏（不带引号，故单独处理）
_BIBTEX_MONTHS = {
    "1": "jan", "2": "feb", "3": "mar", "4": "apr", "5": "may", "6": "jun",
    "7": "jul", "8": "aug", "9": "sep", "10": "oct", "11": "nov", "12": "dec",
}

templates.env.filters["emphasis"] = _emphasis
templates.env.filters["zh_text"] = _zh_text
templates.env.filters["evidence_citation"] = _evidence_citation
templates.env.filters["evidence_badge"] = _evidence_badge


def _verify_data_dir() -> None:
    """确认持久化目录真的可用。

    线上显式配了 DATA_DIR（指向 Railway Volume）却发现目录不存在，说明卷没挂上。
    这时必须直接崩溃：若退回容器内的临时目录，状态会在每次重启后静默丢失，
    然后重新播种基线，把这期间发布的论文全部吞掉 —— 而且完全没有报错。
    """
    path = config.DATA_DIR
    if config.STRICT_DATA_DIR and not path.is_dir():
        raise RuntimeError(
            f"DATA_DIR={path} 不存在。若部署在 Railway，请确认已挂载 Volume 到该路径；"
            f"确实想用临时目录请显式设置 STRICT_DATA_DIR=false。"
        )
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"DATA_DIR={path} 不可写：{exc}") from exc
    log.info("数据目录就绪：%s", path.resolve())


def _startup_check() -> None:
    """容器启动后的自检：库为空则播种基线；否则仅在距上次运行超过 24h 时补跑。"""
    try:
        with db.session() as conn:
            empty = db.is_empty(conn)
            last = db.last_run(conn)
        if empty:
            log.info("数据库为空，开始基线播种")
            scheduler.run_locked("startup-baseline")
            return
        if last and last["started_at"]:
            started = datetime.fromisoformat(last["started_at"])
            if started.tzinfo is None:            # 兼容早期写入的无时区时间戳
                started = started.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - started < timedelta(hours=24):
                log.info("距上次检查不足 24 小时，启动时跳过补跑")
                return
        log.info("距上次检查已超过 24 小时，启动时补跑一次")
        scheduler.run_locked("startup-catchup")
    except Exception:
        log.exception("启动自检失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _verify_data_dir()
    with db.session() as conn:
        db.init(conn)
        # 中文深度总结存在仓库里，每次启动重新载入 —— 改了 JSON 推一次即生效
        summaries.init(conn)
        summaries.load_from_disk(conn)
        # 先对账，再决定要不要补跑：上次若在发信窗口里被杀，这里把残局清干净，
        # 免得启动补跑读到过期的 last_run 又把同一封简报发第二遍。
        db.repair_html_entities(conn)
        fixed = db.reconcile_interrupted(conn)
        if fixed["runs"] or fixed["papers"]:
            log.warning(
                "启动对账：%d 次运行标记为 interrupted，%d 篇论文按已送达处理",
                fixed["runs"], fixed["papers"],
            )
    if not config.ADMIN_TOKEN:
        log.warning("未设置 ADMIN_TOKEN，/admin/* 端点已停用")
    if config.SMTP_PASSWORD_NORMALISED:
        log.info(
            "SMTP_PASSWORD 是按 Gmail 分组格式填写的（含空格），已自动去除空格后使用"
        )
    log.info(
        "邮件：%s%s",
        "enabled" if config.EMAIL_ENABLED else "disabled",
        f"（{config.SMTP_HOST}:{config.SMTP_PORT}）" if config.EMAIL_ENABLED else "",
    )
    scheduler.start(on_startup=_startup_check if config.RUN_ON_STARTUP else None)
    log.info("服务已启动 | 数据目录=%s | 时区=%s", config.DATA_DIR, config.TIMEZONE)
    yield
    scheduler.shutdown()


app = FastAPI(title=config.SITE_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None)


# 浏览器里访问不存在的页面时，FastAPI 默认回一个裸 JSON，
# 既没有导航也没有返回入口，是个死胡同。API 路径仍保持 JSON。
@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    wants_json = (
        request.url.path.startswith(("/api/", "/admin/"))
        or request.url.path.endswith((".json", ".md"))
        or "application/json" in request.headers.get("accept", "")
    )
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request,
        "error.html",
        _ctx(request, code=exc.status_code, detail=exc.detail),
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    """参数不合法。

    典型场景：把页面上看到的长标题整段复制进搜索框（q 限 120 字符），
    或手改 URL 把 page 调到 500 以上。回一个能点回去的页面，
    比一段 JSON 报错有用得多。
    """
    if request.url.path.startswith(("/api/", "/admin/")):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    return templates.TemplateResponse(
        request,
        "error.html",
        _ctx(request, code=422, detail="Invalid request parameters."),
        status_code=422,
    )

_static = BASE_DIR / "static"
if _static.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


def get_conn() -> Iterator[Any]:
    conn = db.connect()
    try:
        db.init(conn)
        summaries.init(conn)
        yield conn
    finally:
        conn.close()


def require_admin(
    request: Request,
) -> None:
    """管理端点鉴权。

    两处刻意的设计：
      * **fail closed**：没配 ADMIN_TOKEN 就直接关闭管理端点。之前是没配即放行，
        而 Railway 一生成公网域名，/admin/run 就对整个互联网敞开。
      * 优先读 X-Admin-Token 请求头。放在 query 里的 token 会被 uvicorn 访问日志、
        Railway 日志、浏览器历史和 Referer 头原样记下来。
    """
    if not config.ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints are disabled; set ADMIN_TOKEN.",
        )
    supplied = request.headers.get("x-admin-token")
    if not supplied or not secrets.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="A valid admin token is required.")


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "site_title": config.SITE_TITLE,
        "timezone": config.TIMEZONE,
        "now": render.stamp(),
        **extra,
    }


# ---------------------------------------------------------------- 页面
# 允许 HEAD：Railway 之外的探活工具（UptimeRobot 等）默认用 HEAD 打首页
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(
    request: Request,
    q: str = Query("", max_length=300),
    jel: str = Query("", max_length=120),
    view: str = Query("", pattern="^(scored|zh)?$"),
    sort: str = Query("date", pattern="^(date|relevance)$"),
    page: int = Query(1, ge=1, le=500),
    conn=Depends(get_conn),
) -> Response:
    offset = (page - 1) * PAGE_SIZE
    scored, zh_only = view == "scored", view == "zh"
    rows = db.list_papers(
        conn, query=q, jel=jel, scored=scored, zh_only=zh_only,
        sort=sort, limit=PAGE_SIZE, offset=offset,
    )
    total = db.count_papers(conn, query=q, jel=jel, scored=scored, zh_only=zh_only)
    papers = [db.row_to_dict(r) for r in rows]
    with_zh = summaries.series_with_summary(conn)
    for paper in papers:
        paper["tier"] = scoring.tier(int(paper.get("relevance") or 0))
        paper["has_zh"] = paper["series"] in with_zh
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            nav="papers",
            papers=papers,
            zh_count=len(with_zh),
            total=total,
            page=page,
            pages=max(1, math.ceil(total / PAGE_SIZE)),
            q=q,
            jel=jel,
            view=view,
            sort=sort,
            facets=db.facet_counts(conn),
            stats=db.stats(conn),
            last_run=db.last_run(conn),
            next_run=scheduler.next_run_time(),
            running=scheduler.is_running(),
        ),
    )


@app.get("/paper/{series}", response_class=HTMLResponse)
def paper_detail(request: Request, series: str, conn=Depends(get_conn)) -> Response:
    row = db.get_paper(conn, f"/publ/work{series}")
    if row is None:
        raise HTTPException(status_code=404, detail="Paper not found.")
    paper = db.row_to_dict(row)
    paper["tier"] = scoring.tier(int(paper.get("relevance") or 0))
    zh = summaries.get(conn, series)
    prev_doc, next_doc = summaries.neighbours(conn, series) if zh else (None, None)
    return templates.TemplateResponse(
        request,
        "paper.html",
        _ctx(
            request,
            nav="papers",
            paper=paper,
            sections=render.SECTION_MAP,
            coverage_note=render.COVERAGE_NOTE,
            zh=zh,
            zh_sections=summaries.SECTIONS,
            bibtex=_bibtex(zh, paper) if zh else "",
            prev_doc=prev_doc,
            next_doc=next_doc,
            has_any=any(
                paper.get(k) for k in ("focus", "findings", "contribution", "abstract")
            ),
        ),
    )


@app.get("/thesis", response_class=HTMLResponse)
def thesis(request: Request, conn=Depends(get_conn)) -> Response:
    """按主题归类的中文深度总结索引，供写文献综述时纵览。"""
    items = summaries.list_all(conn)
    by_theme: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        paper = db.get_paper(conn, f"/publ/work{item['series']}")
        item["paper"] = db.row_to_dict(paper) if paper else None
        by_theme.setdefault(item.get("theme") or "Uncategorized", []).append(item)
    return templates.TemplateResponse(
        request,
        "thesis.html",
        _ctx(
            request,
            nav="thesis",
            by_theme=by_theme,
            total=len(items),
            counts=summaries.review_counts(conn),
        ),
    )


@app.get("/thesis/{series}.md", response_class=PlainTextResponse)
def thesis_markdown(series: str, conn=Depends(get_conn)) -> str:
    """单篇中文总结的 Markdown，可直接粘进论文草稿。"""
    zh = summaries.get(conn, series)
    if not zh:
        raise HTTPException(status_code=404, detail="No Chinese research note is available for this paper.")
    row = db.get_paper(conn, f"/publ/work{series}")
    return render.summary_to_markdown(zh, db.row_to_dict(row) if row else None)


@app.get("/thesis.md", response_class=PlainTextResponse)
def thesis_all_markdown(conn=Depends(get_conn)) -> str:
    """全部中文总结打包成一份 Markdown，方便一次性导入写作工具。"""
    parts: list[str] = [
        "# Chinese research notes on BIS Working Papers",
        "",
        f"{summaries.count(conn)} notes · generated {render.stamp()}",
        "",
        "> Core findings and evidence describe the authors’ results; limitations and implications include the note writer’s judgment. Verify all claims against the original papers. These are unofficial notes, not BIS translations.",
        "",
    ]
    for item in summaries.list_all(conn):
        row = db.get_paper(conn, f"/publ/work{item['series']}")
        parts.append(
            render.summary_to_markdown(item, db.row_to_dict(row) if row else None)
        )
        parts.append("\n---\n")
    return "\n".join(parts)


def _mask_email(address: str) -> str:
    """Mask an email address for display."""
    name, _, domain = address.partition("@")
    if not domain:
        return "***"
    head = name[:1]
    tail = name[-1:] if len(name) > 2 else ""
    return f"{head}***{tail}@{domain}"


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, conn=Depends(get_conn)) -> Response:
    rows = []
    for row in db.recent_runs(conn, 50):
        item = dict(row)
        # 原始 traceback 可能带着配置细节，公开页面只保留一句话
        if item.get("error"):
            item["error"] = "An error occurred; check private server logs."
        rows.append(item)
    return templates.TemplateResponse(
        request,
        "runs.html",
        _ctx(
            request,
            nav="runs",
            runs=rows,
            next_run=scheduler.next_run_time(),
            email_enabled=config.EMAIL_ENABLED,
            external_mailer=config.EXTERNAL_MAILER,
            mail_to=[_mask_email(a) for a in config.MAIL_TO],
            cron=f"{config.CRON_DAY_OF_WEEK} {config.CRON_HOUR:02d}:"
                 f"{config.CRON_MINUTE:02d} {config.TIMEZONE}",
        ),
    )


# ---------------------------------------------------------------- 数据接口
_FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='12' fill='#0b5fa5'/>"
    "<text x='32' y='44' font-size='34' font-family='Helvetica,Arial' "
    "font-weight='bold' fill='#fff' text-anchor='middle'>B</text></svg>"
)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(content=_FAVICON, media_type="image/svg+xml")


@app.api_route("/healthz", methods=["GET", "HEAD"], response_class=JSONResponse)
def healthz(conn=Depends(get_conn)) -> dict[str, Any]:
    return {
        "status": "ok",
        "papers": db.stats(conn)["total"],
        "next_run": scheduler.next_run_time(),
        "email_enabled": config.EMAIL_ENABLED,
    }


@app.get("/api/papers.json", response_class=JSONResponse)
def api_papers(
    q: str = Query("", max_length=300),
    jel: str = Query("", max_length=120),
    limit: int = Query(50, ge=1, le=500),
    # 必须限上界：超过 SQLite 的 64 位整数范围时 sqlite3 抛 OverflowError，
    # 返回 500 而不是 422。取 100 万足够——全库也才 1369 篇。
    offset: int = Query(0, ge=0, le=1_000_000),
    conn=Depends(get_conn),
) -> dict[str, Any]:
    rows = db.list_papers(conn, query=q, jel=jel, limit=limit, offset=offset)
    return {
        "total": db.count_papers(conn, query=q, jel=jel),
        "items": [db.row_to_dict(r) for r in rows],
    }


@app.get("/digest/latest.md", response_class=PlainTextResponse)
def latest_digest_md(conn=Depends(get_conn)) -> str:
    """最近一期简报的 Markdown（可直接 curl 下来 commit 进仓库）。"""
    last = db.last_run(conn)
    if not last:
        return "No checks have run yet."
    import json as _json

    paths = _json.loads(last["new_paths"] or "[]")
    papers = [db.row_to_dict(r) for p in paths if (r := db.get_paper(conn, p))]
    papers.sort(key=lambda x: -int(x.get("relevance") or 0))
    return render.to_markdown(papers)


# ---------------------------------------------------------------- 管理端点
@app.get("/admin", response_class=HTMLResponse)
def admin_console(request: Request) -> Response:
    """控制台页面本身不含任何数据，所有操作都要带 token 才会执行。"""
    return templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            external_mailer=config.EXTERNAL_MAILER,
            actions_url=(
                (f"https://github.com/{config.GITHUB_REPO}"
                 f"/actions/workflows/{config.WORKFLOW_FILE}" if config.GITHUB_REPO else "")
            ),
        ),
    )


# 有副作用的端点一律只收 POST。用 GET 的话，聊天软件的链接预览、浏览器预取
# 或搜索引擎爬虫访问一次带 token 的链接，就会真的跑一次检查或真的发一封邮件。
@app.post("/admin/run", response_class=JSONResponse)
def admin_run(
    send_email: bool = Query(True),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """手动触发一次检查。send_email=false 可做不发信的演练。"""
    result = scheduler.run_locked("manual", send_email=send_email)
    return {
        "status": result.status,
        "message": result.message,
        "is_baseline": result.is_baseline,
        "listed_count": result.listed_count,
        "new_count": result.new_count,
        "new_paths": result.new_paths,
        "email_status": result.email_status,
        "delivered": result.delivered,
        "export_path": result.export_path,
        "error": result.error,
    }


@app.post("/admin/backfill", response_class=JSONResponse)
def admin_backfill(
    limit: int = Query(30, ge=1, le=300),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """为尚无摘要的历史论文补抓详情。"""
    from . import pipeline

    # 与定时检查共用同一把锁，避免两边同时对 BIS 发请求
    if scheduler.is_running():
        raise HTTPException(status_code=409, detail="A check is already running; try again later.")
    with db.session() as conn:
        done = pipeline.backfill_details(conn, limit)
        remaining = len(db.needs_detail(conn, 10_000))
    return {"filled": done, "remaining": remaining}


# ---------------------------------------------------------------- 外部代发
# Railway 封禁了全部出站 SMTP 端口（25/465/587/2525 全超时，443 正常），
# 所以发信改由 GitHub Actions 代劳。分成两步是为了保住"发失败就不打标记"：
#   1) prepare-digest —— 跑检查、开一条 external 投递记录、返回邮件内容
#   2) confirm-delivery —— 代发方确认发出后才标记为已报告
# 若第 2 步没来，启动对账会把这条 external 记录判为失败，下次重试。
@app.post("/admin/prepare-digest", response_class=JSONResponse)
def admin_prepare_digest(_: None = Depends(require_admin)) -> dict[str, Any]:
    result = scheduler.run_locked("external-relay", send_email=False)
    if result.status == "error":
        raise HTTPException(status_code=500, detail=result.error)
    if result.status == "busy":
        raise HTTPException(status_code=409, detail="A check is already running.")

    with db.session() as conn:
        rows = db.unreported(conn)
        if not rows:
            return {"count": 0, "message": result.message}

        papers = [db.row_to_dict(r) for r in rows]
        paths = [p["path"] for p in papers]
        delivery_id = db.open_delivery(
            conn, run_id=0, paths=paths, channel="external"
        )

    return {
        "delivery_id": delivery_id,
        "count": len(papers),
        "paths": paths,
        "subject": f"{config.MAIL_SUBJECT_PREFIX} {render.digest_title(len(papers))}".strip(),
        "recipients": config.MAIL_TO,
        "html": render.to_html(papers),
        "markdown": render.to_markdown(papers),
    }


@app.post("/admin/preview-digest", response_class=JSONResponse)
def admin_preview_digest(
    limit: int = Query(3, ge=1, le=10),
    _: None = Depends(require_admin),
    conn=Depends(get_conn),
) -> dict[str, Any]:
    """用最近几篇论文渲染一封"样张"，**不改动任何状态**。

    用途：在真的有新论文之前，先看看周一那封邮件长什么样、能不能收到。
    这些论文早已被标记为已报告，这里只是借它们的内容排版，
    不会开投递记录，也不会影响去重。
    """
    rows = db.list_papers(conn, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail="No papers are indexed yet.")
    papers = [db.row_to_dict(r) for r in rows]
    papers.sort(key=lambda p: -int(p.get("relevance") or 0))

    # 样张的标题不能沿用「发现 N 篇新论文」——那会让人误以为真有新论文
    subject = f"{config.MAIL_SUBJECT_PREFIX} [Preview] Delivery test ({len(papers)} recent papers)".strip()
    banner = (
        "<div style='background:#fff8e6;border-left:3px solid #f0b429;"
        "padding:12px 14px;margin:0 0 16px;font-size:13px;color:#5c4813'>"
        "<b>This is a delivery preview, not a new-paper digest.</b><br>"
        "It uses recent papers and does not change reported status.</div>"
    )
    html = render.to_html(papers).replace("<div class='wrap'>", "<div class='wrap'>" + banner, 1)

    return {
        "preview": True,
        "count": len(papers),
        "subject": subject,
        "recipients": config.MAIL_TO,
        "html": html,
        "markdown": render.to_markdown(papers),
    }


@app.post("/admin/confirm-delivery", response_class=JSONResponse)
def admin_confirm_delivery(
    delivery_id: int = Query(..., ge=1),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """代发方确认邮件已发出后调用，此时才把论文标记为已报告。"""
    with db.session() as conn:
        row = db.get_delivery(conn, delivery_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        if row["status"] != "sending":
            return {"ok": True, "already": row["status"], "marked": 0}

        import json as _json

        paths = _json.loads(row["paths"] or "[]")
        db.mark_reported(conn, paths)
        db.settle_delivery(conn, delivery_id, "sent", "由外部代发确认送达")
        conn.commit()
    log.info("外部代发确认送达，已标记 %d 篇为已报告", len(paths))
    return {"ok": True, "marked": len(paths), "paths": paths}


@app.post("/admin/check-smtp", response_class=JSONResponse)
def admin_check_smtp(_: None = Depends(require_admin)) -> dict[str, Any]:
    """只验证 SMTP 能否登录，不发送任何邮件（收件人不会被打扰）。"""
    ok, detail = mailer.check_login()
    payload: dict[str, Any] = {
        "ok": ok,
        "result": detail,
        "recipients_would_be": config.MAIL_TO,
    }
    if not ok:
        # 连不上时顺便探一下出站端口，区分"密码错"与"端口被封"
        payload["egress_probe"] = mailer.probe_egress()
    return payload


@app.post("/admin/test-email", response_class=JSONResponse)
def admin_test_email(_: None = Depends(require_admin)) -> dict[str, Any]:
    """真的发一封测试邮件给 MAIL_TO 里的所有人。想先确认凭据用 /admin/check-smtp。"""
    ok, detail = mailer.send_test()
    return {"ok": ok, "result": detail, "sent_to": config.MAIL_TO if ok else []}


@app.get("/admin/export.zip")
def admin_export(_: None = Depends(require_admin)) -> Response:
    return Response(
        content=exporter.archive_zip(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="bis-archive.zip"'
        },
    )
