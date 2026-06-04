"""
api/index.py — Complete Location Tracker backend (flat, Vercel-compatible)
MVC logic is organised into clear sections within this file.
"""
# ═══════════════════════════════════════════════════════════════
#  IMPORTS
# ═══════════════════════════════════════════════════════════════
import os, sys, math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from passlib.context import CryptContext
import libsql_client

# ═══════════════════════════════════════════════════════════════
#  CONFIG  (settings.py equivalent)
# ═══════════════════════════════════════════════════════════════
TURSO_DATABASE_URL: str = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN:   str = os.getenv("TURSO_AUTH_TOKEN", "")
SECRET_KEY:         str = os.getenv("SECRET_KEY", "change-me-in-production")
ADMIN_PASSWORD:     str = os.getenv("ADMIN_PASSWORD", "admin123")
ALGORITHM               = "HS256"
TOKEN_EXPIRE_MINUTES    = 60 * 24 * 7   # 7 days

# ═══════════════════════════════════════════════════════════════
#  DATABASE  (database.py equivalent)
# ═══════════════════════════════════════════════════════════════
def _db():
    return libsql_client.create_client(
        url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN,
    )

async def init_db():
    async with _db() as client:
        await client.batch([
            """CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    UNIQUE NOT NULL,
                password   TEXT    NOT NULL,
                is_admin   INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS checkins (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                latitude   REAL    NOT NULL,
                longitude  REAL    NOT NULL,
                label      TEXT,
                checked_at TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )""",
        ])

# ═══════════════════════════════════════════════════════════════
#  MODELS  (Pydantic schemas)
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
#  AUTH CONTROLLER
# ═══════════════════════════════════════════════════════════════
pwd_ctx      = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

def hash_pw(pw: str) -> str:          return pwd_ctx.hash(pw)
def verify_pw(plain, hashed) -> bool: return pwd_ctx.verify(plain, hashed)

def make_token(user_id: int) -> str:
    exp = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

async def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid token")
    async with _db() as db:
        r = await db.execute("SELECT id,username,is_admin,created_at FROM users WHERE id=?", [uid])
        if not r.rows: raise HTTPException(401, "User not found")
        row = r.rows[0]
    return {"id": row[0], "username": row[1], "is_admin": bool(row[2]), "created_at": row[3]}

async def admin_user(user=Depends(current_user)):
    if not user["is_admin"]: raise HTTPException(403, "Admin only")
    return user

# ═══════════════════════════════════════════════════════════════
#  USER CONTROLLER
# ═══════════════════════════════════════════════════════════════
async def ctrl_register(username: str, password: str, admin_code: str = ""):
    async with _db() as db:
        ex = await db.execute("SELECT id FROM users WHERE username=?", [username])
        if ex.rows: raise HTTPException(400, "Username already taken")
        is_admin = 1 if admin_code == ADMIN_PASSWORD else 0
        r = await db.execute(
            "INSERT INTO users (username,password,is_admin) VALUES (?,?,?) "
            "RETURNING id,username,is_admin,created_at",
            [username, hash_pw(password), is_admin]
        )
        row = r.rows[0]
    user = {"id": row[0], "username": row[1], "is_admin": bool(row[2]), "created_at": row[3]}
    return {"access_token": make_token(user["id"]), "token_type": "bearer", "user": user}

async def ctrl_login(username: str, password: str):
    async with _db() as db:
        r = await db.execute(
            "SELECT id,username,password,is_admin,created_at FROM users WHERE username=?", [username])
        if not r.rows: raise HTTPException(401, "Invalid credentials")
        row = r.rows[0]
        if not verify_pw(password, row[2]): raise HTTPException(401, "Invalid credentials")
    user = {"id": row[0], "username": row[1], "is_admin": bool(row[3]), "created_at": row[4]}
    return {"access_token": make_token(user["id"]), "token_type": "bearer", "user": user}

async def ctrl_list_users():
    async with _db() as db:
        r = await db.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY created_at DESC")
        return [{"id": x[0],"username": x[1],"is_admin": bool(x[2]),"created_at": x[3]} for x in r.rows]

async def ctrl_delete_user(uid: int):
    async with _db() as db:
        await db.batch([
            ("DELETE FROM checkins WHERE user_id=?", [uid]),
            ("DELETE FROM users WHERE id=?",         [uid]),
        ])
    return {"detail": "Deleted"}

# ═══════════════════════════════════════════════════════════════
#  CHECKIN CONTROLLER
# ═══════════════════════════════════════════════════════════════
def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1,p2 = math.radians(lat1), math.radians(lat2)
    dp,dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

