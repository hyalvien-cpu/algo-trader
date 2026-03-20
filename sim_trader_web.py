#!/usr/bin/env python3
"""
sim_trader_web.py — 新闻驱动板块轮动 + K线分析 模拟交易系统
════════════════════════════════════════════════════════════
核心逻辑：
  ① 每天爬取财经新闻 → 分析各板块情绪热度
  ② 选出最热板块 → 对板块内股票做技术分析
  ③ K线形态识别（锤子线、吞没、十字星等）+ 指标综合打分
  ④ 决定买入/持有/卖出，并评估合理持仓周期

安装: pip install flask yfinance apscheduler requests vaderSentiment
启动: python sim_trader_web.py
"""

import json, time, warnings, threading, os, math
from datetime import datetime, timedelta, timezone

# PDT = UTC-7（西雅图夏令时 3月-11月）
_PDT = timezone(timedelta(hours=-7))

def now_pdt():
    """返回当前西雅图 PDT 时间"""
    return datetime.now(timezone.utc).astimezone(_PDT)

def fmt_pdt(dt=None):
    """格式化为 PDT 时间字符串"""
    if dt is None:
        dt = now_pdt()
    return dt.strftime("%Y-%m-%d %H:%M:%S PDT")
from pathlib import Path
from functools import wraps
from flask import Flask, jsonify, request, render_template_string, make_response, redirect

warnings.filterwarnings("ignore")
DATA_FILE = Path(os.environ.get("DATA_PATH", str(Path.home() / ".sim_trader.json")))
app = Flask(__name__)

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

# ══════════════════════════════════════════════════════
#  Alpaca Paper Trading（模拟盘）
# ══════════════════════════════════════════════════════

ALPACA_KEY    = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
ALPACA_URL    = "https://paper-api.alpaca.markets"
alpaca_enabled = bool(ALPACA_KEY and ALPACA_SECRET)

def alpaca_request(method, path, data=None):
    """统一的 Alpaca API 请求函数"""
    import requests as req
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }
    url = ALPACA_URL + path
    try:
        if method == "GET":
            r = req.get(url, headers=headers, timeout=10)
        elif method == "POST":
            r = req.post(url, headers=headers, json=data, timeout=10)
        elif method == "DELETE":
            r = req.delete(url, headers=headers, timeout=10)
        else:
            return {}
        return r.json() if r.ok else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}

def alpaca_get_account():
    return alpaca_request("GET", "/v2/account")

def alpaca_get_positions():
    return alpaca_request("GET", "/v2/positions")

def alpaca_place_order(ticker, side, usd_amount):
    """市价按金额下单（支持分数股）"""
    return alpaca_request("POST", "/v2/orders", {
        "symbol": ticker,
        "notional": str(round(usd_amount, 2)),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    })

def alpaca_close_position(ticker):
    """清仓某只股票"""
    return alpaca_request("DELETE", f"/v2/positions/{ticker}")

def alpaca_is_market_open():
    clock = alpaca_request("GET", "/v2/clock")
    return clock.get("is_open", False)

@app.before_request
def check_auth():
    if not ACCESS_PASSWORD:
        return
    if request.path in ("/login", "/logout"):
        return
    if request.path.startswith("/api/"):
        if request.cookies.get("auth") != ACCESS_PASSWORD:
            return jsonify({"error": "unauthorized"}), 401
    else:
        if request.cookies.get("auth") != ACCESS_PASSWORD:
            return """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>请输入密码</title>
<style>body{display:flex;justify-content:center;align-items:center;height:100vh;
background:#08080f;font-family:sans-serif;}
form{display:flex;flex-direction:column;gap:12px;align-items:center;}
input{padding:10px 16px;border-radius:8px;border:1px solid #333;
background:#161622;color:#fff;font-size:16px;width:220px;}
button{padding:10px 24px;background:#7c6eff;color:#fff;border:none;
border-radius:8px;font-size:15px;cursor:pointer;width:220px;}
p{color:#666;font-size:13px;}
</style></head><body>
<form method="POST" action="/login">
<p>ALGOTRADE 访问验证</p>
<input type="password" name="pwd" placeholder="输入访问密码" autofocus>
<button type="submit">进入</button>
</form></body></html>"""

@app.route("/login", methods=["POST"])
def login():
    pwd = request.form.get("pwd", "")
    if pwd == ACCESS_PASSWORD:
        resp = make_response(redirect("/"))
        resp.set_cookie("auth", pwd, max_age=86400 * 30, httponly=True)
        return resp
    return redirect("/")

# ══════════════════════════════════════════════════════
#  板块定义
# ══════════════════════════════════════════════════════

SECTORS = {
    "AI与半导体": {
        "keywords": ["AI","artificial intelligence","semiconductor","chip","nvidia","amd",
                     "GPU","data center","LLM","inference","machine learning","openai",
                     "anthropic","microsoft AI","google AI","TSMC"],
        "tickers": ["NVDA","AMD","SMCI","AVGO","ARM","MRVL","TSM","INTC","QCOM","MU","LRCX","AMAT"],
        "color": "#7b6cff"
    },
    "加密货币": {
        "keywords": ["bitcoin","crypto","blockchain","ethereum","BTC","ETH","coinbase",
                     "digital asset","defi","stablecoin","crypto mining","halving","SEC crypto"],
        "tickers": ["COIN","MSTR","RIOT","MARA","CLSK","HUT","BTBT","CIFR","BITO"],
        "color": "#f5c842"
    },
    "新能源": {
        "keywords": ["solar","wind energy","EV","electric vehicle","clean energy","battery",
                     "renewable","charging station","lithium","IRA","energy storage","grid"],
        "tickers": ["ENPH","FSLR","PLUG","BE","CHPT","RUN","SEDG","ARRY","NEE","CEG"],
        "color": "#22d67a"
    },
    "生物医药": {
        "keywords": ["FDA","drug approval","clinical trial","biotech","pharma","cancer",
                     "oncology","vaccine","GLP-1","obesity","alzheimer","gene therapy"],
        "tickers": ["MRNA","BNTX","NVAX","CRSP","BEAM","EDIT","RXRX","ILMN","INCY","SGEN"],
        "color": "#ff6b9d"
    },
    "科技成长": {
        "keywords": ["cloud","SaaS","cybersecurity","software","enterprise","subscription",
                     "ARR","digital transformation","automation","palantir","crowdstrike"],
        "tickers": ["CRWD","PLTR","SNOW","DDOG","NET","ZS","GTLB","BILL","HUBS","MNDY"],
        "color": "#00d4ff"
    },
    "航天国防": {
        "keywords": ["space","rocket","satellite","defense","military","SpaceX","launch",
                     "missile","drone","Pentagon","NATO","geopolitical"],
        "tickers": ["RKLB","ASTS","LUNR","IONQ","ACHR","JOBY","PL","LMT","NOC","RTX"],
        "color": "#ff8c42"
    },
}

CONFIG = {
    "INITIAL_CASH":   100_000,
    "MAX_POSITIONS":  8,
    "POSITION_PCT":   0.12,
    "STOP_LOSS":     -0.07,
    "TAKE_PROFIT":    0.20,
    "COMMISSION":     0.001,
    "TOP_SECTORS":    2,
    "MIN_SCORE":      58,
    "TRADE_HOURS":    [9, 11, 14, 15],
}

MAX_PER_SECTOR = 2
TRAILING_STOP_PCT = 0.08

# ══════════════════════════════════════════════════════
#  宏观政策事件定义
# ══════════════════════════════════════════════════════

MACRO_EVENTS = {
    "美国加征关税": {
        "keywords": ["tariff","trade war","import duty","trade barrier","customs duty",
                     "retaliatory tariff","trade deficit","section 301","trade sanction"],
        "sector_impact": {"AI与半导体":-2,"加密货币":+1,"新能源":-1,"生物医药":0,"科技成长":-1,"航天国防":+2},
        "market_bias": -1, "desc": "关税升级冲击供应链，国防/加密受益", "hold_days_adj": -3,
    },
    "美联储加息鹰派": {
        "keywords": ["rate hike","hawkish","tighten","fed funds rate increase","quantitative tightening",
                     "higher for longer","restrictive policy","fed raise","powell hawk"],
        "sector_impact": {"AI与半导体":-2,"加密货币":-1,"新能源":-2,"生物医药":-1,"科技成长":-2,"航天国防":-1},
        "market_bias": -2, "desc": "加息压制成长股估值，全面利空", "hold_days_adj": -4,
    },
    "美联储降息鸽派": {
        "keywords": ["rate cut","dovish","pivot","easing","fed lower","quantitative easing",
                     "accommodative","soft landing","powell dove","fed pause"],
        "sector_impact": {"AI与半导体":+2,"加密货币":+2,"新能源":+2,"生物医药":+1,"科技成长":+2,"航天国防":+1},
        "market_bias": +2, "desc": "降息释放流动性，风险资产全面利好", "hold_days_adj": +5,
    },
    "地缘政治冲突": {
        "keywords": ["geopolitical","military conflict","war","invasion","missile strike",
                     "NATO","sanctions","territorial dispute","military escalation","nuclear threat"],
        "sector_impact": {"AI与半导体":0,"加密货币":+1,"新能源":0,"生物医药":0,"科技成长":-1,"航天国防":+2},
        "market_bias": -1, "desc": "地缘紧张推升避险情绪，国防板块受益", "hold_days_adj": -2,
    },
    "AI政策监管": {
        "keywords": ["AI regulation","AI ban","AI safety","AI executive order","AI oversight",
                     "chip export ban","chip restriction","AI compliance","compute restriction","AI ethics law"],
        "sector_impact": {"AI与半导体":-2,"加密货币":0,"新能源":0,"生物医药":0,"科技成长":-1,"航天国防":0},
        "market_bias": -1, "desc": "AI监管收紧影响半导体和科技成长", "hold_days_adj": -3,
    },
    "加密监管": {
        "keywords": ["crypto regulation","SEC crypto","crypto ban","stablecoin regulation",
                     "crypto enforcement","CBDC","crypto crackdown","bitcoin ETF reject","crypto tax"],
        "sector_impact": {"AI与半导体":0,"加密货币":-2,"新能源":0,"生物医药":0,"科技成长":0,"航天国防":0},
        "market_bias": 0, "desc": "加密监管趋严，加密板块承压", "hold_days_adj": -2,
    },
    "财政刺激基建法案": {
        "keywords": ["fiscal stimulus","infrastructure bill","government spending","CHIPS act",
                     "IRA","industrial policy","subsidy","federal investment","economic package","stimulus package"],
        "sector_impact": {"AI与半导体":+2,"加密货币":0,"新能源":+2,"生物医药":0,"科技成长":+1,"航天国防":+1},
        "market_bias": +1, "desc": "财政刺激推动半导体和新能源", "hold_days_adj": +3,
    },
    "通胀CPI数据": {
        "keywords": ["CPI","inflation surge","core inflation","price index","inflation higher than expected",
                     "consumer prices","PPI","inflation acceleration","cost of living","sticky inflation"],
        "sector_impact": {"AI与半导体":-1,"加密货币":0,"新能源":-1,"生物医药":0,"科技成长":-1,"航天国防":0},
        "market_bias": -1, "desc": "通胀超预期压制成长板块", "hold_days_adj": -2,
    },
    "经济衰退信号": {
        "keywords": ["recession","GDP decline","economic contraction","yield curve inversion",
                     "mass layoffs","unemployment surge","consumer confidence drop","PMI contraction","hard landing"],
        "sector_impact": {"AI与半导体":-2,"加密货币":-1,"新能源":-2,"生物医药":+1,"科技成长":-2,"航天国防":+1},
        "market_bias": -2, "desc": "衰退信号，防御板块受益", "hold_days_adj": -5,
    },
    "强劲就业经济数据": {
        "keywords": ["jobs beat","strong employment","GDP growth","consumer spending strong",
                     "retail sales beat","economic expansion","PMI expansion","jobless claims low","goldilocks"],
        "sector_impact": {"AI与半导体":+1,"加密货币":+1,"新能源":+1,"生物医药":+1,"科技成长":+1,"航天国防":+1},
        "market_bias": +1, "desc": "经济数据强劲，全面利好", "hold_days_adj": +2,
    },
}

MACRO_NEWS_SOURCES = [
    {"url": "https://news.google.com/rss/search?q=tariff+trade+war+US+China&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 关税贸易战", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=Federal+Reserve+rate+decision+hawkish+dovish&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 美联储利率", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=US+China+tech+chip+export+ban&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 中美科技", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=geopolitical+conflict+military+NATO&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 地缘政治", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=CPI+inflation+data+consumer+prices&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 通胀数据", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=fiscal+stimulus+infrastructure+spending+bill&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 财政政策", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=AI+regulation+oversight+executive+order&hl=en-US&gl=US&ceid=US:en",
     "name": "Google AI监管", "category": "宏观政策"},
    {"url": "https://news.google.com/rss/search?q=crypto+regulation+SEC+enforcement&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 加密政策", "category": "宏观政策"},
    {"url": "https://feeds.reuters.com/reuters/businessNews",
     "name": "路透社商业(宏观)", "category": "宏观政策"},
    {"url": "https://feeds.reuters.com/reuters/technologyNews",
     "name": "路透社科技(宏观)", "category": "宏观政策"},
]

CONFIG["MACRO_WEIGHT"] = 0.35

# ══════════════════════════════════════════════════════
#  数据存储
# ══════════════════════════════════════════════════════

def load():
    if DATA_FILE.exists():
        d = json.loads(DATA_FILE.read_text("utf-8"))
        # 恢复用户设置的资金量
        if "initial_cash" in d:
            CONFIG["INITIAL_CASH"] = d["initial_cash"]
        return d
    d = {"cash": CONFIG["INITIAL_CASH"], "positions": {}, "trades": [],
         "daily_nav": [], "prices": {}, "sector_scores": {},
         "base_nav": CONFIG["INITIAL_CASH"],
         "initial_cash": CONFIG["INITIAL_CASH"],
         "created": now_pdt().strftime("%Y-%m-%d")}
    save(d); return d

def save(d):
    DATA_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), "utf-8")

# ══════════════════════════════════════════════════════
#  新闻爬取 & 板块情绪分析
# ══════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════
#  新闻源配置（分类，便于溯源）
# ══════════════════════════════════════════════════════

NEWS_SOURCES = [
    # ── 宏观市场（路透社 / MarketWatch / CNBC）──────────
    {"url": "https://feeds.reuters.com/reuters/businessNews",
     "name": "路透社商业", "category": "宏观"},
    {"url": "https://feeds.reuters.com/reuters/technologyNews",
     "name": "路透社科技", "category": "科技"},
    {"url": "https://mw3.wsj.com/mdc/public/page/rss_news.xml",
     "name": "WSJ市场", "category": "宏观"},
    {"url": "https://www.reutersagency.com/feed/?best-topics=tech&post_type=best",
     "name": "路透社Tech", "category": "科技"},

    # ── 雅虎财经（大盘 + 板块 ETF）────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
     "name": "Yahoo S&P500", "category": "宏观"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^IXIC&region=US&lang=en-US",
     "name": "Yahoo 纳指", "category": "宏观"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ&region=US&lang=en-US",
     "name": "Yahoo QQQ", "category": "科技"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US",
     "name": "Yahoo SPY", "category": "宏观"},

    # ── AI / 半导体 ────────────────────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US",
     "name": "Yahoo NVDA", "category": "AI半导体"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AMD&region=US&lang=en-US",
     "name": "Yahoo AMD", "category": "AI半导体"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SMCI&region=US&lang=en-US",
     "name": "Yahoo SMCI", "category": "AI半导体"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AVGO&region=US&lang=en-US",
     "name": "Yahoo AVGO", "category": "AI半导体"},
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence+stock+market&hl=en-US&gl=US&ceid=US:en",
     "name": "Google AI新闻", "category": "AI半导体"},
    {"url": "https://news.google.com/rss/search?q=semiconductor+chip+shortage+2025&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 芯片新闻", "category": "AI半导体"},

    # ── 加密货币 ───────────────────────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US",
     "name": "Yahoo BTC", "category": "加密货币"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=COIN&region=US&lang=en-US",
     "name": "Yahoo COIN", "category": "加密货币"},
    {"url": "https://news.google.com/rss/search?q=bitcoin+crypto+regulation&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 加密监管", "category": "加密货币"},
    {"url": "https://news.google.com/rss/search?q=ethereum+defi+blockchain&hl=en-US&gl=US&ceid=US:en",
     "name": "Google ETH/DeFi", "category": "加密货币"},

    # ── 新能源 ─────────────────────────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ENPH&region=US&lang=en-US",
     "name": "Yahoo ENPH", "category": "新能源"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=FSLR&region=US&lang=en-US",
     "name": "Yahoo FSLR", "category": "新能源"},
    {"url": "https://news.google.com/rss/search?q=solar+energy+EV+clean+energy+stock&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 清洁能源", "category": "新能源"},
    {"url": "https://news.google.com/rss/search?q=electric+vehicle+battery+lithium&hl=en-US&gl=US&ceid=US:en",
     "name": "Google EV/锂电", "category": "新能源"},

    # ── 生物医药 ───────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=FDA+approval+biotech+drug&hl=en-US&gl=US&ceid=US:en",
     "name": "Google FDA/生物技术", "category": "生物医药"},
    {"url": "https://news.google.com/rss/search?q=GLP-1+obesity+drug+clinical+trial&hl=en-US&gl=US&ceid=US:en",
     "name": "Google GLP-1减肥药", "category": "生物医药"},

    # ── 宏观经济 ───────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=Federal+Reserve+interest+rate+inflation&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 美联储", "category": "宏观"},
    {"url": "https://news.google.com/rss/search?q=US+economy+recession+GDP&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 美国经济", "category": "宏观"},
    {"url": "https://news.google.com/rss/search?q=stock+market+earnings+report&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 财报季", "category": "宏观"},

    # ── 科技成长 ───────────────────────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=CRWD&region=US&lang=en-US",
     "name": "Yahoo CRWD", "category": "科技成长"},
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=PLTR&region=US&lang=en-US",
     "name": "Yahoo PLTR", "category": "科技成长"},
    {"url": "https://news.google.com/rss/search?q=cybersecurity+cloud+SaaS+growth&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 网络安全/云", "category": "科技成长"},

    # ── 航天国防 ───────────────────────────────────────
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=RKLB&region=US&lang=en-US",
     "name": "Yahoo RKLB", "category": "航天国防"},
    {"url": "https://news.google.com/rss/search?q=space+rocket+satellite+defense+stock&hl=en-US&gl=US&ceid=US:en",
     "name": "Google 航天国防", "category": "航天国防"},
]

