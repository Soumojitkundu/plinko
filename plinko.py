# plinko_all_in_one.py
"""
Single-file Plinko Mini-App stack:
- Flask server that serves the Web App (index.html) at /plinko
  and REST endpoints /api/submit_score and /api/leaderboard (SQLite).
- Telegram bot that exposes /plinko command (button -> web_app) and
  handles incoming WEB_APP_DATA messages from Telegram Web App (tg.sendData).
- Simple balance fallback if no external balance.py exists.
Usage:
  pip install python-telegram-bot flask requests
  export BOT_TOKEN="123:ABC..."
  export HOST_URL="https://your-public-host"   # public HTTPS base URL where this server is reachable
  export LEADERBOARD_SECRET="super-secret"
  python plinko_all_in_one.py
"""
import os
import json
import sqlite3
import threading
import time
from datetime import datetime

# Flask server (leaderboard + static webapp)
from flask import Flask, request, jsonify, g, Response
from flask import make_response

# Telegram bot
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# HTTP client
import requests

# ---------- Configuration (env) ----------
BOT_TOKEN = os.getenv("7361382641:AAFq5xboYLEPtO86v1blW6BhygSEXuZIga0")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var required")

# PUBLIC URL where this Flask server will be reachable (must be HTTPS for Telegram WebApp)
HOST_URL = os.getenv("HOST_URL", "https://example.com")  # replace with your public URL (ngrok / domain)
PLINKO_PATH = "/plinko"  # path to webapp
PLINKO_WEBAPP_URL = os.getenv("PLINKO_WEBAPP_URL", f"{HOST_URL}{PLINKO_PATH}")

LEADERBOARD_SECRET = os.getenv("LEADERBOARD_SECRET", "replace-me")
PORT = int(os.getenv("PORT", "5001"))
DB_PATH = os.getenv("PLINKO_DB", "plinko_leaderboard.db")

# Basic payout / multiplier logic params (adjust to taste)
MIN_MULT = 0.5
MAX_MULT = 3.0

# ---------- Optional: try import your real balance functions ----------
try:
    from balance import get_balance, update_balance, add_wager
    BALANCE_BACKEND = "external"
except Exception:
    # Fallback in-memory balances for demo/testing
    BALANCE_BACKEND = "memory"
    _balances = {}
    def get_balance(user_id:int) -> float:
        return float(_balances.get(user_id, 100.0))  # start with 100 credits by default
    def update_balance(user_id:int, delta: float):
        _balances[user_id] = float(_balances.get(user_id, 100.0) + delta)
        return _balances[user_id]
    def add_wager(user_id:int, wager:float):
        # store wagers count simple
        _balances[f"wager_{user_id}"] = _balances.get(f"wager_{user_id}", 0) + wager

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("plinko_all")

# ---------- Flask app ----------
app = Flask(__name__)

def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
        g._db = db
    return db

def init_db():
    db = get_db()
    db.execute("""
    CREATE TABLE IF NOT EXISTS scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        score INTEGER,
        wager REAL,
        payout REAL,
        created_at TEXT
    )
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS game_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        mode TEXT,
        played_at TEXT,
        bet REAL,
        won_amount REAL,
        is_win INTEGER
    )""")
    db.commit()

@app.before_first_request
def startup():
    init_db()

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db:
        db.close()

