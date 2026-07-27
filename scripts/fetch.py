#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch.py -- 抓 sources.json 里各国/各军种类全部源的真实数据,带"最近 N 天"过滤,
时间规整为北京时间"MM-DD HH:MM",每分支按时间倒序,写 ../data.js。纯标准库,零依赖。
分组结构:countries[].branches[].items (三级嵌套)。
"""
import json, os, re, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PER = 5
CUTOFF = None
REDLINE = []
BEIJING = timezone(timedelta(hours=8))

# 单源抓取超时（秒）。海外源在部分网络下握手就要十几秒，14s 会把大量本来能成功的
# 源判成失败（issue #3）。走代理时建议再放宽。
# urllib 默认就会读取 HTTPS_PROXY / HTTP_PROXY / NO_PROXY 环境变量，无需额外配置。
TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "30"))

def strip_html(s): return re.sub(r"\s+"," ", re.sub(r"<[^>]+>","", s or "")).strip()
def local(tag): return tag.split("}")[-1]

def parse_dt(s):
    if not s: return None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try: dt = datetime.fromisoformat(s.strip().replace("Z","+00:00"))
        except Exception: return None
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt

def fetch(src):
    try:
        req = urllib.request.Request(src["url"], headers={"User-Agent":UA,
              "Accept":"application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        out = []
        for n in [e for e in root.iter() if local(e.tag) in ("item","entry")]:
            if len(out) >= PER: break
            d = {"title":"","url":"","time":"","ts":0,"summary":"","source":src["name"]}
            rawtime = ""
            for c in n:
                t = local(c.tag)
                if t=="title" and not d["title"]: d["title"]=(c.text or "").strip()
                elif t=="link" and not d["url"]: d["url"]=c.get("href") or (c.text or "").strip()
                elif t in ("pubDate","published","updated","date") and not rawtime: rawtime=(c.text or "").strip()
                elif t in ("description","summary","content") and not d["summary"]: d["summary"]=strip_html(c.text or "")[:160]
            if not d["title"]: continue
            blob = (d["title"] + " " + d["summary"]).lower()   # 红线过滤(用户自填词表)
            if any(k in blob for k in REDLINE): continue
            dt = parse_dt(rawtime)
            if dt is not None:
                if CUTOFF and dt < CUTOFF: continue          # 旧文剔除
                d["time"] = dt.astimezone(BEIJING).strftime("%m-%d %H:%M")
                d["ts"] = int(dt.timestamp())
            else:
                d["time"] = "-"
            out.append(d)
        return out
    except Exception as e:   # 不再静默吞掉:打出失败源名+原因(超时/非法XML/编码)
        print("  ⚠️ 源抓取失败 %s: %s" % (src.get("name","?"), str(e)[:80]))
        return None          # None=出错(区别于 []=成功但近 N 天无新内容)

def main():
    global CUTOFF, REDLINE, PER
    cfg = json.load(open(os.path.join(ROOT,"sources.json"), encoding="utf-8"))
    days = cfg.get("fetch",{}).get("recent_days", 120)
    CUTOFF = datetime.now(timezone.utc) - timedelta(days=days)
    REDLINE = [k.lower() for k in cfg.get("redline_keywords", [])]
    PER = int(cfg.get("fetch",{}).get("per_source", 5))
    countries_cfg = cfg["countries"]
    branches_cfg = cfg["branches"]

    # 按 (country, branch) 分桶源
    bycb = {}
    for s in cfg["sources"]:
        bycb.setdefault((s["country"], s["branch"]), []).append(s)

    # 构建三级嵌套:只创建有源的 country/branch 节点(空分支不渲染,避免 72 格噪音)
    countries, tasks = [], []
    for c in countries_cfg:
        cbranches = []
        for b in branches_cfg:
            pool = bycb.get((c["key"], b["key"]), [])
            if not pool: continue
            bnode = {"key":b["key"],"name":b["name"],"total":len(pool),"items":[]}
            cbranches.append(bnode)
            for s in pool: tasks.append((bnode, s))
        if cbranches:
            countries.append({"key":c["key"],"name":c["name"],"accent":c["accent"],"branches":cbranches})

    with ThreadPoolExecutor(max_workers=40) as ex:
        results = list(ex.map(lambda t: (t[0], t[1].get("name",""), fetch(t[1])), tasks))
    failed_sources = []
    for bnode, name, items in results:
        if items is None:          # 该源抓取出错
            failed_sources.append(name); continue
        bnode["items"].extend(items)
    # 每分支按时间倒序(新->旧)
    for c in countries:
        for b in c["branches"]:
            b["items"].sort(key=lambda x: x.get("ts",0), reverse=True)

    branches_active = sum(len(c["branches"]) for c in countries)

    # 合并上次的 AI 产物(翻译 zh + 要点 points):fetch 会重建全新 item 对象,
    # 若不回填,digest 就得把所有条目重译一遍(浪费 token)。按 url 把旧 zh 贴回新 item。
    old_zh, old_pts = {}, {}
    oldp = os.path.join(ROOT, "data.js")
    if os.path.exists(oldp):
        try:
            txt = open(oldp, encoding="utf-8").read()
            od = json.loads(txt[txt.index("{"):txt.rindex("}")+1])
            for c in od.get("countries", []):
                if c.get("points"): old_pts[c["key"]] = c["points"]
                for b in c.get("branches", []):
                    for it in b.get("items", []):
                        if it.get("zh") and it.get("url"):
                            old_zh[it["url"]] = it["zh"]
            merged = 0
            for c in countries:
                if c["key"] in old_pts: c["points"] = old_pts[c["key"]]
                for b in c["branches"]:
                    for it in b["items"]:
                        if it.get("url") in old_zh:
                            it["zh"] = old_zh[it["url"]]; merged += 1
            if merged: print("  ↻ 已回填 %d 条旧翻译(免重译)" % merged)
        except Exception as e:
            print("  (跳过旧翻译合并:%s)" % str(e)[:60])

    data = {"generated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
            "recent_days":days, "countries":countries,
            "stats":{"countries":len(countries), "branches_active":branches_active,
                     "total_sources":len(cfg["sources"])}}
    with open(os.path.join(ROOT,"data.js"),"w",encoding="utf-8") as f:
        f.write("// data.js -- 各国/军种源真实数据(最近%d天,北京时间,新->旧)。fetch.py 抓取 -> digest.py 补 AI 要点+翻译。\n"%days)
        f.write("window.DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n")
    print("最近 %d 天 · 国家/分支 | 源数 | 条数" % days)
    for c in countries:
        print("【%s】" % c["name"])
        for b in c["branches"]:
            print("  %-10s %3d 源 -> %d 条" % (b["name"], b["total"], len(b["items"])))
    print("总源:", len(cfg["sources"]), "· 活跃分支:", branches_active, "· 生成:", data["generated_at"])
    if failed_sources:
        print("⚠️  %d/%d 个源抓取失败:%s%s" % (len(failed_sources), len(cfg["sources"]),
              "、".join(failed_sources[:15]), " …" if len(failed_sources) > 15 else ""))

if __name__ == "__main__":
    main()
