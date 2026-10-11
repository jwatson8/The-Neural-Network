import pandas as pd

from utils.dataProvider import getDataPath


def checkIfNewUser(userID: int) -> bool:
    """
    Determines of the userID has watched a film yet on the service.
    Args:
        userId: takes in a userID from users.csv
    Returns:
        True: if user has no watch or rating history
        False: if user has watch or rating history
    """
    eventsData = getDataPath("events.csv.gz")
    events = pd.read_csv(eventsData)
    userEvents = events.loc[events["user_id"] == userID]

    ## if not found in dataset then doesn't exist
    if len(userEvents) == 0:
        raise ValueError(f"Invalid: {userID} does not exist")

    hasInteractions = userEvents["event_type"].isin(["watch", "rating"]).any()
    return not hasInteractions
