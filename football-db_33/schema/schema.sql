-- ============================================================
-- Football Database Schema
-- One SQLite file = your whole database. No server needed.
-- ============================================================

PRAGMA foreign_keys = ON;

-- Static reference data -----------------------------------------------

CREATE TABLE IF NOT EXISTS leagues (
    league_id       TEXT PRIMARY KEY,      -- e.g. 'ENG-Premier League'
    name            TEXT NOT NULL,
    country         TEXT NOT NULL,
    tier            INTEGER DEFAULT 1,
    source_codes    TEXT                    -- JSON: {"fbref": "9", "football_data_co_uk": "E0", "espn": "eng.1"}
);

CREATE TABLE IF NOT EXISTS teams (
    team_id         TEXT PRIMARY KEY,      -- your own stable slug, e.g. 'arsenal'
    name            TEXT NOT NULL,
    country         TEXT,
    founded         INTEGER,
    stadium         TEXT,
    city            TEXT,
    logo_url        TEXT,
    source_ids      TEXT                    -- JSON: {"fbref": "18bb7c10", "espn": "359", "understat": "83"}
);

CREATE TABLE IF NOT EXISTS players (
    player_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    birth_date      TEXT,                   -- ISO date, nullable
    nationality     TEXT,
    position        TEXT,
    photo_url       TEXT,
    source_ids      TEXT                    -- JSON: {"api_football": "12345", "fbref": "..."}
);

-- Time-varying facts (append-only, never overwritten) ------------------

CREATE TABLE IF NOT EXISTS player_team_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    team_id         TEXT NOT NULL REFERENCES teams(team_id),
    season          TEXT NOT NULL,          -- '2026-27'
    squad_number    INTEGER,
    on_loan_from    TEXT,
    is_current      INTEGER DEFAULT 1,      -- 0 once they've left; lets us keep history without deleting rows
    UNIQUE(player_id, team_id, season)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,       -- source-stable id, e.g. fbref match id
    league_id       TEXT NOT NULL REFERENCES leagues(league_id),
    season          TEXT NOT NULL,
    date_utc        TEXT NOT NULL,           -- ISO datetime
    home_team_id    TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id    TEXT NOT NULL REFERENCES teams(team_id),
    home_goals      INTEGER,                 -- NULL until played
    away_goals      INTEGER,
    status          TEXT DEFAULT 'scheduled',-- scheduled | finished | postponed | cancelled
    venue           TEXT,
    referee         TEXT
);

-- Rich per-match stats, one row per match (nullable until source has it)
CREATE TABLE IF NOT EXISTS match_stats (
    match_id            TEXT PRIMARY KEY REFERENCES matches(match_id),
    home_xg             REAL, away_xg             REAL,
    home_shots          INTEGER, away_shots          INTEGER,
    home_shots_on_target INTEGER, away_shots_on_target INTEGER,
    home_corners        INTEGER, away_corners        INTEGER,
    home_fouls          INTEGER, away_fouls          INTEGER,
    home_yellow_cards   INTEGER, away_yellow_cards   INTEGER,
    home_red_cards      INTEGER, away_red_cards      INTEGER,
    home_possession     REAL, away_possession     REAL
);

-- Betting market snapshots — kept as a TIME SERIES (never overwrite),
-- so you can see how a line moved and compare your model to the CLOSING line.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    bookmaker       TEXT NOT NULL,           -- 'pinnacle', 'bet365', etc.
    captured_at_utc TEXT NOT NULL,
    market          TEXT NOT NULL,           -- '1x2', 'btts', 'over_under_2.5', 'corners_over_9.5'
    outcome         TEXT NOT NULL,           -- 'home' | 'draw' | 'away' | 'yes' | 'no' | 'over' | 'under'
    odds_decimal    REAL NOT NULL
);

-- The ONE table that's yours, not scraped. Your subjective adjustment layer.
CREATE TABLE IF NOT EXISTS user_ratings (
    team_id         TEXT NOT NULL REFERENCES teams(team_id),
    as_of_date      TEXT NOT NULL,
    attack_adj      REAL DEFAULT 0,          -- your manual nudge to the computed attack strength
    defense_adj     REAL DEFAULT 0,
    note            TEXT,
    PRIMARY KEY (team_id, as_of_date)
);

-- Same idea as user_ratings but per-player, since you said ratings are
-- the one thing you want to set yourself, not have scraped/computed.
CREATE TABLE IF NOT EXISTS player_ratings (
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    as_of_date      TEXT NOT NULL,
    rating          INTEGER NOT NULL,       -- your 0-100 rating, arbitrary/subjective
    note            TEXT,
    PRIMARY KEY (player_id, as_of_date)
);

-- Model outputs, kept historically so you can grade past predictions ---

CREATE TABLE IF NOT EXISTS sim_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at_utc      TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    n_simulations   INTEGER NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS sim_results (
    run_id          INTEGER NOT NULL REFERENCES sim_runs(run_id),
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    p_home_win      REAL, p_draw REAL, p_away_win REAL,
    p_btts_yes      REAL,
    p_over_2_5      REAL,
    exp_home_goals  REAL, exp_away_goals REAL,
    exp_home_corners REAL, exp_away_corners REAL,
    PRIMARY KEY (run_id, match_id)
);

-- Pattern scanner output — logged historically so you can check
-- whether a flagged pattern actually held up afterward.
CREATE TABLE IF NOT EXISTS pattern_flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    flagged_at_utc  TEXT NOT NULL,
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    pattern_type    TEXT NOT NULL,           -- 'h2h_dominance', 'btts_streak', 'corners_trend', ...
    description     TEXT NOT NULL,
    sample_size     INTEGER NOT NULL,
    hit_rate        REAL NOT NULL,
    confidence_score REAL NOT NULL,          -- 0-1, penalized for small samples (see patterns/scanner.py)
    context_flags   TEXT                     -- JSON: {"same_manager": true, "same_stakes": true}
);

CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(date_utc);
CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(home_team_id, away_team_id);
CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(match_id, market);
