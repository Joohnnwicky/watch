# 军事新闻 RSS 阅读器 — 实施计划

基于 [investment-news](https://github.com/simonlin1212/investment-news) 项目，复用其四段式流水线（fetch → digest → index，server.py 串联，纯 Python 标准库 + 一个大模型），把「12 行业扁平分类」改为「国家 → 军种/类别」三级结构，AI「今日要点」按**国家**出。

## 已确认的设计决策
- **摘要粒度**：按国家出今日要点（每国一份，聚合该国全部军种/智库/报刊）；分支层只做双语新闻列表。
- **首批国家**：美国、中国、俄罗斯、英国、法国、日本、印度、以色列（8 国）。
- **过滤策略**：保留 `redline_keywords` 机制，初始词表留空，由用户自填。
- **LLM 引擎**：默认 `claude-cli`（已验证 `claude` 在 PATH，$0）。

## 0. 总体策略
- **复用为主**：`llm.py`（双引擎）、`server.py`、`fetch.py` 抓取内核、`digest.py` 的「要点+翻译+JSON 回写」机制、`index.html` 的双语列表/刷新逻辑——原样或小改复用。
- **改的是数据模型**：2 级扁平（industry → source）→ 3 级嵌套（country → branch → source）。
- **分两阶段**：本计划交付完整架构 + 一个已 liveness 验证的种子源集（美国为主 + 每国 1–2 个），跑通端到端；源清单的完整 8 国扩充作为后续阶段，用 `build_sources.py` 滚动验证。

## 1. Bootstrap（基础骨架）
1. `git clone https://github.com/simonlin1212/investment-news.git .ref`（参考，不改原仓库）
2. 拷贝到 `J:\RSS` 根：
   - 原样：`scripts/llm.py`、`server.py`、`.gitignore`、`scripts/build_sources.py`
   - 改写：`sources.json`、`scripts/fetch.py`、`scripts/digest.py`、`index.html`
   - 保留：`llm.config.json`（默认 claude-cli）
3. 可选删除 `.ref`（保留作参考亦可）
4. → verify：目录结构成立，`python -c "import scripts.fetch"` 无报错

## 2. 数据模型（sources.json）
```json
{
  "_comment": "军事新闻策展源 - 8 国 / 9 类",
  "fetch": {"per_source": 6, "timeout": 20, "recent_days": 7},
  "countries": [
    {"key":"us","name":"美国","accent":"#2563eb"},
    {"key":"cn","name":"中国","accent":"#dc2626"},
    {"key":"ru","name":"俄罗斯","accent":"#7c3aed"},
    {"key":"uk","name":"英国","accent":"#0e7490"},
    {"key":"fr","name":"法国","accent":"#0369a1"},
    {"key":"jp","name":"日本","accent":"#db2777"},
    {"key":"in","name":"印度","accent":"#ea580c"},
    {"key":"il","name":"以色列","accent":"#4f46e5"}
  ],
  "branches": [
    {"key":"army","name":"陆军"},{"key":"navy","name":"海军"},
    {"key":"airforce","name":"空军"},{"key":"space","name":"太空军"},
    {"key":"marines","name":"海军陆战队"},{"key":"guard","name":"国民警卫队"},
    {"key":"gov","name":"政府机构"},{"key":"thinktank","name":"智库"},
    {"key":"press","name":"报刊"}
  ],
  "sources": [
    {"name":"U.S. Army","country":"us","branch":"army","type":"rss","url":"..."}
  ],
  "redline_keywords": []
}
```
- 源条目：原 `hint` 单字段 → 拆成 `country` + `branch` 两字段。
- `redline_keywords`：初始空数组（机制保留，词表自填）。
- → verify：`python -c "import json;json.load(open('sources.json'))"` 合法

## 3. fetch.py 改动
- 读取 `countries` + `branches` + `sources`（替代原 `industries`）。
- `fetch(source)` 函数体不变（urllib 抓 RSS/Atom → ElementTree 解析 → 截 PER 条 → CUTOFF/REDLINE 过滤）。
- `main()` 分组逻辑改写：按 `source.country` + `source.branch` 把 items 归入 `countries[c].branches[b].items`；只创建有源或有条目的 country/branch 节点。
- data.js 输出结构：
```js
window.DATA = {
  generated_at, recent_days,
  countries: [{ key, name, accent,
    branches: [{ key, name, total, items:[{title,url,time,ts,summary,source,zh}] }]
  }],
  stats: { countries, branches_active, total_sources }
}
```
- 并发（40 worker）、CUTOFF（recent_days）、REDLINE、北京时间归一：不变。
- → verify：`python scripts/fetch.py` 产出 data.js 嵌套结构；`window.DATA.countries[0].branches[0].items` 非空

## 4. digest.py 改动（按国家出要点）
- SYS 提示词改为「军事新闻分析助手」，任务不变：提取 3–5 条今日要点（≤40 字，`refs` 指向条目序号）+ 逐条标题译中文，纯 JSON 输出。
- `process(country)`：收集该国**所有分支**的 items（跨分支合并、按 ts 排序、截 TOPN≈24），构建编号清单，调 LLM。
- 结果：`country.points` = 要点数组；`zh` 翻译回填到各分支 items。
- 跳过已处理、3 次重试（`ATTEMPTS`）、`extract_json` 不变；`ThreadPoolExecutor`（8 worker 并行 8 国）。
- `data["has_ai"]=True`，回写 data.js。
- → verify：digest 后某 `country.points` 非空；部分 `item.zh` 有值

## 5. index.html 改动（两级导航）
- 侧栏：可折叠两级。国家分组（色点 + 名 + 计数）→ 展开显示 9 个分支（名 + 计数）。默认首个国家展开。
- 活动态：追踪 `activeCountry` + `activeBranch`（或合并 key）。
- 头部：面包屑「国家 › 分支」。
- 今日要点卡片：展示**当前国家**的 points（点国家或其任一分支时，该国要点置顶）。
- 文章列表：按 `activeBranch` 过滤 items。
- 配色：分支条目用所属国家 accent（国家共享色，不再每分支一色）。
- 标签：`赛道 / Sectors` → `国家 / Countries`。
- 底部统计：国家数、分支数、源数。
- 双语列表、刷新按钮、`safeUrl`、缓存戳：逻辑不变。
- → verify：浏览器开 `http://localhost:8793`，侧栏两级折叠可点，切国家/分支时文章列表与要点卡片正确刷新

## 6. server.py / llm.config.json / build_sources.py
- `server.py`：原样复用（可选改注释 investment→military，端口 8793 不变）。
- `llm.config.json`：默认 claude-cli。
- `build_sources.py`：适配新 schema（`country`+`branch` 替代 `hint`）做逐源 liveness 实测。
- → verify：`python scripts/build_sources.py` 报告各源存活情况

## 7. 种子源集（执行阶段逐个 liveness 实测）
首批填入 sources.json，执行时用 `build_sources.py` 实测存活，死链替换：
- **美国（最全）**：army.mil / navy.mil / af.mil / spaceforce.mil / marines.mil / nationalguard.mil / defense.gov（gov）；RAND / CSIS / Brookings / War on the Rocks（thinktank）；Defense News / Breaking Defense / Defense One（press）
- **中国**：中国军网 81.cn、国防部 mod.gov.cn（gov，可能无 RSS）；环球军事 mil.huanqiu.com、新华网军事（press）
- **俄罗斯**：TASS、ISW（thinktank）、Sputnik（press）
- **英国**：gov.uk MOD、royalnavy/army/raf.mod.uk（gov）；RUSI / IISS（thinktank）；Janes / UK Defence Journal（press）
- **法国**：defense.gouv.fr（gov）；IFRI / IRSEM（thinktank）；Opex360（press）
- **日本**：防衛省 mod.go.jp、防衛研究所 nids.mod.go.jp（thinktank）
- **印度**：mod.gov.in（gov）；MP-IDSA（thinktank）；Livefist（press）
- **以色列**：idf.il（gov）；INSS（thinktank）；Israel Defense（press）
- **无 RSS 的高价值源**：对确认无 RSS 的关键官方源（如中国国防部），评估加一个轻量 HTML 抓取适配器到 fetch.py（可选增强，不阻塞主流程）。

## 8. 端到端验证（每步 verify）
1. Bootstrap → 目录就绪，脚本可 import
2. sources.json → JSON 合法
3. fetch.py → data.js 嵌套结构非空
4. digest.py → country.points 生成
5. server.py + 浏览器 → 两级导航 + 要点 + 双语列表 + 一键刷新全通

## 9. 不做的事（范围控制）
- 不改 `llm.py` 双引擎机制
- 不加数据库 / 托管 / RSSHub
- 不预置 redline 词表（留空，用户自填）
- 不在本计划内做完整 8 国全源扩充（留作后续阶段，`build_sources.py` 滚动验证）
