#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
林语 · 文博行业新闻爬虫（GitHub Actions 每日运行）

抓取公开新闻源 → 过滤（垃圾信息硬排除、负面新闻单独分类）→ 打分挑选 → 去重 →
生成 news.json（供 Tavo「行业见闻」插件读取）和 digest.md（给 jiaying 看的日报存档）。

设计原则（与世界书两线并行）：
- 只做「行业见闻」：公开新闻，与他的个人项目无关。
- 负面/丑闻新闻单独一类（negative）：只收国内媒体报道的公开大新闻，
  每天最多 1 条，强制敏感标记，同一题材 7 天内不重复。
- 唐三彩、院内项目类新闻只能作素材级（谈资），不标事件级。
- 每日最多 3 条，事件级最多 1 条。

仅使用 Python 标准库，无需 pip 安装。
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FEED_PATH = os.path.join(ROOT, "news.json")
DIGEST_PATH = os.path.join(ROOT, "digest.md")
SEEN_PATH = os.path.join(HERE, "seen.json")

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# ---------------------------------------------------------------- 配置

# 搜索词住在仓库根目录的 topics.txt 里（jiaying 可以自己在网页上改），
# 分 [他的行业] 和 [她的近况] 两组。下面是 topics.txt 缺失时的内置兜底。
DEFAULT_WORK_QUERIES = [
    "南京博物院",            # 单位本身
    "文物修复",              # 大类
    "青花瓷 文物",           # 核心品类（硕士论文与在研数据库方向）
    "陶瓷文物 保护修复",     # 品类扩展
    "青铜器 修复保护",       # 品类扩展
    "文物 科技检测",         # 技术方向（无损检测/成分分析）
    "文物 预防性保护",       # 工作内容（保存环境/微环境）
    "考古发掘 文物",         # 出土文物保护与外派支援线
    "古籍 纸质文献 保护",    # 金陵图书馆顾问线
    "博物馆 特展 文物",      # 展陈线
]
TOPICS_PATH = os.path.join(ROOT, "topics.txt")


