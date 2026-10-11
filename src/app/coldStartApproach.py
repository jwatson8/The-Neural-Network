import os

import instructor
import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from utils.dataProvider import getDataPath

moviesData = getDataPath("movies.csv.gz")
movies = pd.read_csv(moviesData)
usersData = getDataPath("users.csv.gz")
users = pd.read_csv(usersData)


## load secrets
load_dotenv()
apiKey = os.getenv("OPENAI_API_KEY")
modelName = os.getenv("OPENAI_MODEL")

## Genres are obtained from up-to-date movies list
genres = movies["genres"].dropna().str.split("|").explode().unique()
allowedGenres = set(genres)

## build client and reply model


class GenrePreference(BaseModel):
    liked_genres: list[str] = Field(
        default_factory=list, description="Genres inferred from the users likes"
    )
    disliked_genres: list[str] = Field(
        default_factory=list, description="Genres inferred from the users dislikes"
    )

    @field_validator("liked_genres", "disliked_genres")
    @classmethod
    def validate_genres(cls, values):
        invalidGenres = set(values) - allowedGenres

        if invalidGenres:
            raise ValueError(f"Invalid genres returned: {invalidGenres}")

        return values


client = instructor.from_provider(
    f"openai/{modelName}", async_client=True, api_key=apiKey
)


async def coldStart(userID: int, numberOfMovies) -> pd.DataFrame:
    """
    Desc: Uses an LLM call to evaluate the likes/dislikes of the user. Returns numberOfMovies based on highest rating.
    Args:
        userID: the userID we are making suggestions to
        numberOfMovies: integer, supplies a number of movies
    Returns:
        pd.DataFrames from the movies dataset.
    """
    if userID not in users["user_id"].values:
        raise ValueError(f"Error: {userID} is not valid")

    if numberOfMovies <= 0:
        raise ValueError("Error: numberOfMovies must be a positive integer")

    userProfile = users.loc[users["user_id"] == userID].iloc[0]

    userLikes = userProfile["self_description_likes"]
    userLikes = "" if pd.isna(userLikes) else str(userLikes)
    userDislikes = userProfile["self_description_dislikes"]
    userDislikes = "" if pd.isna(userDislikes) else str(userDislikes)

    ## no point in making an LLM call if theres nothing to tell them. Just grab most popular movies
    if not userLikes and not userDislikes:
        return movies.nlargest(numberOfMovies, "popularity")

    preferences = await client.create(
        response_model=GenrePreference,
        max_retries=3,
        messages=[
            {
                "role": "system",
                "content": (
                    f"You classify a users stated likes and dislikes into genres. The available genres are {genres}. Infer genres based on available evidence. If user responses are blank return nothing in those categories. Don't invent genres outside the available genres."
                ),
            },
            {
                "role": "user",
                "content": (f"Likes: {userLikes}. Dislikes: {userDislikes}"),
            },
        ],
    )
    likedGenres = set(preferences.liked_genres)
    dislikedGenres = set(preferences.disliked_genres)

    ## core idea: if a movie has a LIKED genre, its preferred, if it has mulitple -- even better! Then prefer by popularity
    def scoreMovie(genres, liked, disliked):
        score = 0

        for genre in genres.split("|"):
            if genre in liked:
                score += 1
            if genre in disliked:
                score -= 1

        return score

    rankedMovies = movies.dropna(subset=["genres"]).copy()
    rankedMovies["genre_preference_score"] = rankedMovies["genres"].apply(
        lambda genres: scoreMovie(genres, likedGenres, dislikedGenres)
    )

    rankedMovies = rankedMovies.sort_values(
        by=["genre_preference_score", "popularity"], ascending=[False, False]
    )

    return rankedMovies.head(numberOfMovies)
