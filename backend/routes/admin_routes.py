"""
routes/admin_routes.py — /api/admin endpoints (admin only)
"""
from fastapi import APIRouter, Depends
from controllers.user_controller import list_users, delete_user
from controllers.checkin_controller import get_all_checkins, delete_checkin
from controllers.auth_controller import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
async def all_users(admin=Depends(require_admin)):
    return await list_users()


@router.delete("/users/{user_id}")
async def remove_user(user_id: int, admin=Depends(require_admin)):
    return await delete_user(user_id)


@router.get("/checkins")
async def all_checkins(admin=Depends(require_admin)):
    return await get_all_checkins()


@router.delete("/checkins/{checkin_id}")
async def remove_checkin(checkin_id: int, admin=Depends(require_admin)):
    return await delete_checkin(checkin_id)