def fetch_all_news():
    """爬取所有新闻源，去重，返回带来源信息的新闻列表"""
    import requests, xml.etree.ElementTree as ET
    items = []
    seen_titles = set()
    ok_count = 0
    fail_count = 0

    for src in NEWS_SOURCES:
        try:
            r = requests.get(src["url"], timeout=7,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
            if r.status_code != 200:
                fail_count += 1; continue
            root = ET.fromstring(r.content)
            fetched = 0
            for i in root.findall(".//item")[:20]:
                title = i.findtext("title", "").strip()
                if not title or title in seen_titles: continue
                seen_titles.add(title)
                items.append({
                    "title":    title,
                    "desc":     i.findtext("description", "")[:300],
                    "source":   src["name"],
                    "category": src["category"],
                    "pub":      i.findtext("pubDate", ""),
                })
                fetched += 1
            ok_count += 1
            time.sleep(0.15)
        except:
            fail_count += 1

    print(f"[新闻] 爬取完成: {ok_count}源成功 {fail_count}源失败 共{len(items)}条去重新闻")
    return items

# ══════════════════════════════════════════════════════
#  宏观政策新闻爬取 & 事件分析
# ══════════════════════════════════════════════════════

def fetch_macro_news():
    """专门爬取宏观政策新闻源"""
    import requests, xml.etree.ElementTree as ET
    items = []
    seen_titles = set()
    ok_count = 0; fail_count = 0
    for src in MACRO_NEWS_SOURCES:
        try:
            r = requests.get(src["url"], timeout=7,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
            if r.status_code != 200:
                fail_count += 1; continue
            root = ET.fromstring(r.content)
            for i in root.findall(".//item")[:15]:
                title = i.findtext("title", "").strip()
                if not title or title in seen_titles: continue
                seen_titles.add(title)
                items.append({
                    "title": title,
                    "desc": i.findtext("description", "")[:300],
                    "source": src["name"],
                    "category": src["category"],
                    "pub": i.findtext("pubDate", ""),
                })
            ok_count += 1
            time.sleep(0.15)
        except:
            fail_count += 1
    print(f"[宏观] 爬取完成: {ok_count}源成功 {fail_count}源失败 共{len(items)}条宏观新闻")
    return items

def analyze_macro_events(macro_news):
    """分析宏观新闻，匹配事件，计算板块调整"""
    triggered_events = []
    sector_adj = {s: 0 for s in SECTORS}
    total_market_bias = 0
    total_hold_adj = 0

    for event_name, ev in MACRO_EVENTS.items():
        kws = [k.lower() for k in ev["keywords"]]
        matched_headlines = []
        for item in macro_news:
            text = (item["title"] + " " + item.get("desc", "")).lower()
            hits = sum(1 for kw in kws if kw in text)
            if hits >= 2:
                sent = score_sentiment(item["title"])
                matched_headlines.append({"title": item["title"][:80], "hits": hits,
                                          "sentiment": sent, "source": item.get("source", "")})
        if not matched_headlines:
            continue
        # 事件触发
        avg_sent = sum(h["sentiment"] for h in matched_headlines) / len(matched_headlines)
        # 情绪修正方向
        direction = 1.0
        if ev["market_bias"] < 0 and avg_sent > 0.2:
            direction = 0.5  # 负面事件但情绪偏正，减半影响
        elif ev["market_bias"] > 0 and avg_sent < -0.2:
            direction = 0.5
        for s, impact in ev["sector_impact"].items():
            sector_adj[s] += int(impact * direction * 5)  # 映射到评分维度
        total_market_bias += ev["market_bias"]
        total_hold_adj += ev["hold_days_adj"]
        triggered_events.append({
            "name": event_name,
            "desc": ev["desc"],
            "market_bias": ev["market_bias"],
            "hold_days_adj": ev["hold_days_adj"],
            "matched_count": len(matched_headlines),
            "avg_sentiment": round(avg_sent, 3),
            "headlines": matched_headlines[:3],
        })

    # 限幅
    total_market_bias = max(-3, min(3, total_market_bias))
    total_hold_adj = max(-8, min(8, total_hold_adj))

    # 找最重大事件
    top_event = max(triggered_events, key=lambda e: e["matched_count"]) if triggered_events else None

    # 一句话总结
    if not triggered_events:
        summary = "宏观面平静，无重大政策事件"
    elif total_market_bias <= -2:
        summary = f"⚠️ 检测到{len(triggered_events)}个利空事件：{'、'.join(e['name'] for e in triggered_events[:3])}，大盘偏空"
    elif total_market_bias >= 2:
        summary = f"✅ 检测到{len(triggered_events)}个利好事件：{'、'.join(e['name'] for e in triggered_events[:3])}，大盘偏多"
    else:
        summary = f"检测到{len(triggered_events)}个事件：{'、'.join(e['name'] for e in triggered_events[:3])}，影响有限"

    return {
        "events": triggered_events,
        "sector_adj": sector_adj,
        "market_bias": total_market_bias,
        "hold_days_adj": total_hold_adj,
        "top_event": {"name": top_event["name"], "desc": top_event["desc"]} if top_event else None,
        "summary": summary,
    }

# ── FinBERT 情绪引擎（单例，首次调用时加载）──────────────

_finbert = None
_finbert_status = "未加载"   # "未加载" | "加载中" | "就绪" | "失败"

def _load_finbert():
    """后台线程加载 FinBERT，加载期间自动降级到 VADER"""
    global _finbert, _finbert_status
    try:
        _finbert_status = "加载中"
        try:
            from transformers import pipeline
            print("[FinBERT] 首次加载模型，约需 1-2 分钟，请稍候...")
            _finbert = pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
                top_k=None,          # 返回全部标签概率
                device=-1,           # CPU（Mac M 系列会自动用 MPS）
                truncation=True,
                max_length=512,
            )
            _finbert_status = "就绪"
            print("[FinBERT] ✅ 模型加载完成，后续分析将使用金融专用模型")
        except Exception as e:
            _finbert_status = "失败"
            print(f"[FinBERT] ⚠️ 加载失败({e})，降级到 VADER")
    except Exception as e:
        _finbert_status = "失败"
        print(f"[FinBERT] ⚠️ 致命错误({e})，降级到 VADER")

def _ensure_finbert():
    """触发 FinBERT 加载（如果还没加载）"""
    global _finbert_status
    if _finbert_status == "未加载":
        t = threading.Thread(target=_load_finbert, daemon=True)
        t.start()

def score_sentiment(text: str) -> float:
    """
    对单条文本打情绪分，返回 -1（看跌）~ +1（看涨）
    优先用 FinBERT，不可用时降级到 VADER
    """
    if _finbert_status == "就绪" and _finbert:
        try:
            results = _finbert(text[:512])[0]   # [{label, score}, ...]
            score_map = {r["label"]: r["score"] for r in results}
            # FinBERT 标签: positive / negative / neutral
            pos = score_map.get("positive", 0)
            neg = score_map.get("negative", 0)
            return round(pos - neg, 3)          # -1 ~ +1
        except:
            pass
    # 降级：VADER
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return round(SentimentIntensityAnalyzer().polarity_scores(text)["compound"], 3)
    except:
        return 0.0

def analyze_sector_sentiment(news_items):
    """
    用 FinBERT（或 VADER）对每条新闻打分
    批量送入 FinBERT 以加速（每批 16 条）
    """
    _ensure_finbert()   # 触发后台加载（非阻塞）

    # 批量预打分（如果 FinBERT 就绪，一次性算完所有标题）
    title_scores = {}
    titles = [item["title"][:512] for item in news_items]

    if _finbert_status == "就绪" and _finbert:
        try:
            BATCH = 16
            all_results = []
            for i in range(0, len(titles), BATCH):
                batch = titles[i:i+BATCH]
                out = _finbert(batch)
                all_results.extend(out)
            for title, res in zip(titles, all_results):
                sm = {r["label"]: r["score"] for r in res}
                title_scores[title] = round(sm.get("positive",0) - sm.get("negative",0), 3)
            print(f"[FinBERT] 批量评分 {len(titles)} 条完成")
        except Exception as e:
            print(f"[FinBERT] 批量评分失败({e})，改逐条用VADER")

    if not title_scores:
        # VADER 逐条
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            va = SentimentIntensityAnalyzer()
            for t in titles:
                title_scores[t] = round(va.polarity_scores(t)["compound"], 3)
        except:
            for t in titles:
                title_scores[t] = 0.0

    # 按板块聚合
    results = {}
    for sector, info in SECTORS.items():
        kws = [k.lower() for k in info["keywords"]]
        scores, headlines = [], []
        for item in news_items:
            text = (item["title"] + " " + item["desc"]).lower()
            hits = sum(1 for kw in kws if kw in text)
            if hits:
                sent = title_scores.get(item["title"][:512], 0.0)
                # 关键词命中越多，权重越高
                scores.append(sent * (1 + hits * 0.4))
                headlines.append({
                    "title":     item["title"][:80],
                    "sentiment": sent,
                    "hits":      hits,
                    "source":    item.get("source",""),
                    "model":     "FinBERT" if _finbert_status=="就绪" else "VADER",
                })
        if scores:
            avg  = sum(scores) / len(scores)
            heat = min(100, int(abs(avg) * 70 + len(scores) * 3))
            adj  = heat if avg >= 0 else int(heat * 0.55)
        else:
            avg = 0.0; heat = 0; adj = 0
        results[sector] = {
            "score":    round(avg, 3),
            "heat":     adj,
            "raw_heat": heat,
            "count":    len(scores),
            "headlines": sorted(headlines, key=lambda x: -(x["hits"] + abs(x["sentiment"])))[:5],
            "label":    "看涨" if avg > 0.1 else "看跌" if avg < -0.1 else "中性",
            "model":    "FinBERT" if _finbert_status=="就绪" else "VADER",
        }
    return dict(sorted(results.items(), key=lambda x: -x[1]["heat"]))

# ══════════════════════════════════════════════════════
#  每日新闻总结生成
# ══════════════════════════════════════════════════════

def generate_daily_summary(news_items: list, sector_scores: dict, top_sectors: list,
                            actions: list) -> dict:
    today = now_pdt().strftime("%Y年%m月%d日")
    now   = now_pdt().strftime("%H:%M")

    # 按板块分组
    by_sector = {}
    for item in news_items:
        by_sector.setdefault(item.get("category","其他"), []).append(item)

    # 高价值新闻（关键词命中 ≥ 2）
    all_kws = [k.lower() for info in SECTORS.values() for k in info["keywords"]]
    top_news = []
    for item in news_items:
        text = (item["title"] + " " + item["desc"]).lower()
        hits = sum(1 for kw in all_kws if kw in text)
        if hits >= 2:
            sent = score_sentiment(item["title"])
            top_news.append({"title": item["title"], "source": item["source"],
                             "hits": hits, "sentiment": sent,
                             "category": item.get("category",""),
                             "model": "FinBERT" if _finbert_status=="就绪" else "VADER"})
    top_news = sorted(top_news, key=lambda x: -(x["hits"] + abs(x["sentiment"])))[:12]

    # 宏观信号
    macro_texts = " ".join(i["title"].lower() for i in news_items if i.get("category")=="宏观")
    macro_signal = "中性"; macro_notes = []
    if any(w in macro_texts for w in ["rate cut","pivot","dovish","easing"]):
        macro_signal="偏多"; macro_notes.append("降息预期升温，风险资产偏利好")
    if any(w in macro_texts for w in ["rate hike","hawkish","tighten","inflation surge"]):
        macro_signal="偏空"; macro_notes.append("通胀/加息预期压制市场")
    if any(w in macro_texts for w in ["recession","gdp decline","layoffs"]):
        macro_signal="偏空"; macro_notes.append("经济衰退信号出现")
    if any(w in macro_texts for w in ["earnings beat","record high","bull","rally"]):
        macro_signal="偏多"; macro_notes.append("企业盈利超预期，市场情绪积极")
    if not macro_notes: macro_notes.append("宏观面无明显方向性信号")

    # 板块解读
    sector_insights = []
    for sector in list(sector_scores.keys()):
        s = sector_scores[sector]
        best = sorted(s.get("headlines",[]), key=lambda x: -(x.get("hits",0)+abs(x.get("sentiment",0))))[:2]
        sector_insights.append({
            "sector":     sector,
            "heat":       s.get("heat",0),
            "label":      s.get("label","中性"),
            "score":      s.get("score",0),
            "count":      s.get("count",0),
            "model":      s.get("model","VADER"),
            "key_news":   [h["title"] for h in best],
            "conclusion": _sector_conclusion(sector, s, best),
            "is_top":     sector in top_sectors,
        })

    action_summary = [{"action":a.get("action",""),"ticker":a.get("ticker",""),
                        "reason":a.get("reason",""),"score":a.get("score",0),
                        "patterns":a.get("patterns",[]),"hold_days":a.get("hold_days",0)}
                       for a in actions]

    return {
        "date":             today,
        "time":             now,
        "total_news":       len(news_items),
        "sources_used":     len(set(i["source"] for i in news_items)),
        "sentiment_model":  "FinBERT" if _finbert_status=="就绪" else "VADER",
        "macro_signal":     macro_signal,
        "macro_notes":      macro_notes,
        "top_sectors":      top_sectors,
        "sector_insights":  sector_insights,
        "top_news":         top_news,
        "actions":          action_summary,
        "one_line":         _one_line_summary(macro_signal, top_sectors, sector_scores, action_summary),
    }

def _sector_conclusion(sector, s, best_headlines):
    """为板块生成一句人话总结"""
    label = s.get("label","中性"); heat = s.get("heat",0); count = s.get("count",0)
    title_hint = best_headlines[0]["title"][:50] if best_headlines else ""
    if heat > 60 and label == "看涨":
        return f"高热度看涨，{count}条相关新闻，正面情绪集中。代表性：「{title_hint}」"
    elif heat > 60 and label == "看跌":
        return f"高热度但偏空，{count}条负面新闻，短期风险较高。代表：「{title_hint}」"
    elif heat > 30:
        return f"中等热度（{heat}），{count}条相关新闻，情绪{label}，值得关注。"
    else:
        return f"热度较低（{heat}），市场关注度不足，暂不布局。"

def _one_line_summary(macro, top_sectors, sector_scores, actions):
    buys  = [a["ticker"] for a in actions if a["action"]=="BUY"]
    sells = [a["ticker"] for a in actions if a["action"] in ("SELL","STOP_LOSS","TAKE_PROFIT","PERIOD_SELL","SIGNAL_SELL")]
    parts = [f"宏观{macro}"]
    if top_sectors:
        parts.append(f"重点板块 {'/'.join(top_sectors)}")
    if buys:
        parts.append(f"买入 {' '.join(buys)}")
    if sells:
        parts.append(f"卖出 {' '.join(sells)}")
    if not buys and not sells:
        parts.append("无操作")
    return "  |  ".join(parts)

# ══════════════════════════════════════════════════════
#  K线形态识别
# ══════════════════════════════════════════════════════

def detect_patterns(df):
    if df is None or len(df) < 5: return {}
    o = df["Open"].values;  h = df["High"].values
    l = df["Low"].values;   c = df["Close"].values
    v = df["Volume"].values
    o1,h1,l1,c1 = float(o[-1]),float(h[-1]),float(l[-1]),float(c[-1])
    o2,h2,l2,c2 = float(o[-2]),float(h[-2]),float(l[-2]),float(c[-2])
    o3,h3,l3,c3 = float(o[-3]),float(h[-3]),float(l[-3]),float(c[-3])
    body1  = abs(c1 - o1); range1 = h1 - l1
    if range1 == 0: return {}
    upper1 = h1 - max(o1,c1); lower1 = min(o1,c1) - l1
    br = body1 / range1
    patterns = {}
    if lower1 > body1*2 and upper1 < body1*0.5 and br < 0.4 and c2 < o2:
        patterns["锤子线"] = {"signal":"bullish","strength":2,"desc":"空方衰竭，买方反攻信号"}
    if lower1 > body1*2 and upper1 < body1*0.5 and br < 0.4 and c2 > o2:
        patterns["上吊线"] = {"signal":"bearish","strength":2,"desc":"高位长下影，警惕回调"}
    if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2:
        patterns["看涨吞没"] = {"signal":"bullish","strength":3,"desc":"阳线吞没前阴，强力反转"}
    if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2:
        patterns["看跌吞没"] = {"signal":"bearish","strength":3,"desc":"阴线吞没前阳，趋势逆转"}
    if br < 0.1:
        patterns["十字星"] = {"signal":"neutral","strength":1,"desc":"多空僵持，等待方向确认"}
    if (c3 < o3 and br < 0.3 and c1 > o1 and c1 > (o3+c3)/2):
        patterns["早晨之星"] = {"signal":"bullish","strength":3,"desc":"三日底部反转"}
    if (c3 > o3 and br < 0.3 and c1 < o1 and c1 < (o3+c3)/2):
        patterns["黄昏之星"] = {"signal":"bearish","strength":3,"desc":"三日顶部见顶"}
    if upper1 > body1*2 and lower1 < body1*0.5 and br < 0.4 and c2 > o2:
        patterns["射击之星"] = {"signal":"bearish","strength":2,"desc":"长上影线，上方压力大"}
    if c1 > c2*1.02 and float(v[-1]) > float(v[-2])*1.5:
        patterns["放量突破"] = {"signal":"bullish","strength":2,"desc":"量价齐升，买盘积极"}
    if c1 < c2*0.98 and float(v[-1]) > float(v[-2])*1.5:
        patterns["放量下跌"] = {"signal":"bearish","strength":2,"desc":"量价齐跌，卖压较重"}
    return patterns

# ══════════════════════════════════════════════════════
#  综合技术分析
# ══════════════════════════════════════════════════════

def full_analysis(ticker, macro_adj=None, sector=None):
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="6mo")
    except: return None
    if df is None or len(df) < 30: return None

    c = df["Close"]; price = float(c.iloc[-1])
    score = 50; reasons = []

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi = float(100 - 100/(1+gain/loss)) if loss else 50
    if   rsi < 30:  score+=18; reasons.append(f"RSI{rsi:.0f} 超卖↑")
    elif rsi < 45:  score+=9;  reasons.append(f"RSI{rsi:.0f} 偏低↑")
    elif rsi > 75:  score-=18; reasons.append(f"RSI{rsi:.0f} 超买↓")
    elif rsi > 65:  score-=8;  reasons.append(f"RSI{rsi:.0f} 偏高↓")

    # MACD
    macd = c.ewm(span=12).mean() - c.ewm(span=26).mean()
    sig  = macd.ewm(span=9).mean()
    hist = float((macd-sig).iloc[-1]); hist_p = float((macd-sig).iloc[-2])
    if   hist>0 and hist>hist_p: score+=12; reasons.append("MACD金叉扩大↑")
    elif hist>0:                 score+=6;  reasons.append("MACD金叉↑")
    elif hist<0 and hist<hist_p: score-=12; reasons.append("MACD死叉扩大↓")
    elif hist<0:                 score-=6;  reasons.append("MACD死叉↓")

    # 均线
    ma5  = float(c.rolling(5).mean().iloc[-1])
    ma10 = float(c.rolling(10).mean().iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    if   price > ma5 > ma10 > ma20 > ma50: score+=15; reasons.append("均线多头排列↑")
    elif price > ma20 and price > ma50:     score+=8;  reasons.append("站稳均线↑")
    elif price < ma5 < ma10 < ma20:         score-=12; reasons.append("均线空头排列↓")
    elif price < ma20 and price < ma50:     score-=8;  reasons.append("跌破均线↓")

    # 布林带
    bb_up = float((c.rolling(20).mean() + 2*c.rolling(20).std()).iloc[-1])
    bb_lo = float((c.rolling(20).mean() - 2*c.rolling(20).std()).iloc[-1])
    if   price <= bb_lo: score+=10; reasons.append("触布林下轨↑")
    elif price >= bb_up: score-=8;  reasons.append("触布林上轨↓")

    # 量比
    vol_avg = float(df["Volume"].rolling(20).mean().iloc[-1])
    vol_now = float(df["Volume"].iloc[-1])
    vol_ratio = vol_now/vol_avg if vol_avg else 1
    pct_1d = float(c.pct_change().iloc[-1]*100)
    if   vol_ratio>2.0 and pct_1d>0: score+=10; reasons.append(f"巨量涨{pct_1d:.1f}%↑")
    elif vol_ratio>1.5 and pct_1d>0: score+=6;  reasons.append(f"放量涨{pct_1d:.1f}%↑")
    elif vol_ratio>1.5 and pct_1d<0: score-=8;  reasons.append(f"放量跌{pct_1d:.1f}%↓")

    # 动量
    pct_5d = float((price/c.iloc[-6]-1)*100)  if len(c)>5  else 0
    pct_1m = float((price/c.iloc[-22]-1)*100) if len(c)>21 else 0
    pct_3m = float((price/c.iloc[-66]-1)*100) if len(c)>65 else 0
    if   pct_1m>15: score+=10; reasons.append(f"月涨{pct_1m:.1f}%↑")
    elif pct_1m>5:  score+=5;  reasons.append(f"月涨{pct_1m:.1f}%↑")
    elif pct_1m<-15:score-=10; reasons.append(f"月跌{pct_1m:.1f}%↓")
    elif pct_1m<-5: score-=5;  reasons.append(f"月跌{pct_1m:.1f}%↓")

    # K线形态
    patterns = detect_patterns(df)
    ps = sum((p["strength"]*4 if p["signal"]=="bullish" else -p["strength"]*4)
             for p in patterns.values())
    score += ps
    bl = [n for n,p in patterns.items() if p["signal"]=="bullish"]
    be = [n for n,p in patterns.items() if p["signal"]=="bearish"]
    if bl: reasons.append(f"K线{'/'.join(bl[:2])}↑")
    if be: reasons.append(f"K线{'/'.join(be[:2])}↓")

    score = max(0, min(100, score))

    # 持仓周期建议
    hold = 14
    if pct_1m>10:  hold+=7
    if pct_3m>20:  hold+=7
    if score>70:   hold+=5
    if rsi>70:     hold-=7
    if pct_1m>25:  hold-=5
    bear_s = sum(p["strength"] for p in patterns.values() if p["signal"]=="bearish")
    bull_s = sum(p["strength"] for p in patterns.values() if p["signal"]=="bullish")
    hold = hold - bear_s*2 + bull_s
    if vol_ratio>2: hold+=3
    hold = max(3, min(45, hold))

    # 持仓理由描述
    hr = []
    if rsi>70:   hr.append("RSI超买，不宜长持")
    if rsi<35:   hr.append("超卖反弹，波段为主")
    if pct_1m>15:hr.append("已有大涨，注意止盈")
    if be:       hr.append(f"{be[0]}出现，考虑缩短")
    if bl:       hr.append(f"{bl[0]}支撑，趋势延续")
    if not hr:   hr.append("指标平稳，按计划持有")

    if   score>=72: signal="强烈买入"
    elif score>=58: signal="买入"
    elif score>=45: signal="持有观望"
    elif score>=32: signal="减仓"
    else:           signal="卖出"

    # 宏观政策加权
    if macro_adj and sector:
        s_adj = macro_adj.get("sector_adj", {}).get(sector, 0)
        macro_bonus = int(s_adj * CONFIG.get("MACRO_WEIGHT", 0.35))
        if macro_bonus != 0:
            score += macro_bonus
            reasons.append(f"宏观政策{'+' if macro_bonus>0 else ''}{macro_bonus}")
        mb = macro_adj.get("market_bias", 0)
        if mb <= -2:
            score -= 8; reasons.append("大盘极度利空-8")
        elif mb >= 2:
            score += 6; reasons.append("大盘强势利好+6")
        hold += macro_adj.get("hold_days_adj", 0)
        hold = max(3, min(30, hold))
        score = max(0, min(100, score))
        # 重新计算信号
        if   score>=72: signal="强烈买入"
        elif score>=58: signal="买入"
        elif score>=45: signal="持有观望"
        elif score>=32: signal="减仓"
        else:           signal="卖出"

    result = {
        "ticker": ticker, "score": score, "signal": signal,
        "price": round(price,2), "reasons": reasons,
        "patterns": {k:{"signal":v["signal"],"strength":v["strength"],"desc":v["desc"]}
                     for k,v in patterns.items()},
        "indicators": {"rsi":round(rsi,1),"macd_hist":round(hist,4),
                       "ma20":round(ma20,2),"ma50":round(ma50,2),
                       "bb_up":round(bb_up,2),"bb_lo":round(bb_lo,2),
                       "vol_ratio":round(vol_ratio,2),
                       "pct_1d":round(pct_1d,2),"pct_5d":round(pct_5d,2),
                       "pct_1m":round(pct_1m,2),"pct_3m":round(pct_3m,2)},
        "suggested_hold_days": hold,
        "hold_reason": "；".join(hr[:2]),
    }
    result["confidence"] = calc_confidence(result)
    return result

# ══════════════════════════════════════════════════════
#  价格工具
# ══════════════════════════════════════════════════════

def get_prices(tickers):
    if not tickers: return {}
    try:
        import yfinance as yf
        prices = {}
        for t in tickers:
            try:
                h = yf.Ticker(t).history(period="2d")
                if not h.empty: prices[t] = round(float(h["Close"].iloc[-1]),2)
            except: pass
            time.sleep(0.08)
        return prices
    except: return {}

# ══════════════════════════════════════════════════════
#  财报日历
# ══════════════════════════════════════════════════════

def get_earnings_calendar(tickers):
    import yfinance as yf
    result = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            if cal is None:
                continue
            # calendar 可能是 dict 或 DataFrame
            if hasattr(cal, 'get'):
                ed = cal.get("Earnings Date")
                if isinstance(ed, list) and ed:
                    ed = ed[0]
            elif hasattr(cal, 'iloc'):
                try:
                    ed = cal.iloc[0, 0]
                except:
                    continue
            else:
                continue
            if ed is None:
                continue
            from datetime import datetime as dt
            if hasattr(ed, 'date'):
                ed_date = ed.date() if hasattr(ed.date, '__call__') else ed.date
            elif isinstance(ed, str):
                ed_date = dt.strptime(ed[:10], "%Y-%m-%d").date()
            else:
                continue
            days_away = (ed_date - dt.now().date()).days
            if days_away < 0:
                continue
            if days_away <= 1:
                warning = "🚨 明日财报，高风险"
            elif days_away <= 3:
                warning = "⚠️ 3日内财报，谨慎"
            elif days_away <= 7:
                warning = "📅 本周财报，留意"
            else:
                warning = ""
            result[t] = {"date": str(ed_date), "days_away": days_away, "warning": warning}
        except:
            pass
        time.sleep(0.05)
    return result

# ══════════════════════════════════════════════════════
#  盘前盘后价格监控
# ══════════════════════════════════════════════════════

def get_premarket_data(tickers):
    import yfinance as yf
    from datetime import datetime as dt
    try:
        import pytz
        et = pytz.timezone("America/New_York")
        now_et = dt.now(et)
        hour = now_et.hour
    except:
        hour = dt.now().hour
    if 4 <= hour < 9:
        session = "盘前"
    elif 16 <= hour < 20:
        session = "盘后"
    else:
        session = "盘中"
    result = {}
    for t in tickers:
        try:
            h = yf.Ticker(t).history(period="2d", interval="1m", prepost=True)
            if h.empty:
                continue
            pre_price = round(float(h["Close"].iloc[-1]), 2)
            # 昨收：前一天最后一个非NaN收盘价
            dates = h.index.normalize().unique()
            if len(dates) >= 2:
                prev_day = h[h.index.normalize() == dates[-2]]
                if not prev_day.empty:
                    prev_close = float(prev_day["Close"].iloc[-1])
                else:
                    prev_close = pre_price
            else:
                prev_close = pre_price
            change_pct = round((pre_price - prev_close) / prev_close * 100, 2) if prev_close else 0
            result[t] = {"pre_price": pre_price, "pre_change_pct": change_pct, "session": session}
        except:
            pass
        time.sleep(0.05)
    return result

# ══════════════════════════════════════════════════════
#  信号置信度评估
# ══════════════════════════════════════════════════════

def calc_confidence(analysis):
    bullish = 0; bearish = 0; neutral = 0
    ind = analysis.get("indicators", {})
    reasons = analysis.get("reasons", [])

    # RSI
    rsi = ind.get("rsi", 50)
    if rsi < 40: bullish += 1
    elif rsi > 65: bearish += 1
    else: neutral += 1

    # MACD
    macd_hist = ind.get("macd_hist", 0)
    if macd_hist > 0: bullish += 1
    elif macd_hist < 0: bearish += 1
    else: neutral += 1

    # 均线
    price = analysis.get("price", 0)
    ma20 = ind.get("ma20", 0); ma50 = ind.get("ma50", 0)
    if price > ma20 and price > ma50: bullish += 1
    elif price < ma20 and price < ma50: bearish += 1
    else: neutral += 1

    # 月动量
    pct_1m = ind.get("pct_1m", 0)
    if pct_1m > 5: bullish += 1
    elif pct_1m < -5: bearish += 1
    else: neutral += 1

    # K线形态
    patterns = analysis.get("patterns", {})
    bull_p = any(v.get("signal") == "bullish" for v in patterns.values())
    bear_p = any(v.get("signal") == "bearish" for v in patterns.values())
    if bull_p and not bear_p: bullish += 1
    elif bear_p and not bull_p: bearish += 1
    else: neutral += 1

    # 新闻/宏观（从 reasons 推断）
    reasons_text = " ".join(reasons).lower()
    if any(w in reasons_text for w in ["板块热度", "↑", "看涨", "金叉", "多头", "突破"]):
        bullish += 1
    elif any(w in reasons_text for w in ["↓", "看跌", "死叉", "空头", "下跌"]):
        bearish += 1
    else:
        neutral += 1

    total = bullish + bearish + neutral
    dominant = max(bullish, bearish, neutral)
    consensus = dominant / total if total else 0
    # 数量加成：信号越多越可信
    count_bonus = 1.0 + (bullish + bearish) * 0.05
    score = min(100, int(consensus * 100 * count_bonus))

    if bullish > bearish and score >= 70:
        level = "高置信看涨"
    elif bullish > bearish and score >= 50:
        level = "中等置信看涨"
    elif bearish > bullish and score >= 70:
        level = "高置信看跌"
    elif bearish > bullish and score >= 50:
        level = "中等置信看跌"
    elif bullish == bearish:
        level = "多空分歧"
    else:
        level = "低置信"

    return {"score": score, "level": level, "bullish": bullish, "bearish": bearish}

# ══════════════════════════════════════════════════════
#  动态仓位计算
# ══════════════════════════════════════════════════════

def calc_position_size(total_capital, score, confidence, current_positions, max_positions):
    base = total_capital * 0.12

    # 置信度系数
    if confidence >= 80: cf = 1.5
    elif confidence >= 70: cf = 1.25
    elif confidence >= 60: cf = 1.0
    elif confidence >= 50: cf = 0.8
    else: cf = 0.6

    # 评分系数
    if score >= 80: sf = 1.2
    elif score >= 70: sf = 1.0
    else: sf = 0.85

    # 集中度系数
    conc = 1.0 - (current_positions / max_positions) * 0.3

    size = base * cf * sf * conc
    # 硬限制
    size = max(total_capital * 0.05, min(total_capital * 0.18, size))
    return round(size, 2)

# ══════════════════════════════════════════════════════
#  板块集中度控制
# ══════════════════════════════════════════════════════

def sector_count(positions):
    counts = {}
    for t, p in positions.items():
        if p.get("source","local") != "local": continue
        s = p.get("sector", "")
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts

def is_sector_full(positions, sector):
    return sector_count(positions).get(sector, 0) >= MAX_PER_SECTOR

# ══════════════════════════════════════════════════════
#  移动止损
# ══════════════════════════════════════════════════════

def update_trailing_stop(positions, prices):
    triggered = []
    for ticker, p in positions.items():
        if p.get("source","local") != "local": continue
        price = prices.get(ticker)
        if not price:
            continue
        peak = p.get("peak_price", p["avg_cost"])
        if price > peak:
            peak = price
            p["peak_price"] = peak
        drawdown = (price - peak) / peak if peak else 0
        p["trailing_drawdown"] = round(drawdown, 4)
        if drawdown <= -TRAILING_STOP_PCT:
            triggered.append(ticker)
    return triggered

# ══════════════════════════════════════════════════════
#  历史回测系统
# ══════════════════════════════════════════════════════

def run_backtest(tickers, period_days=180, initial_cash=100000):
    import yfinance as yf
    import numpy as np

    # 拉取历史数据
    end = now_pdt()
    start = end - timedelta(days=period_days + 30)  # 多拉30天给指标预热
    data_frames = {}
    for t in tickers:
        try:
            df = yf.Ticker(t).history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if df is not None and len(df) >= 30:
                data_frames[t] = df
        except:
            pass
        time.sleep(0.1)

    if not data_frames:
        return {"error": "无法获取任何标的数据"}

    # 找出共同交易日范围
    all_dates = None
    for df in data_frames.values():
        idx = set(df.index.strftime("%Y-%m-%d"))
        all_dates = idx if all_dates is None else all_dates & idx
    if not all_dates or len(all_dates) < 20:
        return {"error": "共同交易日不足"}

    all_dates = sorted(all_dates)
    # 截取到 period_days 范围内
    if len(all_dates) > period_days:
        all_dates = all_dates[-period_days:]

    cash = initial_cash
    positions = {}  # {ticker: {shares, cost, peak_price}}
    trades = []
    nav_series = [{"date": all_dates[0], "nav": initial_cash}]
    commission_rate = 0.001

    def _calc_score(df, idx):
        """简化版评分（RSI、MACD、均线、动量）"""
        if idx < 30:
            return 50
        c = df["Close"].iloc[:idx+1]
        price = float(c.iloc[-1])
        score = 50

        # RSI
        delta = c.diff()
        gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
        rsi = float(100 - 100/(1+gain/loss)) if loss else 50
        if rsi < 30: score += 15
        elif rsi < 45: score += 7
        elif rsi > 75: score -= 15
        elif rsi > 65: score -= 7

        # MACD
        macd = c.ewm(span=12).mean() - c.ewm(span=26).mean()
        sig = macd.ewm(span=9).mean()
        hist = float((macd - sig).iloc[-1])
        if hist > 0: score += 10
        else: score -= 10

        # 均线
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else ma20
        if price > ma20 and price > ma50: score += 10
        elif price < ma20 and price < ma50: score -= 10

        # 动量
        if len(c) >= 22:
            pct_1m = (price / float(c.iloc[-22]) - 1) * 100
            if pct_1m > 5: score += 8
            elif pct_1m < -5: score -= 8

        return max(0, min(100, score))

    # 获取所有标的的 date->index 映射
    ticker_date_idx = {}
    for t, df in data_frames.items():
        date_strs = df.index.strftime("%Y-%m-%d")
        ticker_date_idx[t] = {d: i for i, d in enumerate(date_strs)}

    # 模拟交易
    for day_i, date in enumerate(all_dates):
        # 每5个交易日评估
        if day_i % 5 != 0 and day_i != len(all_dates) - 1:
            # 但每天检查止损
            for ticker in list(positions.keys()):
                if ticker not in ticker_date_idx or date not in ticker_date_idx[ticker]:
                    continue
                idx = ticker_date_idx[ticker][date]
                price = float(data_frames[ticker]["Close"].iloc[idx])
                p = positions[ticker]
                # 更新峰值
                if price > p["peak_price"]:
                    p["peak_price"] = price
                pnl_pct = (price - p["cost"]) / p["cost"]
                trail_dd = (price - p["peak_price"]) / p["peak_price"]
                # 止损 / 移动止损 / 止盈
                reason = None
                if pnl_pct <= -0.07:
                    reason = "STOP_LOSS"
                elif trail_dd <= -0.08:
                    reason = "TRAILING_STOP"
                elif pnl_pct >= 0.20:
                    reason = "TAKE_PROFIT"
                if reason:
                    net = p["shares"] * price * (1 - commission_rate)
                    pnl = net - p["shares"] * p["cost"]
                    cash += net
                    trades.append({"date": date, "ticker": ticker, "action": reason,
                                   "price": round(price, 2), "pnl": round(pnl, 2),
                                   "pnl_pct": round(pnl_pct * 100, 2)})
                    del positions[ticker]
            # 记录净值
            pos_val = sum(p["shares"] * float(data_frames[t]["Close"].iloc[ticker_date_idx[t][date]])
                          for t, p in positions.items() if t in ticker_date_idx and date in ticker_date_idx[t])
            nav_series.append({"date": date, "nav": round(cash + pos_val, 2)})
            continue

        # 检查持仓止损/止盈
        for ticker in list(positions.keys()):
            if ticker not in ticker_date_idx or date not in ticker_date_idx[ticker]:
                continue
            idx = ticker_date_idx[ticker][date]
            price = float(data_frames[ticker]["Close"].iloc[idx])
            p = positions[ticker]
            if price > p["peak_price"]:
                p["peak_price"] = price
            pnl_pct = (price - p["cost"]) / p["cost"]
            trail_dd = (price - p["peak_price"]) / p["peak_price"]
            reason = None
            if pnl_pct <= -0.07:
                reason = "STOP_LOSS"
            elif trail_dd <= -0.08:
                reason = "TRAILING_STOP"
            elif pnl_pct >= 0.20:
                reason = "TAKE_PROFIT"
            if reason:
                net = p["shares"] * price * (1 - commission_rate)
                pnl = net - p["shares"] * p["cost"]
                cash += net
                trades.append({"date": date, "ticker": ticker, "action": reason,
                               "price": round(price, 2), "pnl": round(pnl, 2),
                               "pnl_pct": round(pnl_pct * 100, 2)})
                del positions[ticker]

        # 扫描买入机会
        scored = []
        for t in tickers:
            if t in positions:
                continue
            if t not in ticker_date_idx or date not in ticker_date_idx[t]:
                continue
            idx = ticker_date_idx[t][date]
            s = _calc_score(data_frames[t], idx)
            if s >= 60:
                scored.append((t, s, idx))
        scored.sort(key=lambda x: -x[1])

        for t, s, idx in scored:
            if len(positions) >= 6:
                break
            price = float(data_frames[t]["Close"].iloc[idx])
            usd = cash * 0.12
            if usd < cash * 0.03:
                break
            comm = usd * commission_rate
            if cash < usd + comm:
                continue
            shares = usd / price
            cash -= (usd + comm)
            positions[t] = {"shares": shares, "cost": price, "peak_price": price}
            trades.append({"date": date, "ticker": t, "action": "BUY",
                           "price": round(price, 2), "pnl": 0, "pnl_pct": 0})

        # 净值
        pos_val = sum(p["shares"] * float(data_frames[t]["Close"].iloc[ticker_date_idx[t][date]])
                      for t, p in positions.items() if t in ticker_date_idx and date in ticker_date_idx[t])
        nav_series.append({"date": date, "nav": round(cash + pos_val, 2)})

    # 统计
    navs = [n["nav"] for n in nav_series]
    total_return = (navs[-1] - initial_cash) / initial_cash * 100
    trading_days = len(all_dates)
    annualized = ((navs[-1] / initial_cash) ** (252 / max(trading_days, 1)) - 1) * 100

    # 最大回撤
    peak_nav = navs[0]
    max_dd = 0
    for n in navs:
        if n > peak_nav:
            peak_nav = n
        dd = (n - peak_nav) / peak_nav * 100
        if dd < max_dd:
            max_dd = dd

    # 夏普比率
    if len(navs) > 1:
        returns = [(navs[i] - navs[i-1]) / navs[i-1] for i in range(1, len(navs))]
        avg_r = sum(returns) / len(returns)
        std_r = (sum((r - avg_r) ** 2 for r in returns) / len(returns)) ** 0.5
        ann_ret = avg_r * 252
        ann_vol = std_r * (252 ** 0.5)
        rf = 0.05  # 无风险利率5%
        sharpe = round((ann_ret - rf) / ann_vol, 2) if ann_vol > 0 else 0
    else:
        sharpe = 0

    # 胜率
    closed = [t for t in trades if t["action"] != "BUY"]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    return {
        "total_return": round(total_return, 2),
        "annualized": round(annualized, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": sharpe,
        "win_rate": win_rate,
        "nav_series": nav_series,
        "recent_trades": trades[-30:],
        "total_trades": len(trades),
        "tickers_used": list(data_frames.keys()),
        "trading_days": trading_days,
    }

# ══════════════════════════════════════════════════════
#  交易执行
# ══════════════════════════════════════════════════════

def local_positions(data):
    """返回只含 source=local 的持仓字典"""
    return {k:v for k,v in data["positions"].items() if v.get("source","local")=="local"}

def local_pos_count(data):
    return sum(1 for v in data["positions"].values() if v.get("source","local")=="local")

def _safe_price(prices, ticker, fallback):
    """获取价格，跳过 NaN"""
    p = prices.get(ticker, fallback)
    try:
        p = float(p)
        return p if not math.isnan(p) else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)

def portfolio_value(data, prices):
    """只计算 source=local 的持仓市值 + 现金"""
    cash = float(data.get("cash", 0) or 0)
    mkt = sum(v["shares"]*_safe_price(prices,k,v["avg_cost"]) for k,v in data["positions"].items()
              if v.get("source","local")=="local")
    val = cash + mkt
    return val if not math.isnan(val) else cash

def sim_buy(data, ticker, price, usd, analysis):
    # 板块集中度双重检查
    sector = analysis.get("sector", "")
    if sector and is_sector_full(data["positions"], sector):
        return False, f"板块[{sector}]已达上限({MAX_PER_SECTOR}只)"
    comm = usd*CONFIG["COMMISSION"]
    if data["cash"] < usd+comm: return False,"现金不足"
    shares = usd/price
    if ticker in data["positions"]:
        p=data["positions"][ticker]; ns=p["shares"]+shares
        p["avg_cost"]=(p["shares"]*p["avg_cost"]+shares*price)/ns; p["shares"]=ns
        p["target_sell_date"]=(now_pdt()+timedelta(days=analysis.get("suggested_hold_days",14))).strftime("%Y-%m-%d")
    else:
        conf = analysis.get("confidence", {})
        data["positions"][ticker]={
            "shares":shares,"avg_cost":price,
            "buy_date":now_pdt().strftime("%Y-%m-%d"),
            "target_sell_date":(now_pdt()+timedelta(days=analysis.get("suggested_hold_days",14))).strftime("%Y-%m-%d"),
            "hold_reason":analysis.get("hold_reason",""),
            "buy_score":analysis.get("score",0),
            "sector":analysis.get("sector",""),
            "peak_price": price,
            "trailing_drawdown": 0.0,
            "confidence_score": conf.get("score", 0),
            "confidence_level": conf.get("level", ""),
            "source": "local",
        }
    data["cash"]-=(usd+comm)
    data["trades"].append({"time":now_pdt().strftime("%Y-%m-%d %H:%M"),
        "action":"BUY","ticker":ticker,"shares":round(shares,4),"price":price,
        "amount":round(usd,2),"commission":round(comm,2),"pnl":0,"pnl_pct":0,
        "score":analysis.get("score",0),
        "patterns":list(analysis.get("patterns",{}).keys()),
        "sector":analysis.get("sector",""),
        "hold_days":analysis.get("suggested_hold_days",14)})
    # Alpaca 模拟盘同步下单
    if alpaca_enabled:
        try:
            acct = alpaca_get_account()
            bp = float(acct.get("buying_power", 0))
            if bp < 1000:
                print(f"[Alpaca] Alpaca资金不足（${bp:,.2f}），跳过")
            else:
                alpaca_usd = usd
                if bp < usd:
                    alpaca_usd = bp * 0.95
                    print(f"[Alpaca] 可用${bp:,.2f} < 目标${usd:,.2f}，调整为${alpaca_usd:,.2f}")
                order = alpaca_place_order(ticker, "buy", alpaca_usd)
                if "id" in order:
                    data["positions"][ticker]["alpaca_order_id"] = order["id"]
                    print(f"[Alpaca] {ticker} 下单成功 order_id={order['id']}")
                elif "error" in order:
                    print(f"[Alpaca] {ticker} 下单失败: {order['error']}")
        except Exception as e:
            print(f"[Alpaca] {ticker} 下单异常: {e}")
    return True,f"买入 {ticker} {shares:.4f}股 @ ${price:.2f} 仓位${usd:,.0f}"

def sim_sell(data, ticker, price, reason="SELL"):
    if ticker not in data["positions"]: return False,"未持仓"
    p=data["positions"][ticker]
    net=p["shares"]*price*(1-CONFIG["COMMISSION"])
    pnl=net-p["shares"]*p["avg_cost"]
    pnl_pct=pnl/(p["shares"]*p["avg_cost"])*100
    hold_days=(now_pdt()-datetime.strptime(p.get("buy_date",now_pdt().strftime("%Y-%m-%d")),"%Y-%m-%d").replace(tzinfo=_PDT)).days
    if p.get("source","local")=="local":
        data["cash"]+=net
    data["trades"].append({"time":now_pdt().strftime("%Y-%m-%d %H:%M"),
        "action":reason,"ticker":ticker,"shares":round(p["shares"],4),
        "price":price,"amount":round(net,2),"pnl":round(pnl,2),
        "pnl_pct":round(pnl_pct,2),"hold_days":hold_days,"sector":p.get("sector","")})
    # Alpaca 模拟盘同步清仓
    if alpaca_enabled:
        try:
            result = alpaca_close_position(ticker)
            if "error" in (result or {}):
                print(f"[Alpaca] {ticker} 平仓失败: {result['error']}，继续更新本地记录")
        except Exception as e:
            print(f"[Alpaca] {ticker} 平仓异常: {e}，继续更新本地记录")
    del data["positions"][ticker]
    return True,f"卖出 {ticker} 盈亏${pnl:+.2f}({pnl_pct:+.1f}%) 持{hold_days}天"

# ══════════════════════════════════════════════════════
#  主交易周期
# ══════════════════════════════════════════════════════

scan_status={"running":False,"log":[],"progress":0,"total":0,
             "phase":"","sector_scores":{},"analyses":[]}

def alpaca_sync_positions():
    """同步 Alpaca 持仓到本地（扫描前调用）。只更新 source=alpaca 的记录，不影响 local。"""
    if not alpaca_enabled:
        return
    try:
        data = load()
        ap = alpaca_get_positions()
        if isinstance(ap, dict) and "error" in ap:
            print(f"[Alpaca同步] 获取持仓失败: {ap['error']}")
            return
        alpaca_tickers = set()
        for pos in ap:
            ticker = pos.get("symbol", "")
            if not ticker:
                continue
            alpaca_tickers.add(ticker)
            price = float(pos.get("current_price", 0))
            shares = float(pos.get("qty", 0))
            avg_cost = float(pos.get("avg_entry_price", 0))
            # 用 alpaca_ 前缀的 key 存 Alpaca 持仓，不覆盖 local 持仓
            akey = f"_alpaca_{ticker}"
            if akey in data["positions"]:
                data["positions"][akey]["shares"] = shares
                data["positions"][akey]["avg_cost"] = avg_cost
                data["positions"][akey]["peak_price"] = max(data["positions"][akey].get("peak_price", price), price)
            elif ticker in data["positions"] and data["positions"][ticker].get("source") == "alpaca":
                # 迁移旧格式的 alpaca 记录
                old = data["positions"].pop(ticker)
                old["shares"] = shares
                old["avg_cost"] = avg_cost
                data["positions"][akey] = old
            else:
                # 新 Alpaca 持仓（不是本地买入的）
                # 跳过已有 local 持仓的同名 ticker（本地下单的已同步到 Alpaca）
                if ticker in data["positions"] and data["positions"][ticker].get("source","local") == "local":
                    data["prices"][ticker] = price
                    continue
                data["positions"][akey] = {
                    "shares": shares, "avg_cost": avg_cost,
                    "buy_date": now_pdt().strftime("%Y-%m-%d"),
                    "target_sell_date": (now_pdt() + timedelta(days=14)).strftime("%Y-%m-%d"),
                    "hold_reason": "Alpaca同步", "buy_score": 0, "sector": "",
                    "peak_price": price, "trailing_drawdown": 0.0,
                    "confidence_score": 0, "confidence_level": "",
                    "source": "alpaca",
                }
                print(f"[Alpaca同步] 新增持仓 {ticker} {shares}股 @ ${avg_cost:.2f}")
            data["prices"][akey] = price
            data["prices"][ticker] = price
        # 移除 Alpaca 已清仓的 source=alpaca 记录
        for t in list(data["positions"].keys()):
            if data["positions"][t].get("source") == "alpaca":
                real_ticker = t.replace("_alpaca_", "")
                if real_ticker not in alpaca_tickers:
                    print(f"[Alpaca同步] {real_ticker} 已在Alpaca平仓，同步移除")
                    del data["positions"][t]
        save(data)
    except Exception as e:
        print(f"[Alpaca同步] 出错: {e}")

def run_premarket_analysis():
    """盘前准备：只爬新闻分析宏观，不下单"""
    print(f"[盘前准备 {now_pdt():%H:%M:%S} PDT] 开始盘前新闻分析...")
    try:
        news = fetch_all_news()
        macro_news = fetch_macro_news()
        data = load()
        sector_scores = analyze_sector_sentiment(news)
        data["sector_scores"] = sector_scores
        macro_adj = analyze_macro_events(macro_news)
        data["macro_adj"] = macro_adj
        sorted_s = sorted(sector_scores.items(), key=lambda x: -x[1]["heat"])
        top_sectors = [n for n, _ in sorted_s[:CONFIG["TOP_SECTORS"]]]
        bias_text = "利空" if macro_adj["market_bias"] < 0 else "利好" if macro_adj["market_bias"] > 0 else "中性"
        data["pre_market_analysis"] = {
            "time": now_pdt().strftime("%Y-%m-%d %H:%M PDT"),
            "news_count": len(news),
            "macro_news_count": len(macro_news),
            "macro_bias": macro_adj["market_bias"],
            "macro_summary": macro_adj.get("summary", ""),
            "top_sectors": top_sectors,
            "events": [e["name"] for e in macro_adj.get("events", [])],
        }
        save(data)
        print(f"[盘前准备] 完成 | {len(news)}条新闻 | 宏观{bias_text} | {macro_adj['summary']}")
    except Exception as e:
        print(f"[盘前准备] 出错: {e}")

def run_cycle_bg():
    global scan_status
    scan_status={"running":True,"log":[],"progress":0,"total":0,
                 "phase":"爬取新闻","sector_scores":{},"analyses":[]}

    def log(msg,t="info"):
        scan_status["log"].append({"msg":msg,"type":t,"time":now_pdt().strftime("%H:%M:%S")})
        print(f"[{now_pdt():%H:%M:%S} PDT] {msg}")

    # Alpaca 持仓同步（不覆盖本地资金）
    if alpaca_enabled:
        log("🔄 同步 Alpaca 持仓...","info")
        alpaca_sync_positions()

    data=load()

    # 自动交易开关检查
    auto_enabled = data.get("auto_trade_enabled", True)
    if not auto_enabled:
        log("⏸️ 自动交易已暂停，仅分析模式","warning")

    # Phase 1: 新闻 & 板块分析
    log("📰 爬取财经新闻...")
    news=fetch_all_news()
    log(f"获取 {len(news)} 条新闻","info")
    sector_scores=analyze_sector_sentiment(news)
    data["sector_scores"]=sector_scores
    scan_status["sector_scores"]=sector_scores
    sorted_s=sorted(sector_scores.items(),key=lambda x:-x[1]["heat"])
    for name,s in sorted_s:
        e="🔥" if s["heat"]>50 else "📊" if s["heat"]>20 else "💤"
        log(f"{e} {name}: 热度{s['heat']} {s['label']} ({s['count']}条)","info")
    top_sectors=[n for n,_ in sorted_s[:CONFIG["TOP_SECTORS"]]]
    log(f"🎯 重点板块: {' | '.join(top_sectors)}","success")

    # Phase 1.5: 宏观政策事件分析
    scan_status["phase"]="宏观政策分析"
    log("🏛️ 爬取宏观政策新闻...")
    macro_news = fetch_macro_news()
    log(f"获取 {len(macro_news)} 条宏观新闻","info")
    macro_adj = analyze_macro_events(macro_news)
    data["macro_adj"] = macro_adj
    scan_status["macro_adj"] = macro_adj
    if macro_adj["events"]:
        for ev in macro_adj["events"]:
            bias_tag = "🔴" if ev["market_bias"]<0 else "🟢" if ev["market_bias"]>0 else "⚪"
            log(f"  {bias_tag} {ev['name']}: {ev['desc']} (命中{ev['matched_count']}条 情绪{ev['avg_sentiment']:+.2f})","warning" if ev["market_bias"]<0 else "success")
    log(f"📊 {macro_adj['summary']}","warning" if macro_adj["market_bias"]<0 else "success" if macro_adj["market_bias"]>0 else "info")
    # 动态调整买入门槛
    orig_min_score = CONFIG["MIN_SCORE"]
    if macro_adj["market_bias"] <= -2:
        CONFIG["MIN_SCORE"] = 68
        log(f"⚠️ 大盘极度利空，买入门槛提高到 {CONFIG['MIN_SCORE']} 分","warning")
    elif macro_adj["market_bias"] <= -1:
        CONFIG["MIN_SCORE"] = 63
        log(f"⚠️ 大盘偏空，买入门槛提高到 {CONFIG['MIN_SCORE']} 分","warning")
    else:
        CONFIG["MIN_SCORE"] = 58

    # Phase 2: 检查持仓
    scan_status["phase"]="检查持仓"
    held_tickers = [t for t,p in data["positions"].items() if p.get("source","local")=="local"]
    held_prices=get_prices(held_tickers)
    data["prices"].update(held_prices)
    total=portfolio_value(data,data["prices"])

    # 财报日历
    earnings_cal = {}
    if held_tickers:
        log("📅 查询持仓财报日历...")
        earnings_cal = get_earnings_calendar(held_tickers)
        for t, ec in earnings_cal.items():
            if t in data["positions"]:
                data["positions"][t]["earnings_date"] = ec["date"]
                data["positions"][t]["earnings_warning"] = ec["warning"]
            if ec["warning"]:
                log(f"  {t} {ec['warning']} (距财报{ec['days_away']}天)","warning")

    # 盘前盘后监控
    if held_tickers:
        log("📊 盘前盘后价格监控...")
        premarket = get_premarket_data(held_tickers)
        for t, pm in premarket.items():
            arrow = "↑" if pm["pre_change_pct"] > 0 else "↓"
            color_tag = "success" if pm["pre_change_pct"] > 0 else "danger"
            if abs(pm["pre_change_pct"]) > 3:
                log(f"  {t} {pm['session']} ${pm['pre_price']} {arrow}{pm['pre_change_pct']:+.2f}% ⚠️ 波动较大", color_tag)

    # 移动止损
    trailing_triggered = update_trailing_stop(data["positions"], held_prices)
    for ticker in trailing_triggered:
        price = held_prices.get(ticker)
        if price:
            dd = data["positions"][ticker].get("trailing_drawdown", 0) * 100
            ok, msg = sim_sell(data, ticker, price, "TRAILING_STOP")
            log(f"📉 移动止损: {msg} (从最高回撤{dd:.1f}%)", "danger")

    # 宏观利空时收紧止损线
    effective_stop = CONFIG["STOP_LOSS"]
    if macro_adj["market_bias"] <= -2:
        effective_stop = CONFIG["STOP_LOSS"] * 0.7  # -4.9% instead of -7%
        log(f"⚠️ 宏观极度利空，止损线收紧至 {effective_stop*100:.1f}%","warning")

    for ticker in [t for t in list(data["positions"].keys()) if data["positions"][t].get("source","local")=="local"]:
        price=held_prices.get(ticker)
        if not price: continue
        p=data["positions"][ticker]
        pnl_pct=(price-p["avg_cost"])/p["avg_cost"]
        if pnl_pct<=effective_stop:
            ok,msg=sim_sell(data,ticker,price,"STOP_LOSS"); log(f"⚡ 止损: {msg}","danger"); continue
        if pnl_pct>=CONFIG["TAKE_PROFIT"]:
            ok,msg=sim_sell(data,ticker,price,"TAKE_PROFIT"); log(f"🎯 止盈: {msg}","success"); continue
        # 宏观极度利空 + 板块受损 + 浮亏 → 重新评估
        pos_sector = p.get("sector", "")
        if pos_sector and macro_adj["sector_adj"].get(pos_sector, 0) <= -10 and pnl_pct < 0:
            log(f"🏛️ {ticker} 板块[{pos_sector}]受宏观冲击，重新评估...","warning")
            a = full_analysis(ticker, macro_adj=macro_adj, sector=pos_sector)
            if a and a["score"] < 38:
                ok, msg = sim_sell(data, ticker, price, "MACRO_SELL")
                log(f"🏛️ 宏观卖出: {msg}","danger")
                continue
        # 财报当天跳过评估
        ec = earnings_cal.get(ticker, {})
        if ec.get("days_away") == 0:
            log(f"📅 {ticker} 今日财报，跳过评估","warning")
            continue
        td=p.get("target_sell_date","")
        if td and now_pdt().strftime("%Y-%m-%d")>=td:
            log(f"📅 {ticker} 到期重新评估...","info")
            a=full_analysis(ticker, macro_adj=macro_adj, sector=pos_sector)
            if a:
                if a["score"]<45:
                    ok,msg=sim_sell(data,ticker,price,"PERIOD_SELL"); log(f"📤 期满离场: {msg}","warning")
                else:
                    extra=a["suggested_hold_days"]
                    data["positions"][ticker]["target_sell_date"]=(now_pdt()+timedelta(days=extra)).strftime("%Y-%m-%d")
                    data["positions"][ticker]["hold_reason"]=a.get("hold_reason","")
                    log(f"🔄 {ticker} 评分{a['score']}，延期{extra}天","success")
            time.sleep(0.3)

    # Phase 3: 扫描热门板块
    scan_status["phase"]="分析标的"
    candidates=[]
    # 预获取所有扫描标的的财报日历
    all_scan_tickers = []
    for sname in top_sectors:
        all_scan_tickers.extend(SECTORS[sname]["tickers"])
    scan_earnings = get_earnings_calendar(all_scan_tickers) if all_scan_tickers else {}

    for sname in top_sectors:
        tickers=SECTORS[sname]["tickers"]
        log(f"📡 扫描 {sname} ({len(tickers)}只)...")
        scan_status["total"]=scan_status.get("total",0)+len(tickers)
        for ticker in tickers:
            if ticker in data["positions"]: continue
            scan_status["progress"]=scan_status.get("progress",0)+1
            # 板块集中度检查
            if is_sector_full(data["positions"], sname):
                log(f"  {ticker:<6} 跳过：板块[{sname}]已达上限({MAX_PER_SECTOR}只)","warning")
                continue
            # 财报3天内跳过
            sec = scan_earnings.get(ticker, {})
            if sec.get("days_away") is not None and sec["days_away"] <= 3:
                log(f"  {ticker:<6} 跳过：{sec.get('warning','')} 距财报{sec['days_away']}天","warning")
                continue
            a=full_analysis(ticker, macro_adj=macro_adj, sector=sname)
            if not a: continue
            a["sector"]=sname
            bonus=int(sector_scores.get(sname,{}).get("heat",0)*0.15)
            a["score"]=min(100,a["score"]+bonus)
            if bonus>0: a["reasons"].append(f"板块热度+{bonus}")
            # 置信度处理
            conf = a.get("confidence", {})
            if conf.get("bullish", 0) == conf.get("bearish", 0) and conf.get("bullish", 0) > 0:
                a["score"] = max(0, a["score"] - 8)
                a["reasons"].append("多空分歧-8")
            if conf.get("score", 100) < 45:
                log(f"  {ticker:<6} {a['score']:3d}分 置信度{conf.get('score',0)}太低，跳过","info")
                scan_status["analyses"].append({
                    "ticker":ticker,"sector":sname,"score":a["score"],"signal":a["signal"],
                    "price":a["price"],"patterns":list(a["patterns"].keys()),
                    "hold_days":a["suggested_hold_days"],"reasons":a["reasons"][:3],
                    "confidence":conf.get("score",0),"confidence_level":conf.get("level","")})
                continue
            scan_status["analyses"].append({
                "ticker":ticker,"sector":sname,"score":a["score"],"signal":a["signal"],
                "price":a["price"],"patterns":list(a["patterns"].keys()),
                "hold_days":a["suggested_hold_days"],"reasons":a["reasons"][:3],
                "confidence":conf.get("score",0),"confidence_level":conf.get("level","")})
            if a["score"]>=CONFIG["MIN_SCORE"]: candidates.append(a)
            t2="success" if a["score"]>=CONFIG["MIN_SCORE"] else "info"
            pk="/".join(list(a["patterns"].keys())[:2])
            log(f"  {ticker:<6} {a['score']:3d}分 {a['signal']} 置信{conf.get('score',0)} 持{a['suggested_hold_days']}天 {pk}",t2)
            time.sleep(0.2)

    # Phase 4: 买入
    scan_status["phase"]="执行交易"
    if not auto_enabled:
        log("⏸️ 仅分析模式，跳过买入操作","warning")
    candidates=sorted(candidates,key=lambda x:-x["score"])
    total=portfolio_value(data,data["prices"])
    for a in candidates:
        if not auto_enabled: break
        if local_pos_count(data)>=CONFIG["MAX_POSITIONS"]: break
        price=a.get("price") or get_prices([a["ticker"]]).get(a["ticker"])
        if not price: continue
        conf = a.get("confidence", {})
        pos_size = calc_position_size(total, a["score"], conf.get("score", 50),
                                       local_pos_count(data), CONFIG["MAX_POSITIONS"])
        ok,msg=sim_buy(data,a["ticker"],price,pos_size,a)
        log(f"📥 {msg} | 置信{conf.get('score',0)} {conf.get('level','')} | 持{a['suggested_hold_days']}天 | {a['hold_reason'][:40]}","success" if ok else "warning")

    # 恢复原始 MIN_SCORE
    CONFIG["MIN_SCORE"] = 58

    # Phase 5: 净值快照
    new_prices=get_prices([t for t,p in data["positions"].items() if p.get("source","local")=="local"])
    data["prices"].update(new_prices)
    # 清理价格中的 NaN
    data["prices"]={k:v for k,v in data["prices"].items() if isinstance(v,(int,float)) and not math.isnan(v)}
    nav_val=portfolio_value(data,data["prices"])
    today=now_pdt().strftime("%Y-%m-%d")
    nav=data.get("daily_nav",[])
    # 过滤掉历史中的 NaN 记录
    nav=[n for n in nav if isinstance(n.get("nav"), (int,float)) and not math.isnan(n["nav"])]
    if isinstance(nav_val,(int,float)) and not math.isnan(nav_val):
        if not nav or nav[-1]["date"]!=today:
            nav.append({"date":today,"nav":round(nav_val,2),"top_sector":top_sectors[0] if top_sectors else ""})
    data["daily_nav"]=nav[-365:]

    # Phase 6: 生成每日总结
    scan_status["phase"]="生成总结"
    log("📝 生成每日新闻总结...")
    today_trades = [t for t in data.get("trades",[]) if t["time"].startswith(today)]
    action_list  = [{"action":t["action"],"ticker":t["ticker"],
                     "reason":"；".join(t.get("patterns",[])) or "综合指标",
                     "score":t.get("score",0),"patterns":t.get("patterns",[]),
                     "hold_days":t.get("hold_days",0)} for t in today_trades]
    daily_summary = generate_daily_summary(news, sector_scores, top_sectors, action_list)
    summaries = data.get("daily_summaries", [])
    summaries = [s for s in summaries if s["date"] != daily_summary["date"]]  # 去重同天
    summaries.append(daily_summary)
    data["daily_summaries"] = summaries[-90:]  # 保留90天
    scan_status["daily_summary"] = daily_summary

    save(data)
    pnl=nav_val-data.get("base_nav", CONFIG["INITIAL_CASH"])
    log(f"✅ 完成 | 总资产${nav_val:,.0f} | 累计${pnl:+,.0f} | 持仓{local_pos_count(data)}/{CONFIG['MAX_POSITIONS']}","success")
    log(f"📋 今日摘要: {daily_summary['one_line']}","info")
    scan_status["running"]=False

# ══════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/api/model_status")
def api_model_status():
    return jsonify({"status": _finbert_status})

@app.route("/api/portfolio")
def api_portfolio():
    data=load(); prices=data.get("prices",{}); total=portfolio_value(data,prices)
    base=data.get("base_nav", CONFIG["INITIAL_CASH"])
    init=CONFIG["INITIAL_CASH"]
    positions=[]
    alpaca_positions=[]
    for ticker,p in data["positions"].items():
        source=p.get("source","local")
        display_ticker=ticker.replace("_alpaca_","") if ticker.startswith("_alpaca_") else ticker
        price=_safe_price(prices,ticker,p["avg_cost"]); mkt=p["shares"]*price; cost=p["shares"]*p["avg_cost"]
        hold_days=(now_pdt()-datetime.strptime(p.get("buy_date",now_pdt().strftime("%Y-%m-%d")),"%Y-%m-%d").replace(tzinfo=_PDT)).days
        days_left=0
        if p.get("target_sell_date"):
            try: days_left=max(0,(datetime.strptime(p["target_sell_date"],"%Y-%m-%d").replace(tzinfo=_PDT)-now_pdt()).days)
            except: pass
        ref_total = total if source=="local" else mkt  # alpaca 持仓 weight 用自身
        row={"ticker":display_ticker,"shares":round(p["shares"],4),"avg_cost":p["avg_cost"],
            "price":price,"mkt":round(mkt,2),"cost":round(cost,2),
            "pnl":round(mkt-cost,2),"pnl_pct":round((mkt-cost)/cost*100,2) if cost else 0,
            "weight":round(mkt/total*100,1) if total and source=="local" else 0,
            "buy_date":p.get("buy_date",""),"target_sell_date":p.get("target_sell_date",""),
            "hold_days":hold_days,"days_left":days_left,
            "hold_reason":p.get("hold_reason",""),"sector":p.get("sector",""),
            "buy_score":p.get("buy_score",0),
            "earnings_date":p.get("earnings_date",""),
            "earnings_warning":p.get("earnings_warning",""),
            "peak_price":round(p.get("peak_price",price),2),
            "trailing_drawdown":round(p.get("trailing_drawdown",0)*100,1),
            "confidence_score":p.get("confidence_score",0),
            "confidence_level":p.get("confidence_level",""),
            "source":source}
        if source=="local":
            positions.append(row)
        else:
            alpaca_positions.append(row)
    return jsonify({"cash":round(data["cash"],2),"total":round(total,2),
        "pnl":round(total-base,2),"pnl_pct":round((total-base)/base*100,2) if base else 0,
        "positions":positions,"alpaca_positions":alpaca_positions,
        "nav":[n for n in data.get("daily_nav",[])[-90:] if isinstance(n.get("nav"),(int,float)) and n["nav"]==n["nav"]],
        "trades":data.get("trades",[])[-40:],"sector_scores":data.get("sector_scores",{}),
        "macro_adj":data.get("macro_adj",{}),
        "config":{"initial":init,"base_nav":round(base,2),"max_pos":CONFIG["MAX_POSITIONS"]}})

@app.route("/api/scan",methods=["POST"])
def api_scan():
    if scan_status["running"]: return jsonify({"ok":False,"msg":"扫描中"})
    threading.Thread(target=run_cycle_bg,daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/force_trade",methods=["POST"])
def api_force_trade():
    if scan_status["running"]: return jsonify({"ok":False,"msg":"扫描中"})
    threading.Thread(target=run_cycle_bg,daemon=True).start()
    return jsonify({"ok":True,"message":"已触发交易，请查看扫描日志"})

@app.route("/api/scan_status")
def api_scan_status(): return jsonify(scan_status)

@app.route("/api/summaries")
def api_summaries():
    data = load()
    summaries = data.get("daily_summaries", [])
    return jsonify({"summaries": list(reversed(summaries))})  # 最新在前

@app.route("/api/live_news")
def api_live_news():
    limit = int(request.args.get("limit", 50))
    cat = request.args.get("category", "")
    items = _live_news["items"]
    if cat:
        items = [i for i in items if i.get("category", "") == cat]
    return jsonify({
        "items": items[:limit],
        "macro_items": _live_news["macro_items"][:20],
        "last_fetch": _live_news["last_fetch"],
        "fetch_count": _live_news["fetch_count"],
        "running": _live_news["running"],
        "sector_snapshot": _live_news.get("sector_snapshot", {}),
        "macro_snapshot": _live_news.get("macro_snapshot", {}),
        "total": len(_live_news["items"]),
    })

@app.route("/api/backtest",methods=["POST"])
def api_backtest():
    body = request.get_json(force=True) or {}
    period = int(body.get("period", 180))
    tickers = body.get("tickers", [])
    if not tickers:
        # 默认用所有板块标的
        for info in SECTORS.values():
            tickers.extend(info["tickers"])
        tickers = list(set(tickers))
    result = run_backtest(tickers, period_days=period)
    return jsonify(result)

@app.route("/api/alpaca_status")
def api_alpaca_status():
    if not alpaca_enabled:
        return jsonify({"enabled":False,"account":{},"market_open":False,"positions":[]})
    return jsonify({
        "enabled":True,
        "account":alpaca_get_account(),
        "market_open":alpaca_is_market_open(),
        "positions":alpaca_get_positions(),
    })

@app.route("/api/alpaca_sync",methods=["POST"])
def api_alpaca_sync():
    if not alpaca_enabled:
        return jsonify({"ok":False,"msg":"Alpaca 未启用"})
    alpaca_sync_positions()
    return jsonify({"ok":True})

@app.route("/api/auto_trade_toggle",methods=["POST"])
def api_auto_trade_toggle():
    body=request.get_json(force=True) or {}
    enabled=body.get("enabled",True)
    data=load()
    data["auto_trade_enabled"]=bool(enabled)
    save(data)
    return jsonify({"ok":True,"auto_trade_enabled":data["auto_trade_enabled"]})

@app.route("/api/market_schedule")
def api_market_schedule():
    try:
        import zoneinfo
        et=datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        et=datetime.utcnow()-timedelta(hours=5)
    weekday=et.weekday()  # 0=Mon
    is_market_day=weekday<5
    hour,minute=et.hour,et.minute
    market_open=is_market_day and 9<=hour<16 and (hour>9 or minute>=30)
    # 下次扫描时间
    scan_times=[(9,25,"09:25 盘前准备"),(9,31,"09:31 开盘扫描"),(11,0,"11:00"),(14,0,"14:00"),(15,30,"15:30")]
    next_scan="明日 09:25 ET"
    now_min=hour*60+minute
    if is_market_day:
        for h,m,label in scan_times:
            if now_min<h*60+m:
                next_scan=f"{label} ET"
                break
    # 今日交易数
    data=load()
    today=et.strftime("%Y-%m-%d")
    today_trades=len([t for t in data.get("trades",[]) if t["time"].startswith(today)])
    return jsonify({
        "is_market_day":is_market_day,
        "market_open":market_open,
        "next_scan":next_scan,
        "et_time":et.strftime("%H:%M:%S ET"),
        "et_weekday":["周一","周二","周三","周四","周五","周六","周日"][weekday],
        "today_trades":today_trades,
        "auto_enabled":data.get("auto_trade_enabled",True),
    })

@app.route("/api/reset_baseline",methods=["POST"])
def api_reset_baseline():
    data=load()
    prices=data.get("prices",{})
    total=portfolio_value(data,prices)
    data["base_nav"]=total
    save(data)
    return jsonify({"ok":True,"base_nav":round(total,2)})


@app.route("/api/set_capital",methods=["POST"])
def api_set_capital():
    body=request.get_json(force=True) or {}
    new_capital=body.get("capital")
    if not new_capital or not isinstance(new_capital,(int,float)) or new_capital<1000:
        return jsonify({"ok":False,"msg":"资金量必须 >= $1,000"})
    new_capital=float(new_capital)
    data=load()
    old_init=CONFIG["INITIAL_CASH"]
    # 计算当前持仓市值
    prices=data.get("prices",{})
    pos_value=sum(p["shares"]*prices.get(t,p["avg_cost"]) for t,p in data["positions"].items() if p.get("source","local")=="local")
    if new_capital<pos_value:
        return jsonify({"ok":False,"msg":f"新资金量不能低于当前持仓市值 ${pos_value:,.0f}"})
    # 更新 CONFIG 和 data（持久化到文件）
    CONFIG["INITIAL_CASH"]=new_capital
    data["initial_cash"]=new_capital
    data["cash"]=new_capital-pos_value
    # 重算 daily_nav 最新一条
    nav_list=data.get("daily_nav",[])
    if nav_list:
        nav_list[-1]["nav"]=new_capital
    save(data)
    return jsonify({"ok":True,"capital":new_capital,"cash":round(data["cash"],2)})

@app.route("/api/reset",methods=["POST"])
def api_reset():
    if DATA_FILE.exists(): DATA_FILE.unlink()
    return jsonify({"ok":True})

# ══════════════════════════════════════════════════════
#  前端
# ══════════════════════════════════════════════════════

HTML = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>智能模拟交易</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#08080f;--surface:#101018;--card:#161622;--border:#222233;
  --text:#e8e8f4;--muted:#5a5a78;--accent:#7c6eff;--green:#1ecc6e;
  --red:#ff3d5a;--yellow:#f0bc30;--blue:#3ab8ff;
  --mono:'DM Mono',monospace;--sans:'Syne',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh}
.app{display:grid;grid-template-columns:230px 1fr;min-height:100vh}
.sidebar{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column}
.logo{padding:20px 18px;font-size:16px;font-weight:800;border-bottom:1px solid var(--border)}
.logo span{color:var(--accent)}
.logo small{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:3px;letter-spacing:0.05em}
.nav{padding:10px 0;flex:1}
.nav-item{display:flex;align-items:center;gap:8px;padding:9px 16px;cursor:pointer;
  font-size:12px;color:var(--muted);border-left:2px solid transparent;transition:all 0.12s}
.nav-item:hover{color:var(--text);background:rgba(124,110,255,0.05)}
.nav-item.active{color:var(--text);border-left-color:var(--accent);background:rgba(124,110,255,0.09)}
.sidebar-foot{padding:14px;border-top:1px solid var(--border)}
.scan-btn{width:100%;padding:10px;background:var(--accent);color:#fff;border:none;
  border-radius:8px;font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;transition:all 0.15s}
.scan-btn:hover:not(:disabled){background:#6a5ce8}
.scan-btn:disabled{background:var(--card);color:var(--muted);cursor:not-allowed}
.scan-btn.running{background:var(--card);color:var(--yellow);animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.4}}
.pbar-wrap{height:2px;background:rgba(124,110,255,0.12);border-radius:1px;margin-bottom:8px;display:none;overflow:hidden}
.pbar-fill{height:100%;background:var(--accent);transition:width 0.3s}
.phase{font-size:10px;color:var(--accent);text-align:center;margin-bottom:6px;min-height:13px}
.last{font-size:10px;color:var(--muted);text-align:center;margin-top:7px}
.cap-preset{padding:6px 14px;background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer;transition:all .15s}
.cap-preset:hover{color:var(--text);border-color:var(--accent);background:rgba(124,110,255,0.08)}
.main{overflow-y:auto;padding:24px 26px}
.page{display:none}.page.active{display:block}
.ptitle{font-size:19px;font-weight:800;margin-bottom:20px;letter-spacing:-0.4px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.clabel{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px}
.cval{font-size:22px;font-weight:800;font-family:var(--mono);letter-spacing:-1px;line-height:1}
.csub{font-size:11px;color:var(--muted);margin-top:4px;font-family:var(--mono)}
.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.blue{color:var(--blue)}
.stitle{font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:10px}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:18px}
.cwrap{position:relative;height:185px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:18px}
.box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
table{width:100%;border-collapse:collapse}
th{padding:7px 10px;font-size:10px;color:var(--muted);text-align:left;font-weight:500;
   text-transform:uppercase;letter-spacing:0.08em;border-bottom:1px solid var(--border)}
td{padding:9px 10px;font-size:11px;border-bottom:1px solid rgba(34,34,51,0.6);font-family:var(--mono)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(124,110,255,0.03)}
.badge{padding:2px 6px;border-radius:3px;font-size:10px;font-weight:500;font-family:var(--sans)}
.ba{background:rgba(124,110,255,0.18);color:var(--accent)}
.bg{background:rgba(30,204,110,0.14);color:var(--green)}
.br{background:rgba(255,61,90,0.14);color:var(--red)}
.by{background:rgba(240,188,48,0.14);color:var(--yellow)}
.srow{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(34,34,51,0.4)}
.srow:last-child{border-bottom:none}
.sname{width:80px;font-size:11px}
.sbar{flex:1;height:4px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden}
.sbar-f{height:100%;border-radius:2px;transition:width 0.5s}
.sheat{width:26px;text-align:right;font-size:10px;font-family:var(--mono);color:var(--muted)}
.log-box{background:var(--bg);border:1px solid var(--border);border-radius:7px;
  padding:10px;font-family:var(--mono);font-size:11px;height:250px;overflow-y:auto}
.le{display:flex;gap:7px;padding:2px 0;line-height:1.5}
.lt{color:var(--muted);flex-shrink:0;font-size:10px;padding-top:1px}
.li{color:var(--text)}.ls{color:var(--green)}.ld{color:var(--red)}.lw{color:var(--yellow)}
.pill{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;
  margin:1px;background:rgba(124,110,255,0.1);color:var(--accent);font-family:var(--sans)}
.pill-b{background:rgba(30,204,110,0.1);color:var(--green)}
.pill-r{background:rgba(255,61,90,0.1);color:var(--red)}
.hbar{height:3px;border-radius:1px;background:rgba(124,110,255,0.1);overflow:hidden;margin-top:2px}
.hbar-f{height:100%;border-radius:1px;background:var(--accent)}
.empty{text-align:center;padding:28px;color:var(--muted);font-size:12px}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style></head><body>
<div class="app">
<div class="sidebar">
  <div class="logo">ALGO<span>TRADE</span><small>新闻轮动 · K线分析 · 自动持仓</small></div>
  <div class="nav">
    <div class="nav-item active" onclick="nav('dashboard',this)">◈ &nbsp;仪表盘</div>
    <div class="nav-item" onclick="nav('sectors',this)">◉ &nbsp;板块热度</div>
    <div class="nav-item" onclick="nav('positions',this)">◎ &nbsp;持仓明细</div>
    <div class="nav-item" onclick="nav('signals',this)">◌ &nbsp;扫描信号</div>
    <div class="nav-item" onclick="nav('summary',this)">✦ &nbsp;每日总结</div>
    <div class="nav-item" onclick="nav('trades',this)">◇ &nbsp;交易记录</div>
    <div class="nav-item" onclick="nav('livenews',this)">◈ &nbsp;实时新闻</div>
    <div class="nav-item" onclick="nav('backtest',this)">◆ &nbsp;回测系统</div>
    <div class="nav-item" onclick="nav('settings',this)">⚙ &nbsp;资金设置</div>
  </div>
  <div class="sidebar-foot">
    <div class="phase" id="phase"></div>
    <div class="pbar-wrap" id="pbar-wrap"><div class="pbar-fill" id="pbar"></div></div>
    <button class="scan-btn" id="scan-btn" onclick="doScan()">▶ 立即扫描</button>
    <div class="last" id="last-scan">最后扫描: —</div>
    <div style="margin-top:8px;text-align:center">
      <span style="font-size:10px;color:var(--muted)">情绪模型: </span>
      <span id="model-badge" style="font-size:10px;font-family:var(--mono)">检测中...</span>
    </div>
  </div>
</div>
<div class="main">

<div class="page active" id="page-dashboard">
  <div class="ptitle">仪表盘</div>
  <div class="cards">
    <div class="card"><div class="clabel">总资产</div><div class="cval" id="d-total">—</div><div class="csub" id="d-init">—</div></div>
    <div class="card"><div class="clabel">累计盈亏</div><div class="cval" id="d-pnl">—</div><div class="csub" id="d-pct">—</div>
      <div style="margin-top:6px"><button onclick="resetBaseline()" style="font-size:9px;padding:2px 8px;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--muted);cursor:pointer">重置基准</button></div></div>
    <div class="card"><div class="clabel">可用现金</div><div class="cval" id="d-cash">—</div><div class="csub" id="d-cp">—</div></div>
    <div class="card"><div class="clabel">当前持仓</div><div class="cval" id="d-pos">—</div><div class="csub" id="d-pm">—</div></div>
  </div>
  <div id="alpaca-bar" style="display:none;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:18px;align-items:center;gap:16px;flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:6px">
      <div id="alpaca-dot" style="width:8px;height:8px;border-radius:50%;background:var(--muted)"></div>
      <span style="font-size:12px;font-weight:500">Alpaca 模拟盘</span>
    </div>
    <span id="alpaca-equity" style="font-size:12px;font-family:var(--mono);color:var(--muted)">—</span>
    <span id="alpaca-cash" style="font-size:12px;color:var(--muted)">现金 —</span>
    <span id="alpaca-market" style="font-size:11px;padding:2px 8px;border-radius:4px;background:var(--card);color:var(--muted)">市场 —</span>
    <span id="auto-trade-badge" style="font-size:11px;padding:2px 8px;border-radius:4px;cursor:pointer" onclick="toggleAutoTrade()">—</span>
  </div>
  <div id="schedule-card" style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:18px;display:flex;align-items:center;gap:20px;flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:6px">
      <span id="sch-market-dot" style="font-size:10px">⚫</span>
      <span id="sch-market-text" style="font-size:12px;font-weight:500;color:var(--muted)">—</span>
    </div>
    <span id="sch-et" style="font-size:11px;font-family:var(--mono);color:var(--muted)">—</span>
    <span id="sch-next" style="font-size:11px;color:var(--accent)">—</span>
    <span id="sch-trades" style="font-size:11px;color:var(--muted)">—</span>
    <span id="sch-auto" style="font-size:11px;padding:2px 8px;border-radius:4px;cursor:pointer" onclick="toggleAutoTrade()">—</span>
  </div>
  <div class="chart-card"><div class="stitle">净值曲线</div><div class="cwrap"><canvas id="nav-chart"></canvas></div></div>
  <div class="box" style="margin-bottom:18px"><div class="stitle">🏛️ 宏观政策事件</div><div id="macro-mini"><div class="empty">扫描后可见</div></div></div>
  <div class="grid2">
    <div class="box"><div class="stitle">板块热度</div><div id="sector-mini"></div></div>
    <div class="box"><div class="stitle">持仓概览</div><div id="pos-mini"></div></div>
  </div>
</div>

<div class="page" id="page-sectors">
  <div class="ptitle">板块热度分析</div>
  <div id="sector-detail"></div>
</div>

<div class="page" id="page-positions">
  <div class="ptitle">持仓明细</div>
  <div class="box" style="overflow-x:auto">
    <table><thead><tr><th>代码</th><th>板块</th><th>成本</th><th>现价</th><th>市值</th>
      <th>盈亏</th><th>收益率</th><th>持天</th><th>目标卖出日</th><th>置信度</th><th>财报</th><th>移动止损</th><th>持仓理由</th>
    </tr></thead><tbody id="pos-table"></tbody></table>
  </div>
</div>

<div class="page" id="page-signals">
  <div class="ptitle">扫描信号</div>
  <div class="grid2">
    <div class="box"><div class="stitle">标的评分</div><div id="sig-list"></div></div>
    <div class="box"><div class="stitle">实时日志</div><div class="log-box" id="scan-log"><div class="empty">点击「立即扫描」开始</div></div></div>
  </div>
</div>

<div class="page" id="page-summary">
  <div class="ptitle">每日新闻总结</div>
  <div id="summary-list"><div class="empty">扫描后生成每日总结</div></div>
</div>

<div class="page" id="page-trades">
  <div class="ptitle">交易记录</div>
  <div class="box" style="overflow-x:auto">
    <table><thead><tr><th>时间</th><th>操作</th><th>代码</th><th>板块</th>
      <th>价格</th><th>金额</th><th>盈亏</th><th>持天</th><th>K线形态</th>
    </tr></thead><tbody id="trade-table"></tbody></table>
  </div>
</div>

<div class="page" id="page-livenews">
  <div class="ptitle">实时新闻流</div>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
    <span id="ln-status" style="font-size:11px;color:var(--muted)">等待数据...</span>
    <span id="ln-count" style="font-size:10px;color:var(--accent);font-family:var(--mono)"></span>
    <div style="margin-left:auto;display:flex;gap:6px">
      <button class="ln-filter active" onclick="filterNews('',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--text);font-size:10px;cursor:pointer;font-family:var(--sans)">全部</button>
      <button class="ln-filter" onclick="filterNews('宏观政策',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--muted);font-size:10px;cursor:pointer;font-family:var(--sans)">宏观政策</button>
      <button class="ln-filter" onclick="filterNews('宏观',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--muted);font-size:10px;cursor:pointer;font-family:var(--sans)">宏观</button>
      <button class="ln-filter" onclick="filterNews('AI半导体',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--muted);font-size:10px;cursor:pointer;font-family:var(--sans)">AI半导体</button>
      <button class="ln-filter" onclick="filterNews('加密货币',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--muted);font-size:10px;cursor:pointer;font-family:var(--sans)">加密</button>
      <button class="ln-filter" onclick="filterNews('科技',this)" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--muted);font-size:10px;cursor:pointer;font-family:var(--sans)">科技</button>
    </div>
  </div>
  <div class="grid2">
    <div>
      <div class="box" style="margin-bottom:14px">
        <div class="stitle">宏观事件实时检测</div>
        <div id="ln-macro"><div class="empty">爬取中...</div></div>
      </div>
      <div class="box">
        <div class="stitle">板块情绪快照</div>
        <div id="ln-sectors"><div class="empty">爬取中...</div></div>
      </div>
    </div>
    <div class="box" style="max-height:600px;overflow-y:auto">
      <div class="stitle">新闻流 <span id="ln-live" style="font-size:9px;color:var(--green);animation:blink 2s infinite">● LIVE</span></div>
      <div id="ln-feed"><div class="empty">后台爬虫启动中，约1分钟后显示...</div></div>
    </div>
  </div>
