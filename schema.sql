-- Recommendation for SQLite (most SQLs have FK automatically on) 
PRAGMA foreign_keys = ON;

-- Players
CREATE TABLE players (
    player_id           INTEGER PRIMARY KEY,          -- BIGINT → INTEGER (SQLite)
    created_at_utc      TEXT NOT NULL,               -- ISO8601 datetime string

    country_code        TEXT NOT NULL,               -- e.g. 'US'
    platform            TEXT NOT NULL,               -- 'iOS' / 'Android'
    device_model        TEXT,
    os_version          TEXT,

    acquisition_channel  TEXT,
    acquisition_campaign TEXT,

    language_code       TEXT,
    time_zone           TEXT
);

-- Teams
CREATE TABLE teams (
    team_id          INTEGER PRIMARY KEY,
    created_at_utc   TEXT NOT NULL,
    disbanded_at_utc TEXT,               -- NULL = still active

    tier_level       INTEGER NOT NULL,   -- 0,1,2,... e.g. member_count/5
    max_members      INTEGER NOT NULL    -- e.g. 50
    -- We don't necessarily need team_name in the mock data
);

-- Team membership history
CREATE TABLE team_memberships (
    membership_id   INTEGER PRIMARY KEY,
    player_id       INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,

    joined_at_utc   TEXT NOT NULL,
    left_at_utc     TEXT,               -- NULL = still a member

    is_leader       INTEGER NOT NULL DEFAULT 0,  -- 0 = false, 1 = true

    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id)   REFERENCES teams(team_id)
);

-- Ensure at most one leader per team
CREATE UNIQUE INDEX idx_team_one_leader
    ON team_memberships (team_id)
    WHERE is_leader = 1;

-- Sessions (login + play segment)
CREATE TABLE sessions (
    session_id        INTEGER PRIMARY KEY,
    player_id         INTEGER NOT NULL,
    session_start_utc TEXT NOT NULL,
    session_end_utc   TEXT,
    duration_sec      INTEGER,

    client_version    TEXT,
    build_number      TEXT,
    country_code      TEXT,
    platform          TEXT,
    entry_point       TEXT,      -- e.g. 'icon_tap', 'push', ...

    season_id         INTEGER,   -- 2-month seasons etc. for analysis

    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- Events – in-game events
CREATE TABLE events (
    event_id                 INTEGER PRIMARY KEY,
    player_id                INTEGER NOT NULL,
    session_id               INTEGER,

    event_time_utc           TEXT NOT NULL,
    event_type               TEXT NOT NULL,   -- 'match_start', 'match_end', 'level_up', ...

    game_mode                TEXT,           -- 'solo', 'team'
    match_outcome            TEXT,           -- 'win', 'loss', 'draw' (only for 'match_end')

    level                    INTEGER,
    match_id                 INTEGER,

    -- currency changes
    soft_delta               INTEGER,        -- soft currency earned/spent in game
    soft_currency_purchased  INTEGER,        -- soft currency bought with real money (delta)
    hard_delta               INTEGER,        -- premium currency (gems etc.) delta

    metadata_json            TEXT,

    FOREIGN KEY (player_id)  REFERENCES players(player_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- Real-money purchases
CREATE TABLE purchases (
    purchase_id        INTEGER PRIMARY KEY,
    player_id          INTEGER NOT NULL,
    session_id         INTEGER,

    purchase_time_utc  TEXT NOT NULL,
    product_id         TEXT NOT NULL,
    product_type       TEXT NOT NULL,     -- 'StarterPack', 'Skin', 'BattlePass', ...

    currency_code      TEXT NOT NULL,     -- 'EUR', 'USD', ...
    price_local        INT NOT NULL,     -- in local currency
    price_eur          INT NOT NULL,     -- converted to EUR (units in cents!)
    quantity           INTEGER NOT NULL DEFAULT 1,

    grants_soft_amount INTEGER,           -- soft currency granted by the bundle
    grants_hard_amount INTEGER,           -- hard currency granted by the bundle

    platform           TEXT,
    country_code       TEXT,

    FOREIGN KEY (player_id)  REFERENCES players(player_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- A/B test variants
CREATE TABLE experiment_assignments (
    experiment_name   TEXT NOT NULL,
    player_id         INTEGER NOT NULL,
    variant           TEXT NOT NULL,     -- 'A', 'B', 'control', ...
    assigned_at_utc   TEXT NOT NULL,

    PRIMARY KEY (experiment_name, player_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

----------------------------------------------------------------
-- Useful indexes for analysis
----------------------------------------------------------------

-- Player cohorts, countries, UA channels
CREATE INDEX idx_players_created
    ON players (created_at_utc);
CREATE INDEX idx_players_country
    ON players (country_code);
CREATE INDEX idx_players_acq
    ON players (acquisition_channel, acquisition_campaign);

-- Time- and country-based session queries
CREATE INDEX idx_sessions_player_time
    ON sessions (player_id, session_start_utc);
CREATE INDEX idx_sessions_start
    ON sessions (session_start_utc);
CREATE INDEX idx_sessions_country_time
    ON sessions (country_code, session_start_utc);
CREATE INDEX idx_sessions_season
    ON sessions (season_id, session_start_utc);

-- Event queries by player, time, and type
CREATE INDEX idx_events_player_time
    ON events (player_id, event_time_utc);
CREATE INDEX idx_events_type_time
    ON events (event_type, event_time_utc);
CREATE INDEX idx_events_session
    ON events (session_id);

-- Purchase queries by player, time, and product
CREATE INDEX idx_purchases_player_time
    ON purchases (player_id, purchase_time_utc);
CREATE INDEX idx_purchases_time
    ON purchases (purchase_time_utc);
CREATE INDEX idx_purchases_product
    ON purchases (product_id);

-- Inspecting team memberships
CREATE INDEX idx_team_members_current
    ON team_memberships (team_id, left_at_utc);
CREATE INDEX idx_player_membership
    ON team_memberships (player_id, joined_at_utc);

-- A/B tests: fast lookups by variant
CREATE INDEX idx_exp_variant
    ON experiment_assignments (experiment_name, variant);
