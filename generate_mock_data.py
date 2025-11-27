#!/usr/bin/env python3
"""
Mock game data generator for the provided schema.sql.

This script will:

1. Create a SQLite database (default: mock_game.db)
2. Apply the provided schema.sql file.
3. Generate a synthetic dataset for roughly one year of activity:
   - players
   - teams & team_memberships
   - daily DAU curve with seasonal and patch effects
   - sessions
   - events (match loop + currency deltas)
   - purchases (with whales / dolphins / minnows)
   - experiment_assignments

The goal is to be:
- Realistic enough to run meaningful analyses.
- Small enough to run comfortably on a laptop (minutes, not hours; DB size
  well below tens of GB).

If you need a bigger or smaller dataset, tweak the knobs in the
"Global config / knobs" section.
"""

import math
import random
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional

# ---------------------------------------
# Global config / knobs
# ---------------------------------------

DB_PATH = Path("mock_game2.db")
SCHEMA_PATH = Path("schema.sql")

START_DATE = datetime(2025, 1, 1)

# ---------------------------------------------------------------------
# Data volume knobs:
# - NUM_DAYS: length of the simulated period in days
# - N_PLAYERS: total number of unique players ever created
# - MAX_DAU: "design" peak DAU target; the realised DAU is
#            min(target, number of available (non-churned) players).
#
# These defaults are tuned for laptop-friendly generation: the script
# should complete in a few minutes and produce a database well below
# ~1 GB instead of tens of gigabytes.
# ---------------------------------------------------------------------
NUM_DAYS = 180          # simulate ~6 months instead of a full year
N_PLAYERS = 40_000      # total player base (down from 200k)
MAX_DAU   = 15_000      # peak daily active users (down from 100k)

# Patch cadence: roughly every 60 days (used for DAU spikes)
PATCH_DAYS = [60, 120, 180]

# Random seeds for reproducibility (feel free to tweak/remove)
RANDOM_SEED = 131287

# ---------------------------------------
# Utility functions
# ---------------------------------------

def daterange(start: datetime, days: int) -> List[datetime]:
    """Return a list of day-start datetime objects from start for `days` days."""
    return [start + timedelta(days=i) for i in range(days)]


def choose_weighted(choices: List[Tuple[object, float]]) -> object:
    """
    Randomly choose one element from a list of (value, weight) pairs.
    We assume weights are non-negative. If all weights are zero, choose uniformly.
    """
    total = sum(w for _, w in choices)
    if total <= 0:
        # all weights zero -> fallback to uniform
        return random.choice([v for v, _ in choices])
    r = random.random() * total
    upto = 0.0
    for value, weight in choices:
        upto += weight
        if r <= upto:
            return value
    # Fallback
    return choices[-1][0]


def normal_clamp(mu: float, sigma: float, lo: float, hi: float) -> float:
    """Draw from N(mu, sigma^2) and clamp to [lo, hi]."""
    x = random.gauss(mu, sigma)
    return max(lo, min(hi, x))


def pick_country_and_tz() -> Tuple[str, int]:
    """
    Rough-and-ready country + timezone selection.

    We overweight US/NA and Western Europe slightly, and add a bucket for "OTHER"
    so that the dataset is not too Euro/US centric but still has a clear majority.
    """
    choices = [
        ("US", -5),
        ("US", -8),
        ("CA", -5),
        ("GB", 0),
        ("DE", 1),
        ("FR", 1),
        ("BR", -3),
        ("IN", 5),
        ("JP", 9),
        ("KR", 9),
        ("OTHER", 0),
    ]
    weights = [0.12, 0.08, 0.04, 0.08, 0.06, 0.06, 0.08, 0.08, 0.08, 0.08, 0.24]
    assert len(choices) == len(weights)
    total = sum(weights)
    r = random.random() * total
    upto = 0.0
    for (cc, tz), w in zip(choices, weights):
        upto += w
        if r <= upto:
            return cc, tz
    return choices[-1]


