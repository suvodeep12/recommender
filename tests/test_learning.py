from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recommender.app import create_app
from recommender.db import Database
from recommender.tmdb import Item
from recommender.tvtime import match_records, parse_tvtime_export


def item(media_type: str, tmdb_id: int, title: str, year: str = "2024") -> Item:
    return Item(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        overview=f"A story about {title.lower()}.",
        year=year,
        poster_path=f"/{tmdb_id}.jpg",
        genres=("genre:science-fiction",),
        keywords=("keyword:space",),
        cast=(f"person:{tmdb_id}",),
        crew=(),
        collections=(),
        companies=(),
        vote_average=8.0,
        vote_count=100,
        popularity=10,
        raw={"id": tmdb_id},
        is_hydrated=True,
    )


class FakeTMDB:
    has_token = True

    def __init__(self):
        self.items = {
            ("movie", 1): item("movie", 1, "Deep Space", "2020"),
            ("movie", 2): item("movie", 2, "Night Shift", "2021"),
            ("tv", 1): item("tv", 1, "The Expanse", "2015"),
            ("tv", 2): item("tv", 2, "Slow Horses", "2022"),
        }

    def search(self, query: str, media_type: str):
        return [value for (kind, _), value in self.items.items() if kind == media_type and query.casefold() in value.title.casefold()]

    def discover(self, media_type: str, pages: int = 2):
        return [value for (kind, _), value in self.items.items() if kind == media_type]

    def details(self, media_type: str, tmdb_id: int):
        return self.items[(media_type, tmdb_id)]


def test_tvtime_parser_and_matching_are_deterministic():
    payload = b"show_title,year,season,episode,watched_at\nThe Expanse,2015,1,1,2026-01-01\nThe Expanse,2015,1,2,2026-01-08\n"
    records = parse_tvtime_export("tracking-prod-records-v2.csv", payload)

    assert len(records) == 1
    assert records[0].title == "The Expanse"
    assert len(records[0].watched_episodes) == 2

    matches = match_records(records, [item("tv", 1, "The Expanse", "2015")])
    assert matches[0]["status"] == "accepted"
    assert matches[0]["selected_key"] == "tv:1"


def test_library_events_taste_profile_and_episode_progress(tmp_path: Path):
    database_path = tmp_path / "recommendations.sqlite3"
    with TestClient(create_app(database_path, FakeTMDB())) as client:
        saved = client.patch(
            "/api/library/items/tv/1",
            json={"status": "watched", "sentiment": "love"},
        )
        assert saved.status_code == 200

        progress = client.patch(
            "/api/library/episodes/1/1/1",
            json={"watched": True, "metadata": {"name": "Dulcinea", "air_date": "2099-01-01"}},
        )
        assert progress.status_code == 200
        client.patch(
            "/api/library/episodes/1/1/2",
            json={"watched": False, "metadata": {"name": "The Big Empty", "air_date": "2099-01-08"}},
        )

        library = client.get("/api/library?view=watched")
        assert library.status_code == 200
        assert library.json()["items"][0]["library"]["sentiment"] == "love"

        taste = client.get("/api/profile/taste")
        assert taste.status_code == 200
        assert taste.json()["event_count"] >= 3

        upcoming = client.get("/api/library?view=upcoming")
        assert upcoming.status_code == 200
        assert upcoming.json()["items"][0]["upcoming_episode"]["episode_number"] == 2


def test_tvtime_import_review_commit_and_reset_preserve_catalog(tmp_path: Path):
    database_path = tmp_path / "recommendations.sqlite3"
    csv_payload = b"show_title,year,season,episode,watched_at\nThe Expanse,2015,1,1,2026-01-01\n"
    with TestClient(create_app(database_path, FakeTMDB())) as client:
        preview = client.post(
            "/api/import/tvtime/preview",
            files={"file": ("tracking-prod-records-v2.csv", csv_payload, "text/csv")},
        )
        assert preview.status_code == 200
        import_data = preview.json()
        match = import_data["matches"][0]
        assert match["status"] == "accepted"

        committed = client.post(
            f"/api/import/tvtime/commit?import_id={import_data['import_id']}",
            json={"matches": [{"match_id": match["id"], "selected_key": "tv:1", "status": "accepted"}]},
        )
        assert committed.status_code == 200
        assert committed.json()["summary"]["accepted"] == 1

        library = client.get("/api/library?view=watched")
        assert any(entry["key"] == "tv:1" for entry in library.json()["items"])

        reset = client.post("/api/profile/reset")
        assert reset.status_code == 200
        assert reset.json()["cached_item_count"] >= 1
        assert client.get("/api/library").json()["items"] == []


def test_profile_export_import_round_trip(tmp_path: Path):
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    fake = FakeTMDB()
    with TestClient(create_app(first_path, fake)) as first:
        first.patch("/api/library/items/movie/1", json={"status": "watched", "sentiment": "like"})
        exported = first.post("/api/profile/export").json()

    with TestClient(create_app(second_path, fake)) as second:
        second.get("/api/search?q=Deep&media_type=movie")
        restored = second.post("/api/profile/import", json=exported)
        assert restored.status_code == 200
        assert restored.json()["imported_events"] >= 2
        assert second.get("/api/library?view=watched").json()["items"][0]["key"] == "movie:1"


def test_legacy_profile_events_migrate_into_learning_journal(tmp_path: Path):
    database_path = tmp_path / "legacy.sqlite3"
    database = Database(database_path)
    database.init()
    database.upsert_items([item("movie", 1, "Legacy Favorite")])
    with database._connect() as connection:
        connection.execute(
            """
            INSERT INTO profile_events (media_type, tmdb_id, signal, event_type, created_at)
            VALUES ('movie', 1, 1, 'seed', '2026-01-01T00:00:00+00:00')
            """
        )

    database.init()

    assert database.learning_event_count() == 1
    assert database.seed_items()[0].key == "movie:1"
    assert database.library_items("watched")[0]["key"] == "movie:1"
