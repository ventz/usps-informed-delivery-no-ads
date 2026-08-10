#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "beautifulsoup4", "openai"]
# ///
"""Run the pipeline locally against a fixture, a local .eml, or an S3 object.

    uv run tools/test_local.py --all                    # every fixture in ./samples
    uv run tools/test_local.py --eml samples/2026-08-07.eml
    uv run tools/test_local.py --s3-key <key>           # a real forwarded email
    uv run tools/test_local.py --all --vision           # actually call OpenAI
    uv run tools/test_local.py --eml <f> --send         # really send via SES

Vision and sending are BOTH off by default — this stays free and side-effect-free
unless asked otherwise.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from chalicelib import config  # noqa: E402  (needs ROOT on sys.path)


def load_env():
    config.load_dotenv()


def summarize(digest, classifications):
    print(f"  subject      : {digest.subject}")
    print(f"  date         : {digest.digest_date}  ({digest.date_text or 'n/a'})")
    print(f"  announced    : {digest.announced_mail} mail, {digest.announced_packages} packages")
    print(f"  scans KEPT   : {len(digest.scans)}  {[s.filename for s in digest.scans]}")
    print(f"  ads DROPPED  : {len(digest.dropped_ads)}  {digest.dropped_ads}")
    print(f"  hidden by ads: {digest.hidden_mail_count}")
    if digest.campaign_senders:
        print(f"  advertisers  : {digest.campaign_senders}")
    print(f"  packages     : {len(digest.packages)}")
    for p in digest.packages:
        print(f"      - {p.sender!r} [{p.status}] {p.tracking or '-'} {p.eta or ''}")
    for scan, cls in zip(digest.scans, classifications):
        print(f"      * {scan.filename}: {cls.sender} / {cls.mail_type} / action={cls.actionable}")


def run(raw: bytes, label: str, use_vision: bool, send: bool, outdir: Path) -> bool:
    from chalicelib import classify, parse, render

    print(f"\n=== {label} ===")
    digest = parse.parse_digest(raw)

    if use_vision:
        classifications = classify.classify_all(digest.scans, digest.campaign_senders)
    else:
        classifications = [classify.Classification(
            sender=f"(unclassified) {s.filename}", mail_type="unknown"
        ) for s in digest.scans]

    summarize(digest, classifications)

    html = render.build_html(digest, classifications)
    outdir.mkdir(parents=True, exist_ok=True)
    preview = outdir / f"{label}.html"
    preview.write_text(html, encoding="utf-8")
    print(f"  preview      : {preview}")

    message = render.build_message(
        digest, classifications,
        config.require("DIGEST_FROM"),
        config.require("DIGEST_TO"),
    )
    size = len(message.as_bytes())
    print(f"  MIME size    : {size:,} bytes  (SES limit 40MB)")
    print(f"  subject line : {render.subject_for(digest)}")

    if send:
        import boto3
        ses = boto3.Session(
            profile_name=config.require("AWS_PROFILE"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        ).client("ses")
        resp = ses.send_raw_email(
            Source=os.environ["DIGEST_FROM"],
            Destinations=[os.environ["DIGEST_TO"]],
            RawMessage={"Data": message.as_bytes()},
        )
        print(f"  SENT         : {resp['MessageId']} -> {os.environ['DIGEST_TO']}")

    return True


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--all", action="store_true", help="run every .eml in ./samples")
    src.add_argument("--eml", help="path to a local .eml")
    src.add_argument("--s3-key", help="key in the input bucket")
    ap.add_argument("--vision", action="store_true", help="really call OpenAI")
    ap.add_argument("--send", action="store_true", help="really send via SES")
    ap.add_argument("--out", default=str(ROOT / "out"))
    args = ap.parse_args()

    load_env()
    outdir = Path(args.out)

    if args.all:
        samples = sorted((ROOT / "samples").glob("*.eml"))
        if not samples:
            print("No fixtures. Run: uv run tools/make_fixture.py", file=sys.stderr)
            return 1
        for path in samples:
            run(path.read_bytes(), path.stem, args.vision, args.send, outdir)
        return 0

    if args.eml:
        path = Path(args.eml)
        return 0 if run(path.read_bytes(), path.stem, args.vision, args.send, outdir) else 1

    import boto3
    s3 = boto3.Session(
        profile_name=config.require("AWS_PROFILE"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    ).client("s3")
    bucket = config.require("APP_BUCKET_NAME")
    raw = s3.get_object(Bucket=bucket, Key=args.s3_key)["Body"].read()
    return 0 if run(raw, args.s3_key.replace("/", "_"), args.vision, args.send, outdir) else 1


if __name__ == "__main__":
    sys.exit(main())
