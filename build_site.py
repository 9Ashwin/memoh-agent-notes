#!/usr/bin/env python3
"""Build the Memoh study notes as a standalone local documentation site."""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
OUTPUT = ROOT / "_site"

DOCS = [
    ("00_学习计划.md", "学习计划", "从全局地图开始，规划三条循序渐进的学习路线。", "导读"),
    ("01_agent_内核.md", "Agent 内核", "Stream、事件流、Prompt、循环检测与重试。", "P0"),
    ("02_工具系统.md", "工具系统", "按会话装配工具，让工具用法住在工具自身。", "P0"),
    ("03_长期记忆.md", "长期记忆", "向量、稀疏检索和 LLM 抽取组成的多 Provider 架构。", "P0"),
    ("04_对话流编排.md", "对话流编排", "理解 flow resolver 与 pipeline 两条编排路径。", "P0"),
    ("05_prompt工程与模式切换.md", "Prompt 工程", "公共底座、五种运行模式与动态模板组装。", "P1"),
    ("06_上下文压缩.md", "上下文压缩", "异步与同步压缩，以及摘要替换原始历史的机制。", "P1"),
    ("07_容器工作空间.md", "容器与工作空间", "gRPC over UDS、运行时抽象和资源配额。", "P1"),
    ("08_多渠道适配.md", "多渠道适配", "统一渠道抽象、身份绑定与来源感知 ACL。", "P1"),
    ("09_mcp集成.md", "MCP 集成", "连接、OAuth、工具联邦和长生命周期会话。", "P1"),
    ("10_调度与自动化.md", "调度与自动化", "Heartbeat 与 Schedule 两套时间驱动模型。", "P2"),
    ("11_acp插件用户输入.md", "ACP、插件与用户输入", "外部 Agent 池、插件生命周期和阻塞式交互。", "P2"),
    ("12_数据库双后端.md", "数据库双后端", "PostgreSQL 与 SQLite 双轨迁移的工程纪律。", "P2"),
    ("13_桌面端.md", "桌面端", "Electron 如何管理本地服务、Qdrant 与打包资源。", "P2"),
    ("17_可观测性.md", "可观测性", "日志、健康检查、资源指标与 Hook 事件流。", "P2"),
]


def page_name(filename: str) -> str:
    return f"{Path(filename).stem}.html"