# Serve the embedded index.html (Web App)
INDEX_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Plinko — Telegram Web App</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { margin:0; font-family: system-ui, -apple-system, Roboto, "Segoe UI", sans-serif; display:flex; flex-direction:column; align-items:center; background:#0f172a; color:#fff; }
    canvas { background: linear-gradient(#0b1220,#081122); border-radius:8px; margin-top:10px; box-shadow: 0 8px 24px rgba(0,0,0,0.6);}
    #ui { margin:12px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center; }
    button { padding:10px 16px; font-size:16px; border-radius:8px; border:none; cursor:pointer; }
    #score { font-weight:700; min-width:120px; text-align:center;}
    #leaderboard { width:360px; margin-top:12px; background:rgba(255,255,255,0.03); padding:8px; border-radius:6px; max-height:220px; overflow:auto; }
    .lb-row { display:flex; justify-content:space-between; padding:6px 4px; border-bottom:1px solid rgba(255,255,255,0.03); }
    input[type=number] { padding:6px 8px; border-radius:6px; border:none; }
  </style>
</head>
<body>
  <h2 style="margin-top:10px">🎯 Plinko — Web App</h2>
  <div id="ui">
    <button id="dropBtn">Drop Ball</button>
    <button id="resetBtn">Reset</button>
    <label>Wager: <input id="wagerInput" type="number" step="0.5" min="0" value="0" style="width:80px"></label>
    <div id="score">Score: 0</div>
    <button id="cashoutBtn">Send Result</button>
    <button id="refreshLb">Refresh Leaderboard</button>
  </div>

  <canvas id="c" width="360" height="600"></canvas>

  <div id="leaderboard">
    <strong>Leaderboard</strong>
    <div id="lbRows"></div>
  </div>

<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const tg = window.Telegram?.WebApp;
if (tg) tg.expand();
const HOST = "%(host_url)s";
const LB_URL = HOST + "/api/leaderboard?n=10";
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const W = canvas.width, H = canvas.height;
let pegs = [], bins = [], ball = null, score = 0, animId = null;
const rows = 8, pegRadius = 4, spacingX = 40, spacingY = 45, gravity = 0.45, friction = 0.995;
function build() {
  pegs = [];
  for (let row=0; row<rows; row++) {
    const y = 100 + row*spacingY;
    const cols = Math.floor(W/spacingX);
    for (let col=0; col<cols; col++) {
      const offset = (row%2===0) ? spacingX/2 : 0;
      const x = offset + col*spacingX + spacingX/2;
      pegs.push({x,y});
    }
  }
  bins = []; const binCount = 8; const binW = W / binCount;
  const mults = [0,0.5,1,1.5,2,1.5,1,0];
  for (let i=0;i<binCount;i++) bins.push({x:i*binW,w:binW,mul:mults[i]});
}
function dropBall() { if (ball) return; ball = {x: W/2 + (Math.random()*60-30), y: 20, vx: (Math.random()*2-1), vy:0, r: 7}; }
function step() {
  if (!ball) return;
  ball.vy += gravity;
  ball.vx *= friction;
  ball.x += ball.vx; ball.y += ball.vy;
  for (const p of pegs) {
    const dx = ball.x - p.x, dy = ball.y - p.y;
    const d = Math.sqrt(dx*dx + dy*dy);
    if (d < ball.r + pegRadius && d>0.1) {
      const nx = dx/d, ny = dy/d, overlap = (ball.r + pegRadius) - d;
      ball.x += nx * overlap; ball.y += ny * overlap;
      const dot = ball.vx*nx + ball.vy*ny;
      ball.vx -= 1.4*dot*nx; ball.vy -= 1.4*dot*ny;
    }
  }
  if (ball.x < ball.r) { ball.x = ball.r; ball.vx = -ball.vx*0.6; }
  if (ball.x > W - ball.r) { ball.x = W - ball.r; ball.vx = -ball.vx*0.6; }
  if (ball.y > H - 20) {
    const bidx = Math.floor(ball.x / (W / bins.length));
    const bin = bins[Math.max(0, Math.min(bidx, bins.length-1))];
    const reward = Math.round(100 * bin.mul);
    score += reward; updateScoreDisplay(); ball = null;
  }
}
function draw() {
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle = "#f1f5f9";
  for (const p of pegs) { ctx.beginPath(); ctx.arc(p.x,p.y,pegRadius,0,Math.PI*2); ctx.fill(); }
  ctx.font = `14px sans-serif`;
  for (let i=0;i<bins.length;i++) {
    const b=bins[i]; ctx.strokeStyle="#94a3b8"; ctx.strokeRect(b.x, H-60, b.w, 60);
    ctx.fillStyle="#f8fafc"; const txt=`${b.mul}×`; ctx.fillText(txt, b.x + b.w/2 - ctx.measureText(txt).width/2, H-30);
  }
  if (ball) { ctx.beginPath(); ctx.fillStyle="#ffb703"; ctx.arc(ball.x,ball.y,ball.r,0,Math.PI*2); ctx.fill(); }
}
function loop() { step(); draw(); animId = requestAnimationFrame(loop); }
function updateScoreDisplay() { document.getElementById('score').textContent = `Score: ${score}`; }
document.getElementById('dropBtn').addEventListener('click', dropBall);
document.getElementById('resetBtn').addEventListener('click', ()=>{ score=0; updateScoreDisplay(); });
document.getElementById('cashoutBtn').addEventListener('click', ()=> {
  const wager = parseFloat(document.getElementById('wagerInput').value || "0");
  const payload = { score: score, timestamp: Date.now(), wager: wager, mode: 'plinko' };
  if (tg && tg.sendData) { tg.sendData(JSON.stringify(payload)); tg.close(); }
  else {
    // fallback direct POST (CORS must be enabled on server)
    fetch(HOST + '/api/submit_score', {method:'POST',headers:{'Content-Type':'application/json','X-PLINKO-SECRET':'%(secret)s'}, body: JSON.stringify(payload)})
      .then(r=>r.json()).then(j=>alert('Submitted: '+JSON.stringify(j))).catch(e=>alert('Submit failed'));
  }
});
document.getElementById('refreshLb').addEventListener('click', fetchLeaderboard);
async function fetchLeaderboard(){
  try {
    const res = await fetch(LB_URL);
    const data = await res.json();
    const container = document.getElementById('lbRows'); container.innerHTML = '';
    data.forEach((row, idx) => {
      const div = document.createElement('div'); div.className='lb-row';
      div.innerHTML = `<div>#${idx+1} ${row.username || row.user_id}</div><div>${row.score}</div>`;
      container.appendChild(div);
    });
  } catch(e) { console.error(e); }
}
build(); loop(); updateScoreDisplay(); fetchLeaderboard();
</script>
</body>
</html>
""" % {"host_url": HOST_URL.rstrip("/"), "secret": LEADERBOARD_SECRET}

@app.route(PLINKO_PATH, methods=["GET"])
def serve_plinko():
    resp = make_response(INDEX_HTML)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    # Allow from Telegram webview + dev clients
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route("/api/submit_score", methods=["POST"])
def api_submit_score():
    # simple secret header check
    header = request.headers.get("X-PLINKO-SECRET", "")
    if header != LEADERBOARD_SECRET:
        return jsonify({"error":"unauthorized"}), 401
    data = request.get_json() or {}
    user_id = data.get("user_id")  # optional if direct client submission
    username = data.get("username", "")[:64]
    score = int(data.get("score", 0))
    wager = float(data.get("wager", 0.0))
    payout = float(data.get("payout", 0.0))
    created_at = datetime.utcnow().isoformat()
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO scores(user_id, username, score, wager, payout, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, username, score, wager, payout, created_at))
    db.commit()
    return jsonify({"status":"ok"}), 201

@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    top_n = int(request.args.get("n", 10))
    db = get_db()
    rows = db.execute("SELECT user_id, username, score, payout, created_at FROM scores ORDER BY score DESC LIMIT ?",
                      (top_n,)).fetchall()
    out = [dict(r) for r in rows]
    return jsonify(out)

# ---------- Telegram bot ----------
async def plinko_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    webapp_url = PLINKO_WEBAPP_URL
    keyboard = [[InlineKeyboardButton("🎯 Play Plinko", web_app={"url": webapp_url})]]
    await update.message.reply_text("Tap to launch Plinko!", reply_markup=InlineKeyboardMarkup(keyboard))

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data_raw = None
    if update.effective_message and update.effective_message.web_app_data:
        data_raw = update.effective_message.web_app_data.data
    if not data_raw:
        await update.message.reply_text("No game data received.")
        return
    try:
        payload = json.loads(data_raw)
    except Exception:
        await update.message.reply_text("Invalid game payload.")
        return

    score = int(payload.get("score", 0))
    wager = float(payload.get("wager", 0.0))
    mode = payload.get("mode", "plinko")
    ts = payload.get("timestamp", None)

    uid = user.id
    uname = user.first_name or user.username or str(uid)

    # Basic balance/payout handling
    try:
        if wager > 0:
            bal = get_balance(uid)
            if wager > bal:
                await update.message.reply_text(f"Insufficient balance (${bal:.2f}) to place wager ${wager:.2f}.")
                return
            update_balance(uid, -wager)  # lock wager
            add_wager(uid, wager)
        else:
            bal = get_balance(uid)
    except Exception as e:
        logger.exception("balance error")
        await update.message.reply_text("Balance error. Try later.")
        return

    # payout policy: multiplier grows slowly with score
    multiplier = max(MIN_MULT, min(MAX_MULT, 0.01 * score + 0.5))
    payout = 0.0
    if wager > 0:
        payout = round(wager * multiplier, 2)
        update_balance(uid, payout)

    # save session in DB
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO game_sessions(user_id, mode, played_at, bet, won_amount, is_win) VALUES (?,?,?,?,?,?)",
                    (uid, mode, datetime.utcnow().isoformat(), wager, payout, int(payout > 0)))
        db.execute("INSERT INTO scores(user_id, username, score, wager, payout, created_at) VALUES (?,?,?,?,?,?)",
                   (uid, uname, score, wager, payout, datetime.utcnow().isoformat()))
        db.commit()
    except Exception:
        logger.exception("db save failed")

    # post to leaderboard endpoint (internal call)
    try:
        requests.post(f"http://127.0.0.1:{PORT}/api/submit_score",
                      json={"user_id": uid, "username": uname, "score": score, "wager": wager, "payout": payout},
                      headers={"X-PLINKO-SECRET": LEADERBOARD_SECRET},
                      timeout=3)
    except Exception:
        logger.exception("failed to post to leaderboard endpoint")

    # reply to user
    new_bal = get_balance(uid)
    msg = f"🎯 {user.first_name}, your score: <b>{score}</b>\n"
    if wager > 0:
        msg += f"Wager: ${wager:.2f}\nMultiplier: {multiplier:.2f}×\nPayout credited: ${payout:.2f}\n"
    else:
        msg += f"Free play, points recorded.\n"
    msg += f"Balance: ${new_bal:.2f}"
    await update.message.reply_text(msg, parse_mode="HTML")

def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("plinko", plinko_command))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    logger.info("Starting Telegram bot (polling)...")
    application.run_polling()

# ---------- Run Flask in thread and Bot in main thread ----------
if __name__ == "__main__":
    # Start flask server in background thread
    def flask_thread():
        logger.info(f"Starting Flask server on 0.0.0.0:{PORT} (serving {PLINKO_PATH})")
        # app.run uses werkzeug development server; ok for testing
        app.run(host="0.0.0.0", port=PORT, threaded=True)

    t = threading.Thread(target=flask_thread, daemon=True)
    t.start()
    # wait a little for Flask to be ready
    time.sleep(1.0)
    # Run bot (blocking)
    run_bot()
