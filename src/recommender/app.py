from __future__ import annotations

import json
from pathlib import Path
import os
from typing import Literal

from fastapi import FastAPI, File, Query, Request, UploadFile
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
    pair_key,
    pair_target_media_type,
    profile_traits,
    rank_learning_events,
    target_media_types,
)
from .tmdb import Item, MEDIA_TYPES, TMDBClient, TMDBError
from .tvtime import match_records, parse_tvtime_export


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


class LibraryUpdate(BaseModel):
    status: Literal["planned", "watching", "watched", "dropped"] | None = None
    sentiment: Literal["love", "like", "neutral", "dislike", "not_for_me"] | None = None
    saved: bool | None = None
    hidden: bool | None = None
    learning_enabled: bool | None = None
    context: dict = Field(default_factory=dict)


class EpisodeProgressRequest(BaseModel):
    watched: bool = True
    metadata: dict = Field(default_factory=dict)
    watched_at: str | None = None


class TasteNoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=240)
    polarity: Literal["positive", "negative"] = "positive"


class LearningPauseRequest(BaseModel):
    paused: bool


class ImportMatchChoice(BaseModel):
    match_id: int = Field(gt=0)
    selected_key: str | None = None
    status: Literal["accepted", "skipped", "review"] = "accepted"


class ImportCommitRequest(BaseModel):
    matches: list[ImportMatchChoice] = Field(default_factory=list)


def _default_database_path() -> Path:
    return Path(os.getenv("RECOMMENDER_DB_PATH", "data/recommender.sqlite3"))


def _context(request: Request) -> tuple[Database, TMDBClient]:
    return request.app.state.database, request.app.state.tmdb


def _app_error(error: TMDBError) -> AppError:
    return AppError(error.code, error.status_code, error.message)


def _item_key_set(items: list[Item]) -> set[str]:
    return {item.key for item in items}


def _ensure_item(database: Database, tmdb: TMDBClient, input_item: MediaItemInput) -> Item:
    item = database.get_item(input_item.media_type, input_item.tmdb_id)
    if item:
        return item
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
    return item


def _ensure_candidates(database: Database, tmdb: TMDBClient, media_type: str) -> list[Item]:
    cached = database.items_for_media(media_type, limit=120)
    if len(cached) < 40 and tmdb.has_token:
        try:
            database.upsert_items(tmdb.discover(media_type, pages=2))
        except TMDBError as error:
            if not cached:
                raise _app_error(error) from error
        cached = database.items_for_media(media_type, limit=120)
    if not cached and not tmdb.has_token:
        raise AppError(
            "tmdb_token_missing",
            503,
            "Set TMDB_API_READ_ACCESS_TOKEN or populate the local catalog first.",
        )
    return cached


def _profile_ready(database: Database) -> None:
    if not database.has_learning_evidence():
        raise AppError("seeds_required", 409, f"Select {MIN_SEEDS} to {MAX_SEEDS} seed items or import a history first.")


def _profile_rows(database: Database) -> list[dict]:
    _profile_ready(database)
    return database.learning_signal_rows()


def _library_map(database: Database) -> dict[str, dict]:
    return {item["key"]: item["library"] for item in database.library_items("all", limit=5000)}


def _negative_keys(database: Database) -> set[str]:
    return {
        item["key"]
        for item in database.library_items("all", limit=5000)
        if item["library"]["hidden"] or item["library"]["sentiment"] == "not_for_me"
    }


def _rank_catalog(database: Database, tmdb: TMDBClient, limit: int = 500, context: dict | None = None) -> list:
    rows = _profile_rows(database)
    context = context or {}
    notes = database.taste_notes()
    if context.get("mood"):
        notes = [*notes, {"text": str(context["mood"]), "polarity": "positive"}]
    candidates: list[Item] = []
    for media_type in MEDIA_TYPES:
        candidates.extend(_ensure_candidates(database, tmdb, media_type))
    blocked = _negative_keys(database)
    candidates = [item for item in candidates if item.key not in blocked]
    minutes = context.get("minutes")
    if isinstance(minutes, int) and minutes > 0:
        filtered = [
            item
            for item in candidates
            if not isinstance(item.raw.get("runtime"), (int, float)) or item.raw["runtime"] <= minutes
        ]
        if filtered:
            candidates = filtered
    return rank_learning_events(rows, candidates, notes=notes, limit=limit)


