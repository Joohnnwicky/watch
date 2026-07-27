# 哨兵 Watch

本地运行的军事新闻聚合阅读器。抓取多国军方/防务媒体的 RSS,用大模型生成「今日要点」摘要并翻译标题,呈现为按「国家 → 军种」两级折叠的看板。

- **9 国**:美国 / 中国 / 俄罗斯 / 英国 / 法国 / 日本 / 印度 / 以色列 / 乌克兰
- **9 类**:陆军 / 海军 / 空军 / 太空军 / 海军陆战队 / 国民警卫队 / 政府机构 / 智库 / 报刊
- **四段流水线**:`fetch.py` 抓取 → `digest.py` AI 要点+翻译 → `index.html` 看板 → `server.py` 本地服务
- **AI 增量**:要点一次生成不重算;翻译只补近 3 天未译条目,已译跳过(省 token)
- **源管理面板**:侧栏 ⚙ 齿轮,贴官网 URL 自动发现 RSS(三级发现 + LLM 兜底分类),可增删源
- **已读标记**:点过的链接变暗 + 删除线,localStorage 持久化

## 截图功能

- 两级折叠侧栏(国家 → 军种),面包屑导航
- AI 今日要点卡(按国家,每国 3-5 条,带跳转原文 ↗)
- 中文标题翻译(原文标题作副行小字)
- 源管理弹窗(增删 RSS 源)
- 刷新按钮一键重新抓取 + 生成要点

## 快速开始

### 1. 依赖
- Python 3.8+(纯标准库,无需 `pip install`)
- 一个大模型供要点+翻译,二选一:
  - **Claude Code 订阅**(默认,免费):本机装好 Claude Code 并 `claude login`
  - **DeepSeek 等 OpenAI 兼容 API**:填 key(见下)

### 2. 配置大模型
编辑 `llm.config.json`:
```json
{
  "provider": "api",          // 或 "claude-cli" 用本机订阅
  "api": {
    "base_url": "https://api.deepseek.com",
    "api_key": "",            // 不要填真实 key 进 git,用环境变量
    "api_key_env": "LLM_API_KEY",
    "model": "deepseek-chat"
  }
}
```
API 模式走环境变量(避免 key 进 git):
```powershell
# PowerShell(用户级,重开终端生效)
[Environment]::SetEnvironmentVariable("LLM_API_KEY","sk-...","User")
```
```bash
# bash
export LLM_API_KEY=sk-...
```

### 3. 跑起来
```bash
# Windows:必须设 UTF-8,避免 GBK 控制台因 emoji 报错
$env:PYTHONUTF8=1
python scripts/fetch.py       # 抓取 RSS -> data.js
python scripts/digest.py      # AI 要点 + 翻译 -> 写回 data.js
python server.py 8793         # 启动看板
```
浏览器打开 <http://localhost:8793/index.html>

日常刷新:看板右上角 ⟳ 按钮(触发 fetch+digest),或重新跑上面三步。

## 目录结构

```
.
├── sources.json        # 源配置:国家/分支/源列表(可用面板增删)
├── scripts/
│   ├── fetch.py        # 抓取:三级嵌套分组,近 N 天过滤,北京时间
│   ├── digest.py       # AI 要点(无则生成)+ 近3天增量翻译
│   ├── discover.py     # RSS 发现引擎(贴官网 URL -> 三级发现)
│   ├── build_sources.py# 源 liveness 实测工具
│   └── llm.py          # 统一 LLM 入口(claude-cli / api 二选一)
├── index.html          # 看板(两级折叠 + 要点卡 + 源管理面板)
├── server.py           # 本地服务 + /api/refresh + /api/sources
├── data.js             # 抓取+AI 产物(gitignore 不忽略,作示例数据)
└── llm.config.json     # 大模型 provider 配置
```

## RSS 源管理

点侧栏 ⚙ 齿轮打开面板:

- **添加源**:贴官网 URL(如 `https://www.navalnews.com/`)或 RSS 直链。
  - 贴 RSS 直链:验证合法即用。
  - 贴官网:三级发现 ① 抓首页 `<link rel=alternate>` ② 试常见路径 `/feed` `/rss.xml` ③ LLM 兜底判断是否军事站 + 归类(不编 URL)。
- **删除源**:列表每行删除按钮。

改完源点 ⟳ 刷新抓取即可生效。`sources.json` 原子写,不会损坏。

## 配置说明

`sources.json` 关键字段:
```json
{
  "fetch": {"per_source": 6, "timeout": 30, "recent_days": 7},
  "countries": [{"key":"us","name":"美国","accent":"#3b82f6"}, ...],
  "branches":  [{"key":"army","name":"陆军"}, ...],
  "sources":   [{"name":"Army Times","country":"us","branch":"army","type":"rss","url":"..."}],
  "redline_keywords": []   // 红线词,命中则过滤(自填)
}
```
- `per_source`:每源最多取几条(默认 6)
- `recent_days`:只取近 N 天(默认 7)
- `accent`:国家主题色(看板 UI)

## 安全说明

- server.py **只绑 127.0.0.1**,不对局域网开放(/api/refresh 会跑本机子进程)。
- `llm.config.json` 的 `api_key` **不要填真实 key** -- 走 `LLM_API_KEY` 环境变量。仓库模板里 key 始终留空。
- 外链来自 RSS `<link>`,前端只放行 `http(s)` 并转义,防 XSS / `javascript:` 伪协议。
- `/api/refresh` 仅 POST(防 GET 触发的 CSRF)。

## 致谢

基于 [investment-news](https://github.com/simonlin1212/investment-news) 的四段式流水线改造,把「12 行业扁平」改成「国家 → 军种」三级结构。

## 声明

公开资讯聚合,非作战情报。仅供个人阅读与研究。
