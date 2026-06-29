#!/usr/bin/env python3
"""Local live dashboard for the paper-trading bot.

Serves an auto-refreshing page at http://127.0.0.1:8765 showing P&L, the
exact markets it is holding, the resting orders it is waiting on, and every
completed trade. Localhost only. Built-ins only. Reads log files; changes
nothing.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PNL_PATH = os.path.join("logs", "paper_pnl.csv")
TRADES_PATH = os.path.join("logs", "paper_trades.csv")
STATE_PATH = os.path.join("logs", "paper_state.json")
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8765"))
TOKEN = os.environ.get("DASH_TOKEN", "")   # if set, /data requires ?token=...


def _num(x, cast=float, default=0.0):
    try:
        return cast(x)
    except (TypeError, ValueError):
        return default


def read_rows(path):
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path) as f:
            for r in csv.reader(f):
                if not r or r[0] == "timestamp":
                    continue
                rows.append(r)
    except Exception:
        pass
    return rows


def build_data():
    pnl = read_rows(PNL_PATH)
    trades = read_rows(TRADES_PATH)
    out = {"running": bool(pnl), "trades": [], "series": [], "score": {},
           "n_trades": len(trades), "positions": [], "resting": [], "updated": ""}

    if pnl:
        last = pnl[-1]
        out["score"] = {
            "cycle": _num(last[1], int, 0), "candidates": _num(last[2], int, 0),
            "open": _num(last[4], int, 0), "round_trips": _num(last[5], int, 0),
            "wins": _num(last[6], int, 0), "losses": _num(last[7], int, 0),
            "realized": _num(last[8]), "unrealized": _num(last[9]),
            "total": _num(last[10]), "fees": _num(last[11]),
        }
        out["series"] = [_num(r[10]) for r in pnl][-200:]

    for r in trades[-200:]:
        out["trades"].append({
            "time": r[0][11:19] if len(r[0]) >= 19 else r[0],
            "ticker": r[1], "action": r[2], "type": r[3],
            "count": r[4], "price": r[5], "fee": r[6],
            "pnl": (r[8] if len(r) > 8 else ""),
            "name": (r[9] if len(r) > 9 else r[1]),
        })
    out["trades"].reverse()

    if os.path.exists(STATE_PATH):
        try:
            st = json.load(open(STATE_PATH))
            out["positions"] = st.get("positions", [])
            out["resting"] = st.get("resting", [])
            out["updated"] = st.get("updated", "")
            out["start"] = st.get("start", 100.0)
            out["equity"] = st.get("equity")
            out["cash"] = st.get("cash")
            # state.json is the single source of truth for live numbers, so the
            # cards always match the holdings panel (no cross-file disagreement)
            sc = out.get("score") or {}
            sc["open"] = len(out["positions"])
            for k in ("realized", "unrealized", "total", "wins", "losses",
                      "round_trips", "fees"):
                if k in st:
                    sc[k] = st[k]
            out["score"] = sc
            out["running"] = True
        except Exception:
            pass
    wpath = os.path.join("logs", "weather_state.json")
    if os.path.exists(wpath):
        try:
            out["weather"] = json.load(open(wpath))
        except Exception:
            out["weather"] = None
    return out


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Kalshi Bot - Live</title>
<style>
 body{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:960px;margin:0 auto;padding:20px}
 h1{font-size:18px;font-weight:600;margin:0 0 2px}
 h3{margin:22px 0 4px;font-size:14px}
 .sub{color:#8b949e;font-size:12px;margin-bottom:16px}
 .big{font-size:46px;font-weight:700;margin:6px 0} .up{color:#3fb950}.down{color:#f85149}.flat{color:#8b949e}
 .cards{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}
 .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;min-width:120px}
 .card .k{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
 .card .v{font-size:20px;font-weight:600;margin-top:3px}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
 th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d} th{color:#8b949e;font-weight:500}
 .buy{color:#58a6ff}.sell{color:#d2a8ff} .pos{color:#3fb950}.neg{color:#f85149}
 .pill{font-size:10px;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:1px 6px}
 svg{background:#161b22;border:1px solid #30363d;border-radius:10px}
 .muted{color:#8b949e;font-size:12px;margin-top:18px}
 .empty{color:#8b949e;font-size:12px;padding:8px 2px}
 .tk{color:#6e7681;font-size:10px;margin-top:2px}
 .bd{display:flex;align-items:stretch;gap:10px;margin:12px 0 4px;flex-wrap:wrap}
 .bd .box{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 18px;flex:1;min-width:150px}
 .bd .box .k{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
 .bd .box .v{font-size:26px;font-weight:700;margin-top:4px}
 .bd .op{display:flex;align-items:center;justify-content:center;font-size:26px;color:#6e7681;min-width:18px}
 .bd .combo{border-color:#3fb95066;background:#11261a}
</style></head><body><div class=wrap>
 <h1>Kalshi Paper Bot <span class=pill>LIVE - simulation, no money</span></h1>
 <div class=sub id=sub>connecting...</div>
 <div class="big flat" id=total>$0.00</div>
 <div class=sub id=balances>Starting balance $100.00</div>
 <svg id=spark width=920 height=90 viewBox="0 0 920 90"></svg>
 <div class=cards id=cards></div>

 <h3>Gains breakdown</h3>
 <div class=bd id=bd></div>

 <div id=wxwrap>
 <h3>Weather edge - separate $100 experiment <span class=pill id=wxsum></span></h3>
 <table><thead><tr><th>market</th><th>side</th><th>entry</th><th>contracts</th><th>our prob</th></tr></thead>
 <tbody id=weather></tbody></table>
 </div>

 <h3>Markets it's holding now <span class=pill id=poscount></span></h3>
 <table><thead><tr><th>market</th><th>contracts</th><th>avg cost</th><th>now (bid)</th><th>unrealized</th></tr></thead>
 <tbody id=positions></tbody></table>

 <h3>Resting orders - waiting to fill <span class=pill id=restcount></span></h3>
 <table><thead><tr><th>market</th><th>side</th><th>price</th><th>contracts</th></tr></thead>
 <tbody id=resting></tbody></table>

 <h3>Trade history - all past trades <span class=pill id=tcount></span></h3>
 <table><thead><tr><th>time</th><th>action</th><th>type</th><th>contracts</th><th>fee</th><th>P&L</th><th>market</th></tr></thead>
 <tbody id=trades></tbody></table>

 <div class=muted>Auto-refreshes every 5s. Reads the log files only. Nothing is ever placed.</div>
</div>
<script>
function money(x){x=Number(x);return (x>=0?'+$':'-$')+Math.abs(x).toFixed(2);}
function cls(x){x=Number(x);return x>0?'up':(x<0?'down':'flat');}
function spark(arr){
 const w=920,h=90,p=8;if(!arr.length)return '';
 const mn=Math.min(0,...arr),mx=Math.max(0,...arr),rng=(mx-mn)||1;
 const X=i=>p+i*(w-2*p)/Math.max(1,arr.length-1), Y=v=>h-p-(v-mn)/rng*(h-2*p);
 const d=arr.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
 const z=Y(0).toFixed(1), col=arr[arr.length-1]>=0?'#3fb950':'#f85149';
 return '<line x1=0 y1='+z+' x2='+w+' y2='+z+' stroke="#30363d" stroke-dasharray="3 3"/>'+
        '<path d="'+d+'" fill=none stroke="'+col+'" stroke-width=2/>';
}
async function tick(){
 try{
  const tk=new URLSearchParams(location.search).get('token')||'';const d=await (await fetch('/data?token='+encodeURIComponent(tk),{cache:'no-store'})).json();
  if(!d.running){document.getElementById('sub').textContent='No data yet - start 9_paper_live.bat and let it run.';return;}
  const s=d.score;
  const t=document.getElementById('total');t.textContent=money(s.total);t.className='big '+cls(s.total);
  if(d.start!=null){const eq=(d.equity!=null?d.equity:d.start);document.getElementById('balances').innerHTML='Starting balance <b>$'+Number(d.start).toFixed(2)+'</b> &nbsp;&rarr;&nbsp; Current <b>$'+Number(eq).toFixed(2)+'</b>';}
  document.getElementById('sub').textContent='cycle '+s.cycle+' - watching '+s.candidates+' markets'+(d.updated?' - state @ '+d.updated.slice(11,19):'');
  const wr=s.round_trips?Math.round(100*s.wins/s.round_trips):0;
  document.getElementById('cards').innerHTML=[
    ['Open positions',s.open],['Completed trades',s.round_trips],
    ['Win rate',s.wins+'W / '+s.losses+'L ('+wr+'%)'],['Fees paid','$'+s.fees.toFixed(2)]
  ].map(c=>'<div class=card><div class=k>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
  document.getElementById('bd').innerHTML=
    '<div class=box><div class=k>Realized (banked)</div><div class="v '+cls(s.realized)+'">'+money(s.realized)+'</div></div>'+
    '<div class=op>+</div>'+
    '<div class=box><div class=k>Unrealized (open)</div><div class="v '+cls(s.unrealized)+'">'+money(s.unrealized)+'</div></div>'+
    '<div class=op>=</div>'+
    '<div class="box combo"><div class=k>Combined total</div><div class="v '+cls(s.total)+'">'+money(s.total)+'</div></div>';
  document.getElementById('spark').innerHTML=spark(d.series);

  if(d.weather && d.weather.summary){
    const w=d.weather.summary;
    document.getElementById('wxwrap').style.display='';
    document.getElementById('wxsum').textContent='P&L $'+Number(w.realized).toFixed(2)+'  -  '+w.wins+'W/'+w.losses+'L ('+w.win_rate+'%)  -  '+w.open_bets+' open  -  '+w.placed+' placed';
    document.getElementById('weather').innerHTML=(d.weather.open||[]).map(b=>
      '<tr><td>'+b.city+' '+b.strike+'\u00b0'+b.hl+'</td><td class='+(b.side==='yes'?'buy':'sell')+'>'+b.side.toUpperCase()+'</td><td>'+b.entry+'c</td><td>'+b.count+'</td><td>'+Math.round(b.pside*100)+'%</td></tr>'
    ).join('')||'<tr><td colspan=5 class=empty>No open weather bets right now - waiting for an edge.</td></tr>';
  } else { document.getElementById('wxwrap').style.display='none'; }

  // holdings
  document.getElementById('poscount').textContent=d.positions.length+' markets';
  document.getElementById('positions').innerHTML=d.positions.map(p=>
    '<tr><td>'+(p.name||p.ticker)+'<div class=tk>'+p.ticker+'</div></td><td>'+p.count+'</td><td>'+p.avg+'c</td><td>'+p.bid+'c</td>'+
    '<td class='+(Number(p.unreal)>=0?'pos':'neg')+'>'+money(p.unreal)+'</td></tr>'
  ).join('')||'<tr><td colspan=5 class=empty>Not holding anything right now.</td></tr>';

  // resting orders
  document.getElementById('restcount').textContent=d.resting.length+' orders';
  document.getElementById('resting').innerHTML=d.resting.map(o=>{
    const a=o.action==='buy'?'buy':'sell';
    return '<tr><td>'+(o.name||o.ticker)+'<div class=tk>'+o.ticker+'</div></td><td class='+a+'>'+o.action.toUpperCase()+'</td><td>'+o.price+'c</td><td>'+o.count+'</td></tr>';
  }).join('')||'<tr><td colspan=4 class=empty>No resting orders yet.</td></tr>';

  // trades
  document.getElementById('tcount').textContent=d.n_trades+' fills';
  document.getElementById('trades').innerHTML=d.trades.map(r=>{
    const a=r.action==='BUY'?'buy':'sell';
    const pnl=r.pnl===''?'':'<span class='+(Number(r.pnl)>=0?'pos':'neg')+'>'+money(r.pnl)+'</span>';
    return '<tr><td>'+r.time+'</td><td class='+a+'>'+r.action+'</td><td>'+r.type+'</td><td>'+r.count+' @ '+r.price+'c</td><td>'+r.fee+'c</td><td>'+pnl+'</td><td>'+(r.name||r.ticker)+'</td></tr>';
  }).join('')||'<tr><td colspan=7 class=empty>No fills yet - orders are resting, waiting to be hit.</td></tr>';
 }catch(e){document.getElementById('sub').textContent='waiting for bot...';}
}
tick();setInterval(tick,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
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
