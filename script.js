const ROOM_POLL_INTERVAL_MS = 450;
const LOCAL_PLAYER_MIN = 2;
const LOCAL_PLAYER_MAX = 12;
const AUTH_TOKEN_KEY = "spelling-bee-auth-token";
const state = {
    config: null,
    authToken: window.localStorage.getItem(AUTH_TOKEN_KEY) || "",
    account: null,
    discordStatus: null,
    sessionId: null,
    currentRound: null,
    roomCode: null,
    roomPollId: null,
    currentAudio: null,
    streak: 0,
    mode: "solo",
    roundStartedAt: 0,
    typingUnlockedAt: 0,
    typingUnlockTimeoutId: null,
    typingReady: false,
    lastWpm: 0,
    wpmFlashTimeoutId: null,
    roomDraftTimeoutId: null,
    onlineRoom: null,
    lastOnlineRoundKey: null,
    localGame: null,
    roundTimeLimitSeconds: 0,
    roundExpiresAt: 0,
    timerIntervalId: null,
    timeoutSkipInFlight: false,
    onlineTurnRemainingSeconds: 0,
    onlineTurnSyncedAt: 0,
    onlineTurnTotalSeconds: 0,
    lastAutoPlayerName: "",
    selectedDifficulty: "",
    localPlayerSlots: LOCAL_PLAYER_MIN,
};

