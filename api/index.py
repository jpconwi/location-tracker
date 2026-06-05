"""
LocationTrack — api/index.py
Satellite map, light UI, mobile-responsive for Android/iOS.
"""
import os, math, logging, warnings

logging.getLogger("passlib").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", ".*error reading bcrypt version.*")

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
#  TURSO HTTP CLIENT
# ═══════════════════════════════════════════════════
def _make_arg(v):
    if v is None:         return {"type": "null",    "value": None}
    if isinstance(v, bool): return {"type": "integer","value": str(int(v))}
    if isinstance(v, int):  return {"type": "integer","value": str(v)}
    if isinstance(v, float):return {"type": "float",  "value": str(v)}
    return {"type": "text", "value": str(v)}

async def turso(statements: list) -> list:
    url = f"{TURSO_URL}/v2/pipeline"
    requests = [
        {"type": "execute", "stmt": {"sql": s["q"], "args": [_make_arg(v) for v in s.get("params", [])]}}
        for s in statements
    ]
    requests.append({"type": "close"})
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url,
            headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
            json={"requests": requests})
        if not r.is_success:
            raise HTTPException(502, f"Turso HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    results = []
    for item in data.get("results", []):
        if item.get("type") == "ok":
            rs = item.get("response", {}).get("result", {})
            cols = [c["name"] for c in rs.get("cols", [])]
            rows = [[cell.get("value") for cell in row] for row in rs.get("rows", [])]
            results.append({"cols": cols, "rows": rows, "last_insert_rowid": rs.get("last_insert_rowid")})
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
    if not creds: raise HTTPException(401, "Not authenticated")
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
    r = await q1("INSERT INTO users (username,password,is_admin) VALUES (?,?,?)", [username, hashed, is_admin])
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
            "latitude": float(row[3]), "longitude": float(row[4]), "label": row[5], "checked_at": row[6]}

async def ctrl_live():
    r = await q1("""SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at
        FROM checkins c JOIN users u ON c.user_id=u.id
        WHERE c.id IN (SELECT MAX(id) FROM checkins GROUP BY user_id)
        ORDER BY c.checked_at DESC""")
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]), "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_history(uid):
    r = await q1("SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
                 "FROM checkins c JOIN users u ON c.user_id=u.id "
                 "WHERE c.user_id=? ORDER BY c.checked_at DESC LIMIT 20", [uid])
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]), "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_all_checkins():
    r = await q1("SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
                 "FROM checkins c JOIN users u ON c.user_id=u.id ORDER BY c.checked_at DESC")
    return [{"id": int(x[0]), "user_id": int(x[1]), "username": x[2],
             "latitude": float(x[3]), "longitude": float(x[4]), "label": x[5], "checked_at": x[6]} for x in r["rows"]]

async def ctrl_list_users():
    r = await q1("SELECT id,username,is_admin,created_at FROM users ORDER BY created_at DESC")
    return [{"id": int(x[0]), "username": x[1], "is_admin": bool(int(x[2] or 0)), "created_at": x[3]} for x in r["rows"]]

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
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    km = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return {"km": round(km, 3), "meters": round(km*1000, 1)}

# ═══════════════════════════════════════════════════
#  SHARED CSS — Light, clean, mobile-first
# ═══════════════════════════════════════════════════
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{
  --bg:#f0f4f8;--sf:#ffffff;--sf2:#f8fafc;--bd:#e2e8f0;
  --ac:#0ea5e9;--ac2:#7c3aed;--ok:#10b981;--err:#ef4444;--warn:#f59e0b;
  --tx:#1e293b;--dim:#64748b;--dim2:#94a3b8;
  --shadow:0 2px 12px rgba(0,0,0,.08);--shadow-lg:0 8px 32px rgba(0,0,0,.12);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--tx);font-family:'Inter',sans-serif;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--bd);border-radius:4px}

