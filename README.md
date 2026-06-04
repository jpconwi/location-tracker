# 📍 LocationTrack v4

Real-time location tracker — Vercel-ready, zero native dependencies.

## Why v4?
- `libsql-client` has native C deps that crash on Vercel → replaced with direct **Turso HTTP API** calls via `httpx`
- HTML pages inlined in Python → no filesystem reads on Vercel
- Single `api/index.py` entry point → no import path issues

## Files
```
├── api/index.py       ← Entire app (FastAPI + Turso HTTP client + inlined HTML)
├── requirements.txt   ← Pure Python deps only
├── vercel.json
└── .env.example
```

## Local Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
uvicorn api.index:app --reload --port 8000
```

## Turso Setup
```bash
curl -sSfL https://get.tur.so/install.sh | bash
turso db create location-tracker
turso db show location-tracker          # → copy URL
turso db tokens create location-tracker # → copy token
```

## Deploy to Vercel
1. Push to GitHub
2. Import at vercel.com
3. Add env vars in Vercel dashboard:
   - `TURSO_DATABASE_URL` = `libsql://your-db.turso.io`
   - `TURSO_AUTH_TOKEN`   = your token
   - `SECRET_KEY`         = any random string
   - `ADMIN_PASSWORD`     = your admin code
4. Deploy ✅
