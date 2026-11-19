"""Generate and persist cold-start profiles outside the HTTP request path."""

import json
import logging
import os
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from openai import OpenAI
from pydantic import BaseModel

from src.database.interactions import llm_usage
from src.service.store import ProfileStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

INSTRUCTIONS = """You are a movie preference extractor. Extract search phrases from user preferences.

IMPORTANT: Return ONLY a JSON object with exactly these fields:
- "positive": search phrases for movies they like
- "negative": search phrases for movies they dislike

Example output:
{"positive": "sci-fi dystopian", "negative": "horror violence"}

Do not explain. Do not add other fields. Return only the JSON."""


class PreferenceProfile(BaseModel):
    positive: str
    negative: str


class UserNotFoundError(LookupError):
    """The User API returned no record for the requested user."""


def fetch_descriptions(user_id):
    template = os.environ["USER_LOOKUP_URL_TEMPLATE"]
    url = template.format(user_id=quote(user_id, safe=""))
    headers = {"Accept": "application/json"}
    bearer_token = os.getenv("USER_API_BEARER_TOKEN")
    if bearer_token:
        headers["Authorization"] = "Bearer " + bearer_token
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            raise UserNotFoundError from None
        if error.code == 400:
            try:
                error_payload = json.loads(error.read(4096))
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_payload = None
            if error_payload == {"message": "user not found"}:
                raise UserNotFoundError from None
        raise
    except json.JSONDecodeError as error:
        if not error.doc.strip():
            raise UserNotFoundError from None
        raise
    if payload is None or payload == {} or payload == {"user": None}:
        raise UserNotFoundError
    if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
        payload = payload["user"]
    likes = payload.get("self_description_likes") or ""
    dislikes = payload.get("self_description_dislikes") or ""
    if not isinstance(likes, str) or not isinstance(dislikes, str):
        raise ValueError("User lookup descriptions must be strings")
    return likes, dislikes


def generate_profile(client, model_name, likes, dislikes):
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": INSTRUCTIONS},
            {
                "role": "user",
                "content": f"User likes: {likes}\nUser dislikes: {dislikes}\n\nExtract to JSON format with 'positive' and 'negative' fields only.",
            },
        ],
        max_tokens=3000,
        temperature=0,
    )
    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError(
            f"Model did not return a complete preference profile (finish_reason={choice.finish_reason})"
        )
    profile = PreferenceProfile.model_validate_json(choice.message.content)
    return profile, response.usage


def process_one(store, client, model_name):
    job = store.claim_next()
    if job is None:
        return False
    user_id = job["user_id"]
    llm_called = False
    llm_succeeded = False
    try:
        likes, dislikes = fetch_descriptions(user_id)
        llm_request_id = None
        if likes.strip() or dislikes.strip():
            llm_called = True
            profile, usage = generate_profile(client, model_name, likes, dislikes)
            llm_succeeded = True
            input_tokens = getattr(usage, 'input_tokens', None) or getattr(usage, 'prompt_tokens', 0)
            output_tokens = getattr(usage, 'output_tokens', None) or getattr(usage, 'completion_tokens', 0)
            estimated_cost_usd = None
            if input_tokens is not None and output_tokens is not None:
                estimated_cost_usd = (
                    Decimal(input_tokens) * Decimal("0.18")
                    + Decimal(output_tokens) * Decimal("0.18")
                ) / Decimal("1000000")
            usage_record = llm_usage.create(
                user_id=user_id,
                provider="deepseek",
                model_name=model_name,
                status="succeeded",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost_usd,
            )
            llm_request_id = usage_record.request_id
        else:
            profile = PreferenceProfile(positive="", negative="")
        store.save_profile(
            user_id, profile.positive, profile.negative, llm_request_id=llm_request_id
        )
        logger.info("profile_created user_id=%s attempts=%d", user_id, job["attempts"])
    except UserNotFoundError:
        try:
            store.cancel_job(user_id)
        except Exception as error:
            store.retry_later(user_id, job["attempts"], type(error).__name__)
            logger.warning(
                "profile_cancel_retry user_id=%s attempts=%d error=%s",
                user_id,
                job["attempts"],
                type(error).__name__,
            )
        else:
            logger.info("profile_cancelled user_id=%s reason=user_not_found", user_id)
    except Exception as error:
        if llm_called and not llm_succeeded:
            try:
                llm_usage.create(
                    user_id=user_id,
                    provider="openai",
                    model_name=model_name,
                    status="failed",
                )
            except Exception as audit_error:
                logger.warning(
                    "llm_usage_audit_failed user_id=%s error_type=%s",
                    user_id,
                    type(audit_error).__name__,
                )
        store.retry_later(user_id, job["attempts"], type(error).__name__)
        logger.warning(
            "profile_retry user_id=%s attempts=%d error=%s",
            user_id,
            job["attempts"],
            type(error).__name__,
        )
    return True


def main():
    if not os.getenv("USER_LOOKUP_URL_TEMPLATE"):
        raise ValueError("Set USER_LOOKUP_URL_TEMPLATE before enabling live profiling")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise ValueError("Set DEEPSEEK_API_KEY before enabling live profiling")
    
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-r1:1.5b")
    base_url = "https://spark-60c4.tailcead09.ts.net:10000/v1"
    
    store = ProfileStore()
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=300,
        max_retries=0,
    )
    logger.info("profile_worker_started model=%s", model_name)
    while True:
        if not process_one(store, client, model_name):
            time.sleep(0.5)


if __name__ == "__main__":
    main()
