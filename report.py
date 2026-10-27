"""關鍵指數小秘書：算 5日線與 KD，產生 HTML + 文字版，送到 Telegram。

規則以 docs/SPEC.md 為準；版面樣式直接取自 template/daily_report_template.html。

用法：
  python report.py --edition morning            # 只產生檔案到 out/
  python report.py --edition afternoon --send   # 產生並送 Telegram
環境變數：TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID（--send 時需要）
"""
import argparse
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "template" / "daily_report_template.html"
OUT = ROOT / "out"
SITE = ROOT / "site"
TW_TZ = timezone(timedelta(hours=8))
WEEKDAYS = "一二三四五六日"

# 排序即報告順序（SPEC 第 1 節）。group 相同的放同一張卡。
INDICES = [
    {"key": "SOX", "name": "費城半導體指數", "short": "費半", "src": "yahoo", "symbol": "^SOX", "market": "US", "group": "費城半導體指數"},
    {"key": "IXIC", "name": "那斯達克綜合", "short": "那指綜合", "src": "yahoo", "symbol": "^IXIC", "market": "US", "group": "那斯達克", "sub": "綜合指數", "code": "IXIC"},
    {"key": "NDX", "name": "那斯達克 100", "short": "那指100", "src": "yahoo", "symbol": "^NDX", "market": "US", "group": "那斯達克", "sub": "100 指數", "code": "NDX"},
    {"key": "TWII", "name": "加權指數", "short": "加權", "src": "finmind", "symbol": "TAIEX", "mis": "tse_t00.tw", "market": "TW", "group": "台灣加權指數"},
    {"key": "TPEX", "name": "櫃買指數", "short": "櫃買", "src": "finmind", "symbol": "TPEx", "mis": "otc_o00.tw", "market": "TW", "group": "櫃買指數"},
]


# ---------- 抓資料 ----------

def fetch_yahoo(symbol):
    df = yf.Ticker(symbol).history(period="9mo", auto_adjust=False)
    df.index = df.index.date
    return df[["High", "Low", "Close"]]


def fetch_finmind(data_id):
    start = (date.today() - timedelta(days=270)).isoformat()
    r = requests.get("https://api.finmindtrade.com/api/v4/data",
                     params={"dataset": "TaiwanStockPrice", "data_id": data_id, "start_date": start}, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["data"])
    df.index = pd.to_datetime(df["date"]).dt.date
    return df.rename(columns={"max": "High", "min": "Low", "close": "Close"})[["High", "Low", "Close"]]


def add_twse_today(df, mis_ch):
    """FinMind 收盤後要等一段時間才更新；13:35 先用證交所即時資料補上當天收盤。"""
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": mis_ch, "json": "1", "delay": "0"}, timeout=20)
        m = r.json()["msgArray"][0]
        day = datetime.strptime(m["d"], "%Y%m%d").date()
        if day > df.index[-1] and m["t"] >= "13:30:00":
            df.loc[day] = [float(m["h"]), float(m["l"]), float(m["z"])]
    except Exception as e:
        print(f"證交所即時資料抓不到（{mis_ch}）：{e}", flush=True)
    return df


VIX = {"key": "VIX", "name": "VIX 恐慌指數", "short": "VIX"}


def fetch_vix(today):
    """只有早報用。抓不到時回傳 None，報告照常送，這格顯示「抓不到」。"""
    try:
        df = fetch_yahoo("^VIX")
        return {"date": df.index[-1], "close": df["Close"].iloc[-1], "high": df["High"].iloc[-1],
                "stale": df.index[-1] < expected_date("US", "morning", today)}
    except Exception as e:
        print(f"VIX 抓不到：{e}", flush=True)
        return None


FG = {"key": "FG", "name": "仿 CNN 恐懼貪婪指數", "short": "恐懼貪婪"}
CNN_FG_URL = "https://edition.cnn.com/markets/fear-and-greed"


