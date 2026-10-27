"""优先级排序。

需求里要求"多篇新论文时给出汇总表, 按最值得阅读排序, 并说明排序理由"。
在不调用大模型的前提下, 用一套**可解释 / 可调**的加权规则来排：

  1. JEL 分类命中（config.JEL_WEIGHTS）——最强信号, 学科归属由 BIS 自己标注
  2. 标题 / 关键词 / 摘要中的主题词命中（config.KEYWORD_WEIGHTS）
  3. 结构完整度加分（有 Findings / 有 Abstract 的论文更好读）
  4. 篇幅惩罚（超长论文投入产出比低一点）

权重是按固收 / 外汇 / 货币政策方向的读者设定的, 改 config.py 即可调整。
每篇都会同时产出一句人话的"为什么排这个位置"。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .bis import PaperDetail


# JEL 加分的上限：最多计 3 个二级大类, 且总额不超过 30 分。
# 目的是让"Topics: + 结构完整度"仍有话语权, 而不是谁 JEL 标得多谁排前面。
MAX_JEL_FAMILIES = 3
MAX_JEL_POINTS = 30


@dataclass
class Score:
    total: int
    why: str


def _jel_prefixes(code: str) -> list[str]:
    """'F34 - International Lending' -> ['F34', 'F3']"""
    token = code.strip().split()[0].split("-")[0].strip().upper() if code.strip() else ""
    if not token:
        return []
    out = [token]
    if len(token) >= 2:
        out.append(token[:2])
    return out


def score_paper(title: str, detail: PaperDetail) -> Score:
    points = 0
    jel_hits: list[str] = []
    kw_hits: list[str] = []

    # 1) JEL 分类
    #    同一二级大类（E4 / F3…）只取最高分, 避免 E43/E44/E58 这种同族标签叠加刷分；
    #    再只保留分数最高的 3 个大类并设总额上限, 防止"JEL 堆料"压倒主题相关性。
    best_by_family: dict[str, tuple[int, str]] = {}
    for code in detail.jel_codes:
        for prefix in _jel_prefixes(code):
            weight = config.JEL_WEIGHTS.get(prefix)
            if not weight:
                continue
            family = prefix[:2]
            if weight > best_by_family.get(family, (0, ""))[0]:
                best_by_family[family] = (weight, prefix)

    top_families = sorted(best_by_family.values(), reverse=True)[:MAX_JEL_FAMILIES]
    jel_points = min(sum(w for w, _ in top_families), MAX_JEL_POINTS)
    points += jel_points
    jel_hits = [prefix for _, prefix in top_families]

    # 2) 主题词：标题权重加倍, 关键词字段其次, 摘要正文只算一次
    haystacks = [
        (title.lower(), 2.0),
        (" ".join(detail.keywords).lower(), 1.5),
        (f"{detail.focus} {detail.abstract}".lower(), 1.0),
    ]
    seen_kw: set[str] = set()
    for term, weight in sorted(config.KEYWORD_WEIGHTS.items(), key=lambda x: -x[1]):
        for text, multiplier in haystacks:
            if term in text:
                if term not in seen_kw:
                    points += int(weight * multiplier)
                    seen_kw.add(term)
                    kw_hits.append(term)
                break

    # 3) 结构完整度
    structure_bonus = 0
    if detail.findings:
        structure_bonus += 6
    if detail.contribution:
        structure_bonus += 3
    if detail.abstract:
        structure_bonus += 3
    points += structure_bonus

    # 4) 篇幅惩罚
    length_note = ""
    pages = detail.pdf_pages or 0
    if pages > 80:
        points -= 6
        length_note = f"; long ({pages} pages)"
    elif pages and pages <= 40:
        points += 3
        length_note = f"; concise ({pages} pages)"

    # ---- 生成理由 ----
    reasons: list[str] = []
    if jel_hits:
        reasons.append("JEL matches: " + "/".join(sorted(jel_hits)))
    if kw_hits:
        reasons.append("Topics: " + " / ".join(kw_hits[:4]))
    if not reasons:
        reasons.append("Limited fixed income / FX relevance")
    if structure_bonus >= 12:
        reasons.append("Complete four-part abstract")
    why = ", ".join(reasons) + length_note

    return Score(total=max(points, 0), why=why)


def tier(score: int) -> str:
    """把分数映射成三档优先级标签, 用于汇总表。"""
    if score >= 45:
        return "High"
    if score >= 22:
        return "Medium"
    return "Low"
