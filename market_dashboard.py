# -*- coding: utf-8 -*-
"""
台美波動率作戰儀表板 market_dashboard.py
========================================
抓取:  ^VIX / ^IXIC(那斯達克) / ^SOX(費半) / ^TWII(加權) / VIXTWN(台指VIX)
計算:  MA5 / MA20 / MA60、乖離率、三批進場觸發條件
輸出:  docs/index.html (自包含單頁, 適合 GitHub Pages) + 可選 Telegram 推播

用法:
  python market_dashboard.py                # 正式抓資料並產出 HTML
  python market_dashboard.py --mock         # 用範例資料產出 HTML (測版面)
  python market_dashboard.py --telegram     # 產出 HTML 並推 Telegram 摘要
環境變數 (Telegram, 沿用 tw-invest 慣例):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

TPE = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 三批進場參數（動態）
# 點位不寫死：每次執行用「波段高點(近120日收盤高)」與「當日季線MA60」重算
#   第1批 = 高點回檔 7.5%–8.5%，或 台指VIX > 25
#   第2批 = 季線 MA60 ±1%（季線上移, 區間自動跟著上移 → 涵蓋以盤代跌劇本）
#   第3批 = 跌破季線 3% 以上，或 VIX > 40
B1_PULLBACK = (0.915, 0.925)   # 高點 × 此區間
B2_BAND = 0.01                 # 季線 ±1%
B3_BELOW_MA = 0.97             # 季線 × 0.97 以下
TVIX_B1, VIX_B3 = 25, 40


def build_batches(metrics):
    """回傳 [(batch_dict, hit_bool), ...]，zone 上下限一併回傳供價格梯繪圖。"""
    tw = metrics["TWII"]
    if not tw["ok"]:
        return []
    px = tw["close"]
    hi = tw["hi120"]
    ma60 = tw["ma60"] or px
    tvix = metrics["VIXTWN"]["close"]
    vix = metrics["VIX"]["close"]

    b1_lo, b1_hi = round(hi * B1_PULLBACK[0]), round(hi * B1_PULLBACK[1])
    b2_lo, b2_hi = round(ma60 * (1 - B2_BAND)), round(ma60 * (1 + B2_BAND))
    b3_hi = round(ma60 * B3_BELOW_MA)

    batches = [
        {"name": "第1批 20-25%", "lo": b1_lo, "hi": b1_hi,
         "zone": f"{b1_lo:,} – {b1_hi:,}",
         "desc": f"波段高點 {hi:,.0f} 回檔7.5–8.5%，或台指VIX>{TVIX_B1}",
         "hit": px <= b1_hi or (tvix is not None and tvix > TVIX_B1)},
        {"name": "第2批 40%（最重）", "lo": b2_lo, "hi": b2_hi,
         "zone": f"{b2_lo:,} – {b2_hi:,}",
         "desc": f"季線 MA60 {ma60:,.0f} ±1%（每日重算, 自動上移）",
         "hit": px <= b2_hi},
        {"name": "第3批 35%", "lo": None, "hi": b3_hi,
         "zone": f"{b3_hi:,} 以下",
         "desc": f"跌破季線3%以上；或已深入第2批下緣後遇 VIX>{VIX_B3} 過度殺跌",
         "hit": px <= b3_hi or (vix is not None and vix > VIX_B3 and px <= b2_lo)},
    ]
    return [(b, b["hit"]) for b in batches]

SYMBOLS = {
    "VIX":    {"yf": "^VIX",  "label": "VIX 恐慌指數",   "kind": "vol",   "zones": [(0, 20, "平靜"), (20, 28, "警戒"), (28, 40, "恐慌"), (40, 999, "極端")]},
    "VIXTWN": {"yf": None,    "label": "台指VIX",        "kind": "vol",   "zones": [(0, 25, "平靜"), (25, 30, "警戒"), (30, 40, "恐慌"), (40, 999, "極端")]},
    "IXIC":   {"yf": "^IXIC", "label": "那斯達克",        "kind": "price"},
    "SOX":    {"yf": "^SOX",  "label": "費城半導體",      "kind": "price"},
    "TWII":   {"yf": "^TWII", "label": "台灣加權指數",    "kind": "price"},
}

LOOKBACK_DAYS = 130   # 足夠算 MA60 並留 60 根 sparkline


# ---------------------------------------------------------------- 資料抓取
def fetch_yf(symbol: str):
    """回傳 (dates, closes) 由舊到新。用 yfinance；失敗回 (None, None)。"""
    try:
        import yfinance as yf
        end = datetime.now(TPE)
        start = end - timedelta(days=LOOKBACK_DAYS * 2)
        df = yf.download(symbol, start=start.strftime("%Y-%m-%d"),
                         auto_adjust=False, progress=False, interval="1d")
        if df is None or df.empty:
            return None, None
        closes = df["Close"]
        if hasattr(closes, "columns"):  # MultiIndex 保護
            closes = closes.iloc[:, 0]
        closes = closes.dropna()
        dates = [d.strftime("%Y-%m-%d") for d in closes.index]
        return dates[-LOOKBACK_DAYS:], [float(v) for v in closes.values][-LOOKBACK_DAYS:]
    except Exception as e:
        print(f"[warn] yfinance {symbol} 失敗: {e}", file=sys.stderr)
        return None, None


def _parse_vix_rows(rows):
    """rows: iterable of (date_str, value)。回傳 (dates, closes) 或 (None, None)。"""
    dates, closes = [], []
    for ds, v in rows:
        for f in ("%Y/%m/%d", "%Y-%m-%d"):
            try:
                d = datetime.strptime(ds.strip(), f).strftime("%Y-%m-%d")
                break
            except ValueError:
                d = None
        if d is None:
            continue
        try:
            closes.append(float(str(v).replace(",", "").strip()))
            dates.append(d)
        except ValueError:
            continue
    if not closes:
        return None, None
    pairs = sorted(zip(dates, closes))
    dates = [p[0] for p in pairs][-LOOKBACK_DAYS:]
    closes = [p[1] for p in pairs][-LOOKBACK_DAYS:]
    return dates, closes


TAIFEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://www.taifex.com.tw/cht/7/vixDaily3MNew",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def fetch_vixtwn():
    """台指VIX (VIXTWN) v3: 從期交所「前3個月每日收盤」頁面自我發現資料連結。

    步驟: 1) GET/POST 頁面 → 先試著直接解析表格內的 日期+數值
          2) 解析頁面上所有含 vix / file 的連結, 逐一下載嘗試解析 CSV
          全程印 [diag] 供 Actions log 除錯。
    """
    import re
    page_url = "https://www.taifex.com.tw/cht/7/vixDaily3MNew"
    all_rows = []
    try:
        html = ""
        for method in ("GET", "POST"):
            r = (requests.get(page_url, headers=TAIFEX_HEADERS, timeout=25) if method == "GET"
                 else requests.post(page_url, headers=TAIFEX_HEADERS, data={}, timeout=25))
            print(f"[diag] {method} vixDaily3MNew -> HTTP {r.status_code}, {len(r.text)} chars", file=sys.stderr)
            if r.ok and len(r.text) > 1000:
                html = r.text
                # 1) 直接解析頁面表格: <td>YYYY/MM/DD</td><td>數值</td>
                rows = re.findall(
                    r">(\d{4}/\d{2}/\d{2})<[^>]*>\s*(?:</td>\s*<td[^>]*>)?\s*([0-9]+\.[0-9]+)<",
                    html.replace("\n", " "))
                print(f"[diag] {method} 頁內表格解析到 {len(rows)} 筆", file=sys.stderr)
                all_rows.extend(rows)
                if len(all_rows) >= 40:
                    break
        if len(all_rows) >= 40:
            out = _parse_vix_rows(all_rows)
            if out[0]:
                print(f"[ok] VIXTWN 來源=頁內表格, {len(out[0])}筆", file=sys.stderr)
                return out

        # 2) 從頁面找出所有可能的資料檔連結
        links = re.findall(r'href="([^"]+)"', html, flags=re.I)
        cands = []
        for lk in links:
            low = lk.lower()
            if ("vix" in low or "/file/" in low) and not low.startswith("javascript"):
                if low.endswith((".css", ".js", ".png", ".jpg", ".gif", ".pdf")):
                    continue
                if lk.startswith("/"):
                    lk = "https://www.taifex.com.tw" + lk
                if lk.startswith("http") and lk not in cands:
                    cands.append(lk)
        print(f"[diag] 頁面候選連結 {len(cands)} 個:", file=sys.stderr)
        for c in cands[:15]:
            print(f"[diag]   {c}", file=sys.stderr)

        for lk in cands[:15]:
            if "/cht/7/vix" in lk and lk.rstrip("/").endswith(("vixMinNew", "vixMin3MNew", "vixQA", "vixDaily3MNew")):
                continue  # 導覽頁, 跳過
            try:
                r = requests.get(lk, headers=TAIFEX_HEADERS, timeout=25)
                body = r.content.decode("big5", errors="ignore")
                if "<html" in body[:300].lower():
                    body = r.content.decode("utf-8", errors="ignore")
                    if "<html" in body[:300].lower():
                        continue
                rows = []
                for line in body.splitlines():
                    parts = [p.strip().strip('"') for p in line.replace("\t", ",").split(",")]
                    if len(parts) >= 2:
                        rows.append((parts[0], parts[-1]))
                out = _parse_vix_rows(rows)
                if out[0] and len(out[0]) >= 15:
                    print(f"[ok] VIXTWN 來源={lk}, {len(out[0])}筆", file=sys.stderr)
                    return out
            except Exception as e:
                print(f"[warn] 候選 {lk} 失敗: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] vixDaily3MNew 抓取失敗: {e}", file=sys.stderr)

    print("[warn] VIXTWN 全部候選來源失敗, 卡片將顯示資料源失敗", file=sys.stderr)
    return None, None


def mock_series(base, drift, vol, n=LOOKBACK_DAYS, seed=1):
    import random
    random.seed(seed)
    today = datetime.now(TPE)
    dates, closes, v = [], [], base
    for i in range(n):
        v = max(1.0, v * (1 + drift + random.uniform(-vol, vol)))
        dates.append((today - timedelta(days=n - i)).strftime("%Y-%m-%d"))
        closes.append(round(v, 2))
    return dates, closes


# ---------------------------------------------------------------- 指標計算
def ma(vals, n):
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def build_metric(key, dates, closes):
    cfg = SYMBOLS[key]
    if not closes:
        return {"key": key, "label": cfg["label"], "kind": cfg["kind"], "ok": False,
                "close": None, "chg": None, "ma5": None, "ma20": None, "ma60": None,
                "hi120": None, "bias60": None, "zone": None, "spark": [], "date": None}
    close, prev = closes[-1], (closes[-2] if len(closes) > 1 else closes[-1])
    m5, m20, m60 = ma(closes, 5), ma(closes, 20), ma(closes, 60)
    out = {
        "key": key, "label": cfg["label"], "kind": cfg["kind"], "ok": True,
        "date": dates[-1], "close": round(close, 2),
        "chg": round((close / prev - 1) * 100, 2),
        "ma5": m5, "ma20": m20, "ma60": m60,
        "hi120": round(max(closes[-120:]), 2),
        "bias60": round((close / m60 - 1) * 100, 2) if m60 else None,
        "spark": [round(v, 2) for v in closes[-60:]],
        "zone": None,
    }
    if cfg["kind"] == "vol":
        for lo, hi, name in cfg["zones"]:
            if lo <= close < hi:
                out["zone"] = name
                break
    return out


# ---------------------------------------------------------------- HTML 產出
def sparkline_svg(vals, w=220, h=44, cls=""):
    if not vals or len(vals) < 2:
        return "<svg class='spark'></svg>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(
        f"{round(i * w / (len(vals) - 1), 1)},{round(h - 3 - (v - lo) / rng * (h - 8), 1)}"
        for i, v in enumerate(vals))
    return (f"<svg class='spark {cls}' viewBox='0 0 {w} {h}' preserveAspectRatio='none'>"
            f"<polyline points='{pts}' fill='none' stroke='currentColor' "
            f"stroke-width='1.6' vector-effect='non-scaling-stroke'/></svg>")


def fmt(v, nd=2):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}"


def render_html(metrics, batches_state, ts):
    twii = metrics["TWII"]
    ma60 = twii["ma60"] or (twii["close"] or 0)
    hi120 = twii["hi120"] or (twii["close"] or 0)
    batches = [b for b, _ in batches_state]

    # 價格梯上下界: 動態涵蓋 高點+緩衝 到 第3批下緣-緩衝
    b3_hi = batches[2]["hi"] if batches else round(ma60 * 0.97)
    ladder_hi = round(hi120 * 1.006)
    ladder_lo = round(b3_hi * 0.988)

    def ypos(price):
        p = max(ladder_lo, min(ladder_hi, price))
        return round((ladder_hi - p) / (ladder_hi - ladder_lo) * 100, 2)

    zones_html = ""
    if batches:
        b1, b2, b3 = batches
        zones_html = f"""
      <div class="lz b1" style="top:{ypos(b1['hi'])}%;height:{ypos(b1['lo'])-ypos(b1['hi'])}%"><span>第1批 {b1['zone']}</span></div>
      <div class="lz b2" style="top:{ypos(b2['hi'])}%;height:{ypos(b2['lo'])-ypos(b2['hi'])}%"><span>第2批 {b2['zone']}</span></div>
      <div class="lz b3" style="top:{ypos(b3['hi'])}%;height:{100-ypos(b3['hi'])}%"><span>第3批 {b3['zone']}</span></div>
      <div class="lmark hi" style="top:{ypos(hi120)}%"><i></i>波段高點 {fmt(hi120,0)}</div>
      <div class="lmark ma" style="top:{ypos(ma60)}%"><i></i>季線 MA60 {fmt(ma60,0)}（每日重算）</div>
      <div class="lmark now" style="top:{ypos(twii['close'] or ma60)}%"><i></i>現價 {fmt(twii['close'],0)}</div>
    """

    cards = ""
    for key in ["TWII", "IXIC", "SOX", "VIX", "VIXTWN"]:
        m = metrics[key]
        up = (m["chg"] or 0) >= 0
        cls = "up" if up else "dn"   # 台灣慣例: 紅漲綠跌
        if not m["ok"]:
            cards += (f"<article class='card off'><header><h2>{m['label']}</h2>"
                      f"<span class='badge miss'>資料源失敗</span></header>"
                      f"<p class='px'>—</p><p class='sub'>請檢查抓取端點</p></article>")
            continue
        zone_badge = f"<span class='badge z{m['zone']}'>{m['zone']}</span>" if m["zone"] else ""
        ma_row = ""
        if m["kind"] == "price":
            ma_row = (f"<dl class='mas'>"
                      f"<div><dt>MA5</dt><dd class='{'ok' if m['close']>=m['ma5'] else 'bad'}'>{fmt(m['ma5'],0)}</dd></div>"
                      f"<div><dt>MA20</dt><dd class='{'ok' if m['close']>=m['ma20'] else 'bad'}'>{fmt(m['ma20'],0)}</dd></div>"
                      f"<div><dt>MA60</dt><dd class='{'ok' if m['close']>=m['ma60'] else 'bad'}'>{fmt(m['ma60'],0)}</dd></div>"
                      f"<div><dt>季線乖離</dt><dd>{'+' if (m['bias60'] or 0)>=0 else ''}{fmt(m['bias60'])}%</dd></div></dl>")
        else:
            ma_row = (f"<dl class='mas'><div><dt>MA5</dt><dd>{fmt(m['ma5'])}</dd></div>"
                      f"<div><dt>MA20</dt><dd>{fmt(m['ma20'])}</dd></div>"
                      f"<div><dt>MA60</dt><dd>{fmt(m['ma60'])}</dd></div></dl>")
        cards += f"""
        <article class="card {'vol' if m['kind']=='vol' else ''}">
          <header><h2>{m['label']}</h2>{zone_badge}</header>
          <p class="px {cls}">{fmt(m['close'])}<small>{'+' if up else ''}{fmt(m['chg'])}%</small></p>
          {sparkline_svg(m['spark'], cls=cls)}
          {ma_row}
          <p class="sub">資料日 {m['date']}</p>
        </article>"""

    rows = ""
    for b, hit in batches_state:
        rows += (f"<tr class='{'hit' if hit else ''}'><td>{b['name']}</td>"
                 f"<td class='mono'>{b['zone']}</td><td>{b['desc']}</td>"
                 f"<td class='st'>{'● 觸發' if hit else '待命'}</td></tr>")

    return f"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>台美波動作戰盤 | {ts}</title>
<style>
:root{{
  --bg:#0d1220;--panel:#151c30;--edge:#232d4a;--txt:#dfe6f5;--dim:#7b87a6;
  --up:#ff5a6e;--dn:#2fd08c;      /* 台灣慣例: 紅漲綠跌 */
  --amber:#f5b942;--mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --sans:'Noto Sans TC',system-ui,sans-serif;
}}
*{{box-sizing:border-box;margin:0}}
body{{background:var(--bg);color:var(--txt);font-family:var(--sans);padding:14px;max-width:1080px;margin:auto}}
h1{{font-size:17px;letter-spacing:.14em;font-weight:700}}
.top{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px;border-bottom:1px solid var(--edge);padding-bottom:10px}}
.top time{{font-family:var(--mono);color:var(--dim);font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:10px}}
.card{{background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:12px 14px}}
.card header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
.card h2{{font-size:13px;font-weight:500;color:var(--dim)}}
.px{{font-family:var(--mono);font-size:26px;font-weight:600}}
.px small{{font-size:12px;margin-left:8px}}
.up{{color:var(--up)}}.dn{{color:var(--dn)}}
.spark{{width:100%;height:44px;margin:6px 0 4px;opacity:.85}}
.mas{{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;font-size:12px}}
.mas dt{{color:var(--dim);display:inline}}.mas dd{{display:inline;font-family:var(--mono);float:right}}
.mas .ok{{color:var(--up)}}.mas .bad{{color:var(--dn)}}
.sub{{color:var(--dim);font-size:11px;margin-top:8px}}
.badge{{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--edge);font-family:var(--mono)}}
.z警戒{{color:var(--amber);border-color:var(--amber)}}
.z恐慌,.z極端{{color:var(--up);border-color:var(--up)}}
.miss{{color:var(--amber);border-color:var(--amber)}}
.card.off{{opacity:.55}}
section{{margin-top:18px}}
section>h2{{font-size:13px;color:var(--dim);letter-spacing:.12em;margin-bottom:10px}}
.battle{{display:grid;grid-template-columns:150px 1fr;gap:14px;background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:14px}}
.ladder{{position:relative;height:330px;border-left:2px solid var(--edge)}}
.lz{{position:absolute;left:0;right:0;border-left:3px solid var(--amber);background:rgba(245,185,66,.08)}}
.lz.b2{{border-color:var(--up);background:rgba(255,90,110,.10)}}
.lz span{{position:absolute;left:8px;top:2px;font-size:10px;color:var(--dim);white-space:nowrap}}
.lmark{{position:absolute;left:0;right:0;font-size:11px;font-family:var(--mono);color:var(--dim)}}
.lmark i{{display:inline-block;width:26px;border-top:1px dashed var(--dim);vertical-align:middle;margin-right:6px}}
.lmark.now{{color:var(--txt);font-weight:700}}.lmark.now i{{border-top:2px solid var(--txt)}}
.lmark.ma{{color:var(--amber)}}.lmark.ma i{{border-color:var(--amber)}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
td{{padding:8px 6px;border-top:1px solid var(--edge);vertical-align:top}}
.mono{{font-family:var(--mono);white-space:nowrap}}
.st{{white-space:nowrap;color:var(--dim)}}
tr.hit .st{{color:var(--amber);font-weight:700}}
tr.hit td:first-child{{color:var(--amber)}}
footer{{margin-top:16px;color:var(--dim);font-size:10.5px;line-height:1.6}}
@media(max-width:640px){{.battle{{grid-template-columns:1fr}}.ladder{{height:280px}}}}
</style></head><body>
<div class="top"><h1>台美波動作戰盤</h1><time>更新 {ts}（台北）</time></div>
<div class="grid">{cards}</div>
<section><h2>三批進場作戰圖 — 加權指數</h2>
  <div class="battle">
    <div class="ladder">{zones_html}</div>
    <table><tbody>{rows}</tbody></table>
  </div>
</section>
<footer>紅漲綠跌（台灣慣例）。均線為日收盤簡單移動平均；季線每日重算，勿用固定點位。
波動率分區 — VIX：20警戒 / 28恐慌 / 40極端；台指VIX：25 / 30 / 40。
資料源：Yahoo Finance、臺灣期貨交易所。本頁為個人監控工具，非投資建議。</footer>
</body></html>"""


