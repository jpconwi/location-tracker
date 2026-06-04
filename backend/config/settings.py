"""
config/settings.py — App-wide settings from environment
"""
import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")
