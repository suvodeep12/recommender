from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Iterable
from uuid import uuid4

from .tmdb import Item, normalize_item, now_iso


STATUS_VALUES = {"planned", "watching", "watched", "dropped"}
SENTIMENT_VALUES = {"love", "like", "neutral", "dislike", "not_for_me"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    overview TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    poster_path TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    cast_json TEXT NOT NULL DEFAULT '[]',
    crew_json TEXT NOT NULL DEFAULT '[]',
    collections_json TEXT NOT NULL DEFAULT '[]',
    companies_json TEXT NOT NULL DEFAULT '[]',
    vote_average REAL NOT NULL DEFAULT 0,
    vote_count INTEGER NOT NULL DEFAULT 0,
    popularity REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    is_hydrated INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
);

CREATE TABLE IF NOT EXISTS profile_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    signal REAL NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('seed', 'winner', 'loser')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL UNIQUE,
    left_key TEXT NOT NULL,
    right_key TEXT NOT NULL,
    winner_key TEXT NOT NULL,
    loser_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_entries (
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'watching', 'watched', 'dropped')),
    sentiment TEXT NOT NULL DEFAULT 'neutral' CHECK (sentiment IN ('love', 'like', 'neutral', 'dislike', 'not_for_me')),
    saved INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    learning_enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
);

CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key TEXT NOT NULL,
    media_type TEXT CHECK (media_type IS NULL OR media_type IN ('movie', 'tv')),
    tmdb_id INTEGER,
    season_number INTEGER,
    episode_number INTEGER,
    event_type TEXT NOT NULL,
    value TEXT,
    numeric_value REAL,
    signal REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    learning_enabled INTEGER NOT NULL DEFAULT 1,
    context_json TEXT NOT NULL DEFAULT '{}',
    legacy_event_id INTEGER UNIQUE
);

CREATE TABLE IF NOT EXISTS episodes (
    tv_id INTEGER NOT NULL,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    air_date TEXT NOT NULL DEFAULT '',
    runtime INTEGER,
    still_path TEXT,
    watched INTEGER NOT NULL DEFAULT 0,
    watch_count INTEGER NOT NULL DEFAULT 0,
    last_watched_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tv_id, season_number, episode_number)
);

CREATE TABLE IF NOT EXISTS profile_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taste_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    learning_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    year TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    confidence REAL NOT NULL DEFAULT 0,
    candidate_keys_json TEXT NOT NULL DEFAULT '[]',
    selected_key TEXT,
    status TEXT NOT NULL,
    record_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (batch_id, source_key),
    FOREIGN KEY (batch_id) REFERENCES import_batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_media_popularity
    ON items (media_type, popularity DESC);
CREATE INDEX IF NOT EXISTS idx_profile_events_item
    ON profile_events (media_type, tmdb_id);
