# RSS 源管理面板

## 目标
侧栏顶部加齿轮图标,点击弹出管理面板。用户贴一个**网站官网 URL**(不必找 RSS 链接),后端自动发现/转换成 RSS feed 并加入 sources.json,可删除现有源。无 RSS 时用 LLM 兜底判断站点归类。

## 核心流程:贴 URL -> 订阅
用户可在输入框贴**官网 URL**(如 `https://www.navalnews.com/`)或**直接贴 RSS URL**(如 `https://www.navalnews.com/feed/`)。

- 若贴的是 **RSS/Atom URL**(后端 HEAD/GET 验证返回合法 RSS/Atom XML):跳过发现,直接当 feed 用。
- 若贴的是**网页 URL**:执行**三级发现**:
  1. **第一级 抓首页 RSS link**:urllib 抓 HTML,HTMLParser 找 `<link rel=alternate type=application/rss+xml|atom+xml>`,有就直接用。(实测 navalnews/topwar/UK Defence Journal 命中)
  2. **第二级 试常见路径**:首页没 link 标签的,试 `/feed`、`/feed/`、`/rss.xml`、`/rss`、`/atom.xml`、`/feeds/posts/default`(博客),逐个 GET 验证返回合法 RSS/Atom XML。命中即用。
  3. **第三级 LLM 兜底**:前两级都失败,调 DeepSeek(llm.call),给它站点标题+前几百字正文,让它判断:(a) 这是不是军事/国防相关站;(b) 归哪个国家、哪个分支。LLM **不编 feed URL**,只给分类建议。如果判定非军事站,返回提示「该站疑似非军事/国防主题,确认添加?」让用户定。仍然没有 feed URL 则如实返回「未发现 RSS,该站可能不提供订阅;可尝试直接贴该站的 RSS 链接」。
- 发现 feed 后,用 build_sources.py 同款 liveness 验证(能解析出 item/entry 才算成功),失败就告诉用户具体原因(403/SSL/非 XML)。
- **源名**:抓页面 `<title>` 或第一个 `<h1>`,清理后作 name;用户可在面板里改。
- **国家/分支**:用户在下拉里选(默认按 LLM 建议预填,或选"自动"让 LLM 定)。
- 写回 sources.json:读 JSON、append 源、写回(原子写:写临时文件再 rename,避免半写损坏)。
- 成功后前端刷新面板列表 + 提示「已订阅,点右上角刷新抓取」。

## 删除源
- `DELETE /api/sources` 带源 URL 或 name,从 sources.json 移除并写回。
- 前端列表每行一个删除按钮,确认后调接口。

## 文件改动

### 新增:`scripts/discover.py`
纯标准库。核心函数:
- `discover_feed(site_url) -> (feed_url, site_name, err)`  
  第一级抓 link + 第二级试常见路径 + liveness 验证。
- `classify_with_llm(site_url, site_name, snippet) -> {country, branch, is_military, reason}`  
  调 llm.call(复用 llm.py + llm.config.json),给 SYS 提示让它输出 JSON。
- 不重复抓取逻辑:首页抓取函数与 fetch.py 的 UA/超时一致(可 import 或复制,保持纯标准库)。

### 改:`server.py`
加路由(沿用现有 SimpleHTTPRequestHandler 风格):
- `POST /api/sources` {url, country?, branch?} -> 调 discover.py 逻辑,写 sources.json,返回 {ok, feed_url, name, country, branch} 或 {ok:false, error}
- `DELETE /api/sources` {url} -> 从 sources.json 删,返回 {ok}
- `GET /api/sources` -> 返回 sources.json 全量(面板初始化用,避免前端再解析)
- 路由分发扩展 do_POST/do_DELETE。PYTHONUTF8 子进程环境沿用 child_env()。
- sources.json 写回用原子写(临时文件 + os.replace),防并发损坏。

### 改:`index.html`
- 侧栏 brand 区:刷新按钮旁加齿轮图标按钮(复用 .refresh 样式)。
- 新增面板 HTML/CSS:全屏覆盖层 `.modal`,含源列表(每行:源名、国家、分支、删除按钮)+ 底部添加表单(URL 输入 + 国家下拉 + 分支下拉 + 订阅按钮)。
- 面板逻辑:打开时 GET /api/sources 渲染列表;订阅时 POST,成功后刷新列表;删除时 confirm + DELETE。
- 国家/分支下拉选项从 window.DATA 或 /api/sources 取(已有 countries/branches 配置)。

## LLM 调用边界(明确,避免滥用)
- **唯一调 LLM 的时机**:第三级兜底(前两级发现失败时)。正常有 RSS link 的站不调 LLM,零成本。
- **LLM 只做分类判断**(是否军事、国家、分支),绝不生成 URL。
- 复用 digest.py 的 llm.call + llm.config.json(provider 走 deepseek api,已有 key 环境变量)。

## 验证标准
1. 贴 `https://www.navalnews.com/` -> 第一级命中 `https://www.navalnews.com/feed/`,加入后能被 fetch.py 抓到条目。
2. **直接贴 RSS URL**(如 `https://news.usni.org/feed`)-> 跳过发现,直接订阅成功。
3. 贴无 RSS link 的站 -> 第二级试路径,或第三级 LLM 判定后如实返回。
4. 删除一个源 -> sources.json 移除,fetch 重跑后该源条目消失。
5. sources.json 写回不损坏(原子写 + JSON 合法)。
6. 面板入口齿轮图标可见,面板能开关,列表与 sources.json 一致。

## 不做(避免过度)
- 不做拖拽排序、批量导入。
- 不做源编辑(改 URL/name),只增删;要改就删了重加。
- 不在订阅时自动跑 fetch(让用户手动点刷新,避免订阅一个慢源卡住面板)。
- 不做 RSS-bridge 等外部服务依赖。
