"""SQLite-backed cold-start profiles and deduplicated work queue."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class ProfileStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=0.1)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=100')
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA busy_timeout=5000')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    positive TEXT NOT NULL,
                    negative TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profile_jobs (
                    user_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
            ''')

    def healthy(self):
        with self.connect() as connection:
            connection.execute('SELECT 1 FROM profiles LIMIT 1').fetchone()
        return True

    def get_profile(self, user_id):
        with self.connect() as connection:
            row = connection.execute(
                'SELECT positive, negative FROM profiles WHERE user_id = ?',
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_profile(self, user_id, positive, negative):
        with self.connect() as connection:
            connection.execute('''
                INSERT INTO profiles (user_id, positive, negative, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    positive = excluded.positive,
                    negative = excluded.negative,
                    updated_at = excluded.updated_at
            ''', (user_id, positive, negative, time.time()))
            connection.execute('DELETE FROM profile_jobs WHERE user_id = ?', (user_id,))

    def seed_profiles(self, path):
        seed_path = Path(path)
        if not seed_path.is_file():
            return
        profiles = json.loads(seed_path.read_text(encoding='utf-8')).get('profiles', {})
        now = time.time()
        with self.connect() as connection:
            connection.executemany('''
                INSERT OR IGNORE INTO profiles (user_id, positive, negative, updated_at)
                VALUES (?, ?, ?, ?)
            ''', [
                (str(user_id), profile['positive'], profile['negative'], now)
                for user_id, profile in profiles.items()
                if isinstance(profile.get('positive'), str)
                and isinstance(profile.get('negative'), str)
            ])

    def enqueue(self, user_id):
        now = time.time()
        with self.connect() as connection:
            connection.execute('''
                INSERT OR IGNORE INTO profile_jobs (user_id, next_attempt_at, created_at)
                VALUES (?, ?, ?)
            ''', (user_id, now, now))

    def claim_next(self, lease_seconds=60):
        now = time.time()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''
                SELECT user_id, attempts FROM profile_jobs
                WHERE next_attempt_at <= ? AND lease_until <= ?
                ORDER BY created_at LIMIT 1
            ''', (now, now)).fetchone()
            if row is None:
                return None
            connection.execute('''
                UPDATE profile_jobs
                SET attempts = attempts + 1, lease_until = ?
                WHERE user_id = ?
            ''', (now + lease_seconds, row['user_id']))
            return {'user_id': row['user_id'], 'attempts': row['attempts'] + 1}

    def retry_later(self, user_id, attempts, max_delay=900):
        delay = min(2 ** min(attempts, 10), max_delay)
        with self.connect() as connection:
            connection.execute('''
                UPDATE profile_jobs
                SET next_attempt_at = ?, lease_until = 0
                WHERE user_id = ?
            ''', (time.time() + delay, user_id))