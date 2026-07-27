#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discover.py -- RSS 源发现 + 分类(供 server.py 的源管理面板调用)。

贴一个 URL(官网或 RSS 直链),判断并转成可订阅的 RSS feed:
- 若是 RSS/Atom 直链:验证合法 XML 即用。
- 若是网页:三级发现
  1) 抓首页 <link rel=alternate type=application/rss+xml|atom+xml>
  2) 试常见路径 /feed /rss.xml /atom.xml ...
  3) LLM 兜底:判定是否军事/国防站 + 归哪个国家/分支(不编 URL,只分类)
发现 feed 后用与 build_sources.py 同款 liveness 验证(能解析出 item/entry)。

对外主入口:discover(site_url) -> dict
  {ok, feed_url, name, country?, branch?, is_military?, note?}
纯标准库(LLM 走 llm.py)。
"""
import json, os, re, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import llm   # 复用 digest.py 的统一 LLM 入口

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
# 第二级试的常见 feed 路径(相对根)
COMMON_PATHS = ["feed", "feed/", "rss.xml", "rss", "atom.xml", "rss/index.xml",
                "feeds/posts/default", "feed.xml", "index.xml"]

CFG = {"provider": "claude-cli"}   # main/调用方用 llm.config.json 覆盖


def local(tag): return tag.split("}")[-1]


def _get(url, headers=None):
    """GET,返回 (bytes, err)。err 非空即失败。"""
    h = {"User-Agent": UA, "Accept": "application/rss+xml,application/atom+xml,"
          "application/xml,text/xml,text/html,*/*"}
    if headers: h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(), None
    except Exception as e:
        return None, str(e)[:100]


def is_feed_xml(raw):
    """字节串能否解析为 RSS/Atom,且含 item/entry(与 build_sources.py 同款判断)。"""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return False
    return any(local(e.tag) in ("item", "entry") for e in root.iter())


def liveness_ok(feed_url):
    """feed URL 能否抓到合法 RSS/Atom。返回 (ok, n_items, err)。"""
    raw, err = _get(feed_url)
    if err: return False, 0, err
    if not is_feed_xml(raw): return False, 0, "非 RSS/Atom XML"
    try:
        root = ET.fromstring(raw)
        n = sum(1 for e in root.iter() if local(e.tag) in ("item", "entry"))
        return True, n, ""
    except Exception as e:
        return False, 0, str(e)[:80]


class _LinkFinder(HTMLParser):
    """从 HTML <head> 抓 <link rel=alternate type=rss/atom>。"""
    def __init__(self):
        super().__init__(); self.feeds = []; self.title = ""
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "link":
            t = d.get("type", "").lower()
            if ("rss" in t or "atom" in t) and d.get("href"):
                self.feeds.append(d["href"])
        elif tag == "title" and not self.title:
            pass  # title 在 handle_data 里收
    def handle_data(self, data):
        # 只收最近一个 <title> 的文本(简化:取第一个非空)
        if not self.title and data.strip():
            self._maybe_title = getattr(self, "_maybe_title", "") + data


def _fetch_html(url):
    """抓网页,返回 (html_str, final_url, err)。final_url 是重定向后的。"""
    raw, err = _get(url)
    if err: return "", url, err
    try:
        html = raw.decode("utf-8", errors="replace")
    except Exception:
        html = raw.decode("latin-1", errors="replace")
    # urllib 会跟随重定向,但拿不到最终 URL;简化处理不追 final
    return html, url, None


def _abs(base, u):
    if not u: return ""
    if u.startswith("http"): return u
    return urllib.parse.urljoin(base, u)


def _looks_rss(url):
    """URL 路径看起来像 feed(粗判,用于第一级候选筛选)。"""
    u = url.lower()
    return any(x in u for x in ["rss", "feed", "atom", ".xml"])


def _extract_title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m: return ""
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return t[:60]


def _try_paths(base):
    """第二级:对 base 试常见 feed 路径,返回首个可用的 feed_url。"""
    parsed = urllib.parse.urlparse(base)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)
    for p in COMMON_PATHS:
        cand = urllib.parse.urljoin(origin + "/", p)
        ok, n, err = liveness_ok(cand)
        if ok: return cand
    return ""


def classify_llm(site_url, name, snippet):
    """第三级 LLM 兜底:判断是否军事/国防站 + 国家 + 分支。不编 URL。
    返回 {is_military, country, branch, reason} 或 None。"""
    user = ("网站URL:%s\n网站名:%s\n页面摘要:%s\n"
            "判断:(1)是否军事/国防相关站(2)主要属于哪国(3)哪个军种/类别。"
            "只输出JSON: {\"is_military\":bool,\"country\":\"us/cn/ru/uk/fr/jp/in/il/ua之一或空\","
            "\"branch\":\"army/navy/airforce/space/marines/guard/gov/thinktank/press之一或空\","
            "\"reason\":\"一句理由\"}") % (site_url, name, snippet[:600])
    SYS = "你是军事新闻源分类助手。根据网站信息判断主题与归属,只输出JSON。"
    try:
        out = llm.call(SYS, user, CFG, timeout=60)
    except Exception as e:
        return {"error": str(e)[:80]}
    if not out: return None
    a, b = out.find("{"), out.rfind("}")
    if a < 0 or b < 0: return None
    try:
        return json.loads(out[a:b+1])
    except Exception:
        return None


def discover(site_url, cfg=None):
    """主入口。返回 {ok, feed_url, name, country, branch, note}。"""
    global CFG
    if cfg: CFG = cfg
    else: CFG = llm.load_config(ROOT)
    site_url = (site_url or "").strip()
    if not site_url: return {"ok": False, "error": "URL 为空"}
    if not site_url.startswith("http"): site_url = "https://" + site_url

    # 1) 先当 RSS 直链试(贴的就是 feed 的情况)
    ok, n, err = liveness_ok(site_url)
    if ok:
        return {"ok": True, "feed_url": site_url, "name": "", "via": "direct",
                "note": "已是 RSS/Atom 直链(%d 条)" % n}

    # 2) 当网页抓,第一级找 <link>
    html, final, herr = _fetch_html(site_url)
    if herr:
        # 网页抓不到,且直链也不是 feed:彻底失败
        return {"ok": False, "error": "无法抓取该 URL(直链非RSS,网页也抓不到):%s" % err}
    name = _extract_title(html) or ""
    lf = _LinkFinder(); lf.feed(html)
    feed_url = ""
    for u in lf.feeds:
        cand = _abs(final, u)
        ok, n, e = liveness_ok(cand)
        if ok: feed_url = cand; break
    if not feed_url:
        # 第二级试常见路径
        feed_url = _try_paths(final)

    via = "link" if feed_url and lf.feeds else ("path" if feed_url else "")
    if feed_url:
        return {"ok": True, "feed_url": feed_url, "name": name, "via": via,
                "note": "发现于%s" % ("首页link标签" if via == "link" else "常见路径")}

    # 3) 第三级 LLM 兜底:判定主题/分类(仍无 feed_url)
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))[:600]
    cls = classify_llm(site_url, name, snippet)
    note = "未发现 RSS feed(首页无link、常见路径也不通)。"
    if cls:
        if cls.get("error"):
            note += " LLM分类失败:%s" % cls["error"]
        else:
            mil = cls.get("is_military")
            note += " LLM判定:%s" % cls.get("reason", "")
            if mil is False:
                note += " ⚠️疑似非军事/国防主题"
            return {"ok": False, "error": "no_feed", "name": name,
                    "country": cls.get("country", ""), "branch": cls.get("branch", ""),
                    "is_military": mil, "note": note,
                    "hint": "可尝试直接贴该站的 RSS 链接"}
    return {"ok": False, "error": "no_feed", "name": name, "note": note,
            "hint": "可尝试直接贴该站的 RSS 链接"}


if __name__ == "__main__":
    # CLI 自测:python scripts/discover.py <url>
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.navalnews.com/"
    r = discover(url)
    print(json.dumps(r, ensure_ascii=False, indent=2))
