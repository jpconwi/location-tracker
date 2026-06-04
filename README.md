# 📍 Location Tracker

A real-time location tracking system with Leaflet maps, user tracking, distance estimation, and admin panel.

## Stack
- **Backend**: Python (FastAPI)
- **Frontend**: HTML/CSS/JS + Leaflet.js
- **Database**: Turso (LibSQL)
- **Deploy**: Vercel (frontend) + Railway/Render (backend)

## Structure (MVC)
```
location-tracker/
├── backend/
│   ├── config/         # DB config, settings
│   ├── models/         # Database models
│   ├── controllers/    # Business logic
│   ├── routes/         # API endpoints
│   └── main.py
├── frontend/
│   ├── templates/      # HTML pages
│   └── static/
│       ├── css/
│       └── js/
├── vercel.json
└── requirements.txt
```

## Setup

### 1. Clone & Install
```bash
git clone https://github.com/YOUR_USERNAME/location-tracker
cd location-tracker
pip install -r requirements.txt
```

### 2. Turso Database
```bash
# Install Turso CLI
curl -sSfL https://get.tur.so/install.sh | bash

# Create DB
turso db create location-tracker
turso db show location-tracker  # copy URL
turso db tokens create location-tracker  # copy token
```

### 3. Environment Variables
Create `.env` file:
```
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your-token
SECRET_KEY=your-secret-key-here
ADMIN_PASSWORD=admin123
```

### 4. Run Locally
```bash
cd backend
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`

## Deploy to Vercel

1. Push to GitHub
2. Import repo on vercel.com
3. Set environment variables in Vercel dashboard
4. Deploy!

## Features
- 📍 One-click location check-in
- 🗺️ Live map with all user pins
- 📏 Distance calculator between users
- 👤 User registration & login
- 🔐 Admin panel (view/delete users, all check-ins)
- 📱 Mobile responsive
