#!/usr/bin/env python3
"""Test STATUS command on ALL folders to find which one hangs."""
import imaplib
import socket
import json
import sys
import time
from pathlib import Path

# Load your secrets
secrets_path = Path("data") / "secrets.json"
if not secrets_path.exists():
    print(f"❌ Secrets file not found at {secrets_path}")
    sys.exit(1)
with open(secrets_path) as f:
    secrets = json.load(f)

host = secrets["imap"]["host"]
port = secrets["imap"].get("port", 993)
username = secrets["imap"]["username"]
password = secrets["imap"]["password"]

print(f"Testing IMAP STATUS on ALL {username}'s folders")
print(f"Server: {host}:{port}")
print()

# Connect
print("Connecting...")
mail = imaplib.IMAP4_SSL(host, port, timeout=15)
mail.login(username, password)
print("✓ Logged in\n")

# Get all folders
print("Listing all folders...")
status, mailboxes = mail.list()
folders = []
for mb in mailboxes:
    parts = mb.decode().split(' "/" ')
    if len(parts) == 2:
        folder = parts[1].strip('"')
        folders.append(folder)

print(f"✓ Got {len(folders)} folders\n")

# Test STATUS on all folders with timeout tracking
print(f"Testing STATUS on all {len(folders)} folders...")
print("(This may take a minute or two - watch for which folder stalls)\n")

start_time = time.time()
slow_folders = []
failed_folders = []

for i, folder in enumerate(folders, 1):
    folder_start = time.time()
    try:
        status, data = mail.status(f'"{folder}"', "(MESSAGES)")
        folder_time = time.time() - folder_start

        if folder_time > 1.0:  # Flag folders that take >1 second
            slow_folders.append((folder, folder_time))
            print(f"[{i:4d}/{len(folders)}] ⚠️  SLOW ({folder_time:.2f}s): {folder[:60]}")
        else:
            if i % 100 == 0:
                print(f"[{i:4d}/{len(folders)}] ✓ {folder[:60]}")
    except socket.timeout:
        print(f"\n[{i:4d}/{len(folders)}] ❌ TIMEOUT on: {folder}")
        failed_folders.append((folder, "TIMEOUT"))
        break
    except Exception as e:
        failed_folders.append((folder, str(e)))
        print(f"\n[{i:4d}/{len(folders)}] ❌ ERROR on: {folder} - {e}")
        break

total_time = time.time() - start_time

print(f"\n{'='*70}")
print(f"Total time: {total_time:.2f}s for {len(folders)} folders")
print(f"Average: {total_time/len(folders)*1000:.1f}ms per folder")

if slow_folders:
    print("\n⚠️  Slow folders (>1s):")
    for folder, elapsed in sorted(slow_folders, key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {elapsed:.2f}s - {folder}")

if failed_folders:
    print("\n❌ Failed folders:")
    for folder, error in failed_folders:
        print(f"   {folder}: {error}")
else:
    print(f"\n✅ All {len(folders)} folders completed successfully!")

mail.logout()
