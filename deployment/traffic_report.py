"""Count a precise course-log window; correlate successful personalized responses."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from zoneinfo import ZoneInfo

from src.database.interactions import course_requests, recommendation_serves

PATTERN = re.compile(r'^([^,]+),([^,]+),recommendation request (.*?),\s*status\s+(\d+),\s*result:\s*(.*),\s*([^,]+)\s*$')


def timestamp(value, zone):
    parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
    return parsed.timestamp()


def _epoch(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def summarize(lines, audits, server, start, end, zone):
    counts = Counter()
    used_audits = set()
    signatures = set()
    audit_index = {}
    for audit in audits:
        canonical = ','.join(
            str(item['movie_id'] if isinstance(item, dict) else item.movie_id)
            for item in audit.recommendations
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        audit_index.setdefault((audit.user_id, digest), []).append(audit)
    for candidates in audit_index.values():
        candidates.sort(key=lambda audit: _epoch(audit.served_at))

    for line in lines:
        prefix = line.strip().split(',', 2)
        if len(prefix) != 3:
            counts['unparsed_records'] += 1
            continue
        target = prefix[2].split(',', 1)[0].removeprefix('recommendation request ').strip()
        if target != server:
            continue
        try:
            when = timestamp(prefix[0], zone)
        except ValueError:
            counts['target_records_with_invalid_time'] += 1
            continue
        if not start <= when < end:
            continue
        counts['all_requests'] += 1
        match = PATTERN.match(line.strip())
        if match is None:
            counts['unparsed_target_requests'] += 1
            continue
        _, user_id, _, status, body, _ = match.groups()
        if status != '200':
            continue
        counts['successful_status_200'] += 1
        # Normalize optional spaces in course log's comma-separated list.
        canonical = ','.join(item.strip() for item in body.split(','))
        signatures.add(canonical)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        candidates = audit_index.get((user_id.strip(), digest), [])
        for audit in sorted(candidates, key=lambda item: abs(_epoch(item.served_at) - when)):
            if audit.request_id in used_audits:
                continue
            if abs(_epoch(audit.served_at) - when) > 120:
                continue
            used_audits.add(audit.request_id)
            counts['matched_api_responses'] += 1
            if audit.model_route in ('collaborative_svd', 'llm_content'):
                counts['matched_personalized_successes'] += 1
            break
    total = counts['all_requests']
    result = {key: counts[key] for key in (
        'all_requests', 'successful_status_200', 'matched_api_responses',
        'matched_personalized_successes', 'unparsed_target_requests',
        'target_records_with_invalid_time', 'unparsed_records')}
    result['distinct_successful_lists'] = len(signatures)
    result['matched_personalized_fraction_of_ALL_requests'] = (
        counts['matched_personalized_successes'] / total if total else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default=os.getenv('COURSE_SERVER_LABEL'))
    parser.add_argument('--start', help='Window start with explicit timezone, e.g. 2026-10-12T03:59:00Z')
    parser.add_argument('--end', help='Window end; defaults to 24 hours after --start')
    parser.add_argument('--list-servers', action='store_true')
    args = parser.parse_args()
    records = []
    offset = 0
    while True:
        page = course_requests.list_records(offset=offset, limit=1000)
        records.extend(page)
        if len(page) < 1000:
            break
        offset += len(page)
    lines = [record.raw_line for record in records]
    if args.list_servers:
        counts = Counter()
        for line in lines:
            parts = line.strip().split(',', 2)
            if len(parts) == 3:
                counts[parts[2].split(',', 1)[0].removeprefix('recommendation request ').strip()] += 1
        print(json.dumps(counts, indent=2))
        return
    if not args.server:
        parser.error('Use --list-servers, then supply the exact team server label with --server')
    now = datetime.now(timezone.utc)
    if args.start:
        dt = datetime.fromisoformat(args.start.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            parser.error('--start must include timezone')
        start = dt.timestamp()
        end = start + 86400
    else:
        start, end = (now - timedelta(hours=24)).timestamp(), now.timestamp()
    if args.end:
        dt = datetime.fromisoformat(args.end.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            parser.error('--end must include timezone')
        end = dt.timestamp()
    if end <= start or end - start > 86400:
        parser.error('Choose a positive window of at most 24 hours')
    zone = os.getenv('COURSE_LOG_TIMEZONE', 'UTC')
    audit_start = datetime.fromtimestamp(start - 120, timezone.utc)
    audit_end = datetime.fromtimestamp(end + 120, timezone.utc)
    audits = []
    cursor = None
    while True:
        page = recommendation_serves.search(
            start=audit_start,
            end=audit_end,
            cursor=cursor,
            limit=1000,
        )
        audits.extend(page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    result = summarize(lines, audits, args.server, start, end, zone)
    result.update(server=args.server, naive_log_timezone=zone,
                  window_start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                  window_end_utc=datetime.fromtimestamp(end, timezone.utc).isoformat(),
                  note='Captured data only; verify complete window coverage and consumer lag. Personalization is matched by user, exact list and timestamp within 120s, one API record per course event; unmatched events are not counted as personalized. Course logs are authoritative.')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
