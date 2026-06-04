"""
main.py — FastAPI application entry point
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
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

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

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
