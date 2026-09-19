from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import re
import unicodedata
from zipfile import BadZipFile, ZipFile

from .tmdb import Item


@dataclass(frozen=True)
class TVTimeRecord:
    source_key: str
    title: str
    year: str
    media_type: str
    status: str
    sentiment: str
    watched_episodes: tuple[dict, ...]
    last_watched_at: str | None
    raw: dict[str, str]

    def to_dict(self) -> dict:
        return asdict(self) | {"watched_episodes": list(self.watched_episodes)}


def _header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _row_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(_header(name), "")
        if value.strip():
            return value.strip()
    return ""


def _normalise_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "watched", "complete", "completed"}


def _sentiment(row: dict[str, str]) -> str:
    value = _row_value(row, "sentiment", "rating", "score", "user_rating")
    lowered = value.lower()
    if lowered in {"love", "loved", "favorite", "favourite"}:
        return "love"
    if lowered in {"like", "liked", "good"}:
        return "like"
    if lowered in {"dislike", "disliked", "bad", "not for me"}:
        return "not_for_me"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if score >= 8:
        return "love"
    if score >= 6:
        return "like"
    if score > 0 and score <= 4:
        return "not_for_me"
    return "neutral"


def _media_type(row: dict[str, str], filename: str) -> str:
    value = _row_value(row, "media_type", "type", "kind", "media")
    if value.lower() in {"movie", "film", "movies"}:
        return "movie"
    if value.lower() in {"tv", "show", "series", "tv_show", "tv series"}:
        return "tv"
    return "movie" if "movie" in filename.lower() or "film" in filename.lower() else "tv"


def _episode(row: dict[str, str]) -> dict | None:
    season = _row_value(row, "season", "season_number", "season_num")
    number = _row_value(row, "episode", "episode_number", "episode_num")
    try:
        season_number = int(float(season))
        episode_number = int(float(number))
    except (TypeError, ValueError):
        return None
    if season_number < 0 or episode_number <= 0:
        return None
    return {
        "season_number": season_number,
        "episode_number": episode_number,
        "name": _row_value(row, "episode_title", "episode_name", "name"),
        "air_date": _row_value(row, "air_date", "aired_at", "airdate"),
        "watched_at": _row_value(row, "watched_at", "date_watched", "completed_at", "watched_date"),
    }


def _read_csv(filename: str, text: str) -> list[tuple[str, dict[str, str]]]:
    reader = csv.DictReader(text.splitlines())
    output: list[tuple[str, dict[str, str]]] = []
    for row in reader:
        normalised = {_header(str(key)): str(value or "").strip() for key, value in row.items() if key}
        output.append((filename, normalised))
    return output


def _csv_files(filename: str, content: bytes) -> list[tuple[str, str]]:
    if filename.lower().endswith(".zip"):
        try:
            with ZipFile(content) as archive:
                return [
                    (name, archive.read(name).decode("utf-8-sig", errors="replace"))
                    for name in archive.namelist()
                    if name.lower().endswith(".csv")
                ]
        except (BadZipFile, OSError) as error:
            raise ValueError("The uploaded TV Time archive is not a readable ZIP file.") from error
    if filename.lower().endswith(".csv"):
        return [(filename, content.decode("utf-8-sig", errors="replace"))]
    raise ValueError("Upload a TV Time .zip archive or .csv file.")


def parse_tvtime_export(filename: str, content: bytes) -> list[TVTimeRecord]:
    grouped: dict[tuple[str, str, str], dict] = {}
    for csv_name, text in _csv_files(filename, content):
        for row_number, (source_name, row) in enumerate(_read_csv(csv_name, text), start=2):
            title = _row_value(row, "title", "show_title", "show_name", "series_name", "name")
            if not title:
                continue
            media_type = _media_type(row, source_name)
            year = _row_value(row, "year", "release_year", "first_air_year")[:4]
            key = (media_type, _normalise_title(title), year)
            grouped.setdefault(
                key,
                {
                    "source_key": f"{source_name}:{row_number}",
                    "title": title,
                    "year": year,
                    "media_type": media_type,
                    "status": "planned",
                    "sentiment": _sentiment(row),
                    "watched_episodes": {},
                    "last_watched_at": None,
                    "raw": row,
                },
            )
            record = grouped[key]
            episode = _episode(row)
            if episode:
                episode_key = (episode["season_number"], episode["episode_number"])
                record["watched_episodes"][episode_key] = episode
                record["status"] = "watched"
            if _truthy(_row_value(row, "watched", "is_watched", "completed", "seen")):
                record["status"] = "watched"
            watched_at = _row_value(row, "watched_at", "date_watched", "completed_at", "watched_date")
            if watched_at and (record["last_watched_at"] is None or watched_at > record["last_watched_at"]):
                record["last_watched_at"] = watched_at
            if record["sentiment"] == "neutral":
                record["sentiment"] = _sentiment(row)

    return [
        TVTimeRecord(
            source_key=value["source_key"],
            title=value["title"],
            year=value["year"],
            media_type=value["media_type"],
            status=value["status"],
            sentiment=value["sentiment"],
            watched_episodes=tuple(value["watched_episodes"].values()),
            last_watched_at=value["last_watched_at"],
            raw=value["raw"],
        )
        for value in grouped.values()
    ]


def _candidate_score(record: TVTimeRecord, item: Item) -> float:
    if record.media_type != item.media_type:
        return 0.0
    left = _normalise_title(record.title)
    right = _normalise_title(item.title)
    if not left or not right:
        return 0.0
    score = 1.0 if left == right else len(set(left.split()) & set(right.split())) / max(len(set(left.split()) | set(right.split())), 1)
    if record.year and item.year and record.year == item.year:
        score += 0.12
    return min(score, 1.0)


def match_records(records: list[TVTimeRecord], catalog: list[Item], limit: int = 5) -> list[dict]:
    matches: list[dict] = []
    for record in records:
        candidates = sorted(
            ((item, _candidate_score(record, item)) for item in catalog),
            key=lambda pair: (-pair[1], pair[0].title.casefold(), pair[0].key),
        )
        candidates = [pair for pair in candidates if pair[1] > 0][:limit]
        confidence = candidates[0][1] if candidates else 0.0
        status = "accepted" if confidence >= 0.98 and (len(candidates) == 1 or confidence > candidates[1][1] + 0.08) else "review"
        matches.append(
            {
                "source_key": record.source_key,
                "title": record.title,
                "year": record.year,
                "media_type": record.media_type,
                "status": status,
                "confidence": round(confidence, 3),
                "candidate_keys": [item.key for item, _ in candidates],
                "selected_key": candidates[0][0].key if status == "accepted" and candidates else None,
                "record": record.to_dict(),
            }
        )
    return matches
