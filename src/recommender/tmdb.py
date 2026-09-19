from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
MEDIA_TYPES = ("movie", "tv")

GENRE_NAMES = {
    12: "Adventure",
    14: "Fantasy",
    16: "Animation",
    18: "Drama",
    27: "Horror",
    28: "Action",
    35: "Comedy",
    36: "History",
    37: "Western",
    53: "Thriller",
    80: "Crime",
    99: "Documentary",
    878: "Science Fiction",
    9648: "Mystery",
    10749: "Romance",
    10751: "Family",
    10752: "War",
    10759: "Action and Adventure",
    10762: "Kids",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi and Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War and Politics",
    10770: "TV Movie",
}


class TMDBError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return text or "unknown"


def _entity_tokens(values: Iterable[Any], prefix: str, limit: int) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in list(values)[:limit]:
        if isinstance(value, dict):
            raw = value.get("name") or value.get("original_name") or value.get("title") or value.get("id")
        else:
            raw = value
        if raw is None:
            continue
        token = f"{prefix}:{_slug(raw)}"
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _keyword_values(payload: dict[str, Any]) -> list[Any]:
    keywords = payload.get("keywords") or {}
    if isinstance(keywords, dict):
        return keywords.get("keywords") or keywords.get("results") or []
    return keywords if isinstance(keywords, list) else []


def genre_label(value: Any) -> str:
    try:
        return GENRE_NAMES.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def _genre_tokens(values: Iterable[Any]) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = value.get("name") or value.get("id") if isinstance(value, dict) else value
        if raw is None:
            continue
        token = f"genre:{_slug(genre_label(raw))}"
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _credit_values(payload: dict[str, Any], key: str, limit: int) -> tuple[str, ...]:
    credits = payload.get("credits") or {}
    values = credits.get(key) or []
    if key == "crew":
        allowed_jobs = {"director", "creator", "writer", "screenplay", "executive producer"}
        values = [item for item in values if str(item.get("job", "")).lower() in allowed_jobs]
    return _entity_tokens(values, "person", limit)


def _company_values(payload: dict[str, Any]) -> tuple[str, ...]:
    values = list(payload.get("production_companies") or [])
    values.extend(payload.get("networks") or [])
    return _entity_tokens(values, "company", 12)


def _collection_values(payload: dict[str, Any]) -> tuple[str, ...]:
    collection = payload.get("belongs_to_collection")
    if isinstance(collection, dict):
        return _entity_tokens([collection], "collection", 1)
    return _entity_tokens(collection or [], "collection", 3)


@dataclass(frozen=True)
class Item:
    media_type: str
    tmdb_id: int
    title: str
    overview: str
    year: str
    poster_path: str | None
    genres: tuple[str, ...]
    keywords: tuple[str, ...]
    cast: tuple[str, ...]
    crew: tuple[str, ...]
    collections: tuple[str, ...]
    companies: tuple[str, ...]
    vote_average: float
    vote_count: int
    popularity: float
    raw: dict[str, Any]
    is_hydrated: bool = False

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.tmdb_id}"

    @property
    def poster_url(self) -> str | None:
        return f"{TMDB_IMAGE_BASE}{self.poster_path}" if self.poster_path else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "overview": self.overview,
            "year": self.year,
            "poster_path": self.poster_path,
            "poster_url": self.poster_url,
            "vote_average": self.vote_average,
            "vote_count": self.vote_count,
            "popularity": self.popularity,
            "genres": list(self.genres),
            "next_episode": self.raw.get("next_episode_to_air"),
        }


@dataclass(frozen=True)
class Episode:
    tv_id: int
    season_number: int
    episode_number: int
    name: str
    overview: str
    air_date: str
    runtime: int | None
    still_path: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tv_id": self.tv_id,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "name": self.name,
            "overview": self.overview,
            "air_date": self.air_date,
            "runtime": self.runtime,
            "still_path": self.still_path,
            "still_url": f"{TMDB_IMAGE_BASE}{self.still_path}" if self.still_path else None,
        }