</div>

<div class="page" id="page-backtest">
  <div class="ptitle">回测系统</div>
  <div class="box" style="margin-bottom:18px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <div>
        <label style="font-size:10px;color:var(--muted)">回测周期</label><br>
        <select id="bt-period" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 10px;font-family:var(--mono);font-size:12px">
          <option value="90">3个月</option>
          <option value="180" selected>6个月</option>
          <option value="365">1年</option>
        </select>
      </div>
      <button onclick="runBacktest()" id="bt-btn" style="padding:8px 18px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;margin-top:14px">▶ 运行回测</button>
      <span id="bt-loading" style="font-size:11px;color:var(--yellow);display:none;animation:blink 1.4s infinite">回测中，请稍候...</span>
    </div>
  </div>
  <div id="bt-results" style="display:none">
    <div class="cards" id="bt-cards"></div>
    <div class="chart-card"><div class="stitle">净值曲线</div><div class="cwrap"><canvas id="bt-chart"></canvas></div></div>
    <div class="box" style="margin-bottom:18px">
      <div class="stitle">策略说明</div>
      <div style="font-size:11px;color:var(--muted);line-height:1.8">
        • 每5个交易日用技术指标（RSI、MACD、均线、动量）计算评分，≥60分买入<br>
        • 每仓12%资金，最多同时持有6只<br>
        • 止损-7%，移动止损从最高价回撤-8%，止盈+20%<br>
        • 手续费0.1%，夏普比率以无风险利率5%/年计算
      </div>
    </div>
    <div class="box" style="overflow-x:auto">
      <div class="stitle">最近回测交易</div>
      <table><thead><tr><th>日期</th><th>代码</th><th>操作</th><th>价格</th><th>盈亏</th><th>收益率</th></tr></thead>
      <tbody id="bt-trades"></tbody></table>
    </div>
  </div>
