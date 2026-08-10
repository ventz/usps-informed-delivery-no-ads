#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "beautifulsoup4"]
# ///
"""Catch verification emails sent to SES_RECIPIENT.

That address has no mailbox — SES writes raw MIME straight to S3 — so any confirmation
code or click-to-verify link has to be pulled back out by hand. This does that.

    uv run tools/catch_verification.py --watch     # poll until something arrives (default 5 min)
    uv run tools/catch_verification.py             # show newest message's codes/links

Used for:
  - Gmail "Forwarding and POP/IMAP" -> add forwarding address (sends a numeric code)
  - USPS Informed Delivery account email change (sends a verification link)
  - SES identity verification, if we ever add one
"""

import argparse
import email
import email.policy
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chalicelib import config  # noqa: E402  (loads .env)

BUCKET = config.require("APP_BUCKET_NAME")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Gmail's forwarding confirmation is a 9-digit code; USPS/others use links.
CODE_PATTERNS = [
    re.compile(r"\b(\d{9})\b"),                       # Gmail forwarding code
    re.compile(r"\b(?:code|PIN)[^\w]{0,12}(\d{4,10})\b", re.I),
]
LINK_HINTS = re.compile(
    r"verify|confirm|activat|validat|authoriz", re.I
)


def client():
    profile = config.require("AWS_PROFILE")
    return boto3.Session(profile_name=profile, region_name=REGION).client("s3")


def newest(c, since=None):
    objs = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        objs.extend(page.get("Contents", []))
    if since:
        objs = [o for o in objs if o["LastModified"] > since]
    if not objs:
        return None
    return max(objs, key=lambda o: o["LastModified"])


def report(c, obj):
    raw = c.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    print(f"\n{'=' * 78}")
    print(f"From:    {msg.get('From')}")
    print(f"Subject: {msg.get('Subject')}")
    print(f"Date:    {msg.get('Date')}")
    print(f"S3 key:  {obj['Key']}")

    text, html = "", ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            text += (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        elif part.get_content_type() == "text/html":
            html += (part.get_payload(decode=True) or b"").decode("utf-8", "replace")

    body = text or BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    codes = []
    for pat in CODE_PATTERNS:
        codes.extend(pat.findall(body))
    codes = list(dict.fromkeys(codes))

    links = []
    if html:
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and (LINK_HINTS.search(href) or LINK_HINTS.search(a.get_text())):
                links.append(href)
    links.extend(u for u in re.findall(r"https?://\S+", text) if LINK_HINTS.search(u))
    links = list(dict.fromkeys(links))

    if codes:
        print(f"\n  >>> CODE(S): {'  '.join(codes)}")
    if links:
        print(f"\n  >>> VERIFICATION LINK(S):")
        for u in links:
            print(f"      {u}")
    if not codes and not links:
        print("\n  (no code/link matched — full body follows)")
        print("\n" + body[:3000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="poll for a NEW message")
    ap.add_argument("--timeout", type=int, default=300, help="watch seconds (default 300)")
    args = ap.parse_args()

    c = client()

    if not args.watch:
        obj = newest(c)
        if not obj:
            print(f"Nothing in s3://{BUCKET} yet.", file=sys.stderr)
            return 1
        report(c, obj)
        return 0

    start = datetime.now(timezone.utc)
    print(f"Watching s3://{BUCKET} for new mail (timeout {args.timeout}s)…")
    print("Trigger the verification now.")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        obj = newest(c, since=start)
        if obj:
            report(c, obj)
            return 0
        time.sleep(5)
    print("Timed out — nothing new arrived.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
