"""Test offline data buffer."""

import os
import tempfile

import pytest

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
    batch_id = buffer.store(
        session_id="session-123",
        timestamp=1234567890000,
        samples=[{"channel": "ECG", "values": [100, 200, 300]}],
    )

    assert batch_id > 0

    batches = buffer.get_unsynced(limit=10)
    assert len(batches) == 1
    assert batches[0].session_id == "session-123"
    assert batches[0].samples[0]["channel"] == "ECG"


def test_mark_synced(buffer):
    """Test marking batches as synced."""
    id1 = buffer.store("session-1", 1000, [])
    id2 = buffer.store("session-1", 2000, [])

    assert buffer.get_pending_count() == 2

    buffer.mark_synced([id1])

    assert buffer.get_pending_count() == 1

    batches = buffer.get_unsynced()
    assert len(batches) == 1
    assert batches[0].id == id2


def test_cleanup_old(buffer):
    """Test cleanup of old data."""
    id1 = buffer.store("session-1", 1000, [])
    buffer.mark_synced([id1])

    buffer._conn.execute(
        "UPDATE buffered_data SET synced_at = synced_at - 100000 WHERE id = ?",
        (id1,),
    )
    buffer._conn.commit()

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


def test_empty_mark_synced(buffer):
    """Test marking empty list as synced."""
    buffer.mark_synced([])


def test_multiple_sessions(buffer):
    """Test handling multiple sessions."""
    buffer.store("session-1", 1000, [{"channel": "ECG", "values": [1, 2, 3]}])
    buffer.store("session-2", 2000, [{"channel": "ECG", "values": [4, 5, 6]}])
    buffer.store("session-1", 3000, [{"channel": "ECG", "values": [7, 8, 9]}])

    batches = buffer.get_unsynced()
    
    assert len(batches) == 3
    assert batches[0].timestamp == 1000
    assert batches[1].timestamp == 2000
    assert batches[2].timestamp == 3000
