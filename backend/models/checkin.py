"""
models/checkin.py — Pydantic schemas for Check-ins
"""
from pydantic import BaseModel
from typing import Optional


class CheckinCreate(BaseModel):
    latitude: float
    longitude: float
    label: Optional[str] = None


class CheckinOut(BaseModel):
    id: int
    user_id: int
    username: str
    latitude: float
    longitude: float
    label: Optional[str]
    checked_at: str


class DistanceRequest(BaseModel):
    lat1: float
    lon1: float
    lat2: float
    lon2: float
