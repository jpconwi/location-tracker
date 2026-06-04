"""
controllers/checkin_controller.py — Check-in CRUD and distance math
"""
import math
from fastapi import HTTPException
from config.database import get_client


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in km between two GPS coordinates."""
    R = 6371.0  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def create_checkin(user_id: int, latitude: float, longitude: float, label: str = None):
    """Record a new check-in for a user."""
    async with await get_client() as client:
        result = await client.execute(
            "INSERT INTO checkins (user_id, latitude, longitude, label) VALUES (?, ?, ?, ?) "
            "RETURNING id, user_id, latitude, longitude, label, checked_at",
            [user_id, latitude, longitude, label]
        )
        row = result.rows[0]
        # fetch username
        u = await client.execute("SELECT username FROM users WHERE id = ?", [user_id])
        username = u.rows[0][0] if u.rows else "unknown"
        return {
            "id": row[0], "user_id": row[1], "username": username,
            "latitude": row[2], "longitude": row[3],
            "label": row[4], "checked_at": row[5]
        }


async def get_latest_checkins():
    """Get the latest check-in per user (for live map view)."""
    async with await get_client() as client:
        result = await client.execute("""
            SELECT c.id, c.user_id, u.username, c.latitude, c.longitude, c.label, c.checked_at
            FROM checkins c
            JOIN users u ON c.user_id = u.id
            WHERE c.id IN (
                SELECT MAX(id) FROM checkins GROUP BY user_id
            )
            ORDER BY c.checked_at DESC
        """)
        return [
            {"id": r[0], "user_id": r[1], "username": r[2],
             "latitude": r[3], "longitude": r[4], "label": r[5], "checked_at": r[6]}
            for r in result.rows
        ]


async def get_user_history(user_id: int, limit: int = 20):
    """Get recent check-in history for a user."""
    async with await get_client() as client:
        result = await client.execute(
            "SELECT c.id, c.user_id, u.username, c.latitude, c.longitude, c.label, c.checked_at "
            "FROM checkins c JOIN users u ON c.user_id = u.id "
            "WHERE c.user_id = ? ORDER BY c.checked_at DESC LIMIT ?",
            [user_id, limit]
        )
        return [
            {"id": r[0], "user_id": r[1], "username": r[2],
             "latitude": r[3], "longitude": r[4], "label": r[5], "checked_at": r[6]}
            for r in result.rows
        ]


async def get_all_checkins():
    """Admin: get all check-ins."""
    async with await get_client() as client:
        result = await client.execute("""
            SELECT c.id, c.user_id, u.username, c.latitude, c.longitude, c.label, c.checked_at
            FROM checkins c JOIN users u ON c.user_id = u.id
            ORDER BY c.checked_at DESC
        """)
        return [
            {"id": r[0], "user_id": r[1], "username": r[2],
             "latitude": r[3], "longitude": r[4], "label": r[5], "checked_at": r[6]}
            for r in result.rows
        ]


async def delete_checkin(checkin_id: int):
    """Admin: delete a check-in."""
    async with await get_client() as client:
        await client.execute("DELETE FROM checkins WHERE id = ?", [checkin_id])
    return {"detail": "Check-in deleted"}


def calc_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> dict:
    km = haversine_km(lat1, lon1, lat2, lon2)
    return {"km": round(km, 3), "meters": round(km * 1000, 1)}
