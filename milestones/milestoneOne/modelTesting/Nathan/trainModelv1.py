import argparse
from pathlib import Path

import numpy as np
import tensorflow as tf

from utils.dataProvider import DATA_PATH, PreparedModelData, loadPreparedData

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "nathan_model.keras"


def train_model(
    data: PreparedModelData,
    model_path: Path | str = DEFAULT_MODEL_PATH,
    epochs: int = 10,
) -> dict:
    """Train Nathan's original embedding model on the shared training ratings."""
    training_ratings = data.train_events.loc[
        data.train_events["event_type"] == "rating", ["user_id", "movie_id", "rating"]
    ].copy()
    training_ratings["user_id"] = training_ratings["user_id"].astype(str)
    training_ratings["movie_id"] = training_ratings["movie_id"].astype(str)
    training_ratings["rating"] = training_ratings["rating"].astype("float32")

    unique_users = training_ratings["user_id"].unique()
    unique_movies = training_ratings["movie_id"].unique()

    user_lookup = tf.keras.layers.StringLookup(vocabulary=unique_users, mask_token=None)
    movie_lookup = tf.keras.layers.StringLookup(vocabulary=unique_movies, mask_token=None)

    embedding_size = 32
    user_embeddings = tf.keras.layers.Embedding(
        input_dim=len(user_lookup.get_vocabulary()), output_dim=embedding_size
    )
    movie_embeddings = tf.keras.layers.Embedding(
        input_dim=len(movie_lookup.get_vocabulary()), output_dim=embedding_size
    )

    user_input = tf.keras.Input(shape=(), dtype=tf.string, name="user_id")
    movie_input = tf.keras.Input(shape=(), dtype=tf.string, name="movie_id")
    user_index = user_lookup(user_input)
    movie_index = movie_lookup(movie_input)
    user_vector = user_embeddings(user_index)
    movie_vector = movie_embeddings(movie_index)
    interaction = tf.keras.layers.Dot(axes=1, name="interaction")(
        [user_vector, movie_vector]
    )

    user_bias_layer = tf.keras.layers.Embedding(
        input_dim=len(user_lookup.get_vocabulary()), output_dim=1, name="user_bias"
    )
    movie_bias_layer = tf.keras.layers.Embedding(
        input_dim=len(movie_lookup.get_vocabulary()), output_dim=1, name="movie_bias"
    )
    combined = tf.keras.layers.Add()(
        [interaction, user_bias_layer(user_index), movie_bias_layer(movie_index)]
    )
    prediction = tf.keras.layers.Rescaling(
        scale=1.0,
        offset=float(training_ratings["rating"].mean()),
        name="predicted_rating",
    )(combined)

    model = tf.keras.Model(
        inputs={"user_id": user_input, "movie_id": movie_input},
        outputs=prediction,
        name="collaborative_filter_v1",
    )

    features = {
        "user_id": tf.constant(training_ratings["user_id"].tolist()),
        "movie_id": tf.constant(training_ratings["movie_id"].tolist()),
    }
    labels = tf.constant(training_ratings["rating"].to_numpy().reshape(-1, 1))
    train_dataset = tf.data.Dataset.from_tensor_slices((features, labels))
    train_dataset = train_dataset.shuffle(
        len(training_ratings), seed=12345
    ).batch(128)

    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss=tf.keras.losses.MeanSquaredError(),
        metrics=[tf.keras.metrics.RootMeanSquaredError(name="rmse")],
    )
    history = model.fit(train_dataset, epochs=epochs)

    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save(model_path)
    return {
        "training_ratings": len(training_ratings),
        "epochs": epochs,
        "final_loss": float(history.history["loss"][-1]),
        "final_rmse": float(history.history["rmse"][-1]),
    }


def load_model(model_path: Path | str = DEFAULT_MODEL_PATH):
    return tf.keras.models.load_model(Path(model_path))


def recommend(
    model,
    user_id,
    movie_ids,
    seen_movie_ids=(),
    k: int = 20,
):
    """Score a candidate catalog and return Nathan's top recommendations."""
    if k < 1:
        raise ValueError("k must be positive")

    candidates = [str(movie_id) for movie_id in movie_ids]
    if not candidates:
        return []

    predictions = model(
        {
            "user_id": tf.constant([str(user_id)] * len(candidates)),
            "movie_id": tf.constant(candidates),
        },
        training=False,
    )
    scores = np.asarray(predictions.numpy()).reshape(-1)
    if len(scores) != len(candidates):
        raise ValueError("Nathan model returned a score count that does not match candidates")

    seen = {str(movie_id) for movie_id in seen_movie_ids}
    ranked = np.argsort(-scores, kind="stable")
    return [
        {"movie_id": candidates[index], "score": float(scores[index])}
        for index in ranked
        if candidates[index] not in seen and np.isfinite(scores[index])
    ][:k]


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Nathan's recommendation model")
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--epochs", type=int, default=10)
    args = parser.parse_args()
    stats = train_model(
        loadPreparedData(args.data_dir), args.model_out, epochs=args.epochs
    )
    load_model(args.model_out)
    print(stats)


if __name__ == "__main__":
    main()
