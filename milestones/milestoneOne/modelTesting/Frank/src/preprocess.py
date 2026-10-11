import pandas as pd

EVENTS_PATH = "data/events.csv.gz"
USERS_PATH = "data/users.csv.gz"
MOVIES_PATH = "data/movies.csv.gz"


def load_data():
    events = pd.read_csv(EVENTS_PATH)
    users = pd.read_csv(USERS_PATH)
    movies = pd.read_csv(MOVIES_PATH)

    return events, users, movies


def build_ratings(events):
    ratings = events[events["event_type"] == "rating"][
        ["user_id", "movie_id", "rating"]
    ].copy()

    ratings = ratings.dropna(subset=["user_id", "movie_id", "rating"])

    return ratings


def build_watch_history(events):
    watch_events = events[events["event_type"] == "watch"][
        ["user_id", "movie_id"]
    ].copy()

    watched_by_user = watch_events.groupby("user_id")["movie_id"].apply(set).to_dict()

    return watched_by_user


def build_user_item_matrix(ratings):
    user_item_matrix = ratings.pivot_table(
        index="user_id", columns="movie_id", values="rating", aggfunc="mean"
    )

    return user_item_matrix


def prepare_movie_metadata(movies):
    movie_metadata = movies[
        [
            "movie_id",
            "title",
            "genres",
            "overview",
            "popularity",
            "vote_average",
            "vote_count",
            "license_cost",
        ]
    ].copy()

    return movie_metadata


def main():
    events, users, movies = load_data()

    ratings = build_ratings(events)
    watched_by_user = build_watch_history(events)
    user_item_matrix = build_user_item_matrix(ratings)
    movie_metadata = prepare_movie_metadata(movies)

    print("Ratings shape:")
    print(ratings.shape)

    print("\nRatings sample:")
    print(ratings.head())

    print("\nNumber of users with watch history:")
    print(len(watched_by_user))

    print("\nUser-item matrix shape:")
    print(user_item_matrix.shape)

    print("\nMovie metadata shape:")
    print(movie_metadata.shape)

    print("\nExample watched movies for user 1:")
    print(watched_by_user.get(1))

    print("\nExample user ratings:")
    print(user_item_matrix.loc[1].dropna().sort_values(ascending=False).head(10))

    watch_pairs = set(
        events.loc[events["event_type"] == "watch", ["user_id", "movie_id"]].itertuples(
            index=False, name=None
        )
    )

    rating_pairs = set(
        events.loc[
            events["event_type"] == "rating", ["user_id", "movie_id"]
        ].itertuples(index=False, name=None)
    )

    print("\nWatch pairs without ratings:")
    print(len(watch_pairs - rating_pairs))

    print("\nRating pairs without watches:")
    print(len(rating_pairs - watch_pairs))

    duplicate_ratings = ratings.duplicated(subset=["user_id", "movie_id"]).sum()

    print("\nDuplicate user-movie rating rows:")
    print(duplicate_ratings)

    print("\nUnique users in ratings:")
    print(ratings["user_id"].nunique())

    print("\nUnique movies in ratings:")
    print(ratings["movie_id"].nunique())


if __name__ == "__main__":
    main()
