/* static/js/auth.js — Authentication helpers */

const API = window.location.origin;

const Auth = {
  token: () => localStorage.getItem('lt_token'),
  user:  () => JSON.parse(localStorage.getItem('lt_user') || 'null'),
  isAdmin: () => Auth.user()?.is_admin === true,

  save(data) {
    localStorage.setItem('lt_token', data.access_token);
    localStorage.setItem('lt_user', JSON.stringify(data.user));
  },

  logout() {
    localStorage.removeItem('lt_token');
    localStorage.removeItem('lt_user');
    window.location.href = '/';
  },

  async request(method, path, body = null) {
    const headers = { 'Content-Type': 'application/json' };
    if (Auth.token()) headers['Authorization'] = `Bearer ${Auth.token()}`;
    const res = await fetch(API + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    return data;
  },
};

/* ── Toast helper ── */
function showToast(msg, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

/* ── Redirect if not logged in ── */
function requireAuth() {
  if (!Auth.token()) window.location.href = '/';
}

/* ── Populate navbar ── */
function initNavbar() {
  const user = Auth.user();
  const el = document.getElementById('navbar-user');
  const adminLink = document.getElementById('nav-admin');
  if (el && user) {
    el.innerHTML = `Logged in as <strong>${user.username}</strong>`;
  }
  if (adminLink && user?.is_admin) {
    adminLink.style.display = 'inline';
  }
}
