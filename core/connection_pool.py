"""Thread-safe IMAP connection pool for parallel cache operations."""
from __future__ import annotations

import imaplib
import queue
import threading
from pathlib import Path

from core.imap_client import imap_login
from core.logging_utils import JsonLogger


class IMAPConnectionPool:
    """
    Thread-safe connection pool for IMAP operations.

    Manages a pool of IMAP connections for parallel folder processing.
    Uses lazy connection creation up to a maximum limit.
    """

    def __init__(self, secrets_path: Path, max_connections: int, logger: JsonLogger):
        """
        Initialize the connection pool.

        Args:
            secrets_path: Path to IMAP secrets file
            max_connections: Maximum number of concurrent connections
            logger: JsonLogger for logging
        """
        self.secrets_path = secrets_path
        self.max_connections = max_connections
        self.logger = logger
        self._pool: queue.Queue[imaplib.IMAP4_SSL] = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def acquire(self) -> imaplib.IMAP4_SSL:
        """
        Acquire a connection from the pool.

        Returns a connection from the pool if available, or creates a new one
        if below the max_connections limit. Blocks if at max capacity.

        Returns:
            An authenticated IMAP4_SSL connection
        """
        # Try to get a connection from the pool without blocking
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass

        # Reserve a slot under the lock, then create the connection outside it.
        # imap_login() must NOT be called while holding _lock: it is slow and
        # if it raises, incrementing _created inside the lock would leave the
        # counter permanently wrong, causing waiters on _pool.get() to deadlock.
        with self._lock:
            if self._created < self.max_connections:
                self._created += 1
                should_create = True
            else:
                should_create = False

        if should_create:
            try:
                return imap_login(self.secrets_path, self.logger)
            except Exception:
                with self._lock:
                    self._created -= 1
                raise

        # Wait for a connection to be returned to the pool (120 s safety cap)
        try:
            return self._pool.get(timeout=120)
        except queue.Empty:
            raise RuntimeError("Timed out waiting for an IMAP connection from the pool")

    def release(self, conn: imaplib.IMAP4_SSL) -> None:
        """
        Release a connection back to the pool.

        Args:
            conn: The IMAP connection to return to the pool
        """
        self._pool.put(conn)

    def discard(self, conn: imaplib.IMAP4_SSL) -> None:
        """
        Discard a connection that is in a bad/corrupted state.

        Closes the connection and decrements the created count so a fresh
        connection can be created on the next acquire() call.

        Args:
            conn: The broken IMAP connection to close and discard
        """
        with self._lock:
            self._created = max(0, self._created - 1)
        try:
            conn.shutdown()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Close all connections in the pool."""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.logout()
            except (queue.Empty, Exception):
                # Ignore errors during shutdown
                pass
