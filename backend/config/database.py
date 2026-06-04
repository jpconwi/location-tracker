"""
config/database.py — Turso (LibSQL) connection and initialization
"""
import os
import libsql_client
from dotenv import load_dotenv

load_dotenv()

TURSO_DATABASE_URL: str = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN: str   = os.getenv("TURSO_AUTH_TOKEN", "")


def _client():
    """Create a new Turso async client."""
    return libsql_client.create_client(
        url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN,
    )


# Alias used by controllers
async def get_client():
    return _client()


async def init_db():
    """Create tables if they don't exist."""
    async with _client() as client:
        await client.batch([
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    UNIQUE NOT NULL,
                password   TEXT    NOT NULL,
                is_admin   INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS checkins (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                latitude   REAL    NOT NULL,
                longitude  REAL    NOT NULL,
                label      TEXT,
                checked_at TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """,
        ])
