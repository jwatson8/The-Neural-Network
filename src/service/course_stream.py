"""Consume course events without writing to Kafka. Save evidence before commits."""
import logging
import os
import re

from kafka import KafkaConsumer
from src.service.store import ProfileStore

logger = logging.getLogger(__name__)


def handle_line(store, topic, partition, offset, line):
    parts = line.strip().split(',', 2)
    if len(parts) != 3:
        return 'ignored'
    _, user_id, event = [part.strip() for part in parts]
    if re.fullmatch(r'GET /create_account/?(?:\s+HTTP/\S+)?', event):
        if not store.get_profile(user_id):
            store.enqueue(user_id)
        return 'account'
    if event.startswith('recommendation request '):
        store.record_course_request(topic, partition, offset, line)
        return 'recommendation'
    return 'ignored'


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
    for key in ('KAFKA_BOOTSTRAP_SERVERS', 'KAFKA_TOPIC', 'KAFKA_GROUP_ID'):
        if not os.getenv(key):
            raise ValueError(f'Set {key} before enabling the course stream')
    store = ProfileStore(os.environ['PROFILE_DB_PATH'])
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
            counts = {'account': 0, 'recommendation': 0, 'ignored': 0}
            for records in batch.values():
                for record in records:
                    kind = handle_line(store, record.topic, record.partition, record.offset,
                                       record.value.decode('utf-8'))
                    counts[kind] += 1
            if batch:
                consumer.commit()
            if counts['account'] or counts['recommendation']:
                logger.info('course_events accounts=%d recommendations=%d',
                            counts['account'], counts['recommendation'])
    finally:
        consumer.close()


if __name__ == '__main__':
    main()