def fetch_fear_greed(today):
    """自己算的仿 CNN 指數（SPEC 第 4 節）：4 項指標各換成 50 + 15 × z 分數（過去 252 天），平均成 0-100。
    只有早報用；抓不到時回傳 None。"""
    try:
        px = yf.download(["^GSPC", "^VIX", "SPY", "IEF", "HYG", "LQD"], period="3y",
                         auto_adjust=True, progress=False)["Close"].dropna()
        raw = pd.DataFrame({
            "momentum": px["^GSPC"] / px["^GSPC"].rolling(125).mean(),         # 高 = 貪婪
            "volatility": -px["^VIX"] / px["^VIX"].rolling(50).mean(),         # VIX 高於均線 = 恐慌
            "safe_haven": px["SPY"].pct_change(20) - px["IEF"].pct_change(20),  # 股票贏債券 = 貪婪
            "junk_bond": px["HYG"].pct_change(20) - px["LQD"].pct_change(20),   # 垃圾債贏投資級債 = 貪婪
        }).dropna()
        z = (raw - raw.rolling(252).mean()) / raw.rolling(252).std()
        fg = (50 + 15 * z).clip(0, 100).dropna().mean(axis=1).round().astype(int)
        day = fg.index[-1].date()
        return {"date": day, "today": fg.iloc[-1], "yesterday": fg.iloc[-2],
                "stale": day < expected_date("US", "morning", today)}
    except Exception as e:
        print(f"恐懼貪婪指數算不出來：{e}", flush=True)
        return None


# ---------- 計算 ----------

def add_indicators(df):
    """MA5 = 最近 5 天收盤平均；KD(9,3,3)：RSV 取 9 天，K、D 各以 1/3 權重平滑，起始值 50。"""
    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    low9 = df["Low"].rolling(9).min()
    high9 = df["High"].rolling(9).max()
    rsv = ((df["Close"] - low9) / (high9 - low9) * 100).fillna(50)
    k, d, ks, ds = 50.0, 50.0, [], []
    for v in rsv:
        k = k * 2 / 3 + v / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    df["K"], df["D"] = ks, ds
    return df


def analyze(df):
    t, y = df.iloc[-1], df.iloc[-2]
    ma_cross = None
    if y["Close"] <= y["MA5"] and t["Close"] > t["MA5"]:
        ma_cross = "up"
    elif y["Close"] >= y["MA5"] and t["Close"] < t["MA5"]:
        ma_cross = "down"
    kd_cross = None
    if y["K"] <= y["D"] and t["K"] > t["D"]:
        kd_cross = "up"
    elif y["K"] >= y["D"] and t["K"] < t["D"]:
        kd_cross = "down"
    return {
        "date": df.index[-1],
        "close": t["Close"], "chg": t["Close"] - y["Close"], "chg_pct": (t["Close"] / y["Close"] - 1) * 100,
        "ma5": t["MA5"], "dist_pct": (t["Close"] / t["MA5"] - 1) * 100,
        "k": t["K"], "d": t["D"], "ma_cross": ma_cross, "kd_cross": kd_cross,
    }


def expected_date(market, edition, today):
    """早報看美股前一晚（台灣日期 - 1）；午報看台股當天。早報裡的台股不檢查新舊。"""
    if market == "US":
        return today - timedelta(days=1)
    return today if edition == "afternoon" else None


# ---------- 輸出 ----------

def f2(x):
    return f"{x:,.2f}"


def chg_text(r):
    if r["chg"] == 0:
        return "平盤", "flat"
    arrow, cls = ("▲", "up") if r["chg"] > 0 else ("▼", "down")
    return f"{arrow} {f2(abs(r['chg']))}（{r['chg_pct']:+.2f}%）", cls


def ma_pill(r):
    if r["ma_cross"] == "up":
        return "今天突破", "up"
    if r["ma_cross"] == "down":
        return "今天跌破", "down"
    return "持續", "flat"


def kd_pill(r):
    if r["kd_cross"] == "up":
        return "黃金交叉", "up"
    if r["kd_cross"] == "down":
        return "死亡交叉", "down"
    return ("K 在 D 上面" if r["k"] > r["d"] else "K 在 D 下面"), "flat"


def vix_alerts(v):
    """SPEC 第 4 節：盤中最高 > 20 就提醒；收盤在 20 以上每天提醒。"""
    if not v:
        return []
    if v["close"] >= 20:
        return [("warn", "⚠ 風險", VIX, f"收盤 {v['close']:.2f}，在 20 以上", "市場緊張，波動可能變大")]
    if v["high"] > 20:
        return [("warn", "⚠ 風險", VIX, f"盤中最高 {v['high']:.2f}，超過 20", f"收盤 {v['close']:.2f} 回到 20 以下")]
    return []


