#!/usr/bin/env python3
"""
IMAP server capability and performance probe.

Discovers server limits, supported extensions, and throughput
characteristics in a single automated run.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import tqdm as _tqdm_module

# Redirect tqdm.write to stderr so stdout stays clean for the report.
# imap_login() calls tqdm.write() which defaults to stdout, corrupting report output.
_tqdm_module.tqdm.write = lambda s, file=None, end="\n", nolock=False: print(
    s, file=sys.stderr, end=end, flush=True
)

from core.config import build_default_config
from core.imap_client import imap_login, list_all_folders
from core.logging_utils import JsonLogger
from core.server_probe import (
    ProbeResults,
    format_report,
    probe_capabilities,
    probe_command_latency,
    probe_connection_limit,
    probe_download_speed,
    probe_folder_attributes,
    probe_search_performance,
    probe_upload_speed,
    save_json_report,
)


def _progress(msg: str) -> None:
    """Print progress message to stderr."""
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    """Run the IMAP server probe."""
    parser = argparse.ArgumentParser(
        description="IMAP Server Probe — capability and performance discovery"
    )
    parser.add_argument(
        "--folders",
        metavar="FOLDER",
        nargs="+",
        default=["INBOX"],
        help="Folders to benchmark (default: INBOX)",
    )
    parser.add_argument(
        "--all-folders",
        action="store_true",
        help="Test all folders (capped at 5 for speed)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip upload test (avoids writing to server)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=16,
        metavar="N",
        help="Max simultaneous connections to test (default: 16)",
    )
    parser.add_argument(
        "--latency-samples",
        type=int,
        default=10,
        metavar="N",
        help="NOOP/SELECT/STATUS samples per command (default: 10)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        metavar="PATH",
        help="Save detailed JSON report to this file",
    )

    try:
        args = parser.parse_args()

        config = build_default_config()
        secrets_path = config.paths.secrets_file
        logger = JsonLogger(log_file=config.paths.log_file)

        # Read host/port for report header before connecting
        host = "unknown"
        port = 993
        if secrets_path.exists():
            with secrets_path.open(encoding="utf-8") as fh:
                secrets = json.load(fh)
            imap_cfg = secrets.get("imap", {})
            host = imap_cfg.get("host", "unknown")
            port = imap_cfg.get("port", 993)

        probed_at = datetime.now(timezone.utc).isoformat()
        results = ProbeResults(host=host, port=port, probed_at=probed_at)

        # --- Open main connection ---
        _progress("Connecting to IMAP server...")
        client = imap_login(secrets_path, logger)

        # --- Capabilities ---
        _progress("Probing capabilities...")
        results.capabilities = probe_capabilities(client)

        # --- Connection limit ---
        _progress("Probing connection concurrency...")
        results.connection_limit = probe_connection_limit(
            secrets_path, args.max_connections, logger
        )

        # --- Resolve folders to benchmark ---
        if args.all_folders:
            _progress("Listing all folders...")
            all_folders = list_all_folders(client)
            benchmark_folders = all_folders[:5]
        else:
            benchmark_folders = args.folders

        first_folder = benchmark_folders[0] if benchmark_folders else "INBOX"

        # --- Command latency ---
        _progress(f"Measuring command latency ({args.latency_samples} samples, {first_folder})...")
        results.latency = probe_command_latency(client, first_folder, args.latency_samples)

        # --- Download speed ---
        for folder in benchmark_folders:
            _progress(f"Benchmarking download speed: {folder}...")
            results.download.append(probe_download_speed(client, folder))

        # --- Upload speed ---
        if not args.skip_upload:
            _progress(f"Benchmarking upload speed: {first_folder}...")
            results.upload = probe_upload_speed(client, first_folder)

        # --- Search performance ---
        for folder in benchmark_folders:
            _progress(f"Benchmarking search performance: {folder}...")
            results.search.append(probe_search_performance(client, folder))

        # --- Folder attributes ---
        _progress("Listing folder attributes...")
        results.folders = probe_folder_attributes(client)

        try:
            client.logout()
        except Exception:
            pass

        # --- Output ---
        report = format_report(results)
        print(report)

        if args.output_json:
            save_json_report(results, args.output_json)
            _progress(f"JSON report saved to {args.output_json}")

        return 0

    except KeyboardInterrupt:
        print(file=sys.stderr)
        print("Probe cancelled by user.", file=sys.stderr)
        return 130

    except Exception as exc:
        print(f"\nUnexpected error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
