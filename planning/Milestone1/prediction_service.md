
# Prediction Service
The Flask API loads the selected Jace model bundle at startup (by default, `src/app/fullTrainedModel/jace_model.joblib`; deployments can override this with `MODEL_PATH`). `GET /recommend/<user_id>` returns up to 20 movie IDs as one comma-separated `text/plain` response. If the model is unavailable, the API returns HTTP 503.

The model is loaded once when the service starts so requests can use it without re-reading the artifact. The `/health` endpoint reports whether the model and profile store are ready. The recommendation route records the selected route, model version, response time, and status in its log output.

For a user with interaction history, the serving adapter scores every movie by the dot product of that user's learned SVD factors and the movie factors. It removes movies already seen by the user, orders the remaining scores from high to low, and returns the first 20 distinct IDs. This is a ranking score, not a predicted star rating. The factors are learned by 32-component TruncatedSVD over the user/movie interaction matrix; explicit ratings overwrite watch-only values.

The SVD model represents users and movies as vectors in a shared latent space. A larger dot product indicates a stronger match according to the patterns in the training interactions. The service sorts these scores to produce an ordered list; it does not convert them into calibrated ratings or a probability that a user will like a movie.

Filtering seen movies prevents the endpoint from returning items the user has already watched or rated. The final list is capped at 20 IDs, which keeps the response compact for the consuming application.

For a new user without interaction history, the cold-start process (`src/service/cold_start.py`) asks an LLM to create a personalized SVD "fingerprint" from the user's profile and stated preferences, such as genres and types of movies they enjoy or avoid. The fingerprint is intended to represent those preferences in the same latent space as the model's movie factors. The service can then score candidate movies by comparing the new user's fingerprint with each movie factor, while filtering out any movies they have already seen. This makes it possible to personalize recommendations before the user has built up an interaction history. The fingerprint is generated asynchronously. In the meantime, the user receives highly-ranked and popular movies in the interim.

The selected model's learned SVD parameters were not changed for this service. The planned extension retains the trained movie factors and uses each new user's profile and preferences to create a compatible user-specific fingerprint. The existing dot-product ranking can then be reused for that user. The fingerprint generation and storage steps are service additions; they do not require retraining the shared model for every new account.
