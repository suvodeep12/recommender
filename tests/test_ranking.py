from recommender.ranking import (
    choose_pair,
    feature_document,
    pair_key,
    rank_items,
    target_media_types,
)
from recommender.tmdb import Item


def item(media_type: str, tmdb_id: int, title: str, genres: tuple[str, ...], score: float = 8.0) -> Item:
    return Item(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        overview=f"A story about {title.lower()}.",
        year="2024",
        poster_path=None,
        genres=genres,
        keywords=("keyword:story",),
        cast=(f"person:{tmdb_id}",),
        crew=(),
        collections=(),
        companies=(),
        vote_average=score,
        vote_count=100,
        popularity=10,
        raw={"id": tmdb_id},
        is_hydrated=True,
    )


def test_feature_document_keeps_structured_tokens():
    value = feature_document(item("movie", 1, "Orbit", ("genre:science-fiction",)))
    assert "genre:science-fiction" in value
    assert "title:Orbit" in value


def test_ranker_prefers_matching_metadata_and_is_deterministic():
    seed = item("movie", 1, "Deep Space", ("genre:science-fiction",))
    negative = item("tv", 2, "Farm Kitchen", ("genre:cooking",))
    match = item("tv", 3, "Outer Space", ("genre:science-fiction",))
    mismatch = item("tv", 4, "Kitchen Rules", ("genre:cooking",))

    first = rank_items([seed], [negative], [mismatch, match])
    second = rank_items([seed], [negative], [mismatch, match])

    assert first[0].item.key == match.key
    assert [result.item.key for result in first] == [result.item.key for result in second]


def test_target_media_type_switches_to_opposite_format_and_balances_ties():
    movies = [item("movie", index, f"Movie {index}", ("genre:drama",)) for index in range(1, 6)]
    tv = [item("tv", index, f"Show {index}", ("genre:drama",)) for index in range(1, 6)]
    assert target_media_types([*movies, tv[0]]) == ("tv",)
    assert target_media_types([*movies[:2], *tv[:2]]) == ("movie", "tv")


def test_choose_pair_skips_compared_pairs_and_negative_items():
    left = item("tv", 1, "Alpha", ("genre:drama",))
    right = item("tv", 2, "Beta", ("genre:drama",))
    blocked = item("tv", 3, "Blocked", ("genre:drama",))
    fresh = item("tv", 4, "Fresh", ("genre:drama",))
    ranked = rank_items([item("movie", 10, "Seed", ("genre:drama",))], [], [left, right, blocked, fresh])

    selected = choose_pair(ranked, {pair_key(left, right)}, {blocked.key})

    assert selected is not None
    assert blocked.key not in {selected[0].key, selected[1].key}
    assert selected[2] != pair_key(left, right)
