#!/usr/bin/env python3
"""
Standalone script to manually merge worker database files.

This script is used as a fallback when the automatic merge process fails.
It reads all thread_*.db files from a temporary directory and merges them
into a target database.

Usage:
    python3 merge_worker_dbs.py --temp-dir /path/to/temp/dir --output /path/to/merged.db
    python3 merge_worker_dbs.py --temp-dir /tmp/imapfilter_cache_12345 --output ./cache.db
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Manually merge worker database files from parallel cache building"
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        required=True,
        help="Directory containing thread_*.db files to merge"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output database file path"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed merge progress"
    )

    args = parser.parse_args()

    # Validate temp directory
    if not args.temp_dir.exists():
        print(f"❌ Error: Temp directory does not exist: {args.temp_dir}")
        return 1

    if not args.temp_dir.is_dir():
        print(f"❌ Error: Not a directory: {args.temp_dir}")
        return 1

    # Find all worker databases
    worker_dbs = sorted(args.temp_dir.glob("thread_*.db"))
    if not worker_dbs:
        print(f"❌ Error: No worker databases found in {args.temp_dir}")
        print(f"   Looking for: thread_*.db files")
        return 1

    print(f"📂 Found {len(worker_dbs)} worker database(s) to merge")
    for db_path in worker_dbs:
        print(f"   - {db_path.name}")

    # Prepare output database
    output_dir = args.output.parent
    if output_dir != Path(".") and not output_dir.exists():
        print(f"📁 Creating output directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n🔄 Merging into: {args.output}")

    # Create or open output database
    try:
        main_db = sqlite3.connect(str(args.output), timeout=60.0, check_same_thread=False)
        print("✅ Opened output database")
    except sqlite3.OperationalError as err:
        print(f"❌ Error opening output database: {err}")
        return 1

    # Ensure schema exists
    try:
        main_db.execute(
            "CREATE TABLE IF NOT EXISTS headers "
            "(folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
            "PRIMARY KEY (folder, uid))"
        )
        main_db.execute(
            "CREATE TABLE IF NOT EXISTS folders "
            "(id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)"
        )
        print("✅ Initialized database schema")
    except sqlite3.OperationalError as err:
        print(f"❌ Error initializing schema: {err}")
        main_db.close()
        return 1

    # Merge each worker database
    total_headers = 0
    total_folders = 0
    failed_dbs = []

    for worker_db_path in worker_dbs:
        db_name = worker_db_path.name
        try:
            if args.verbose:
                print(f"\n📖 Reading {db_name}...")

            # Open and read worker database
            temp_db = sqlite3.connect(str(worker_db_path), timeout=30.0)
            temp_db.execute("PRAGMA query_only=TRUE")

            # Read headers
            headers_cursor = temp_db.execute(
                "SELECT folder, uid, data, updated_at FROM headers"
            )
            headers_rows = headers_cursor.fetchall()

            # Read folders
            folders_cursor = temp_db.execute(
                "SELECT name, parent, updated_at FROM folders"
            )
            folders_rows = folders_cursor.fetchall()

            # Read UIDVALIDITY snapshots (absent in temp DBs from older builds)
            try:
                uidvalidity_rows = temp_db.execute(
                    "SELECT folder, uidvalidity, updated_at FROM folder_uidvalidity"
                ).fetchall()
            except sqlite3.OperationalError:
                uidvalidity_rows = []

            temp_db.close()

            if args.verbose:
                print(f"   - Read {len(headers_rows)} headers, {len(folders_rows)} folders")

            # Insert with retries
            max_retries = 5
            insert_successful = False

            for attempt in range(max_retries):
                try:
                    # Insert headers
                    main_db.executemany(
                        "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
                        headers_rows
                    )

                    # Insert folders
                    main_db.executemany(
                        "INSERT OR REPLACE INTO folders (name, parent, updated_at) VALUES (?, ?, ?)",
                        folders_rows
                    )

                    if uidvalidity_rows:
                        main_db.execute(
                            "CREATE TABLE IF NOT EXISTS folder_uidvalidity "
                            "(folder TEXT PRIMARY KEY, uidvalidity TEXT, updated_at TEXT)"
                        )
                        main_db.executemany(
                            "INSERT OR REPLACE INTO folder_uidvalidity (folder, uidvalidity, updated_at) VALUES (?, ?, ?)",
                            uidvalidity_rows
                        )

                    insert_successful = True
                    break

                except sqlite3.OperationalError as db_error:
                    if "database is locked" in str(db_error) and attempt < max_retries - 1:
                        delay = 0.5 * (2 ** attempt)
                        if args.verbose:
                            print(f"   ⏳ Database locked, retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                    raise

            if insert_successful:
                total_headers += len(headers_rows)
                total_folders += len(folders_rows)
                print(f"✅ {db_name}: {len(headers_rows)} headers, {len(folders_rows)} folders")
            else:
                failed_dbs.append(db_name)
                print(f"❌ {db_name}: Failed to insert data")

        except Exception as err:
            failed_dbs.append(db_name)
            print(f"❌ {db_name}: {err}")

    # Commit changes
    try:
        main_db.commit()
        print(f"\n✅ Committed all changes")
    except sqlite3.OperationalError as err:
        print(f"❌ Error committing changes: {err}")
        main_db.close()
        return 1

    main_db.close()

    # Report results
    print(f"\n📊 Merge Complete")
    print(f"   📧 Total headers: {total_headers}")
    print(f"   📁 Total folders: {total_folders}")

    if failed_dbs:
        print(f"\n⚠️  {len(failed_dbs)} database(s) failed:")
        for db_name in failed_dbs:
            print(f"   - {db_name}")
        return 1

    print(f"\n✅ Successfully merged {len(worker_dbs)} databases into {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
