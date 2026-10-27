"""Markdown 归档。

每次发现新论文就写两类文件到 EXPORT_DIR（Railway Volume 上，重启不丢）：
  digests/YYYY-MM-DD-HHMM.md   —— 该期完整简报
  papers/workNNNN.md           —— 单篇论文的常驻档案
另外维护一个 README.md 作为归档索引。
可从 /admin/export.zip 一次性打包下载，再 commit 进 GitHub 仓库。
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any, Sequence

from . import config, render

log = logging.getLogger(__name__)


def _root() -> Path:
    root = Path(config.EXPORT_DIR)
    (root / "digests").mkdir(parents=True, exist_ok=True)
    (root / "papers").mkdir(parents=True, exist_ok=True)
    return root


def write_digest(papers: Sequence[dict[str, Any]]) -> Path:
    root = _root()
    name = render.now_local().strftime("%Y-%m-%d-%H%M") + ".md"
    target = root / "digests" / name
    target.write_text(render.to_markdown(papers), encoding="utf-8")

    for paper in papers:
        write_paper(paper)

    _write_index(root)
    log.info("已写入归档 %s（%d 篇）", target, len(papers))
    return target


def write_paper(paper: dict[str, Any]) -> Path:
    root = _root()
    target = root / "papers" / f"work{paper['series']}.md"
    target.write_text(render.to_markdown([paper]), encoding="utf-8")
    return target


def _write_index(root: Path) -> None:
    digests = sorted((root / "digests").glob("*.md"), reverse=True)
    papers = sorted((root / "papers").glob("*.md"), reverse=True)
    lines = [
        "# BIS Working Papers 归档",
        "",
        f"最后更新：{render.stamp()}",
        "",
        f"## 历期简报（{len(digests)}）",
        "",
    ]
    lines += [f"- [{p.stem}](digests/{p.name})" for p in digests[:200]]
    lines += ["", f"## 单篇档案（{len(papers)}）", ""]
    lines += [f"- [{p.stem}](papers/{p.name})" for p in papers[:500]]
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def archive_zip() -> bytes:
    """把整个归档目录打包成 zip，供浏览器下载。"""
    root = _root()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*.md")):
            zf.write(path, arcname=str(path.relative_to(root)))
    return buffer.getvalue()
