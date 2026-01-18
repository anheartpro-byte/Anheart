"""Offline data buffer using SQLite."""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

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