</div>

<div class="page" id="page-settings">
  <div class="ptitle">资金设置</div>
  <div class="box" style="max-width:480px">
    <div class="stitle">调整总资金量</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:16px;line-height:1.7">
      修改初始资金后，系统会自动重新计算可用现金（总资金 − 当前持仓市值）。<br>
      已有持仓不受影响，盈亏将基于新资金量重新计算。
    </div>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <label style="font-size:11px;color:var(--muted);min-width:70px">当前资金</label>
      <span id="set-cur" style="font-size:16px;font-weight:700;font-family:var(--mono);color:var(--text)">—</span>
    </div>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <label style="font-size:11px;color:var(--muted);min-width:70px">新资金量</label>
      <input id="set-input" type="number" min="1000" step="1000" placeholder="例如 200000"
        style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:var(--mono);font-size:13px;outline:none">
      <span style="font-size:12px;color:var(--muted)">USD</span>
    </div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button onclick="setCapitalPreset(50000)" class="cap-preset">$50K</button>
      <button onclick="setCapitalPreset(100000)" class="cap-preset">$100K</button>
      <button onclick="setCapitalPreset(200000)" class="cap-preset">$200K</button>
      <button onclick="setCapitalPreset(500000)" class="cap-preset">$500K</button>
      <button onclick="setCapitalPreset(1000000)" class="cap-preset">$1M</button>
    </div>
    <button onclick="applyCapital()" id="set-btn"
      style="margin-top:20px;width:100%;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer">
      确认修改
    </button>
    <div id="set-msg" style="margin-top:10px;font-size:11px;text-align:center"></div>
  </div>
  <div class="box" style="max-width:480px;margin-top:16px">
    <div class="stitle">重置账户</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">清空所有持仓、交易记录，以当前资金量重新开始。此操作不可撤销。</div>
    <button onclick="resetAccount()"
      style="padding:8px 20px;background:transparent;color:var(--red);border:1px solid var(--red);border-radius:6px;font-family:var(--sans);font-size:12px;font-weight:600;cursor:pointer">
      重置账户
    </button>
  </div>
