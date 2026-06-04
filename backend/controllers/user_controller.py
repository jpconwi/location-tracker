"""
controllers/user_controller.py — User registration, login, listing
"""
from fastapi import HTTPException
from config.database import get_client
from config.settings import ADMIN_PASSWORD
from controllers.auth_controller import hash_password, verify_password, create_access_token


async def register_user(username: str, password: str, admin_code: str = ""):
    """Register a new user. Pass ADMIN_PASSWORD to become admin."""
    async with await get_client() as client:
        # Check existing
        existing = await client.execute("SELECT id FROM users WHERE username = ?", [username])
        if existing.rows:
            raise HTTPException(status_code=400, detail="Username already taken")

        is_admin = 1 if admin_code == ADMIN_PASSWORD else 0
        hashed = hash_password(password)
        result = await client.execute(
            "INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?) RETURNING id, username, is_admin, created_at",
            [username, hashed, is_admin]
        )
        row = result.rows[0]
        user = {"id": row[0], "username": row[1], "is_admin": bool(row[2]), "created_at": row[3]}
        token = create_access_token({"sub": user["id"]})
        return {"access_token": token, "token_type": "bearer", "user": user}


async def login_user(username: str, password: str):
    """Authenticate a user and return JWT."""
    async with await get_client() as client:
        result = await client.execute(
            "SELECT id, username, password, is_admin, created_at FROM users WHERE username = ?",
            [username]
        )
        if not result.rows:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        row = result.rows[0]
        if not verify_password(password, row[2]):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        user = {"id": row[0], "username": row[1], "is_admin": bool(row[3]), "created_at": row[4]}
        token = create_access_token({"sub": user["id"]})
        return {"access_token": token, "token_type": "bearer", "user": user}


async def list_users():
    """Return all users (admin view)."""
    async with await get_client() as client:
        result = await client.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY created_at DESC")
        return [
            {"id": r[0], "username": r[1], "is_admin": bool(r[2]), "created_at": r[3]}
            for r in result.rows
        ]


async def delete_user(user_id: int):
    """Delete a user and their check-ins (admin)."""
    async with await get_client() as client:
        await client.batch([
            ("DELETE FROM checkins WHERE user_id = ?", [user_id]),
            ("DELETE FROM users WHERE id = ?", [user_id]),
        ])
    return {"detail": "User deleted"}
