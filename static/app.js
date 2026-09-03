// Shared helpers for the Meeting Commitment Tracker prototype frontend.

async function api(path, options = {}) {
    const res = await fetch(path, {
        method: options.method || "GET",
        headers: { "Content-Type": "application/json" },
        body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = null;
    try {
        data = await res.json();
    } catch (e) {
        data = null;
    }
    if (!res.ok) {
        const message = (data && data.error) || `Request failed (${res.status})`;
        throw new Error(message);
    }
    return data;
}

function toast(message) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : str;
    return div.innerHTML;
}

function formatDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const STATUS_LABELS = { pending: "Pending", in_progress: "In progress", done: "Done" };
const CADENCE_LABELS = {
    day_before: "1 day before due",
    on_due: "On the due date",
    daily_after_overdue: "Daily once overdue",
};

// Redirects to the landing page if there's no active (email-only) session.
// Returns the identified email on success.
async function requireIdentified() {
    try {
        const me = await api("/api/me");
        return me.email;
    } catch (e) {
        window.location.href = "/";
        return new Promise(() => {}); // navigating away — never resolve, so callers stop here
    }
}

async function initNav(activePage) {
    const emailEl = document.getElementById("nav-email");
    const avatarEl = document.getElementById("nav-avatar");
    try {
        const me = await api("/api/me");
        if (emailEl) emailEl.textContent = me.email;
        if (avatarEl) avatarEl.textContent = me.email.slice(0, 2).toUpperCase();
    } catch (e) {
        // handled by requireIdentified() on protected pages
    }
    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", async () => {
            await api("/api/logout", { method: "POST" });
            window.location.href = "/";
        });
    }
}

// ---------- theme (light/dark) ----------

const ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></path></svg>';
const ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>';

function getTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("ct-theme", theme); } catch (e) { /* ignore */ }
    const btn = document.getElementById("theme-toggle");
    if (btn) {
        btn.innerHTML = theme === "dark" ? ICON_SUN : ICON_MOON;
        btn.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    }
}

function initThemeToggle() {
    setTheme(getTheme());
    const btn = document.getElementById("theme-toggle");
    if (btn) {
        btn.addEventListener("click", () => setTheme(getTheme() === "dark" ? "light" : "dark"));
    }
}
