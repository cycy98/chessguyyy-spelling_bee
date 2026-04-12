import math


def elo_last_man_standing(players, k1=32, k2=0.01):
    """
    players: list of dicts like
        {"name": str, "elo": float, "rank": int}
        rank = 1 for first place, 2 for second, etc.

    returns updated player list
    """
    n = len(players)

    rank_norm = n * (n - 1) / 2
    denom = sum(math.exp(k2 * p["elo"]) for p in players)

    deltas = []
    for p in players:
        x = p["elo"]
        y = p["rank"]
        pred = math.exp(k2 * x) / denom
        r = (n - y) / rank_norm
        delta = k1 * (r - pred)
        deltas.append(delta)

    for p, d in zip(players, deltas):
        p["elo"] += d

    return players
