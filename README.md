# Memoh Agent 工程学习笔记

基于 Memoh 源码整理的 Agent 平台学习文档，覆盖 Agent 内核、工具系统、长期记忆、对话编排、容器工作空间、MCP、调度、桌面端与可观测性。

## 网站能力

- 15 篇 Markdown 笔记的本地全文搜索
- 浅色/深色主题切换并持久化偏好
- 自动生成阅读时长、更新时间和 GitHub 编辑入口
- 桌面端三栏阅读布局与移动端折叠菜单

## 本地预览

需要 Python 3 和 Pandoc：

```bash
python3 build_site.py
python3 -m http.server 4173 --directory _site
```

打开 <http://127.0.0.1:4173/>。

## 目录结构

```text
content/       Markdown 源文档
site_assets/   网站样式与交互
build_site.py  静态站点生成器
_site/         构建产物
```

推送到 `main` 后，GitHub Actions 会自动构建并部署 GitHub Pages。