# ---------------------------------------
# DB init
# ---------------------------------------

def init_db(conn: sqlite3.Connection, schema_path: Path) -> None:
    """Create all tables and indexes from schema.sql."""
    with schema_path.open("r", encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)
    # Make sure foreign keys are enforced
    conn.execute("PRAGMA foreign_keys = ON;")


# ---------------------------------------
# Data model in memory (for generation)
# ---------------------------------------

class PlayerMeta:
    """Lightweight in-memory representation of a player for simulation.

    This is intentionally kept very small (via __slots__) so that we can
    keep tens of thousands of players in memory without allocating large
    Python dicts for each.
    """
    __slots__ = (
        "player_id",
        "created_at",
        "country_code",
        "platform",
        "time_zone_offset",
        "engagement_segment",
        "spend_segment",
        "level",
        "total_spend_eur",
        "churn_day_idx",  # last day index on which this player can be active
    )

    def __init__(
        self,
        player_id: int,
        created_at: datetime,
        country_code: str,
        platform: str,
        time_zone_offset: int,
        engagement_segment: str,
        spend_segment: str,
        churn_day_idx: int,
    ):
        self.player_id = player_id
        self.created_at = created_at
        self.country_code = country_code
        self.platform = platform
        self.time_zone_offset = time_zone_offset  # hours
        self.engagement_segment = engagement_segment  # 'casual', 'midcore', 'heavy'
        self.spend_segment = spend_segment  # 'nonpayer', 'minnow', 'dolphin', 'whale'
        self.level = 1
        self.total_spend_eur = 0.0
        # Index of the last simulated day (0-based) on which this player
        # can still appear as active. After this they are treated as fully
        # churned and removed from the active pool.
        self.churn_day_idx = churn_day_idx


# ---------------------------------------
# DAU curve with seasonality + patches
# ---------------------------------------

def generate_dau_curve(num_days: int) -> List[int]:
    """
    Build a daily DAU curve with a more realistic "new game" shape:

    - Big launch spike on the first days.
    - Fast early decay during the first ~2–3 weeks.
    - Then a slower decay towards a long-term retention plateau.
    - Seasonal effects (summer dip, Christmas spike) are layered on top.
    - Patch spikes every ~60 days add short-lived boosts.

    The function returns absolute DAU targets (clamped to [0, MAX_DAU])
    for each simulated day.
    """
    rel: List[float] = []

    # Characteristic time scale for the early post-launch decay.
    # Roughly "how many days until the curve has dropped most of the way
    # from the launch spike towards the long-term plateau".
    tau_fast = max(7.0, num_days * 0.10)   # ~10 % of the period, min ~1 week

    # Long-term floor for DAU in relative units; after normalisation
    # this will still be scaled to MAX_DAU, but the ratio launch/floor
    # controls how steep the curve feels.
    long_term_floor = 0.6   # 60 % of the eventual normalised max

    # Extra height of the launch spike above the long-term floor.
    launch_spike_height = 0.8  # gives 1.4 at t=0 (0.6 + 0.8)

    for t in range(num_days):
        # --- Base post-launch decay ---
        # At t=0: base ≈ long_term_floor + launch_spike_height
        # As t → ∞: base → long_term_floor
        base = long_term_floor + launch_spike_height * math.exp(-t / tau_fast)

        # --- Seasonal factor (mapped from "day-of-year" idea) ---
        day_of_year = int(t * (365.0 / max(1, num_days)))
        season_mult = 1.0
        # Rough summer window: 150–240 (lower activity)
        if 150 <= day_of_year <= 240:
            season_mult *= 0.8  # -20 %
        # Christmas period: 330–364 (higher activity)
        if 330 <= day_of_year <= 364:
            season_mult *= 1.3  # +30 %

        # --- Patch spikes every ~60 days ---
        patch_mult = 1.0
        for pd in PATCH_DAYS:
            if pd < 0 or pd >= num_days:
                continue
            dist = abs(t - pd)
            if dist == 0:
                patch_mult *= 1.4
            elif dist == 1:
                patch_mult *= 1.2
            elif dist == 2:
                patch_mult *= 1.1

        # Small multiplicative noise so the curve is not perfectly smooth
        noise = random.uniform(0.95, 1.05)

        rel.append(base * season_mult * patch_mult * noise)

    # Normalise so the maximum relative value becomes 1.0,
    # then scale to the configured MAX_DAU.
    max_rel = max(rel)
    scaled = [int(round(MAX_DAU * (x / max_rel))) for x in rel]
    return scaled