/* NAVBAR */
.navbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 1rem;height:56px;
  background:var(--sf);border-bottom:1px solid var(--bd);
  position:fixed;top:0;left:0;right:0;z-index:1000;
  box-shadow:var(--shadow);
}
.brand{font-size:1rem;font-weight:800;color:var(--ac);text-decoration:none;display:flex;align-items:center;gap:.4rem}
.brand span{color:var(--tx)}
.nav-links{display:flex;gap:.5rem;align-items:center}
.nav-links a{color:var(--dim);text-decoration:none;font-size:.8rem;font-weight:600;padding:.3rem .6rem;border-radius:6px;transition:all .2s}
.nav-links a:hover,.nav-links a.on{background:var(--ac);color:#fff}
.nu{font-size:.72rem;color:var(--dim);display:none}
@media(min-width:640px){.nu{display:block}}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;padding:.55rem 1.1rem;border-radius:10px;border:none;font-family:'Inter',sans-serif;font-size:.84rem;font-weight:600;cursor:pointer;transition:all .18s;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
.bp{background:var(--ac);color:#fff;box-shadow:0 2px 8px rgba(14,165,233,.3)}.bp:hover{background:#0284c7;transform:translateY(-1px)}
.bs{background:var(--ok);color:#fff;box-shadow:0 2px 8px rgba(16,185,129,.3)}.bs:hover{background:#059669;transform:translateY(-1px)}
.bd2{background:var(--err);color:#fff;box-shadow:0 2px 8px rgba(239,68,68,.25)}.bd2:hover{background:#dc2626}
.bo{background:var(--sf);border:1.5px solid var(--bd);color:var(--dim)}.bo:hover{border-color:var(--ac);color:var(--ac)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none!important}
.btn:active{transform:scale(.97)!important}

/* INPUTS */
.fl{display:block;font-size:.72rem;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.3rem}
.fi{width:100%;padding:.65rem .9rem;background:var(--sf2);border:1.5px solid var(--bd);border-radius:10px;color:var(--tx);font-family:'Inter',sans-serif;font-size:.88rem;transition:border-color .2s;outline:none;-webkit-appearance:none}
.fi:focus{border-color:var(--ac);background:#fff}

/* ALERTS */
.al{padding:.65rem .9rem;border-radius:8px;font-size:.82rem;margin-top:.5rem;display:none}
.al-e{background:#fef2f2;border:1px solid #fecaca;color:#dc2626}
.al-s{background:#f0fdf4;border:1px solid #86efac;color:#15803d}
.al.show{display:block}

/* BADGES */
.badge{display:inline-block;padding:.12rem .5rem;border-radius:20px;font-size:.68rem;font-weight:700;letter-spacing:.04em}
.ba{background:#ede9fe;color:#7c3aed}.bu{background:#e0f2fe;color:#0284c7}

/* TOAST */
#toast-c{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;align-items:center;gap:.4rem;pointer-events:none;width:90%;max-width:360px}
.toast{padding:.7rem 1.1rem;border-radius:10px;font-size:.82rem;font-weight:600;width:100%;text-align:center;animation:si .3s ease;box-shadow:var(--shadow-lg)}
.ts{background:#f0fdf4;border:1px solid #86efac;color:#15803d}
.te{background:#fef2f2;border:1px solid #fecaca;color:#dc2626}
.ti{background:#f0f9ff;border:1px solid #7dd3fc;color:#0284c7}
@keyframes si{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}

/* LEAFLET overrides for satellite */
.leaflet-popup-content-wrapper{background:#fff!important;border-radius:12px!important;box-shadow:var(--shadow-lg)!important;border:1px solid var(--bd)!important;color:var(--tx)!important;font-family:'Inter',sans-serif!important}
.leaflet-popup-tip{background:#fff!important}
.leaflet-popup-close-button{color:var(--dim)!important;font-size:1.1rem!important}
.leaflet-control-zoom a{background:#fff!important;color:var(--tx)!important;border-color:var(--bd)!important;font-weight:700!important}
.leaflet-control-attribution{background:rgba(255,255,255,.8)!important;color:var(--dim)!important;font-size:.6rem!important}

/* CARD */
.card{background:var(--sf);border:1px solid var(--bd);border-radius:16px;padding:1.4rem;box-shadow:var(--shadow)}

/* GPS PILL */
.gps-pill{display:inline-flex;align-items:center;gap:.4rem;padding:.3rem .7rem;border-radius:20px;font-size:.72rem;font-weight:700;transition:all .3s;border:1.5px solid var(--bd);background:var(--sf2)}
.gps-pill.active{border-color:var(--ok);color:var(--ok);background:#f0fdf4}
.gps-pill.error{border-color:var(--err);color:var(--err);background:#fef2f2}
.gps-pill.searching{border-color:var(--warn);color:#d97706;background:#fffbeb}
.gps-dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex-shrink:0}
.gps-pill.active .gps-dot{animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}

/* ADMIN TABLE */
.dt{width:100%;border-collapse:collapse;font-size:.8rem}
.dt th{text-align:left;padding:.55rem .75rem;font-size:.67rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);border-bottom:2px solid var(--bd);font-weight:600}
.dt td{padding:.65rem .75rem;border-bottom:1px solid var(--bd);color:var(--tx)}
.dt tr:hover td{background:var(--sf2)}

/* SPINNER */
.spin{width:15px;height:15px;border:2px solid rgba(14,165,233,.25);border-top-color:var(--ac);border-radius:50%;animation:sp .7s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
"""

JS_AUTH = """
const API='';
const Auth={
  token:()=>localStorage.getItem('lt_tok'),
  user:()=>JSON.parse(localStorage.getItem('lt_usr')||'null'),
  isAdmin:()=>Auth.user()?.is_admin===true,
  save(d){localStorage.setItem('lt_tok',d.access_token);localStorage.setItem('lt_usr',JSON.stringify(d.user));},
  logout(){localStorage.removeItem('lt_tok');localStorage.removeItem('lt_usr');location.href='/';},
  async req(method,path,body=null){
    const h={'Content-Type':'application/json'};
    if(Auth.token()) h['Authorization']='Bearer '+Auth.token();
    const r=await fetch(API+path,{method,headers:h,body:body?JSON.stringify(body):null});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'Request failed');
    return d;
  }
};
function toast(msg,type='info'){
  let c=document.getElementById('toast-c');
  if(!c){c=document.createElement('div');c.id='toast-c';document.body.appendChild(c);}
  const t=document.createElement('div');
  t.className=`toast t${type[0]}`;t.textContent=msg;
  c.appendChild(t);setTimeout(()=>t.remove(),3400);
}
function requireAuth(){if(!Auth.token())location.href='/';}
function initNav(){
  const u=Auth.user(),el=document.getElementById('nav-user'),al=document.getElementById('nav-admin');
  if(el&&u) el.textContent='Hi, '+u.username;
  if(al&&u?.is_admin) al.style.display='inline';
}
"""

JS_MAP = """
let map,markers={},distLine=null,myLat=null,myLon=null;
let watchId=null,trackingActive=false,lastSentLat=null,lastSentLon=null;
const MIN_MOVE_M=10;

function initMap(){
  map=L.map('map',{zoomControl:false}).setView([9.05,125.98],13);
  // Esri satellite with maxNativeZoom cap to avoid blank tiles
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
    attribution:'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
    maxZoom:20,
    maxNativeZoom:18
  }).addTo(map);
  // Labels overlay so streets/places are visible on satellite
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png',{
    attribution:'',maxZoom:20,maxNativeZoom:18,opacity:0.9
  }).addTo(map);
  L.control.zoom({position:'bottomright'}).addTo(map);
}

function mkIcon(color='#0ea5e9',label='',pulse=false){
  const ring=pulse?`<circle cx="20" cy="20" r="16" fill="none" stroke="${color}" stroke-width="2.5" opacity="0"><animate attributeName="r" from="12" to="22" dur="1.8s" repeatCount="indefinite"/><animate attributeName="opacity" from="0.7" to="0" dur="1.8s" repeatCount="indefinite"/></circle>`:'';
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="40" height="50" viewBox="0 0 40 50">
    ${ring}
    <filter id="ds"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="rgba(0,0,0,.28)"/></filter>
    <path d="M20 0C9 0 0 9 0 20c0 15 20 30 20 30S40 35 40 20C40 9 31 0 20 0z" fill="${color}" filter="url(#ds)"/>
    <circle cx="20" cy="20" r="9" fill="white" opacity=".95"/>
    <text x="20" y="24" text-anchor="middle" fill="${color}" font-size="9" font-family="Inter,sans-serif" font-weight="800">${label.slice(0,2).toUpperCase()}</text>
  </svg>`;
  return L.divIcon({html:svg,iconSize:[40,50],iconAnchor:[20,50],popupAnchor:[0,-50],className:''});
}

function hav(lat1,lon1,lat2,lon2){
  const R=6371000,toR=Math.PI/180;
  const dLat=(lat2-lat1)*toR,dLon=(lon2-lon1)*toR;
  const a=Math.sin(dLat/2)**2+Math.cos(lat1*toR)*Math.cos(lat2*toR)*Math.sin(dLon/2)**2;
  return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
}
function fmtDist(m){return m<1000?m.toFixed(0)+' m':(m/1000).toFixed(2)+' km';}

function setGpsStatus(state,msg=''){
  const pill=document.getElementById('gps-pill');if(!pill)return;
  pill.className='gps-pill '+state;
  const labels={active:'LIVE',searching:'Searching…',error:'GPS Off'};
  pill.innerHTML=`<span class="gps-dot"></span>${labels[state]||state}${msg?' · '+msg:''}`;
}

async function pushLocation(lat,lon,label=null){
  try{
    await Auth.req('POST','/api/checkins/',{latitude:lat,longitude:lon,label});
    lastSentLat=lat;lastSentLon=lon;
    // Persist last known location so it survives page reload
    localStorage.setItem('lt_last_loc',JSON.stringify({lat,lon,ts:Date.now()}));
  }
  catch(e){console.warn('Push failed:',e.message);}
}

function startTracking(){
  if(!navigator.geolocation){toast('Geolocation not supported','e');return;}
  setGpsStatus('searching');
  watchId=navigator.geolocation.watchPosition(
    async pos=>{
      const{latitude:lat,longitude:lon}=pos.coords;
      const wasFirst=lastSentLat===null;
      myLat=lat;myLon=lon;trackingActive=true;
      setGpsStatus('active');
      updateMyMarker(lat,lon);
      const moved=wasFirst||hav(lastSentLat,lastSentLon,lat,lon)>=MIN_MOVE_M;
      if(moved){
        await pushLocation(lat,lon);
        // After first fix or significant move, refresh the live map
        await loadLive();
      }
    },
    err=>{setGpsStatus('error',err.code===1?'Denied':'Unavailable');toast('GPS: '+err.message,'e');},
    {enableHighAccuracy:true,maximumAge:3000,timeout:15000}
  );
}



function updateMyMarker(lat,lon){
  const me=Auth.user();if(!me)return;
  const myId=parseInt(me.id);
  myLat=lat;myLon=lon;
  if(markers[myId])map.removeLayer(markers[myId]);
  markers[myId]=L.marker([lat,lon],{icon:mkIcon('#10b981',me.username,true)}).addTo(map)
    .bindPopup(`<div style="padding:4px 2px">
      <div style="font-weight:700;font-size:.95rem;margin-bottom:4px">📍 ${me.username} <span style="color:#10b981;font-size:.75rem">(You)</span></div>
      <div style="font-size:.72rem;color:#64748b">${lat.toFixed(6)}, ${lon.toFixed(6)}</div>
      <div style="margin-top:6px;font-size:.72rem;color:#10b981;font-weight:600">● Sharing location</div>
    </div>`);
}

async function loadLive(){
  try{
    const cs=await Auth.req('GET','/api/checkins/live');
    const me=Auth.user();
    const myId=me?parseInt(me.id):null;
    // Remove all non-self markers (self marker managed by updateMyMarker via GPS watch)
    Object.entries(markers).forEach(([uid,m])=>{if(parseInt(uid)!==myId){map.removeLayer(m);delete markers[parseInt(uid)];}});
    cs.forEach(c=>{
      const isMe=parseInt(c.user_id)===myId;
      const time=new Date(c.checked_at+'Z').toLocaleString();
      const distTxt=(!isMe&&myLat!==null)?fmtDist(hav(myLat,myLon,c.latitude,c.longitude)):'';
      // Green pulsing = you, Orange = others
      const color=isMe?'#10b981':'#f97316';
      const uid=parseInt(c.user_id);
      if(markers[uid])map.removeLayer(markers[uid]);
      const m=L.marker([c.latitude,c.longitude],{icon:mkIcon(color,c.username,isMe)}).addTo(map)
        .bindPopup(`<div style="padding:4px 2px;min-width:170px">
          <div style="font-weight:700;font-size:.95rem;margin-bottom:4px">${c.username}${isMe?' <span style="color:#10b981;font-size:.72rem">(You)</span>':''}</div>
          ${c.label?`<div style="font-size:.78rem;color:#64748b;margin-bottom:4px">📍 ${c.label}</div>`:''}
          <div style="font-size:.7rem;color:#94a3b8;margin-bottom:6px">${time}</div>
          ${!isMe&&distTxt?`<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:5px 8px;margin-bottom:8px;text-align:center">
            <span style="font-size:.7rem;color:#c2410c">Distance from you</span><br>
            <span style="font-size:1.1rem;font-weight:800;color:#f97316">${distTxt}</span>
          </div>`:''}
          ${!isMe?`<button onclick="pickDist(${c.latitude},${c.longitude},'${c.username}')"
            style="width:100%;padding:5px;background:#f97316;color:#fff;border:none;border-radius:7px;font-size:.75rem;font-weight:600;cursor:pointer">
            📏 Measure Distance</button>`:''}
        </div>`);
      markers[uid]=m;
    });
    updateUserList(cs);
  }catch(e){toast('Refresh failed: '+e.message,'e');}
}

async function pickDist(lat,lon,name){
  if(myLat===null){toast('Share your location first!','e');return;}
  await calcDist(myLat,myLon,lat,lon,'You',name);
}
async function calcDist(lat1,lon1,lat2,lon2,nA='A',nB='B'){
  try{
    const r=await Auth.req('POST','/api/checkins/distance',{lat1,lon1,lat2,lon2});
    if(distLine)map.removeLayer(distLine);
    distLine=L.polyline([[lat1,lon1],[lat2,lon2]],{color:'#f59e0b',weight:3,dashArray:'10,6',opacity:.9}).addTo(map);
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
  const el=document.getElementById('user-list');if(!el)return;
  const me=Auth.user();
  const myId2=me?parseInt(me.id):null;
  if(!cs.length){el.innerHTML='<div style="padding:1rem;color:#94a3b8;font-size:.82rem;text-align:center">📡 Waiting for users to share location…</div>';return;}
  el.innerHTML=cs.map(c=>{
    const isMe=parseInt(c.user_id)===myId2;
    const distTxt=!isMe&&myLat!==null?fmtDist(hav(myLat,myLon,c.latitude,c.longitude)):'';
    const dotColor=isMe?'#10b981':'#f97316';
    return `<div onclick="flyTo(${c.user_id})" style="padding:.75rem 1rem;border-bottom:1px solid var(--bd);cursor:pointer;transition:background .15s;-webkit-tap-highlight-color:transparent" onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background=''">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px">
        <span style="font-weight:700;font-size:.88rem;color:var(--tx)">
          <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${dotColor};margin-right:5px;vertical-align:middle"></span>
          ${c.username}${isMe?' <span style="color:#10b981;font-size:.7rem">(You)</span>':''}
        </span>
        <span style="font-size:.68rem;color:var(--dim2)">${new Date(c.checked_at+'Z').toLocaleTimeString()}</span>
      </div>
      ${c.label?`<div style="font-size:.72rem;color:var(--dim);margin-top:1px">📍 ${c.label}</div>`:''}
      ${distTxt?`<div style="font-size:.74rem;color:#f97316;font-weight:600;margin-top:3px">📏 ${distTxt} away</div>`:''}
    </div>`;
  }).join('');
}
function flyTo(uid){const m=markers[parseInt(uid)];if(m){map.flyTo(m.getLatLng(),16,{animate:true,duration:1});m.openPopup();}}

async function loadHistory(){
  const el=document.getElementById('hist-list');if(!el)return;
  try{
    const h=await Auth.req('GET','/api/checkins/history');
    el.innerHTML=h.length?h.map(c=>`
      <div style="padding:.55rem .9rem;border-bottom:1px solid var(--bd);font-size:.77rem">
        <div style="display:flex;justify-content:space-between;gap:.5rem;flex-wrap:wrap">
          <span style="color:#0ea5e9;font-weight:600">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</span>
          <span style="color:var(--dim2)">${new Date(c.checked_at+'Z').toLocaleString()}</span>
        </div>
        ${c.label?`<div style="color:var(--dim);margin-top:2px">📍 ${c.label}</div>`:''}
      </div>`).join(''):'<div style="padding:.9rem;color:var(--dim);font-size:.82rem">No check-ins yet.</div>';
  }catch(e){console.warn('History failed');}
}
"""

JS_ADMIN = """
async function loadUsers(){
  const tb=document.getElementById('users-tb');if(!tb)return;
  tb.innerHTML='<tr><td colspan="5" style="padding:1.2rem;color:#94a3b8">Loading…</td></tr>';
  try{
    const us=await Auth.req('GET','/api/admin/users');
    tb.innerHTML=us.map(u=>`<tr>
      <td>${u.id}</td><td><strong>${u.username}</strong></td>
      <td><span class="badge ${u.is_admin?'ba':'bu'}">${u.is_admin?'Admin':'User'}</span></td>
      <td>${new Date(u.created_at+'Z').toLocaleString()}</td>
      <td><button class="btn bd2" onclick="delUser(${u.id},'${u.username}')" style="padding:.25rem .6rem;font-size:.72rem">Delete</button></td>
    </tr>`).join('');
    const el=document.getElementById('tot-users');if(el)el.textContent=us.length;
  }catch(e){tb.innerHTML=`<tr><td colspan="5" style="color:#dc2626">${e.message}</td></tr>`;}
}
async function loadCheckins(){
  const tb=document.getElementById('cins-tb');if(!tb)return;
  tb.innerHTML='<tr><td colspan="6" style="padding:1.2rem;color:#94a3b8">Loading…</td></tr>';
  try{
    const cs=await Auth.req('GET','/api/admin/checkins');
    tb.innerHTML=cs.map(c=>`<tr>
      <td>${c.id}</td><td><strong>${c.username}</strong></td>
      <td style="font-size:.72rem">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</td>
      <td style="color:#64748b">${c.label||'—'}</td>
      <td style="font-size:.72rem">${new Date(c.checked_at+'Z').toLocaleString()}</td>
      <td><button class="btn bd2" onclick="delCin(${c.id})" style="padding:.25rem .6rem;font-size:.72rem">Delete</button></td>
    </tr>`).join('');
    const el=document.getElementById('tot-cins');if(el)el.textContent=cs.length;
  }catch(e){tb.innerHTML=`<tr><td colspan="6" style="color:#dc2626">${e.message}</td></tr>`;}
}
async function delUser(id,name){
  if(!confirm(`Delete "${name}" and all their check-ins?`))return;
  try{await Auth.req('DELETE',`/api/admin/users/${id}`);toast(`"${name}" deleted`,'s');loadUsers();loadCheckins();}
  catch(e){toast('Failed: '+e.message,'e');}
}
async function delCin(id){
  if(!confirm('Delete this check-in?'))return;
  try{await Auth.req('DELETE',`/api/admin/checkins/${id}`);toast('Deleted','s');loadCheckins();}
  catch(e){toast('Failed: '+e.message,'e');}
}
function switchTab(tab,btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.tab-pnl').forEach(p=>p.style.display='none');
  document.getElementById('tp-'+tab).style.display='block';
  btn.classList.add('on');
  if(tab==='users')loadUsers();else loadCheckins();
}
"""

# ─── LOGIN PAGE ───────────────────────────────────
PAGE_INDEX = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<meta name="theme-color" content="#0ea5e9"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="default"/>
<title>LocationTrack</title>
<style>{CSS}
body{{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}}
.wrap{{width:100%;max-width:400px}}
.hero{{text-align:center;margin-bottom:2rem;padding-top:1rem}}
.hero-icon{{font-size:3rem;margin-bottom:.5rem;display:block}}
.hero-title{{font-size:1.8rem;font-weight:800;color:var(--tx);margin-bottom:.25rem}}
.hero-title span{{color:var(--ac)}}
.hero-sub{{font-size:.82rem;color:var(--dim);margin-top:.25rem}}
.tabs{{display:flex;background:var(--sf2);border:1.5px solid var(--bd);border-radius:12px;padding:3px;margin-bottom:1rem;gap:3px}}
.tb{{flex:1;padding:.55rem;background:transparent;border:none;color:var(--dim);font-family:'Inter',sans-serif;font-size:.84rem;font-weight:600;cursor:pointer;transition:all .2s;border-radius:9px;-webkit-tap-highlight-color:transparent}}
.tb.on{{background:#fff;color:var(--ac);box-shadow:var(--shadow)}}
.pnl{{display:none}}.pnl.on{{display:block}}
.fg{{display:flex;flex-direction:column;gap:.85rem}}
.adm-t{{margin-top:.25rem;padding:.65rem .9rem;border-radius:10px;background:#faf5ff;border:1.5px solid #e9d5ff;font-size:.78rem;color:#7c3aed}}
.adm-t summary{{cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:.4rem}}
.adm-t .fi{{margin-top:.5rem}}
.bg-art{{position:fixed;inset:0;z-index:-1;background:linear-gradient(135deg,#f0f9ff 0%,#e0f2fe 50%,#f0fdf4 100%);pointer-events:none}}
.bg-circle{{position:fixed;border-radius:50%;pointer-events:none;z-index:-1}}
</style></head><body>
<div class="bg-art"></div>
<div class="bg-circle" style="width:400px;height:400px;background:radial-gradient(circle,rgba(14,165,233,.08),transparent 70%);top:-100px;right:-100px"></div>
<div class="bg-circle" style="width:300px;height:300px;background:radial-gradient(circle,rgba(16,185,129,.06),transparent 70%);bottom:-60px;left:-60px"></div>
<div class="wrap">
  <div class="hero">
    <span class="hero-icon">📍</span>
    <div class="hero-title">Location<span>Track</span></div>
    <div class="hero-sub">Real-time GPS tracking & sharing</div>
  </div>
  <div class="tabs">
    <button class="tb on" onclick="sw('login',this)">Sign In</button>
    <button class="tb" onclick="sw('reg',this)">Register</button>
  </div>
  <div id="pnl-login" class="pnl on">
    <div class="card"><div class="fg">
      <div><label class="fl">Username</label><input id="lu" class="fi" type="text" placeholder="your_username" autocomplete="username" autocapitalize="none"/></div>
      <div><label class="fl">Password</label><input id="lp" class="fi" type="password" placeholder="••••••••" autocomplete="current-password"/></div>
      <button class="btn bp" style="width:100%;padding:.7rem" onclick="doLogin()">Sign In →</button>
      <div id="l-al" class="al al-e"></div>
    </div></div>
  </div>
  <div id="pnl-reg" class="pnl">
    <div class="card"><div class="fg">
      <div><label class="fl">Username</label><input id="ru" class="fi" type="text" placeholder="choose_a_name" autocomplete="username" autocapitalize="none"/></div>
      <div><label class="fl">Password</label><input id="rp" class="fi" type="password" placeholder="••••••••" autocomplete="new-password"/></div>
      <details class="adm-t"><summary>🔐 Admin Code (optional)</summary><input id="ra" class="fi" type="password" placeholder="Enter admin code"/></details>
      <button class="btn bs" style="width:100%;padding:.7rem" onclick="doReg()">Create Account →</button>
      <div id="r-al" class="al al-e"></div>
    </div></div>
  </div>
</div>
<script>{JS_AUTH}
if(Auth.token()) location.href='/map';
function sw(n,btn){{
  document.querySelectorAll('.tb').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.pnl').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on');document.getElementById('pnl-'+n).classList.add('on');
}}
async function doLogin(){{
  const u=document.getElementById('lu').value.trim(),p=document.getElementById('lp').value,al=document.getElementById('l-al');
  al.classList.remove('show');
  if(!u||!p){{al.textContent='Please fill in all fields';al.classList.add('show');return;}}
  try{{const d=await Auth.req('POST','/api/auth/login',{{username:u,password:p}});Auth.save(d);location.href='/map';}}
  catch(e){{al.textContent=e.message;al.classList.add('show');}}
}}
async function doReg(){{
  const u=document.getElementById('ru').value.trim(),p=document.getElementById('rp').value,
        a=document.getElementById('ra').value,al=document.getElementById('r-al');
  al.classList.remove('show');
  if(!u||!p){{al.textContent='Please fill in all fields';al.classList.add('show');return;}}
  try{{const d=await Auth.req('POST',`/api/auth/register?admin_code=${{encodeURIComponent(a)}}`,{{username:u,password:p}});Auth.save(d);location.href='/map';}}
  catch(e){{al.textContent=e.message;al.classList.add('show');}}
}}
document.addEventListener('keydown',e=>{{if(e.key==='Enter'){{if(document.getElementById('pnl-login').classList.contains('on'))doLogin();else doReg();}}}});
</script></body></html>"""

# ─── MAP PAGE ────────────────────────────────────
PAGE_MAP = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<meta name="theme-color" content="#0ea5e9"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="default"/>
<title>LocationTrack — Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>{CSS}
body{{overflow:hidden;position:fixed;width:100%;height:100%}}
.page{{padding-top:56px;height:100%;display:flex;flex-direction:column}}

/* MAP fills screen */
.map-w{{flex:1;position:relative;z-index:1}}
#map{{width:100%;height:100%}}

/* BOTTOM SHEET — mobile drawer */
.sheet{{
  position:fixed;bottom:0;left:0;right:0;z-index:500;
  background:var(--sf);border-top:1.5px solid var(--bd);
  border-radius:20px 20px 0 0;
  box-shadow:0 -4px 24px rgba(0,0,0,.12);
  transition:transform .3s cubic-bezier(.32,.72,0,1);
  max-height:80vh;overflow:hidden;
  display:flex;flex-direction:column;
}}
.sheet-handle{{width:36px;height:4px;background:var(--bd);border-radius:2px;margin:.6rem auto .2rem;flex-shrink:0;cursor:pointer}}
.sheet-tabs{{display:flex;border-bottom:1px solid var(--bd);flex-shrink:0}}
.stab{{flex:1;padding:.6rem;background:transparent;border:none;font-family:'Inter',sans-serif;font-size:.78rem;font-weight:600;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;-webkit-tap-highlight-color:transparent}}
.stab.on{{color:var(--ac);border-bottom-color:var(--ac)}}
.sheet-body{{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}}
.sheet-pnl{{display:none;padding:.75rem}}.sheet-pnl.on{{display:block}}

/* FAB controls on map */
.fab-group{{position:absolute;top:.75rem;left:.75rem;z-index:400;display:flex;flex-direction:column;gap:.5rem}}
.fab{{display:flex;align-items:center;gap:.5rem;padding:.55rem .9rem;border-radius:24px;border:none;font-family:'Inter',sans-serif;font-size:.8rem;font-weight:700;cursor:pointer;box-shadow:var(--shadow-lg);-webkit-tap-highlight-color:transparent;white-space:nowrap;transition:all .2s}}
.fab-live{{background:#fff;color:var(--ok)}}
.fab-stop{{background:var(--err);color:#fff}}
.fab-start{{background:var(--ac);color:#fff}}
.fab-center{{background:#fff;color:var(--tx)}}

/* Distance panel floating */
.dist-float{{
  position:absolute;top:.75rem;right:.75rem;z-index:400;
  background:#fff;border:1.5px solid var(--bd);border-radius:14px;
  padding:.75rem 1rem;box-shadow:var(--shadow-lg);
  display:none;min-width:170px;
}}
.dist-float .dp-t{{font-size:.65rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin-bottom:.3rem}}
.dist-float .dp-r{{font-size:.75rem;color:var(--dim);margin-bottom:.3rem}}
#dist-km{{font-size:1.6rem;font-weight:800;color:var(--warn)}}
.dp-x{{background:none;border:none;color:var(--dim2);cursor:pointer;font-size:.72rem;margin-top:.3rem;float:right}}

/* User card in list */
.user-card{{padding:.75rem;border-bottom:1px solid var(--bd);cursor:pointer;-webkit-tap-highlight-color:transparent;transition:background .15s}}
.user-card:active{{background:var(--sf2)}}

/* History item */
.hist-item{{padding:.6rem .75rem;border-bottom:1px solid var(--bd);font-size:.78rem}}

/* Tracking section in sheet */
.track-section{{padding:.75rem;border-bottom:1px solid var(--bd)}}
.track-row{{display:flex;align-items:center;gap:.6rem;margin-bottom:.6rem}}

/* Manual distance inputs */
.mdist-grid{{display:grid;grid-template-columns:1fr 1fr;gap:.4rem}}

@media(min-width:768px){{
  /* Desktop: sidebar layout */
  .page{{flex-direction:row}}
  .sheet{{
    position:relative;bottom:auto;left:auto;right:auto;
    width:300px;min-width:300px;border-radius:0;border-top:none;
    border-right:1.5px solid var(--bd);box-shadow:none;
    max-height:none;transform:none!important;
  }}
  .sheet-handle{{display:none}}
  .map-w{{flex:1}}
  .fab-group{{top:.75rem;left:.75rem}}
  .dist-float{{top:.75rem;right:.75rem}}
}}
</style></head><body>
<nav class="navbar">
  <a href="/map" class="brand">📍 Location<span>Track</span></a>
  <div class="nav-links">
    <a href="/map" class="on">Map</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
    <a href="#" onclick="Auth.logout()">Sign Out</a>
  </div>
  <div class="nu" id="nav-user"></div>
</nav>
<div class="page">
  <!-- SIDEBAR / BOTTOM SHEET -->
  <div class="sheet" id="sheet">
    <div class="sheet-handle" onclick="toggleSheet()"></div>
    <!-- Tracking controls -->
    <div class="track-section">
      <div class="track-row">
        <span id="gps-pill" class="gps-pill searching"><span class="gps-dot"></span>Searching…</span>
        <button class="btn bo" style="padding:.48rem .7rem;font-size:.8rem" onclick="flyToMe()">🎯 Center</button>
      </div>
    </div>
    <!-- Tabs -->
    <div class="sheet-tabs">
      <button class="stab on" onclick="switchSheetTab('users',this)">👥 Users</button>
      <button class="stab" onclick="switchSheetTab('history',this)">🕐 History</button>
      <button class="stab" onclick="switchSheetTab('dist',this)">📏 Distance</button>
    </div>
    <div class="sheet-body">
      <!-- Users -->
      <div id="sp-users" class="sheet-pnl on">
        <div id="user-list"><div style="padding:1rem;color:var(--dim);font-size:.82rem;text-align:center">Loading…</div></div>
      </div>
      <!-- History -->
      <div id="sp-history" class="sheet-pnl">
        <div id="hist-list"><div style="padding:1rem;color:var(--dim);font-size:.82rem">No check-ins yet.</div></div>
      </div>
      <!-- Distance -->
      <div id="sp-dist" class="sheet-pnl">
        <p style="font-size:.8rem;color:var(--dim);margin-bottom:.75rem;line-height:1.5">Tap <strong style="color:var(--ac)">Measure Distance</strong> on any user pin, or enter coordinates manually.</p>
        <div class="mdist-grid">
          <input id="m1" class="fi" type="number" step="any" placeholder="Lat 1" inputmode="decimal"/>
          <input id="m2" class="fi" type="number" step="any" placeholder="Lon 1" inputmode="decimal"/>
          <input id="m3" class="fi" type="number" step="any" placeholder="Lat 2" inputmode="decimal"/>
          <input id="m4" class="fi" type="number" step="any" placeholder="Lon 2" inputmode="decimal"/>
        </div>
        <button class="btn bp" onclick="manDist()" style="width:100%;margin-top:.6rem">Calculate</button>
      </div>
    </div>
  </div>

  <!-- MAP -->
  <div class="map-w">
    <div id="map"></div>
    <!-- FAB buttons on map -->
    <div class="fab-group">
      <button class="fab fab-center" onclick="flyToMe()">🎯 My Location</button>
    </div>
    <!-- Distance result floating -->
    <div class="dist-float" id="dist-panel">
      <div class="dp-t">📏 Distance</div>
      <div class="dp-r"><span id="dist-from">A</span> → <span id="dist-to">B</span></div>
      <div id="dist-km">—</div>
      <button class="dp-x" onclick="closeDist()">✕ Clear</button>
    </div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>{JS_AUTH}{JS_MAP}
requireAuth();initNav();initMap();loadLive();loadHistory();

// Restore last known position AND push to server so we appear for others immediately
(async function restoreLastLocation(){{
  try{{
    const saved=JSON.parse(localStorage.getItem('lt_last_loc')||'null');
    if(saved&&saved.lat&&saved.lon){{
      myLat=saved.lat;myLon=saved.lon;
      updateMyMarker(saved.lat,saved.lon);
      map.setView([saved.lat,saved.lon],15);
      setGpsStatus('searching');
      // Push saved location to server so other users can see us right away
      await pushLocation(saved.lat,saved.lon);
      // Reload the live map so everyone (including self) appears
      await loadLive();
    }}
  }}catch(e){{}}
}})();

// Auto-start tracking on load
startTracking();

setInterval(loadLive,10000);

// Sheet toggle for mobile
let sheetOpen=true;
function toggleSheet(){{
  sheetOpen=!sheetOpen;
  document.getElementById('sheet').style.transform=sheetOpen?'':'translateY(calc(100% - 120px))';
}}
// On mobile collapse sheet initially to show more map
if(window.innerWidth<768){{
  setTimeout(()=>{{sheetOpen=false;document.getElementById('sheet').style.transform='translateY(calc(100% - 120px))';}},800);
}}

function switchSheetTab(name,btn){{
  document.querySelectorAll('.stab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.sheet-pnl').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on');
  document.getElementById('sp-'+name).classList.add('on');
  // expand sheet on tab click (mobile)
  if(window.innerWidth<768&&!sheetOpen){{sheetOpen=true;document.getElementById('sheet').style.transform='';}}
}}

function flyToMe(){{
  if(myLat!==null)map.flyTo([myLat,myLon],17,{{animate:true,duration:1.2}});
  else toast('Location not available yet','e');
}}
function closeDist(){{
  document.getElementById('dist-panel').style.display='none';
  if(distLine){{map.removeLayer(distLine);distLine=null;}}
}}
async function manDist(){{
  const v=[...document.querySelectorAll('#m1,#m2,#m3,#m4')].map(i=>parseFloat(i.value));
  if(v.some(isNaN)){{toast('Enter valid coordinates','e');return;}}
  await calcDist(v[0],v[1],v[2],v[3],'Point A','Point B');
}}
</script></body></html>"""

# ─── ADMIN PAGE ───────────────────────────────────
PAGE_ADMIN = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="theme-color" content="#0ea5e9"/>
<title>LocationTrack — Admin</title>
<style>{CSS}
.page{{padding-top:72px;max-width:1080px;margin:0 auto;padding-left:1rem;padding-right:1rem;padding-bottom:2rem}}
.ah{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.2rem;flex-wrap:wrap;gap:.5rem}}
.at{{font-size:1.3rem;font-weight:800}}.at span{{color:var(--ac2)}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem;margin-bottom:1.2rem}}
@media(max-width:500px){{.stats{{grid-template-columns:1fr 1fr}}}}
.sc{{background:var(--sf);border:1.5px solid var(--bd);border-radius:12px;padding:1rem 1.2rem;box-shadow:var(--shadow)}}
.sl{{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:.3rem}}
.sv{{font-size:1.7rem;font-weight:800;color:var(--ac)}}
.tabs{{display:flex;background:var(--sf2);border:1.5px solid var(--bd);border-radius:10px;padding:3px;gap:3px;margin-bottom:1rem;width:fit-content}}
.tab-btn{{padding:.45rem 1.2rem;background:transparent;border:none;color:var(--dim);font-family:'Inter',sans-serif;font-size:.82rem;font-weight:600;cursor:pointer;transition:all .2s;border-radius:7px}}
.tab-btn.on{{background:#fff;color:var(--ac2);box-shadow:var(--shadow)}}
.tw{{background:var(--sf);border:1.5px solid var(--bd);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}}
.tt{{display:flex;align-items:center;justify-content:space-between;padding:.75rem 1rem;border-bottom:1.5px solid var(--bd)}}
.tt h3{{font-size:.85rem;font-weight:700}}
.tsc{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
#acc-denied{{display:none;text-align:center;padding:4rem 1rem}}
#acc-denied .ic{{font-size:3rem;margin-bottom:.75rem}}
#acc-denied h2{{color:var(--err);margin-bottom:.4rem}}
#acc-denied p{{color:var(--dim)}}
</style></head><body>
<nav class="navbar">
  <a href="/map" class="brand">📍 Location<span>Track</span></a>
  <div class="nav-links">
    <a href="/map">Map</a>
    <a href="/admin" class="on">Admin</a>
    <a href="#" onclick="Auth.logout()">Sign Out</a>
  </div>
  <div class="nu" id="nav-user"></div>
</nav>
<div class="page">
  <div id="acc-denied"><div class="ic">🔐</div><h2>Admin Only</h2><p>You don't have permission here.</p><a href="/map" class="btn bo" style="margin-top:1rem">← Back to Map</a></div>
  <div id="adm-content">
    <div class="ah">
      <div><div class="at">Admin <span>Panel</span></div><div style="font-size:.78rem;color:var(--dim);margin-top:.2rem">System management</div></div>
      <button class="btn bo" onclick="loadUsers();loadCheckins()">↻ Refresh</button>
    </div>
    <div class="stats">
      <div class="sc"><div class="sl">Total Users</div><div class="sv" id="tot-users">—</div></div>
      <div class="sc"><div class="sl">Check-ins</div><div class="sv" id="tot-cins">—</div></div>
      <div class="sc"><div class="sl">Admin</div><div class="sv" id="adm-name" style="font-size:.95rem;padding-top:.35rem">—</div></div>
    </div>
    <div class="tabs">
      <button class="tab-btn on" onclick="switchTab('users',this)">👥 Users</button>
      <button class="tab-btn" onclick="switchTab('checkins',this)">📍 Check-ins</button>
    </div>
    <div id="tp-users" class="tw tab-pnl">
      <div class="tt"><h3>All Users</h3><button class="btn bo" onclick="loadUsers()" style="padding:.25rem .6rem;font-size:.72rem">↻</button></div>
      <div class="tsc"><table class="dt"><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Registered</th><th></th></tr></thead>
      <tbody id="users-tb"></tbody></table></div>
    </div>
    <div id="tp-checkins" class="tw tab-pnl" style="display:none">
      <div class="tt"><h3>All Check-ins</h3><button class="btn bo" onclick="loadCheckins()" style="padding:.25rem .6rem;font-size:.72rem">↻</button></div>
      <div class="tsc"><table class="dt"><thead><tr><th>ID</th><th>User</th><th>Coordinates</th><th>Label</th><th>Time</th><th></th></tr></thead>
      <tbody id="cins-tb"></tbody></table></div>
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

@app.post("/api/auth/register")
async def register(body: UserCreate, admin_code: str = ""):
    return await ctrl_register(body.username, body.password, admin_code)

@app.post("/api/auth/login")
async def login(body: UserLogin):
    return await ctrl_login(body.username, body.password)

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

@app.get("/",      response_class=HTMLResponse)
async def pg_index(): return HTMLResponse(PAGE_INDEX)

@app.get("/map",   response_class=HTMLResponse)
async def pg_map():   return HTMLResponse(PAGE_MAP)

@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return HTMLResponse(PAGE_ADMIN)
