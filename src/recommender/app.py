from __future__ import annotations

from pathlib import Path
import os
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import Database
from .ranking import (
    MAX_SEEDS,
    MIN_SEEDS,
    TOTAL_COMPARISONS,
    choose_pair,
    interleave,
    pair_key,
    pair_target_media_type,
    rank_items,
    target_media_types,
)
from .tmdb import Item, MEDIA_TYPES, TMDBClient, TMDBError


class AppError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


class MediaItemInput(BaseModel):
    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(gt=0)

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.tmdb_id}"


class SeedRequest(BaseModel):
    items: list[MediaItemInput]


class PairRequest(BaseModel):
    pair_id: str = Field(min_length=1)
    winner_key: str = Field(min_length=1)


def _default_database_path() -> Path:
    return Path(os.getenv("RECOMMENDER_DB_PATH", "data/recommender.sqlite3"))


def _context(request: Request) -> tuple[Database, TMDBClient]:
    return request.app.state.database, request.app.state.tmdb


def _app_error(error: TMDBError) -> AppError:
    return AppError(error.code, error.status_code, error.message)


def _item_key_set(items: list[Item]) -> set[str]:
    return {item.key for item in items}


def _ensure_item_details(database: Database, tmdb: TMDBClient, item: Item) -> Item:
    if item.is_hydrated or not tmdb.has_token:
        return item
    try:
        detailed = tmdb.details(item.media_type, item.tmdb_id)
    except TMDBError as error:
        return item
    database.upsert_items([detailed])
    return detailed


def _ensure_candidates(database: Database, tmdb: TMDBClient, media_type: str) -> list[Item]:
    cached = database.items_for_media(media_type, limit=100)
    if len(cached) < 100 and tmdb.has_token:
        try:
            database.upsert_items(tmdb.discover(media_type, pages=5))
        except TMDBError as error:
            if not cached:
                raise _app_error(error) from error
        cached = database.items_for_media(media_type, limit=100)

    if not cached and not tmdb.has_token:
        raise AppError(
            "tmdb_token_missing",
            503,
            "Set TMDB_API_READ_ACCESS_TOKEN or populate the local catalog first.",
        )

    if tmdb.has_token:
        for item in cached[:50]:
            if not item.is_hydrated:
                detailed = _ensure_item_details(database, tmdb, item)
                cached[cached.index(item)] = detailed
    return database.items_for_media(media_type, limit=100)


def _profile(database: Database) -> tuple[list[Item], list[Item], list[Item]]:
    seeds = database.seed_items()
    if not MIN_SEEDS <= len(seeds) <= MAX_SEEDS:
        raise AppError("seeds_required", 409, f"Select {MIN_SEEDS} to {MAX_SEEDS} positive seed items first.")
    positive, negative = database.profile_signal_items()
    return seeds, positive, negative


def _rank_for_type(database: Database, tmdb: TMDBClient, media_type: str) -> list:
    seeds, positive, negative = _profile(database)
    seed_keys = _item_key_set(seeds)
    negative_keys = _item_key_set(negative)
    candidates = [
        item
        for item in _ensure_candidates(database, tmdb, media_type)
        if item.key not in seed_keys and item.key not in negative_keys
    ]
    return rank_items(positive, negative, candidates, limit=100)


def _pair_state(database: Database, tmdb: TMDBClient) -> dict:
    seeds, _, negative = _profile(database)
    count = database.comparison_count()
    if count >= TOTAL_COMPARISONS:
        return {
            "complete": True,
            "round": count,
            "total_rounds": TOTAL_COMPARISONS,
        }

    media_type = pair_target_media_type(seeds, count)
    ranked = _rank_for_type(database, tmdb, media_type)
    selected = choose_pair(ranked, database.comparison_pair_ids(), _item_key_set(negative))
    if selected is None:
        raise AppError(
            "pair_candidates_exhausted",
            409,
            "There are not enough uncategorized candidates for another comparison.",
        )
    left, right, identifier = selected
    return {
        "complete": False,
        "pair_id": identifier,
        "left": left.to_dict(),
        "right": right.to_dict(),
        "target_media_type": media_type,
        "round": count,
        "total_rounds": TOTAL_COMPARISONS,
    }


