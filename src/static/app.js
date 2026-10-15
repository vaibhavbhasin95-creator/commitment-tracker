// Shared helpers for the Meeting Commitment Tracker prototype frontend.

// options.body: a plain object (sent as JSON) or a FormData (file uploads).
// Every call carries X-CT-Request: 1 — the server refuses state-changing
// calls without it, which a forged cross-site form can't add (CSRF guard).
async function api(path, options = {}) {
    const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
    const headers = { "X-CT-Request": "1" };
    if (!isForm) headers["Content-Type"] = "application/json";
    const res = await fetch(path, {
        method: options.method || "GET",
        headers,
        credentials: "same-origin",
        cache: "no-store",
        body: options.body ? (isForm ? options.body : JSON.stringify(options.body)) : undefined,
    });
    let data = null;
    try {
        data = await res.json();
    } catch (e) {
        data = null;
    }
    if (!res.ok) {
        // Session ended elsewhere (password changed, signed out everywhere): back to sign-in.
        if (res.status === 401 && !path.startsWith("/api/auth/") && path !== "/api/me"
                && window.location.pathname !== "/") {
            window.location.href = "/";
        }
        const message = (data && data.error) || `Request failed (${res.status})`;
        throw new Error(message);
    }
    return data;
}

// A page restored from the browser's back/forward cache shows whatever was
// on screen when you left. Pages that show saved settings call this so they
// always re-read them from the server (Integrations, AI extraction, ...).
function reloadOnReturn(reload) {
    window.addEventListener("pageshow", (e) => {
        if (e.persisted) reload();
    });
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

// Redirects to the sign-in page if there's no active session.
// Returns the signed-in email on success.
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
        if (emailEl) emailEl.textContent = me.display_name || me.email;
        if (avatarEl) {
            avatarEl.textContent = (me.display_name || me.email).slice(0, 2).toUpperCase();
            const link = document.createElement("a");
            link.href = "/account";
            link.title = "Account & sign-in";
            link.className = "avatar-link";
            avatarEl.parentNode.insertBefore(link, avatarEl);
            link.appendChild(avatarEl);
            if (emailEl) link.appendChild(emailEl);
        }
    } catch (e) {
        // handled by requireIdentified() on protected pages
    }
    const brand = document.querySelector(".sidebar-brand");
    if (brand) {
        try {
            const { version } = await api("/api/version");
            const tag = document.createElement("span");
            tag.textContent = `v${version}`;
            tag.title = "App version — see CHANGELOG.md";
            tag.style.cssText = "margin-left:auto; font-size:11px; font-weight:500; color:var(--text-faint);";
            brand.appendChild(tag);
        } catch (e) {
            // version label is cosmetic; ignore failures
        }
    }
    // [ai-extraction v0.5.0] nav link, shown only when the optional feature is
    // registered (the status route 404s when AI_EXTRACTION_ENABLED=0).
    const nav = document.querySelector(".sidebar-nav");
    if (nav) {
        try {
            await api("/api/ai-extraction/status");
            const link = document.createElement("a");
            link.href = "/ai-extraction";
            link.className = "sidebar-link" + (window.location.pathname === "/ai-extraction" ? " active" : "");
            link.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"></path><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8z"></path></svg>AI extraction';
            nav.appendChild(link);
        } catch (e) {
            // feature disabled — no link
        }
    }
    // [/ai-extraction v0.5.0]
    if (nav) {
        const jobsLink = document.createElement("a");
        jobsLink.href = "/jobs";
        jobsLink.className = "sidebar-link" + (window.location.pathname === "/jobs" ? " active" : "");
        jobsLink.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M7 9h10M7 13h6M7 17h3"></path></svg>Background jobs';
        nav.appendChild(jobsLink);
    }
    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", async () => {
            await api("/api/logout", { method: "POST" });
            window.location.href = "/";
        });
    }
}

// ---------- live updates (Server-Sent Events from /api/events) ----------
//
// onChange(kind, data) is called (debounced) whenever the server reports a
// change to this account — a meeting arriving by webhook, a status change
// from the Sheet, a nudge going out. If the stream can't be used (old
// browser, or the server refuses it), falls back to calling
// onChange("poll", {}) every pollMs while the tab is visible. The element
// with id "live-indicator", if present, shows the connection state.

const LIVE_EVENT_KINDS = ["meeting_created", "meetings_changed", "commitments_changed", "commitment_status", "nudge_sent", "job_updated"];

function connectLiveEvents(onChange, { pollMs = 8000, debounceMs = 300 } = {}) {
    const indicator = document.getElementById("live-indicator");
    const setState = (state, label) => {
        if (!indicator) return;
        indicator.dataset.state = state;
        indicator.textContent = label;
    };

    let timer = null;
    let pending = [];
    const fire = (kind, data) => {
        pending.push({ kind, data });
        clearTimeout(timer);
        timer = setTimeout(() => {
            const batch = pending;
            pending = [];
            batch.forEach(e => onChange(e.kind, e.data));
        }, debounceMs);
    };

    let pollTimer = null;
    const startPolling = () => {
        if (pollTimer || !pollMs) return;  // pollMs: 0 = live updates only, no fallback
        setState("polling", "Auto-refresh");
        pollTimer = setInterval(() => {
            if (document.visibilityState === "visible") onChange("poll", {});
        }, pollMs);
    };

    if (!window.EventSource) {
        startPolling();
        return null;
    }

    const source = new EventSource("/api/events");
    let everConnected = false;
    source.addEventListener("ready", () => {
        // On a reconnect, catch up on anything missed while disconnected.
        if (everConnected) fire("reconnected", {});
        everConnected = true;
        setState("live", "Live");
    });
    LIVE_EVENT_KINDS.forEach(kind => source.addEventListener(kind, (e) => {
        let data = {};
        try { data = JSON.parse(e.data); } catch (err) { /* ignore */ }
        fire(kind, data);
    }));
    source.onerror = () => {
        if (source.readyState === EventSource.CLOSED) {
            // The server refused the stream (e.g. signed out) — browsers don't retry this case.
            startPolling();
        } else {
            setState("reconnecting", "Reconnecting…");
        }
    };
    return source;
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
