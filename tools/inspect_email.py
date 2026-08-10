#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "beautifulsoup4"]
# ///
"""Pull forwarded Informed Delivery emails out of S3 and dump their MIME shape.

This is the first thing to run once an email has been forwarded to
SES_RECIPIENT. It answers the question the parser design hinges on:
are the mailpiece scans inline MIME attachments, or remote URLs?

    uv run tools/inspect_email.py              # newest object
    uv run tools/inspect_email.py --all        # every object in the bucket
    uv run tools/inspect_email.py --save       # also write parts to ./samples/<key>/
"""

import argparse
import email
import email.policy
import os
import re
import sys
from collections import Counter
from pathlib import Path

import boto3
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chalicelib import config  # noqa: E402  (loads .env)

BUCKET = config.require("APP_BUCKET_NAME")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


def s3():
    profile = config.require("AWS_PROFILE")
    return boto3.Session(profile_name=profile, region_name=REGION).client("s3")


def list_keys(client):
    """Newest-first list of objects in the input bucket."""
    objs = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        objs.extend(page.get("Contents", []))
    return [o["Key"] for o in sorted(objs, key=lambda o: o["LastModified"], reverse=True)]


def describe(msg, key, save=False):
    print(f"\n{'=' * 78}\nKEY: {key}")
    print(f"From:    {msg.get('From')}")
    print(f"To:      {msg.get('To')}")
    print(f"Subject: {msg.get('Subject')}")
    print(f"Date:    {msg.get('Date')}")
    print(f"Top-level Content-Type: {msg.get_content_type()}")

    outdir = SAMPLE_DIR / re.sub(r"[^\w.-]", "_", key)
    if save:
        outdir.mkdir(parents=True, exist_ok=True)

    print(f"\n--- MIME tree ---")
    types = Counter()
    html_parts = []
    for i, part in enumerate(msg.walk()):
        ctype = part.get_content_type()
        types[ctype] += 1
        cid = part.get("Content-ID", "")
        disp = part.get_content_disposition() or ""
        fname = part.get_filename() or ""
        depth = "  " * len(part.get("Content-Type", "").split("/"))

        try:
            payload = part.get_payload(decode=True)
            size = len(payload) if payload else 0
        except Exception:
            payload, size = None, 0

        if ctype.startswith("multipart/"):
            print(f"{depth}[{i:>3}] {ctype}")
            continue

        bits = [f"{size:>8,}B", ctype]
        if cid:
            bits.append(f"cid={cid}")
        if disp:
            bits.append(f"disp={disp}")
        if fname:
            bits.append(f"name={fname}")
        print(f"{depth}[{i:>3}] " + "  ".join(bits))

        if ctype == "text/html" and payload:
            html_parts.append(payload)
        if save and payload:
            ext = (ctype.split("/")[-1] or "bin")[:8]
            (outdir / f"part{i:03d}.{ext}").write_bytes(payload)

    print(f"\n--- part-type totals ---")
    for t, n in types.most_common():
        print(f"  {n:>3}  {t}")

    # THE question: inline scans vs. remote URLs.
    inline_images = sum(n for t, n in types.items() if t.startswith("image/"))
    print(f"\n--- image sourcing ---")
    print(f"  inline image parts: {inline_images}")

    for payload in html_parts:
        soup = BeautifulSoup(payload, "html.parser")
        imgs = [i.get("src", "") for i in soup.find_all("img")]
        cid_refs = [s for s in imgs if s.startswith("cid:")]
        remote = [s for s in imgs if s.startswith("http")]
        data_uris = [s for s in imgs if s.startswith("data:")]
        print(f"  <img> total={len(imgs)}  cid:={len(cid_refs)}  http={len(remote)}  data:={len(data_uris)}")
        if remote:
            print(f"  remote hosts:")
            for host, n in Counter(re.sub(r"^https?://([^/]+)/.*", r"\1", u) for u in remote).most_common():
                print(f"    {n:>3}  {host}")
            print(f"  sample remote URLs:")
            for u in remote[:5]:
                print(f"    {u[:160]}")

    if save:
        print(f"\n  saved parts -> {outdir}")

    verdict = "INLINE (self-contained)" if inline_images else "REMOTE (must re-host on receipt)"
    print(f"\n  >>> SCAN SOURCING VERDICT: {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="inspect every object, not just newest")
    ap.add_argument("--save", action="store_true", help="write decoded parts to ./samples/")
    args = ap.parse_args()

    client = s3()
    keys = list_keys(client)
    if not keys:
        print(f"No objects in s3://{BUCKET} — forward an Informed Delivery email to "
              f"{config.get('SES_RECIPIENT', 'your SES address')} first.", file=sys.stderr)
        return 1

    print(f"{len(keys)} object(s) in s3://{BUCKET}")
    for key in keys if args.all else keys[:1]:
        raw = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        describe(msg, key, save=args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