CREATE INDEX IF NOT EXISTS idx_learning_events_entity
    ON learning_events (entity_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_events_item
    ON learning_events (media_type, tmdb_id, learning_enabled);
CREATE INDEX IF NOT EXISTS idx_episodes_air_date
    ON episodes (air_date, watched);
"""


def _json_list(value: Iterable[str]) -> str:
    return json.dumps(list(value), separators=(",", ":"))


def _read_list(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    return tuple(str(item) for item in decoded) if isinstance(decoded, list) else ()


def _read_json(value: str | None) -> dict:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_to_item(row: sqlite3.Row) -> Item:
    raw = _read_json(row["raw_json"])
    normalised = normalize_item(raw, row["media_type"], hydrated=bool(row["is_hydrated"])) if raw.get("id") else None
    return Item(
        media_type=row["media_type"],
        tmdb_id=int(row["tmdb_id"]),
        title=row["title"],
        overview=row["overview"],
        year=row["year"],
        poster_path=row["poster_path"],
        genres=normalised.genres if normalised and normalised.genres else _read_list(row["genres_json"]),
        keywords=normalised.keywords if normalised and normalised.keywords else _read_list(row["keywords_json"]),
        cast=normalised.cast if normalised and normalised.cast else _read_list(row["cast_json"]),
        crew=normalised.crew if normalised and normalised.crew else _read_list(row["crew_json"]),
        collections=normalised.collections if normalised and normalised.collections else _read_list(row["collections_json"]),
        companies=normalised.companies if normalised and normalised.companies else _read_list(row["companies_json"]),
        vote_average=float(row["vote_average"]),
        vote_count=int(row["vote_count"]),
        popularity=float(row["popularity"]),
        raw=raw,
        is_hydrated=bool(row["is_hydrated"]),
    )


def _episode_key(tv_id: int, season_number: int, episode_number: int) -> str:
    return f"tv:{tv_id}:s{season_number:02d}e{episode_number:02d}"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_legacy_events(connection)

    def _migrate_legacy_events(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM profile_events ORDER BY id").fetchall()
        for row in rows:
            event_type = {
                "seed": "seed",
                "winner": "comparison_winner",
                "loser": "comparison_loser",
            }.get(row["event_type"], row["event_type"])
            connection.execute(
                """
                INSERT OR IGNORE INTO learning_events (
                    entity_key, media_type, tmdb_id, event_type, value, signal,
                    source, occurred_at, learning_enabled, legacy_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'migration', ?, 1, ?)
                """,
                (
                    f"{row['media_type']}:{row['tmdb_id']}",
                    row["media_type"],
                    row["tmdb_id"],
                    event_type,
                    row["event_type"],
                    float(row["signal"]),
                    row["created_at"],
                    row["id"],
                ),
            )
            if row["event_type"] == "seed":
                connection.execute(
                    """
                    INSERT OR IGNORE INTO library_entries
                        (media_type, tmdb_id, status, sentiment, updated_at)
                    VALUES (?, ?, 'watched', 'like', ?)
                    """,
                    (row["media_type"], row["tmdb_id"], row["created_at"]),
                )

    def upsert_items(self, items: Iterable[Item]) -> None:
        with self._connect() as connection:
            for item in items:
                existing = connection.execute(
                    "SELECT is_hydrated FROM items WHERE media_type = ? AND tmdb_id = ?",
                    (item.media_type, item.tmdb_id),
                ).fetchone()
                if existing and existing["is_hydrated"] and not item.is_hydrated:
                    continue
                connection.execute(
                    """
                    INSERT INTO items (
                        media_type, tmdb_id, title, overview, year, poster_path,
                        genres_json, keywords_json, cast_json, crew_json,
                        collections_json, companies_json, vote_average, vote_count,
                        popularity, raw_json, is_hydrated, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                        title = excluded.title,
                        overview = excluded.overview,
                        year = excluded.year,
                        poster_path = excluded.poster_path,
                        genres_json = excluded.genres_json,
                        keywords_json = excluded.keywords_json,
                        cast_json = excluded.cast_json,
                        crew_json = excluded.crew_json,
                        collections_json = excluded.collections_json,
                        companies_json = excluded.companies_json,
                        vote_average = excluded.vote_average,
                        vote_count = excluded.vote_count,
                        popularity = excluded.popularity,
                        raw_json = excluded.raw_json,
                        is_hydrated = excluded.is_hydrated,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        item.media_type,
                        item.tmdb_id,
                        item.title,
                        item.overview,
                        item.year,
                        item.poster_path,
                        _json_list(item.genres),
                        _json_list(item.keywords),
                        _json_list(item.cast),
                        _json_list(item.crew),
                        _json_list(item.collections),
                        _json_list(item.companies),
                        item.vote_average,
                        item.vote_count,
                        item.popularity,
                        json.dumps(item.raw, separators=(",", ":")),
                        int(item.is_hydrated),
                        now_iso(),
                    ),
                )

    def get_item(self, media_type: str, tmdb_id: int) -> Item | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE media_type = ? AND tmdb_id = ?",
                (media_type, tmdb_id),
            ).fetchone()
        return _row_to_item(row) if row else None

    def get_item_by_key(self, key: str) -> Item | None:
        try:
            media_type, raw_id = key.split(":", 1)
            return self.get_item(media_type, int(raw_id))
        except (ValueError, TypeError):
            return None

    def search_items(self, query: str, media_type: str, limit: int = 20) -> list[Item]:
        pattern = f"%{query}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM items
                WHERE media_type = ? AND (title LIKE ? OR overview LIKE ?)
                ORDER BY popularity DESC, title COLLATE NOCASE ASC
                LIMIT ?
                """,
                (media_type, pattern, pattern, max(1, min(limit, 100))),
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def items_for_media(self, media_type: str, limit: int = 100) -> list[Item]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM items
                WHERE media_type = ?
                ORDER BY popularity DESC, vote_average DESC, title COLLATE NOCASE ASC
                LIMIT ?
                """,
                (media_type, max(1, min(limit, 1000))),
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def count_items(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def _ensure_library_entry(self, connection: sqlite3.Connection, item: Item) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO library_entries (media_type, tmdb_id, updated_at)
            VALUES (?, ?, ?)
            """,
            (item.media_type, item.tmdb_id, now_iso()),
        )

    def _apply_item_event(
        self,
        connection: sqlite3.Connection,
        item: Item,
        event_type: str,
        value: str | None,
    ) -> None:
        self._ensure_library_entry(connection, item)
        updates: dict[str, object] = {"updated_at": now_iso()}
        if event_type in {"status", "import_status"} and value in STATUS_VALUES:
            updates["status"] = value
        elif event_type == "seed":
            updates.update(status="watched", sentiment="like")
        elif event_type in {"sentiment", "import_sentiment"} and value in SENTIMENT_VALUES:
            updates["sentiment"] = value
        elif event_type == "save":
            updates["saved"] = 1
        elif event_type == "unsave":
            updates["saved"] = 0
        elif event_type in {"hide", "snooze"}:
            updates["hidden"] = 1
        elif event_type == "unhide":
            updates["hidden"] = 0
        elif event_type == "learning_exclude":
            updates["learning_enabled"] = 0
        elif event_type == "learning_include":
            updates["learning_enabled"] = 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE library_entries SET {assignments} WHERE media_type = ? AND tmdb_id = ?",
            (*updates.values(), item.media_type, item.tmdb_id),
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        item: Item | None = None,
        entity_key: str | None = None,
        media_type: str | None = None,
        tmdb_id: int | None = None,
        season_number: int | None = None,
        episode_number: int | None = None,
        value: str | None = None,
        numeric_value: float | None = None,
        signal: float = 0.0,
        source: str = "manual",
        occurred_at: str | None = None,
        learning_enabled: bool = True,
        context: dict | None = None,
        legacy_event_id: int | None = None,
    ) -> int:
        if item:
            entity_key = item.key
            media_type = item.media_type
            tmdb_id = item.tmdb_id
            self._apply_item_event(connection, item, event_type, value)
        connection.execute(
            """
            INSERT INTO learning_events (
                entity_key, media_type, tmdb_id, season_number, episode_number,
                event_type, value, numeric_value, signal, source, occurred_at,
                learning_enabled, context_json, legacy_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_key or "profile",
                media_type,
                tmdb_id,
                season_number,
                episode_number,
                event_type,
                value,
                numeric_value,
                signal,
                source,
                occurred_at or now_iso(),
                int(learning_enabled),
                json.dumps(context or {}, separators=(",", ":")),
                legacy_event_id,
            ),
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _learning_paused_connection(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT setting_value FROM profile_settings WHERE setting_key = 'learning_paused'"
        ).fetchone()
        return bool(row and row["setting_value"] == "1")

    def learning_paused(self) -> bool:
        with self._connect() as connection:
            return self._learning_paused_connection(connection)

    def set_learning_paused(self, paused: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO profile_settings (setting_key, setting_value) VALUES ('learning_paused', ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
                """,
                ("1" if paused else "0",),
            )

    def record_event(
        self,
        event_type: str,
        *,
        item: Item | None = None,
        value: str | None = None,
        numeric_value: float | None = None,
        signal: float = 0.0,
        source: str = "manual",
        occurred_at: str | None = None,
        learning_enabled: bool = True,
        context: dict | None = None,
    ) -> int:
        with self._connect() as connection:
            effective_learning = learning_enabled and not self._learning_paused_connection(connection)
            return self._insert_event(
                connection,
                event_type=event_type,
                item=item,
                value=value,
                numeric_value=numeric_value,
                signal=signal,
                source=source,
                occurred_at=occurred_at,
                learning_enabled=effective_learning,
                context=context,
            )

    def add_taste_note(self, text: str, polarity: str = "positive") -> dict:
        if polarity not in {"positive", "negative"}:
            raise ValueError("Taste note polarity must be positive or negative.")
        text = text.strip()
        if not text:
            raise ValueError("Taste note text is required.")
        created_at = now_iso()
        with self._connect() as connection:
            note_id = int(
                connection.execute(
                    "INSERT INTO taste_notes (text, polarity, created_at) VALUES (?, ?, ?)",
                    (text, polarity, created_at),
                ).lastrowid
            )
            self._insert_event(
                connection,
                event_type="taste_note",
                value=text,
                signal=1 if polarity == "positive" else -1,
                source="manual",
                occurred_at=created_at,
            )
        return {"id": note_id, "text": text, "polarity": polarity, "created_at": created_at}

    def taste_notes(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, text, polarity, learning_enabled, created_at FROM taste_notes ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_profile(self, seeds: Iterable[Item]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM profile_events")
            connection.execute("DELETE FROM comparisons")
            connection.execute("DELETE FROM learning_events")
            connection.execute("DELETE FROM library_entries")
            for item in seeds:
                created_at = now_iso()
                connection.execute(
                    """
                    INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
                    VALUES (?, ?, 1, 'seed', ?)
                    """,
                    (item.media_type, item.tmdb_id, created_at),
                )
                self._insert_event(
                    connection,
                    event_type="seed",
                    item=item,
                    signal=1,
                    source="manual",
                    occurred_at=created_at,
                )

    def add_seed_events(self, seeds: Iterable[Item]) -> None:
        with self._connect() as connection:
            for item in seeds:
                created_at = now_iso()
                connection.execute(
                    """
                    INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
                    VALUES (?, ?, 1, 'seed', ?)
                    """,
                    (item.media_type, item.tmdb_id, created_at),
                )
                self._insert_event(
                    connection,
                    event_type="seed",
                    item=item,
                    signal=1,
                    source="manual",
                    occurred_at=created_at,
                )

    def seed_items(self) -> list[Item]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT i.*
                FROM items i
                JOIN profile_events p ON p.media_type = i.media_type AND p.tmdb_id = i.tmdb_id
                WHERE p.event_type = 'seed'
                ORDER BY i.title COLLATE NOCASE ASC
                """
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def profile_signal_items(self) -> tuple[list[Item], list[Item]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, SUM(p.signal) AS total_signal
                FROM items i
                JOIN profile_events p ON p.media_type = i.media_type AND p.tmdb_id = i.tmdb_id
                GROUP BY i.media_type, i.tmdb_id
                ORDER BY i.title COLLATE NOCASE ASC
                """
            ).fetchall()
        positive = [_row_to_item(row) for row in rows if float(row["total_signal"]) > 0]
        negative = [_row_to_item(row) for row in rows if float(row["total_signal"]) < 0]
        return positive, negative

    def learning_signal_rows(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, e.event_type, e.value AS event_value, e.signal,
                       e.source, e.occurred_at, e.learning_enabled
                FROM items i
                JOIN learning_events e
                  ON e.media_type = i.media_type AND e.tmdb_id = i.tmdb_id
                WHERE e.learning_enabled = 1
                ORDER BY e.occurred_at ASC, e.id ASC
                """
            ).fetchall()
        return [
            {
                "item": _row_to_item(row),
                "event_type": row["event_type"],
                "value": row["event_value"],
                "signal": float(row["signal"]),
                "source": row["source"],
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    def learning_event_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM learning_events WHERE learning_enabled = 1").fetchone()[0])

    def has_learning_evidence(self) -> bool:
        return self.learning_event_count() > 0

    def profile_media_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT media_type, COUNT(DISTINCT tmdb_id) AS count
                FROM learning_events
                WHERE media_type IS NOT NULL AND learning_enabled = 1
                GROUP BY media_type
                """
            ).fetchall()
        return {row["media_type"]: int(row["count"]) for row in rows}

    def comparison_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0])

    def comparison_pair_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT pair_id FROM comparisons").fetchall()
        return {str(row["pair_id"]) for row in rows}

    def record_comparison(self, pair_id: str, left: Item, right: Item, winner: Item, loser: Item) -> None:
        created_at = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO comparisons (pair_id, left_key, right_key, winner_key, loser_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pair_id, left.key, right.key, winner.key, loser.key, created_at),
            )
            for item, event_type, signal in ((winner, "comparison_winner", 1), (loser, "comparison_loser", -1)):
                connection.execute(
                    """
                    INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (item.media_type, item.tmdb_id, signal, "winner" if signal > 0 else "loser", created_at),
                )
                self._insert_event(
                    connection,
                    event_type=event_type,
                    item=item,
                    signal=signal,
                    source="comparison",
                    occurred_at=created_at,
                )

    def record_episode_progress(
        self,
        *,
        tv_id: int,
        season_number: int,
        episode_number: int,
        watched: bool,
        metadata: dict | None = None,
        source: str = "manual",
        watched_at: str | None = None,
    ) -> None:
        metadata = metadata or {}
        occurred_at = watched_at or now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT watch_count, last_watched_at FROM episodes
                WHERE tv_id = ? AND season_number = ? AND episode_number = ?
                """,
                (tv_id, season_number, episode_number),
            ).fetchone()
            previous_count = int(existing["watch_count"]) if existing else 0
            next_count = previous_count + 1 if watched else previous_count
            connection.execute(
                """
                INSERT INTO episodes (
                    tv_id, season_number, episode_number, name, overview, air_date,
                    runtime, still_path, watched, watch_count, last_watched_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tv_id, season_number, episode_number) DO UPDATE SET
                    name = excluded.name,
                    overview = excluded.overview,
                    air_date = excluded.air_date,
                    runtime = excluded.runtime,
                    still_path = excluded.still_path,
                    watched = excluded.watched,
                    watch_count = excluded.watch_count,
                    last_watched_at = excluded.last_watched_at,
                    raw_json = excluded.raw_json
                """,
                (
                    tv_id,
                    season_number,
                    episode_number,
                    str(metadata.get("name") or ""),
                    str(metadata.get("overview") or ""),
                    str(metadata.get("air_date") or ""),
                    metadata.get("runtime"),
                    metadata.get("still_path"),
                    int(watched),
                    next_count,
                    occurred_at if watched else (existing["last_watched_at"] if existing else None),
                    json.dumps(metadata, separators=(",", ":")),
                ),
            )
            self._insert_event(
                connection,
                event_type="episode_progress",
                entity_key=_episode_key(tv_id, season_number, episode_number),
                media_type="tv",
                tmdb_id=tv_id,
                season_number=season_number,
                episode_number=episode_number,
                value="watched" if watched else "unwatched",
                numeric_value=1 if watched else 0,
                signal=0.55 if watched else -0.1,
                source=source,
                occurred_at=occurred_at,
            )

    def _library_entry_dict(self, row: sqlite3.Row | None) -> dict:
        if not row:
            return {
                "status": "planned",
                "sentiment": "neutral",
                "saved": False,
                "hidden": False,
                "learning_enabled": True,
                "notes": "",
            }
        return {
            "status": row["status"],
            "sentiment": row["sentiment"],
            "saved": bool(row["saved"]),
            "hidden": bool(row["hidden"]),
            "learning_enabled": bool(row["learning_enabled"]),
            "notes": row["notes"],
            "updated_at": row["updated_at"],
        }

    def _item_with_library(self, row: sqlite3.Row) -> dict:
        item = _row_to_item(row)
        return item.to_dict() | {
            "library": self._library_entry_dict(row),
            "next_episode": _read_json(row["raw_json"]).get("next_episode_to_air"),
        }

    def library_items(self, view: str = "all", limit: int = 100) -> list[dict]:
        allowed_views = {"all", "up_next", "watching", "watched", "watchlist", "rewatch", "upcoming"}
        if view not in allowed_views:
            raise ValueError(f"Unknown library view: {view}")
        with self._connect() as connection:
            if view == "upcoming":
                rows = connection.execute(
                    """
                    SELECT i.*, l.status, l.sentiment, l.saved, l.hidden,
                           l.learning_enabled, l.notes, l.updated_at,
                           e.season_number, e.episode_number, e.name AS episode_name,
                           e.air_date, e.watched AS episode_watched
                    FROM episodes e
                    JOIN items i ON i.media_type = 'tv' AND i.tmdb_id = e.tv_id
                    LEFT JOIN library_entries l ON l.media_type = i.media_type AND l.tmdb_id = i.tmdb_id
                    WHERE e.air_date >= date('now') AND e.watched = 0
                      AND COALESCE(l.hidden, 0) = 0
                    ORDER BY e.air_date ASC, i.title COLLATE NOCASE ASC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 500)),),
                ).fetchall()
                return [
                    self._item_with_library(row)
                    | {
                        "upcoming_episode": {
                            "season_number": row["season_number"],
                            "episode_number": row["episode_number"],
                            "name": row["episode_name"],
                            "air_date": row["air_date"],
                        }
                    }
                    for row in rows
                ]

            where = ""
            if view in {"up_next", "watching"}:
                where = "WHERE l.status = 'watching' AND l.hidden = 0"
            elif view == "watched":
                where = "WHERE l.status = 'watched' AND l.hidden = 0"
            elif view == "watchlist":
                where = "WHERE l.saved = 1 AND l.hidden = 0"
            elif view == "rewatch":
                where = "WHERE l.status = 'watched' AND l.sentiment = 'love' AND l.hidden = 0"
            rows = connection.execute(
                f"""
                SELECT i.*, l.status, l.sentiment, l.saved, l.hidden,
                       l.learning_enabled, l.notes, l.updated_at
                FROM library_entries l
                JOIN items i ON i.media_type = l.media_type AND i.tmdb_id = l.tmdb_id
                {where}
                ORDER BY l.updated_at DESC, i.title COLLATE NOCASE ASC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._item_with_library(row) for row in rows]

    def library_entry(self, media_type: str, tmdb_id: int) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM library_entries WHERE media_type = ? AND tmdb_id = ?",
                (media_type, tmdb_id),
            ).fetchone()
        return self._library_entry_dict(row)

    def episodes_for_tv(self, tv_id: int, limit: int = 1000) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM episodes WHERE tv_id = ?
                ORDER BY season_number, episode_number LIMIT ?
                """,
                (tv_id, max(1, min(limit, 5000))),
            ).fetchall()
        return [dict(row) | {"raw": _read_json(row["raw_json"])} for row in rows]

    def create_import_batch(self, filename: str, summary: dict | None = None) -> str:
        batch_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO import_batches (id, source, filename, status, summary_json, created_at)
                VALUES (?, 'tvtime', ?, 'preview', ?, ?)
                """,
                (batch_id, filename, json.dumps(summary or {}, separators=(",", ":")), now_iso()),
            )
        return batch_id

    def save_import_matches(self, batch_id: str, matches: Iterable[dict]) -> None:
        with self._connect() as connection:
            for match in matches:
                record = match.get("record") or {}
                connection.execute(
                    """
                    INSERT INTO import_matches (
                        batch_id, source_key, title, year, media_type, confidence,
                        candidate_keys_json, selected_key, status, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id, source_key) DO UPDATE SET
                        title = excluded.title,
                        year = excluded.year,
                        media_type = excluded.media_type,
                        confidence = excluded.confidence,
                        candidate_keys_json = excluded.candidate_keys_json,
                        selected_key = excluded.selected_key,
                        status = excluded.status,
                        record_json = excluded.record_json
                    """,
                    (
                        batch_id,
                        match["source_key"],
                        match["title"],
                        match.get("year", ""),
                        match["media_type"],
                        float(match.get("confidence", 0)),
                        json.dumps(match.get("candidate_keys", []), separators=(",", ":")),
                        match.get("selected_key"),
                        match.get("status", "review"),
                        json.dumps(record, separators=(",", ":")),
                    ),
                )

    def get_import_batch(self, batch_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            return None
        return dict(row) | {"summary": _read_json(row["summary_json"])}

    def get_import_matches(self, batch_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM import_matches WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [
            dict(row)
            | {
                "candidate_keys": _read_list(row["candidate_keys_json"]),
                "record": _read_json(row["record_json"]),
            }
            for row in rows
        ]

    def update_import_match(self, match_id: int, selected_key: str | None, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE import_matches SET selected_key = ?, status = ? WHERE id = ?",
                (selected_key, status, match_id),
            )

    def mark_import_committed(self, batch_id: str, summary: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE import_batches SET status = 'committed', summary_json = ? WHERE id = ?",
                (json.dumps(summary, separators=(",", ":")), batch_id),
            )

    def export_profile(self) -> dict:
        with self._connect() as connection:
            events = [
                dict(row) | {"context": json.loads(row["context_json"] or "{}")}
                for row in connection.execute("SELECT * FROM learning_events ORDER BY id").fetchall()
            ]
            library = [dict(row) for row in connection.execute("SELECT * FROM library_entries ORDER BY updated_at").fetchall()]
            episodes = [
                dict(row) | {"raw": json.loads(row["raw_json"] or "{}")}
                for row in connection.execute("SELECT * FROM episodes ORDER BY tv_id, season_number, episode_number").fetchall()
            ]
            settings = {row["setting_key"]: row["setting_value"] for row in connection.execute("SELECT * FROM profile_settings")}
        return {"version": 1, "exported_at": now_iso(), "events": events, "library": library, "episodes": episodes, "settings": settings}

    def import_profile(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("Profile export must contain an events list.")
        imported = 0
        restored_library = 0
        restored_episodes = 0
        with self._connect() as connection:
            for event in payload["events"]:
                entity_key = str(event.get("entity_key") or "profile")
                item = None
                if entity_key.count(":") == 1:
                    media_type, raw_id = entity_key.split(":", 1)
                    item_row = connection.execute(
                        "SELECT * FROM items WHERE media_type = ? AND tmdb_id = ?",
                        (media_type, int(raw_id)),
                    ).fetchone()
                    item = _row_to_item(item_row) if item_row else None
                self._insert_event(
                    connection,
                    event_type=str(event.get("event_type") or "restored"),
                    item=item,
                    entity_key=entity_key,
                    media_type=event.get("media_type"),
                    tmdb_id=event.get("tmdb_id"),
                    season_number=event.get("season_number"),
                    episode_number=event.get("episode_number"),
                    value=event.get("value"),
                    numeric_value=event.get("numeric_value"),
                    signal=float(event.get("signal") or 0),
                    source="restore",
                    occurred_at=event.get("occurred_at"),
                    learning_enabled=bool(event.get("learning_enabled", True)),
                    context=event.get("context") or {},
                )
                imported += 1
            for library in payload.get("library", []):
                media_type = library.get("media_type")
                tmdb_id = library.get("tmdb_id")
                if media_type not in {"movie", "tv"} or not tmdb_id:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM items WHERE media_type = ? AND tmdb_id = ?",
                    (media_type, tmdb_id),
                ).fetchone()
                if not exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO library_entries (
                        media_type, tmdb_id, status, sentiment, saved, hidden,
                        learning_enabled, notes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                        status = excluded.status,
                        sentiment = excluded.sentiment,
                        saved = excluded.saved,
                        hidden = excluded.hidden,
                        learning_enabled = excluded.learning_enabled,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        media_type,
                        tmdb_id,
                        library.get("status", "planned"),
                        library.get("sentiment", "neutral"),
                        int(bool(library.get("saved"))),
                        int(bool(library.get("hidden"))),
                        int(bool(library.get("learning_enabled", True))),
                        library.get("notes", ""),
                        library.get("updated_at") or now_iso(),
                    ),
                )
                restored_library += 1
            for episode in payload.get("episodes", []):
                if not all(key in episode for key in ("tv_id", "season_number", "episode_number")):
                    continue
                connection.execute(
                    """
                    INSERT INTO episodes (
                        tv_id, season_number, episode_number, name, overview, air_date,
                        runtime, still_path, watched, watch_count, last_watched_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tv_id, season_number, episode_number) DO UPDATE SET
                        name = excluded.name,
                        overview = excluded.overview,
                        air_date = excluded.air_date,
                        runtime = excluded.runtime,
                        still_path = excluded.still_path,
                        watched = excluded.watched,
                        watch_count = excluded.watch_count,
                        last_watched_at = excluded.last_watched_at,
                        raw_json = excluded.raw_json
                    """,
                    (
                        episode["tv_id"],
                        episode["season_number"],
                        episode["episode_number"],
                        episode.get("name", ""),
                        episode.get("overview", ""),
                        episode.get("air_date", ""),
                        episode.get("runtime"),
                        episode.get("still_path"),
                        int(bool(episode.get("watched"))),
                        int(episode.get("watch_count") or 0),
                        episode.get("last_watched_at"),
                        json.dumps(episode.get("raw", {}), separators=(",", ":")),
                    ),
                )
                restored_episodes += 1
            for key, value in (payload.get("settings") or {}).items():
                connection.execute(
                    """
                    INSERT INTO profile_settings (setting_key, setting_value) VALUES (?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
                    """,
                    (str(key), str(value)),
                )
        return {
            "imported_events": imported,
            "restored_library": restored_library,
            "restored_episodes": restored_episodes,
        }

    def reset_profile(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM profile_events")
            connection.execute("DELETE FROM comparisons")
            connection.execute("DELETE FROM learning_events")
            connection.execute("DELETE FROM library_entries")
            connection.execute("DELETE FROM taste_notes")
            connection.execute("DELETE FROM import_matches")
            connection.execute("DELETE FROM import_batches")
            connection.execute("UPDATE episodes SET watched = 0, watch_count = 0, last_watched_at = NULL")
            connection.execute(
                """
                INSERT INTO profile_settings (setting_key, setting_value) VALUES ('learning_paused', '0')
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = '0'
                """
            )
