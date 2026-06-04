"""
routes/checkin_routes.py — /api/checkins endpoints
"""
from fastapi import APIRouter, Depends
from models.checkin import CheckinCreate, DistanceRequest
from controllers.checkin_controller import (
    create_checkin, get_latest_checkins, get_user_history, calc_distance
)
from controllers.auth_controller import get_current_user

router = APIRouter(prefix="/api/checkins", tags=["checkins"])


@router.post("/")
async def checkin(payload: CheckinCreate, user=Depends(get_current_user)):
    return await create_checkin(user["id"], payload.latitude, payload.longitude, payload.label)


@router.get("/live")
async def live_map(user=Depends(get_current_user)):
    """Latest check-in per user — powers the live map."""
    return await get_latest_checkins()


@router.get("/history")
async def my_history(user=Depends(get_current_user)):
    return await get_user_history(user["id"])


@router.post("/distance")
async def distance(payload: DistanceRequest, user=Depends(get_current_user)):
    return calc_distance(payload.lat1, payload.lon1, payload.lat2, payload.lon2)