# ---------------------------------------------------------------- Telegram
def push_telegram(metrics, batches_state, ts):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[warn] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, 略過推播", file=sys.stderr)
        return
    lines = [f"📟 波動作戰盤 {ts}"]
    for k in ["TWII", "IXIC", "SOX", "VIX", "VIXTWN"]:
        m = metrics[k]
        if not m["ok"]:
            lines.append(f"{m['label']}: 資料源失敗")
            continue
        seg = f"{m['label']} {fmt(m['close'])} ({'+' if m['chg']>=0 else ''}{m['chg']}%)"
        if m["kind"] == "price":
            seg += f" | MA60 {fmt(m['ma60'],0)} 乖離{'+' if m['bias60']>=0 else ''}{m['bias60']}%"
        elif m["zone"]:
            seg += f" [{m['zone']}]"
        lines.append(seg)
    hits = [b["name"] for b, h in batches_state if h]
    lines.append("🎯 觸發: " + ("、".join(hits) if hits else "無，各批待命"))
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": "\n".join(lines)}, timeout=15)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="用範例資料產版面")
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--out", default="docs/index.html")
    args = ap.parse_args()

    raw = {}
    if args.mock:
        raw["VIX"] = mock_series(17, 0.004, 0.05, seed=3)
        raw["VIXTWN"] = mock_series(21, 0.005, 0.05, seed=4)
        raw["IXIC"] = mock_series(21500, 0.0018, 0.012, seed=5)
        raw["SOX"] = mock_series(6900, 0.002, 0.02, seed=6)
        raw["TWII"] = mock_series(41000, 0.002, 0.011, seed=7)
    else:
        for key, cfg in SYMBOLS.items():
            raw[key] = fetch_yf(cfg["yf"]) if cfg["yf"] else fetch_vixtwn()

    metrics = {k: build_metric(k, *raw[k]) for k in SYMBOLS}
    batches_state = build_batches(metrics)

    ts = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")
    html = render_html(metrics, batches_state, ts)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 已輸出 {args.out}")

    if args.telegram:
        push_telegram(metrics, batches_state, ts)


if __name__ == "__main__":
    main()
