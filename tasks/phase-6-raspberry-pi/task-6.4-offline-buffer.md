# Task 6.4: Offline Data Buffer

## Objective

Implement SQLite-based local storage for ECG data when network is unavailable.

## Dependencies

- Task 6.1 (Project Setup) completed

---

## Acceptance Criteria

### DataBuffer Class

- [ ] SQLite database for durability
- [ ] store() method saves batch with metadata
- [ ] get_unsynced() returns oldest unsynced batches
- [ ] mark_synced() marks batches as successfully sent
- [ ] get_pending_count() returns unsynced count
- [ ] cleanup_old() removes old synced data

### Data Integrity

- [ ] Atomic writes
- [ ] No data loss on crash
- [ ] Handles concurrent access

### Storage Format

- [ ] session_id, timestamp, samples (JSON)
- [ ] synced flag, created_at, synced_at

---

## Implementation

```python
# raspberry-pi/src/data_buffer.py
"""Offline data buffer using SQLite."""

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import time

logger = logging.getLogger(__name__)


@dataclass
class BufferedBatch:
    """A buffered data batch."""
    id: int
    session_id: str
    timestamp: int
    samples: list[dict]
    created_at: int


class DataBuffer:
    """SQLite-based offline data buffer."""

    def __init__(self, db_path: str = "buffer.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS buffered_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                samples TEXT NOT NULL,
                synced INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                synced_at INTEGER
            )
        """)

        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_unsynced
            ON buffered_data(synced, created_at)
        """)

        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session
            ON buffered_data(session_id, timestamp)
        """)

        self._conn.commit()
        logger.info(f"Data buffer initialized: {self.db_path}")

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def store(
        self,
        session_id: str,
        timestamp: int,
        samples: list[dict],
    ) -> int:
        """
        Store a data batch.

        Returns:
            Batch ID
        """
        samples_json = json.dumps(samples)
        created_at = int(time.time() * 1000)

        cursor = self._conn.execute(
            """
            INSERT INTO buffered_data
            (session_id, timestamp, samples, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, timestamp, samples_json, created_at),
        )
        self._conn.commit()

        batch_id = cursor.lastrowid
        logger.debug(f"Stored batch {batch_id} for session {session_id}")
        return batch_id

    def get_unsynced(self, limit: int = 100) -> list[BufferedBatch]:
        """
        Get oldest unsynced batches.

        Args:
            limit: Maximum number of batches to return

        Returns:
            List of buffered batches, oldest first
        """
        cursor = self._conn.execute(
            """
            SELECT id, session_id, timestamp, samples, created_at
            FROM buffered_data
            WHERE synced = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        )

        batches = []
        for row in cursor.fetchall():
            batches.append(BufferedBatch(
                id=row["id"],
                session_id=row["session_id"],
                timestamp=row["timestamp"],
                samples=json.loads(row["samples"]),
                created_at=row["created_at"],
            ))

        return batches

    def mark_synced(self, batch_ids: list[int]) -> None:
        """Mark batches as successfully synced."""
        if not batch_ids:
            return

        synced_at = int(time.time() * 1000)
        placeholders = ",".join("?" * len(batch_ids))

        self._conn.execute(
            f"""
            UPDATE buffered_data
            SET synced = 1, synced_at = ?
            WHERE id IN ({placeholders})
            """,
            [synced_at] + batch_ids,
        )
        self._conn.commit()
        logger.debug(f"Marked {len(batch_ids)} batches as synced")

    def get_pending_count(self) -> int:
        """Get count of unsynced batches."""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM buffered_data WHERE synced = 0"
        )
        return cursor.fetchone()[0]

    def get_pending_for_session(self, session_id: str) -> int:
        """Get count of unsynced batches for a session."""
        cursor = self._conn.execute(
            """
            SELECT COUNT(*) FROM buffered_data
            WHERE synced = 0 AND session_id = ?
            """,
            (session_id,),
        )
        return cursor.fetchone()[0]

    def cleanup_old(self, max_age_hours: int = 24) -> int:
        """
        Remove old synced data.

        Args:
            max_age_hours: Maximum age of synced data to keep

        Returns:
            Number of rows deleted
        """
        cutoff = int(time.time() * 1000) - (max_age_hours * 60 * 60 * 1000)

        cursor = self._conn.execute(
            """
            DELETE FROM buffered_data
            WHERE synced = 1 AND synced_at < ?
            """,
            (cutoff,),
        )
        self._conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old synced batches")
        return deleted

    def get_stats(self) -> dict:
        """Get buffer statistics."""
        cursor = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN synced = 0 THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN synced = 1 THEN 1 ELSE 0 END) as synced,
                MIN(created_at) as oldest,
                MAX(created_at) as newest
            FROM buffered_data
        """)
        row = cursor.fetchone()

        return {
            "total": row["total"],
            "pending": row["pending"],
            "synced": row["synced"],
            "oldest": row["oldest"],
            "newest": row["newest"],
        }
```

```python
# raspberry-pi/tests/test_buffer.py
"""Test offline data buffer."""

import os
import pytest
import tempfile

from src.data_buffer import DataBuffer


@pytest.fixture
def buffer():
    """Create buffer with temp database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    buf = DataBuffer(db_path=path)
    yield buf

    buf.close()
    os.unlink(path)


def test_store_and_retrieve(buffer):
    """Test storing and retrieving batches."""
    # Store batch
    batch_id = buffer.store(
        session_id="session-123",
        timestamp=1234567890000,
        samples=[{"channel": "ECG", "values": [100, 200, 300]}],
    )

    assert batch_id > 0

    # Retrieve
    batches = buffer.get_unsynced(limit=10)
    assert len(batches) == 1
    assert batches[0].session_id == "session-123"
    assert batches[0].samples[0]["channel"] == "ECG"


def test_mark_synced(buffer):
    """Test marking batches as synced."""
    # Store batches
    id1 = buffer.store("session-1", 1000, [])
    id2 = buffer.store("session-1", 2000, [])

    assert buffer.get_pending_count() == 2

    # Mark first as synced
    buffer.mark_synced([id1])

    assert buffer.get_pending_count() == 1

    # Verify only unsynced returned
    batches = buffer.get_unsynced()
    assert len(batches) == 1
    assert batches[0].id == id2


def test_cleanup_old(buffer):
    """Test cleanup of old data."""
    # Store and mark as synced
    id1 = buffer.store("session-1", 1000, [])
    buffer.mark_synced([id1])

    # Cleanup (0 hours = delete all synced immediately)
    deleted = buffer.cleanup_old(max_age_hours=0)

    assert deleted == 1


def test_pending_count(buffer):
    """Test pending count by session."""
    buffer.store("session-1", 1000, [])
    buffer.store("session-1", 2000, [])
    buffer.store("session-2", 3000, [])

    assert buffer.get_pending_count() == 3
    assert buffer.get_pending_for_session("session-1") == 2
    assert buffer.get_pending_for_session("session-2") == 1


def test_stats(buffer):
    """Test buffer statistics."""
    id1 = buffer.store("session-1", 1000, [])
    buffer.store("session-1", 2000, [])
    buffer.mark_synced([id1])

    stats = buffer.get_stats()

    assert stats["total"] == 2
    assert stats["pending"] == 1
    assert stats["synced"] == 1
```

---

## Testing Steps

1. Run `pytest tests/test_buffer.py`
2. Verify all tests pass
3. Test persistence:
   - Store data
   - Close and reopen buffer
   - Verify data still exists
4. Test with large amounts of data (1000+ batches)