# ---------------------------------------
# Player generation
# ---------------------------------------

def generate_players(num_players: int) -> List[PlayerMeta]:
    """
    Generate players with:
    - creation date spread across the simulated period
    - country/platform/timezone
    - engagement segment
    - spend segment
    - a simple lifetime / churn model

    The churn model is important for realistic WAU/MAU behaviour:
    most players drop out within a few weeks, while a minority of
    "core" users stay active for the whole window.
    """
    players: List[PlayerMeta] = []

    for pid in range(1, num_players + 1):
        # Creation date: heavily front-loaded around launch, with a tail.
        #
        # We don't want players to appear uniformly across the whole window,
        # otherwise early DAU will ramp up simply because there are not yet
        # enough accounts created.
        u = random.random()
        if u < 0.60:
            # ~60% of players arrive in the first 4 days (huge launch spike)
            offset_days = random.randint(0, 3)
        elif u < 0.85:
            # ~25% arrive over the next month
            offset_days = random.randint(4, min(30, NUM_DAYS - 1))
        elif u < 0.95:
            # ~10% arrive over the following 2 months
            offset_days = random.randint(31, min(90, NUM_DAYS - 1))
        else:
            # ~5% trickle in over the rest of the period
            offset_days = random.randint(91, NUM_DAYS - 1)

        offset_days = max(0, min(offset_days, NUM_DAYS - 1))

        created_at = START_DATE + timedelta(days=offset_days)
        birth_day_idx = offset_days  # 0-based index

        country, tz = pick_country_and_tz()
        platform = random.choices(["Android", "iOS"], weights=[0.6, 0.4])[0]

        # Engagement segments: mostly casual, some midcore, few heavy.
        engagement_segment = random.choices(
            ["casual", "midcore", "heavy"],
            weights=[0.7, 0.2, 0.1],
        )[0]

        # Spend segments:
        # approx: whales 0.2%, dolphins 1.8%, minnows 8%, rest non-payer
        r = random.random()
        if r < 0.002:
            spend_segment = "whale"
        elif r < 0.002 + 0.018:
            spend_segment = "dolphin"
        elif r < 0.002 + 0.018 + 0.08:
            spend_segment = "minnow"
        else:
            spend_segment = "nonpayer"

        # --- Simple lifetime / churn model ---
        # A small fraction of players are long-lived and effectively
        # never churn within the simulated window. Everyone else has an
        # exponential-like lifetime in days.
        if random.random() < 0.15:
            # ~15% "core" users: active until the end of the window.
            churn_day_idx = NUM_DAYS - 1
        else:
            # Others: geometric / exponential style lifetime
            mean_lifetime_days = 45.0
            extra_life = max(1, int(random.expovariate(1.0 / mean_lifetime_days)))
            churn_day_idx = min(NUM_DAYS - 1, birth_day_idx + extra_life)

        p = PlayerMeta(
            player_id=pid,
            created_at=created_at,
            country_code=country,
            platform=platform,
            time_zone_offset=tz,
            engagement_segment=engagement_segment,
            spend_segment=spend_segment,
            churn_day_idx=churn_day_idx,
        )
        players.append(p)

    return players


