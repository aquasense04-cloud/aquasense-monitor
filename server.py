"""
server.py — Water Leakage Detection (Cloud Version for Render)
==============================================================
This version runs on Render.com (free tier) giving you a permanent
public URL like: https://aquasense-monitor.onrender.com

Changes from local version:
  - Excel saving replaced with SQLite database (persists on Render disk)
  - Reads PORT from environment variable (required by Render)
  - HTTPS is handled automatically by Render's proxy
  - ESP32 must use the Render URL instead of local IP

HOW TO DEPLOY (do this once):
  1. Create account at github.com
  2. Create new repository called "aquasense-monitor"
  3. Upload this file + requirements.txt + render.yaml
  4. Create account at render.com
  5. New Web Service → connect your GitHub repo
  6. Render auto-detects settings from render.yaml
  7. Copy your Render URL (e.g. https://aquasense-monitor.onrender.com)
  8. Update LOCAL_SERVER_IP in your .ino to that URL
"""

from flask import Flask, request, jsonify, Response
from datetime import datetime
from collections import deque
import sqlite3
import os
import threading

# ── CONFIGURATION ────────────────────────────────────────────────────────────

MAX_MEMORY_READINGS = 200
LEAK_THRESHOLD_MPa  = 0.030   # must match LEAK_THRESHOLD_MBAR/1000 in .ino

# SQLite database — Render gives you a persistent disk if configured
# For free tier: data resets on redeploy (use for demo; upgrade for production)
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "readings.db")

# ── APP SETUP ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

readings_lock  = threading.Lock()
live_readings  = deque(maxlen=MAX_MEMORY_READINGS)
latest_reading = None

# ── DATABASE ──────────────────────────────────────────────────────────────────