</div>

</div></div>
<script>
let navChart=null,portfolio={};
const COLORS={"AI与半导体":"#7c6eff","加密货币":"#f0bc30","新能源":"#1ecc6e",
              "生物医药":"#ff6b9d","科技成长":"#3ab8ff","航天国防":"#ff8c42"};
function nav(p,el){
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));
  document.getElementById('page-'+p).classList.add('active'); el.classList.add('active');
}
const fmt=(n,d=2)=>n==null?'—':'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct=n=>n==null?'—':(n>=0?'+':'')+Number(n).toFixed(2)+'%';
const cc=n=>n>0?'green':n<0?'red':'';
async function load(){
  const r=await fetch('/api/portfolio');portfolio=await r.json();
  renderDash();renderSectors();renderPositions();renderTrades();updateSettingsPage();updateScanBtn();
}
function renderDash(){
  const p=portfolio;
  document.getElementById('d-total').textContent=fmt(p.total);
  const pe=document.getElementById('d-pnl');
  pe.textContent=(p.pnl>=0?'+':'')+fmt(p.pnl);pe.className='cval '+(p.pnl>=0?'green':'red');
  document.getElementById('d-pct').textContent=pct(p.pnl_pct)+' 总收益率';
  document.getElementById('d-init').textContent='基准 '+fmt(p.config?.base_nav||p.config?.initial||100000);
  document.getElementById('d-cash').textContent=fmt(p.cash);
  document.getElementById('d-cp').textContent=p.total?pct(p.cash/p.total*100)+' 仓位':'—';
  document.getElementById('d-pos').textContent=p.positions?.length||0;
  document.getElementById('d-pm').textContent='最多 '+(p.config?.max_pos||8)+' 只';
  const nav=p.nav||[];
  if(nav.length>1){
    if(navChart)navChart.destroy();
    const init=p.config?.initial||100000,vals=nav.map(n=>n.nav);
    const col=vals[vals.length-1]>=init?'#1ecc6e':'#ff3d5a';
    navChart=new Chart(document.getElementById('nav-chart').getContext('2d'),{
      type:'line',data:{labels:nav.map(n=>n.date.slice(5)),datasets:[{data:vals,
        borderColor:col,backgroundColor:col+'12',fill:true,tension:0.4,pointRadius:0,borderWidth:1.5}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:'#5a5a78',font:{size:9}},grid:{color:'#1a1a28'}},
                y:{ticks:{color:'#5a5a78',font:{size:9},callback:v=>'$'+Math.round(v/1000)+'k'},grid:{color:'#1a1a28'}}}}});
  }
  // 宏观政策事件卡片
  const ma=portfolio.macro_adj||{};
  const macroEl=document.getElementById('macro-mini');
  if(ma.events&&ma.events.length){
    const biasCol=ma.market_bias<=-1?'var(--red)':ma.market_bias>=1?'var(--green)':'var(--muted)';
    const biasText=ma.market_bias<=-2?'极度利空':ma.market_bias<=-1?'偏空':ma.market_bias>=2?'强势利好':ma.market_bias>=1?'偏多':'中性';
    macroEl.innerHTML=`
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <span style="font-size:14px;font-weight:800;font-family:var(--mono);color:${biasCol}">${biasText}</span>
        <span style="font-size:10px;color:var(--muted)">偏向值 ${ma.market_bias>0?'+':''}${ma.market_bias} · 持仓周期调整 ${ma.hold_days_adj>0?'+':''}${ma.hold_days_adj}天</span>
      </div>
      <div style="background:rgba(124,110,255,0.06);border-radius:6px;padding:8px 10px;margin-bottom:10px;font-size:11px;color:var(--text)">${ma.summary||''}</div>
      ${ma.events.map(e=>{
        const ec=e.market_bias<0?'var(--red)':e.market_bias>0?'var(--green)':'var(--muted)';
        return`<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid rgba(34,34,51,0.4)">
          <span style="font-size:11px;font-weight:600;color:${ec};min-width:120px">${e.name}</span>
          <span style="font-size:10px;color:var(--muted);flex:1">${e.desc}</span>
          <span style="font-size:10px;font-family:var(--mono);color:${e.avg_sentiment>0?'var(--green)':e.avg_sentiment<0?'var(--red)':'var(--muted)'}">${e.avg_sentiment>0?'+':''}${e.avg_sentiment}</span>
          <span style="font-size:10px;color:var(--muted)">${e.matched_count}条</span>
        </div>`;}).join('')}`;
  } else {
    macroEl.innerHTML='<div class="empty">扫描后可见宏观政策事件</div>';
  }

  const ss=portfolio.sector_scores||{};
  const sk=Object.keys(ss).sort((a,b)=>ss[b].heat-ss[a].heat);
  document.getElementById('sector-mini').innerHTML=sk.length?
    sk.slice(0,5).map(k=>{const s=ss[k],col=COLORS[k]||'#7c6eff';
      return`<div class="srow"><span class="sname">${k}</span>
        <div class="sbar"><div class="sbar-f" style="width:${s.heat}%;background:${col}"></div></div>
        <span class="sheat">${s.heat}</span>
        <span style="width:32px;text-align:right;font-size:10px;color:${s.score>0?'var(--green)':s.score<0?'var(--red)':'var(--muted)'}">${s.label}</span>
      </div>`;}).join(''):'<div class="empty">扫描后可见</div>';
  const pos=portfolio.positions||[];
  document.getElementById('pos-mini').innerHTML=pos.length?
    pos.slice(0,6).map(p=>`<div style="display:flex;align-items:center;gap:7px;padding:5px 0;border-bottom:1px solid rgba(34,34,51,0.4)">
      <span class="badge ba">${p.ticker}</span>
      <span style="font-size:10px;color:var(--muted);flex:1">${p.sector||''}</span>
      <span class="${cc(p.pnl)}" style="font-size:11px;font-family:var(--mono)">${pct(p.pnl_pct)}</span>
      <span style="font-size:10px;color:var(--muted)">剩${p.days_left}天</span>
    </div>`).join(''):'<div class="empty">暂无持仓</div>';
}
function renderSectors(){
  const ss=portfolio.sector_scores||{};const keys=Object.keys(ss).sort((a,b)=>ss[b].heat-ss[a].heat);
  if(!keys.length){document.getElementById('sector-detail').innerHTML='<div class="empty">扫描后可见</div>';return;}
  document.getElementById('sector-detail').innerHTML='<div class="grid3">'+keys.map(k=>{
    const s=ss[k],col=COLORS[k]||'#7c6eff';
    const sc=s.score>0?'var(--green)':s.score<0?'var(--red)':'var(--muted)';
    return`<div class="box">
      <div style="display:flex;align-items:center;gap:7px;margin-bottom:10px">
        <div style="width:7px;height:7px;border-radius:50%;background:${col};flex-shrink:0"></div>
        <span style="font-size:12px;font-weight:600;flex:1">${k}</span>
        <span style="font-size:10px;color:${sc}">${s.label}</span>
      </div>
      <div style="display:flex;gap:14px;margin-bottom:10px">
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:1px">热度</div><div style="font-size:20px;font-weight:800;font-family:var(--mono)">${s.heat}</div></div>
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:1px">新闻</div><div style="font-size:20px;font-weight:800;font-family:var(--mono)">${s.count}</div></div>
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:1px">情绪</div><div style="font-size:20px;font-weight:800;font-family:var(--mono);color:${sc}">${s.score>0?'+':''}${s.score}</div></div>
      </div>
      <div style="font-size:10px;color:var(--muted);margin-bottom:5px">相关新闻</div>
      ${(s.headlines||[]).slice(0,3).map(h=>`<div style="font-size:10px;padding:3px 0;border-bottom:1px solid rgba(34,34,51,0.35);color:var(--text);line-height:1.4">
        ${h.title}<span style="color:${h.sentiment>0?'var(--green)':h.sentiment<0?'var(--red)':'var(--muted)'}"> ${h.sentiment>0?'+':''}${h.sentiment}</span>
      </div>`).join('')}
    </div>`;}).join('')+'</div>';
}
function renderPositions(){
  const local=portfolio.positions||[];
  const alpaca=portfolio.alpaca_positions||[];
  const rows=[...local,...alpaca];
  document.getElementById('pos-table').innerHTML=rows.length?rows.map(p=>{
    const isAlpaca=p.source==='alpaca';
    const rowStyle=isAlpaca?'opacity:0.7':'';
    const alpacaTag=isAlpaca?' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:rgba(124,110,255,0.15);color:#7c6eff">Alpaca</span>':'';
    const prog=Math.max(0,Math.min(100,(1-p.days_left/Math.max(p.hold_days||14,1))*100));
    const tdCol=Math.abs(p.trailing_drawdown||0)>5?'var(--red)':Math.abs(p.trailing_drawdown||0)>3?'var(--yellow)':'var(--muted)';
    const confCol=p.confidence_score>=70?'var(--green)':p.confidence_score>=50?'var(--yellow)':'var(--muted)';
    return`<tr style="${rowStyle}">
      <td><span class="badge ba">${p.ticker}</span>${alpacaTag}</td>
      <td style="font-size:10px;color:var(--muted)">${p.sector||'—'}</td>
      <td>${fmt(p.avg_cost)}</td><td>${fmt(p.price)}</td><td>${fmt(p.mkt)}</td>
      <td class="${cc(p.pnl)}">${(p.pnl>=0?'+':'')+fmt(p.pnl)}</td>
      <td class="${cc(p.pnl_pct)}">${pct(p.pnl_pct)}</td>
      <td>${p.hold_days}天</td>
      <td style="font-size:10px">${p.target_sell_date}
        <div class="hbar"><div class="hbar-f" style="width:${prog}%"></div></div>
        <span style="font-size:9px;color:var(--muted)">剩${p.days_left}天</span>
      </td>
      <td style="font-size:10px;color:${confCol}">${p.confidence_score||0}分<br><span style="font-size:9px">${p.confidence_level||''}</span></td>
      <td style="font-size:10px">${p.earnings_date||'—'}${p.earnings_warning?'<br><span style="font-size:9px">'+p.earnings_warning+'</span>':''}</td>
      <td style="font-size:10px;color:${tdCol}">${p.trailing_drawdown!=null?p.trailing_drawdown.toFixed(1)+'%':'—'}<br><span style="font-size:9px;color:var(--muted)">峰${fmt(p.peak_price)}</span></td>
      <td style="font-size:10px;color:var(--muted);max-width:150px">${p.hold_reason||'—'}</td>
    </tr>`;}).join(''):'<tr><td colspan="13"><div class="empty">暂无持仓</div></td></tr>';
}
function renderTrades(){
  const trades=(portfolio.trades||[]).slice().reverse();
  const am={BUY:'📥 买入',SELL:'📤 卖出',STOP_LOSS:'⚡ 止损',TAKE_PROFIT:'🎯 止盈',SIGNAL_SELL:'📤 信号卖',PERIOD_SELL:'📅 期满卖',TRAILING_STOP:'📉 移动止损',MACRO_SELL:'🏛️ 宏观卖'};
  document.getElementById('trade-table').innerHTML=trades.length?trades.map(t=>`<tr>
    <td style="color:var(--muted);font-size:10px">${t.time}</td>
    <td style="font-size:11px">${am[t.action]||t.action}</td>
    <td><span class="badge ba">${t.ticker}</span></td>
    <td style="font-size:10px;color:var(--muted)">${t.sector||'—'}</td>
    <td>${fmt(t.price)}</td><td>${fmt(t.amount)}</td>
    <td class="${cc(t.pnl||0)}">${t.pnl?(t.pnl>=0?'+':'')+fmt(t.pnl):'—'}</td>
    <td style="color:var(--muted)">${t.hold_days!=null?t.hold_days+'天':'—'}</td>
    <td>${(t.patterns||[]).map(p=>`<span class="pill">${p}</span>`).join('')||'—'}</td>
  </tr>`).join(''):'<tr><td colspan="9"><div class="empty">暂无记录</div></td></tr>';
}
let scanPoll=null;
async function doScan(){
  const btn=document.getElementById('scan-btn');
  btn.disabled=true;btn.textContent='分析中...';btn.classList.add('running');
  document.getElementById('pbar-wrap').style.display='block';
  document.getElementById('scan-log').innerHTML='';
  document.getElementById('sig-list').innerHTML='';
  // switch to signals tab
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));
  document.getElementById('page-signals').classList.add('active');
  document.querySelectorAll('.nav-item').forEach(x=>{if(x.textContent.includes('信号'))x.classList.add('active');});
  await fetch('/api/force_trade',{method:'POST'});
  scanPoll=setInterval(pollScan,900);
}
async function pollScan(){
  const r=await fetch('/api/scan_status');const s=await r.json();
  document.getElementById('pbar').style.width=Math.round((s.progress||0)/Math.max(s.total||1,1)*100)+'%';
  document.getElementById('phase').textContent=s.phase||'';
  const logDiv=document.getElementById('scan-log');
  logDiv.innerHTML=s.log.map(e=>`<div class="le"><span class="lt">${e.time}</span><span class="l${(e.type||'i')[0]}">${e.msg}</span></div>`).join('');
  logDiv.scrollTop=logDiv.scrollHeight;
  if(s.analyses?.length){
    const sorted=[...s.analyses].sort((a,b)=>b.score-a.score);
    document.getElementById('sig-list').innerHTML=sorted.map(a=>{
      const col=a.score>=58?'var(--green)':a.score<=35?'var(--red)':'var(--muted)';
      return`<div style="padding:5px 0;border-bottom:1px solid rgba(34,34,51,0.4)">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:2px">
          <span class="badge ba" style="width:48px;text-align:center">${a.ticker}</span>
          <span style="font-size:10px;color:var(--muted);flex:1">${a.sector}</span>
          <span style="font-size:11px;font-family:var(--mono);color:${col}">${a.score}分</span>
          <span style="font-size:10px;color:${col}">${a.signal}</span>
        </div>
        <div style="height:3px;background:rgba(255,255,255,0.04);border-radius:1px;margin-bottom:3px">
          <div style="height:100%;width:${a.score}%;background:${col};border-radius:1px;transition:width 0.4s"></div>
        </div>
        <div style="font-size:10px;color:var(--muted)">
          ${(a.patterns||[]).map(p=>`<span class="pill">${p}</span>`).join('')}
          <span style="margin-left:4px">持${a.hold_days}天</span>
          ${a.confidence!=null?`<span class="pill${a.confidence>=60?' pill-b':a.confidence<45?' pill-r':''}" style="margin-left:4px">置信${a.confidence} ${a.confidence_level||''}</span>`:''}
        </div>
      </div>`;}).join('');
  }
  if(!s.running){
    clearInterval(scanPoll);
    const btn=document.getElementById('scan-btn');
    btn.disabled=false;btn.classList.remove('running');updateScanBtn();
    document.getElementById('last-scan').textContent='最后: '+new Date().toLocaleTimeString('zh')+' PDT';
    document.getElementById('phase').textContent='';
    setTimeout(load,600);
  }
}
async function loadSummaries(){
  const r=await fetch('/api/summaries');const d=await r.json();
  const el=document.getElementById('summary-list');
  const sums=d.summaries||[];
  if(!sums.length){el.innerHTML='<div class="empty">扫描后生成每日总结</div>';return;}
  const macroColor=m=>m==='偏多'?'var(--green)':m==='偏空'?'var(--red)':'var(--muted)';
  const actIcon={BUY:'📥',SELL:'📤',STOP_LOSS:'⚡',TAKE_PROFIT:'🎯',SIGNAL_SELL:'📤',PERIOD_SELL:'📅'};
  el.innerHTML=sums.map(s=>`
    <div class="box" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border)">
        <span style="font-size:14px;font-weight:700">${s.date}</span>
        <span style="font-size:10px;color:var(--muted)">${s.time} · ${s.total_news}条新闻 · ${s.sources_used}个来源</span>
        <span style="margin-left:auto;font-size:12px;font-weight:600;color:${macroColor(s.macro_signal)}">宏观${s.macro_signal}</span>
      </div>

      <div style="background:rgba(124,110,255,0.06);border-radius:6px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:var(--text);line-height:1.6">
        ${s.one_line}
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
        <div>
          <div class="stitle">宏观环境</div>
          ${s.macro_notes.map(n=>`<div style="font-size:11px;color:var(--muted);padding:3px 0;border-bottom:1px solid rgba(34,34,51,0.3);line-height:1.5">• ${n}</div>`).join('')}
        </div>
        <div>
          <div class="stitle">本轮重点板块</div>
          ${s.top_sectors.map(t=>`<span class="badge ba" style="margin:2px">${t}</span>`).join('')}
        </div>
      </div>

      <div class="stitle">板块详细解读</div>
      <div style="margin-bottom:12px">
        ${(s.sector_insights||[]).map(si=>{
          const col=si.heat>50?'var(--green)':si.heat>20?'var(--yellow)':'var(--muted)';
          return`<div style="padding:7px 0;border-bottom:1px solid rgba(34,34,51,0.3)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
              <span style="font-size:12px;font-weight:600;width:90px">${si.sector}</span>
              <span style="font-size:10px;color:${col}">热度${si.heat}</span>
              <span style="font-size:10px;color:var(--muted)">${si.label} · ${si.count}条新闻</span>
            </div>
            <div style="font-size:11px;color:var(--text);line-height:1.5;padding-left:98px">${si.conclusion}</div>
          </div>`;}).join('')}
      </div>

      <div class="stitle">高价值新闻（关键词命中最高）</div>
      <div style="margin-bottom:12px">
        ${(s.top_news||[]).slice(0,8).map(n=>{
          const sc=n.sentiment>0.1?'var(--green)':n.sentiment<-0.1?'var(--red)':'var(--muted)';
          return`<div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px solid rgba(34,34,51,0.25);align-items:flex-start">
            <span style="font-size:10px;color:var(--muted);flex-shrink:0;padding-top:1px;width:70px">${n.source}</span>
            <span style="font-size:11px;color:var(--text);flex:1;line-height:1.5">${n.title}</span>
            <span style="font-size:10px;font-family:var(--mono);color:${sc};flex-shrink:0">${n.sentiment>0?'+':''}${n.sentiment}</span>
          </div>`;}).join('')}
      </div>

      ${s.actions&&s.actions.length?`
      <div class="stitle">今日操作决策</div>
      ${s.actions.map(a=>`<div style="display:flex;gap:8px;align-items:center;padding:5px 0;border-bottom:1px solid rgba(34,34,51,0.25)">
        <span style="font-size:13px">${actIcon[a.action]||'·'}</span>
        <span class="badge ba">${a.ticker}</span>
        <span style="font-size:11px;color:var(--muted)">${a.action}</span>
        ${a.score?`<span style="font-size:10px;color:var(--accent)">评分${a.score}</span>`:''}
        ${a.hold_days?`<span style="font-size:10px;color:var(--muted)">持${a.hold_days}天</span>`:''}
        <span style="font-size:11px;color:var(--muted);flex:1;text-align:right">${(a.patterns||[]).join(' · ')||a.reason}</span>
      </div>`).join('')}`:''}
    </div>`).join('');
}