def load_topics():
    groups = {"work": [], "life": []}
    cur = "work"
    try:
        with open(TOPICS_PATH, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("["):
                    cur = ("life" if ("近况" in line or "life" in line.lower())
                           else "work")
                    continue
                groups[cur].append(line)
    except Exception:
        pass
    if not groups["work"]:
        groups["work"] = list(DEFAULT_WORK_QUERIES)
    return groups
# 直抓源（政府/行业媒体，服务器渲染，最好努力，失败不影响整体）
DIRECT_PAGES = [
    ("国家文物局", "https://www.ncha.gov.cn"),
    ("中国文物新闻网", "https://www.zgwwb.com.cn"),
    ("中国文物新闻网", "https://www.zgwwb.com.cn/bwg/"),
]

FRESH_HOURS = 72          # 只把 72 小时内发布的新闻当作候选
KEEP_DAYS = 4             # seen 记录保留天数（防重复注入）
MAX_ITEMS = 3             # 每日 feed 条数上限
MAX_EVENT_ITEMS = 1       # 其中事件级上限
MAX_NEGATIVE_ITEMS = 1    # 其中负面大新闻上限
MAX_LIFE_ITEMS = 1        # 其中「她的近况」上限
NEGATIVE_KEEP_DAYS = 7    # 同一负面题材的冷却天数（后续进展不天天刷屏）

# 「她的近况」一组的话题不走文博闸门，按正经新闻把关：
# 国内大媒体来源加权，教育/考试类锚点加权，负面词表另加考试作弊类
LIFE_MAJOR_SOURCES = ["人民", "新华", "央视", "中央", "中国新闻", "澎湃",
                      "日报", "晚报", "光明", "教育报"]
LIFE_ANCHOR_WORDS = ["考研", "研究生", "考试", "招生", "教育"]
LIFE_SCANDAL_WORDS = ["抄袭", "作弊", "泄题"]

# 垃圾/无关信息硬排除：命中任一直接丢弃
SPAM_EXCLUDE_WORDS = [
    "攻略", "人均", "自由行", "旅游线路", "网红打卡地",
]

# 负面/丑闻词：命中不再直接丢弃，而是归入 negative 类单独把关
# （只收大新闻、每天至多 1 条，见 score_and_classify）
SCANDAL_WORDS = [
    "判刑", "倒卖", "疑案", "腐败", "受贿", "行贿", "举报", "问责", "违纪",
    "违法", "被查", "落马", "丑闻", "流失", "追责", "双开", "留置", "贪污",
    "渎职", "失职", "盗窃", "走私", "造假", "售假", "诈骗", "刑期",
    "有期徒刑", "调查组进驻",
]
# 「流失文物回归」类是正面新闻，命中这些词时不按负面处理
RECOVERY_WORDS = ["回归", "追索", "返还", "回国", "重聚"]

# 主题闸门：标题必须至少命中一个专业词，纯地名命中的旅游/生活资讯进不来
GATE_WORDS = ["文物", "考古", "修复", "博物馆", "博物院", "遗址", "发掘",
              "展览", "特展", "出土", "古籍", "书画", "陶瓷", "藏品", "文保",
              "青铜", "玉石", "瓷器", "纸质", "文献", "微环境", "釉"]

# 正面相关性打分（标题命中）
SCORE_WORDS = {
    "文物修复": 3, "保护修复": 3, "修复": 2,
    "考古": 3, "发掘": 3, "遗址": 3, "出土": 2, "出土文物": 2,
    "南京博物院": 3, "南博": 2, "南京市文物局": 2, "金陵图书馆": 2,
    "展览": 2, "特展": 2, "开展": 1, "博物馆": 1,
    "预防性保护": 2, "科技保护": 2, "科技检测": 2, "无损检测": 2,
    "检测": 1, "微环境": 2,
    "陶瓷": 1, "青花瓷": 1, "青铜": 2, "书画": 1, "古籍": 1,
    "纸质文献": 2, "文献保护": 2, "江苏": 1, "南京": 1,
}

# 事件级触发：考古现场 + 人事调动语境
EVENT_PLACE_WORDS = ["考古", "发掘", "遗址", "工地"]
EVENT_ACTION_WORDS = ["支援", "调动", "派", "现场", "抢救性", "领队", "队员"]
# 只能作素材、且要提示「不是他的项目」的词
PROJECT_SENSITIVE_WORDS = ["唐三彩", "结项", "立项", "课题", "项目验收"]

# ---------------------------------------------------------------- 工具

def now_cst():
    return datetime.now(CST)


def fetch(url, retries=3, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:  # 单个源失败不中断整体
            last = e
    print(f"[warn] fetch failed: {url} ({last})", file=sys.stderr)
    return None


def clean_text(s, limit=None):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip("，。；、 ") + "…"
    return s


def norm_title(t):
    return re.sub(r"[\s\W]+", "", t or "").lower()


def item_id(title):
    return hashlib.md5(norm_title(title).encode("utf-8")).hexdigest()[:12]


def parse_rfc822(s):
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST)
    except Exception:
        return None

# ---------------------------------------------------------------- 来源

def from_google_news(queries, group):
    out = []
    for q in queries:
        url = ("https://news.google.com/rss/search?q="
               + urllib.request.quote(q)
               + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        xml = fetch(url, retries=2)
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            src_el = it.find("source")
            source = src_el.text if src_el is not None and src_el.text else "新闻"
            title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()  # 去掉尾部「- 来源」
            pub = parse_rfc822(it.findtext("pubDate") or "")
            desc = clean_text(it.findtext("description") or "", 90)
            # Google 的 description 常是同题多源罗列：以标题开头、太短的都弃用，
            # 只留像句子的真实摘要
            if desc.startswith(title[:10]) or len(desc) < 25:
                desc = ""
            out.append({
                "title": title, "source": source,
                "date": pub.strftime("%Y-%m-%d") if pub else "",
                "pub_dt": pub, "summary": desc,
                "url": it.findtext("link") or "",
                "_group": group,
            })
    return out


def extract_article_summary(url):
    page = fetch(url, retries=2, timeout=20)
    if not page:
        return ""
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", page, flags=re.S)
    for p in paras:
        t = clean_text(p)
        if len(t) >= 40 and not any(k in t for k in ("责任编辑", "版权所有", "ICP", "分享到")):
            return clean_text(t, 100)
    return ""


def from_direct():
    out = []
    art_re = re.compile(
        r'href="(?P<href>(?:https?://www\.ncha\.gov\.cn)?\.?/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html)"[^>]*>(?P<t>[^<]{6,80})</a>')
    ww_re = re.compile(
        r'href="(?P<href>https?://www\.zgwwb\.com\.cn/(?:news|bwg)/\d{4}-\d{2}/\d{2}/content_\d+\.htm)"[^>]*>(?P<t>[^<]{6,80})</a>')
    for source, page_url in DIRECT_PAGES:
        page = fetch(page_url)
        if not page:
            continue
        seen_here = set()
        for regex, base in ((art_re, "https://www.ncha.gov.cn"), (ww_re, "")):
            for m in regex.finditer(page):
                href = m.group("href")
                if href.startswith("."):
                    href = base + href[1:]
                elif href.startswith("/"):
                    href = base + href
                if href in seen_here:
                    continue
                seen_here.add(href)
                dm = re.search(r"/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/", href)
                pub = None
                if dm:
                    pub = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), tzinfo=CST)
                out.append({
                    "title": clean_text(m.group("t")), "source": source,
                    "date": pub.strftime("%Y-%m-%d") if pub else "",
                    "pub_dt": pub, "summary": "", "url": href,
                    "_need_summary": True, "_group": "work",
                })
    return out

