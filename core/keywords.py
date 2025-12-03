"""Predefined keywords management for IMAP filter rules."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List


class KeywordManager:
    """Manage predefined keywords that take precedence over cached keywords."""

    DEFAULT_KEYWORDS = [
        "Important",
        "Review Needed",
        "Action Item",
        "Archive",
        "Hold",
        "Personal",
    ]

    def __init__(self, config_dir: Path):
        """
        Initialize KeywordManager.

        Args:
            config_dir: Path to configuration directory (e.g., data/ or ~/.imapfilter)
        """
        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / "keywords.json"
        self.keywords = self._load_keywords()
        # Create config file with defaults if it doesn't exist
        if not self.config_file.exists():
            self.save_keywords(self.keywords)

    def _load_keywords(self) -> List[str]:
        """
        Load predefined keywords from JSON config.

        Returns:
            List of keyword strings, defaults to DEFAULT_KEYWORDS if file doesn't exist
        """
        if not self.config_file.exists():
            return self.DEFAULT_KEYWORDS.copy()

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "predefined_keywords" in data:
                    keywords = data["predefined_keywords"]
                    if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
                        return keywords
        except (json.JSONDecodeError, IOError, TypeError):
            pass

        return self.DEFAULT_KEYWORDS.copy()

    def save_keywords(self, keywords: List[str]) -> None:
        """
        Save keywords to JSON config file.

        Args:
            keywords: List of keyword strings to save
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump({"predefined_keywords": keywords}, f, indent=2)
        self.keywords = keywords

    def add_keyword(self, keyword: str) -> bool:
        """
        Add a keyword if not already present.

        Args:
            keyword: Keyword to add

        Returns:
            True if added, False if already exists
        """
        keyword = keyword.strip()
        if not keyword:
            return False

        if keyword not in self.keywords:
            self.keywords.append(keyword)
            self.save_keywords(self.keywords)
            return True

        return False

    def remove_keyword(self, keyword: str) -> bool:
        """
        Remove a keyword.

        Args:
            keyword: Keyword to remove

        Returns:
            True if removed, False if not found
        """
        keyword = keyword.strip()
        if keyword in self.keywords:
            self.keywords.remove(keyword)
            self.save_keywords(self.keywords)
            return True

        return False

    def get_keywords(self) -> List[str]:
        """
        Get all predefined keywords.

        Returns:
            List of keyword strings
        """
        return self.keywords.copy()

    def keyword_exists(self, keyword: str) -> bool:
        """
        Check if a keyword exists in predefined keywords.

        Args:
            keyword: Keyword to check

        Returns:
            True if keyword exists
        """
        return keyword in self.keywords