def vix_status(v):
    if v["close"] >= 20:
        return "在 20 以上", "收盤在 20 以上，市場緊張，波動可能變大。"
    if v["high"] > 20:
        return "盤中超過 20", f"盤中最高 {v['high']:.2f} 超過 20，收盤回到 20 以下。"
    return "沒超過 20", f"盤中最高 {v['high']:.2f}，沒超過 20，市場還算平靜。"


def fg_level(x):
    """CNN 分級（SPEC 第 4 節）。"""
    for top, name in ((24, "極度恐慌"), (44, "恐慌"), (55, "中性"), (75, "貪婪")):
        if x <= top:
            return name
    return "極度貪婪"


def vix_html(v):
    if not v:
        return '<div><h3>VIX 恐慌指數</h3><p>今天抓不到 VIX 資料。</p></div>'
    src = f"美股 {v['date'].month}/{v['date'].day} 收盤" + ("（還沒更新）" if v["stale"] else "")
    return f"""<div>
    <div class="card-top"><h3>VIX 恐慌指數　<span class="num">{v['close']:.2f}</span></h3><span class="src{" stale" if v["stale"] else ""}">{src}</span></div>
    <p>{vix_status(v)[1]}</p></div>"""


def fg_html(f):
    link = f'<a href="{CNN_FG_URL}">看 CNN 官方數字</a>'
    if not f:
        return f'<div><h3>{FG["name"]}</h3><p>今天算不出來。{link}</p></div>'
    src = f"美股 {f['date'].month}/{f['date'].day} 收盤" + ("（還沒更新）" if f["stale"] else "")
    return f"""<div>
    <div class="card-top"><h3>{FG['name']}　<span class="num">{f['today']}</span>（{fg_level(f['today'])}）</h3><span class="src{" stale" if f["stale"] else ""}">{src}</span></div>
    <p>前一天 <span class="num">{f['yesterday']}</span>（{fg_level(f['yesterday'])}）。這是我們自己算的參考值，和 CNN 常差 10 分左右，不會自動提醒。{link}</p>
    <div class="fg"><span></span><span></span><span></span><span></span><span></span><i style="left:{f['today']}%"></i></div>
    <div class="fg-labels"><span>極度恐慌</span><span>恐慌</span><span>中性</span><span>貪婪</span><span>極度貪婪</span></div></div>"""


def risk_html(v, f):
    return f'<section><h2>風險溫度計</h2><div class="risk">{vix_html(v)}{fg_html(f)}</div></section>'


def build_alerts(items, vix=None):
    alerts = []
    for ix, r in items:
        if r["ma_cross"] == "up":
            alerts.append(("up", "▲ 突破", ix, "漲回 5日線上面", "短線轉強"))
        elif r["ma_cross"] == "down":
            alerts.append(("down", "▼ 跌破", ix, "跌到 5日線下面", "短線轉弱"))
        if r["kd_cross"] == "up":
            alerts.append(("up", "▲ 黃金交叉", ix, "KD 黃金交叉", "K 往上穿過 D，可能開始轉強"))
        elif r["kd_cross"] == "down":
            alerts.append(("down", "▼ 死亡交叉", ix, "KD 死亡交叉", "K 往下穿過 D，可能開始轉弱"))
    return alerts + vix_alerts(vix)


def headline(alerts):
    if not alerts:
        return "今天<b>沒有特別提醒</b>：沒有指數穿過 5日線，也沒有 KD 交叉。"
    counts = [(sum(a[0] == c for a in alerts), label) for c, label in (("up", "轉強"), ("down", "轉弱"), ("warn", "風險"))]
    parts = [f"{n} 個{label}" for n, label in counts if n]
    return f"今天有 <b>{len(alerts)} 個提醒</b>：{'、'.join(parts)}。"


def src_label(ix, r, stale):
    label = f"{'美股' if ix['market'] == 'US' else '台股'} {r['date'].month}/{r['date'].day} 收盤"
    return label + ("（還沒更新）" if stale else "")


