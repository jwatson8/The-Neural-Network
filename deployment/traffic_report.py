"""Count a precise course-log window; correlate successful personalized responses."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

PATTERN = re.compile(r'^([^,]+),([^,]+),recommendation request (.*?),\s*status\s+(\d+),\s*result:\s*(.*),\s*([^,]+)\s*$')


def timestamp(value, zone):
    parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
    return parsed.timestamp()


def summarize(connection, server, start, end, zone):
    counts = Counter()
    used_audits = set()
    signatures = set()
    rows = connection.execute('SELECT raw_line FROM course_requests ORDER BY received_at, partition_id, offset_id')
    for (line,) in rows:
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
        audit = connection.execute('''
            SELECT id, route FROM response_audit
            WHERE user_id=? AND result_hash=? AND served_at BETWEEN ? AND ?
            ORDER BY ABS(served_at-?)
        ''', (user_id.strip(), digest, when - 120, when + 120, when)).fetchall()
        for audit_id, route in audit:
            if audit_id in used_audits:
                continue
            used_audits.add(audit_id)
            counts['matched_api_responses'] += 1
            if route in ('collaborative_svd', 'llm_content'):
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
    connection = sqlite3.connect(os.environ['PROFILE_DB_PATH'], timeout=5)
    if args.list_servers:
        counts = Counter()
        for (line,) in connection.execute('SELECT raw_line FROM course_requests'):
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
    result = summarize(connection, args.server, start, end, zone)
    result.update(server=args.server, naive_log_timezone=zone,
                  window_start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                  window_end_utc=datetime.fromtimestamp(end, timezone.utc).isoformat(),
                  note='Captured data only; verify complete window coverage and consumer lag. Personalization is matched by user, exact list and timestamp within 120s, one API record per course event; unmatched events are not counted as personalized. Course logs are authoritative.')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
