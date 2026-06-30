"""Regression tests for the Date:-header fallback used by age-based rules.

Caches built without (or with a FETCH-parser that misses) INTERNALDATE store
only ``{"header": ...}``. Previously this left the message date as None, so
every age-guarded rule silently failed to match. These tests lock in the
fallback that derives the date from the message's own Date: header.
"""
import json
from datetime import datetime, timezone

from core.rule_engine import (
    _extract_message_metadata,
    _parse_header_date,
    conditions_match,
)
from core.cache_builder import _internaldate_from_header


RAW_HEADER = (
    "From: \"British Gas Energy\" <service@mail.energy.britishgas.co.uk>\r\n"
    "Subject: Join our PeakSave Green Flex event on 03/06/2025\r\n"
    "Date: Mon, 15 May 2023 12:37:22 +0100\r\n"
    "\r\n"
)


def test_extract_metadata_falls_back_to_date_header():
    """No internaldate in the payload -> date comes from the Date: header."""
    payload = json.dumps({"header": RAW_HEADER})
    header, flags, date = _extract_message_metadata(payload)

    assert header["from"].endswith("@mail.energy.britishgas.co.uk>")
    assert date is not None
    assert date == datetime(2023, 5, 15, 12, 37, 22, tzinfo=date.tzinfo)
    assert date.tzinfo is not None  # always timezone-aware


def test_internaldate_in_payload_takes_precedence():
    """An explicit internaldate is not overridden by the Date: header."""
    payload = json.dumps(
        {"header": RAW_HEADER, "internaldate": "20-Oct-2025 07:30:19 +0000"}
    )
    _, _, date = _extract_message_metadata(payload)
    assert date == datetime(2025, 10, 20, 7, 30, 19, tzinfo=timezone.utc)


def test_age_rule_matches_with_date_header_only():
    """The original failure: an age_days_gt rule must match an old message
    whose date is only available via the Date: header."""
    payload = json.dumps({"header": RAW_HEADER})
    header, flags, date = _extract_message_metadata(payload)

    conditions = {
        "all": [
            {"header": "from", "contains": "@mail.energy.britishgas.co.uk"},
            {"header": "subject", "regex": "(?i)peak.?save"},
            {"age_days_gt": 3},
        ]
    }
    assert conditions_match(header, conditions, flags, date) is True


def test_parse_header_date_handles_missing_and_bad_values():
    assert _parse_header_date(None) is None
    assert _parse_header_date("") is None
    assert _parse_header_date("not a date") is None
    # Naive date (no tz) is assumed UTC.
    dt = _parse_header_date("Mon, 15 May 2023 12:37:22")
    assert dt is not None and dt.tzinfo is not None


def test_builder_derives_internaldate_from_header():
    """Builder fallback emits an INTERNALDATE-format string from Date:."""
    result = _internaldate_from_header(RAW_HEADER.encode())
    assert result is not None
    # Round-trips back through the engine's internaldate parser.
    payload = json.dumps({"header": RAW_HEADER, "internaldate": result})
    _, _, date = _extract_message_metadata(payload)
    assert date == datetime(2023, 5, 15, 12, 37, 22, tzinfo=date.tzinfo)


def test_builder_internaldate_none_without_date_header():
    assert _internaldate_from_header(b"From: x@y.com\r\nSubject: no date\r\n\r\n") is None
    assert _internaldate_from_header(b"") is None
