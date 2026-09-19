from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .tmdb import Item


TOTAL_COMPARISONS = 10


@dataclass(frozen=True)
class RankedItem:
    item: Item
    score: float


def feature_document(item: Item) -> str:
    parts = [
        f"title:{item.title}",
        item.overview,
        *item.genres,
        *item.keywords,
        *item.cast,
        *item.crew,
        *item.collections,
        *item.companies,
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


def rank_items(
    positive_items: Iterable[Item],
    negative_items: Iterable[Item],
    candidates: Iterable[Item],
    limit: int = 100,
) -> list[RankedItem]:
    positive = _unique(positive_items)
    negative = _unique(negative_items)
    candidate_list = _unique(candidates)
    if not positive or not candidate_list:
        return []

    training_items = _unique([*positive, *negative, *candidate_list])
    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        sublinear_tf=True,
        token_pattern=r"(?u)\b[\w:-]+\b",
    )
    matrix = vectorizer.fit_transform([feature_document(item) for item in training_items])
    index = {item.key: position for position, item in enumerate(training_items)}

    positive_matrix = matrix[[index[item.key] for item in positive]]
    profile = np.asarray(positive_matrix.mean(axis=0)).reshape(-1)
    if negative:
        negative_matrix = matrix[[index[item.key] for item in negative]]
        profile -= np.asarray(negative_matrix.mean(axis=0)).reshape(-1)
    if not np.any(profile):
        profile = np.asarray(positive_matrix.mean(axis=0)).reshape(-1)

    candidate_positions = [index[item.key] for item in candidate_list]
    similarity = cosine_similarity(matrix[candidate_positions], profile.reshape(1, -1)).reshape(-1)

    quality_values = np.array(
        [math.log1p(max(item.vote_count, 0)) * max(item.vote_average, 0) for item in candidate_list],
        dtype=float,
    )
    quality_min = float(quality_values.min())
    quality_max = float(quality_values.max())
    if quality_max == quality_min:
        quality = np.full(len(candidate_list), 0.5)
    else:
        quality = (quality_values - quality_min) / (quality_max - quality_min)

    scores = 0.85 * similarity + 0.15 * quality
    ranked = [RankedItem(item, float(score)) for item, score in zip(candidate_list, scores)]
    ranked.sort(key=lambda result: (-result.score, result.item.title.casefold(), result.item.key))
    return ranked[: max(1, min(limit, len(ranked)))]


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
    available = [result.item for result in list(ranked_items)[:window] if result.item.key not in negative_keys]
    for left_index, left in enumerate(available):
        for right in available[left_index + 1 :]:
            identifier = pair_key(left, right)
            if identifier not in compared_pair_ids:
                return left, right, identifier
    return None


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
