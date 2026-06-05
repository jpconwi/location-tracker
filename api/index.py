"""
LocationTrack — api/index.py
Pure-Python FastAPI app for Vercel.
Turso accessed via HTTP REST API (no native libsql-client).
All HTML inlined to avoid filesystem path issues on Vercel.

FIXES:
  - Turso 400: args now uses correct typed-value format for all params
  - Real-time GPS: navigator.geolocation.watchPosition() auto-tracks movement
  - Distance shown live: each user sees km from themselves to others
"""
import os, math, json, logging, warnings

logging.getLogger("passlib").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", ".*error reading bcrypt version.*")

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from passlib.context import CryptContext

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════
TURSO_URL:      str = os.getenv("TURSO_DATABASE_URL", "").replace("libsql://", "https://")
TURSO_TOKEN:    str = os.getenv("TURSO_AUTH_TOKEN", "")
SECRET_KEY:     str = os.getenv("SECRET_KEY", "change-me")
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")
ALGORITHM           = "HS256"
TOKEN_TTL_MIN       = 60 * 24 * 7

# ═══════════════════════════════════════════════════
#  TURSO HTTP CLIENT  — FIX: correct typed-value args
# ═══════════════════════════════════════════════════
def _make_arg(v):
    """Convert a Python value to a Turso typed-value dict."""
    if v is None:
        return {"type": "null", "value": None}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}

async def turso(statements: list) -> list:
    url = f"{TURSO_URL}/v2/pipeline"
    requests = [
        {
            "type": "execute",
            "stmt": {
                "sql": s["q"],
                "args": [_make_arg(v) for v in s.get("params", [])]
            }
        }
        for s in statements
    ]
    requests.append({"type": "close"})

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
            json={"requests": requests}
        )
        if not r.is_success:
            raise HTTPException(502, f"Turso HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()

    results = []
    for item in data.get("results", []):
        if item.get("type") == "ok":
            rs = item.get("response", {}).get("result", {})
            cols = [c["name"] for c in rs.get("cols", [])]
            rows = [[cell.get("value") for cell in row] for row in rs.get("rows", [])]
            results.append({"cols": cols, "rows": rows,
                             "last_insert_rowid": rs.get("last_insert_rowid")})
        else:
            err = item.get("error", {})
            raise HTTPException(500, f"Turso error: {err.get('message', item)}")
    return results

async def q1(sql: str, params: list = []) -> dict:
    results = await turso([{"q": sql, "params": params}])
    return results[0] if results else {"cols": [], "rows": []}

# ═══════════════════════════════════════════════════
#  DB INIT
# ═══════════════════════════════════════════════════
async def init_db():
    await turso([
        {"q": """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )"""},
        {"q": """CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            label TEXT,
            checked_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )"""},
    ])

# ═══════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════
class UserCreate(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class CheckinCreate(BaseModel):
    latitude:  float
    longitude: float
    label:     Optional[str] = None

class DistanceRequest(BaseModel):
    lat1: float; lon1: float
    lat2: float; lon2: float

# ═══════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer(auto_error=False)

def make_token(uid: int) -> str:
    exp = datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MIN)
    return jwt.encode({"sub": str(uid), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

async def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        uid = int(jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid token")
    r = await q1("SELECT id,username,is_admin,created_at FROM users WHERE id=?", [uid])
    if not r["rows"]: raise HTTPException(401, "User not found")
    row = r["rows"][0]
    return {"id": int(row[0]), "username": row[1], "is_admin": bool(int(row[2] or 0)), "created_at": row[3]}

async def admin_only(user=Depends(current_user)):
    if not user["is_admin"]: raise HTTPException(403, "Admin only")
    return user

# ═══════════════════════════════════════════════════
#  CONTROLLERS
# ═══════════════════════════════════════════════════
async def ctrl_register(username, password, admin_code=""):
    ex = await q1("SELECT id FROM users WHERE username=?", [username])
    if ex["rows"]: raise HTTPException(400, "Username already taken")
    is_admin = 1 if admin_code == ADMIN_PASSWORD else 0
    hashed = pwd_ctx.hash(password)
    r = await q1("INSERT INTO users (username,password,is_admin) VALUES (?,?,?)",
                 [username, hashed, is_admin])
    uid = int(r["last_insert_rowid"])
    ur = await q1("SELECT id,username,is_admin,created_at FROM users WHERE id=?", [uid])
    row = ur["rows"][0]
    user = {"id": int(row[0]), "username": row[1], "is_admin": bool(int(row[2] or 0)), "created_at": row[3]}
    return {"access_token": make_token(user["id"]), "token_type": "bearer", "user": user}

async def ctrl_login(username, password):
    r = await q1("SELECT id,username,password,is_admin,created_at FROM users WHERE username=?", [username])
    if not r["rows"]: raise HTTPException(401, "Invalid credentials")
    row = r["rows"][0]
    if not pwd_ctx.verify(password, row[2]): raise HTTPException(401, "Invalid credentials")
    user = {"id": int(row[0]), "username": row[1], "is_admin": bool(int(row[3] or 0)), "created_at": row[4]}
    return {"access_token": make_token(user["id"]), "token_type": "bearer", "user": user}

async def ctrl_checkin(user_id, lat, lon, label=None):
    r = await q1("INSERT INTO checkins (user_id,latitude,longitude,label) VALUES (?,?,?,?)",
                 [user_id, lat, lon, label])
    cid = int(r["last_insert_rowid"])
    cr = await q1("SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
                  "FROM checkins c JOIN users u ON c.user_id=u.id WHERE c.id=?", [cid])
    row = cr["rows"][0]
    return {"id": int(row[0]), "user_id": int(row[1]), "username": row[2],
            "latitude": float(row[3]), "longitude": float(row[4]),
            "label": row[5], "checked_at": row[6]}

async def ctrl_live():
    r = await q1("""SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at
        FROM checkins c JOIN users u ON c.user_id=u.id
        WHERE c.id IN (SELECT MAX(id) FROM checkins GROUP BY user_id)
        ORDER BY c.checked_at DESC""")
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]),
             "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_history(uid):
    r = await q1("SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
                 "FROM checkins c JOIN users u ON c.user_id=u.id "
                 "WHERE c.user_id=? ORDER BY c.checked_at DESC LIMIT 20", [uid])
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]),
             "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_all_checkins():
    r = await q1("SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
                 "FROM checkins c JOIN users u ON c.user_id=u.id ORDER BY c.checked_at DESC")
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]),
             "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_list_users():
    r = await q1("SELECT id,username,is_admin,created_at FROM users ORDER BY created_at DESC")
    return [{"id": int(x[0]), "username": x[1], "is_admin": bool(int(x[2] or 0)), "created_at": x[3]}
            for x in r["rows"]]

