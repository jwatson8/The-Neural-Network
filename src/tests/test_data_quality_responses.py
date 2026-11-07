"""Offline tests of the supported API-to-persistence boundary subset."""

import json
import unittest
from datetime import date
from decimal import Decimal

from src.data_quality.boundary import validate_movie_response, validate_user_response
from src.database.schemas import MovieCreate, UserCreate


class ResponseValidationTests(unittest.TestCase):
    def assert_invalid(self, validator, raw: object, location: tuple) -> None:
        result, errors = validator(raw)
        self.assertIsNone(result)
        self.assertTrue(errors)
        self.assertIn(location, [error["loc"] for error in errors])
        for error in errors:
            self.assertEqual(set(error), {"loc", "type", "msg"})
            self.assertTrue(error["type"])
            self.assertTrue(error["msg"])
        json.dumps(errors, allow_nan=False)

    def test_valid_user(self) -> None:
        user, errors = validate_user_response({
            "user_id": "test-user", "age": 25, "occupation": "tester",
            "gender": "test", "self_description_likes": "fiction",
            "self_description_dislikes": "horror",
        })
        self.assertEqual(errors, [])
        self.assertIsInstance(user, UserCreate)
        self.assertEqual(user.user_id, "test-user")
        self.assertEqual(user.age, 25)
        self.assertEqual(user.self_description_likes, "fiction")

    def test_user_wrapper_mapping_and_normalization(self) -> None:
        raw = {"user": {"user_id": " test-user \t", "age": "25"}}
        user, errors = validate_user_response(raw)
        self.assertEqual(errors, [])
        self.assertIsInstance(user, UserCreate)
        self.assertEqual(user.user_id, "test-user")
        self.assertEqual(user.age, 25)
        self.assertEqual(raw["user"]["user_id"], " test-user \t")

    def test_user_requires_identifier(self) -> None:
        self.assert_invalid(validate_user_response, {}, ("user_id",))

    def test_user_invalid_identifiers(self) -> None:
        for identifier in (None, "", " \t", 123, True, [], {}):
            with self.subTest(identifier=identifier):
                self.assert_invalid(validate_user_response, {"user_id": identifier}, ("user_id",))

    def test_user_invalid_age(self) -> None:
        for age in (-1, 1.5, True, "invalid", [], float("nan"), float("inf")):
            with self.subTest(age=age):
                self.assert_invalid(validate_user_response, {"user_id": "test-user", "age": age}, ("age",))

    def test_user_optional_fields(self) -> None:
        for fields in ({}, {"age": None, "occupation": None, "gender": None,
                            "first_seen": None, "self_description_likes": None,
                            "self_description_dislikes": ""}):
            with self.subTest(fields=fields):
                user, errors = validate_user_response({"user_id": "test-user", **fields})
                self.assertEqual(errors, [])
                self.assertIsInstance(user, UserCreate)
                self.assertIsNone(user.age)

    def test_user_invalid_optional_types(self) -> None:
        for field, value in (("occupation", []), ("gender", 12),
                             ("self_description_likes", {}), ("first_seen", "invalid")):
            with self.subTest(field=field):
                self.assert_invalid(validate_user_response, {"user_id": "test-user", field: value}, (field,))

    def test_user_unexpected_fields(self) -> None:
        self.assert_invalid(validate_user_response, {"user_id": "test-user", "unknown": 1}, ("unknown",))
        self.assert_invalid(validate_user_response, {"user": {"user_id": "test-user"}, "metadata": {}}, ("metadata",))

    def test_user_malformed_responses(self) -> None:
        for raw in (None, [], "{}", 1, True):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_user_response, raw, ())
                self.assert_invalid(validate_user_response, {"user": raw}, ("user",))

    def test_user_nested_error_location(self) -> None:
        self.assert_invalid(validate_user_response, {"user": {}}, ("user", "user_id"))

    def test_valid_movie_with_schema_coercions(self) -> None:
        raw = {"movie_id": " test-movie ", "title": "Test Movie", "genres": "Drama|Comedy",
               "release_date": "2020-01-02", "runtime": "100", "popularity": 2.5,
               "vote_average": 7.5, "vote_count": 10, "license_cost": "1.25",
               "imdb_id": "test-imdb", "tmdb_id": "test-tmdb"}
        movie, errors = validate_movie_response(raw)
        self.assertEqual(errors, [])
        self.assertIsInstance(movie, MovieCreate)
        self.assertEqual(movie.movie_id, "test-movie")
        self.assertEqual(movie.title, "Test Movie")
        self.assertEqual(movie.genres, "Drama|Comedy")
        self.assertEqual(movie.runtime, 100)
        self.assertEqual(movie.release_date, date(2020, 1, 2))
        self.assertEqual(movie.license_cost, Decimal("1.25"))
        self.assertEqual(raw["movie_id"], " test-movie ")

    def test_movie_required_fields(self) -> None:
        for field in ("movie_id", "title"):
            raw = {"movie_id": "test-movie", "title": "Test"}
            del raw[field]
            self.assert_invalid(validate_movie_response, raw, (field,))

    def test_movie_invalid_identifiers(self) -> None:
        for identifier in (None, "", " \t", 123, True, [], {}):
            with self.subTest(identifier=identifier):
                self.assert_invalid(validate_movie_response, {"movie_id": identifier, "title": "Test"}, ("movie_id",))

    def test_movie_invalid_title(self) -> None:
        for title in (None, "", 123, [], {}):
            with self.subTest(title=title):
                self.assert_invalid(validate_movie_response, {"movie_id": "test-movie", "title": title}, ("title",))

    def test_movie_numeric_constraints(self) -> None:
        for field in ("runtime", "vote_count", "license_cost"):
            with self.subTest(field=field):
                self.assert_invalid(validate_movie_response, {"movie_id": "test-movie", "title": "Test", field: -1}, (field,))

    def test_movie_rejects_empty_and_whitespace_only_titles(self) -> None:
        for title in ("", " ", "\t\n\r", "\u2003\u00a0"):
            with self.subTest(title=title):
                raw = {"movie_id": "test-movie", "title": title}
                self.assert_invalid(validate_movie_response, raw, ("title",))
                self.assertEqual(raw["title"], title)

    def test_movie_preserves_nonblank_title_whitespace(self) -> None:
        title = " \tTest Movie\n "
        movie, errors = validate_movie_response(
            {"movie_id": "test-movie", "title": title}
        )
        self.assertEqual(errors, [])
        self.assertIsInstance(movie, MovieCreate)
        self.assertEqual(movie.title, title)

    def test_preference_only_user_requires_verified_identifier(self) -> None:
        for preferences in (
            {"self_description_likes": "fiction"},
            {"self_description_dislikes": None},
        ):
            with self.subTest(preferences=preferences):
                self.assert_invalid(validate_user_response, preferences, ("user_id",))
                self.assert_invalid(
                    validate_user_response, {"user": preferences}, ("user", "user_id")
                )

    def test_configured_preference_names_require_explicit_mapping(self) -> None:
        raw = {"user_id": "test-user", "likes": "fiction", "dislikes": "horror"}
        self.assert_invalid(validate_user_response, raw, ("likes",))
        self.assert_invalid(
            validate_user_response, {"user": raw}, ("user", "dislikes")
        )

    def test_caller_maps_preference_only_response_with_request_identity(self) -> None:
        # Mimic the integration owner's explicit mapping, without importing the
        # worker or making a request. These names represent deployment config.
        likes_field, dislikes_field = "likes", "dislikes"
        request_user_id = "test-user"
        for preferences in ({likes_field: "fiction"}, {dislikes_field: None}):
            for wrapped in (False, True):
                with self.subTest(preferences=preferences, wrapped=wrapped):
                    raw = {"user": preferences} if wrapped else preferences
                    source = raw["user"] if wrapped else raw
                    mapped = {
                        "user_id": request_user_id,
                        "self_description_likes": source.get(likes_field),
                        "self_description_dislikes": source.get(dislikes_field),
                    }
                    user, errors = validate_user_response(mapped)
                    self.assertEqual(errors, [])
                    self.assertIsInstance(user, UserCreate)
                    self.assertEqual(user.user_id, request_user_id)
                    self.assertEqual(user.self_description_likes, source.get(likes_field))
                    self.assertEqual(user.self_description_dislikes, source.get(dislikes_field))
                    self.assertNotIn("user_id", source)

    def test_movie_invalid_numeric_types_and_nonfinite_values(self) -> None:
        for field in ("runtime", "vote_count", "license_cost", "popularity", "vote_average"):
            for value in (True, "invalid", [], float("nan"), float("inf"), "Infinity"):
                with self.subTest(field=field, value=value):
                    self.assert_invalid(validate_movie_response, {"movie_id": "test-movie", "title": "Test", field: value}, (field,))

    def test_movie_optional_fields(self) -> None:
        for fields in ({}, {field: None for field in MovieCreate.model_fields
                            if field not in ("movie_id", "title")}):
            with self.subTest(fields=fields):
                movie, errors = validate_movie_response({"movie_id": "test-movie", "title": "Test", **fields})
                self.assertEqual(errors, [])
                self.assertIsInstance(movie, MovieCreate)
                self.assertIsNone(movie.runtime)
                self.assertIsNone(movie.release_date)

    def test_movie_invalid_optional_types(self) -> None:
        for field, value in (("genres", ["Drama"]), ("release_date", "invalid"),
                             ("overview", {}), ("tmdb_id", 123)):
            with self.subTest(field=field):
                self.assert_invalid(validate_movie_response, {"movie_id": "test-movie", "title": "Test", field: value}, (field,))

    def test_movie_unexpected_fields_and_unverified_wrappers(self) -> None:
        self.assert_invalid(validate_movie_response, {"movie_id": "test-movie", "title": "Test", "id": 123}, ("id",))
        self.assert_invalid(validate_movie_response, {"movie": {"movie_id": "test-movie", "title": "Test"}}, ("movie",))

    def test_movie_malformed_responses(self) -> None:
        for raw in (None, [], "{}", 1, True):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_movie_response, raw, ())


if __name__ == "__main__":
    unittest.main()
