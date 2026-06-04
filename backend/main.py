"""
main.py — FastAPI application entry point
"""
import sys
import os

# ── Fix Vercel import paths ──────────────────────────────────────────────────
# Vercel runs from /var/task (repo root), so we add backend/ to sys.path
# so that `from config.x import y` works correctly.
_backend_dir = os.path.dirname(os.path.abspath(__file__))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)
# ─────────────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from config.database import init_db
from routes import auth_router, checkin_router, admin_router
from views.page_view import render


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Location Tracker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files — path relative to THIS file (backend/main.py)
_root = os.path.dirname(_backend_dir)          # repo root
_static_dir = os.path.join(_root, "frontend", "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# API routers
app.include_router(auth_router)
app.include_router(checkin_router)
app.include_router(admin_router)


# Page routes (View layer)
@app.get("/", response_class=HTMLResponse)
async def index():
    return render("index.html")


@app.get("/map", response_class=HTMLResponse)
async def map_page():
    return render("map.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return render("admin.html")
