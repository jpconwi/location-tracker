"""
routes/auth_routes.py — /api/auth endpoints
"""
from fastapi import APIRouter
from models.user import UserCreate, UserLogin
from controllers.user_controller import register_user, login_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def register(payload: UserCreate, admin_code: str = ""):
    return await register_user(payload.username, payload.password, admin_code)


@router.post("/login")
async def login(payload: UserLogin):
    return await login_user(payload.username, payload.password)
