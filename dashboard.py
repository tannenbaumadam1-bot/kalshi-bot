#!/usr/bin/env python3
"""Live dashboard for the Kalshi WEATHER paper bot.

Serves an auto-refreshing page (default http://127.0.0.1:8765) showing the
weather strategy's P&L, open bets (with live market price + unrealized P/L),
and settled history. Net P&L = banked (settled) P&L; open bets also show a
mark-to-market so you can see how they're doing before settlement.

No money, no API key, nothing sensitive. Reads logs/weather_state.json.

Public mode (for a cloud server):
    DASH_HOST=0.0.0.0 DASH_PORT=8765 DASH_TOKEN=somesecret python3 dashboard.py
"""

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import requests
except Exception:       # dashboard still works without live prices
    requests = None

WEATHER_PATH = os.path.join("logs", "weather_state.json")
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8765"))
TOKEN = os.environ.get("DASH_TOKEN", "")   # if set, /data requires ?token=...
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# live-price cache for open bets: one batched Kalshi call, max once / 60s
_PRICES = {"ts": 0.0, "by_ticker": {}}
_PRICES_LOCK = threading.Lock()


def _cents(mk, key):
    v = mk.get(key)
    if isinstance(v, (int, float)) and v > 0:
        return int(round(float(v)))
    v = mk.get(key + "_dollars")
    try:
        return int(round(float(v) * 100)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def fetch_prices(tickers):
    """Current yes_bid/yes_ask (cents) per ticker; cached 60s. Never raises."""
    if not tickers or requests is None:
        return {}
    with _PRICES_LOCK:
        fresh = (time.time() - _PRICES["ts"] < 60
                 and all(t in _PRICES["by_ticker"] for t in tickers))
        if fresh:
            return _PRICES["by_ticker"]
        out = dict(_PRICES["by_ticker"])   # keep stale marks if refresh fails
        try:
            for i in range(0, len(tickers), 40):
                batch = tickers[i:i + 40]
                d = requests.get(KALSHI + "/markets",
                                 params={"tickers": ",".join(batch), "limit": len(batch)},
                                 timeout=10).json()
                for mk in d.get("markets", []) or []:
                    out[mk.get("ticker", "")] = {
                        "yes_bid": _cents(mk, "yes_bid"),
                        "yes_ask": _cents(mk, "yes_ask")}
            _PRICES["ts"] = time.time()
            _PRICES["by_ticker"] = out
        except Exception:
            pass
        return out


def build_data():
    out = {"running": False, "updated": "", "summary": {}, "open": [], "settled": []}
    if os.path.exists(WEATHER_PATH):
        try:
            w = json.load(open(WEATHER_PATH))
            out["running"] = True
            out["updated"] = w.get("updated", "")
            out["summary"] = w.get("summary", {}) or {}
            out["open"] = w.get("open", []) or []
            out["settled"] = w.get("settled", []) or []
        except Exception:
            pass
    # enrich open bets with current market price + unrealized P/L
    tickers = [b.get("ticker") for b in out["open"] if b.get("ticker")]
    prices = fetch_prices(tickers)
    unreal = 0.0
    priced = 0
    for b in out["open"]:
        px = prices.get(b.get("ticker") or "")
        if not px or not (px["yes_bid"] or px["yes_ask"]):
            b["now"] = None
            b["upnl"] = None
            continue
        # mark = what our side could sell for now
        mark = px["yes_bid"] if b.get("side") == "yes" else (100 - px["yes_ask"])
        mark = max(0, min(100, mark))
        b["now"] = mark
        b["value"] = round(mark * b.get("count", 0) / 100.0, 2)
        b["upnl"] = round((mark - b.get("entry", 0)) * b.get("count", 0) / 100.0, 2)
        unreal += b["upnl"]
        priced += 1
    if out["summary"] and priced:
        out["summary"]["unrealized"] = round(unreal, 2)
    # cumulative banked P&L curve, oldest -> newest (settled is newest-first)
    curve, run = [], 0.0
    for b in reversed(out["settled"]):
        run += float(b.get("pnl", 0) or 0)
        curve.append(round(run, 2))
    out["curve"] = curve
    # live trader state (only exists once real-money weather trading starts)
    live_path = os.path.join("logs", "weather_live_state.json")
    if os.path.exists(live_path):
        try:
            lv = json.load(open(live_path))
            out["live"] = {"updated": lv.get("updated", ""),
                           "summary": lv.get("summary", {}) or {},
                           "balance_c": lv.get("balance_c")}
        except Exception:
            pass
    return out


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Kalshi Weather Bot</title>
<style>
:root{--bg:#0b1220;--card:#131c2e;--card2:#0f1827;--ink:#e8eefb;--mut:#8aa0c2;
--line:#22304a;--grn:#36d399;--red:#f87272;--accent:#6aa3ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:22px 16px 60px}
h1{font-size:21px;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.pnl{font-size:46px;font-weight:800;letter-spacing:-1px;line-height:1.1}
.pos{color:var(--grn)}.neg{color:var(--red)}
.equity{color:var(--mut);font-size:14px;margin:2px 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:700;margin-top:4px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
margin:26px 0 8px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:12px;overflow:hidden;font-size:13.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;font-weight:700}
.yes{background:rgba(54,211,153,.15);color:var(--grn)}
.no{background:rgba(248,114,114,.15);color:var(--red)}
.won{color:var(--grn);font-weight:700}.lost{color:var(--red);font-weight:700}
.empty{color:var(--mut);text-align:center;padding:18px}
.mkt{font-weight:600}.tk{color:var(--mut);font-size:11px}
.banner{background:rgba(106,163,255,.1);border:1px solid var(--line);color:var(--mut);
border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:18px}
svg{display:block;width:100%;height:64px;margin-top:6px}
.foot{color:var(--mut);font-size:12px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class=wrap>
<h1>Kalshi Weather Bot</h1>
<div class=sub id=sub>loading...</div>
<div class=banner>Live simulation - no money, no API key. Strategy: out-forecast daily
temperature markets, bet only disciplined edges (Kelly-sized, no tails, no data-error gaps,
model probability shrunk toward the market price).</div>
<div class="pnl" id=pnl>--</div>
<div class=equity id=equity></div>
<svg id=spark viewBox="0 0 600 64" preserveAspectRatio=none></svg>
<div class=grid id=cards></div>
<h2>Open bets (held to settlement)</h2>
<table><thead><tr><th>Market</th><th>Side</th><th class=num>Our prob</th>
<th class=num>Entry</th><th class=num>Now</th><th class=num>Contracts</th>
<th class=num>At stake</th><th class=num>Value now</th><th class=num>Unrl P&L</th></tr></thead>
<tbody id=open></tbody></table>
<h2>Settled bets (history)</h2>
<table><thead><tr><th>Market</th><th>Side</th><th class=num>Our prob</th>
<th class=num>Price</th><th class=num>Contracts</th><th>Result</th><th class=num>P&L</th></tr></thead>
<tbody id=settled></tbody></table>
<div class=foot id=foot></div>
</div>
<script>
const $=id=>document.getElementById(id);
function money(x){const n=Number(x||0);const s=(n>=0?'+':'-')+'$'+Math.abs(n).toFixed(2);return s;}
function cls(x){return Number(x||0)>=0?'pos':'neg';}
function mkt(b){const name=(b.city||'')+' '+(b.strike)+'° '+((b.hl==='lo')?'low':'high');
  return '<td><span class=mkt>'+name+'</span></td>';}
function side(s){s=(s||'').toLowerCase();return '<td><span class="tag '+(s==='yes'?'yes':'no')+'">'+s.toUpperCase()+'</span></td>';}
function prob(p){return '<td class=num>'+Math.round((Number(p)||0)*100)+'%</td>';}
function spark(curve){
  const el=$('spark');
  if(!curve||curve.length<2){el.innerHTML='';return;}
  const W=600,H=64,pad=4;const mn=Math.min(0,...curve),mx=Math.max(0,...curve);
  const rng=(mx-mn)||1;
  const pts=curve.map((v,i)=>{
    const x=pad+(W-2*pad)*i/(curve.length-1);
    const y=H-pad-(H-2*pad)*(v-mn)/rng;return x.toFixed(1)+','+y.toFixed(1);}).join(' ');
  const last=curve[curve.length-1];const col=last>=0?'#36d399':'#f87272';
  const zeroY=(H-pad-(H-2*pad)*(0-mn)/rng).toFixed(1);
  el.innerHTML='<line x1=0 y1="'+zeroY+'" x2="'+W+'" y2="'+zeroY+'" stroke="#22304a" stroke-width=1/>'
    +'<polyline points="'+pts+'" fill=none stroke="'+col+'" stroke-width=2/>';
}
async function load(){
  const tk=new URLSearchParams(location.search).get('token')||'';
  let d;try{d=await(await fetch('/data?token='+encodeURIComponent(tk),{cache:'no-store'})).json();}
  catch(e){$('sub').textContent='cannot reach bot';return;}
  if(d.auth===false){$('sub').textContent='bad token';return;}
  const s=d.summary||{};
  if(!d.running){$('sub').textContent='waiting for the weather bot to write its first state...';return;}
  $('sub').textContent='updated '+(d.updated?d.updated.replace('T',' ').slice(0,19):'-');
  if(d.live&&d.live.summary){const L=d.live.summary;
    $('sub').textContent+='   |   LIVE: '+money(L.net||0)+' net, '
      +(L.wins||0)+'W/'+(L.losses||0)+'L, '+(L.open||0)+' open'
      +(d.live.balance_c!=null?', bal $'+(d.live.balance_c/100).toFixed(2):'');}
  const total=Number(s.total||0);
  const unrl=(s.unrealized==null)?null:Number(s.unrealized);
  $('pnl').innerHTML='<span class="'+cls(total)+'">'+money(total)+'</span>';
  const equity=(Number(s.start||0)+total).toFixed(2);
  $('equity').textContent='Started $'+Number(s.start||0).toFixed(2)+'  →  banked $'+equity
    +'   ('+(s.open_bets||0)+' open bet'+((s.open_bets===1)?'':'s')+', $'+Number(s.open_exposure||0).toFixed(2)+' at stake)'
    +(unrl==null?'':'   |   marked equity $'+(Number(s.start||0)+total+unrl).toFixed(2));
  spark(d.curve);
  const wr=(s.settled||0)>0?(s.win_rate+'%'):'-';
  $('cards').innerHTML=[
    ['Net P&L (banked)','<span class="'+cls(total)+'">'+money(total)+'</span>'],
    ['Unrealized P&L (open)',unrl==null?'-':'<span class="'+cls(unrl)+'">'+money(unrl)+'</span>'],
    ['Settled bets',(s.settled||0)+'  ('+(s.wins||0)+'W / '+(s.losses||0)+'L)'],
    ['Win rate',wr],
    ['Open bets',(s.open_bets||0)],
    ['Total placed',(s.placed||0)],
    ['Fees paid','$'+Number(s.fees||0).toFixed(2)],
  ].map(c=>'<div class=card><div class=k>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
  $('open').innerHTML=(d.open||[]).map(b=>{
    const hasNow=(b.now!=null);
    return '<tr>'+mkt(b)+side(b.side)+prob(b.pside)
    +'<td class=num>'+b.entry+'¢</td>'
    +'<td class=num>'+(hasNow?b.now+'¢':'-')+'</td>'
    +'<td class=num>'+b.count+'</td>'
    +'<td class=num>$'+((b.entry*b.count)/100).toFixed(2)+'</td>'
    +'<td class=num>'+(hasNow?'$'+Number(b.value||0).toFixed(2):'-')+'</td>'
    +'<td class=num>'+(hasNow?'<span class="'+cls(b.upnl)+'">'+money(b.upnl)+'</span>':'-')+'</td></tr>';
  }).join('')||'<tr><td colspan=9 class=empty>No open bets - waiting for a disciplined edge.</td></tr>';
  $('settled').innerHTML=(d.settled||[]).map(b=>{
    const won=Number(b.outcome)===1;
    return '<tr>'+mkt(b)+side(b.side)+prob(b.pside)
    +'<td class=num>'+b.entry+'¢</td><td class=num>'+b.count+'</td>'
    +'<td><span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span></td>'
    +'<td class=num><span class="'+cls(b.pnl)+'">'+money(b.pnl)+'</span></td></tr>';
  }).join('')||'<tr><td colspan=7 class=empty>No settled bets yet - they resolve at end of day.</td></tr>';
  $('foot').textContent='Paper trading only. Net P&L is banked (settled) profit; open bets also '
    +'show a live mark (what your side could sell for now). Marks refresh about once a minute. '
    +'Auto-refreshes every 20s.';
}
load();setInterval(load,20000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/public"):
            # Read-only JSON, no token. Paper-trading stats only (no keys,
            # no account data) - lets tooling check on the bot remotely.
            body = json.dumps(build_data()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/data"):
            if TOKEN:
                from urllib.parse import urlparse, parse_qs
                given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
                if given != TOKEN:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"auth":false}')
                    return
            body = json.dumps(build_data()).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        srv = HTTPServer((HOST, PORT), H)
    except OSError as e:
        print(f"Could not start dashboard on {url}: {e}")
        print("If it says 'address already in use', a dashboard is already")
        print("running - just open the address in your browser.")
        input("Press Enter to close...")
        return 1
    shown = url + (f"/?token={TOKEN}" if TOKEN else "")
    print(f"Dashboard running at {shown}")
    if HOST == "127.0.0.1":
        print("Opening your browser... (keep this window open; Ctrl+C to stop)")
        threading.Timer(1.0, lambda: webbrowser.open(shown)).start()
    else:
        print("(Public mode - open the address above from any device.)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