def html_block(r):
    ct, cc = chg_text(r)
    mp, mc = ma_pill(r)
    kp, kc = kd_pill(r)
    side, dc = ("在線上", "up") if r["dist_pct"] >= 0 else ("在線下", "down")
    pos = 50 + max(-3, min(3, r["dist_pct"])) / 3 * 50
    return f"""<div class="price"><span class="close num">{f2(r['close'])}</span><span class="chg num {cc}">{ct}</span></div>
        <dl class="rows">
          <div class="row"><dt>5日線</dt><dd><span class="num">{f2(r['ma5'])}</span>　{side} <span class="num {dc}">{r['dist_pct']:+.1f}%</span><span class="pill {mc}">{mp}</span></dd></div>
          <div class="row"><dt>KD</dt><dd>K <span class="num">{r['k']:.1f}</span> ／ D <span class="num">{r['d']:.1f}</span><span class="pill {kc}">{kp}</span></dd></div>
        </dl>
        <div><div class="meter"><span class="mid"></span><span class="dot {dc}" style="left:{pos:.1f}%"></span></div><div class="meter-labels"><span>線下 -3%</span><span>5日線</span><span>線上 +3%</span></div></div>"""


def render_html(items, stale_keys, edition, today, now, vix, fg):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    head = tpl.split('<div class="wrap">')[0]
    alerts = build_alerts(items, vix)

    alert_html = "\n".join(
        f'<li class="alert"><span class="tag {c}">{tag}</span><p><b>{ix["name"]}</b> {what}<small>{note}</small></p></li>'
        for c, tag, ix, what, note in alerts) or '<li class="alert"><span class="tag">無</span><p>今天沒有指數穿過 5日線或 KD 交叉</p></li>'

    cards, groups = [], {}
    for ix, r in items:
        groups.setdefault(ix["group"], []).append((ix, r))
    for name, members in groups.items():
        ix0, r0 = members[0]
        stale = any(ix["key"] in stale_keys for ix, _ in members)
        top = f'<div class="card-top"><h3>{name}</h3><span class="src{" stale" if stale else ""}">{src_label(ix0, r0, stale)}</span></div>'
        if len(members) == 1:
            body = html_block(r0)
        else:
            body = "\n".join(f'<div class="sub"><h4>{ix["sub"]}<span class="code">{ix["code"]}</span></h4>{html_block(r)}</div>'
                             for ix, r in members)
        cards.append(f'<article class="card">{top}{body}</article>')

    edition_label = "早報" if edition == "morning" else "午報"
    body = f"""<div class="wrap">
  <header>
    <h1>關鍵指數小秘書</h1>
    <p class="date">{today:%Y/%m/%d}（{WEEKDAYS[today.weekday()]}）<span class="edition">{edition_label}</span></p>
    <p class="headline">{headline(alerts)}</p>
  </header>
  <section><h2>特別提醒</h2><ul class="alerts">{alert_html}</ul></section>
  <section><h2>指數狀態</h2><div class="cards">{''.join(cards)}</div></section>
  {risk_html(vix, fg) if edition == "morning" else ""}
  {re.search(r'<details>.*?</details>', tpl, re.S).group(0)}
  <footer>
    資料來源：美股 Yahoo Finance、台股 FinMind；恐懼貪婪指數為自行計算，非 CNN 官方<br>
    KD 參數：日線 KD(9,3,3)<br>
    產生時間：{now:%Y/%m/%d %H:%M}（台灣時間）<br>
    這份是提醒，不是投資建議。
  </footer>
</div>"""
    return ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'{head}</head><body>{body}</body></html>')


