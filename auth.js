const AUTH_TOKEN_KEY = "spelling-bee-auth-token";

const state = {
    authToken: window.localStorage.getItem(AUTH_TOKEN_KEY) || "",
    account: null,
    discordStatus: null,
};

const elements = {
    authStatus: document.getElementById("auth-status"),
    authNote: document.getElementById("auth-note"),
    authUsername: document.getElementById("auth-username"),
    authPassword: document.getElementById("auth-password"),
    registerButton: document.getElementById("register-button"),
    loginButton: document.getElementById("login-button"),
    discordLoginButton: document.getElementById("discord-login-button"),
    logoutButton: document.getElementById("logout-button"),
    feedbackCard: document.getElementById("auth-feedback"),
    feedbackTitle: document.getElementById("feedback-title"),
    feedbackBody: document.getElementById("feedback-body"),
};

async function api(path, options = {}) {
    const headers = {
        ...(options.headers || {}),
    };
    if (options.body !== undefined && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
    }
    if (state.authToken && !headers.Authorization) {
        headers.Authorization = `Bearer ${state.authToken}`;
    }

    const response = await fetch(path, {
        headers,
        ...options,
    });
    const payload = await response.json();
    if (!response.ok) {
        throw new Error(payload.error || "Request failed.");
    }
    return payload;
}

function persistAuthToken(token) {
    state.authToken = token || "";
    if (state.authToken) {
        window.localStorage.setItem(AUTH_TOKEN_KEY, state.authToken);
    } else {
        window.localStorage.removeItem(AUTH_TOKEN_KEY);
    }
}

function setFeedback(title, body, tone = "") {
    elements.feedbackTitle.textContent = title;
    elements.feedbackBody.textContent = body;
    elements.feedbackCard.classList.remove("success", "error");
    if (tone) {
        elements.feedbackCard.classList.add(tone);
    }
}

function updateAuthUi() {
    const isLoggedIn = Boolean(state.account);
    elements.logoutButton.classList.toggle("hidden", !isLoggedIn);

    if (state.account) {
        elements.authStatus.textContent = `${state.account.username} | ${Math.round(state.account.elo)} ELO`;
        elements.authNote.textContent = "You are signed in. Your account name is reserved for your online games.";
    } else {
        elements.authStatus.textContent = "Not logged in";
        elements.authNote.textContent = "Create an account, sign in, or use Discord to unlock public multiplayer.";
    }

    elements.discordLoginButton.disabled = true;
    elements.discordLoginButton.title = "Discord login does not work at this moment.";
}

async function loadAuthStatus() {
    if (!state.authToken) {
        state.account = null;
        updateAuthUi();
        return;
    }

    const payload = await api("/api/auth/me");
    state.account = payload.account || null;
    if (!state.account) {
        persistAuthToken("");
    }
    updateAuthUi();
}

async function loadDiscordStatus() {
    try {
        state.discordStatus = await api("/api/auth/discord/status");
    } catch {
        state.discordStatus = null;
    }
    updateAuthUi();
}

async function registerAccount() {
    const payload = await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
            username: elements.authUsername.value.trim(),
            password: elements.authPassword.value,
        }),
    });
    persistAuthToken(payload.token);
    state.account = payload.account;
    elements.authPassword.value = "";
    updateAuthUi();
    setFeedback("Account ready", `Signed in as ${payload.account.username}.`, "success");
}

async function loginAccount() {
    const payload = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
            username: elements.authUsername.value.trim(),
            password: elements.authPassword.value,
        }),
    });
    persistAuthToken(payload.token);
    state.account = payload.account;
    elements.authPassword.value = "";
    updateAuthUi();
    setFeedback("Logged in", `Welcome back, ${payload.account.username}.`, "success");
}

async function logoutAccount() {
    try {
        await api("/api/auth/logout", { method: "POST" });
    } catch {
        // Best effort logout is enough here.
    }
    persistAuthToken("");
    state.account = null;
    updateAuthUi();
    setFeedback("Logged out", "You can sign in again whenever you want.", "success");
}

function startDiscordLogin() {
    throw new Error("Discord login does not work at this moment.");
}

function handleDiscordMessage(event) {
    if (event.origin !== window.location.origin || !event.data || event.data.type !== "discord-auth") {
        return;
    }

    if (event.data.status !== "success") {
        setFeedback("Discord login failed", event.data.payload || "Discord login could not be completed.", "error");
        return;
    }

    const payload = JSON.parse(event.data.payload);
    persistAuthToken(payload.token);
    state.account = payload.account;
    updateAuthUi();
    setFeedback("Discord linked", `Signed in as ${payload.account.username}.`, "success");
}

elements.registerButton.addEventListener("click", () => {
    registerAccount().catch((error) => setFeedback("Account failed", error.message, "error"));
});

elements.loginButton.addEventListener("click", () => {
    loginAccount().catch((error) => setFeedback("Login failed", error.message, "error"));
});

elements.discordLoginButton.addEventListener("click", () => {
    try {
        startDiscordLogin();
    } catch (error) {
        setFeedback("Discord login failed", error.message, "error");
    }
});

elements.logoutButton.addEventListener("click", () => {
    logoutAccount().catch((error) => setFeedback("Logout failed", error.message, "error"));
});

window.addEventListener("message", handleDiscordMessage);

updateAuthUi();
Promise.all([
    loadAuthStatus(),
    loadDiscordStatus(),
]).catch((error) => {
    setFeedback("Load failed", error.message, "error");
});
