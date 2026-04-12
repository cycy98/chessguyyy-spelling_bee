const elements = {
    leaderboardSummary: document.getElementById("leaderboard-summary"),
    leaderboardHighlights: document.getElementById("leaderboard-highlights"),
    leaderboardTableBody: document.getElementById("leaderboard-table-body"),
};

async function api(path) {
    const response = await fetch(path, {
        headers: {
            "Content-Type": "application/json",
        },
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

function formatCountWithPercent(count, percent) {
    return `${count} (${formatPercent(percent)})`;
}

function renderHighlights(players) {
    elements.leaderboardHighlights.innerHTML = "";

    if (!players.length) {
        return;
    }

    const highestElo = players[0];
    const bestWinRate = [...players]
        .filter((player) => player.games_played > 0)
        .sort((a, b) => b.win_rate - a.win_rate || b.wins - a.wins || b.elo - a.elo)[0] || players[0];
    const bestCorrectRate = [...players]
        .filter((player) => player.words_attempted > 0)
        .sort((a, b) => b.correct_rate - a.correct_rate || b.correct_words - a.correct_words || b.elo - a.elo)[0] || players[0];
    const highestWpm = [...players].sort((a, b) => b.highest_wpm - a.highest_wpm || b.elo - a.elo)[0];

    [
        { label: "Top ELO", player: highestElo, value: Math.round(highestElo.elo) },
        { label: "Best Win %", player: bestWinRate, value: formatPercent(bestWinRate.win_rate) },
        { label: "Best Correct %", player: bestCorrectRate, value: formatPercent(bestCorrectRate.correct_rate) },
        { label: "High WPM", player: highestWpm, value: highestWpm.highest_wpm },
    ].forEach((item) => {
        const card = document.createElement("article");
        card.className = "leaderboard-highlight";
        card.innerHTML = `
            <p class="status-label">${item.label}</p>
            <p class="leaderboard-highlight-value">${item.value}</p>
            <p class="leaderboard-highlight-name">${item.player.username}</p>
        `;
        elements.leaderboardHighlights.append(card);
    });
}

function renderTable(players) {
    elements.leaderboardTableBody.innerHTML = "";

    if (!players.length) {
        const empty = document.createElement("p");
        empty.className = "setup-note";
        empty.textContent = "No players have stats yet.";
        elements.leaderboardTableBody.append(empty);
        return;
    }

    players.forEach((player, index) => {
        const row = document.createElement("div");
        row.className = "leaderboard-table-row";
        row.innerHTML = `
            <span>#${index + 1}</span>
            <a class="leaderboard-table-player" href="/account.html?username=${encodeURIComponent(player.username)}">${player.username}</a>
            <span>${Math.round(player.elo)}</span>
            <span>${player.games_played}</span>
            <span>${formatCountWithPercent(player.wins, player.win_rate)}</span>
            <span>${formatCountWithPercent(player.correct_words, player.correct_rate)}</span>
            <span title="${player.best_wpm_word || "No recorded word yet"}">${player.highest_wpm}</span>
        `;
        elements.leaderboardTableBody.append(row);
    });
}

async function loadLeaderboard() {
    const payload = await api("/api/leaderboard");
    const players = payload.players || [];
    elements.leaderboardSummary.textContent = players.length
        ? `${players.length} tracked players`
        : "No tracked players yet";
    renderHighlights(players);
    renderTable(players);
}

loadLeaderboard().catch((error) => {
    elements.leaderboardSummary.textContent = "Could not load standings";
    elements.leaderboardTableBody.innerHTML = "";
    const message = document.createElement("p");
    message.className = "setup-note";
    message.textContent = error.message;
    elements.leaderboardTableBody.append(message);
});
