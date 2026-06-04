# 📍 LocationTrack v3

Real-time location tracking with Leaflet maps, user check-ins, distance calculator, and admin panel.

## Project Structure
```
location-tracker/
├── api/
│   └── index.py          ← Entire backend (FastAPI, MVC-organised)
├── frontend/
│   ├── templates/        ← HTML pages (index, map, admin)
│   └── static/
│       ├── css/style.css
│       └── js/           ← auth.js, map.js, admin.js
├── vercel.json
├── requirements.txt
└── .env.example
```

## Setup

### 1. Turso DB
```bash
curl -sSfL https://get.tur.so/install.sh | bash
turso db create location-tracker
turso db show location-tracker          # copy URL
turso db tokens create location-tracker # copy token
```

### 2. Environment Variables
```
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your-token
SECRET_KEY=any-random-secret-string
ADMIN_PASSWORD=your-admin-code
```

### 3. Run Locally
```bash
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

### 4. Deploy to Vercel
1. Push to GitHub
2. Import at vercel.com → New Project
3. Add the 4 env vars in Vercel dashboard
4. Deploy ✅