async def ctrl_checkin(user_id: int, lat: float, lon: float, label: str = None):
    async with _db() as db:
        r = await db.execute(
            "INSERT INTO checkins (user_id,latitude,longitude,label) VALUES (?,?,?,?) "
            "RETURNING id,user_id,latitude,longitude,label,checked_at",
            [user_id, lat, lon, label]
        )
        row = r.rows[0]
        u = await db.execute("SELECT username FROM users WHERE id=?", [user_id])
        uname = u.rows[0][0] if u.rows else "unknown"
    return {"id":row[0],"user_id":row[1],"username":uname,
            "latitude":row[2],"longitude":row[3],"label":row[4],"checked_at":row[5]}

async def ctrl_live():
    async with _db() as db:
        r = await db.execute("""
            SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at
            FROM checkins c JOIN users u ON c.user_id=u.id
            WHERE c.id IN (SELECT MAX(id) FROM checkins GROUP BY user_id)
            ORDER BY c.checked_at DESC""")
        return [{"id":x[0],"user_id":x[1],"username":x[2],
                 "latitude":x[3],"longitude":x[4],"label":x[5],"checked_at":x[6]} for x in r.rows]

async def ctrl_history(user_id: int, limit: int = 20):
    async with _db() as db:
        r = await db.execute(
            "SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
            "FROM checkins c JOIN users u ON c.user_id=u.id "
            "WHERE c.user_id=? ORDER BY c.checked_at DESC LIMIT ?", [user_id, limit])
        return [{"id":x[0],"user_id":x[1],"username":x[2],
                 "latitude":x[3],"longitude":x[4],"label":x[5],"checked_at":x[6]} for x in r.rows]

async def ctrl_all_checkins():
    async with _db() as db:
        r = await db.execute(
            "SELECT c.id,c.user_id,u.username,c.latitude,c.longitude,c.label,c.checked_at "
            "FROM checkins c JOIN users u ON c.user_id=u.id ORDER BY c.checked_at DESC")
        return [{"id":x[0],"user_id":x[1],"username":x[2],
                 "latitude":x[3],"longitude":x[4],"label":x[5],"checked_at":x[6]} for x in r.rows]

async def ctrl_delete_checkin(cid: int):
    async with _db() as db:
        await db.execute("DELETE FROM checkins WHERE id=?", [cid])
    return {"detail": "Deleted"}

# ═══════════════════════════════════════════════════════════════
#  VIEW (HTML template reader)
# ═══════════════════════════════════════════════════════════════
_HERE = os.path.dirname(os.path.abspath(__file__))          # api/
_ROOT = os.path.dirname(_HERE)                               # repo root

def render_html(name: str) -> HTMLResponse:
    path = os.path.join(_ROOT, "frontend", "templates", name)
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())

# ═══════════════════════════════════════════════════════════════
#  FASTAPI APP
# ═══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="LocationTracker", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"])

# Static files
_static = os.path.join(_ROOT, "frontend", "static")
if os.path.isdir(_static):
    app.mount("/static", StaticFiles(directory=_static), name="static")

# ── AUTH ROUTES ──────────────────────────────────────────────
@app.post("/api/auth/register")
async def register(body: UserCreate, admin_code: str = ""):
    return await ctrl_register(body.username, body.password, admin_code)

@app.post("/api/auth/login")
async def login(body: UserLogin):
    return await ctrl_login(body.username, body.password)

# ── CHECKIN ROUTES ───────────────────────────────────────────
@app.post("/api/checkins/")
async def checkin(body: CheckinCreate, user=Depends(current_user)):
    return await ctrl_checkin(user["id"], body.latitude, body.longitude, body.label)

@app.get("/api/checkins/live")
async def live(_user=Depends(current_user)):
    return await ctrl_live()

@app.get("/api/checkins/history")
async def history(user=Depends(current_user)):
    return await ctrl_history(user["id"])

@app.post("/api/checkins/distance")
async def distance(body: DistanceRequest, _user=Depends(current_user)):
    km = haversine(body.lat1, body.lon1, body.lat2, body.lon2)
    return {"km": round(km, 3), "meters": round(km * 1000, 1)}

# ── ADMIN ROUTES ─────────────────────────────────────────────
@app.get("/api/admin/users")
async def admin_users(_admin=Depends(admin_user)):
    return await ctrl_list_users()

@app.delete("/api/admin/users/{uid}")
async def admin_del_user(uid: int, _admin=Depends(admin_user)):
    return await ctrl_delete_user(uid)

@app.get("/api/admin/checkins")
async def admin_checkins(_admin=Depends(admin_user)):
    return await ctrl_all_checkins()

@app.delete("/api/admin/checkins/{cid}")
async def admin_del_checkin(cid: int, _admin=Depends(admin_user)):
    return await ctrl_delete_checkin(cid)

# ── PAGE ROUTES (View) ────────────────────────────────────────
@app.get("/",      response_class=HTMLResponse)
async def page_index(): return render_html("index.html")

@app.get("/map",   response_class=HTMLResponse)
async def page_map():   return render_html("map.html")

@app.get("/admin", response_class=HTMLResponse)
async def page_admin(): return render_html("admin.html")