def init_db():
    """Create the readings table if it doesn't exist."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            sensor_a    REAL    NOT NULL,
            sensor_b    REAL    NOT NULL,
            delta_p     REAL    NOT NULL,
            threshold   REAL    NOT NULL,
            leak        INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Database ready: {DB_FILE}")

def save_to_db(reading: dict):
    """Save one reading row to SQLite. Runs in background thread."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            INSERT INTO readings (timestamp, sensor_a, sensor_b, delta_p, threshold, leak)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            reading["timestamp"],
            reading["sensor_a_mpa"],
            reading["sensor_b_mpa"],
            reading["delta_p_mpa"],
            reading["threshold_mpa"],
            1 if reading["leak_detected"] else 0
        ))
        conn.commit()
        conn.close()
        status = "LEAK" if reading["leak_detected"] else "OK"
        print(f"[DB] Saved → {reading['timestamp']}  ΔP={reading['delta_p_mpa']:.4f} MPa  {status}")
    except Exception as e:
        print(f"[DB] ERROR: {e}")

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/api/data", methods=["POST"])
def receive_data():
    """ESP32 POSTs here every 10 seconds."""
    global latest_reading

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    required = ["sensor_a_mbar", "sensor_b_mbar", "delta_p_mbar", "leak_detected"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields", "required": required}), 400

    reading = {
        "timestamp"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sensor_a_mpa" : data["sensor_a_mbar"] / 1000.0,
        "sensor_b_mpa" : data["sensor_b_mbar"] / 1000.0,
        "delta_p_mpa"  : data["delta_p_mbar"]  / 1000.0,
        "leak_detected": bool(data["leak_detected"]),
        "threshold_mpa": LEAK_THRESHOLD_MPa,
    }

    with readings_lock:
        live_readings.append(reading)
        latest_reading = reading

    # Save to database in background (non-blocking)
    threading.Thread(target=save_to_db, args=(reading,), daemon=True).start()

    return jsonify({"status": "ok", "timestamp": reading["timestamp"]}), 200


@app.route("/api/latest", methods=["GET"])
def get_latest():
    """Returns most recent reading. Dashboard polls this every 3 s."""
    with readings_lock:
        if latest_reading is None:
            return jsonify({"status": "no_data"}), 200
        return jsonify(latest_reading), 200


@app.route("/api/readings", methods=["GET"])
def get_readings():
    """Returns last N readings from memory."""
    limit = min(int(request.args.get("limit", 50)), MAX_MEMORY_READINGS)
    with readings_lock:
        data = list(live_readings)[-limit:]
    return jsonify(data), 200


@app.route("/api/history", methods=["GET"])
def get_history():
    """Returns readings from SQLite database (persisted across restarts)."""
    limit = min(int(request.args.get("limit", 100)), 1000)
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("""
            SELECT timestamp, sensor_a, sensor_b, delta_p, threshold, leak
            FROM readings
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        result = [{
            "timestamp"    : r[0],
            "sensor_a_mpa" : r[1],
            "sensor_b_mpa" : r[2],
            "delta_p_mpa"  : r[3],
            "threshold_mpa": r[4],
            "leak_detected": bool(r[5])
        } for r in rows]
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def get_status():
    """Health check — Render uses this to confirm app is running."""
    with readings_lock:
        count = len(live_readings)
        last  = latest_reading["timestamp"] if latest_reading else "none"
    return jsonify({
        "status"                : "running",
        "readings_in_memory"    : count,
        "last_reading_timestamp": last,
        "leak_threshold_mpa"    : LEAK_THRESHOLD_MPa,
    }), 200


@app.route("/")
def serve_dashboard():
    """Serves the embedded dashboard. No external HTML file needed."""
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ── EMBEDDED DASHBOARD ────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AquaSense Monitor</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=JetBrains+Mono:wght@400;500&family=Inter:wght@300;400;500;600&display=swap');
  :root{--bg-deep:#050d1a;--bg-panel:#0a1628;--bg-card:#0f2040;--border:#1a3a6a;--accent:#00c8ff;--accent2:#0077cc;--safe:#00e5a0;--warn:#ff4d4d;--text-hi:#e8f4ff;--text-lo:#5a7a9f;--glow:0 0 20px rgba(0,200,255,.25);--glow-red:0 0 20px rgba(255,77,77,.35);}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg-deep);color:var(--text-hi);font-family:'Inter',sans-serif;min-height:100vh;}
  body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,200,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,200,255,.03) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0;}
  .z{position:relative;z-index:1;}
  header{border-bottom:1px solid var(--border);padding:14px 32px;display:flex;align-items:center;justify-content:space-between;background:linear-gradient(90deg,rgba(0,10,30,.95),rgba(0,20,50,.85));backdrop-filter:blur(10px);position:sticky;top:0;z-index:100;}
  .brand{display:flex;align-items:center;gap:14px;}
  .brand-icon{width:40px;height:40px;background:linear-gradient(135deg,var(--accent2),var(--accent));border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:var(--glow);}
  .brand-text h1{font-family:'Rajdhani',sans-serif;font-size:18px;font-weight:700;letter-spacing:1px;color:var(--accent);}
  .brand-text p{font-size:11px;color:var(--text-lo);}
  .hdr-r{display:flex;align-items:center;gap:16px;}
  .conn{font-size:11px;font-weight:600;padding:4px 10px;border-radius:20px;background:rgba(0,229,160,.12);color:var(--safe);border:1px solid rgba(0,229,160,.25);transition:all .3s;}
  .conn.off{background:rgba(255,77,77,.12);color:var(--warn);border-color:rgba(255,77,77,.25);}
  .clock{font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text-lo);}
  .live-b{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--safe);text-transform:uppercase;}
  .ldot{width:8px;height:8px;border-radius:50%;background:var(--safe);animation:pulse 1.6s ease-in-out infinite;}
  .ldot.off{background:var(--warn);}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}
  main{padding:28px 32px;max-width:1400px;margin:0 auto;}
  .notice{display:none;text-align:center;padding:32px;background:rgba(255,77,77,.04);border:1px dashed rgba(255,77,77,.35);border-radius:14px;margin-bottom:24px;}
  .notice.show{display:block;}
  .notice h3{color:var(--warn);font-family:'Rajdhani',sans-serif;font-size:20px;margin-bottom:8px;}
  .notice p{color:var(--text-lo);font-size:13px;font-family:'JetBrains Mono',monospace;line-height:2;}
  .alert-bar{display:none;background:linear-gradient(90deg,rgba(255,77,77,.12),rgba(255,77,77,.06));border:1px solid var(--warn);border-left:4px solid var(--warn);border-radius:10px;padding:14px 20px;margin-bottom:24px;animation:ap 2s ease-in-out infinite;align-items:center;gap:14px;}
  .alert-bar.show{display:flex;}
  @keyframes ap{0%,100%{box-shadow:none}50%{box-shadow:var(--glow-red)}}
  .alert-bar h3{font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:700;color:var(--warn);}
  .alert-bar p{font-size:12px;color:rgba(255,77,77,.8);margin-top:2px;}
  .alert-bar button{margin-left:auto;background:none;border:1px solid var(--warn);color:var(--warn);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:11px;}
  .cards{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-bottom:24px;}
  .card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:22px 24px;position:relative;overflow:hidden;transition:border-color .3s,box-shadow .3s;}
  .card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--accent),transparent);}
  .card.leak::before{background:linear-gradient(90deg,transparent,var(--warn),transparent);}
  .card.leak{border-color:var(--warn);box-shadow:var(--glow-red);background:linear-gradient(135deg,#0f2040,#1a0a0a);}
  .clabel{font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-lo);margin-bottom:10px;display:flex;align-items:center;gap:6px;}
  .clabel .cd{width:6px;height:6px;border-radius:50%;background:var(--accent);}
  .card.leak .clabel .cd{background:var(--warn);}
  .cval{font-family:'Rajdhani',sans-serif;font-size:52px;font-weight:700;line-height:1;color:var(--accent);transition:color .3s;}
  .card.leak .cval{color:var(--warn);}
  .cdelta .cval{color:var(--text-hi);}
  .cdelta.leak .cval{color:var(--warn);}
  .cunit{font-size:16px;color:var(--text-lo);margin-left:4px;}
  .cstat{display:inline-flex;align-items:center;gap:6px;margin-top:12px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;text-transform:uppercase;background:rgba(0,229,160,.12);color:var(--safe);border:1px solid rgba(0,229,160,.25);}
  .cstat.lk{background:rgba(255,77,77,.12);color:var(--warn);border-color:rgba(255,77,77,.25);}
  .chart-box{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px;}
  .chart-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;}
  .chart-ttl{font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:600;}
  .legend{display:flex;gap:16px;}
  .li{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-lo);}
  .ld{width:10px;height:10px;border-radius:50%;}
  .tbl-box{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px;}
  .tbl-box h2{font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:600;margin-bottom:16px;}
  table{width:100%;border-collapse:collapse;}
  thead th{text-align:left;padding:10px 14px;font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text-lo);border-bottom:1px solid var(--border);}
  tbody tr{border-bottom:1px solid rgba(26,58,106,.4);}
  tbody tr:hover{background:rgba(0,200,255,.04);}
  tbody td{padding:10px 14px;font-family:'JetBrains Mono',monospace;font-size:12px;}
  td.ts{color:var(--text-lo);font-size:11px;}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;}
  .badge.ok{background:rgba(0,229,160,.12);color:var(--safe);border:1px solid rgba(0,229,160,.2);}
  .badge.lk{background:rgba(255,77,77,.12);color:var(--warn);border:1px solid rgba(255,77,77,.2);}
  .notif-box{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:24px;}
  .notif-box h2{font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:600;margin-bottom:16px;}
  .nlist{display:flex;flex-direction:column;gap:10px;max-height:280px;overflow-y:auto;}
  .ni{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border-radius:8px;background:var(--bg-panel);border:1px solid var(--border);}
  .ni.lk{border-color:rgba(255,77,77,.3);background:rgba(255,77,77,.05);}
  .ni .ico{font-size:16px;}
  .ni .nt{font-size:13px;font-weight:600;}
  .ni .nd{font-size:11px;color:var(--text-lo);margin-top:2px;font-family:'JetBrains Mono',monospace;}
  .ni .ntime{margin-left:auto;font-size:10px;color:var(--text-lo);font-family:'JetBrains Mono',monospace;white-space:nowrap;}
  ::-webkit-scrollbar{width:6px;}::-webkit-scrollbar-track{background:var(--bg-deep);}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}
  @media(max-width:900px){.cards{grid-template-columns:1fr 1fr}main{padding:16px}header{padding:12px 16px}}
  @media(max-width:600px){.cards{grid-template-columns:1fr}.legend{display:none}}
</style>
</head>
<body><div class="z">
<header>
  <div class="brand">
    <div class="brand-icon">&#128167;</div>
    <div class="brand-text">
      <h1>AQUASENSE MONITOR</h1>
      <p>IoT Water Leakage Detection &mdash; George Kent (Malaysia)</p>
    </div>
  </div>
  <div class="hdr-r">
    <div class="conn off" id="conn">&#9679; Connecting&hellip;</div>
    <div class="clock" id="clock">--:--:--</div>
    <div class="live-b"><div class="ldot off" id="ldot"></div>LIVE</div>
  </div>
</header>
<main>
  <div class="notice show" id="notice">
    <h3>&#9889; Waiting for ESP32 Data</h3>
    <p>Make sure ESP32 is powered and connected to WiFi.<br>
    ESP32 should POST to: <strong id="esp32url"></strong></p>
  </div>
  <div class="alert-bar" id="alertBar">
    <span style="font-size:24px">&#9888;&#65039;</span>
    <div><h3>LEAK DETECTED &mdash; Pressure Differential Exceeded Threshold</h3>
    <p id="alertDetail"></p></div>
    <button onclick="document.getElementById('alertBar').classList.remove('show')">DISMISS</button>
  </div>
  <div class="cards">
    <div class="card" id="cA">
      <div class="clabel"><div class="cd"></div>Sensor A &mdash; Upstream (Inlet)</div>
      <div><span class="cval" id="vA">&mdash;</span><span class="cunit">MPa</span></div>
      <div class="cstat" id="sA">Waiting&hellip;</div>
    </div>
    <div class="card" id="cB">
      <div class="clabel"><div class="cd"></div>Sensor B &mdash; Downstream (Outlet)</div>
      <div><span class="cval" id="vB">&mdash;</span><span class="cunit">MPa</span></div>
      <div class="cstat" id="sB">Waiting&hellip;</div>
    </div>
    <div class="card cdelta" id="cD">
      <div class="clabel"><div class="cd"></div>Pressure Differential (&Delta;P = A &minus; B)</div>
      <div><span class="cval" id="vD">&mdash;</span><span class="cunit">MPa</span></div>
      <div class="cstat" id="sD">Waiting&hellip;</div>
    </div>
  </div>
  <div class="chart-box">
    <div class="chart-hdr">
      <div class="chart-ttl">&#128200; Pressure Readings Over Time</div>
      <div class="legend">
        <div class="li"><div class="ld" style="background:#00c8ff"></div>Sensor A</div>
        <div class="li"><div class="ld" style="background:#00e5a0"></div>Sensor B</div>
        <div class="li"><div class="ld" style="background:rgba(255,77,77,.7)"></div>Threshold</div>
      </div>
    </div>
    <canvas id="chart" height="90"></canvas>
  </div>
  <div class="tbl-box">
    <h2>&#128203; Recent Readings Log</h2>
    <table>
      <thead><tr><th>Timestamp</th><th>Sensor A (MPa)</th><th>Sensor B (MPa)</th><th>&Delta;P (MPa)</th><th>Threshold</th><th>Status</th></tr></thead>
      <tbody id="tbl"><tr><td colspan="6" style="text-align:center;color:var(--text-lo);padding:24px;">Waiting for data&hellip;</td></tr></tbody>
    </table>
  </div>
  <div class="notif-box">
    <h2>&#128276; Alert Notifications</h2>
    <div class="nlist" id="nlist">
      <div class="ni"><div class="ico">&#8505;&#65039;</div>
      <div><div class="nt">Dashboard Online</div><div class="nd">Connected to cloud server &mdash; polling every 3 s.</div></div>
      <div class="ntime" id="st"></div></div>
    </div>
  </div>
</main>
</div>
<script>
const POLL_MS=3000,CHART_MAX=20,TABLE_MAX=15;
let notifs=[],leakWas=false,pollN=0,gotData=false;
document.getElementById('esp32url').textContent=window.location.origin+'/api/data';
function tick(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('en-MY',{hour12:false});}
setInterval(tick,1000);tick();
document.getElementById('st').textContent=new Date().toLocaleTimeString('en-MY',{hour12:false});
const ch=new Chart(document.getElementById('chart').getContext('2d'),{
  type:'line',data:{labels:[],datasets:[
    {label:'Sensor A',data:[],borderColor:'#00c8ff',backgroundColor:'rgba(0,200,255,.06)',borderWidth:2,pointRadius:4,pointBackgroundColor:'#00c8ff',tension:.35,fill:true},
    {label:'Sensor B',data:[],borderColor:'#00e5a0',backgroundColor:'rgba(0,229,160,.06)',borderWidth:2,pointRadius:4,pointBackgroundColor:'#00e5a0',tension:.35,fill:true},
    {label:'Threshold',data:[],borderColor:'rgba(255,77,77,.6)',backgroundColor:'transparent',borderWidth:1.5,borderDash:[6,3],pointRadius:0,tension:0},
  ]},
  options:{responsive:true,animation:{duration:400},plugins:{legend:{display:false},tooltip:{backgroundColor:'#0a1628',borderColor:'#1a3a6a',borderWidth:1,titleColor:'#5a7a9f',bodyColor:'#e8f4ff',callbacks:{label:c=>` ${c.dataset.label}: ${c.parsed.y.toFixed(4)} MPa`}}},
  scales:{x:{grid:{color:'rgba(26,58,106,.4)'},ticks:{color:'#5a7a9f',font:{family:'JetBrains Mono',size:10}}},y:{grid:{color:'rgba(26,58,106,.4)'},ticks:{color:'#5a7a9f',font:{family:'JetBrains Mono',size:10},callback:v=>v.toFixed(3)+' MPa'},min:0,max:1.3}}}
});
function applyReading(r){
  const sA=r.sensor_a_mpa,sB=r.sensor_b_mpa,dp=r.delta_p_mpa,isLk=r.leak_detected,th=r.threshold_mpa;
  const lbl=r.timestamp?r.timestamp.slice(11,16):'--:--';
  document.getElementById('vA').textContent=sA.toFixed(4);
  document.getElementById('vB').textContent=sB.toFixed(4);
  document.getElementById('vD').textContent=dp.toFixed(4);
  function card(cId,sId,lk,norm){
    const c=document.getElementById(cId),s=document.getElementById(sId);
    if(lk){c.classList.add('leak');s.className='cstat lk';s.textContent='⚠ Leak Detected';}
    else{c.classList.remove('leak');s.className='cstat';s.textContent='✓ '+norm;}
  }
  card('cA','sA',isLk,'Normal');card('cB','sB',isLk,'Normal');card('cD','sD',isLk,'Within threshold');
  const bar=document.getElementById('alertBar');
  if(isLk){bar.classList.add('show');document.getElementById('alertDetail').textContent=`ΔP = ${dp.toFixed(4)} MPa  |  Threshold: ${th.toFixed(3)} MPa  |  Inspect pipeline.`;}
  if(isLk&&!leakWas)addNotif('lk','⚠ Leak Detected!',`ΔP = ${dp.toFixed(4)} MPa at ${lbl}`);
  else if(!isLk&&leakWas){addNotif('ok','✓ Leak Resolved',`Back to normal at ${lbl}`);bar.classList.remove('show');}
  leakWas=isLk;
  if(ch.data.labels.length>=CHART_MAX){ch.data.labels.shift();ch.data.datasets.forEach(d=>d.data.shift());}
  ch.data.labels.push(lbl);ch.data.datasets[0].data.push(sA);ch.data.datasets[1].data.push(sB);ch.data.datasets[2].data.push(th);ch.update();
}
function renderTable(rows){
  const tb=document.getElementById('tbl'),sl=[...rows].reverse().slice(0,TABLE_MAX);
  if(!sl.length){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--text-lo);padding:24px;">No readings yet.</td></tr>';return;}
  tb.innerHTML=sl.map(r=>`<tr><td class="ts">${r.timestamp}</td><td>${r.sensor_a_mpa.toFixed(4)}</td><td>${r.sensor_b_mpa.toFixed(4)}</td><td style="color:${r.leak_detected?'var(--warn)':'var(--text-hi)'}">${r.delta_p_mpa.toFixed(4)}</td><td style="color:var(--text-lo)">${r.threshold_mpa.toFixed(3)}</td><td><span class="badge ${r.leak_detected?'lk':'ok'}">${r.leak_detected?'⚠ LEAK':'✓ Normal'}</span></td></tr>`).join('');
}
function addNotif(type,title,detail){
  const t=new Date().toLocaleTimeString('en-MY',{hour12:false});
  notifs.unshift({type,title,detail,t});if(notifs.length>20)notifs.pop();
  document.getElementById('nlist').innerHTML=notifs.map(n=>`<div class="ni ${n.type==='lk'?'lk':''}"><div class="ico">${n.type==='lk'?'🚨':n.type==='ok'?'✅':'ℹ️'}</div><div><div class="nt" style="color:${n.type==='lk'?'var(--warn)':'var(--text-hi)'}">${n.title}</div><div class="nd">${n.detail}</div></div><div class="ntime">${n.t}</div></div>`).join('');
}
function setOnline(on){
  const c=document.getElementById('conn'),d=document.getElementById('ldot'),no=document.getElementById('notice');
  if(on){c.className='conn';c.textContent='● ESP32 Connected';d.classList.remove('off');no.classList.remove('show');
    if(!gotData){gotData=true;addNotif('info','ESP32 Connected','Live data streaming from cloud.');}}
  else{c.className='conn off';c.textContent='● No Data';d.classList.add('off');no.classList.add('show');}
}
async function poll(){
  try{
    const res=await fetch('/api/latest');
    if(!res.ok)throw new Error('HTTP '+res.status);
    const r=await res.json();
    if(r.status==='no_data'){setOnline(false);}else{setOnline(true);applyReading(r);}
    pollN++;
    if(pollN%5===0){const rr=await fetch('/api/readings?limit=15');if(rr.ok)renderTable(await rr.json());}
  }catch(e){setOnline(false);}
  setTimeout(poll,POLL_MS);
}
setOnline(false);poll();
</script>
</body></html>"""

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    # Render sets the PORT environment variable automatically
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("  AquaSense Monitor — Cloud Server")
    print("=" * 60)
    print(f"  Running on port : {port}")
    print(f"  Leak threshold  : {LEAK_THRESHOLD_MPa} MPa")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
