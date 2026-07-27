#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""digest.py -- AI 要点+翻译层(增量)。读 ../data.js:
1) 要点:该国无 points 时取最新 TOPN 条算一次「今日要点」(增量重跑不重算,省 token)。
2) 翻译:近 TRANS_DAYS 天、且尚未翻译的条目补中文标题(已有 zh 跳过,不重复消耗 token;
   已是中文标题的免译)。
provider 由 llm.config.json 定(claude-cli 订阅 / deepseek 等 OpenAI 兼容 api)。纯标准库。
"""
import json, os, re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import llm   # 统一大模型入口(订阅 claude-cli / API 二选一,见 ../llm.config.json)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOPN = 20           # 要点聚合:每国取最新 N 条做「今日要点」(仅在该国无 points 时算一次)
TRANS_DAYS = 3      # 翻译窗口:近 N 天的新闻都翻译(不是固定条数)
WORKERS = 4
ATTEMPTS = 3        # 单国 LLM 失败重试上限(嵌套跑 claude -p 偶发失败,多试几次)

CFG = {"provider": "claude-cli"}   # main 里按 llm.config.json 覆盖

SYS_POINTS = ("你是中文军事新闻分析助手。给你某国家最近的军事新闻列表(每条带序号),请做两件事:\n"
       "1) 提炼 3-5 条「今日要点」:聚合最重要的军事动向,每条不超过 40 字,可合并同类、突出数字/部队/装备/动向,客观陈述。"
       "每条要点用 refs 标注它主要来自哪几条新闻的序号(用于跳转原文,至少 1 个)。\n"
       "2) 给每条新闻一个简洁准确的中文标题翻译;若原文已是中文则原样返回。\n"
       "只输出 JSON,不要任何解释或代码块标记。格式:\n"
       '{"points":[{"t":"要点1","refs":[0,3]},{"t":"要点2","refs":[5]}],"items":[{"i":0,"zh":"中文标题"}]}')
SYS_TRANS = ("你是军事新闻标题翻译助手。给你某国家的若干英文/外文新闻标题(带序号),请给每条一个简洁准确的中文标题翻译。"
       "只输出 JSON,不要任何解释或代码块标记。格式:\n"
       '{"items":[{"i":0,"zh":"中文标题"}]}')

def _looks_chinese(s):
    # 标题里中文字符占比过半就当已是中文,免翻译(新华网/中新网/中国政府网/防衛省日本名等)
    s = s or ""
    cn = sum(1 for c in s if '一' <= c <= '鿿')
    return cn * 2 >= len(s) and cn > 0

def _llm_points(user, label=""):
    try: return llm.call(SYS_POINTS, user, CFG)
    except Exception as e:
        print("  ⚠️ LLM(要点)异常 [%s]: %s" % (label, str(e)[:120])); return ""

def _llm_trans(user, label=""):
    try: return llm.call(SYS_TRANS, user, CFG)
    except Exception as e:
        print("  ⚠️ LLM(翻译)异常 [%s]: %s" % (label, str(e)[:120])); return ""

def extract_json(s):
    if not s: return None
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0: return None
    try: return json.loads(s[a:b+1])
    except Exception: return None

def process(country):
    # 聚合该国全部分支的 items,按时间倒序(top 元素是分支 items 里的原引用,回填翻译直接生效)
    all_items = []
    for b in country["branches"]:
        all_items.extend(b["items"])
    all_items.sort(key=lambda x: x.get("ts",0), reverse=True)

    # 1) 要点:仅在该国尚无 points 时算一次(增量重跑不重算,省 token)
    if not country.get("points"):
        top = all_items[:TOPN]
        if top:
            lines = ["国家:%s" % country["name"], "新闻:"]
            for i, it in enumerate(top):
                lines.append("%d. %s (%s)" % (i, it["title"], it.get("source","")))
            user = "\n".join(lines)
            d = None
            for _ in range(ATTEMPTS):                   # 偶发失败重试(总计 ATTEMPTS 次)
                d = extract_json(_llm_points(user, country["name"]))
                if d: break
            if not d:
                print("  ⚠️ %s 要点生成失败(已试 %d 次),重跑可只补" % (country["name"], ATTEMPTS))
            else:
                pts = []
                for p in d.get("points", [])[:5]:
                    if isinstance(p, dict):
                        t = (p.get("t") or "").strip()
                        url = ""
                        for r in p.get("refs", []):
                            if isinstance(r, int) and 0 <= r < len(top) and top[r].get("url"):
                                url = top[r]["url"]; break
                        if t: pts.append({"t": t, "url": url})
                    elif isinstance(p, str) and p.strip():
                        pts.append({"t": p.strip(), "url": ""})
                country["points"] = pts
                # 要点这轮的翻译也顺带回填(top 内未翻译的)
                zh = {x.get("i"): x.get("zh","") for x in d.get("items",[]) if isinstance(x, dict)}
                for i, it in enumerate(top):
                    if not it.get("zh"):
                        v = zh.get(i, "")
                        if v: it["zh"] = v
        if not country.get("points"): country["points"] = []

    # 2) 翻译增量:近 TRANS_DAYS 天、且尚未翻译的条目(已有 zh 跳过,不重复消耗 token)
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRANS_DAYS)
    pending = [it for it in all_items if it.get("ts",0) and it["ts"] >= cutoff.timestamp() and not it.get("zh")]
    if not pending: return country
    # 已经是中文的无需翻译(新华网/中新网/中国政府网等),省 token
    pending = [it for it in pending if not _looks_chinese(it["title"])]
    if not pending: return country
    lines = ["国家:%s · 请翻译以下新闻标题为简洁准确的中文(已是中文的无需改):" % country["name"], "新闻:"]
    for i, it in enumerate(pending):
        lines.append("%d. %s" % (i, it["title"]))
    user = "\n".join(lines)
    d = None
    for _ in range(ATTEMPTS):
        d = extract_json(_llm_trans(user, country["name"]))
        if d: break
    if not d:
        print("  ⚠️ %s 翻译失败(已试 %d 次),%d 条未翻译,重跑可只补" % (country["name"], ATTEMPTS, len(pending)))
        return country
    zh = {x.get("i"): x.get("zh","") for x in d.get("items",[]) if isinstance(x, dict)}
    for i, it in enumerate(pending):
        v = zh.get(i, "")
        if v: it["zh"] = v
    return country

def main():
    global CFG
    CFG = llm.load_config(ROOT)
    print("大模型 provider:", CFG.get("provider", "claude-cli"))
    p = os.path.join(ROOT, "data.js")
    txt = open(p, encoding="utf-8").read()
    data = json.loads(txt[txt.index("{"):txt.rindex("}")+1])
    countries = data["countries"]
    print("对 %d 个国家处理:要点(无则生成一次)+ 近%d天未翻译条目增量翻译(已翻译跳过,省 token),%d 并发…" % (len(countries), TRANS_DAYS, WORKERS))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(process, countries))
    data["has_ai"] = True
    with open(p, "w", encoding="utf-8") as f:
        f.write("// data.js -- 含 AI 要点+中文翻译(增量:近%d天未译条目补译,已译跳过)。\n" % TRANS_DAYS)
        f.write("window.DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n")
    failed = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRANS_DAYS)
    for c in countries:
        n = len(c.get("points",[]))
        tot = sum(len(b["items"]) for b in c["branches"])
        z = sum(1 for b in c["branches"] for it in b["items"] if it.get("zh"))
        # 近 TRANS_DAYS 天内、非中文标题却没翻译 = 遗漏(供重跑定位)
        miss = sum(1 for b in c["branches"] for it in b["items"]
                   if it.get("ts",0) and it["ts"] >= cutoff.timestamp()
                   and not it.get("zh") and not _looks_chinese(it["title"]))
        print("  %-10s 要点 %d 条 · 翻译 %d/%d · 近%d天未译 %d" % (c["name"], n, z, tot, TRANS_DAYS, miss))
        if miss: failed.append(c["name"])
    if failed:
        print("\n⚠️  %d 个国家有近%d天未翻译条目:%s" % (len(failed), TRANS_DAYS, "、".join(failed)))
        print("   重跑可只补未译条目(已翻译自动跳过,省 token):python3 scripts/digest.py")

if __name__ == "__main__":
    main()
