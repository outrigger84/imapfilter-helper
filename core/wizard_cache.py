"""
Wizard Cache Manager

Provides persistent caching for wizard data (folders, keywords) with TTL.
Thread-safe for concurrent wizard sessions.
Includes coverage analysis caching for performance optimization.
"""

import json
import os
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

    def get_coverage(self, rules_dir: Path, cache_db: Path) -> Optional[Dict[str, Any]]:
        """
        Get cached coverage analysis if valid.

        Checks if rules directory and cache database are unchanged since cache was built.

        Args:
            rules_dir: Path to the rules directory
            cache_db: Path to the cache database

        Returns:
            Coverage cache dict with stats, uncovered_messages, and domain_clusters,
            or None if cache is stale/missing
        """
        cache = self.load()
        coverage_cache = cache.get('coverage', {})

        data = coverage_cache.get('data')
        if data is None:
            return None

        # Get cached mtimes
        cached_rules_mtime = coverage_cache.get('rules_mtime')
        cached_db_mtime = coverage_cache.get('db_mtime')

        # Get current mtimes
        try:
            current_rules_mtime = os.path.getmtime(str(rules_dir))
            current_db_mtime = os.path.getmtime(str(cache_db))
        except (OSError, ValueError):
            # Files don't exist or can't stat
            return None

        # Invalidate if mtimes changed
        if (cached_rules_mtime != current_rules_mtime or
                cached_db_mtime != current_db_mtime):
            return None

        return data

    def set_coverage(self, coverage_data: Dict[str, Any], rules_dir: Path, cache_db: Path):
        """
        Store coverage analysis result with mtime tracking.

        Args:
            coverage_data: Dict with 'stats', 'uncovered_messages', 'domain_clusters'
            rules_dir: Path to the rules directory
            cache_db: Path to the cache database
        """
        cache = self.load()
        try:
            rules_mtime = os.path.getmtime(str(rules_dir))
            db_mtime = os.path.getmtime(str(cache_db))
        except (OSError, ValueError):
            # Can't get mtimes, don't cache
            return

        # Verify coverage_data is not empty
        if not coverage_data or 'stats' not in coverage_data:
            return

        cache['coverage'] = {
            'timestamp': time.time(),
            'rules_mtime': rules_mtime,
            'db_mtime': db_mtime,
            'data': coverage_data
        }
        self.save()

    def invalidate_coverage(self):
        """Force re-analysis of coverage on next request."""
        cache = self.load()
        if 'coverage' in cache:
            cache['coverage']['data'] = None
            self.save()

    def _empty_cache(self) -> Dict[str, Any]:
        """Create empty cache structure."""
        return {
            'version': self.CACHE_VERSION,
            'folders': {'timestamp': 0, 'data': None},
            'keywords': {'timestamp': 0, 'data': None},
            'coverage': {'timestamp': 0, 'rules_mtime': None, 'db_mtime': None, 'data': None}
        }