const elements = {
    setupPanel: document.getElementById("setup-panel"),
    gamePanel: document.getElementById("game-panel"),
    authStatus: document.getElementById("auth-status"),
    authNote: document.getElementById("auth-note"),
    logoutButton: document.getElementById("logout-button"),
    playerNameGroup: document.getElementById("player-name-group"),
    playerName: document.getElementById("player-name"),
    difficultyPicker: document.getElementById("difficulty-picker"),
    roomCodeInput: document.getElementById("room-code-input"),
    soloButton: document.getElementById("solo-button"),
    onlineButton: document.getElementById("online-button"),
    publicButton: document.getElementById("public-button"),
    localToggleButton: document.getElementById("local-toggle-button"),
    localSetup: document.getElementById("local-setup"),
    addLocalPlayerButton: document.getElementById("add-local-player-button"),
    removeLocalPlayerButton: document.getElementById("remove-local-player-button"),
    localPlayerCountDisplay: document.getElementById("local-player-count-display"),
    localPlayerGrid: document.getElementById("local-player-grid"),
    startLocalButton: document.getElementById("start-local-button"),
    catalogSummary: document.getElementById("catalog-summary"),
    modeLabel: document.getElementById("mode-label"),
    playerLabel: document.getElementById("player-label"),
    difficultyBadge: document.getElementById("difficulty-badge"),
    partOfSpeech: document.getElementById("part-of-speech"),
    definitionText: document.getElementById("definition-text"),
    speakButton: document.getElementById("speak-button"),
    skipButton: document.getElementById("skip-button"),
    submitButton: document.getElementById("submit-button"),
    exitButton: document.getElementById("exit-button"),
    menuButton: document.getElementById("menu-button"),
    answerForm: document.getElementById("answer-form"),
    guessInput: document.getElementById("guess-input"),
    feedbackCard: document.getElementById("feedback-card"),
    feedbackTitle: document.getElementById("feedback-title"),
    feedbackBody: document.getElementById("feedback-body"),
    wpmFlash: document.getElementById("wpm-flash"),
    timerStrip: document.getElementById("timer-strip"),
    timerSeconds: document.getElementById("timer-seconds"),
    timerFill: document.getElementById("timer-fill"),
    leaderboardPanel: document.getElementById("leaderboard-panel"),
    leaderboardKicker: document.getElementById("leaderboard-kicker"),
    leaderboardTitle: document.getElementById("leaderboard-title"),
    leaderboardList: document.getElementById("leaderboard-list"),
    streakBadge: document.getElementById("streak-badge"),
    accountPageLink: document.getElementById("account-page-link"),
    chatPanel: document.getElementById("chat-panel"),
    chatTitle: document.getElementById("chat-title"),
    chatList: document.getElementById("chat-list"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
    chatSendButton: document.getElementById("chat-send-button"),
};

async function api(path, options = {}) {
    const headers = {
        ...(options.headers || {}),
    };
    const hasBody = options.body !== undefined;
    if (hasBody && !headers["Content-Type"]) {
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

function isSharedMultiplayerMode() {
    return state.mode === "online" || state.mode === "public";
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
    const account = state.account;
    const isLoggedIn = Boolean(account);
    elements.logoutButton.classList.toggle("hidden", !isLoggedIn);
    elements.publicButton.disabled = !isLoggedIn;

    if (isLoggedIn) {
        elements.authStatus.textContent = `${account.username} | ${Math.round(account.elo)} ELO`;
        elements.authNote.textContent = "Public multiplayer is unlocked for this account.";
        if (!elements.playerName.value.trim() || elements.playerName.value === state.lastAutoPlayerName) {
            elements.playerName.value = account.username;
            state.lastAutoPlayerName = account.username;
        }
        elements.accountPageLink.href = `/account.html?username=${encodeURIComponent(account.username)}`;
    } else {
        elements.authStatus.textContent = "Not logged in";
        elements.authNote.textContent = "Open the account page to sign in or create an account.";
        if (elements.playerName.value === state.lastAutoPlayerName) {
            elements.playerName.value = "";
        }
        state.lastAutoPlayerName = "";
        elements.accountPageLink.href = "/account.html";
    }
}

function getSelectedDifficulty() {
    return state.selectedDifficulty;
}

function difficultyCardBackground(item) {
    if (item.name === "randomizer") {
        return "linear-gradient(135deg, #2563eb, #22c55e)";
    }
    return item.color || "#d4d4d8";
}

function renderDifficultyPicker() {
    if (!state.config) {
        return;
    }

    if (!state.selectedDifficulty) {
        state.selectedDifficulty = state.config.difficulties[0]?.name || "";
    }

    elements.difficultyPicker.innerHTML = "";
    for (const item of state.config.difficulties) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "difficulty-option";
        if (item.name === state.selectedDifficulty) {
            button.classList.add("is-selected");
        }
        button.innerHTML = `
            <span class="difficulty-option-swatch" style="background:${difficultyCardBackground(item)}"></span>
            <span class="difficulty-option-copy">
                <span class="difficulty-option-name">${item.name === "randomizer" ? "Randomizer" : item.name}</span>
                <span class="difficulty-option-meta">${item.word_count} words</span>
            </span>
        `;
        button.addEventListener("click", () => {
            state.selectedDifficulty = item.name;
            renderDifficultyPicker();
        });
        elements.difficultyPicker.append(button);
    }
}

function renderChat(room = null) {
    const shouldShow = Boolean(isSharedMultiplayerMode() && room);
    elements.chatPanel.classList.toggle("hidden", !shouldShow);
    elements.chatList.innerHTML = "";

    if (!shouldShow) {
        elements.chatInput.value = "";
        return;
    }

    elements.chatTitle.textContent = state.roomCode ? `Lobby ${state.roomCode}` : "Room messages";
    const messages = room.chat_messages || [];
    if (!messages.length) {
        const empty = document.createElement("p");
        empty.className = "setup-note";
        empty.textContent = "No messages yet.";
        elements.chatList.append(empty);
        return;
    }

    messages.forEach((entry) => {
        const item = document.createElement("p");
        item.className = "chat-message";
        if (entry.player_name === (state.account ? state.account.username : "")) {
            item.classList.add("is-self");
        }
        item.textContent = `[${entry.player_name}]: ${entry.message}`;
        elements.chatList.append(item);
    });
    elements.chatList.scrollTop = elements.chatList.scrollHeight;
}

function clearBoardTone() {
    elements.guessInput.classList.remove("board-correct", "board-incorrect");
}

function setBoardTone(tone) {
    clearBoardTone();
    if (tone) {
        elements.guessInput.classList.add(tone);
    }
}

function resizeBoard() {
    elements.guessInput.style.height = "280px";
}

function centerBoardText() {
    const area = elements.guessInput;
    const basePadding = 24;
    area.style.paddingTop = `${basePadding}px`;
    area.style.paddingBottom = `${basePadding}px`;
    const contentHeight = area.scrollHeight - basePadding * 2;
    const centered = Math.max(basePadding, Math.floor((area.clientHeight - contentHeight) / 2));
    const balanced = Math.min(centered, 110);
    area.style.paddingTop = `${balanced}px`;
    area.style.paddingBottom = `${balanced}px`;
}

function roundLabelForMode() {
    if (state.mode === "public") {
        return "Public Arena";
    }
    if (state.mode === "online") {
        return state.roomCode ? `Online Lobby ${state.roomCode}` : "Online Lobby";
    }
    if (state.mode === "local") {
        return "Local Multiplayer";
    }
    return "Solo";
}

function updateStatusHeader() {
    elements.modeLabel.textContent = roundLabelForMode();
}

function updateOnlineButtonLabel() {
    const hasRoomCode = Boolean(elements.roomCodeInput.value.trim());
    elements.onlineButton.textContent = hasRoomCode ? "Join Lobby" : "Create Lobby";
}

function updateStreakBadge() {
    const showStreak = state.mode === "solo" && state.streak >= 5;
    elements.streakBadge.classList.toggle("hidden", !showStreak);
    elements.streakBadge.textContent = `${state.streak} streak`;
}

function requiredWpmForStreak(streak) {
    return 5 * (Math.max(0, streak) ** 0.8) + 10;
}

function baseTimeLimitSecondsForRound(round) {
    if (!round || !round.pronunciation) {
        return 0;
    }

    const typedUnits = Math.max(round.pronunciation.replace(/\s+/g, "").length / 5, 0.2);
    const requiredWpm = state.mode === "solo" ? requiredWpmForStreak(state.streak) : 10;
    return Math.max(3, (typedUnits / requiredWpm) * 60);
}

function stopRoundTimer() {
    if (state.timerIntervalId) {
        window.clearInterval(state.timerIntervalId);
        state.timerIntervalId = null;
    }
    state.roundTimeLimitSeconds = 0;
    state.roundExpiresAt = 0;
    state.timeoutSkipInFlight = false;
    state.onlineTurnRemainingSeconds = 0;
    state.onlineTurnSyncedAt = 0;
    state.onlineTurnTotalSeconds = 0;
    elements.timerStrip.classList.add("hidden");
    elements.timerSeconds.textContent = "0.0s / 0.0s";
    elements.timerFill.style.width = "100%";
}

function refreshRoundTimerDisplay(remainingSeconds, totalSeconds) {
    const safeTotal = Math.max(totalSeconds, 0.1);
    const safeRemaining = Math.max(remainingSeconds, 0);
    const progress = Math.max(0, Math.min(1, safeRemaining / safeTotal));
    elements.timerStrip.classList.remove("hidden");
    elements.timerSeconds.textContent = `${safeRemaining.toFixed(1)}s / ${safeTotal.toFixed(1)}s`;
    elements.timerFill.style.width = `${(1 - progress) * 100}%`;
}

function startRoundTimer(totalSeconds) {
    stopRoundTimer();
    if (!totalSeconds || totalSeconds <= 0) {
        return;
    }

    state.roundTimeLimitSeconds = totalSeconds;
    state.roundExpiresAt = Date.now() + totalSeconds * 1000;
    refreshRoundTimerDisplay(totalSeconds, totalSeconds);
    state.timerIntervalId = window.setInterval(() => {
        updateRoundTimer();
    }, 100);
}

async function handleRoundTimeout() {
    if (state.timeoutSkipInFlight || !state.currentRound || !state.typingReady || !state.sessionId) {
        return;
    }

    state.timeoutSkipInFlight = true;
    try {
        setFeedback("Time expired", "You ran out of time. The round is being counted as a skip.", "error");
        await skipWord();
    } catch (error) {
        setFeedback("Timeout handling failed", error.message, "error");
    } finally {
        state.timeoutSkipInFlight = false;
    }
}

function updateRoundTimer() {
    if (isSharedMultiplayerMode() && state.onlineRoom) {
        const totalSeconds = Number(state.onlineTurnTotalSeconds || 0);
        const elapsedSeconds = Math.max(0, (Date.now() - state.onlineTurnSyncedAt) / 1000);
        const remainingSeconds = Math.max(0, Number(state.onlineTurnRemainingSeconds || 0) - elapsedSeconds);
        if (totalSeconds > 0 && state.onlineRoom.game_phase !== "intermission") {
            refreshRoundTimerDisplay(remainingSeconds, totalSeconds);
            if (!state.timerIntervalId) {
                state.timerIntervalId = window.setInterval(() => {
                    updateRoundTimer();
                }, 100);
            }
        } else {
            stopRoundTimer();
        }
        return;
    }

    if (!state.roundExpiresAt || !state.roundTimeLimitSeconds || !state.typingReady) {
        stopRoundTimer();
        return;
    }

    const remainingSeconds = Math.max(0, (state.roundExpiresAt - Date.now()) / 1000);
    refreshRoundTimerDisplay(remainingSeconds, state.roundTimeLimitSeconds);
    if (remainingSeconds <= 0.05) {
        stopRoundTimer();
        handleRoundTimeout().catch(() => {});
    }
}

function calculateWpm(sampleText) {
    const startedAt = state.typingUnlockedAt || state.roundStartedAt;
    const elapsedMs = Date.now() - startedAt;
    const normalizedSample = String(sampleText || "").replace(/\s+/g, "");
    if (!normalizedSample || elapsedMs <= 0) {
        return 0;
    }

    const effectiveElapsedMs = Math.max(elapsedMs, 2000);
    const minutes = effectiveElapsedMs / 60000;
    const typedUnits = Math.max(normalizedSample.length / 5, 0.2);
    return Math.min(320, Math.max(0, Math.round(typedUnits / minutes)));
}

function hideWpmFlash() {
    if (state.wpmFlashTimeoutId) {
        window.clearTimeout(state.wpmFlashTimeoutId);
        state.wpmFlashTimeoutId = null;
    }
    elements.wpmFlash.classList.add("hidden");
    elements.wpmFlash.classList.remove("success", "error");
}

function showWpmFlash(wpm, tone) {
    hideWpmFlash();
    elements.wpmFlash.textContent = `${wpm} WPM`;
    elements.wpmFlash.classList.remove("hidden");
    if (tone) {
        elements.wpmFlash.classList.add(tone);
    }
    state.wpmFlashTimeoutId = window.setTimeout(() => {
        hideWpmFlash();
    }, 500);
}

function clearRoomPolling() {
    if (state.roomPollId) {
        window.clearInterval(state.roomPollId);
        state.roomPollId = null;
    }
}

function clearRoomDraftSync() {
    if (state.roomDraftTimeoutId) {
        window.clearTimeout(state.roomDraftTimeoutId);
        state.roomDraftTimeoutId = null;
    }
}

function clearTypingUnlockTimer() {
    if (state.typingUnlockTimeoutId) {
        window.clearTimeout(state.typingUnlockTimeoutId);
        state.typingUnlockTimeoutId = null;
    }
}

function unlockTyping(options = {}) {
    const { focusInput = true } = options;
    if (state.typingReady) {
        return;
    }

    state.typingReady = true;
    state.typingUnlockedAt = Date.now();
    state.roundStartedAt = state.typingUnlockedAt;
    clearTypingUnlockTimer();
    if (!isSharedMultiplayerMode()) {
        startRoundTimer(baseTimeLimitSecondsForRound(state.currentRound));
    }

    if (isSharedMultiplayerMode()) {
        const canType = state.onlineRoom
            && state.onlineRoom.game_phase !== "intermission"
            && state.onlineRoom.active_session_id === state.sessionId
            && !state.onlineRoom.winner_session_id;
        elements.guessInput.disabled = !canType;
        elements.skipButton.disabled = !canType;
        elements.submitButton.disabled = !canType;
        if (focusInput && canType) {
            elements.guessInput.focus();
        }
        return;
    }

    if (!(state.mode === "local" && state.localGame && state.localGame.intermissionUntil)) {
        elements.guessInput.disabled = false;
        elements.skipButton.disabled = false;
        elements.submitButton.disabled = false;
        if (focusInput) {
            elements.guessInput.focus();
        }
    }
}

function lockTyping() {
    state.typingReady = false;
    state.roundStartedAt = 0;
    state.typingUnlockedAt = 0;
    clearTypingUnlockTimer();
    stopRoundTimer();
    elements.guessInput.disabled = true;
    elements.skipButton.disabled = true;
    elements.submitButton.disabled = true;
}

function stopCurrentAudio() {
    clearTypingUnlockTimer();
    if (state.currentAudio) {
        state.currentAudio.pause();
        state.currentAudio.currentTime = 0;
        state.currentAudio = null;
    }
}

function showGamePanel() {
    elements.setupPanel.classList.add("hidden");
    elements.gamePanel.classList.remove("hidden");
}

function showSetupPanel() {
    elements.gamePanel.classList.add("hidden");
    elements.setupPanel.classList.remove("hidden");
}

function resetSessionState() {
    clearLocalIntermissionTimer();
    clearRoomPolling();
    stopCurrentAudio();
    state.sessionId = null;
    state.currentRound = null;
    state.roomCode = null;
    state.streak = 0;
    state.roundStartedAt = 0;
    state.typingUnlockedAt = 0;
    state.typingReady = false;
    state.lastWpm = 0;
    state.onlineRoom = null;
    state.lastOnlineRoundKey = null;
    state.mode = "solo";
    state.localGame = null;
    stopRoundTimer();
    clearRoomDraftSync();
    updateStatusHeader();
    updateStreakBadge();
    clearBoardTone();
    renderLeaderboard();
    elements.playerLabel.textContent = "-";
    elements.difficultyBadge.textContent = "-";
    elements.difficultyBadge.style.backgroundColor = "";
    elements.partOfSpeech.textContent = "-";
    elements.definitionText.textContent = "Start a session to get your first word.";
    elements.guessInput.value = "";
    elements.guessInput.disabled = false;
    elements.skipButton.disabled = false;
    elements.submitButton.disabled = false;
    hideWpmFlash();
    resizeBoard();
    centerBoardText();
    renderChat();
}

function returnToMenu({ clearInputs = false } = {}) {
    resetSessionState();
    showSetupPanel();
    if (clearInputs) {
        elements.playerName.value = "";
        elements.roomCodeInput.value = "";
        state.localPlayerSlots = LOCAL_PLAYER_MIN;
        renderLocalPlayerInputs();
        setLocalSetupVisible(false);
    }
    setFeedback("Ready", "Choose a mode to start the next spelling round.");
}

async function forfeitCurrentOnlineSession() {
    if (!isSharedMultiplayerMode() || !state.sessionId) {
        return;
    }

    try {
        await api("/api/forfeit", {
            method: "POST",
            body: JSON.stringify({
                session_id: state.sessionId,
            }),
        });
    } catch {
        // Best effort only. The unload path also uses sendBeacon.
    }
}

function beaconForfeitCurrentOnlineSession() {
    if (!isSharedMultiplayerMode() || !state.sessionId || !navigator.sendBeacon) {
        return;
    }

    const payload = JSON.stringify({ session_id: state.sessionId });
    navigator.sendBeacon("/api/forfeit", new Blob([payload], { type: "application/json" }));
}

function setLocalSetupVisible(isVisible) {
    elements.localSetup.classList.toggle("hidden", !isVisible);
    elements.localToggleButton.textContent = isVisible ? "Hide Local Multiplayer" : "Local Multiplayer";
    elements.playerNameGroup.classList.toggle("hidden", isVisible);
}

function getLocalPlayerNames() {
    const fields = elements.localPlayerGrid.querySelectorAll("input[data-local-player]");
    return [...fields].map((field, index) => field.value.trim() || `Player ${index + 1}`);
}

function renderLocalPlayerInputs() {
    const count = Number(state.localPlayerSlots);
    elements.localPlayerCountDisplay.textContent = `${count} ${count === 1 ? "Player" : "Players"}`;
    elements.removeLocalPlayerButton.disabled = count <= LOCAL_PLAYER_MIN;
    elements.addLocalPlayerButton.disabled = count >= LOCAL_PLAYER_MAX;
    elements.localPlayerGrid.innerHTML = "";
    for (let index = 0; index < count; index += 1) {
        const wrapper = document.createElement("div");
        wrapper.className = "field-group";

        const label = document.createElement("label");
        label.setAttribute("for", `local-player-${index + 1}`);
        label.textContent = `Player ${index + 1}`;

        const input = document.createElement("input");
        input.id = `local-player-${index + 1}`;
        input.type = "text";
        input.maxLength = 24;
        input.placeholder = `Player ${index + 1}`;
        input.setAttribute("data-local-player", "true");

        wrapper.append(label, input);
        elements.localPlayerGrid.append(wrapper);
    }
}

function getActiveLocalPlayer() {
    if (!state.localGame) {
        return null;
    }
    return state.localGame.players[state.localGame.activePlayerIndex] || null;
}

function getAliveLocalPlayers() {
    if (!state.localGame) {
        return [];
    }
    return state.localGame.players.filter((player) => !player.eliminated);
}

function selectNextLocalPlayer() {
    if (!state.localGame) {
        return null;
    }

    const players = state.localGame.players;
    const aliveCount = getAliveLocalPlayers().length;
    if (aliveCount <= 1) {
        return null;
    }

    let nextIndex = state.localGame.activePlayerIndex;
    for (let step = 0; step < players.length; step += 1) {
        nextIndex = (nextIndex + 1) % players.length;
        if (!players[nextIndex].eliminated) {
            state.localGame.activePlayerIndex = nextIndex;
            return players[nextIndex];
        }
    }

    return null;
}

function renderLeaderboard(room = null) {
    const isOnline = isSharedMultiplayerMode() && room;
    const isLocal = state.mode === "local" && state.localGame;
    const shouldShow = Boolean(isOnline || isLocal);
    elements.leaderboardPanel.classList.toggle("hidden", !shouldShow);

    if (!shouldShow) {
        elements.leaderboardList.innerHTML = "";
        return;
    }

    elements.leaderboardList.innerHTML = "";

    if (isOnline) {
        elements.leaderboardKicker.textContent = state.mode === "public" ? "Public arena" : "Online players";
        elements.leaderboardTitle.textContent = room.game_phase === "intermission"
            ? `Intermission ${room.intermission_seconds_remaining}s`
            : (state.mode === "public" ? `${room.difficulty} arena` : (state.roomCode ? `Lobby ${state.roomCode}` : "Live lobby"));

        room.leaderboard.forEach((entry, index) => {
            const row = document.createElement("div");
            row.className = "leaderboard-row";
            if (entry.session_id === state.sessionId) {
                row.classList.add("is-self");
            }
            if (entry.is_eliminated) {
                row.classList.add("is-out");
            }

            const rank = document.createElement("span");
            rank.className = "leaderboard-rank";
            rank.textContent = entry.is_eliminated ? "X" : `#${index + 1}`;

            const name = document.createElement("span");
            name.className = "leaderboard-name";
            name.textContent = entry.player_name;

            const stats = document.createElement("span");
            stats.className = "leaderboard-stats";
            const matchResult = room.last_match_results ? room.last_match_results[entry.session_id] : null;
            if (room.game_phase === "intermission" && matchResult && typeof matchResult.elo === "number") {
                stats.textContent = `Rank ${matchResult.rank} | ${Math.round(matchResult.elo)} ELO`;
            } else if (room.game_phase === "intermission" && matchResult) {
                stats.textContent = `Rank ${matchResult.rank}`;
            } else if (entry.is_eliminated) {
                stats.textContent = "Eliminated";
            } else if (room.winner_session_id === entry.session_id) {
                stats.textContent = "Winner";
            } else if (entry.is_active) {
                stats.textContent = "Typing now";
            } else {
                stats.textContent = "Waiting";
            }

            row.append(rank, name, stats);
            elements.leaderboardList.append(row);
        });
        return;
    }

    elements.leaderboardKicker.textContent = "Local players";
    elements.leaderboardTitle.textContent = `${getAliveLocalPlayers().length} still in`;

    state.localGame.players.forEach((player, index) => {
        const row = document.createElement("div");
        row.className = "leaderboard-row";
        const isActive = index === state.localGame.activePlayerIndex && !player.eliminated;
        if (isActive) {
            row.classList.add("is-self");
        }
        if (player.eliminated) {
            row.classList.add("is-out");
        }

        const rank = document.createElement("span");
        rank.className = "leaderboard-rank";
        rank.textContent = player.eliminated ? "X" : `#${index + 1}`;

        const name = document.createElement("span");
        name.className = "leaderboard-name";
        name.textContent = player.name;

        const stats = document.createElement("span");
        stats.className = "leaderboard-stats";
        if (player.eliminated) {
            stats.textContent = "Eliminated";
        } else if (isActive) {
            stats.textContent = "Now spelling";
        } else {
            stats.textContent = `${player.lastWpm} WPM`;
        }

        row.append(rank, name, stats);
        elements.leaderboardList.append(row);
    });
}

async function sendChatMessage() {
    if (!isSharedMultiplayerMode() || !state.sessionId) {
        return;
    }

    const message = elements.chatInput.value.trim();
    if (!message) {
        return;
    }

    elements.chatSendButton.disabled = true;
    try {
        const room = await api("/api/room/chat", {
            method: "POST",
            body: JSON.stringify({
                session_id: state.sessionId,
                message,
            }),
        });
        elements.chatInput.value = "";
        syncOnlineRoomState(room, { forceRound: false });
    } finally {
        elements.chatSendButton.disabled = false;
    }
}

async function refreshRoomState() {
    if (!state.roomCode) {
        return;
    }

    const room = await api(`/api/room-state?room_code=${encodeURIComponent(state.roomCode)}`);
    syncOnlineRoomState(room);
}

function startRoomPolling() {
    clearRoomPolling();
    if (!state.roomCode) {
        return;
    }

    state.roomPollId = window.setInterval(() => {
        refreshRoomState().catch((error) => {
            setFeedback("Room sync paused", error.message, "error");
            clearRoomPolling();
        });
    }, ROOM_POLL_INTERVAL_MS);
}

function speakCurrentWord(options = {}) {
    const { silent = false, gateTyping = false, focusInput = true } = options;
    if (!state.currentRound) {
        return;
    }

    const audioUrl = state.currentRound.audio_url;
    if (!audioUrl) {
        if (gateTyping) {
            unlockTyping({ focusInput });
        }
        if (!silent) {
            setFeedback("Audio unavailable", "No pre-generated audio was found for this word.", "error");
        }
        return;
    }

    if (gateTyping) {
        lockTyping();
    }

    stopCurrentAudio();
    const audio = new Audio(audioUrl);
    audio.preload = "auto";
    if (gateTyping) {
        state.typingUnlockTimeoutId = window.setTimeout(() => {
            unlockTyping({ focusInput });
        }, 1000);
        audio.addEventListener("ended", () => {
            unlockTyping({ focusInput });
        }, { once: true });
    }
    audio.addEventListener("error", () => {
        if (gateTyping) {
            unlockTyping({ focusInput });
        }
        if (!silent) {
            setFeedback("Audio error", "Unable to play the pre-generated audio for this word.", "error");
        }
    });
    state.currentAudio = audio;
    audio.play().catch(() => {
        if (gateTyping) {
            unlockTyping({ focusInput });
        }
        if (!silent) {
            setFeedback("Playback blocked", "Your browser blocked autoplay. Press Play again to retry.", "error");
        }
    });
}

function applyRound(round, options = {}) {
    const {
        autoplay = true,
        focusInput = true,
        preserveGuess = false,
    } = options;
    stopCurrentAudio();
    state.currentRound = round;
    state.roomCode = round.room_code || state.roomCode;
    state.roundStartedAt = 0;
    state.typingUnlockedAt = 0;
    state.typingReady = false;
    state.timeoutSkipInFlight = false;
    elements.definitionText.textContent = round.definition;
    elements.partOfSpeech.textContent = round.part_of_speech;
    elements.playerLabel.textContent = round.player_name;
    elements.difficultyBadge.textContent = round.difficulty;
    elements.difficultyBadge.style.backgroundColor = round.difficulty_color;
    if (!preserveGuess) {
        elements.guessInput.value = "";
    }
    lockTyping();
    hideWpmFlash();
    clearBoardTone();
    resizeBoard();
    centerBoardText();
    updateStatusHeader();
    updateStreakBadge();
    if (autoplay) {
        window.setTimeout(() => {
            speakCurrentWord({ silent: true, gateTyping: true, focusInput });
        }, 110);
    } else {
        unlockTyping({ focusInput });
    }
}

function onlineRoundKey(room) {
    if (!room || !room.active_round) {
        return null;
    }
    return `${room.active_session_id || ""}:${room.active_round.pronunciation}:${room.active_round.definition}`;
}

function syncOnlineRoomState(room, options = {}) {
    const { forceRound = false } = options;
    if (!room) {
        return;
    }

    const previousRoom = state.onlineRoom;
    const previousActiveSessionId = previousRoom ? previousRoom.active_session_id : null;
    const previousWinnerSessionId = previousRoom ? previousRoom.winner_session_id : null;
    state.onlineRoom = room;
    renderLeaderboard(room);
    renderChat(room);

    if (room.game_phase === "intermission") {
        lockTyping();
        elements.guessInput.value = "";
        centerBoardText();
        const previousIntermission = previousRoom && previousRoom.game_phase === "intermission"
            ? previousRoom.intermission_seconds_remaining
            : null;
        if (previousIntermission !== room.intermission_seconds_remaining || forceRound) {
            const ownResult = room.last_match_results ? room.last_match_results[state.sessionId] : null;
            const resultLine = ownResult
                ? (typeof ownResult.elo === "number"
                    ? ` Your rank was ${ownResult.rank} and your ELO is now ${Math.round(ownResult.elo)}.`
                    : ` Your rank was ${ownResult.rank}.`)
                : "";
            setFeedback(
                room.winner_session_id === state.sessionId ? "You win" : "Intermission",
                `${room.winner_name || "The winner"} took the game. Next round starts in ${room.intermission_seconds_remaining}s.${resultLine}`,
                room.winner_session_id === state.sessionId ? "success" : "error",
            );
        }
        updateRoundTimer();
        return;
    }

    const round = room.active_round;
    if (!round) {
        return;
    }

    const previousRoundKey = state.lastOnlineRoundKey;
    const nextRoundKey = onlineRoundKey(room);
    const wasTypist = previousActiveSessionId === state.sessionId && !previousWinnerSessionId;
    const canType = room.active_session_id === state.sessionId && !room.winner_session_id;
    const roundChanged = forceRound || previousRoundKey !== nextRoundKey;

    if (roundChanged) {
        applyRound(round, {
            autoplay: true,
            focusInput: canType,
            preserveGuess: false,
        });
        state.lastOnlineRoundKey = nextRoundKey;
    }

    state.onlineTurnRemainingSeconds = Number(room.turn_seconds_remaining || 0);
    state.onlineTurnTotalSeconds = Number(room.turn_time_limit_seconds || 0);
    state.onlineTurnSyncedAt = Date.now();
    updateRoundTimer();

    if (!canType || roundChanged) {
        elements.guessInput.value = room.draft_text || "";
        centerBoardText();
    }

    elements.guessInput.disabled = !canType;
    elements.skipButton.disabled = !canType;
    elements.submitButton.disabled = !canType;

    if (room.winner_session_id) {
        if (previousWinnerSessionId !== room.winner_session_id || forceRound) {
            const won = room.winner_session_id === state.sessionId;
            setFeedback(
                won ? "You win" : "Round over",
                room.winner_name ? `${room.winner_name} wins.` : "The room has finished.",
                won ? "success" : "error",
            );
        }
        return;
    }

    if (canType) {
        if (!wasTypist || roundChanged) {
            elements.guessInput.focus();
            setFeedback("Your turn", "The board is live for you now. Type while the others watch.");
        }
    } else if (room.active_player_name && (previousActiveSessionId !== room.active_session_id || roundChanged)) {
        setFeedback("Watching turn", `${room.active_player_name} is spelling right now. The board unlocks again on your turn.`);
    }
}

function queueRoomDraftSync() {
    if (!isSharedMultiplayerMode() || !state.onlineRoom || state.onlineRoom.active_session_id !== state.sessionId) {
        return;
    }

    clearRoomDraftSync();
    state.roomDraftTimeoutId = window.setTimeout(() => {
        api("/api/room/draft", {
            method: "POST",
            body: JSON.stringify({
                session_id: state.sessionId,
                draft_text: elements.guessInput.value,
            }),
        }).catch(() => {
            // Keep typing responsive even if one poll/update misses.
        });
    }, 90);
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

async function logoutAccount() {
    try {
        await api("/api/auth/logout", { method: "POST" });
    } catch {
        // Local logout should still succeed even if the server token already expired.
    }
    persistAuthToken("");
    state.account = null;
    updateAuthUi();
    setFeedback("Logged out", "Public multiplayer now requires signing in again.");
}

async function loadConfig() {
    const config = await api("/api/config");
    state.config = config;

    elements.catalogSummary.textContent = `${config.total_words} words across ${config.difficulties.length} difficulties.`;
    state.selectedDifficulty = config.difficulties[0]?.name || "";
    renderDifficultyPicker();
}

async function startSoloSession() {
    const playerName = elements.playerName.value.trim();
    const payload = await api("/api/session", {
        method: "POST",
        body: JSON.stringify({
            player_name: playerName,
            difficulty: getSelectedDifficulty(),
        }),
    });

    state.mode = "solo";
    state.sessionId = payload.session_id;
    state.roomCode = null;
    state.streak = 0;
    state.lastWpm = 0;
    state.localGame = null;
    showGamePanel();
    applyRound(payload.round);
    renderLeaderboard();
    setFeedback("Round ready", "Audio plays automatically. Finish the word to see its WPM.");
}

async function startOnlineSession() {
    const playerName = elements.playerName.value.trim();
    const roomCode = elements.roomCodeInput.value.trim().toUpperCase();
    const isJoining = Boolean(roomCode);
    const endpoint = isJoining ? "/api/room/join" : "/api/room";
    const body = isJoining
        ? { player_name: playerName, room_code: roomCode }
        : { player_name: playerName, difficulty: getSelectedDifficulty() };

    const payload = await api(endpoint, {
        method: "POST",
        body: JSON.stringify(body),
    });

    state.mode = "online";
    state.sessionId = payload.session_id;
    state.roomCode = payload.room ? payload.room.room_code : roomCode;
    state.streak = 0;
    state.lastWpm = 0;
    state.onlineRoom = null;
    state.lastOnlineRoundKey = null;
    state.localGame = null;
    showGamePanel();
    syncOnlineRoomState(payload.room || null, { forceRound: true });
    startRoomPolling();
    setFeedback(
        isJoining ? "Lobby joined" : "Lobby created",
        isJoining
            ? `You joined ${state.roomCode}.`
            : `Share code ${state.roomCode} so another player can join.`,
    );
}

async function startPublicSession() {
    if (!state.account) {
        throw new Error("Create an account or log in before joining public multiplayer.");
    }

    const payload = await api("/api/public-room", {
        method: "POST",
        body: JSON.stringify({
            difficulty: getSelectedDifficulty(),
        }),
    });

    state.mode = "public";
    state.sessionId = payload.session_id;
    state.roomCode = payload.room ? payload.room.room_code : null;
    state.streak = 0;
    state.lastWpm = 0;
    state.onlineRoom = null;
    state.lastOnlineRoundKey = null;
    state.localGame = null;
    showGamePanel();
    syncOnlineRoomState(payload.room || null, { forceRound: true });
    startRoomPolling();
    setFeedback(
        "Public arena joined",
        `You entered the ${getSelectedDifficulty()} arena as ${state.account.username}.`,
        "success",
    );
}

async function startLocalMultiplayer() {
    const names = getLocalPlayerNames();
    if (names.length < LOCAL_PLAYER_MIN) {
        throw new Error("Choose at least two local players.");
    }

    const difficulty = getSelectedDifficulty();
    const players = await Promise.all(
        names.map(async (name) => {
            const payload = await api("/api/session", {
                method: "POST",
                body: JSON.stringify({
                    player_name: name,
                    difficulty,
                    local_mode: true,
                }),
            });

            return {
                name,
                sessionId: payload.session_id,
                round: payload.round,
                eliminated: false,
                lastWpm: 0,
            };
        }),
    );

    state.mode = "local";
    state.sessionId = players[0].sessionId;
    state.roomCode = null;
    state.streak = 0;
    state.lastWpm = 0;
    state.localGame = {
        difficulty,
        activePlayerIndex: 0,
        finished: false,
        intermissionUntil: null,
        intermissionTimerId: null,
        players,
    };

    showGamePanel();
    applyRound(players[0].round);
    renderLeaderboard();
    setFeedback("Local games started", `${players[0].name} spells first. A wrong answer or skip eliminates the player.`);
}

function getCurrentSessionId() {
    if (state.mode === "local") {
        if (state.localGame && state.localGame.finished) {
            return null;
        }
        const player = getActiveLocalPlayer();
        return player ? player.sessionId : null;
    }
    return state.sessionId;
}

function clearLocalIntermissionTimer() {
    if (state.localGame && state.localGame.intermissionTimerId) {
        window.clearInterval(state.localGame.intermissionTimerId);
        state.localGame.intermissionTimerId = null;
    }
}

async function restartLocalMultiplayer() {
    if (!state.localGame) {
        return;
    }

    clearLocalIntermissionTimer();
    const names = state.localGame.players.map((player) => player.name);
    const difficulty = state.localGame.difficulty;
    const players = await Promise.all(
        names.map(async (name) => {
            const payload = await api("/api/session", {
                method: "POST",
                body: JSON.stringify({
                    player_name: name,
                    difficulty,
                    local_mode: true,
                }),
            });

            return {
                name,
                sessionId: payload.session_id,
                round: payload.round,
                eliminated: false,
                lastWpm: 0,
            };
        }),
    );

    state.sessionId = players[0].sessionId;
    state.lastWpm = 0;
    state.localGame = {
        ...state.localGame,
        activePlayerIndex: 0,
        finished: false,
        intermissionUntil: null,
        intermissionTimerId: null,
        players,
    };

    applyRound(players[0].round);
    renderLeaderboard();
    setFeedback("Next local round", `${players[0].name} starts the next elimination game.`);
}

function startLocalIntermission(winner, tone) {
    if (!state.localGame) {
        return;
    }

    clearLocalIntermissionTimer();
    lockTyping();
    state.localGame.finished = true;
    state.localGame.intermissionUntil = Date.now() + 15000;

    const renderCountdown = () => {
        if (!state.localGame || !state.localGame.intermissionUntil) {
            return;
        }
        const secondsLeft = Math.max(0, Math.ceil((state.localGame.intermissionUntil - Date.now()) / 1000));
        setFeedback(
            "Local intermission",
            `${winner ? winner.name : "Winner"} took the round. Next local game starts in ${secondsLeft}s.`,
            tone,
        );
        if (secondsLeft <= 0) {
            restartLocalMultiplayer().catch((error) => setFeedback("Local restart failed", error.message, "error"));
        }
    };

    renderCountdown();
    state.localGame.intermissionTimerId = window.setInterval(renderCountdown, 1000);
}

async function advanceLocalTurn(resultMessage, tone) {
    const alivePlayers = getAliveLocalPlayers();
    if (alivePlayers.length <= 1) {
        const winner = alivePlayers[0];
        state.sessionId = null;
        elements.guessInput.disabled = true;
        elements.skipButton.disabled = true;
        elements.submitButton.disabled = true;
        renderLeaderboard();
        startLocalIntermission(winner, tone);
        return;
    }

    const nextPlayer = selectNextLocalPlayer();
    if (!nextPlayer) {
        return;
    }

    state.sessionId = nextPlayer.sessionId;
    state.streak = 0;
    applyRound(nextPlayer.round);
    renderLeaderboard();
    setFeedback(
        nextPlayer.eliminated ? "Next player" : `${nextPlayer.name}'s turn`,
        `${resultMessage} ${nextPlayer.name} is up next.`,
        tone,
    );
}

async function submitGuess(event) {
    event.preventDefault();
    const sessionId = getCurrentSessionId();
    if (!sessionId) {
        return;
    }

    const guess = elements.guessInput.value.trim();
    if (!guess) {
        setFeedback("Missing answer", "Type a spelling before submitting.", "error");
        return;
    }

    const wordWpm = calculateWpm(guess);
    state.lastWpm = wordWpm;

    const payload = await api("/api/guess", {
        method: "POST",
        body: JSON.stringify({
            session_id: sessionId,
            guess,
            wpm: wordWpm,
        }),
    });

    if (state.mode === "local") {
        const activePlayer = getActiveLocalPlayer();
        if (!activePlayer) {
            return;
        }

        activePlayer.lastWpm = wordWpm;
        activePlayer.round = payload.round;

        if (payload.result.correct) {
            setBoardTone("board-correct");
            showWpmFlash(wordWpm, "success");
            const message = `${activePlayer.name} spelled it correctly at ${wordWpm} WPM.`;
            window.setTimeout(() => {
                advanceLocalTurn(message, "success").catch((error) => setFeedback("Turn change failed", error.message, "error"));
            }, 320);
        } else {
            activePlayer.eliminated = true;
            setBoardTone("board-incorrect");
            showWpmFlash(wordWpm, "error");
            const message = `${activePlayer.name} is out. The answer was "${payload.result.answer}" at ${wordWpm} WPM.`;
            window.setTimeout(() => {
                advanceLocalTurn(message, "error").catch((error) => setFeedback("Turn change failed", error.message, "error"));
            }, 320);
        }

        updateStatusHeader();
        renderLeaderboard();
        return;
    }

    if (isSharedMultiplayerMode()) {
        if (payload.result.correct) {
            setBoardTone("board-correct");
            showWpmFlash(wordWpm, "success");
            setFeedback("Correct", `${wordWpm} WPM. You stay alive and the turn passes on.`, "success");
        } else {
            setBoardTone("board-incorrect");
            showWpmFlash(wordWpm, "error");
            setFeedback("Eliminated", `The answer was "${payload.result.answer}". ${wordWpm} WPM on that attempt.`, "error");
        }

        window.setTimeout(() => {
            syncOnlineRoomState(payload.room, { forceRound: true });
        }, 320);
        return;
    }

    if (payload.result.correct) {
        state.streak += 1;
        setBoardTone("board-correct");
        const acceptanceNote = payload.result.accepted_as_homophone
            ? `Accepted via homophone "${payload.result.matched_spelling}". `
            : "";
        setFeedback(
            "Correct",
            `${acceptanceNote}${wordWpm} WPM. Next word loaded automatically.`,
            "success",
        );
        showWpmFlash(wordWpm, "success");
    } else {
        state.streak = 0;
        setBoardTone("board-incorrect");
        setFeedback(
            "Not quite",
            `The prompt answer was "${payload.result.answer}". ${wordWpm} WPM on that attempt. A new word is ready.`,
            "error",
        );
        showWpmFlash(wordWpm, "error");
    }

    updateStatusHeader();
    updateStreakBadge();

    window.setTimeout(() => {
        applyRound(payload.round);
        renderLeaderboard(payload.room || null);
    }, 320);
}

async function skipWord() {
    const sessionId = getCurrentSessionId();
    if (!sessionId) {
        return;
    }

    const wordWpm = calculateWpm(elements.guessInput.value);
    state.lastWpm = wordWpm;

    const payload = await api("/api/skip", {
        method: "POST",
        body: JSON.stringify({
            session_id: sessionId,
        }),
    });

    if (state.mode === "local") {
        const activePlayer = getActiveLocalPlayer();
        if (!activePlayer) {
            return;
        }

        activePlayer.lastWpm = wordWpm;
        activePlayer.round = payload.round;
        activePlayer.eliminated = true;
        setBoardTone("board-incorrect");
        showWpmFlash(wordWpm, "error");
        renderLeaderboard();
        window.setTimeout(() => {
            advanceLocalTurn(
                `${activePlayer.name} skipped and was eliminated. The answer was "${payload.skipped_answer}".`,
                "error",
            ).catch((error) => setFeedback("Turn change failed", error.message, "error"));
        }, 320);
        updateStatusHeader();
        return;
    }

    if (isSharedMultiplayerMode()) {
        setBoardTone("board-incorrect");
        showWpmFlash(wordWpm, "error");
        setFeedback("Eliminated", `You skipped. The answer was "${payload.skipped_answer}".`, "error");
        window.setTimeout(() => {
            syncOnlineRoomState(payload.room, { forceRound: true });
        }, 320);
        return;
    }

    state.streak = 0;
    setBoardTone("board-incorrect");
    showWpmFlash(wordWpm, "error");
    updateStatusHeader();
    updateStreakBadge();
    setFeedback("Word skipped", `The answer was "${payload.skipped_answer}". ${wordWpm} WPM for that round.`, "error");
    window.setTimeout(() => {
        applyRound(payload.round);
        renderLeaderboard(payload.room || null);
    }, 320);
}

elements.soloButton.addEventListener("click", () => {
    startSoloSession().catch((error) => setFeedback("Solo start failed", error.message, "error"));
});

elements.onlineButton.addEventListener("click", () => {
    startOnlineSession().catch((error) => setFeedback("Lobby start failed", error.message, "error"));
});

elements.publicButton.addEventListener("click", () => {
    startPublicSession().catch((error) => setFeedback("Public start failed", error.message, "error"));
});

elements.logoutButton.addEventListener("click", () => {
    logoutAccount().catch((error) => setFeedback("Logout failed", error.message, "error"));
});

elements.localToggleButton.addEventListener("click", () => {
    const willShow = elements.localSetup.classList.contains("hidden");
    setLocalSetupVisible(willShow);
});

elements.roomCodeInput.addEventListener("input", updateOnlineButtonLabel);
elements.addLocalPlayerButton.addEventListener("click", () => {
    state.localPlayerSlots = Math.min(LOCAL_PLAYER_MAX, state.localPlayerSlots + 1);
    renderLocalPlayerInputs();
});
elements.removeLocalPlayerButton.addEventListener("click", () => {
    state.localPlayerSlots = Math.max(LOCAL_PLAYER_MIN, state.localPlayerSlots - 1);
    renderLocalPlayerInputs();
});
elements.playerName.addEventListener("input", () => {
    if (elements.playerName.value !== state.lastAutoPlayerName) {
        state.lastAutoPlayerName = "";
    }
});

elements.startLocalButton.addEventListener("click", () => {
    startLocalMultiplayer().catch((error) => setFeedback("Local start failed", error.message, "error"));
});

elements.answerForm.addEventListener("submit", (event) => {
    submitGuess(event).catch((error) => setFeedback("Submit failed", error.message, "error"));
});

elements.guessInput.addEventListener("input", () => {
    clearBoardTone();
    centerBoardText();
    queueRoomDraftSync();
});

elements.guessInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        elements.answerForm.requestSubmit();
    }
});

elements.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    sendChatMessage().catch((error) => setFeedback("Chat failed", error.message, "error"));
});

elements.speakButton.addEventListener("click", speakCurrentWord);
elements.skipButton.addEventListener("click", () => {
    skipWord().catch((error) => setFeedback("Skip failed", error.message, "error"));
});

elements.menuButton.addEventListener("click", () => {
    forfeitCurrentOnlineSession().finally(() => {
        returnToMenu({ clearInputs: false });
    });
});

elements.exitButton.addEventListener("click", () => {
    forfeitCurrentOnlineSession().finally(() => {
        returnToMenu({ clearInputs: true });
    });
});

window.addEventListener("beforeunload", clearRoomPolling);
window.addEventListener("beforeunload", stopCurrentAudio);
window.addEventListener("beforeunload", beaconForfeitCurrentOnlineSession);

updateStatusHeader();
updateStreakBadge();
resizeBoard();
centerBoardText();
renderLocalPlayerInputs();
updateOnlineButtonLabel();
updateAuthUi();
Promise.all([
    loadConfig(),
    loadAuthStatus(),
]).catch((error) => {
    setFeedback("Load failed", error.message, "error");
    elements.catalogSummary.textContent = "Could not load the word catalog.";
});

