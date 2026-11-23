// Small helpers shared by every page.
const PL = {
  async api(path, opts = {}) {
    const o = { credentials: "same-origin", ...opts, headers: { ...(opts.headers || {}) } };
    if (o.json !== undefined) {
      o.body = JSON.stringify(o.json);
      o.headers["Content-Type"] = "application/json";
      delete o.json;
    }
    if (PL.panelToken) o.headers["X-Panel-Token"] = PL.panelToken;
    const r = await fetch(path, o);
    let data = null;
    try { data = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) {
      const err = new Error((data && data.detail) || `Error ${r.status}`);
      err.status = r.status;
      throw err;
    }
    return data;
  },
  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  dur(sec) {
    if (sec == null) return "?";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    if (h && m) return `${h} h ${m} min`;
    if (h) return `${h} h`;
    return `${Math.max(m, 1)} min`;
  },
  time(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  },
  day(iso) {
    if (!iso) return "";
    const d = new Date(iso), today = new Date();
    if (d.toDateString() === today.toDateString()) return PL.time(iso);
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + PL.time(iso);
  },
  STATUS: {
    pending_approval: "Waiting for teacher", approved: "In queue", printing: "Printing", done: "Done",
    failed: "Failed", rejected: "Rejected", cancelled: "Cancelled",
    ready: "Ready", needs_clear: "Clear bed", paused: "Paused", attention: "Needs attention",
    offline: "Offline", maintenance: "Maintenance", error: "Error",
  },
  badge(status) {
    return `<span class="badge s-${status}">${PL.STATUS[status] || status}</span>`;
  },
  show(el, text, kind) {
    el.textContent = text;
    el.className = `msg show ${kind}`;
  },
};
