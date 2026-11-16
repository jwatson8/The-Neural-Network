"""Movie catalog interactions."""

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from src.database.interactions._shared import (
    UNSET,
    RecordNotFoundError,
    UnsetType,
    create_missing_records,
    create_record,
    run,
    schema_values,
    supplied,
    update_record,
)
from src.database.models import Movie
from src.database.schemas import MovieCreate, MovieUpdate

__all__ = ["create", "get", "update", "list_records", "create_missing_many", "get_license_costs"]

### ----------- Helper Functions -----------


def _create(session: Session, data: MovieCreate) -> Movie:
    return create_record(session, Movie, schema_values(data))


def _get(session: Session, movie_id: str) -> Movie | None:
    return session.get(Movie, movie_id)


def _update(session: Session, movie: Movie, data: MovieUpdate) -> Movie:
    return update_record(session, movie, data.model_dump(exclude_unset=True))


def _list(session: Session, *, offset: int, limit: int) -> list[Movie]:
    statement = (
        select(Movie)
        .order_by(Movie.movie_id)
        .offset(offset)
        .limit(min(limit, 1000))
    )
    return list(session.exec(statement).all())


def _create_missing_many(session: Session, records: list[MovieCreate]) -> list[str]:
    return create_missing_records(session, Movie, records, "movie_id")


### ----------- Individual Functions -----------


def create(
    *,
    movie_id: str,
    title: str,
    genres: str | None = None,
    release_date: date | None = None,
    runtime: int | None = None,
    original_language: str | None = None,
    overview: str | None = None,
    popularity: float | None = None,
    vote_average: float | None = None,
    vote_count: int | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    license_cost: Decimal | None = None,
) -> Movie:
    """Create and commit a catalog movie.

    Args:
        movie_id: Stable title/year identifier used by events and recommendations.
        title: Human-readable movie title.
        genres: Pipe-delimited genres such as ``Drama|Romance``.
        release_date: Calendar release date.
        runtime: Runtime in minutes.
        original_language: Source language code.
        overview: Free-text plot summary.
        popularity: Source popularity score.
        vote_average: Source average vote.
        vote_count: Non-negative number of votes.
        imdb_id: IMDb external identifier.
        tmdb_id: TMDB external identifier.
        license_cost: Non-negative estimated licensing cost.

    Returns:
        The committed catalog record.
    """
    data = MovieCreate(**locals())
    return run(lambda session: _create(session, data))


def get(movie_id: str) -> Movie | None:
    """Return the movie identified by ``movie_id``, or ``None`` when absent."""
    return run(lambda session: _get(session, movie_id))


def update(
    movie_id: str,
    *,
    title: str | UnsetType = UNSET,
    genres: str | None | UnsetType = UNSET,
    release_date: date | None | UnsetType = UNSET,
    runtime: int | None | UnsetType = UNSET,
    original_language: str | None | UnsetType = UNSET,
    overview: str | None | UnsetType = UNSET,
    popularity: float | None | UnsetType = UNSET,
    vote_average: float | None | UnsetType = UNSET,
    vote_count: int | None | UnsetType = UNSET,
    imdb_id: str | None | UnsetType = UNSET,
    tmdb_id: str | None | UnsetType = UNSET,
    license_cost: Decimal | None | UnsetType = UNSET,
) -> Movie:
    """Update supplied movie fields; omit to preserve or pass ``None`` to clear."""
    data = MovieUpdate(
        **supplied(
            title=title,
            genres=genres,
            release_date=release_date,
            runtime=runtime,
            original_language=original_language,
            overview=overview,
            popularity=popularity,
            vote_average=vote_average,
            vote_count=vote_count,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            license_cost=license_cost,
        )
    )

    def operation(session):
        movie = _get(session, movie_id)
        if movie is None:
            raise RecordNotFoundError(f"Movie not found: {movie_id}")
        return _update(session, movie, data)

    return run(operation)


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[Movie]:
    """Return movies using zero-based ``offset`` and a maximum ``limit`` of 1,000."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def create_missing_many(
    records: Iterable[MovieCreate | Mapping[str, object]],
) -> list[str]:
    """Insert absent movies atomically and return IDs left unchanged."""
    validated = [MovieCreate.model_validate(record) for record in records]
    return run(lambda session: _create_missing_many(session, validated))


def get_license_costs(movie_ids: Iterable[str]) -> dict[str, Decimal | None]:
    """Read catalog membership and reference prices in one bounded SELECT.

    Missing keys mean nonexistent catalog movies; None means an existing movie
    with unknown price. This reads movies, never the observed licensing ledger.
    At most 500 distinct IDs are accepted; an empty request performs no query.
    """
    ids = []
    seen = set()
    for movie_id in movie_ids:
        if not isinstance(movie_id, str) or not movie_id.strip() or len(movie_id) > 255:
            raise ValueError("movie IDs must be nonblank strings of at most 255 characters")
        if movie_id not in seen:
            ids.append(movie_id)
            seen.add(movie_id)
        if len(ids) > 500:
            raise ValueError("at most 500 distinct movie IDs may be fetched")
    if not ids:
        return {}
    return run(lambda session: dict(session.exec(
        select(Movie.movie_id, Movie.license_cost).where(Movie.movie_id.in_(ids))
    ).all()))
