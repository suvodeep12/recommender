# Repository Guidelines

## Project Structure & Module Organization

- `src/recommender/app.py` defines the FastAPI app and JSON routes.
- `src/recommender/tmdb.py` owns TMDB requests, normalization, and item types.
- `src/recommender/db.py` owns SQLite schema and profile persistence.
- `src/recommender/ranking.py` contains TF-IDF features and ranking logic.
- `src/recommender/static/` contains the vanilla HTML, CSS, and JavaScript client.
- `tests/` contains offline unit and API tests. Tests use fake TMDB responses and never require a network token.
- `data/` is runtime state and is ignored by Git.

Keep the application local-first. Do not commit `.env`, TMDB tokens, SQLite files, or generated Python caches.

## Build, Test, and Development Commands

Create the Windows virtual environment and install the project with test dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

Run the offline suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the local server:

```powershell
.\.venv\Scripts\python.exe -m uvicorn recommender.app:app --app-dir src --reload
```

Set `TMDB_API_READ_ACCESS_TOKEN` in the local environment before live catalog searches. Use `.env.example` as the variable reference.

## Coding Style & Naming Conventions

Use standard Python formatting with four-space indentation, `snake_case` for functions and variables, and `PascalCase` for classes. Keep API validation at the FastAPI boundary and keep ranking logic independent of HTTP. Use semantic HTML, native controls, CSS variables, keyboard-visible focus states, and `camelCase` for JavaScript variables and functions.

## Testing Guidelines

Add tests under `tests/` with `test_*.py` names. Cover normal behavior, invalid input, empty results, cache-only operation, provider failures, and profile reset. Keep tests deterministic and offline. The current suite validates metadata features, ranking, seed validation, pairwise state, caching, and the complete ten-comparison flow.

## Commit & Pull Request Guidelines

Use concise Conventional Commits such as `feat: add cross-media ranking` or `fix: reject duplicate seeds`. Pull requests should summarize behavior, list test commands and results, mention any configuration changes, and include screenshots for UI changes. Never include API tokens or local recommendation data.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `suvodeep12/recommender`; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default labels `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Read root `CONTEXT.md` and `docs/adr/` when they exist. See `docs/agents/domain.md`.