def create_app(database_path: str | Path | None = None, tmdb_client: TMDBClient | None = None) -> FastAPI:
    database = Database(database_path or _default_database_path())
    database.init()
    tmdb = tmdb_client or TMDBClient()

    app = FastAPI(title="Scene Bridge", version="0.1.0")
    app.state.database = database
    app.state.tmdb = tmdb
    static_directory = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(part) for part in detail["loc"]) for detail in error.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": f"Check these fields: {fields or 'request'}.",
                }
            },
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    @app.get("/api/search")
    def search(
        request: Request,
        q: str = Query(min_length=1, max_length=120),
        media_type: Literal["movie", "tv", "all"] = "all",
    ) -> dict:
        database, tmdb = _context(request)
        query = q.strip()
        if not query:
            raise AppError("query_required", 422, "Enter a title to search.")

        media_types = ("movie", "tv") if media_type == "all" else (media_type,)
        items: list[Item] = []
        errors: list[TMDBError] = []
        for search_type in media_types:
            if tmdb.has_token:
                try:
                    remote_items = tmdb.search(query, search_type)
                except TMDBError as error:
                    errors.append(error)
                    remote_items = database.search_items(query, search_type)
                else:
                    database.upsert_items(remote_items)
                items.extend(remote_items)
            else:
                items.extend(database.search_items(query, search_type))

        unique_items = {item.key: item for item in items}
        items = sorted(unique_items.values(), key=lambda item: (-item.popularity, item.title.casefold()))
        if not items:
            if errors:
                raise _app_error(errors[0]) from errors[0]
            raise AppError(
                "tmdb_token_missing",
                503,
                "Set TMDB_API_READ_ACCESS_TOKEN to search titles not already cached.",
            )
        return {"items": [item.to_dict() for item in items[:20]]}

    @app.post("/api/profile/seeds")
    def set_seeds(payload: SeedRequest, request: Request) -> dict:
        database, tmdb = _context(request)
        if not MIN_SEEDS <= len(payload.items) <= MAX_SEEDS:
            raise AppError(
                "seed_count_invalid",
                422,
                f"Select between {MIN_SEEDS} and {MAX_SEEDS} positive seed items.",
            )
        if len({item.key for item in payload.items}) != len(payload.items):
            raise AppError("duplicate_seed", 422, "Each seed item must be unique.")

        selected: list[Item] = []
        for input_item in payload.items:
            item = database.get_item(input_item.media_type, input_item.tmdb_id)
            if item is None:
                if not tmdb.has_token:
                    raise AppError(
                        "cached_item_missing",
                        503,
                        "This title is not cached. Set TMDB_API_READ_ACCESS_TOKEN and try again.",
                    )
                try:
                    item = tmdb.details(input_item.media_type, input_item.tmdb_id)
                except TMDBError as error:
                    raise _app_error(error) from error
                database.upsert_items([item])
            else:
                item = _ensure_item_details(database, tmdb, item)
            selected.append(item)

        database.replace_profile(selected)
        return {
            "items": [item.to_dict() for item in selected],
            "comparison_count": 0,
            "total_comparisons": TOTAL_COMPARISONS,
            "minimum_seeds": MIN_SEEDS,
            "maximum_seeds": MAX_SEEDS,
        }

    @app.get("/api/profile")
    def get_profile(request: Request) -> dict:
        database, _ = _context(request)
        seeds = database.seed_items()
        comparison_count = database.comparison_count()
        return {
            "items": [item.to_dict() for item in seeds],
            "has_profile": MIN_SEEDS <= len(seeds) <= MAX_SEEDS,
            "comparison_count": comparison_count,
            "total_comparisons": TOTAL_COMPARISONS,
            "minimum_seeds": MIN_SEEDS,
            "maximum_seeds": MAX_SEEDS,
            "complete": comparison_count >= TOTAL_COMPARISONS,
        }

    @app.get("/api/profile/pair")
    def get_pair(request: Request) -> dict:
        database, tmdb = _context(request)
        return _pair_state(database, tmdb)

    @app.post("/api/profile/pair")
    def choose_winner(payload: PairRequest, request: Request) -> dict:
        database, tmdb = _context(request)
        state = _pair_state(database, tmdb)
        if state["complete"]:
            raise AppError("comparisons_complete", 409, "The comparison round is already complete.")
        if payload.pair_id != state["pair_id"]:
            raise AppError("stale_pair", 409, "That comparison is no longer current. Load the next pair.")

        left = database.get_item_by_key(state["left"]["key"])
        right = database.get_item_by_key(state["right"]["key"])
        winner = database.get_item_by_key(payload.winner_key)
        if not left or not right or not winner or winner.key not in {left.key, right.key}:
            raise AppError("winner_invalid", 422, "Choose one of the two displayed candidates.")
        loser = right if winner.key == left.key else left
        database.record_comparison(pair_key(left, right), left, right, winner, loser)
        return _pair_state(database, tmdb)

    @app.get("/api/recommendations")
    def recommendations(
        request: Request,
        limit: int = Query(default=10, ge=1, le=20),
    ) -> dict:
        database, tmdb = _context(request)
        seeds, _, negative = _profile(database)
        targets = target_media_types(seeds)
        ranked_by_type = {media_type: _rank_for_type(database, tmdb, media_type) for media_type in targets}
        if len(targets) == 1:
            ranked = ranked_by_type[targets[0]][:limit]
        else:
            ranked = interleave({key: value[:limit] for key, value in ranked_by_type.items()}, limit)
        return {
            "items": [result.item.to_dict() for result in ranked],
            "target_media_types": list(targets),
            "comparison_count": database.comparison_count(),
            "negative_count": len(negative),
        }

    @app.post("/api/profile/reset")
    def reset_profile(request: Request) -> dict:
        database, _ = _context(request)
        database.reset_profile()
        return {"ok": True, "cached_item_count": database.count_items()}

    return app


app = create_app()