async function pollModelStatus(){
  try{
    const r=await fetch('/api/model_status');const d=await r.json();
    const el=document.getElementById('model-badge');
    if(d.status==='就绪'){
      el.textContent='FinBERT ✓';el.style.color='var(--green)';
    } else if(d.status==='加载中'){
      el.textContent='FinBERT 加载中...';el.style.color='var(--yellow)';
      setTimeout(pollModelStatus,5000);
    } else if(d.status==='失败'){
      el.textContent='VADER (降级)';el.style.color='var(--muted)';
    } else {
      el.textContent='VADER';el.style.color='var(--muted)';
      setTimeout(pollModelStatus,3000);
    }
  }catch(e){setTimeout(pollModelStatus,5000);}
}

let btChart=null;
async function runBacktest(){
  const btn=document.getElementById('bt-btn');
  btn.disabled=true;
  document.getElementById('bt-loading').style.display='inline';
  document.getElementById('bt-results').style.display='none';
  const period=document.getElementById('bt-period').value;
  try{
    const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({period:parseInt(period)})});
    const d=await r.json();
    if(d.error){alert(d.error);return;}
    document.getElementById('bt-results').style.display='block';
    const retCol=d.total_return>=0?'green':'red';
    document.getElementById('bt-cards').innerHTML=`
      <div class="card"><div class="clabel">总收益率</div><div class="cval ${retCol}">${d.total_return>=0?'+':''}${d.total_return}%</div><div class="csub">年化 ${d.annualized>=0?'+':''}${d.annualized}%</div></div>
      <div class="card"><div class="clabel">最大回撤</div><div class="cval red">${d.max_drawdown}%</div><div class="csub">${d.trading_days}个交易日</div></div>
      <div class="card"><div class="clabel">夏普比率</div><div class="cval ${d.sharpe>=1?'green':d.sharpe>=0?'yellow':'red'}">${d.sharpe}</div><div class="csub">RF=5%/年</div></div>
      <div class="card"><div class="clabel">胜率</div><div class="cval ${d.win_rate>=50?'green':'yellow'}">${d.win_rate}%</div><div class="csub">${d.total_trades}笔交易</div></div>`;
    // 净值曲线
    if(btChart)btChart.destroy();
    const nav=d.nav_series||[];
    const init=nav.length?nav[0].nav:100000;
    const vals=nav.map(n=>n.nav);
    const baseline=nav.map(()=>init);
    const col=vals[vals.length-1]>=init?'#1ecc6e':'#ff3d5a';
    btChart=new Chart(document.getElementById('bt-chart').getContext('2d'),{
      type:'line',data:{labels:nav.map(n=>n.date.slice(5)),datasets:[
        {data:vals,label:'策略净值',borderColor:col,backgroundColor:col+'12',fill:true,tension:0.4,pointRadius:0,borderWidth:1.5},
        {data:baseline,label:'基准线',borderColor:'#5a5a78',borderDash:[4,4],pointRadius:0,borderWidth:1,fill:false}
      ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#5a5a78',font:{size:10}}}},
        scales:{x:{ticks:{color:'#5a5a78',font:{size:9},maxTicksLimit:12},grid:{color:'#1a1a28'}},
                y:{ticks:{color:'#5a5a78',font:{size:9},callback:v=>'$'+Math.round(v/1000)+'k'},grid:{color:'#1a1a28'}}}}});
    // 交易列表
    const am2={BUY:'📥 买入',STOP_LOSS:'⚡ 止损',TAKE_PROFIT:'🎯 止盈',TRAILING_STOP:'📉 移动止损'};
    const trades=d.recent_trades||[];
    document.getElementById('bt-trades').innerHTML=trades.length?trades.slice().reverse().map(t=>`<tr>
      <td style="color:var(--muted);font-size:10px">${t.date}</td>
      <td><span class="badge ba">${t.ticker}</span></td>
      <td style="font-size:11px">${am2[t.action]||t.action}</td>
      <td>${fmt(t.price)}</td>
      <td class="${cc(t.pnl||0)}">${t.pnl?(t.pnl>=0?'+':'')+fmt(t.pnl):'—'}</td>
      <td class="${cc(t.pnl_pct||0)}">${t.pnl_pct?pct(t.pnl_pct):'—'}</td>
    </tr>`).join(''):'<tr><td colspan="6"><div class="empty">无交易记录</div></td></tr>';
  }catch(e){alert('回测失败: '+e.message);}
  finally{btn.disabled=false;document.getElementById('bt-loading').style.display='none';}
}

