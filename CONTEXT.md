# Scene Bridge Domain

## Item

An `Item` is a TMDB-backed movie or TV series. It is catalog metadata, not a user preference.

## Library entry

A `LibraryEntry` is the user's current relationship with an item. Its viewing status and sentiment are separate: a user can be watching something they dislike, or love something they have already watched.

## Episode progress

`EpisodeProgress` records the user's explicit progress through a TV episode. Air dates and episode metadata come from TMDB; watched state comes from the local profile.

## Learning event

A `LearningEvent` is an immutable explicit user action or migrated/imported fact that can influence the taste profile. Corrections append a new event; they do not rewrite the learning history.

## Taste profile

The `TasteProfile` is a derived weighted view of learning events. It is not a second source of truth and must be rebuildable from the event journal.

## Recommendation

A `Recommendation` is a ranked item or episode action generated from the taste profile and current library state. It is a hypothesis until the user records explicit feedback.

## Comparison

A `Comparison` is an explicit like-for-like choice between two candidates. It produces positive and negative learning events without treating exposure as feedback.