def insert_players(conn: sqlite3.Connection, players: List[PlayerMeta]) -> None:
    """
    Insert players into the players table.
    Only uses fields available in the schema; enrichment lives in PlayerMeta.
    """
    sql = """
        INSERT INTO players (
            player_id, created_at_utc, country_code, platform,
            device_model, os_version,
            acquisition_channel, acquisition_campaign,
            language_code, time_zone
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    channels = ["Organic", "AdsNetworkA", "AdsNetworkB", "CrossPromo"]
    campaigns = ["", "Launch2025", "SummerEvent", "HolidayPush"]

    rows = []
    for p in players:
        channel = random.choices(channels, weights=[0.5, 0.2, 0.2, 0.1])[0]
        if channel == "Organic":
            campaign = ""
        else:
            campaign = random.choice(campaigns)
        lang = "en" if p.country_code in {"US", "GB", "CA"} else "en"
        rows.append((
            p.player_id,
            p.created_at.isoformat(timespec="seconds") + "Z",
            p.country_code,
            p.platform,
            f"{p.platform}_Device_{random.randint(1, 5)}",
            f"{random.randint(12, 16)}.0",
            channel,
            campaign,
            lang,
            f"UTC{p.time_zone_offset:+d}",
        ))
    conn.executemany(sql, rows)
    conn.commit()


# ---------------------------------------
# Teams & memberships
# ---------------------------------------

def generate_teams_and_memberships(
    conn: sqlite3.Connection,
    players: List[PlayerMeta],
) -> None:
    """
    Generate teams with a tri-modal size distribution:
    - Solo players or tiny groups (1–5)
    - 25-player mid-sized teams
    - 45-player large teams (slightly below the max guild size)
    """
    # We first decide how many teams we need to roughly cover players,
    # then assign players into teams while respecting the desired size
    # distribution.
    team_rows = []
    membership_rows = []

    next_team_id = 1
    next_membership_id = 1

    shuffled_players = players[:]
    random.shuffle(shuffled_players)

    i = 0
    while i < len(shuffled_players):
        # Decide team size bucket
        size_bucket = random.choices(
            ["small", "medium", "large"],
            weights=[0.7, 0.2, 0.1],
        )[0]
        if size_bucket == "small":
            max_members = random.randint(1, 5)
            tier_level = 0
        elif size_bucket == "medium":
            max_members = 25
            tier_level = 1
        else:
            # Large tier ~45, representing 45–50 guilds where
            # churn/filling causes a concentration at ~45.
            max_members = 45
            tier_level = 2

        if i >= len(shuffled_players):
            break
        remaining = len(shuffled_players) - i
        team_size = min(max_members, remaining)

        created_at = START_DATE + timedelta(days=random.randint(0, NUM_DAYS - 1))

        team_rows.append((
            next_team_id,
            created_at.isoformat(timespec="seconds") + "Z",
            None,  # disbanded_at_utc
            tier_level,
            max_members,
        ))

        # Assign players to this team
        for j in range(team_size):
            p = shuffled_players[i + j]
            joined_at = p.created_at + timedelta(days=random.randint(0, 60))
            membership_rows.append((
                next_membership_id,
                p.player_id,
                next_team_id,
                joined_at.isoformat(timespec="seconds") + "Z",
                None,  # left_at_utc
                1 if j == 0 else 0,  # leader flag
            ))
            next_membership_id += 1

        next_team_id += 1
        i += team_size

    conn.executemany(
        """
        INSERT INTO teams (
            team_id, created_at_utc, disbanded_at_utc,
            tier_level, max_members
        ) VALUES (?, ?, ?, ?, ?)
        """,
        team_rows,
    )
    conn.executemany(
        """
        INSERT INTO team_memberships (
            membership_id, player_id, team_id,
            joined_at_utc, left_at_utc, is_leader
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        membership_rows,
    )
    conn.commit()


# ---------------------------------------
# Experiments
# ---------------------------------------