def _recommendation_dict(database: Database, result, section: str, *, extra: dict | None = None) -> dict:
    value = result.item.to_dict() | {
        "score": round(result.score, 5),
        "confidence": round(result.confidence, 5),
        "reasons": list(result.reasons),
        "section": section,
        "library": database.library_entry(result.item.media_type, result.item.tmdb_id),
    }
    if extra:
        value.update(extra)
    return value


def _recommendation_sections(
    database: Database,
    tmdb: TMDBClient,
    limit: int,
    cursor: int,
    context: dict | None = None,
) -> tuple[dict[str, list[dict]], list[str]]:
    ranked = _rank_catalog(database, tmdb, limit=500, context=context)
    library = _library_map(database)
    rows = _profile_rows(database)
    positive_items = [row["item"] for row in rows if row["signal"] > 0]
    dominant = target_media_types(positive_items)
    sections: dict[str, list[dict]] = {
        "continue": [],
        "upcoming": [],
        "strong_matches": [],
        "adjacent_discoveries": [],
        "cross_media": [],
        "rewatch": [],
    }

    for item in database.library_items("watching", limit=50):
        sections["continue"].append(
            item
            | {
                "section": "continue",
                "score": 1.0,
                "confidence": 1.0,
                "reasons": ["Continue from your library"],
            }
        )
    for item in database.library_items("upcoming", limit=50):
        sections["upcoming"].append(
            item
            | {
                "section": "upcoming",
                "score": 1.0,
                "confidence": 1.0,
                "reasons": ["Upcoming in your library"],
            }
        )

    for result in ranked:
        state = library.get(result.item.key, {})
        if state.get("hidden") or state.get("sentiment") == "not_for_me":
            continue
        if state.get("status") == "watched":
            if state.get("sentiment") == "love":
                sections["rewatch"].append(_recommendation_dict(database, result, "rewatch"))
            continue
        if result.item.media_type in dominant:
            section = "strong_matches" if result.confidence >= 0.25 else "adjacent_discoveries"
        else:
            section = "cross_media"
        sections[section].append(_recommendation_dict(database, result, section))

    order = ["continue", "upcoming", "strong_matches", "adjacent_discoveries", "cross_media", "rewatch"]
    flat = [(section, item) for section in order for item in sections[section]]
    page = flat[cursor : cursor + limit]
    visible = {section: [] for section in order}
    for section, item in page:
        visible[section].append(item)
    return {section: values for section, values in visible.items() if values}, list(dominant)


def _pair_state(database: Database, tmdb: TMDBClient, allow_extended: bool = False) -> dict:
    rows = _profile_rows(database)
    count = database.comparison_count()
    if count >= TOTAL_COMPARISONS and not allow_extended:
        return {"complete": True, "round": count, "total_rounds": TOTAL_COMPARISONS}

    seeds = database.seed_items()
    if seeds:
        media_type = pair_target_media_type(seeds, count)
    else:
        counts = database.profile_media_counts()
        media_type = "tv" if counts.get("tv", 0) >= counts.get("movie", 0) else "movie"
    candidates = _ensure_candidates(database, tmdb, media_type)
    ranked = rank_learning_events(rows, candidates, notes=database.taste_notes(), limit=100)
    selected = choose_pair(ranked, database.comparison_pair_ids(), _negative_keys(database))
    if selected is None:
        raise AppError("pair_candidates_exhausted", 409, "There are not enough uncategorized candidates for another comparison.")
    left, right, identifier = selected
    return {
        "complete": False,
        "pair_id": identifier,
        "left": left.to_dict(),
        "right": right.to_dict(),
        "target_media_type": media_type,
        "round": count,
        "total_rounds": None if allow_extended else TOTAL_COMPARISONS,
    }


def _signal_for_status(status: str) -> float:
    return {"planned": 0.15, "watching": 0.25, "watched": 0.55, "dropped": -0.5}[status]


def _signal_for_sentiment(sentiment: str) -> float:
    return {"love": 3.0, "like": 2.0, "neutral": 0.0, "dislike": -2.0, "not_for_me": -3.0}[sentiment]


