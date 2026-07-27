#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_sources.py -- liveness 实测 sources.json 里每个源(逐个 fetch 验证返回 RSS/Atom XML),
报告存活/失效,便于替换死链。纯标准库。只读不写,不改 sources.json。
"""
import json, os, time, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def local(tag): return tag.split("}")[-1]

def liveness(s):
    name, url = s.get("name","?"), s["url"]
    for attempt in range(2):                 # 1 次重试,避免好源因瞬时超时被误判
        try:
            req = urllib.request.Request(url, headers={"User-Agent":UA,
                  "Accept":"application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            root = ET.fromstring(raw)
            n = sum(1 for e in root.iter() if local(e.tag) in ("item","entry"))
            return (s, True, n, "")
        except Exception as e:
            err = str(e)[:90]
            if attempt < 1: time.sleep(1.0); continue
            return (s, False, 0, err)

def main():
    cfg = json.load(open(os.path.join(ROOT,"sources.json"), encoding="utf-8"))
    sources = cfg["sources"]
    branches = {b["key"]:b["name"] for b in cfg["branches"]}
    countries = {c["key"]:c["name"] for c in cfg["countries"]}
    print("liveness 实测 %d 个源(每源超时 20s)…" % len(sources))
    with ThreadPoolExecutor(max_workers=20) as ex:
        res = list(ex.map(liveness, sources))
    alive, dead = [], []
    for s, ok, n, err in res:
        if ok: alive.append((s, n))
        else: dead.append((s, err))
    print("\n✅ 存活 %d / %d:" % (len(alive), len(sources)))
    for s, n in sorted(alive, key=lambda x:(x[0]["country"], x[0]["branch"])):
        print("  [%s/%s] %-26s %3d 条  %s" % (countries.get(s["country"],s["country"]),
              branches.get(s["branch"],s["branch"]), s["name"], n, s["url"]))
    if dead:
        print("\n❌ 失效 %d:" % len(dead))
        for s, err in sorted(dead, key=lambda x:(x[0]["country"], x[0]["branch"])):
            print("  [%s/%s] %-26s %s\n        ERR: %s" % (countries.get(s["country"],s["country"]),
                  branches.get(s["branch"],s["branch"]), s["name"], s["url"], err))
    print("\n存活率: %d/%d = %d%%" % (len(alive), len(sources), round(100*len(alive)/max(1,len(sources)))))

if __name__=="__main__":
    main()