def generate_experiments(conn: sqlite3.Connection, players: List[PlayerMeta]) -> None:
    """
    Very simple A/B/C control experiment assignment:
    - experiment_name: "shop_pricing_v1"
    - variants: Control / A / B
    - assigned_at_utc: creation date (for simplicity)
    """
    sql = """
        INSERT INTO experiment_assignments (
            experiment_name, player_id, variant, assigned_at_utc
        ) VALUES (?, ?, ?, ?)
    """
    rows = []
    for p in players:
        variant = random.choices(
            ["Control", "A", "B"],
            weights=[0.5, 0.25, 0.25],
        )[0]
        rows.append((
            "shop_pricing_v1",
            p.player_id,
            variant,
            p.created_at.isoformat(timespec="seconds") + "Z",
        ))
    conn.executemany(sql, rows)
    conn.commit()


# ---------------------------------------
# Sessions, events, purchases
# ---------------------------------------

def generate_sessions_events_purchases(
    conn: sqlite3.Connection,
    players: List[PlayerMeta],
    dau_curve: List[int],
) -> None:
    """
    Generate sessions, events and purchases:
    - For each day, pick DAU(t) active players (only those created_on_or_before
      and not yet churned).
    - For each active player, generate a number of sessions depending on
      engagement segment.
    - For each session, generate a match-based event stream.
    - Occasionally generate purchases, strongly depending on spend segment.
    """
    # Sort players by creation date
    players_sorted = sorted(players, key=lambda p: p.created_at)
    player_by_id = {p.player_id: p for p in players}

    active_pool: List[PlayerMeta] = []
    idx_new = 0

    session_rows = []
    event_rows = []
    purchase_rows = []

    session_id_counter = 1
    event_id_counter = 1
    purchase_id_counter = 1
    match_id_counter = 1

    # We will batch-insert periodically
    BATCH_SIZE = 50_000

    # Helper to flush batches to DB
    def flush_batches():
        nonlocal session_rows, event_rows, purchase_rows
        if session_rows:
            conn.executemany(
                """
                INSERT INTO sessions (
                    session_id, player_id,
                    session_start_utc, session_end_utc,
                    duration_sec, client_version, build_number,
                    country_code, platform, entry_point,
                    season_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                session_rows,
            )
            session_rows = []
        if event_rows:
            conn.executemany(
                """
                INSERT INTO events (
                    event_id, player_id, session_id,
                    event_time_utc, event_type,
                    game_mode, match_outcome,
                    level, match_id,
                    soft_delta, soft_currency_purchased, hard_delta,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event_rows,
            )
            event_rows = []
        if purchase_rows:
            conn.executemany(
                """
                INSERT INTO purchases (
                    purchase_id, player_id, session_id,
                    purchase_time_utc, product_id, product_type,
                    currency_code, price_local, price_eur, quantity,
                    grants_soft_amount, grants_hard_amount,
                    platform, country_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                purchase_rows,
            )
            purchase_rows = []
        conn.commit()

    days = daterange(START_DATE, NUM_DAYS)

    for day_idx, day_start in enumerate(days):
        # Drop players who have fully churned before this day.
        # This keeps WAU/MAU from degenerating into a cumulative count of
        # "anyone who has ever played".
        if active_pool:
            active_pool = [p for p in active_pool if p.churn_day_idx >= day_idx]

        # Add newly created players to the active pool as their creation
        # date is reached.
        while (idx_new < len(players_sorted) and
               players_sorted[idx_new].created_at.date() <= day_start.date()):
            active_pool.append(players_sorted[idx_new])
            idx_new += 1

        if not active_pool:
            continue

        # The realised DAU for the day is the smaller of the DAU target
        # and the number of currently available (non-churned) players.
        target_dau = min(dau_curve[day_idx], len(active_pool))
        if target_dau <= 0:
            continue

        # Sample active players uniformly from eligible pool
        active_players_today = random.sample(active_pool, target_dau)

        # Determine a season_id: 6 seasons of ~2 months
        season_id = (day_idx // 60) + 1  # 1..7 roughly, last may be shorter

        for p in active_players_today:
            # How many sessions does this player have today?
            if p.engagement_segment == "casual":
                # 1-3 sessions, with heavy probability on 1-2
                sessions_today = random.choices(
                    [1, 2, 3], weights=[0.6, 0.3, 0.1]
                )[0]
            elif p.engagement_segment == "midcore":
                sessions_today = random.choices(
                    [1, 2, 3, 4], weights=[0.2, 0.4, 0.3, 0.1]
                )[0]
            else:  # heavy
                # Use a Pareto-like (power-law-ish) distribution, capped at 10
                raw = int(math.floor(random.paretovariate(2.0)))
                sessions_today = max(2, min(raw, 10))

            for _ in range(sessions_today):
                # Session local time ~ evening / late evening in player's TZ
                local_hour = random.choices(
                    [12, 15, 18, 20, 22],
                    weights=[0.1, 0.2, 0.4, 0.2, 0.1],
                )[0]
                local_minute = random.randint(0, 59)
                local_dt = day_start.replace(
                    hour=local_hour, minute=local_minute, second=0
                )
                session_start = local_dt - timedelta(hours=p.time_zone_offset)
                duration_sec = max(
                    60,
                    int(random.lognormvariate(math.log(600), 0.7)),
                )  # lognormal with median ~10 min
                session_end = session_start + timedelta(seconds=duration_sec)

                session_id = session_id_counter
                session_id_counter += 1

                session_rows.append(
                    (
                        session_id,
                        p.player_id,
                        session_start.isoformat(timespec="seconds") + "Z",
                        session_end.isoformat(timespec="seconds") + "Z",
                        duration_sec,
                        "1.0." + str(random.randint(0, 20)),  # client_version
                        str(random.randint(1000, 2000)),      # build_number
                        p.country_code,
                        p.platform,
                        random.choice(["icon_tap", "push", "reengagement_ad"]),
                        season_id,
                    )
                )

                # --- Events: simple match loop per session ---
                # Decide how many matches in this session.
                num_matches = random.choices(
                    [0, 1, 2, 3, 4],
                    weights=[0.1, 0.4, 0.3, 0.15, 0.05],
                )[0]
                if num_matches == 0:
                    # Could still generate some non-match events if desired
                    continue

                match_start_time = session_start + timedelta(
                    seconds=random.randint(0, duration_sec // 3)
                )
                for _m in range(num_matches):
                    match_id = match_id_counter
                    match_id_counter += 1

                    # game_mode: solo, duo, team
                    game_mode = random.choices(
                        ["solo", "duo", "team"],
                        weights=[0.5, 0.2, 0.3],
                    )[0]
                    outcome = random.choices(
                        ["win", "loss", "draw"],
                        weights=[0.45, 0.45, 0.10],
                    )[0]

                    # Soft currency delta, roughly increasing with outcome
                    if outcome == "win":
                        soft_delta = random.randint(15, 40)
                    elif outcome == "loss":
                        soft_delta = random.randint(5, 20)
                    else:  # draw
                        soft_delta = random.randint(5, 25)

                    # Match start event
                    event_rows.append(
                        (
                            event_id_counter,
                            p.player_id,
                            session_id,
                            match_start_time.isoformat(timespec="seconds") + "Z",
                            "match_start",
                            game_mode,
                            None,  # match_outcome
                            p.level,
                            match_id,
                            0,  # soft_delta
                            0,  # soft_currency_purchased
                            0,  # hard_delta
                            json.dumps({"note": "match_started"}),
                        )
                    )
                    event_id_counter += 1

                    # Match end event
                    end_time = match_start_time + timedelta(
                        seconds=random.randint(60, 900)
                    )
                    event_rows.append(
                        (
                            event_id_counter,
                            p.player_id,
                            session_id,
                            end_time.isoformat(timespec="seconds") + "Z",
                            "match_end",
                            game_mode,
                            outcome,
                            p.level,
                            match_id,
                            soft_delta,
                            0,
                            0,
                            json.dumps({"note": "match_ended"}),
                        )
                    )
                    event_id_counter += 1

                    # Chance to level up after match
                    if random.random() < 0.05:
                        p.level += 1

                    # Soft/hard currency purchase events -> purchases table
                    # Purchases only for paying segments
                    if p.spend_segment != "nonpayer":
                        # Probability of purchase depends on spend segment
                        if p.spend_segment == "minnow":
                            prob_purchase = 0.01
                        elif p.spend_segment == "dolphin":
                            prob_purchase = 0.03
                        else:  # whale
                            prob_purchase = 0.10

                        if random.random() < prob_purchase:
                            # Build a very simple product catalogue
                            product = random.choices(
                                [
                                    ("soft_small", "SoftPack", 1.99, 500, 0),
                                    ("soft_large", "SoftPack", 9.99, 3000, 0),
                                    ("hard_small", "HardPack", 4.99, 0, 50),
                                    ("hard_large", "HardPack", 19.99, 0, 300),
                                    ("bundle", "Bundle", 9.99, 2500, 50),
                                ],
                                weights=[0.3, 0.2, 0.2, 0.1, 0.2],
                            )[0]
                            sku, ptype, price_eur, soft_grant, hard_grant = product

                            purchase_time = end_time + timedelta(
                                seconds=random.randint(5, 60)
                            )
                            price_cents = int(round(price_eur * 100))

                            purchase_rows.append(
                                (
                                    purchase_id_counter,
                                    p.player_id,
                                    session_id,
                                    purchase_time.isoformat(timespec="seconds") + "Z",
                                    f"prod_{sku}",
                                    ptype,
                                    "EUR",
                                    price_cents,
                                    price_cents,
                                    1,
                                    soft_grant,
                                    hard_grant,
                                    p.platform,
                                    p.country_code,
                                )
                            )
                            purchase_id_counter += 1
                            p.total_spend_eur += price_eur

                    # Move match_start_time forward a bit for the next match
                    match_start_time = end_time + timedelta(
                        seconds=random.randint(10, 120)
                    )

                # Periodically flush batches to avoid huge memory usage
                if (
                    len(session_rows) + len(event_rows) + len(purchase_rows)
                    >= BATCH_SIZE
                ):
                    flush_batches()

    # Final flush
    flush_batches()


# ---------------------------------------
# Main
# ---------------------------------------

def main() -> None:
    random.seed(RANDOM_SEED)

    if DB_PATH.exists():
        DB_PATH.unlink()  # remove old DB

    conn = sqlite3.connect(DB_PATH)

    # Performance pragmas: for offline one-shot data generation we can
    # safely trade durability guarantees for speed and smaller WAL/SHM
    # files. If you ever generate data in a production environment, you
    # should revisit these settings.
    conn.execute("PRAGMA journal_mode = OFF;")
    conn.execute("PRAGMA synchronous = OFF;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -200000;")  # ~200 MB page cache

    print("Initializing database...")
    init_db(conn, SCHEMA_PATH)

    print("Generating players...")
    players = generate_players(N_PLAYERS)
    insert_players(conn, players)

    print("Generating teams and memberships...")
    generate_teams_and_memberships(conn, players)

    print("Assigning experiments...")
    generate_experiments(conn, players)

    print("Building DAU curve...")
    dau_curve = generate_dau_curve(NUM_DAYS)

    print("Generating sessions, events, and purchases...")
    generate_sessions_events_purchases(conn, players, dau_curve)

    conn.close()
    print("Done. Database written to:", DB_PATH)


if __name__ == "__main__":
    main()
