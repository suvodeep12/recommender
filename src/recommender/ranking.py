from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .tmdb import Item, genre_label


TOTAL_COMPARISONS = 10
MIN_SEEDS = 5
MAX_SEEDS = 10


@dataclass(frozen=True)
class RankedItem:
    item: Item
    score: float
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()


def feature_tokens(item: Item) -> tuple[str, ...]:
    return tuple(
        token
        for token in (
            *item.genres,
            *item.keywords,
            *item.cast,
            *item.crew,
            *item.collections,
            *item.companies,
        )
        if token
    )


def feature_document(item: Item) -> str:
    parts = [
        f"media:{item.media_type}",
        f"title:{item.title}",
        item.overview,
        *feature_tokens(item),
    ]
    document = " ".join(part for part in parts if part).strip()
    return document or f"item:{item.key}"


def _unique(items: Iterable[Item]) -> list[Item]:
    result: list[Item] = []
    seen: set[str] = set()
    for item in items:
        if item.key not in seen:
            seen.add(item.key)
            result.append(item)
    return result


def _weighted_unique(items: Iterable[tuple[Item, float]]) -> tuple[list[Item], list[float]]:
    weights: dict[str, float] = {}
    values: dict[str, Item] = {}
    for item, weight in items:
        if item.key not in values:
            values[item.key] = item
            weights[item.key] = 0.0
        weights[item.key] += float(weight)
    ordered = list(values)
    return [values[key] for key in ordered], [weights[key] for key in ordered]


def _token_profile(items: Iterable[Item], weights: Iterable[float]) -> Counter[str]:
    profile: Counter[str] = Counter()
    for item, weight in zip(items, weights):
        for token in set(feature_tokens(item)):
            profile[token] += max(float(weight), 0.0)
    return profile


def _human_token(token: str) -> str:
    prefix, _, value = token.partition(":")
    if prefix in {"genre", "keyword", "collection", "company", "person"}:
        label = genre_label(value) if prefix == "genre" else value
        return label.replace("-", " ").strip().title()
    return token.replace("-", " ").strip().title()


def _reasons(item: Item, positive_tokens: Counter[str], negative_tokens: Counter[str]) -> tuple[str, ...]:
    shared = sorted(
        ((token, weight) for token, weight in positive_tokens.items() if token in feature_tokens(item)),
        key=lambda pair: (-pair[1], pair[0]),
    )
    blocked = {token for token, weight in negative_tokens.items() if weight > 0}
    return tuple(_human_token(token) for token, _ in shared if token not in blocked)[:3]


def rank_items(
    positive_items: Iterable[Item],
    negative_items: Iterable[Item],
    candidates: Iterable[Item],
    limit: int = 100,
    *,
    positive_weights: Iterable[float] | None = None,
    negative_weights: Iterable[float] | None = None,
    profile_notes: Iterable[tuple[str, str]] = (),
) -> list[RankedItem]:
    positive = _unique(positive_items)
    negative = _unique(negative_items)
    candidate_list = _unique(candidates)
    if not positive or not candidate_list:
        return []

    positive_weights = list(positive_weights or [1.0] * len(positive))
    negative_weights = list(negative_weights or [1.0] * len(negative))
    if len(positive_weights) != len(positive):
        positive_weights = [1.0] * len(positive)
    if len(negative_weights) != len(negative):
        negative_weights = [1.0] * len(negative)

    notes = [(text.strip(), polarity) for text, polarity in profile_notes if text.strip()]
    training_items = _unique([*positive, *negative, *candidate_list])
    documents = [feature_document(item) for item in training_items]
    note_indexes: list[tuple[int, str, float]] = []
    for text, polarity in notes:
        note_indexes.append((len(documents), polarity, 1.0))
        documents.append(f"note:{text}")

    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        sublinear_tf=True,
        token_pattern=r"(?u)\b[\w:-]+\b",
    )
    matrix = vectorizer.fit_transform(documents)
    index = {item.key: position for position, item in enumerate(training_items)}

    profile = np.zeros(matrix.shape[1], dtype=float)
    positive_matrix = matrix[[index[item.key] for item in positive]].toarray()
    profile += (positive_matrix * np.asarray(positive_weights)[:, None]).sum(axis=0)
    if negative:
        negative_matrix = matrix[[index[item.key] for item in negative]].toarray()
        profile -= (negative_matrix * np.asarray(negative_weights)[:, None]).sum(axis=0)
    for note_index, polarity, weight in note_indexes:
        note_vector = matrix[note_index].toarray().reshape(-1)
        profile += note_vector * weight if polarity == "positive" else -note_vector * weight
    if not np.any(profile):
        profile = positive_matrix.mean(axis=0)

    candidate_positions = [index[item.key] for item in candidate_list]
    similarity = cosine_similarity(matrix[candidate_positions], profile.reshape(1, -1)).reshape(-1)

    positive_tokens = _token_profile(positive, positive_weights)
    negative_tokens = _token_profile(negative, negative_weights)
    graph_values = np.array(
        [
            sum(positive_tokens[token] for token in set(feature_tokens(item)))
            - sum(negative_tokens[token] for token in set(feature_tokens(item)))
            for item in candidate_list
        ],
        dtype=float,
    )
    if graph_values.max() == graph_values.min():
        graph = np.full(len(candidate_list), 0.5)
    else:
        graph = (graph_values - graph_values.min()) / (graph_values.max() - graph_values.min())

    quality_values = np.array(
        [math.log1p(max(item.vote_count, 0)) * max(item.vote_average, 0) for item in candidate_list],
        dtype=float,
    )
    if quality_values.max() == quality_values.min():
        quality = np.full(len(candidate_list), 0.5)
    else:
        quality = (quality_values - quality_values.min()) / (quality_values.max() - quality_values.min())

    scores = 0.70 * similarity + 0.15 * graph + 0.15 * quality
    ranked = [
        RankedItem(
            item,
            float(score),
            confidence=float(max(0.0, min(1.0, similarity[index]))),
            reasons=_reasons(item, positive_tokens, negative_tokens),
        )
        for index, (item, score) in enumerate(zip(candidate_list, scores))
    ]
    ranked.sort(key=lambda result: (-result.score, result.item.title.casefold(), result.item.key))
    return ranked[: max(1, min(limit, len(ranked)))]


