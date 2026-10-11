import pandas as pd

from coldStart import coldStart
from modelPrediction import modelPredict
from utils.checkIfNewUser import checkIfNewUser


async def reccomendMetaFunction(userID: int, numberOfMovies: int) -> pd.DataFrame:
    """
    This function is the top-level decider for the reccomendation function. If userID is new, it will run the program provided by coldStart . if userID already exists, it will find new movies for them to watch scored on the model provided to modelPrediction.
    Args:
        userID: the userID we are making suggestions to
        numberOfMovies: integer, supplies a number of movies
    Returns:
        pd.DataFrames from the movies dataset.
    """
    newUser = checkIfNewUser(userID)

    if newUser:
        return await coldStart(userID, numberOfMovies)
    else:
        return modelPredict(userID, numberOfMovies)
