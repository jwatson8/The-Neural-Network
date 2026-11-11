"""Consume course events without writing to Kafka. Save evidence before commits."""
import logging
import os
import re
from datetime import datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from kafka import KafkaConsumer
from pydantic import ValidationError
from src.data_quality.quality_logging import log_validation_failure
from src.service.store import ProfileStore

logger = logging.getLogger(__name__)


def _event_time(value):
    parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo(os.getenv('COURSE_LOG_TIMEZONE', 'UTC'))
        )
    return parsed


WATCH_RE = re.compile(r'GET /data/m/(?P<movie>[^/\s]+)/(?P<minute>\d+)\.mpg(?:\s+HTTP/\S+)?')
RATE_RE = re.compile(r'GET /rate/(?P<movie>[^=\s]+)=(?P<rating>\d+)(?:\s+HTTP/\S+)?')


def _invalid(field, msg):
    log_validation_failure(
        'kafka', [{'loc': (field,), 'type': 'value_error', 'msg': msg}]
    )
    return 'invalid'


def handle_line(store, topic, partition, offset, line):
    basic_parts = line.strip().split(',', 2)
    if len(basic_parts) != 3:
        return 'ignored'
    timestamp, user_id, raw_event = [part.strip() for part in basic_parts]
    event = raw_event.split(',', 1)[0].strip()
    watch = WATCH_RE.fullmatch(event)
    rate = RATE_RE.fullmatch(event)
    if watch or rate:
        try:
            event_time = _event_time(timestamp)
        except ValueError:
            return _invalid('event_time', 'Invalid event time')
        match = watch or rate
        movie_event = {
            'user_id': user_id,
            'event_type': 'watch' if watch else 'rating',
            'event_time': event_time,
            'movie_id': unquote(match.group('movie')),
        }
        if watch:
            movie_event['minute_index'] = int(watch.group('minute'))
        elif rate:
            movie_event['rating'] = float(rate.group('rating'))
        inserted, errors = store.record_movie_event(
            movie_event, topic, partition, offset
        )
        return 'invalid' if inserted is None else movie_event['event_type']
    if re.fullmatch(r'GET /create_account/?(?:\s+HTTP/\S+)?', event):
        try:
            event_time = _event_time(timestamp)
        except ValueError:
            log_validation_failure(
                'kafka',
                [{'loc': ('event_time',), 'type': 'value_error', 'msg': 'Invalid event time'}],
            )
            return 'invalid'
        inserted, errors = store.record_account_created(
            user_id, event_time, topic, partition, offset
        )
        if inserted is None:
            return 'invalid'
        if not store.get_profile(user_id):
            store.enqueue(user_id)
        return 'account'
    if event.startswith('recommendation request '):
        try:
            store.record_course_request(topic, partition, offset, line)
        except ValidationError as error:
            log_validation_failure(
                'kafka',
                error.errors(
                    include_url=False, include_context=False, include_input=False
                ),
            )
            return 'invalid'
        return 'recommendation'
    return 'ignored'


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
    for key in ('KAFKA_BOOTSTRAP_SERVERS', 'KAFKA_TOPIC', 'KAFKA_GROUP_ID'):
        if not os.getenv(key):
            raise ValueError(f'Set {key} before enabling the course stream')
    store = ProfileStore()
    options = dict(
        bootstrap_servers=os.environ['KAFKA_BOOTSTRAP_SERVERS'].split(','),
        group_id=os.environ['KAFKA_GROUP_ID'],
        enable_auto_commit=False,
        auto_offset_reset='latest',
        security_protocol=os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT'),
        max_poll_records=200,
    )
    for env, name in [
        ('KAFKA_SASL_MECHANISM', 'sasl_mechanism'),
        ('KAFKA_SASL_USERNAME', 'sasl_plain_username'),
        ('KAFKA_SASL_PASSWORD', 'sasl_plain_password'),
        ('KAFKA_SSL_CAFILE', 'ssl_cafile'),
    ]:
        if os.getenv(env):
            options[name] = os.environ[env]
    consumer = KafkaConsumer(os.environ['KAFKA_TOPIC'], **options)
    logger.info('course_stream_started topic=%s new_group_starts_at=latest', os.environ['KAFKA_TOPIC'])
    try:
        while True:
            batch = consumer.poll(timeout_ms=1000)
            counts = {
                'account': 0,
                'recommendation': 0,
                'watch': 0,
                'rating': 0,
                'invalid': 0,
                'ignored': 0,
            }
            for records in batch.values():
                for record in records:
                    kind = handle_line(store, record.topic, record.partition, record.offset,
                                       record.value.decode('utf-8'))
                    counts[kind] += 1
            if batch:
                consumer.commit()
            logged_kinds = (
                'account', 'recommendation', 'watch', 'rating', 'invalid'
            )
            if any(counts[kind] for kind in logged_kinds):
                logger.info(
                    'course_events accounts=%d recommendations=%d '
                    'watches=%d ratings=%d invalid=%d',
                    counts['account'], counts['recommendation'], counts['watch'],
                    counts['rating'], counts['invalid'],
                )
    finally:
        consumer.close()


if __name__ == '__main__':
    main()