def create_app(database_path: str | Path | None = None, tmdb_client: TMDBClient | None = None) -> FastAPI:
    database = Database(database_path or _default_database_path())
    database.init()
    tmdb = tmdb_client or TMDBClient()

    app = FastAPI(title="Scene Bridge", version="0.2.0")
    app.state.database = database
    app.state.tmdb = tmdb
    static_directory = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message}})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(part) for part in detail["loc"]) for detail in error.errors())
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": f"Check these fields: {fields or 'request'}."}})

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    @app.get("/api/search")
    def search(request: Request, q: str = Query(min_length=1, max_length=120), media_type: Literal["movie", "tv", "all"] = "all") -> dict:
        database, tmdb = _context(request)
        query = q.strip()
        if not query:
            raise AppError("query_required", 422, "Enter a title to search.")
        media_types = MEDIA_TYPES if media_type == "all" else (media_type,)
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
            raise AppError("tmdb_token_missing", 503, "Set TMDB_API_READ_ACCESS_TOKEN to search titles not already cached.")
        return {"items": [item.to_dict() for item in items[:20]]}

    @app.post("/api/profile/seeds")
    def set_seeds(payload: SeedRequest, request: Request) -> dict:
        database, tmdb = _context(request)
        if not MIN_SEEDS <= len(payload.items) <= MAX_SEEDS:
            raise AppError("seed_count_invalid", 422, f"Select between {MIN_SEEDS} and {MAX_SEEDS} positive seed items.")
        if len({item.key for item in payload.items}) != len(payload.items):
            raise AppError("duplicate_seed", 422, "Each seed item must be unique.")
        selected = [_ensure_item(database, tmdb, input_item) for input_item in payload.items]
        if database.has_learning_evidence():
            database.add_seed_events(selected)
        else:
            database.replace_profile(selected)
        return {
            "items": [item.to_dict() for item in selected],
            "comparison_count": database.comparison_count(),
            "total_comparisons": TOTAL_COMPARISONS,
            "minimum_seeds": MIN_SEEDS,
            "maximum_seeds": MAX_SEEDS,
        }

    @app.get("/api/profile")
    def get_profile(request: Request) -> dict:
        database, _ = _context(request)
        seeds = database.seed_items()
        return {
            "items": [item.to_dict() for item in seeds],
            "has_profile": database.has_learning_evidence(),
            "comparison_count": database.comparison_count(),
            "total_comparisons": TOTAL_COMPARISONS,
            "minimum_seeds": MIN_SEEDS,
            "maximum_seeds": MAX_SEEDS,
            "event_count": database.learning_event_count(),
            "library_count": len(database.library_items("all", limit=5000)),
            "learning_paused": database.learning_paused(),
            "complete": database.comparison_count() >= TOTAL_COMPARISONS,
        }

    @app.get("/api/profile/taste")
    def taste_profile(request: Request) -> dict:
        database, _ = _context(request)
        rows = database.learning_signal_rows()
        return {
            "traits": profile_traits(rows, database.taste_notes()),
            "event_count": database.learning_event_count(),
            "learning_paused": database.learning_paused(),
            "media_counts": database.profile_media_counts(),
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

    @app.get("/api/learning/compare")
    def get_adaptive_comparison(request: Request) -> dict:
        database, tmdb = _context(request)
        return _pair_state(database, tmdb, allow_extended=True)

    @app.post("/api/learning/compare")
    def choose_adaptive_comparison(payload: PairRequest, request: Request) -> dict:
        database, tmdb = _context(request)
        state = _pair_state(database, tmdb, allow_extended=True)
        if payload.pair_id != state["pair_id"]:
            raise AppError("stale_pair", 409, "That comparison is no longer current. Load the next pair.")
        left = database.get_item_by_key(state["left"]["key"])
        right = database.get_item_by_key(state["right"]["key"])
        winner = database.get_item_by_key(payload.winner_key)
        if not left or not right or not winner or winner.key not in {left.key, right.key}:
            raise AppError("winner_invalid", 422, "Choose one of the two displayed candidates.")
        loser = right if winner.key == left.key else left
        database.record_comparison(pair_key(left, right), left, right, winner, loser)
        return _pair_state(database, tmdb, allow_extended=True)

    @app.get("/api/library")
    def library(request: Request, view: Literal["all", "up_next", "watching", "watched", "watchlist", "rewatch", "upcoming"] = "all", limit: int = Query(default=100, ge=1, le=500)) -> dict:
        database, _ = _context(request)
        try:
            items = database.library_items(view, limit)
        except ValueError as error:
            raise AppError("library_view_invalid", 422, str(error)) from error
        return {"view": view, "items": items}

    @app.patch("/api/library/items/{media_type}/{tmdb_id}")
    def update_library_item(media_type: Literal["movie", "tv"], tmdb_id: int, payload: LibraryUpdate, request: Request) -> dict:
        database, tmdb = _context(request)
        item = _ensure_item(database, tmdb, MediaItemInput(media_type=media_type, tmdb_id=tmdb_id))
        events: list[tuple[str, str, float]] = []
        if payload.status:
            events.append(("status", payload.status, _signal_for_status(payload.status)))
        if payload.sentiment:
            events.append(("sentiment", payload.sentiment, _signal_for_sentiment(payload.sentiment)))
        if payload.saved is not None:
            events.append(("save" if payload.saved else "unsave", None, 0.2 if payload.saved else 0.0))
        if payload.hidden is not None:
            events.append(("hide" if payload.hidden else "unhide", None, 0.0))
        if payload.learning_enabled is not None:
            events.append(("learning_include" if payload.learning_enabled else "learning_exclude", None, 0.0))
        for event_type, value, signal in events:
            database.record_event(event_type, item=item, value=value, signal=signal, context=payload.context)
        return {"item": next((value for value in database.library_items("all", 5000) if value["key"] == item.key), item.to_dict())}

    @app.patch("/api/library/items/{media_type}/{tmdb_id}/learning")
    def update_item_learning(media_type: Literal["movie", "tv"], tmdb_id: int, payload: LearningPauseRequest, request: Request) -> dict:
        database, tmdb = _context(request)
        item = _ensure_item(database, tmdb, MediaItemInput(media_type=media_type, tmdb_id=tmdb_id))
        database.record_event(
            "learning_include" if not payload.paused else "learning_exclude",
            item=item,
            signal=0,
        )
        return {"item": next((value for value in database.library_items("all", 5000) if value["key"] == item.key), item.to_dict())}

    @app.get("/api/library/episodes/{tv_id}")
    def library_episodes(tv_id: int, request: Request, limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
        database, _ = _context(request)
        return {"tv_id": tv_id, "episodes": database.episodes_for_tv(tv_id, limit)}

    @app.patch("/api/library/episodes/{tv_id}/{season_number}/{episode_number}")
    def update_episode(tv_id: int, season_number: int, episode_number: int, payload: EpisodeProgressRequest, request: Request) -> dict:
        database, _ = _context(request)
        database.record_episode_progress(
            tv_id=tv_id,
            season_number=season_number,
            episode_number=episode_number,
            watched=payload.watched,
            metadata=payload.metadata,
            watched_at=payload.watched_at,
        )
        return {"tv_id": tv_id, "episodes": database.episodes_for_tv(tv_id)}

    @app.post("/api/profile/taste-notes")
    def add_taste_note(payload: TasteNoteRequest, request: Request) -> dict:
        database, _ = _context(request)
        try:
            return database.add_taste_note(payload.text, payload.polarity)
        except ValueError as error:
            raise AppError("taste_note_invalid", 422, str(error)) from error

    @app.post("/api/learning/pause")
    def pause_learning(payload: LearningPauseRequest, request: Request) -> dict:
        database, _ = _context(request)
        database.set_learning_paused(payload.paused)
        return {"learning_paused": payload.paused}

    @app.get("/api/recommendations")
    def recommendations(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: int = Query(default=0, ge=0),
        context: str | None = Query(default=None, max_length=1000),
    ) -> dict:
        database, tmdb = _context(request)
        if context:
            try:
                json.loads(context)
            except json.JSONDecodeError as error:
                raise AppError("context_invalid", 422, "Recommendation context must be valid JSON.") from error
        session_context = json.loads(context) if context else {}
        sections, targets = _recommendation_sections(database, tmdb, limit, cursor, session_context)
        items = [item for values in sections.values() for item in values]
        has_more = len(items) >= limit
        return {
            "items": items,
            "sections": sections,
            "target_media_types": targets,
            "comparison_count": database.comparison_count(),
            "negative_count": len(_negative_keys(database)),
            "next_cursor": cursor + limit if has_more else None,
        }

    @app.post("/api/import/tvtime/preview")
    async def preview_tvtime(request: Request, file: UploadFile = File(...)) -> dict:
        database, tmdb = _context(request)
        filename = file.filename or "tvtime-export.zip"
        try:
            records = parse_tvtime_export(filename, await file.read())
        except ValueError as error:
            raise AppError("tvtime_import_invalid", 422, str(error)) from error
        catalog = database.items_for_media("movie", 1000) + database.items_for_media("tv", 1000)
        matches: list[dict] = []
        for record in records:
            local_matches = match_records([record], catalog)[0]
            if local_matches["status"] == "review" and tmdb.has_token:
                try:
                    remote = tmdb.search(record.title, record.media_type)
                except TMDBError:
                    remote = []
                if remote:
                    database.upsert_items(remote)
                    catalog.extend(remote)
                    local_matches = match_records([record], catalog)[0]
            matches.append(local_matches)
        batch_id = database.create_import_batch(filename, {"records": len(records)})
        database.save_import_matches(batch_id, matches)
        stored = database.get_import_matches(batch_id)
        return {
            "import_id": batch_id,
            "matches": stored,
            "summary": {
                "records": len(records),
                "accepted": sum(match["status"] == "accepted" for match in matches),
                "review": sum(match["status"] == "review" for match in matches),
            },
        }

    @app.get("/api/import/{import_id}/matches")
    def import_matches(import_id: str, request: Request) -> dict:
        database, _ = _context(request)
        if not database.get_import_batch(import_id):
            raise AppError("import_not_found", 404, "That import batch does not exist.")
        return {"import_id": import_id, "matches": database.get_import_matches(import_id)}

    @app.post("/api/import/tvtime/commit")
    def commit_tvtime(payload: ImportCommitRequest, request: Request, import_id: str = Query(min_length=1)) -> dict:
        database, _ = _context(request)
        batch = database.get_import_batch(import_id)
        if not batch:
            raise AppError("import_not_found", 404, "That import batch does not exist.")
        if batch["status"] == "committed":
            return {"import_id": import_id, "status": "committed", "summary": batch["summary"]}
        for choice in payload.matches:
            database.update_import_match(choice.match_id, choice.selected_key, choice.status)
        matches = database.get_import_matches(import_id)
        accepted = skipped = reviewed = 0
        for match in matches:
            if match["status"] == "review":
                reviewed += 1
                continue
            if match["status"] == "skipped" or not match["selected_key"]:
                skipped += 1
                continue
            item = database.get_item_by_key(match["selected_key"])
            if not item:
                reviewed += 1
                continue
            record = match["record"]
            database.record_event(
                "import_status",
                item=item,
                value=record.get("status", "planned"),
                signal=0.45 if record.get("status") == "watched" else 0.15,
                source="tvtime_import",
                occurred_at=record.get("last_watched_at"),
            )
            sentiment = record.get("sentiment", "neutral")
            if sentiment != "neutral":
                database.record_event(
                    "import_sentiment",
                    item=item,
                    value=sentiment,
                    signal=_signal_for_sentiment(sentiment) * 0.65,
                    source="tvtime_import",
                    occurred_at=record.get("last_watched_at"),
                )
            for episode in record.get("watched_episodes", []):
                database.record_episode_progress(
                    tv_id=item.tmdb_id,
                    season_number=int(episode["season_number"]),
                    episode_number=int(episode["episode_number"]),
                    watched=True,
                    metadata=episode,
                    source="tvtime_import",
                    watched_at=episode.get("watched_at") or record.get("last_watched_at"),
                )
            accepted += 1
        summary = {"accepted": accepted, "skipped": skipped, "review": reviewed}
        database.mark_import_committed(import_id, summary)
        return {"import_id": import_id, "status": "committed", "summary": summary}

    @app.post("/api/profile/export")
    def export_profile(request: Request) -> dict:
        database, _ = _context(request)
        return database.export_profile()

    @app.post("/api/profile/import")
    def import_profile(payload: dict, request: Request) -> dict:
        database, _ = _context(request)
        try:
            return database.import_profile(payload)
        except (TypeError, ValueError, KeyError) as error:
            raise AppError("profile_import_invalid", 422, str(error)) from error

    @app.post("/api/profile/reset")
    def reset_profile(request: Request) -> dict:
        database, _ = _context(request)
        database.reset_profile()
        return {"ok": True, "cached_item_count": database.count_items()}

    return app


app = create_app()
