from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import Iterable

from .tmdb import Item, now_iso


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

CREATE INDEX IF NOT EXISTS idx_items_media_popularity
    ON items (media_type, popularity DESC);
CREATE INDEX IF NOT EXISTS idx_profile_events_item
    ON profile_events (media_type, tmdb_id);
"""


def _json_list(value: Iterable[str]) -> str:
    return json.dumps(list(value), separators=(",", ":"))


def _read_list(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in decoded) if isinstance(decoded, list) else ()


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        media_type=row["media_type"],
        tmdb_id=int(row["tmdb_id"]),
        title=row["title"],
        overview=row["overview"],
        year=row["year"],
        poster_path=row["poster_path"],
        genres=_read_list(row["genres_json"]),
        keywords=_read_list(row["keywords_json"]),
        cast=_read_list(row["cast_json"]),
        crew=_read_list(row["crew_json"]),
        collections=_read_list(row["collections_json"]),
        companies=_read_list(row["companies_json"]),
        vote_average=float(row["vote_average"]),
        vote_count=int(row["vote_count"]),
        popularity=float(row["popularity"]),
        raw=json.loads(row["raw_json"] or "{}"),
        is_hydrated=bool(row["is_hydrated"]),
    )


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

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
                (media_type, max(1, min(limit, 500))),
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def count_items(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def replace_profile(self, seeds: Iterable[Item]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM profile_events")
            connection.execute("DELETE FROM comparisons")
            connection.executemany(
                """
                INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
                VALUES (?, ?, 1, 'seed', ?)
                """,
                [(item.media_type, item.tmdb_id, now_iso()) for item in seeds],
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
            connection.executemany(
                """
                INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (winner.media_type, winner.tmdb_id, 1, "winner", created_at),
                    (loser.media_type, loser.tmdb_id, -1, "loser", created_at),
                ],
            )

    def reset_profile(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM profile_events")
            connection.execute("DELETE FROM comparisons")