def normalize_item(payload: dict[str, Any], media_type: str, hydrated: bool = False) -> Item:
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"Unsupported media type: {media_type}")
    if not payload.get("id"):
        raise ValueError("TMDB item is missing an id")

    release_date = payload.get("release_date") or payload.get("first_air_date") or ""
    genre_values = payload.get("genres") or payload.get("genre_ids") or []
    return Item(
        media_type=media_type,
        tmdb_id=int(payload["id"]),
        title=str(payload.get("title") or payload.get("name") or "Untitled"),
        overview=str(payload.get("overview") or ""),
        year=str(release_date)[:4],
        poster_path=payload.get("poster_path"),
        genres=_genre_tokens(list(genre_values)[:20]),
        keywords=_entity_tokens(_keyword_values(payload), "keyword", 30),
        cast=_credit_values(payload, "cast", 12),
        crew=_credit_values(payload, "crew", 12),
        collections=_collection_values(payload),
        companies=_company_values(payload),
        vote_average=float(payload.get("vote_average") or 0),
        vote_count=int(payload.get("vote_count") or 0),
        popularity=float(payload.get("popularity") or 0),
        raw=payload,
        is_hydrated=hydrated,
    )


def normalize_episode(payload: dict[str, Any], tv_id: int) -> Episode:
    if not payload.get("episode_number"):
        raise ValueError("TMDB episode is missing an episode number")
    return Episode(
        tv_id=tv_id,
        season_number=int(payload.get("season_number") or 0),
        episode_number=int(payload["episode_number"]),
        name=str(payload.get("name") or "Untitled episode"),
        overview=str(payload.get("overview") or ""),
        air_date=str(payload.get("air_date") or ""),
        runtime=int(payload["runtime"]) if payload.get("runtime") else None,
        still_path=payload.get("still_path"),
        raw=payload,
    )


class TMDBClient:
    def __init__(
        self,
        token: str | None = None,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 10,
    ):
        self.token = token if token is not None else os.getenv("TMDB_API_READ_ACCESS_TOKEN")
        self.opener = opener
        self.timeout = timeout

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise TMDBError(
                "tmdb_token_missing",
                503,
                "Set TMDB_API_READ_ACCESS_TOKEN before querying TMDB.",
            )

        query = f"?{urlencode(params or {})}" if params else ""
        request = Request(
            f"{TMDB_API_BASE}/{path}{query}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "accept": "application/json",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            try:
                body = response.read()
            finally:
                response.close()
            return json.loads(body.decode("utf-8"))
        except HTTPError as error:
            if error.code in (401, 403):
                raise TMDBError("tmdb_auth_error", 502, "TMDB rejected the configured token.") from error
            if error.code == 429:
                raise TMDBError("tmdb_rate_limited", 503, "TMDB is temporarily rate-limiting requests.") from error
            raise TMDBError("tmdb_provider_error", 502, "TMDB returned an unexpected error.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise TMDBError("tmdb_unavailable", 503, "TMDB could not be reached.") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TMDBError("tmdb_invalid_response", 502, "TMDB returned an invalid response.") from error

    def search(self, query: str, media_type: str) -> list[Item]:
        payload = self._request(f"search/{media_type}", {"query": query, "page": 1})
        return [normalize_item(item, media_type) for item in payload.get("results", []) if item.get("id")]

    def discover(self, media_type: str, pages: int = 5) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            payload = self._request(
                f"discover/{media_type}",
                {"page": page, "sort_by": "popularity.desc"},
            )
            for item in payload.get("results", []):
                if not item.get("id"):
                    continue
                normalized = normalize_item(item, media_type)
                if normalized.key not in seen:
                    seen.add(normalized.key)
                    items.append(normalized)
            if page >= int(payload.get("total_pages") or pages):
                break
        return items

    def details(self, media_type: str, tmdb_id: int) -> Item:
        payload = self._request(
            f"{media_type}/{tmdb_id}",
            {"append_to_response": "credits,keywords"},
        )
        return normalize_item(payload, media_type, hydrated=True)

    def season(self, tv_id: int, season_number: int) -> list[Episode]:
        payload = self._request(f"tv/{tv_id}/season/{season_number}")
        return [
            normalize_episode(item, tv_id)
            for item in payload.get("episodes", [])
            if item.get("episode_number")
        ]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
