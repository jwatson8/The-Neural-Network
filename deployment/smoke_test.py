"""Check the actual HTTP service against the mounted, trusted model bundle."""
import json
import os
import time
import urllib.parse
import urllib.request

import joblib
from src.database.interactions import (
    cold_start_jobs,
    cold_start_profiles,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users as user_records,
)


def main():
    base = os.getenv('TEST_BASE_URL', 'http://127.0.0.1:8082')
    model = joblib.load(os.environ['MODEL_PATH'])
    with open(os.environ['PROFILE_SEED_PATH'], encoding='utf-8') as stream:
        profiles = json.load(stream)['profiles']
    known = [str(uid) for uid, seen in model['seen'].items() if seen][:3]
    cold = [str(uid) for uid in profiles if not model['seen'].get(str(uid))][:2]
    assert len(known) == 3 and len(cold) == 2, 'Need warm users and seeded cold users'
    catalog = {str(movie['movie_id']) for movie in model['movies']}
    unknown = 'docker-smoke-unknown'
    while unknown in model['ui'] or unknown in profiles:
        unknown += '-new'
    with urllib.request.urlopen(base + '/health', timeout=2) as response:
        assert response.status == 200
    assert user_records.get(known[0]) is not None, 'Seed users were not rolled up'
    assert movies.get(str(model['movies'][0]['movie_id'])) is not None, 'Seed movies were not rolled up'
    assert stream_events.search(user_id=known[0], limit=1).records, 'Seed stream events were not rolled up'
    seen_index = next(iter(model['seen'][known[0]]))
    seen_movie = str(model['movies'][seen_index]['movie_id'])
    assert user_movie_history.get(known[0], seen_movie) is not None, 'User/movie history was not rolled up'
    assert cold_start_profiles.get(cold[0]) is not None, 'Seed cold-start profiles were not inserted'
    outputs = []
    for category, users in [('warm', known), ('cached-cold', cold), ('unknown-fallback', [unknown])]:
        for uid in users:
            started = time.perf_counter()
            with urllib.request.urlopen(base + '/recommend/' + urllib.parse.quote(uid, safe=''), timeout=2) as response:
                assert response.status == 200
                assert response.headers.get_content_type() == 'text/plain'
                body = response.read().decode('utf-8')
            elapsed = time.perf_counter() - started
            assert '\n' not in body and '\r' not in body, repr(body)
            ids = body.split(',')
            assert 1 <= len(ids) <= 20 and len(ids) == len(set(ids))
            assert set(ids) <= catalog, 'Unknown movie IDs in response'
            seen_ids = {str(model['movies'][i]['movie_id']) for i in model['seen'].get(uid, set())}
            assert not set(ids) & seen_ids, 'Already-seen movies returned'
            assert elapsed < 0.6, f'{uid}: response took {elapsed:.3f}s'
            outputs.append(body)
            print(f'PASS {category} user={uid} movies={len(ids)} elapsed_ms={elapsed * 1000:.1f}')
    assert recommendation_serves.search(user_id=known[0], limit=1).records, 'Recommendation serves were not audited'
    assert cold_start_jobs.get(unknown) is not None, 'Unknown users were not queued for profile generation'
    assert len(set(outputs[:3])) > 1, 'Warm sample received identical lists'
    print('PASS HTTP smoke checks. This does not test live LLM generation or course traffic.')


if __name__ == '__main__':
    main()