let lnCategory='';
function filterNews(cat,el){
  lnCategory=cat;
  document.querySelectorAll('.ln-filter').forEach(b=>{b.style.color='var(--muted)';b.classList.remove('active');});
  el.style.color='var(--text)';el.classList.add('active');
  loadLiveNews();
}
async function loadLiveNews(){
  try{
    const url=lnCategory?'/api/live_news?limit=80&category='+encodeURIComponent(lnCategory):'/api/live_news?limit=80';
    const r=await fetch(url);const d=await r.json();
    document.getElementById('ln-status').innerHTML=d.last_fetch?
      `上次爬取: ${d.last_fetch} · 第${d.fetch_count}轮${d.running?' <span style="color:var(--yellow);animation:blink 1.4s infinite">爬取中...</span>':''}`:
      '后台爬虫启动中...';
    document.getElementById('ln-count').textContent=d.total?d.total+'条有价值新闻':'';
    // 新闻流
    const items=d.items||[];
    document.getElementById('ln-feed').innerHTML=items.length?items.map(n=>{
      const sc=n.sentiment>0.1?'var(--green)':n.sentiment<-0.1?'var(--red)':'var(--muted)';
      const dot=n.sentiment>0.1?'🟢':n.sentiment<-0.1?'🔴':'⚪';
      return`<div style="padding:6px 0;border-bottom:1px solid rgba(34,34,51,0.35)">
        <div style="display:flex;align-items:flex-start;gap:6px">
          <span style="font-size:10px;flex-shrink:0;padding-top:2px">${dot}</span>
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;color:var(--text);line-height:1.5;word-break:break-word">${n.title}</div>
            <div style="display:flex;gap:8px;margin-top:2px;flex-wrap:wrap">
              <span style="font-size:9px;color:var(--muted)">${n.source||''}</span>
              <span style="font-size:9px;color:var(--accent)">${n.category||''}</span>
              ${n.kw_hits?`<span style="font-size:9px;color:var(--yellow)">命中${n.kw_hits}</span>`:''}
              <span style="font-size:9px;font-family:var(--mono);color:${sc}">${n.sentiment>0?'+':''}${n.sentiment}</span>
              <span style="font-size:9px;color:var(--muted)">${n.fetch_time||''}</span>
            </div>
          </div>
        </div>
      </div>`;}).join(''):'<div class="empty">暂无数据，等待爬虫完成首轮...</div>';
    // 宏观事件
    const ms=d.macro_snapshot||{};
    const mEl=document.getElementById('ln-macro');
    if(ms.events&&ms.events.length){
      const biasCol=ms.market_bias<=-1?'var(--red)':ms.market_bias>=1?'var(--green)':'var(--muted)';
      const biasText=ms.market_bias<=-2?'极度利空':ms.market_bias<=-1?'偏空':ms.market_bias>=2?'强势利好':ms.market_bias>=1?'偏多':'中性';
      mEl.innerHTML=`<div style="margin-bottom:8px"><span style="font-size:13px;font-weight:800;color:${biasCol}">${biasText}</span>
        <span style="font-size:10px;color:var(--muted);margin-left:8px">${ms.summary||''}</span></div>`+
        ms.events.map(e=>{const ec=e.market_bias<0?'var(--red)':e.market_bias>0?'var(--green)':'var(--muted)';
          return`<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid rgba(34,34,51,0.3)">
            <span style="font-size:10px;font-weight:600;color:${ec};min-width:100px">${e.name}</span>
            <span style="font-size:10px;color:var(--muted);flex:1">${e.desc}</span>
            <span style="font-size:10px;font-family:var(--mono);color:${e.avg_sentiment>0?'var(--green)':'var(--red)'}">${e.avg_sentiment>0?'+':''}${e.avg_sentiment}</span>
          </div>`;}).join('');
    } else { mEl.innerHTML='<div style="font-size:11px;color:var(--muted)">无重大宏观事件</div>'; }
    // 板块快照
    const ss2=d.sector_snapshot||{};
    const sEl=document.getElementById('ln-sectors');
    const sk2=Object.keys(ss2).sort((a,b)=>(ss2[b].heat||0)-(ss2[a].heat||0));
    sEl.innerHTML=sk2.length?sk2.map(k=>{const s=ss2[k],col=COLORS[k]||'#7c6eff';
      return`<div class="srow"><span class="sname">${k}</span>
        <div class="sbar"><div class="sbar-f" style="width:${s.heat||0}%;background:${col}"></div></div>
        <span class="sheat">${s.heat||0}</span>
        <span style="width:32px;text-align:right;font-size:10px;color:${s.score>0?'var(--green)':s.score<0?'var(--red)':'var(--muted)'}">${s.label||'—'}</span>
      </div>`;}).join(''):'<div class="empty">等待首轮爬取...</div>';
  }catch(e){console.error('loadLiveNews',e);}
}

