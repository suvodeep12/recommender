from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from recommender.app import create_app
from recommender.tmdb import Item, TMDBError


def make_item(media_type: str, tmdb_id: int, title: str, popularity: float = 10) -> Item:
    return Item(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        overview=f"A story about {title.lower()}.",
        year="2024",
        poster_path=f"/{tmdb_id}.jpg",
        genres=("genre:drama",),
        keywords=("keyword:story",),
        cast=(f"person:{tmdb_id}",),
        crew=(),
        collections=(),
        companies=(),
        vote_average=8.0,
        vote_count=100,
        popularity=popularity,
        raw={"id": tmdb_id},
        is_hydrated=True,
    )


class FakeTMDB:
    has_token = True

    def __init__(self):
        self.items = {
            (media_type, index): make_item(media_type, index, f"{media_type.title()} {index}", 100 - index)
            for media_type in ("movie", "tv")
            for index in range(1, 31)
        }

    def search(self, query: str, media_type: str):
        return [item for item in self.items.values() if item.media_type == media_type and query.casefold() in item.title.casefold()]

    def discover(self, media_type: str, pages: int = 5):
        return list(self.items.values())[:30] if media_type == "movie" else list(self.items.values())[30:]

    def details(self, media_type: str, tmdb_id: int):
        return self.items[(media_type, tmdb_id)]


class FailingSearchTMDB(FakeTMDB):
    def search(self, query: str, media_type: str):
        raise TMDBError("tmdb_unavailable", 503, "TMDB could not be reached.")


class FailingDetailsTMDB(FakeTMDB):
    def details(self, media_type: str, tmdb_id: int):
        raise TMDBError("tmdb_unavailable", 503, "TMDB could not be reached.")


class PartialCandidateTMDB(FakeTMDB):
    def __init__(self):
        super().__init__()
        self.details_calls = 0

    def discover(self, media_type: str, pages: int = 5):
        return [replace(item, is_hydrated=False) for item in super().discover(media_type, pages)]

    def details(self, media_type: str, tmdb_id: int):
        self.details_calls += 1
        return super().details(media_type, tmdb_id)


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "recommendations.sqlite3", FakeTMDB()))


def seed_payload():
    return {
        "items": [
            {"media_type": "movie" if index <= 5 else "tv", "tmdb_id": index if index <= 5 else index - 5}
            for index in range(1, 11)
        ]
    }


def test_seed_validation_and_reset_preserve_catalog(tmp_path):
    with client(tmp_path) as app_client:
        too_few = app_client.post("/api/profile/seeds", json={"items": []})
        assert too_few.status_code == 422
        assert too_few.json()["error"]["code"] == "seed_count_invalid"

        seeded = app_client.post("/api/profile/seeds", json=seed_payload())
        assert seeded.status_code == 200
        assert seeded.json()["comparison_count"] == 0

        profile = app_client.get("/api/profile")
        assert profile.status_code == 200
        assert profile.json()["has_profile"] is True
        assert len(profile.json()["items"]) == 10

        reset = app_client.post("/api/profile/reset")
        assert reset.status_code == 200
        assert reset.json()["cached_item_count"] == 10

        empty_profile = app_client.get("/api/profile")
        assert empty_profile.json()["has_profile"] is False


