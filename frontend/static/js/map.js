/* static/js/map.js — Leaflet map + check-in logic */

let map, myMarker, markers = {}, distanceLine = null;
let distancePicking = { active: false, points: [] };

function initMap() {
  map = L.map('map', { zoomControl: false }).setView([13.0, 122.0], 6);

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '© OpenStreetMap © CARTO',
    maxZoom: 19,
  }).addTo(map);

  L.control.zoom({ position: 'bottomright' }).addTo(map);
}

/* ── Custom marker icon ── */
function makeIcon(color = '#00d4ff', label = '') {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="32" height="40" viewBox="0 0 32 40">
      <path d="M16 0C7.16 0 0 7.16 0 16c0 12 16 24 16 24S32 28 32 16C32 7.16 24.84 0 16 0z"
            fill="${color}" opacity="0.9"/>
      <circle cx="16" cy="16" r="7" fill="white" opacity="0.9"/>
      <text x="16" y="20" text-anchor="middle" fill="${color}"
            font-size="8" font-family="Syne,sans-serif" font-weight="700">${label.slice(0,2).toUpperCase()}</text>
    </svg>`;
  return L.divIcon({
    html: svg,
    iconSize: [32, 40],
    iconAnchor: [16, 40],
    popupAnchor: [0, -40],
    className: ''
  });
}

/* ── Load all latest check-ins onto map ── */
async function loadLiveMap() {
  try {
    const checkins = await Auth.request('GET', '/api/checkins/live');
    const me = Auth.user();

    // Clear old markers
    Object.values(markers).forEach(m => map.removeLayer(m));
    markers = {};

    checkins.forEach(c => {
      const isMe = c.user_id === me?.id;
      const color = isMe ? '#10b981' : '#00d4ff';
      const icon = makeIcon(color, c.username);
      const time = new Date(c.checked_at + 'Z').toLocaleString();

      const marker = L.marker([c.latitude, c.longitude], { icon })
        .addTo(map)
        .bindPopup(`
          <div style="min-width:160px">
            <div style="font-weight:800;font-size:1rem;margin-bottom:4px">${c.username}</div>
            ${c.label ? `<div style="color:#94a3b8;font-size:0.8rem;margin-bottom:4px">📍 ${c.label}</div>` : ''}
            <div style="font-family:'Space Mono',monospace;font-size:0.72rem;color:#64748b">${time}</div>
            <div style="font-family:'Space Mono',monospace;font-size:0.7rem;color:#64748b;margin-top:4px">
              ${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}
            </div>
            ${!isMe ? `<button onclick="pickForDistance(${c.latitude},${c.longitude},'${c.username}')"
              style="margin-top:8px;padding:4px 10px;background:#00d4ff;color:#0a0e1a;border:none;
              border-radius:4px;font-size:0.75rem;font-weight:700;cursor:pointer">
              📏 Measure Distance
            </button>` : '<div style="color:#10b981;font-size:0.75rem;margin-top:6px">▶ YOU</div>'}
          </div>
        `);

      markers[c.user_id] = marker;
    });

    updateUserList(checkins);
  } catch (e) {
    showToast('Failed to load map: ' + e.message, 'error');
  }
}

/* ── Check In ── */
async function doCheckin() {
  const btn = document.getElementById('btn-checkin');
  const label = document.getElementById('checkin-label')?.value.trim() || null;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Locating...';

  navigator.geolocation.getCurrentPosition(async (pos) => {
    const { latitude, longitude } = pos.coords;
    try {
      const result = await Auth.request('POST', '/api/checkins/', { latitude, longitude, label });
      showToast(`✅ Checked in at ${latitude.toFixed(4)}, ${longitude.toFixed(4)}`, 'success');

      // Fly to location
      map.flyTo([latitude, longitude], 15, { animate: true, duration: 1.2 });

      // Update my marker
      if (markers[result.user_id]) map.removeLayer(markers[result.user_id]);
      const icon = makeIcon('#10b981', result.username);
      markers[result.user_id] = L.marker([latitude, longitude], { icon })
        .addTo(map)
        .bindPopup(`<div><strong>${result.username}</strong><br><span style="color:#6ee7b7">📍 Just checked in</span></div>`)
        .openPopup();

      await loadLiveMap();
    } catch (e) {
      showToast('Check-in failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.innerHTML = '📍 Check In Here';
  }, (err) => {
    showToast('Location denied: ' + err.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '📍 Check In Here';
  }, { enableHighAccuracy: true });
}

/* ── Distance Calculation ── */
let distPoint = null;

function pickForDistance(lat, lon, username) {
  const me = Auth.user();
  // find my latest checkin
  const myMarkerData = markers[me?.id];
  if (!myMarkerData) {
    showToast('Check in first to measure distance!', 'error');
    return;
  }
  const myLatLng = myMarkerData.getLatLng();
  calcAndShowDistance(myLatLng.lat, myLatLng.lng, lat, lon, 'You', username);
}

async function calcAndShowDistance(lat1, lon1, lat2, lon2, nameA = 'A', nameB = 'B') {
  try {
    const res = await Auth.request('POST', '/api/checkins/distance', { lat1, lon1, lat2, lon2 });
    // Draw line
    if (distanceLine) map.removeLayer(distanceLine);
    distanceLine = L.polyline([[lat1, lon1], [lat2, lon2]], {
      color: '#f59e0b', weight: 2, dashArray: '8,6', opacity: 0.8
    }).addTo(map);
    map.fitBounds([[lat1, lon1], [lat2, lon2]], { padding: [60, 60] });

    const panel = document.getElementById('distance-panel');
    const km = res.km;
    const m = res.meters;
    if (panel) {
      panel.style.display = 'block';
      document.getElementById('dist-from').textContent = nameA;
      document.getElementById('dist-to').textContent = nameB;
      document.getElementById('dist-km').textContent = km >= 1 ? km.toFixed(2) + ' km' : m.toFixed(0) + ' m';
    }
  } catch (e) {
    showToast('Distance calc failed', 'error');
  }
}

/* ── User list panel ── */
function updateUserList(checkins) {
  const el = document.getElementById('user-list');
  if (!el) return;
  const me = Auth.user();
  el.innerHTML = checkins.map(c => {
    const isMe = c.user_id === me?.id;
    return `
      <div class="user-item" onclick="flyToUser(${c.user_id})" style="
        padding:0.7rem;border-radius:8px;cursor:pointer;transition:background 0.2s;
        border-bottom:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:700;font-size:0.9rem">${c.username} ${isMe ? '(you)' : ''}</span>
          <span style="font-size:0.7rem;font-family:monospace;color:var(--text-dim)">
            ${new Date(c.checked_at + 'Z').toLocaleTimeString()}
          </span>
        </div>
        ${c.label ? `<div style="font-size:0.75rem;color:var(--text-dim);margin-top:2px">📍 ${c.label}</div>` : ''}
      </div>`;
  }).join('');
}

function flyToUser(userId) {
  const m = markers[userId];
  if (m) { map.flyTo(m.getLatLng(), 15, { animate: true, duration: 1 }); m.openPopup(); }
}

/* ── History list ── */
async function loadHistory() {
  const el = document.getElementById('history-list');
  if (!el) return;
  try {
    const hist = await Auth.request('GET', '/api/checkins/history');
    el.innerHTML = hist.length ? hist.map(c => `
      <div style="padding:0.6rem 0;border-bottom:1px solid var(--border);font-size:0.8rem">
        <div style="display:flex;justify-content:space-between">
          <span style="font-family:monospace;color:var(--accent)">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</span>
          <span style="color:var(--text-dim)">${new Date(c.checked_at + 'Z').toLocaleString()}</span>
        </div>
        ${c.label ? `<div style="color:var(--text-dim);margin-top:2px">📍 ${c.label}</div>` : ''}
      </div>`).join('') : '<div style="color:var(--text-dim);font-size:0.85rem">No check-ins yet.</div>';
  } catch (e) { el.textContent = 'Failed to load history'; }
}