def render_markdown(source: str) -> tuple[str, list[tuple[int, str, str]]]:
    source = re.sub(r"^#\s+.*?\n", "", source, count=1)
    source = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\.md(#[^)]+)?\)",
        lambda match: f"[{match.group(1)}]({page_name(match.group(2) + '.md')}{match.group(3) or ''})",
        source,
    )
    body = subprocess.run(
        ["pandoc", "--from", "gfm", "--to", "html5", "--wrap=none"],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    headings: list[tuple[int, str, str]] = []
    for level, attrs, label in re.findall(r"<h([23])([^>]*)>(.*?)</h\1>", body, re.DOTALL):
        id_match = re.search(r'id="([^"]+)"', attrs)
        if id_match:
            headings.append((int(level), id_match.group(1), re.sub(r"<[^>]+>", "", label)))
    return body, headings


def chapter_nav(active: str) -> str:
    groups = [("导读", "开始这里"), ("P0", "内核与核心设计"), ("P1", "平台能力"), ("P2", "工程与交付")]
    sections = []
    for key, label in groups:
        links = []
        for filename, title, _, group in DOCS:
            if group != key:
                continue
            current = " is-current" if filename == active else ""
            links.append(f'<a class="chapter-link{current}" href="{page_name(filename)}">{html.escape(title)}</a>')
        sections.append(f'<section><div class="nav-label">{label}</div>{"".join(links)}</section>')
    return "".join(sections)


def toc_nav(headings: list[tuple[int, str, str]]) -> str:
    return "".join(
        f'<a class="toc-level-{level}" href="#{anchor}">{html.escape(label)}</a>'
        for level, anchor, label in headings
    )


def shell(title: str, body: str, *, page_class: str, description: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{html.escape(description)}">
  <title>{html.escape(title)} · Memoh Agent 学习笔记</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&family=Noto+Serif+SC:wght@500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="assets/style.css">
</head>
<body class="{page_class}">
{body}
<script src="assets/site.js"></script>
</body>
</html>
"""


def build_home() -> None:
    groups = [("P0", "内核与核心设计"), ("P1", "平台能力"), ("P2", "工程与交付")]
    sections = []
    for group, subtitle in groups:
        cards = []
        for filename, title, description, item_group in DOCS:
            if item_group != group:
                continue
            number = Path(filename).stem.split("_", 1)[0]
            cards.append(
                f'<a class="chapter-card" href="{page_name(filename)}">'
                f'<span class="chapter-number">{number}</span><span class="chapter-copy">'
                f'<strong>{html.escape(title)}</strong><small>{html.escape(description)}</small></span>'
                '<span class="chapter-arrow" aria-hidden="true">→</span></a>'
            )
        sections.append(
            f'<section class="volume"><header><span>{group}</span><h2>{subtitle}</h2></header>{"".join(cards)}</section>'
        )
    body = f"""
<header class="hero">
  <div class="hero-art" aria-hidden="true"><span></span><span></span><span></span></div>
  <div class="hero-content">
    <div class="hero-badge">Architecture Study Notes · 2026</div>
    <h1>Memoh Agent<br><em>工程学习</em></h1>
    <p class="hero-subtitle">从一次对话出发，穿过工具、记忆、容器与平台基础设施</p>
    <p class="hero-meta">15 篇源码学习笔记 · 3 条阅读路线</p>
    <a class="hero-cta" href="{page_name('00_学习计划.md')}">开始阅读 <span>→</span></a>
  </div>
  <a class="scroll-hint" href="#contents"><span>目录</span><i></i></a>
</header>
<main id="contents" class="contents">
  <header class="contents-heading"><span>Contents</span><h2><span>建立一张完整的</span><br><span>Agent 平台地图</span></h2><p>先理解运行内核，再向外扩展到平台能力与工程基础设施。</p></header>
  {''.join(sections)}
</main>
<footer class="site-footer"><span>Memoh Agent 工程学习</span><span>由本地 Markdown 构建</span></footer>
"""
    (OUTPUT / "index.html").write_text(shell("首页", body, page_class="home", description="Memoh Agent 平台源码学习笔记"), encoding="utf-8")


def build_articles() -> None:
    for index, (filename, title, description, group) in enumerate(DOCS):
        source = (CONTENT / filename).read_text(encoding="utf-8")
        article, headings = render_markdown(source)
        previous = DOCS[index - 1] if index else None
        following = DOCS[index + 1] if index + 1 < len(DOCS) else None
        prev_link = (
            f'<a class="pager-prev" href="{page_name(previous[0])}"><span>上一篇</span>{html.escape(previous[1])}</a>'
            if previous else "<span></span>"
        )
        next_link = (
            f'<a class="pager-next" href="{page_name(following[0])}"><span>下一篇</span>{html.escape(following[1])}</a>'
            if following else '<a class="pager-next" href="index.html"><span>阅读完成</span>返回目录</a>'
        )
        number = Path(filename).stem.split("_", 1)[0]
        body = f"""
<button class="mobile-menu" aria-label="打开章节导航" aria-expanded="false">目录</button>
<div class="reading-progress" aria-hidden="true"></div>
<div class="page-layout">
  <aside class="chapter-sidebar">
    <a class="brand" href="index.html"><span class="brand-mark">M</span><span>Memoh Agent<small>工程学习笔记</small></span></a>
    <nav class="chapter-nav">{chapter_nav(filename)}</nav>
  </aside>
  <main class="article-main">
    <article class="article">
      <header class="article-header"><div class="eyebrow">{group} · Chapter {number}</div><h1>{html.escape(title)}</h1><p>{html.escape(description)}</p></header>
      <div class="article-body">{article}</div>
      <nav class="pager">{prev_link}{next_link}</nav>
    </article>
  </main>
  <aside class="toc-sidebar"><div class="toc-title">本页目录</div><nav>{toc_nav(headings)}</nav><a class="back-home" href="index.html">← 返回全书目录</a></aside>
</div>
"""
        output = shell(title, body, page_class="article-page", description=description)
        (OUTPUT / page_name(filename)).write_text(output, encoding="utf-8")


def build() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    (OUTPUT / "assets").mkdir(parents=True)
    shutil.copy2(ROOT / "site_assets" / "style.css", OUTPUT / "assets" / "style.css")
    shutil.copy2(ROOT / "site_assets" / "site.js", OUTPUT / "assets" / "site.js")
    build_home()
    build_articles()
    print(f"Built {len(DOCS) + 1} pages in {OUTPUT}")


if __name__ == "__main__":
    build()