def test_duplicate_seed_is_rejected(tmp_path):
    payload = seed_payload()
    payload["items"][-1] = payload["items"][0]
    with client(tmp_path) as app_client:
        response = app_client.post("/api/profile/seeds", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_seed"


def test_five_seed_profile_is_valid(tmp_path):
    payload = {"items": seed_payload()["items"][:5]}
    with client(tmp_path) as app_client:
        response = app_client.post("/api/profile/seeds", json=payload)

    assert response.status_code == 200
    assert response.json()["minimum_seeds"] == 5
    assert response.json()["maximum_seeds"] == 10


def test_cached_seed_metadata_survives_tmdb_detail_failure(tmp_path):
    from recommender.db import Database

    database_path = tmp_path / "recommendations.sqlite3"
    database = Database(database_path)
    database.init()
    cached_seeds = [make_item("movie", index, f"Cached Movie {index}") for index in range(1, 6)]
    database.upsert_items(cached_seeds)

    with TestClient(create_app(database_path, FailingDetailsTMDB())) as app_client:
        response = app_client.post(
            "/api/profile/seeds",
            json={"items": [{"media_type": item.media_type, "tmdb_id": item.tmdb_id} for item in cached_seeds]},
        )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 5


def test_first_pair_does_not_bulk_hydrate_candidate_details(tmp_path):
    fake_tmdb = PartialCandidateTMDB()
    with TestClient(create_app(tmp_path / "recommendations.sqlite3", fake_tmdb)) as app_client:
        seeded = app_client.post("/api/profile/seeds", json={"items": seed_payload()["items"][:5]})
        assert seeded.status_code == 200
        details_before_pair = fake_tmdb.details_calls

        pair = app_client.get("/api/profile/pair")

    assert pair.status_code == 200
    assert fake_tmdb.details_calls == details_before_pair


def test_pairwise_round_accepts_only_current_winner_and_recommends(tmp_path):
    with client(tmp_path) as app_client:
        app_client.post("/api/profile/seeds", json=seed_payload())
        pair = app_client.get("/api/profile/pair")
        assert pair.status_code == 200
        pair_data = pair.json()
        assert pair_data["complete"] is False

        bad_winner = app_client.post(
            "/api/profile/pair",
            json={"pair_id": pair_data["pair_id"], "winner_key": "movie:999"},
        )
        assert bad_winner.status_code == 422

        winner = app_client.post(
            "/api/profile/pair",
            json={"pair_id": pair_data["pair_id"], "winner_key": pair_data["left"]["key"]},
        )
        assert winner.status_code == 200
        assert winner.json()["round"] == 1

        recommendations = app_client.get("/api/recommendations?limit=10")
        assert recommendations.status_code == 200
        assert len(recommendations.json()["items"]) <= 10


def test_full_ten_comparison_flow_reaches_recommendations(tmp_path):
    with client(tmp_path) as app_client:
        seeded = app_client.post("/api/profile/seeds", json=seed_payload())
        assert seeded.status_code == 200

        for expected_round in range(10):
            pair = app_client.get("/api/profile/pair")
            assert pair.status_code == 200
            pair_data = pair.json()
            assert pair_data["round"] == expected_round
            assert pair_data["complete"] is False

            response = app_client.post(
                "/api/profile/pair",
                json={"pair_id": pair_data["pair_id"], "winner_key": pair_data["left"]["key"]},
            )
            assert response.status_code == 200

        complete = app_client.get("/api/profile/pair")
        assert complete.json() == {"complete": True, "round": 10, "total_rounds": 10}
        recommendations = app_client.get("/api/recommendations?limit=10")
        assert recommendations.status_code == 200
        assert recommendations.json()["items"]


def test_adaptive_comparison_endpoint_continues_after_onboarding(tmp_path):
    with client(tmp_path) as app_client:
        app_client.post("/api/profile/seeds", json=seed_payload())
        for _ in range(10):
            pair = app_client.get("/api/profile/pair").json()
            app_client.post(
                "/api/profile/pair",
                json={"pair_id": pair["pair_id"], "winner_key": pair["left"]["key"]},
            )

        adaptive = app_client.get("/api/learning/compare")
        assert adaptive.status_code == 200
        assert adaptive.json()["complete"] is False
        assert adaptive.json()["total_rounds"] is None


def test_cached_search_works_without_token(tmp_path):
    from recommender.db import Database
    from recommender.tmdb import TMDBClient

    database_path = tmp_path / "recommendations.sqlite3"
    database = Database(database_path)
    database.init()
    database.upsert_items([make_item("movie", 1, "Cached Film")])
    app = create_app(database_path, TMDBClient(token=None))

    with TestClient(app) as app_client:
        cached = app_client.get("/api/search?q=Cached&media_type=movie")
        missing = app_client.get("/api/search?q=Remote&media_type=movie")

    assert cached.status_code == 200
    assert cached.json()["items"][0]["title"] == "Cached Film"
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "tmdb_token_missing"


def test_all_media_search_combines_movie_and_tv_results(tmp_path):
    with client(tmp_path) as app_client:
        response = app_client.get("/api/search?q=1&media_type=all")

    assert response.status_code == 200
    assert {item["media_type"] for item in response.json()["items"]} == {"movie", "tv"}


def test_search_falls_back_to_cached_matches_when_tmdb_fails(tmp_path):
    from recommender.db import Database

    database_path = tmp_path / "recommendations.sqlite3"
    database = Database(database_path)
    database.init()
    database.upsert_items([make_item("movie", 1, "Cached Matrix")])

    with TestClient(create_app(database_path, FailingSearchTMDB())) as app_client:
        response = app_client.get("/api/search?q=Cached&media_type=movie")

    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Cached Matrix"