# ---------------------------------------------------------------- 处理

ANCHOR_WORDS = ["博物馆", "博物院", "文物局", "中国", "江苏", "南京"]


def score_and_classify(it):
    title = it["title"]
    if any(w in title for w in SPAM_EXCLUDE_WORDS):
        return None  # 垃圾信息硬排除
    domestic = bool(re.search(r"[\u4e00-\u9fff]", it.get("source", "")))
    if it.get("_group") == "life":
        # 「她的近况」：不走文博闸门，只收国内正经新闻来源；
        # 大媒体 + 话题锚点加分，负面（含考试作弊类）单独归类
        if not domestic:
            return None
        # 单个学校的招生简章对她没意义，全国性的才留
        if ("招生简章" in title or "招生章程" in title) and "全国" not in title:
            return None
        score = 2
        if any(s in it.get("source", "") for s in LIFE_MAJOR_SOURCES):
            score += 2
        if any(w in title for w in LIFE_ANCHOR_WORDS):
            score += 1
        if score < 4:
            return None
        neg = (any(w in title for w in SCANDAL_WORDS + LIFE_SCANDAL_WORDS)
               and not any(w in title for w in RECOVERY_WORDS))
        return score, ("negative" if neg else "material"), neg
    if not any(w in title for w in GATE_WORDS):
        return None  # 主题闸门
    score = sum(v for w, v in SCORE_WORDS.items() if w in title)
    if any(w in title for w in ANCHOR_WORDS):
        score += 2  # 国内/文博机构锚点加权，避免境外考古新闻挤占名额
    if not domestic:
        score -= 4  # 来源为境外媒体（无中文名）大幅降权
    # 负面/丑闻单独把关：只收国内媒体报道、相关性足够高的「大新闻」，
    # 强制敏感标记；不够格的负面消息仍然丢弃
    if (any(w in title for w in SCANDAL_WORDS)
            and not any(w in title for w in RECOVERY_WORDS)):
        if domestic and score >= 5:
            return score, "negative", True
        return None
    sensitive = any(w in title for w in PROJECT_SENSITIVE_WORDS)
    level = "material"
    if (any(w in title for w in EVENT_PLACE_WORDS)
            and any(w in title for w in EVENT_ACTION_WORDS)
            and not sensitive):
        level = "event"
    return score, level, sensitive


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def main():
    today = now_cst()
    fresh_cutoff = today - timedelta(hours=FRESH_HOURS)

    topics = load_topics()
    candidates = (from_google_news(topics["work"], "work")
                  + from_google_news(topics["life"], "life")
                  + from_direct())

    # 新鲜度 + 标题去重
    uniq = {}
    for it in candidates:
        if not it["title"]:
            continue
        if it["pub_dt"] is not None and it["pub_dt"] < fresh_cutoff:
            continue
        uniq.setdefault(item_id(it["title"]), it)
    candidates = list(uniq.values())

    # 打分分类
    scored = []
    for it in candidates:
        r = score_and_classify(it)
        if r is None:
            continue
        it["score"], it["level"], it["sensitive"] = r
        if it["score"] < 3:
            continue
        scored.append(it)
    scored.sort(key=lambda x: (x["level"] == "event", x["score"],
                               x["pub_dt"] or datetime.min.replace(tzinfo=CST)),
                reverse=True)

    # 去重（seen）：已注入过的永不再选；负面题材另有 7 天冷却记录
    raw_seen = load_seen()
    neg_history = [e for e in raw_seen.get("__neg__", [])
                   if (today.date() - datetime.fromisoformat(e["d"]).date()).days
                   < NEGATIVE_KEEP_DAYS]
    seen = {k: v for k, v in raw_seen.items() if k != "__neg__"
            and (today.date() - datetime.fromisoformat(v).date()).days < KEEP_DAYS}
    fresh_items = [it for it in scored if item_id(it["title"]) not in seen]

    # 挑选：事件级优先占 1 位，总数 ≤ MAX_ITEMS；
    # 同一事件的多源报道只留一条（标题最长公共子串 ≥6 字视为同题）
    def same_story(a, b):
        a, b = norm_title(a), norm_title(b)
        best = 0
        prev = [0] * (len(b) + 1)
        for i in range(1, len(a) + 1):
            cur = [0] * (len(b) + 1)
            for j in range(1, len(b) + 1):
                if a[i - 1] == b[j - 1]:
                    cur[j] = prev[j - 1] + 1
                    best = max(best, cur[j])
            prev = cur
        return best >= 6

    picks, events, negatives, lifes = [], 0, 0, 0
    for it in fresh_items:
        if len(picks) >= MAX_ITEMS:
            break
        if it["level"] == "event":
            if events >= MAX_EVENT_ITEMS:
                continue
            events += 1
        if it.get("_group") == "life":
            if lifes >= MAX_LIFE_ITEMS:
                continue
            lifes += 1
        if it["level"] == "negative":
            if negatives >= MAX_NEGATIVE_ITEMS:
                continue
            # 同一负面题材在冷却期内不再选（后续进展不天天刷屏）
            if any(same_story(it["title"], e["t"]) for e in neg_history):
                continue
            negatives += 1
        if any(same_story(it["title"], p["title"]) for p in picks):
            continue
        picks.append(it)

    # 为直抓源补摘要（最多补 4 篇，省时间）
    n = 0
    for it in picks:
        if it.get("_need_summary") and n < 4:
            it["summary"] = extract_article_summary(it["url"])
            n += 1

    feed_items = []
    for it in picks:
        iid = item_id(it["title"])
        seen[iid] = today.strftime("%Y-%m-%d")
        if it["level"] == "negative":
            neg_history.append({"t": it["title"],
                                "d": today.strftime("%Y-%m-%d")})
        feed_items.append({
            "id": iid,
            "title": it["title"],
            "source": it["source"],
            "date": it["date"] or today.strftime("%Y-%m-%d"),
            "summary": it.get("summary", ""),
            "level": it["level"],           # material | event | negative
            "sensitive": it["sensitive"],   # 涉及唐三彩/院内项目：仅作谈资
            "group": it.get("_group", "work"),  # work 他的行业 | life 她的近况
            "url": it["url"],
        })
    save_seen({**seen, "__neg__": neg_history})

    feed = {
        "generated_at": today.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "date": today.strftime("%Y-%m-%d"),
        "items": feed_items,
    }
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    # digest 存档（给 jiaying 看；没有新条目也记一行，方便确认在跑）
    lines = [f"\n## {feed['date']}\n"]
    if feed_items:
        for it in feed_items:
            tag = {"event": "事件级", "negative": "负面大新闻"}.get(
                it["level"], "素材级")
            if it.get("group") == "life":
                tag += "·她的近况"
            lines.append(f"- [{tag}] 《{it['title']}》（{it['source']}，{it['date']}）"
                         + (f" — {it['summary']}" if it["summary"] else "")
                         + f"  {it['url']}")
    else:
        lines.append("- （今日无符合条件的新条目）")
    need_header = (not os.path.exists(DIGEST_PATH)) or os.path.getsize(DIGEST_PATH) == 0
    with open(DIGEST_PATH, "a", encoding="utf-8") as f:
        if need_header:
            f.write("# 文博行业日报存档\n")
        f.write("\n".join(lines) + "\n")

    print(json.dumps(feed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