function setCapitalPreset(v){document.getElementById('set-input').value=v;}
function updateSettingsPage(){
  const init=portfolio.config?.initial||100000;
  document.getElementById('set-cur').textContent='$'+init.toLocaleString();
  document.getElementById('set-input').placeholder='当前 '+init.toLocaleString();
}
async function applyCapital(){
  const v=parseFloat(document.getElementById('set-input').value);
  const msg=document.getElementById('set-msg');
  if(!v||v<1000){msg.style.color='var(--red)';msg.textContent='请输入 >= $1,000 的金额';return;}
  if(!confirm('确认将总资金修改为 $'+v.toLocaleString()+' ?')) return;
  const btn=document.getElementById('set-btn');btn.disabled=true;btn.textContent='处理中...';
  try{
    const r=await fetch('/api/set_capital',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capital:v})});
    const d=await r.json();
    if(d.ok){msg.style.color='var(--green)';msg.textContent='修改成功！新资金量 $'+d.capital.toLocaleString();
      document.getElementById('set-input').value='';load();}
    else{msg.style.color='var(--red)';msg.textContent=d.msg||'修改失败';}
  }catch(e){msg.style.color='var(--red)';msg.textContent='请求失败';}
  btn.disabled=false;btn.textContent='确认修改';
}
async function resetBaseline(){
  if(!confirm('将当前总资产设为新的盈亏基准线，累计盈亏将清零。确认？')) return;
  await fetch('/api/reset_baseline',{method:'POST'});
  await load();
}
async function resetAccount(){
  if(!confirm('确认重置？所有持仓和交易记录将被清空，此操作不可撤销！')) return;
  await fetch('/api/reset',{method:'POST'});load();
  document.getElementById('set-msg').style.color='var(--green)';
  document.getElementById('set-msg').textContent='账户已重置';
}

async function loadSchedule(){
  try{
    const r=await fetch('/api/market_schedule');const d=await r.json();
    const dot=d.market_open?'🟢':d.is_market_day?'🟡':'⚫';
    const txt=d.market_open?'开市中':d.is_market_day?'休市':'休市（'+d.et_weekday+'）';
    document.getElementById('sch-market-dot').textContent=dot;
    document.getElementById('sch-market-text').textContent=txt;
    document.getElementById('sch-market-text').style.color=d.market_open?'var(--green)':'var(--muted)';
    document.getElementById('sch-et').textContent=d.et_time;
    document.getElementById('sch-next').textContent='下次: '+d.next_scan;
    document.getElementById('sch-trades').textContent='今日 '+d.today_trades+' 笔交易';
    const autoEl=document.getElementById('sch-auto');
    autoEl.textContent=d.auto_enabled?'自动交易 开启':'仅分析模式';
    autoEl.style.background=d.auto_enabled?'rgba(30,204,110,0.12)':'rgba(90,90,120,0.12)';
    autoEl.style.color=d.auto_enabled?'var(--green)':'var(--muted)';
    // 同步 alpaca bar 上的 badge
    const ab=document.getElementById('auto-trade-badge');
    if(ab){ab.textContent=autoEl.textContent;ab.style.background=autoEl.style.background;ab.style.color=autoEl.style.color;}
  }catch(e){}
}
async function toggleAutoTrade(){
  try{
    const r=await fetch('/api/market_schedule');const cur=await r.json();
    const newVal=!cur.auto_enabled;
    if(!newVal&&!confirm('暂停自动交易后，系统只分析不下单。确认？')) return;
    await fetch('/api/auto_trade_toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:newVal})});
    loadSchedule();
  }catch(e){}
}
async function loadAlpacaStatus(){
  try{
    const r=await fetch('/api/alpaca_status');const d=await r.json();
    if(!d.enabled)return;
    document.getElementById('alpaca-bar').style.display='flex';
    document.getElementById('alpaca-dot').style.background=d.market_open?'var(--green)':'var(--muted)';
    const acc=d.account||{};
    document.getElementById('alpaca-equity').textContent=acc.equity?'$'+Number(acc.equity).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
    document.getElementById('alpaca-cash').textContent=acc.cash?'现金 $'+Number(acc.cash).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'现金 —';
    document.getElementById('alpaca-market').textContent=d.market_open?'🟢 开市中':'⚫ 已休市';
    document.getElementById('alpaca-market').style.color=d.market_open?'var(--green)':'var(--muted)';
  }catch(e){}
}
function updateScanBtn(){
  const btn=document.getElementById('scan-btn');
  const hasPos=(portfolio.positions||[]).length>0;
  btn.textContent=hasPos?'▶ 立即扫描':'▶ 立即扫描并建仓';
  btn.style.background=hasPos?'':'var(--green)';
  btn.style.color=hasPos?'':'#fff';
}
load();loadSummaries();pollModelStatus();loadLiveNews();loadAlpacaStatus();loadSchedule();
setInterval(load,30000);setInterval(loadSummaries,60000);setInterval(loadLiveNews,30000);setInterval(loadAlpacaStatus,60000);setInterval(loadSchedule,60000);
</script></body></html>"""

# ══════════════════════════════════════════════════════
#  持续新闻爬取引擎
# ══════════════════════════════════════════════════════

_live_news = {
    "items": [],          # 最新新闻列表
    "macro_items": [],    # 宏观新闻
    "last_fetch": "",     # 上次爬取时间
    "fetch_count": 0,     # 累计爬取次数
    "running": False,
    "sector_snapshot": {},  # 最新板块情绪快照
    "macro_snapshot": {},   # 最新宏观事件快照
}

def _news_crawler_loop():
    """后台持续爬取新闻，每10分钟一轮"""
    import traceback
    while True:
        try:
            _live_news["running"] = True
            now_str = now_pdt().strftime("%H:%M:%S")
            print(f"\n[新闻爬虫 {now_str}] ── 第{_live_news['fetch_count']+1}轮爬取开始 ──")

            # 爬取板块新闻
            news = fetch_all_news()
            # 爬取宏观新闻
            macro = fetch_macro_news()

            # 去重合并，按时间倒序
            seen = set()
            all_items = []
            for item in news + macro:
                t = item["title"]
                if t not in seen:
                    seen.add(t)
                    # 打情绪分
                    item["sentiment"] = score_sentiment(item["title"])
                    item["fetch_time"] = now_str
                    all_items.append(item)

            # 过滤高价值新闻（情绪绝对值 > 0.15 或 关键词命中 ≥ 1）
            all_kws = set()
            for info in SECTORS.values():
                all_kws.update(k.lower() for k in info["keywords"])
            for ev in MACRO_EVENTS.values():
                all_kws.update(k.lower() for k in ev["keywords"])

            valuable = []
            for item in all_items:
                text = (item["title"] + " " + item.get("desc", "")).lower()
                hits = sum(1 for kw in all_kws if kw in text)
                if hits >= 1 or abs(item.get("sentiment", 0)) > 0.15:
                    item["kw_hits"] = hits
                    valuable.append(item)

            # 按情绪绝对值+关键词命中排序
            valuable.sort(key=lambda x: -(x.get("kw_hits", 0) + abs(x.get("sentiment", 0))))

            # 保留最新200条
            _live_news["items"] = valuable[:200]
            _live_news["macro_items"] = [i for i in valuable if i.get("category") == "宏观政策"][:50]
            _live_news["last_fetch"] = now_pdt().strftime("%Y-%m-%d %H:%M:%S PDT")
            _live_news["fetch_count"] += 1

            # 板块情绪快照
            sector_snap = analyze_sector_sentiment(news)
            _live_news["sector_snapshot"] = sector_snap

            # 宏观事件快照
            macro_snap = analyze_macro_events(macro)
            _live_news["macro_snapshot"] = macro_snap

            total_v = len(valuable)
            top3 = valuable[:3]
            print(f"[新闻爬虫 {now_str}] 本轮: {len(news)}条板块 + {len(macro)}条宏观 → {total_v}条有价值新闻")
            for i, item in enumerate(top3):
                s = item.get("sentiment", 0)
                arrow = "🟢" if s > 0.1 else "🔴" if s < -0.1 else "⚪"
                print(f"  {arrow} [{item.get('source','')}] {item['title'][:70]} (情绪{s:+.2f} 命中{item.get('kw_hits',0)})")

            # 宏观事件提醒
            if macro_snap.get("events"):
                for ev in macro_snap["events"][:2]:
                    bias = "⚠️利空" if ev["market_bias"] < 0 else "✅利好" if ev["market_bias"] > 0 else "中性"
                    print(f"  🏛️ 宏观事件: {ev['name']} ({bias}) 命中{ev['matched_count']}条")

            print(f"[新闻爬虫] 下次爬取: 10分钟后\n")

        except Exception as e:
            print(f"[新闻爬虫] 出错: {e}")
            traceback.print_exc()

        _live_news["running"] = False
        time.sleep(600)  # 10分钟

def start_news_crawler():
    """启动后台新闻爬虫线程"""
    t = threading.Thread(target=_news_crawler_loop, daemon=True)
    t.start()
    print("[新闻爬虫] 后台持续爬取已启动（每10分钟一轮）")

# ══════════════════════════════════════════════════════
#  定时 & 启动
# ══════════════════════════════════════════════════════

def start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        s=BackgroundScheduler(timezone="America/New_York")
        # 盘前准备：09:25 只分析不下单
        s.add_job(run_premarket_analysis,"cron",hour=9,minute=25,day_of_week="mon-fri",id="premarket")
        # 开盘后立即扫描（信号最强）
        s.add_job(run_cycle_bg,"cron",hour=9,minute=31,day_of_week="mon-fri",id="scan_0931")
        # 盘中扫描
        s.add_job(run_cycle_bg,"cron",hour=11,minute=0,day_of_week="mon-fri",id="scan_1100")
        s.add_job(run_cycle_bg,"cron",hour=14,minute=0,day_of_week="mon-fri",id="scan_1400")
        s.add_job(run_cycle_bg,"cron",hour=15,minute=30,day_of_week="mon-fri",id="scan_1530")
        s.start()
        print("[定时] 工作日美东 09:25盘前 | 09:31/11:00/14:00/15:30 自动扫描")
    except ImportError:
        print("[提示] pip install apscheduler 开启定时自动交易")

def _auto_init_capital():
    """启动时从持久化数据恢复资金设置（不覆盖本地资金）"""
    data = load()
    if "initial_cash" in data:
        CONFIG["INITIAL_CASH"] = data["initial_cash"]
        print(f"[启动] 恢复资金设置: ${CONFIG['INITIAL_CASH']:,.2f}")

def _auto_first_scan():
    """若无交易记录，自动触发首次扫描"""
    data = load()
    if not data.get("trades"):
        print("[启动] 无交易记录，自动触发首次扫描...")
        threading.Thread(target=run_cycle_bg, daemon=True).start()

def _on_startup():
    """统一启动后台服务（兼容 gunicorn 和直接运行）"""
    _ensure_finbert()
    _auto_init_capital()
    start_scheduler()
    start_news_crawler()
    # 延迟5秒后检查是否需要首次扫描（等 FinBERT 加载）
    threading.Timer(5, _auto_first_scan).start()

# gunicorn / waitress 等 WSGI 服务器会直接 import app，需要在模块加载时启动后台
_startup_done = False
def ensure_startup():
    global _startup_done
    if not _startup_done:
        _startup_done = True
        _on_startup()

if __name__=="__main__":
    import webbrowser, sys, os
    ensure_startup()
    # 获取局域网 IP
    _lan_ip = "localhost"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        _lan_ip = s.getsockname()[0]
        s.close()
    except: pass
    print("\n🤖 智能模拟交易系统启动...")
    print(f"   本机访问: http://localhost:5200")
    print(f"   局域网访问: http://{_lan_ip}:5200")
    print(f"   把上面的局域网地址发给朋友即可（需同一Wi-Fi）")
    print("   Ctrl+C 退出\n")
    port = int(os.environ.get("PORT", 5200))
    threading.Timer(1.2,lambda:webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)
else:
    # 被 gunicorn/waitress import 时
    ensure_startup()
