# Parallel Cache Building Integration Tests

## Overview

The `test_parallel_integration.py` file contains comprehensive integration tests for the parallel cache building feature in imapfilter. These tests verify correctness, performance, error handling, and the smart auto-detection logic.

## Test Categories

### 1. Parallel Cache Correctness Tests (4 tests)

These tests verify that parallel cache building produces correct results:

- **test_parallel_sequential_equivalence**: Ensures parallel and sequential cache builds produce identical database contents
- **test_parallel_folder_independence**: Verifies folders with overlapping UIDs don't interfere with each other
- **test_parallel_error_isolation**: Confirms one folder's failure doesn't stop other folders from processing
- **test_parallel_progress_tracking**: Validates progress bar updates correctly during parallel processing

### 2. Smart Auto-Detection Tests (4 tests)

These tests verify the automatic worker selection logic:

- **test_auto_detect_sequential_few_folders**: Confirms < 5 folders uses 1 worker (sequential)
- **test_auto_detect_parallel_many_folders**: Confirms 5+ folders uses 5 workers (parallel)
- **test_parallel_workers_override**: Verifies `--parallel-workers N` overrides auto-detection
- **test_smart_detection_with_all_folders**: Validates `--all-folders` triggers auto-detection

### 3. Performance Tests (3 tests)

These tests measure and verify performance improvements:

- **test_parallel_faster_than_sequential**: Confirms 5 workers is faster than 1 worker (1.3x+ speedup)
- **test_worker_count_effect**: Verifies more workers = faster processing (up to a point)
- **test_memory_usage_reasonable**: Ensures parallel processing doesn't explode memory usage

### 4. Error Handling Tests (3 tests)

These tests verify robust error handling:

- **test_continue_on_folder_failure**: Confirms processing continues when one folder fails
- **test_imap_connection_failure_recovery**: Tests graceful handling of IMAP connection failures
- **test_database_concurrent_write_safety**: Verifies SQLite WAL mode prevents corruption during concurrent writes

### 5. Additional Integration Tests (3 tests)

These tests cover edge cases and feature interactions:

- **test_parallel_with_limit_and_order**: Verifies `--limit` and `--order` parameters work in parallel mode
- **test_parallel_flags_and_internaldate_preservation**: Ensures FLAGS and INTERNALDATE are correctly stored
- **test_empty_folders_handled_correctly**: Validates empty folders are properly handled

## Running the Tests

Run all parallel integration tests:
```bash
python3 -m pytest tests/test_parallel_integration.py -v
```

Run a specific test:
```bash
python3 -m pytest tests/test_parallel_integration.py::test_parallel_sequential_equivalence -v
```

Run with detailed output:
```bash
python3 -m pytest tests/test_parallel_integration.py -vvs
```

## Test Architecture

### Mock IMAP Client

The tests use a `MockIMAPClient` class that simulates IMAP operations:
- Supports folder selection, UID SEARCH, and UID FETCH commands
- Simulates network delays for performance testing
- Can be configured to fail on specific folders
- Thread-safe for concurrent access testing

### Test Context Fixture

The `test_context` fixture provides:
- Temporary database with proper WAL mode
- JSON logger for capturing log events
- Mock secrets file
- Isolated test environment

### Sample Data

The `sample_folders_data` fixture provides realistic test data:
- 5 folders (INBOX, Sent, Archive, Drafts, Trash)
- 7 total messages with varying FLAGS and INTERNALDATE
- Empty folder (Trash) to test edge cases

## Key Features Tested

1. **Correctness**: Parallel builds produce identical results to sequential builds
2. **Concurrency**: Multiple folders can be processed simultaneously without interference
3. **Error Isolation**: One folder's failure doesn't stop processing of other folders
4. **Performance**: Parallel processing provides measurable speedup (1.3x+)
5. **Auto-Detection**: Smart worker count selection based on folder count
6. **Database Safety**: Concurrent writes use SQLite WAL mode without corruption
7. **Feature Parity**: All cache_builder features (limit, order, flags) work in parallel mode

## Expected Results

All 17 tests should pass:
- 4 correctness tests
- 4 auto-detection tests
- 3 performance tests
- 3 error handling tests
- 3 additional integration tests

Total test runtime: ~6 seconds (includes simulated delays for performance testing)

## Coverage

These tests cover:
- `core/cache_builder.py::build_cache_parallel()`
- `core/connection_pool.py::IMAPConnectionPool`
- `core/cli.py` auto-detection logic
- `core/database.py` WAL mode for concurrency
- Integration with existing cache infrastructure