async def ctrl_del_user(uid):
    await turso([{"q": "DELETE FROM checkins WHERE user_id=?", "params": [uid]},
                 {"q": "DELETE FROM users WHERE id=?", "params": [uid]}])
    return {"detail": "Deleted"}

async def ctrl_del_checkin(cid):
    await q1("DELETE FROM checkins WHERE id=?", [cid])
    return {"detail": "Deleted"}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    km = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return {"km": round(km, 3), "meters": round(km * 1000, 1)}

# ═══════════════════════════════════════════════════
#  SHARED CSS
# ═══════════════════════════════════════════════════
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap');
:root{--bg:#0a0e1a;--sf:#111827;--sf2:#1a2235;--bd:#1e2d45;--ac:#00d4ff;--ac2:#7c3aed;--ok:#10b981;--err:#ef4444;--warn:#f59e0b;--tx:#e2e8f0;--dim:#64748b}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;overflow-x:hidden}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:var(--sf)}::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
.navbar{display:flex;align-items:center;justify-content:space-between;padding:0 1.5rem;height:58px;background:rgba(10,14,26,.95);border-bottom:1px solid var(--bd);position:fixed;top:0;left:0;right:0;z-index:1000;backdrop-filter:blur(12px)}
.brand{font-size:1.05rem;font-weight:800;letter-spacing:.05em;color:var(--ac);text-decoration:none}.brand span{color:var(--tx)}
.nav-links{display:flex;gap:.75rem;align-items:center}.nav-links a{color:var(--dim);text-decoration:none;font-size:.82rem;font-weight:700;letter-spacing:.05em;transition:color .2s}.nav-links a:hover,.nav-links a.on{color:var(--ac)}
.nu{font-size:.75rem;color:var(--dim);font-family:'Space Mono',monospace}.nu strong{color:var(--ac)}
.btn{display:inline-flex;align-items:center;gap:.35rem;padding:.5rem 1.1rem;border-radius:6px;border:none;font-family:'Syne',sans-serif;font-size:.82rem;font-weight:700;letter-spacing:.05em;cursor:pointer;transition:all .2s}
.bp{background:var(--ac);color:var(--bg)}.bp:hover{background:#00b8e0;transform:translateY(-1px)}
.bs{background:var(--ok);color:#fff}.bs:hover{background:#059669;transform:translateY(-1px)}
.bd2{background:var(--err);color:#fff}.bd2:hover{background:#dc2626}
.bo{background:transparent;border:1px solid var(--bd);color:var(--dim)}.bo:hover{border-color:var(--ac);color:var(--ac)}
.blg{padding:.75rem 1.8rem;font-size:.95rem}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none!important}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:1.4rem}
.fl{display:block;font-size:.72rem;font-weight:700;letter-spacing:.1em;color:var(--dim);text-transform:uppercase;margin-bottom:.35rem}
.fi{width:100%;padding:.6rem .85rem;background:var(--sf2);border:1px solid var(--bd);border-radius:6px;color:var(--tx);font-family:'Space Mono',monospace;font-size:.82rem;transition:border-color .2s;outline:none}.fi:focus{border-color:var(--ac)}
.al{padding:.65rem .9rem;border-radius:6px;font-size:.82rem;margin-top:.65rem;display:none}
.al-e{background:rgba(239,68,68,.1);border:1px solid var(--err);color:#fca5a5}.al-s{background:rgba(16,185,129,.1);border:1px solid var(--ok);color:#6ee7b7}.al.show{display:block}
.badge{display:inline-block;padding:.12rem .45rem;border-radius:4px;font-size:.68rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.ba{background:rgba(124,58,237,.18);color:#a78bfa;border:1px solid #7c3aed}.bu{background:rgba(0,212,255,.08);color:var(--ac);border:1px solid rgba(0,212,255,.25)}
#toast-c{position:fixed;bottom:1.2rem;right:1.2rem;z-index:9999;display:flex;flex-direction:column;gap:.4rem}
.toast{padding:.65rem 1.1rem;border-radius:7px;font-size:.82rem;font-weight:600;min-width:200px;animation:si .3s ease;border:1px solid var(--bd)}
.ts{background:var(--sf);border-color:var(--ok);color:#6ee7b7}.te{background:var(--sf);border-color:var(--err);color:#fca5a5}.ti{background:var(--sf);border-color:var(--ac);color:var(--ac)}
@keyframes si{from{opacity:0;transform:translateX(36px)}to{opacity:1;transform:none}}
.leaflet-container{background:#0d1117!important}
.leaflet-popup-content-wrapper{background:var(--sf)!important;border:1px solid var(--bd)!important;border-radius:8px!important;color:var(--tx)!important;font-family:'Syne',sans-serif!important;box-shadow:0 8px 32px rgba(0,0,0,.55)!important}
.leaflet-popup-tip{background:var(--sf)!important}.leaflet-popup-close-button{color:var(--dim)!important}
.leaflet-control-zoom a{background:var(--sf)!important;color:var(--tx)!important;border-color:var(--bd)!important}
.leaflet-control-attribution{background:rgba(10,14,26,.8)!important;color:var(--dim)!important}
.dt{width:100%;border-collapse:collapse;font-size:.8rem}
.dt th{text-align:left;padding:.55rem .75rem;font-size:.67rem;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--bd)}
.dt td{padding:.65rem .75rem;border-bottom:1px solid rgba(30,45,69,.4);font-family:'Space Mono',monospace}
.dt tr:hover td{background:var(--sf2)}
.spin{width:16px;height:16px;border:2px solid rgba(0,212,255,.25);border-top-color:var(--ac);border-radius:50%;animation:sp .7s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
.mt1{margin-top:.5rem}.mt2{margin-top:1rem}.mt3{margin-top:1.5rem}
/* GPS status pill */
.gps-pill{display:inline-flex;align-items:center;gap:.35rem;padding:.28rem .6rem;border-radius:20px;font-size:.68rem;font-weight:700;letter-spacing:.06em;border:1px solid var(--bd);background:var(--sf2);transition:all .3s}
.gps-pill.active{border-color:var(--ok);color:var(--ok);background:rgba(16,185,129,.08)}
.gps-pill.error{border-color:var(--err);color:#fca5a5}
.gps-pill.searching{border-color:var(--warn);color:var(--warn)}
.gps-dot{width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
"""

JS_AUTH = """
const API = '';
const Auth = {
  token: () => localStorage.getItem('lt_tok'),
  user:  () => JSON.parse(localStorage.getItem('lt_usr') || 'null'),
  isAdmin: () => Auth.user()?.is_admin === true,
  save(d){ localStorage.setItem('lt_tok', d.access_token); localStorage.setItem('lt_usr', JSON.stringify(d.user)); },
  logout(){ localStorage.removeItem('lt_tok'); localStorage.removeItem('lt_usr'); location.href='/'; },
  async req(method, path, body=null){
    const h = {'Content-Type':'application/json'};
    if(Auth.token()) h['Authorization'] = 'Bearer '+Auth.token();
    const r = await fetch(API+path, {method, headers:h, body: body?JSON.stringify(body):null});
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail||'Request failed');
    return d;
  }
};
function toast(msg, type='info'){
  let c = document.getElementById('toast-c');
  if(!c){ c=document.createElement('div'); c.id='toast-c'; document.body.appendChild(c); }
  const t = document.createElement('div');
  t.className = `toast t${type[0]}`; t.textContent = msg;
  c.appendChild(t); setTimeout(()=>t.remove(), 3400);
}
function requireAuth(){ if(!Auth.token()) location.href='/'; }
function initNav(){
  const u = Auth.user(), el = document.getElementById('nav-user'), al = document.getElementById('nav-admin');
  if(el && u) el.innerHTML = `Logged in as <strong>${u.username}</strong>`;
  if(al && u?.is_admin) al.style.display='inline';
}
"""

# ═══════════════════════════════════════════════════
#  MAP JS — real-time GPS tracking
# ═══════════════════════════════════════════════════
JS_MAP = """
let map, markers={}, distLine=null, myLat=null, myLon=null;
let watchId=null, trackingActive=false, lastSentLat=null, lastSentLon=null;
const MIN_MOVE_M = 10; // only push update if moved ≥ 10 m

function initMap(){
  map = L.map('map',{zoomControl:false}).setView([13,122],6);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
    attribution:'© OpenStreetMap © CARTO', maxZoom:19}).addTo(map);
  L.control.zoom({position:'bottomright'}).addTo(map);
}

function mkIcon(color='#00d4ff', label='', isLive=false){
  const ring = isLive ? `<circle cx="16" cy="16" r="14" fill="none" stroke="${color}" stroke-width="2" opacity="0.4"><animate attributeName="r" from="10" to="18" dur="2s" repeatCount="indefinite"/><animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite"/></circle>` : '';
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="36" height="44" viewBox="0 0 36 44">
    ${ring}
    <path d="M18 0C8.06 0 0 8.06 0 18c0 13.5 18 26 18 26S36 31.5 36 18C36 8.06 27.94 0 18 0z" fill="${color}" opacity=".92"/>
    <circle cx="18" cy="18" r="8" fill="white" opacity=".88"/>
    <text x="18" y="22" text-anchor="middle" fill="${color}" font-size="8" font-family="Syne,sans-serif" font-weight="700">${label.slice(0,2).toUpperCase()}</text>
  </svg>`;
  return L.divIcon({html:svg,iconSize:[36,44],iconAnchor:[18,44],popupAnchor:[0,-44],className:''});
}

// haversine in JS for client-side distance computation
function hav(lat1,lon1,lat2,lon2){
  const R=6371000,toR=Math.PI/180;
  const dLat=(lat2-lat1)*toR, dLon=(lon2-lon1)*toR;
  const a=Math.sin(dLat/2)**2+Math.cos(lat1*toR)*Math.cos(lat2*toR)*Math.sin(dLon/2)**2;
  return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
}
function fmtDist(m){
  if(m<1000) return m.toFixed(0)+' m';
  return (m/1000).toFixed(2)+' km';
}

// ── Real-time GPS tracking ────────────────────────
function setGpsStatus(state, msg=''){
  const pill = document.getElementById('gps-pill');
  if(!pill) return;
  pill.className = 'gps-pill '+state;
  const labels = {active:'● LIVE', searching:'◌ Searching…', error:'✕ GPS Off'};
  pill.innerHTML = `<span class="gps-dot"></span>${labels[state]||state}${msg?' — '+msg:''}`;
}

async function pushLocation(lat, lon, label=null){
  try{
    await Auth.req('POST','/api/checkins/',{latitude:lat,longitude:lon,label});
    lastSentLat=lat; lastSentLon=lon;
  }catch(e){ console.warn('Push failed:',e.message); }
}

function startTracking(){
  if(!navigator.geolocation){ toast('Geolocation not supported','e'); return; }
  setGpsStatus('searching');
  watchId = navigator.geolocation.watchPosition(
    async pos => {
      const {latitude:lat, longitude:lon, accuracy} = pos.coords;
      myLat=lat; myLon=lon;
      trackingActive=true;
      setGpsStatus('active');
      // update my marker immediately on client
      updateMyMarker(lat,lon);
      // push to server only if moved meaningfully OR first time
      const moved = lastSentLat===null || hav(lastSentLat,lastSentLon,lat,lon) >= MIN_MOVE_M;
      if(moved) await pushLocation(lat,lon);
    },
    err => {
      setGpsStatus('error', err.code===1?'Permission denied':'Unavailable');
      toast('GPS: '+err.message,'e');
    },
    {enableHighAccuracy:true, maximumAge:3000, timeout:15000}
  );
}

function stopTracking(){
  if(watchId!==null){ navigator.geolocation.clearWatch(watchId); watchId=null; }
  trackingActive=false;
  setGpsStatus('error','Stopped');
}

function toggleTracking(){
  const btn = document.getElementById('btn-track');
  if(trackingActive){ stopTracking(); btn.textContent='▶ Start Tracking'; btn.className='btn bp'; }
  else { startTracking(); btn.textContent='⏹ Stop Tracking'; btn.className='btn bd2'; }
}

function updateMyMarker(lat,lon){
  const me = Auth.user();
  if(!me) return;
  if(markers[me.id]) map.removeLayer(markers[me.id]);
  markers[me.id] = L.marker([lat,lon],{icon:mkIcon('#10b981',me.username,true)})
    .addTo(map)
    .bindPopup(`<div style="min-width:155px">
      <div style="font-weight:800;font-size:1rem;margin-bottom:3px">${me.username} <span style="color:#10b981">(you)</span></div>
      <div style="font-family:monospace;font-size:.68rem;color:#64748b">${lat.toFixed(5)}, ${lon.toFixed(5)}</div>
      <div style="color:#10b981;font-size:.72rem;margin-top:5px">📡 Live tracking active</div>
    </div>`);
}

// ── Load all users from server ────────────────────
async function loadLive(){
  try{
    const cs = await Auth.req('GET','/api/checkins/live');
    const me = Auth.user();
    // remove non-me markers (me marker is managed by GPS watch)
    Object.entries(markers).forEach(([uid,m])=>{ if(parseInt(uid)!==me?.id) map.removeLayer(m); });
    cs.forEach(c=>{
      if(c.user_id===me?.id) return; // my marker handled by GPS
      const time = new Date(c.checked_at+'Z').toLocaleString();
      const distTxt = (myLat!==null) ? fmtDist(hav(myLat,myLon,c.latitude,c.longitude)) : '—';
      const m = L.marker([c.latitude,c.longitude],{icon:mkIcon('#00d4ff',c.username)}).addTo(map)
        .bindPopup(`<div style="min-width:165px">
          <div style="font-weight:800;font-size:1rem;margin-bottom:3px">${c.username}</div>
          ${c.label?`<div style="color:#94a3b8;font-size:.78rem;margin-bottom:3px">📍 ${c.label}</div>`:''}
          <div style="font-family:monospace;font-size:.68rem;color:#64748b">${time}</div>
          <div style="font-family:monospace;font-size:.67rem;color:#64748b;margin-top:2px">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</div>
          <div style="margin-top:6px;padding:4px 8px;background:rgba(0,212,255,.08);border-radius:5px;font-size:.76rem">
            📏 <strong style="color:var(--warn)">${distTxt}</strong> from you
          </div>
          <button onclick="pickDist(${c.latitude},${c.longitude},'${c.username}')"
            style="margin-top:7px;padding:3px 9px;background:#00d4ff;color:#0a0e1a;border:none;border-radius:4px;font-size:.72rem;font-weight:700;cursor:pointer">
            📏 Measure Distance</button>
        </div>`);
      markers[c.user_id]=m;
    });
    updateUserList(cs);
  }catch(e){ toast('Map load failed: '+e.message,'e'); }
}

async function pickDist(lat,lon,name){
  if(myLat===null){toast('Enable tracking first!','e');return;}
  await calcDist(myLat,myLon,lat,lon,'You',name);
}
async function calcDist(lat1,lon1,lat2,lon2,nA='A',nB='B'){
  try{
    const r=await Auth.req('POST','/api/checkins/distance',{lat1,lon1,lat2,lon2});
    if(distLine) map.removeLayer(distLine);
    distLine=L.polyline([[lat1,lon1],[lat2,lon2]],{color:'#f59e0b',weight:2,dashArray:'8,6',opacity:.8}).addTo(map);
    map.fitBounds([[lat1,lon1],[lat2,lon2]],{padding:[60,60]});
    const p=document.getElementById('dist-panel');
    if(p){
      p.style.display='block';
      document.getElementById('dist-from').textContent=nA;
      document.getElementById('dist-to').textContent=nB;
      document.getElementById('dist-km').textContent=r.km>=1?r.km.toFixed(2)+' km':r.meters.toFixed(0)+' m';
    }
  }catch(e){toast('Distance calc failed','e');}
}

function updateUserList(cs){
  const el=document.getElementById('user-list'); if(!el)return;
  const me=Auth.user();
  el.innerHTML=cs.map(c=>{
    const isMe=c.user_id===me?.id;
    const distTxt = (!isMe && myLat!==null) ? fmtDist(hav(myLat,myLon,c.latitude,c.longitude)) : '';
    return `<div onclick="flyTo(${c.user_id})" style="padding:.65rem;border-bottom:1px solid var(--bd);cursor:pointer;transition:background .2s" onmouseover="this.style.background='var(--sf2)'" onmouseout="this.style.background=''">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700;font-size:.88rem">${c.username}${isMe?' <span style=\\"color:var(--ok)\\">●</span>':''}</span>
        <span style="font-size:.68rem;font-family:monospace;color:var(--dim)">${new Date(c.checked_at+'Z').toLocaleTimeString()}</span>
      </div>
      ${c.label?`<div style="font-size:.72rem;color:var(--dim);margin-top:2px">📍 ${c.label}</div>`:''}
      ${distTxt?`<div style="font-size:.72rem;color:var(--warn);margin-top:2px">📏 ${distTxt} away</div>`:''}
    </div>`;
  }).join('');
}
function flyTo(uid){ const m=markers[uid]; if(m){map.flyTo(m.getLatLng(),15,{animate:true,duration:1});m.openPopup();} }
async function loadHistory(){
  const el=document.getElementById('hist-list'); if(!el)return;
  try{
    const h=await Auth.req('GET','/api/checkins/history');
    el.innerHTML=h.length?h.map(c=>`
      <div style="padding:.55rem 0;border-bottom:1px solid var(--bd);font-size:.77rem">
        <div style="display:flex;justify-content:space-between">
          <span style="font-family:monospace;color:var(--ac)">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</span>
          <span style="color:var(--dim)">${new Date(c.checked_at+'Z').toLocaleString()}</span>
        </div>
        ${c.label?`<div style="color:var(--dim);margin-top:2px">📍 ${c.label}</div>`:''}
      </div>`).join(''):'<div style="color:var(--dim);font-size:.82rem">No check-ins yet.</div>';
  }catch(e){el.textContent='Failed to load history';}
}
"""

JS_ADMIN = """
async function loadUsers(){
  const tb=document.getElementById('users-tb'); if(!tb)return;
  tb.innerHTML='<tr><td colspan="5" style="color:var(--dim);padding:1rem">Loading…</td></tr>';
  try{
    const us=await Auth.req('GET','/api/admin/users');
    tb.innerHTML=us.map(u=>`<tr>
      <td>${u.id}</td><td><strong>${u.username}</strong></td>
      <td><span class="badge ${u.is_admin?'ba':'bu'}">${u.is_admin?'Admin':'User'}</span></td>
      <td>${new Date(u.created_at+'Z').toLocaleString()}</td>
      <td><button class="btn bd2" onclick="delUser(${u.id},'${u.username}')" style="padding:.28rem .65rem;font-size:.72rem">Delete</button></td>
    </tr>`).join('');
    const el=document.getElementById('tot-users'); if(el)el.textContent=us.length;
  }catch(e){tb.innerHTML=`<tr><td colspan="5" style="color:var(--err)">${e.message}</td></tr>`;}
}
async function loadCheckins(){
  const tb=document.getElementById('cins-tb'); if(!tb)return;
  tb.innerHTML='<tr><td colspan="6" style="color:var(--dim);padding:1rem">Loading…</td></tr>';
  try{
    const cs=await Auth.req('GET','/api/admin/checkins');
    tb.innerHTML=cs.map(c=>`<tr>
      <td>${c.id}</td><td><strong>${c.username}</strong></td>
      <td style="font-size:.72rem">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</td>
      <td style="color:var(--dim)">${c.label||'—'}</td>
      <td style="font-size:.72rem">${new Date(c.checked_at+'Z').toLocaleString()}</td>
      <td><button class="btn bd2" onclick="delCin(${c.id})" style="padding:.28rem .65rem;font-size:.72rem">Delete</button></td>
    </tr>`).join('');
    const el=document.getElementById('tot-cins'); if(el)el.textContent=cs.length;
  }catch(e){tb.innerHTML=`<tr><td colspan="6" style="color:var(--err)">${e.message}</td></tr>`;}
}
async function delUser(id,name){
  if(!confirm(`Delete "${name}" and all their check-ins?`))return;
  try{ await Auth.req('DELETE',`/api/admin/users/${id}`); toast(`"${name}" deleted`,'s'); loadUsers();loadCheckins(); }
  catch(e){toast('Failed: '+e.message,'e');}
}
async function delCin(id){
  if(!confirm('Delete this check-in?'))return;
  try{ await Auth.req('DELETE',`/api/admin/checkins/${id}`); toast('Deleted','s'); loadCheckins(); }
  catch(e){toast('Failed: '+e.message,'e');}
}
function switchTab(tab,btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.tab-pnl').forEach(p=>p.style.display='none');
  document.getElementById('tp-'+tab).style.display='block';
  btn.classList.add('on');
  if(tab==='users')loadUsers(); else loadCheckins();
}
"""

PAGE_INDEX = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LocationTrack</title><style>{CSS}
body{{display:flex;align-items:center;justify-content:center;min-height:100vh}}
.wrap{{width:100%;max-width:400px;padding:1.5rem}}
.brand-box{{text-align:center;margin-bottom:2rem}}
.logo{{font-size:2.4rem;margin-bottom:.4rem}}
.bname{{font-size:1.4rem;font-weight:800;color:var(--ac)}}.bname span{{color:var(--tx)}}
.btag{{font-size:.75rem;color:var(--dim);margin-top:.25rem;font-family:'Space Mono',monospace}}
.tabs{{display:flex;border:1px solid var(--bd);border-radius:7px;overflow:hidden;margin-bottom:1.2rem}}
.tb{{flex:1;padding:.55rem;background:transparent;border:none;color:var(--dim);font-family:'Syne',sans-serif;font-size:.82rem;font-weight:700;letter-spacing:.05em;cursor:pointer;transition:all .2s}}
.tb.on{{background:var(--ac);color:var(--bg)}}
.pnl{{display:none}}.pnl.on{{display:block}}
.adm-t{{margin-top:.7rem;padding:.55rem;border-radius:6px;background:rgba(124,58,237,.07);border:1px solid rgba(124,58,237,.25);font-size:.75rem;color:#a78bfa}}
.adm-t summary{{cursor:pointer;font-weight:700}}.adm-t .fi{{margin-top:.45rem}}
.grid-bg{{position:fixed;inset:0;z-index:-1;pointer-events:none;background-image:linear-gradient(rgba(0,212,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.025) 1px,transparent 1px);background-size:40px 40px}}
.glow{{position:fixed;top:-120px;left:50%;transform:translateX(-50%);width:560px;height:360px;z-index:-1;pointer-events:none;background:radial-gradient(ellipse,rgba(0,212,255,.07) 0%,transparent 70%)}}
.fg{{display:flex;flex-direction:column;gap:.75rem}}
</style></head><body>
<div class="grid-bg"></div><div class="glow"></div>
<div class="wrap">
  <div class="brand-box">
    <div class="logo">📍</div>
    <div class="bname">Location<span>Track</span></div>
    <div class="btag">Real-time GPS tracking & sharing</div>
  </div>
  <div class="tabs">
    <button class="tb on" onclick="sw('login',this)">Sign In</button>
    <button class="tb" onclick="sw('reg',this)">Register</button>
  </div>
  <div id="pnl-login" class="pnl on"><div class="card"><div class="fg">
    <div><label class="fl">Username</label><input id="lu" class="fi" type="text" placeholder="your_username" autocomplete="username"/></div>
    <div><label class="fl">Password</label><input id="lp" class="fi" type="password" placeholder="••••••••" autocomplete="current-password"/></div>
    <button class="btn bp" style="width:100%" onclick="doLogin()">Sign In →</button>
    <div id="l-al" class="al al-e"></div>
  </div></div></div>
  <div id="pnl-reg" class="pnl"><div class="card"><div class="fg">
    <div><label class="fl">Username</label><input id="ru" class="fi" type="text" placeholder="choose_a_name" autocomplete="username"/></div>
    <div><label class="fl">Password</label><input id="rp" class="fi" type="password" placeholder="••••••••" autocomplete="new-password"/></div>
    <details class="adm-t"><summary>🔐 Admin Code (optional)</summary><input id="ra" class="fi" type="password" placeholder="Admin code"/></details>
    <button class="btn bs" style="width:100%" onclick="doReg()">Create Account →</button>
    <div id="r-al" class="al al-e"></div>
  </div></div></div>
</div>
<script>{JS_AUTH}
if(Auth.token()) location.href='/map';
function sw(n,btn){{document.querySelectorAll('.tb').forEach(b=>b.classList.remove('on'));document.querySelectorAll('.pnl').forEach(p=>p.classList.remove('on'));btn.classList.add('on');document.getElementById('pnl-'+n).classList.add('on');}}
async function doLogin(){{
  const u=document.getElementById('lu').value.trim(),p=document.getElementById('lp').value,al=document.getElementById('l-al');
  al.classList.remove('show');
  if(!u||!p){{al.textContent='Fill in all fields';al.classList.add('show');return;}}
  try{{const d=await Auth.req('POST','/api/auth/login',{{username:u,password:p}});Auth.save(d);location.href='/map';}}
  catch(e){{al.textContent=e.message;al.classList.add('show');}}
}}
async function doReg(){{
  const u=document.getElementById('ru').value.trim(),p=document.getElementById('rp').value,
        a=document.getElementById('ra').value,al=document.getElementById('r-al');
  al.classList.remove('show');
  if(!u||!p){{al.textContent='Fill in all fields';al.classList.add('show');return;}}
  try{{const d=await Auth.req('POST',`/api/auth/register?admin_code=${{encodeURIComponent(a)}}`,{{username:u,password:p}});Auth.save(d);location.href='/map';}}
  catch(e){{al.textContent=e.message;al.classList.add('show');}}
}}
document.addEventListener('keydown',e=>{{if(e.key==='Enter'){{if(document.getElementById('pnl-login').classList.contains('on'))doLogin();else doReg();}}}});
</script></body></html>"""

PAGE_MAP = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LocationTrack — Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>{CSS}
body{{overflow:hidden}}.page{{padding-top:58px;height:100vh;display:flex}}
.sb{{width:285px;min-width:285px;display:flex;flex-direction:column;background:var(--sf);border-right:1px solid var(--bd);overflow:hidden;z-index:500}}
.sb-top{{padding:.9rem;border-bottom:1px solid var(--bd)}}
.sb-sec{{padding:.6rem .9rem;font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--bd)}}
.sb-sc{{flex:1;overflow-y:auto}}
.map-w{{flex:1;position:relative}}#map{{width:100%;height:100%}}
.rp{{width:245px;min-width:245px;display:flex;flex-direction:column;background:var(--sf);border-left:1px solid var(--bd);overflow:hidden;z-index:500}}
.rp-top{{padding:.65rem .9rem;border-bottom:1px solid var(--bd);font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}}
#dist-panel{{display:none;margin:.65rem;padding:.65rem;background:var(--sf2);border:1px solid var(--bd);border-radius:8px}}
.dp-t{{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:.4rem}}
.dp-r{{font-size:.75rem;color:var(--dim);margin-bottom:.3rem;font-family:monospace}}
#dist-km{{font-size:1.5rem;font-weight:800;color:var(--warn);font-family:monospace}}
.dp-x{{background:none;border:none;color:var(--dim);cursor:pointer;font-size:.72rem;margin-top:.35rem;display:block;text-align:right}}.dp-x:hover{{color:var(--err)}}
.rld{{display:flex;justify-content:space-between;align-items:center;padding:.4rem .9rem;border-bottom:1px solid var(--bd)}}
.track-box{{display:flex;flex-direction:column;gap:.45rem}}
#hist-list{{padding:0 .75rem;max-height:190px;overflow-y:auto}}
.gps-status-row{{display:flex;align-items:center;justify-content:space-between;margin-bottom:.4rem}}
@media(max-width:640px){{.sb{{width:220px;min-width:220px}}.rp{{display:none}}}}
</style></head><body>
<nav class="navbar">
  <a href="/map" class="brand">📍 <span>Location</span>Track</a>
  <div class="nav-links">
    <a href="/map" class="on">Map</a>
    <a href="/admin" id="nav-admin" style="display:none;color:#a78bfa">Admin</a>
    <a href="#" onclick="Auth.logout()">Sign Out</a>
  </div>
  <div class="nu" id="nav-user"></div>
</nav>
<div class="page">
  <aside class="sb">
    <div class="sb-top"><div class="track-box">
      <div class="gps-status-row">
        <span id="gps-pill" class="gps-pill searching"><span class="gps-dot"></span>Searching…</span>
      </div>
      <button id="btn-track" class="btn bp" style="width:100%;justify-content:center" onclick="toggleTracking()">▶ Start Tracking</button>
      <button class="btn bo" style="width:100%;justify-content:center;font-size:.78rem" onclick="flyToMe()">🎯 Center on Me</button>
    </div></div>
    <div class="rld">
      <div class="sb-sec" style="border:none;padding:0">Online Users</div>
      <button class="btn bo" onclick="loadLive()" style="padding:.22rem .55rem;font-size:.7rem">↻</button>
    </div>
    <div class="sb-sc"><div id="user-list" style="padding:.2rem 0"><div style="padding:.9rem;color:var(--dim);font-size:.8rem">Loading…</div></div></div>
    <div class="sb-sec" style="cursor:pointer" onclick="toggleHist()">My History ▾</div>
    <div id="hist-list"></div>
  </aside>
  <div class="map-w"><div id="map"></div></div>
  <aside class="rp">
    <div class="rp-top">📏 Distance Tool</div>
    <div style="padding:.7rem;font-size:.78rem;color:var(--dim);line-height:1.55">
      Start tracking to see live distance to each user.<br><br>
      Click <strong style="color:var(--ac)">Measure Distance</strong> on any pin for exact calculation.
    </div>
    <div id="dist-panel">
      <div class="dp-t">📏 Estimated Distance</div>
      <div class="dp-r"><span id="dist-from">You</span> → <span id="dist-to">User</span></div>
      <div id="dist-km">—</div>
      <button class="dp-x" onclick="closeDist()">✕ Clear</button>
    </div>
    <div style="padding:.7rem;border-top:1px solid var(--bd);margin-top:auto">
      <div style="font-size:.65rem;color:var(--dim);letter-spacing:.1em;text-transform:uppercase;margin-bottom:.45rem">Manual Distance</div>
      <div style="display:flex;flex-direction:column;gap:.4rem">
        <input id="m1" class="fi" type="number" step="any" placeholder="Lat 1"/>
        <input id="m2" class="fi" type="number" step="any" placeholder="Lon 1"/>
        <input id="m3" class="fi" type="number" step="any" placeholder="Lat 2"/>
        <input id="m4" class="fi" type="number" step="any" placeholder="Lon 2"/>
        <button class="btn bp" onclick="manDist()" style="width:100%">Calculate</button>
      </div>
    </div>
  </aside>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>{JS_AUTH}{JS_MAP}
requireAuth();initNav();initMap();loadLive();loadHistory();
// Auto-start tracking on page load
startTracking();
document.getElementById('btn-track').textContent='⏹ Stop Tracking';
document.getElementById('btn-track').className='btn bd2';
trackingActive=true;
// Refresh other users every 10 seconds
setInterval(loadLive, 10000);
function flyToMe(){{
  if(myLat!==null) map.flyTo([myLat,myLon],16,{{animate:true,duration:1.2}});
  else toast('Location not available yet','e');
}}
function toggleHist(){{const e=document.getElementById('hist-list');e.style.display=e.style.display==='none'?'block':'none';}}
function closeDist(){{document.getElementById('dist-panel').style.display='none';if(distLine){{map.removeLayer(distLine);distLine=null;}}}}
async function manDist(){{
  const v=[...document.querySelectorAll('#m1,#m2,#m3,#m4')].map(i=>parseFloat(i.value));
  if(v.some(isNaN)){{toast('Enter valid coordinates','e');return;}}
  await calcDist(v[0],v[1],v[2],v[3],'Point A','Point B');
}}
</script></body></html>"""

PAGE_ADMIN = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LocationTrack — Admin</title>
<style>{CSS}
.page{{padding-top:78px;max-width:1080px;margin:0 auto;padding-left:1.5rem;padding-right:1.5rem;padding-bottom:2rem}}
.ah{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.4rem}}
.at{{font-size:1.35rem;font-weight:800}}.at span{{color:var(--ac2)}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:.9rem;margin-bottom:1.4rem}}
.sc{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:1.1rem 1.4rem}}
.sl{{font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}}
.sv{{font-size:1.9rem;font-weight:800;font-family:'Space Mono',monospace;color:var(--ac)}}
.tabs{{display:flex;border:1px solid var(--bd);border-radius:7px;overflow:hidden;width:fit-content;margin-bottom:1.1rem}}
.tab-btn{{padding:.5rem 1.4rem;background:transparent;border:none;color:var(--dim);font-family:'Syne',sans-serif;font-size:.82rem;font-weight:700;letter-spacing:.05em;cursor:pointer;transition:all .2s}}
.tab-btn.on{{background:var(--ac2);color:#fff}}.tab-btn:hover:not(.on){{color:var(--tx)}}
.tw{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;overflow:hidden}}
.tt{{display:flex;align-items:center;justify-content:space-between;padding:.8rem .9rem;border-bottom:1px solid var(--bd)}}
.tt h3{{font-size:.82rem;font-weight:700}}.tsc{{overflow-x:auto}}.dt{{min-width:560px}}
#acc-denied{{display:none;text-align:center;padding:4rem 1rem}}
#acc-denied .ic{{font-size:3.5rem;margin-bottom:.9rem}}
#acc-denied h2{{font-size:1.3rem;color:var(--err);margin-bottom:.4rem}}
#acc-denied p{{color:var(--dim)}}
@media(max-width:640px){{.stats{{grid-template-columns:1fr 1fr}}}}
</style></head><body>
<nav class="navbar">
  <a href="/map" class="brand">📍 <span>Location</span>Track</a>
  <div class="nav-links">
    <a href="/map">Map</a>
    <a href="/admin" class="on" style="color:#a78bfa">Admin</a>
    <a href="#" onclick="Auth.logout()">Sign Out</a>
  </div>
  <div class="nu" id="nav-user"></div>
</nav>
<div class="page">
  <div id="acc-denied"><div class="ic">🔐</div><h2>Admin Access Required</h2><p>You don't have permission to view this page.</p><a href="/map" class="btn bo mt2">← Back to Map</a></div>
  <div id="adm-content">
    <div class="ah">
      <div><div class="at">Admin <span>Panel</span></div><div style="font-size:.78rem;color:var(--dim);margin-top:.2rem;font-family:monospace">Full system control</div></div>
      <button class="btn bo" onclick="loadUsers();loadCheckins()">↻ Refresh All</button>
    </div>
    <div class="stats">
      <div class="sc"><div class="sl">Total Users</div><div class="sv" id="tot-users">—</div></div>
      <div class="sc"><div class="sl">Total Check-ins</div><div class="sv" id="tot-cins">—</div></div>
      <div class="sc"><div class="sl">Logged In As</div><div class="sv" id="adm-name" style="font-size:1rem;padding-top:.35rem">—</div></div>
    </div>
    <div class="tabs">
      <button class="tab-btn on" onclick="switchTab('users',this)">👥 Users</button>
      <button class="tab-btn" onclick="switchTab('checkins',this)">📍 Check-ins</button>
    </div>
    <div id="tp-users" class="tw tab-pnl">
      <div class="tt"><h3>All Users</h3><button class="btn bo" onclick="loadUsers()" style="padding:.25rem .6rem;font-size:.7rem">↻</button></div>
      <div class="tsc"><table class="dt"><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Registered</th><th>Actions</th></tr></thead>
      <tbody id="users-tb"><tr><td colspan="5" style="color:var(--dim);padding:1.2rem">Loading…</td></tr></tbody></table></div>
    </div>
    <div id="tp-checkins" class="tw tab-pnl" style="display:none">
      <div class="tt"><h3>All Check-ins</h3><button class="btn bo" onclick="loadCheckins()" style="padding:.25rem .6rem;font-size:.7rem">↻</button></div>
      <div class="tsc"><table class="dt"><thead><tr><th>ID</th><th>User</th><th>Coordinates</th><th>Label</th><th>Time</th><th>Actions</th></tr></thead>
      <tbody id="cins-tb"><tr><td colspan="6" style="color:var(--dim);padding:1.2rem">Loading…</td></tr></tbody></table></div>
    </div>
  </div>
</div>
<script>{JS_AUTH}{JS_ADMIN}
requireAuth();initNav();
const u=Auth.user();
if(!u?.is_admin){{document.getElementById('adm-content').style.display='none';document.getElementById('acc-denied').style.display='block';}}
else{{document.getElementById('adm-name').textContent=u.username;loadUsers();loadCheckins();}}
</script></body></html>"""

# ═══════════════════════════════════════════════════
#  FASTAPI APP
# ═══════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="LocationTrack", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── AUTH ──────────────────────────────────────────
@app.post("/api/auth/register")
async def register(body: UserCreate, admin_code: str = ""):
    return await ctrl_register(body.username, body.password, admin_code)

@app.post("/api/auth/login")
async def login(body: UserLogin):
    return await ctrl_login(body.username, body.password)

# ── CHECKINS ──────────────────────────────────────
@app.post("/api/checkins/")
async def checkin(body: CheckinCreate, user=Depends(current_user)):
    return await ctrl_checkin(user["id"], body.latitude, body.longitude, body.label)

@app.get("/api/checkins/live")
async def live(_u=Depends(current_user)):
    return await ctrl_live()

@app.get("/api/checkins/history")
async def history(user=Depends(current_user)):
    return await ctrl_history(user["id"])

@app.post("/api/checkins/distance")
async def distance(body: DistanceRequest, _u=Depends(current_user)):
    return haversine(body.lat1, body.lon1, body.lat2, body.lon2)

# ── ADMIN ─────────────────────────────────────────
@app.get("/api/admin/users")
async def adm_users(_a=Depends(admin_only)):
    return await ctrl_list_users()

@app.delete("/api/admin/users/{uid}")
async def adm_del_user(uid: int, _a=Depends(admin_only)):
    return await ctrl_del_user(uid)

@app.get("/api/admin/checkins")
async def adm_checkins(_a=Depends(admin_only)):
    return await ctrl_all_checkins()

@app.delete("/api/admin/checkins/{cid}")
async def adm_del_checkin(cid: int, _a=Depends(admin_only)):
    return await ctrl_del_checkin(cid)

# ── PAGES ─────────────────────────────────────────
@app.get("/",      response_class=HTMLResponse)
async def pg_index(): return HTMLResponse(PAGE_INDEX)

@app.get("/map",   response_class=HTMLResponse)
async def pg_map():   return HTMLResponse(PAGE_MAP)

@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return HTMLResponse(PAGE_ADMIN)
