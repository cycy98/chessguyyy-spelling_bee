const AUTH_TOKEN_KEY = "spelling-bee-auth-token";

const state = {
    authToken: window.localStorage.getItem(AUTH_TOKEN_KEY) || "",
    viewedAccount: null,
};

const elements = {
    profileStatus: document.getElementById("profile-status"),
    profileNote: document.getElementById("profile-note"),
    accountHighlights: document.getElementById("account-highlights"),
    accountStatGrid: document.getElementById("account-stat-grid"),
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

function formatPercent(value) {
    return `${Number(value || 0).toFixed(1)}%`;
}

function renderViewedAccount() {
    const account = state.viewedAccount;
    elements.accountHighlights.innerHTML = "";
    elements.accountStatGrid.innerHTML = "";

    if (!account) {
        elements.profileStatus.textContent = "No account selected";
        elements.profileNote.textContent = "Log in first or open another player's profile from the leaderboard.";
        const empty = document.createElement("p");
        empty.className = "setup-note";
        empty.textContent = "No profile is available to display right now.";
        elements.accountStatGrid.append(empty);
        return;
    }

    elements.profileStatus.textContent = `${account.username} | ${Math.round(account.elo)} ELO`;
    elements.profileNote.textContent = "Tracked multiplayer stats for this account.";

    [
        { label: "Games", value: account.games_played },
        { label: "Words", value: account.words_attempted },
        { label: "Win Rate", value: formatPercent(account.win_rate) },
        { label: "Correct Rate", value: formatPercent(account.correct_rate) },
    ].forEach((item) => {
        const card = document.createElement("article");
        card.className = "leaderboard-highlight";
        card.innerHTML = `
            <p class="status-label">${item.label}</p>
            <p class="leaderboard-highlight-value">${item.value}</p>
        `;
        elements.accountHighlights.append(card);
    });

    [
        ["Wins", account.wins],
        ["Correct words", account.correct_words],
        ["Highest WPM", account.highest_wpm],
        ["Best WPM word", account.best_wpm_word || "None yet"],
        ["Discord linked", account.discord_linked ? "Yes" : "No"],
        ["Password login", account.has_password ? "Enabled" : "Not set"],
    ].forEach(([label, value]) => {
        const row = document.createElement("div");
        row.className = "account-stat-row";
        row.innerHTML = `
            <span class="account-stat-label">${label}</span>
            <span class="account-stat-value">${value}</span>
        `;
        elements.accountStatGrid.append(row);
    });
}

async function loadViewedAccount() {
    const params = new URLSearchParams(window.location.search);
    const username = params.get("username");
    if (username) {
        const payload = await api(`/api/account?username=${encodeURIComponent(username)}`);
        state.viewedAccount = payload.account;
        renderViewedAccount();
        return;
    }

    if (!state.authToken) {
        state.viewedAccount = null;
        renderViewedAccount();
        return;
    }

    const payload = await api("/api/account");
    state.viewedAccount = payload.account;
    renderViewedAccount();
}

renderViewedAccount();
loadViewedAccount().catch((error) => {
    elements.profileStatus.textContent = "Could not load account";
    elements.profileNote.textContent = error.message;
    elements.accountStatGrid.innerHTML = "";
    const message = document.createElement("p");
    message.className = "setup-note";
    message.textContent = error.message;
    elements.accountStatGrid.append(message);
});
