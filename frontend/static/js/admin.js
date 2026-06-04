/* static/js/admin.js — Admin panel logic */

async function loadAdminUsers() {
  const tbody = document.getElementById('users-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim);padding:1rem">Loading...</td></tr>';
  try {
    const users = await Auth.request('GET', '/api/admin/users');
    tbody.innerHTML = users.map(u => `
      <tr>
        <td>${u.id}</td>
        <td><strong>${u.username}</strong></td>
        <td>
          <span class="badge ${u.is_admin ? 'badge-admin' : 'badge-user'}">
            ${u.is_admin ? 'Admin' : 'User'}
          </span>
        </td>
        <td>${new Date(u.created_at + 'Z').toLocaleString()}</td>
        <td>
          <button class="btn btn-danger" onclick="deleteUser(${u.id}, '${u.username}')"
            style="padding:0.3rem 0.7rem;font-size:0.75rem">
            Delete
          </button>
        </td>
      </tr>`).join('');
    document.getElementById('total-users').textContent = users.length;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">${e.message}</td></tr>`;
  }
}

async function loadAdminCheckins() {
  const tbody = document.getElementById('checkins-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim);padding:1rem">Loading...</td></tr>';
  try {
    const checkins = await Auth.request('GET', '/api/admin/checkins');
    tbody.innerHTML = checkins.map(c => `
      <tr>
        <td>${c.id}</td>
        <td><strong>${c.username}</strong></td>
        <td style="font-family:monospace;font-size:0.75rem">${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)}</td>
        <td style="color:var(--text-dim);font-size:0.8rem">${c.label || '—'}</td>
        <td style="font-size:0.75rem">${new Date(c.checked_at + 'Z').toLocaleString()}</td>
        <td>
          <button class="btn btn-danger" onclick="deleteCheckin(${c.id})"
            style="padding:0.3rem 0.7rem;font-size:0.75rem">
            Delete
          </button>
        </td>
      </tr>`).join('');
    document.getElementById('total-checkins').textContent = checkins.length;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:var(--danger)">${e.message}</td></tr>`;
  }
}

async function deleteUser(id, username) {
  if (!confirm(`Delete user "${username}" and all their check-ins?`)) return;
  try {
    await Auth.request('DELETE', `/api/admin/users/${id}`);
    showToast(`User "${username}" deleted`, 'success');
    loadAdminUsers();
    loadAdminCheckins();
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function deleteCheckin(id) {
  if (!confirm('Delete this check-in?')) return;
  try {
    await Auth.request('DELETE', `/api/admin/checkins/${id}`);
    showToast('Check-in deleted', 'success');
    loadAdminCheckins();
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

/* ── Tab switching ── */
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.getElementById(`tab-${tab}`).style.display = 'block';
  event.target.classList.add('active');
  if (tab === 'users') loadAdminUsers();
  if (tab === 'checkins') loadAdminCheckins();
}