def render_text(items, stale_keys, edition, today, vix, fg):
    alerts = build_alerts(items, vix)
    plain = re.sub(r"</?b>", "", headline(alerts))
    lines = [f"關鍵指數小秘書｜{today.month}/{today.day}（{WEEKDAYS[today.weekday()]}）{'早報' if edition == 'morning' else '午報'}",
             plain, "", "【特別提醒】"]
    lines += [f"{tag.split()[0]} {ix['short']}：{what}（{note.split('，')[-1]}）" for _, tag, ix, what, note in alerts] or ["無"]
    lines += ["", "【指數狀態】"]
    for ix, r in items:
        ct, _ = chg_text(r)
        mp, _ = ma_pill(r)
        kp, _ = kd_pill(r)
        side = "線上" if r["dist_pct"] >= 0 else "線下"
        stale = "（還沒更新）" if ix["key"] in stale_keys else ""
        lines.append(f"{ix['short']} {r['close']:,.2f} {ct.split('（')[0][0]}{abs(r['chg_pct']):.2f}%（{r['date'].month}/{r['date'].day}）{stale}")
        lines.append(f"　5日線 {side} {r['dist_pct']:+.1f}% {mp}｜KD {r['k']:.1f}/{r['d']:.1f} {kp}")
    if edition == "morning":
        lines += ["", "【風險溫度計】"]
        if vix:
            stale = "（還沒更新）" if vix["stale"] else ""
            lines.append(f"VIX {vix['close']:.2f}（盤中最高 {vix['high']:.2f}）{vix_status(vix)[0]}（{vix['date'].month}/{vix['date'].day}）{stale}")
        else:
            lines.append("VIX 今天抓不到資料")
        if fg:
            stale = "（還沒更新）" if fg["stale"] else ""
            lines.append(f"恐懼貪婪（仿 CNN，參考）{fg['today']} {fg_level(fg['today'])}｜前一天 {fg['yesterday']} {fg_level(fg['yesterday'])}"
                         f"（{fg['date'].month}/{fg['date'].day}）{stale}")
        else:
            lines.append("恐懼貪婪（仿 CNN）今天算不出來")
        lines.append(f"CNN 官方：{CNN_FG_URL}")
    lines += ["", "紅/▲ = 轉強，綠/▼ = 轉弱，⚠ = 風險｜提醒不是投資建議"]
    return "\n".join(lines)


def send_telegram(text, html_path):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}"
    requests.post(f"{api}/sendMessage", data={"chat_id": chat, "text": text}, timeout=30).raise_for_status()
    if html_path:
        with open(html_path, "rb") as f:
            requests.post(f"{api}/sendDocument", data={"chat_id": chat, "caption": "完整 HTML 報告"},
                          files={"document": (html_path.name, f, "text/html")}, timeout=60).raise_for_status()


# ---------- 主程式 ----------

def collect(edition, today):
    chosen = [ix for ix in INDICES if edition == "morning" or ix["market"] == "TW"]
    items, stale, checked = [], set(), set()
    for ix in chosen:
        df = fetch_yahoo(ix["symbol"]) if ix["src"] == "yahoo" else add_twse_today(fetch_finmind(ix["symbol"]), ix["mis"])
        r = analyze(add_indicators(df))
        exp = expected_date(ix["market"], edition, today)
        if exp:
            checked.add(ix["key"])
            if r["date"] < exp:
                stale.add(ix["key"])
        items.append((ix, r))
    return items, stale, checked


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--edition", choices=["morning", "afternoon"], required=True)
    p.add_argument("--send", action="store_true", help="送到 Telegram")
    p.add_argument("--wait-minutes", type=int, default=0, help="資料還沒更新時，最多等幾分鐘（每 5 分鐘重試）")
    p.add_argument("--link-base", help="報告網頁的網址開頭；有給就把 HTML 放進 site/，訊息改附連結")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    deadline = time.time() + args.wait_minutes * 60
    while True:
        now = datetime.now(TW_TZ)
        items, stale, checked = collect(args.edition, now.date())
        if not stale or time.time() >= deadline:
            break
        print(f"資料還沒更新：{sorted(stale)}，5 分鐘後重試", flush=True)
        time.sleep(300)

    today = now.date()
    OUT.mkdir(exist_ok=True)
    html_path = None
    if stale == checked:
        # 要看的市場全部沒新資料 → 多半是休市，只送一句話
        text = f"關鍵指數小秘書｜{today.month}/{today.day}\n今天沒有新資料（可能休市，或資料延遲）。"
    else:
        vix = fetch_vix(today) if args.edition == "morning" else None
        fg = fetch_fear_greed(today) if args.edition == "morning" else None
        html_path = OUT / f"report_{today:%Y%m%d}_{args.edition}.html"
        html_path.write_text(render_html(items, stale, args.edition, today, now, vix, fg), encoding="utf-8")
        text = render_text(items, stale, args.edition, today, vix, fg)
        if args.link_base:
            # 雲端排程用：放進 site/ 由 GitHub Pages 發布，訊息附連結
            SITE.mkdir(exist_ok=True)
            shutil.copy(html_path, SITE / html_path.name)
            shutil.copy(html_path, SITE / "index.html")
            text += f"\n\n完整報告：{args.link_base.rstrip('/')}/{html_path.name}"
    (OUT / "message.txt").write_text(text, encoding="utf-8")
    print(text)
    if args.send:
        send_telegram(text, None if args.link_base else html_path)
        print("已送出 Telegram")


if __name__ == "__main__":
    main()
