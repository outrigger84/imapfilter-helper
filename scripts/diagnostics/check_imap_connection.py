#!/usr/bin/env python3
import imaplib
import socket
import json
import sys
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

print(f"Testing IMAP connection to {host}:{port}")
print(f"Username: {username}")
print()

# Test 1: Basic socket connection
print("1️⃣  Testing socket connection...")
try:
    sock = socket.create_connection((host, port), timeout=10)
    print("   ✓ Socket connected")
    sock.close()
except Exception as e:
    print(f"   ✗ Socket failed: {e}")
    sys.exit(1)

# Test 2: IMAP4_SSL with timeout
print("\n2️⃣  Testing IMAP4_SSL (WITH 15s timeout)...")
try:
    mail = imaplib.IMAP4_SSL(host, port, timeout=15)
    print("   ✓ IMAP4_SSL connected")
    print(f"   Server greeting: {mail.welcome}")
except socket.timeout:
    print("   ✗ TIMEOUT on IMAP4_SSL connection")
    sys.exit(1)
except Exception as e:
    print(f"   ✗ IMAP4_SSL failed: {e}")
    sys.exit(1)

# Test 3: Login
print("\n3️⃣  Testing login...")
try:
    print(f"   Logging in as {username}...")
    mail.login(username, password)
    print("   ✓ Login successful")
except socket.timeout:
    print("   ✗ TIMEOUT during login")
    sys.exit(1)
except Exception as e:
    print(f"   ✗ Login failed: {e}")
    sys.exit(1)

# Test 4: List folders
print("\n4️⃣  Listing folders (LIST command)...")
try:
    status, mailboxes = mail.list()
    print(f"   ✓ Got {len(mailboxes)} mailboxes")
    folders = []
    for mb in mailboxes[:5]:
        parts = mb.decode().split(' "/" ')
        if len(parts) == 2:
            folder = parts[1].strip('"')
            folders.append(folder)
            print(f"     - {folder}")
except socket.timeout:
    print("   ✗ TIMEOUT listing folders")
    sys.exit(1)
except Exception as e:
    print(f"   ✗ List failed: {e}")
    sys.exit(1)

# Test 5: STATUS command on each folder
print(f"\n5️⃣  Testing STATUS command on {len(folders)} folders (THIS IS WHERE IT MIGHT STALL)...")
try:
    for folder in folders:
        print(f"   Testing STATUS on '{folder}'...", end=" ", flush=True)
        status, data = mail.status(f'"{folder}"', "(MESSAGES)")
        if status == "OK" and data and data[0]:
            response = data[0].decode('utf-8', 'ignore')
            print(f"✓ {response}")
        else:
            print(f"⚠ Got status={status}, data={data}")
except socket.timeout:
    print("\n   ✗ TIMEOUT on STATUS command (THIS IS THE ISSUE!)")
    print("   The STATUS command hangs on some folders")
    sys.exit(1)
except Exception as e:
    print(f"\n   ✗ STATUS failed: {e}")
    sys.exit(1)

print("\n✅ All tests passed!")
mail.logout()
