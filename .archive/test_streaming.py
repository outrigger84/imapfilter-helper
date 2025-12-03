#!/usr/bin/env python3
"""Integration tests for streaming functionality."""
from pathlib import Path
import tempfile
import json

from core.rule_engine import find_matching_rule
from core.stream_processor import StreamMessage
from core.stream_resume import ResumeLog


def test_find_matching_rule():
    """Test that find_matching_rule works correctly."""
    rules = [
        {
            "name": "Rule 1",
            "priority": 100,
            "conditions": {"any": [{"header": "from", "contains": "test@example.com"}]},
            "action": {"type": "move", "target": "Test"},
        },
        {
            "name": "Rule 2",
            "priority": 50,
            "conditions": {"any": [{"header": "subject", "contains": "important"}]},
            "action": {"type": "move", "target": "Important"},
        },
    ]

    # Sort by priority (highest first)
    sorted_rules = sorted(rules, key=lambda r: int(r.get("priority", 100)), reverse=True)

    # Test matching first rule
    header1 = {"from": "test@example.com", "subject": "hello"}
    result = find_matching_rule(header1, sorted_rules)
    assert result is not None
    assert result["name"] == "Rule 1"
    print("✓ Matching first rule works")

    # Test matching second rule
    header2 = {"from": "other@example.com", "subject": "important news"}
    result = find_matching_rule(header2, sorted_rules)
    assert result is not None
    assert result["name"] == "Rule 2"
    print("✓ Matching second rule works")

    # Test no match
    header3 = {"from": "random@example.com", "subject": "hello"}
    result = find_matching_rule(header3, sorted_rules)
    assert result is None
    print("✓ No match case works")

    # Test priority ordering (Rule 1 matches both, should return Rule 1)
    header4 = {"from": "test@example.com", "subject": "important news"}
    result = find_matching_rule(header4, sorted_rules)
    assert result is not None
    assert result["name"] == "Rule 1"
    print("✓ Priority ordering works")


def test_resume_log():
    """Test that resume log tracking works."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "resume.json"
        resume_log = ResumeLog(log_file)

        # Test initial state
        assert not resume_log.is_processed("INBOX", "1")
        print("✓ Initial state is clean")

        # Test marking as processed
        resume_log.mark_processed("INBOX", "1")
        assert resume_log.is_processed("INBOX", "1")
        assert not resume_log.is_processed("INBOX", "2")
        print("✓ Mark as processed works")

        # Test persistence
        resume_log2 = ResumeLog(log_file)
        assert resume_log2.is_processed("INBOX", "1")
        print("✓ Persistence across instances works")

        # Test batch operations
        resume_log.mark_processed_batch({
            "INBOX": ["2", "3", "4"],
            "Sent": ["100", "101"],
        })
        assert resume_log.is_processed("INBOX", "2")
        assert resume_log.is_processed("Sent", "100")
        print("✓ Batch processing works")

        # Test stats
        stats = resume_log.stats()
        assert stats["INBOX"] == 4
        assert stats["Sent"] == 2
        print("✓ Statistics tracking works")

        # Test clear
        resume_log.clear()
        assert not resume_log.is_processed("INBOX", "1")
        assert not log_file.exists()
        print("✓ Clear function works")


def test_stream_message():
    """Test StreamMessage creation."""
    msg = StreamMessage(
        folder="INBOX",
        uid="12345",
        header_text="From: test@example.com\nSubject: Hello\n",
    )
    assert msg.folder == "INBOX"
    assert msg.uid == "12345"
    assert "test@example.com" in msg.header_text
    print("✓ StreamMessage creation works")


if __name__ == "__main__":
    print("\n🧪 Running Streaming Integration Tests\n")
    test_find_matching_rule()
    print()
    test_resume_log()
    print()
    test_stream_message()
    print("\n✅ All integration tests passed!\n")