def _event_weight(row: dict, now: datetime) -> float:
    signal = float(row.get("signal") or 0)
    if not signal:
        return 0.0
    try:
        occurred = datetime.fromisoformat(str(row.get("occurred_at")).replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        occurred = now
    age_days = max(0.0, (now - occurred).total_seconds() / 86400)
    decay = 0.35 + 0.65 * math.exp(-age_days / 365)
    if row.get("event_type") in {"seed", "sentiment"} and row.get("value") in {"love", "like"}:
        decay = max(decay, 0.8)
    if row.get("event_type") == "comparison_winner":
        decay = max(decay, 0.6)
    if row.get("source") == "tvtime_import":
        decay *= 0.65
    return signal * decay


def rank_learning_events(
    event_rows: Iterable[dict],
    candidates: Iterable[Item],
    *,
    notes: Iterable[dict] = (),
    limit: int = 100,
    now: datetime | None = None,
) -> list[RankedItem]:
    # Day-level recency keeps a GET followed by a POST on the same screen
    # deterministic. A request-time microsecond should never reshuffle a pair.
    now = now or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    positive: list[tuple[Item, float]] = []
    negative: list[tuple[Item, float]] = []
    for row in event_rows:
        item = row.get("item")
        weight = _event_weight(row, now)
        if not item or not weight:
            continue
        (positive if weight > 0 else negative).append((item, abs(weight)))
    if not positive:
        return []
    positive_items, positive_weights = _weighted_unique(positive)
    negative_items, negative_weights = _weighted_unique(negative)
    profile_notes = [(note.get("text", ""), note.get("polarity", "positive")) for note in notes]
    return rank_items(
        positive_items,
        negative_items,
        candidates,
        limit=limit,
        positive_weights=positive_weights,
        negative_weights=negative_weights,
        profile_notes=profile_notes,
    )


def profile_traits(event_rows: Iterable[dict], notes: Iterable[dict] = (), limit: int = 8) -> dict[str, list[str]]:
    positive: Counter[str] = Counter()
    negative: Counter[str] = Counter()
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for row in event_rows:
        item = row.get("item")
        weight = _event_weight(row, now)
        if not item or not weight:
            continue
        target = positive if weight > 0 else negative
        for token in set(feature_tokens(item)):
            target[token] += abs(weight)
    for note in notes:
        target = positive if note.get("polarity") == "positive" else negative
        target[f"note:{note.get('text', '')}"] += 1
    return {
        "positive": [_human_token(token) for token, _ in positive.most_common(limit)],
        "negative": [_human_token(token) for token, _ in negative.most_common(limit)],
    }


def target_media_types(seed_items: Iterable[Item]) -> tuple[str, ...]:
    counts = Counter(item.media_type for item in seed_items)
    movies = counts.get("movie", 0)
    tv = counts.get("tv", 0)
    if movies > tv:
        return ("tv",)
    if tv > movies:
        return ("movie",)
    return ("movie", "tv")


def pair_target_media_type(seed_items: Iterable[Item], comparison_count: int) -> str:
    targets = target_media_types(seed_items)
    return targets[comparison_count % len(targets)]


def pair_key(left: Item, right: Item) -> str:
    return "|".join(sorted((left.key, right.key)))


def choose_pair(
    ranked_items: Iterable[RankedItem],
    compared_pair_ids: set[str],
    negative_keys: set[str],
    window: int = 20,
) -> tuple[Item, Item, str] | None:
    ranked = [result for result in list(ranked_items)[:window] if result.item.key not in negative_keys]
    pairs: list[tuple[float, str, Item, Item]] = []
    for left_index, left in enumerate(ranked):
        for right in ranked[left_index + 1 :]:
            identifier = pair_key(left.item, right.item)
            if identifier not in compared_pair_ids:
                pairs.append((abs(left.score - right.score), identifier, left.item, right.item))
    if not pairs:
        return None
    _, identifier, left, right = min(pairs, key=lambda value: (value[0], value[1]))
    return left, right, identifier


def interleave(results: dict[str, list[RankedItem]], limit: int) -> list[RankedItem]:
    output: list[RankedItem] = []
    positions = {media_type: 0 for media_type in results}
    media_types = list(results)
    while len(output) < limit and media_types:
        progressed = False
        for media_type in media_types:
            position = positions[media_type]
            if position >= len(results[media_type]):
                continue
            output.append(results[media_type][position])
            positions[media_type] += 1
            progressed = True
            if len(output) >= limit:
                break
        if not progressed:
            break
    return output
