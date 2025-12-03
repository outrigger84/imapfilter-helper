"""
Wizard Cache Manager

Provides persistent caching for wizard data (folders, keywords) with TTL.
Thread-safe for concurrent wizard sessions.
"""

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


class WizardCache:
    """
    Persistent cache for wizard data (folders, keywords) with 6-hour TTL.

    Thread-safe for concurrent wizard sessions.
    """

    CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
    CACHE_VERSION = 1

    def __init__(self, cache_path: Path):
        """
        Initialize cache manager.

        Args:
            cache_path: Path to cache file (e.g., data/wizard_cache.json)
        """
        self.cache_path = cache_path
        self._lock = Lock()
        self._cache: Optional[Dict[str, Any]] = None

    def load(self) -> Dict[str, Any]:
        """
        Load cache from file.

        Returns:
            Cache dictionary or empty structure if not found/invalid
        """
        with self._lock:
            if self._cache is not None:
                return self._cache

            if not self.cache_path.exists():
                self._cache = self._empty_cache()
                return self._cache

            try:
                with open(self.cache_path, 'r') as f:
                    data = json.load(f)

                # Validate structure
                if data.get('version') != self.CACHE_VERSION:
                    self._cache = self._empty_cache()
                    return self._cache

                self._cache = data
                return self._cache

            except (json.JSONDecodeError, IOError):
                self._cache = self._empty_cache()
                return self._cache

    def save(self):
        """Save cache to file (thread-safe)."""
        with self._lock:
            if self._cache is None:
                return

            # Ensure directory exists
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically using temp file
            temp_path = self.cache_path.with_suffix('.tmp')
            try:
                with open(temp_path, 'w') as f:
                    json.dump(self._cache, f, indent=2)
                temp_path.replace(self.cache_path)
            except Exception as e:
                if temp_path.exists():
                    temp_path.unlink()
                raise

    def get_folders(self) -> Optional[List[str]]:
        """
        Get cached folders if valid (< 6 hours old).

        Returns:
            List of folder paths, or None if cache is stale/missing
        """
        cache = self.load()
        folders_cache = cache.get('folders', {})

        timestamp = folders_cache.get('timestamp', 0)
        data = folders_cache.get('data')

        if data is None:
            return None

        # Check if cache is stale
        age = time.time() - timestamp
        if age > self.CACHE_TTL_SECONDS:
            return None

        return data

    def set_folders(self, folders: List[str]):
        """
        Store folders in cache with current timestamp.

        Args:
            folders: List of folder path strings
        """
        cache = self.load()
        cache['folders'] = {
            'timestamp': time.time(),
            'data': folders
        }
        self.save()

    def get_keywords(self) -> Optional[List[Tuple[str, int]]]:
        """
        Get cached keywords if valid (< 6 hours old).

        Returns:
            List of (keyword, count) tuples, or None if stale/missing
        """
        cache = self.load()
        keywords_cache = cache.get('keywords', {})

        timestamp = keywords_cache.get('timestamp', 0)
        data = keywords_cache.get('data')

        if data is None:
            return None

        # Check if cache is stale
        age = time.time() - timestamp
        if age > self.CACHE_TTL_SECONDS:
            return None

        # Convert from JSON list format to tuples
        return [(kw, count) for kw, count in data]

    def set_keywords(self, keywords: List[Tuple[str, int]]):
        """
        Store keywords in cache with current timestamp.

        Args:
            keywords: List of (keyword, count) tuples
        """
        cache = self.load()
        # Convert tuples to lists for JSON serialization
        cache['keywords'] = {
            'timestamp': time.time(),
            'data': [[kw, count] for kw, count in keywords]
        }
        self.save()

    def invalidate_folders(self):
        """Force re-fetch of folders on next request."""
        cache = self.load()
        if 'folders' in cache:
            cache['folders']['timestamp'] = 0
            self.save()

    def invalidate_keywords(self):
        """Force re-extraction of keywords on next request."""
        cache = self.load()
        if 'keywords' in cache:
            cache['keywords']['timestamp'] = 0
            self.save()

    def clear(self):
        """Clear entire cache."""
        with self._lock:
            self._cache = self._empty_cache()
            self.save()

    def _empty_cache(self) -> Dict[str, Any]:
        """Create empty cache structure."""
        return {
            'version': self.CACHE_VERSION,
            'folders': {'timestamp': 0, 'data': None},
            'keywords': {'timestamp': 0, 'data': None}
        }
