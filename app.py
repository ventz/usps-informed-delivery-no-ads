"""USPS Informed Delivery cleaner — S3-triggered Lambda.

forward -> SES (SES_RECIPIENT) -> S3 -> here -> clean email -> DIGEST_TO

All addresses/bucket names come from the environment (.chalice/config.json in
Lambda, .env locally). Nothing environment-specific is hardcoded.

Output is EMAIL ONLY. There is deliberately no output bucket and no web surface;
see CLAUDE.md "CRITICAL CONSTRAINT". Do not add one.
"""

import logging
import os

import boto3
from botocore.exceptions import ClientError
from chalice import Chalice

from chalicelib import classify, config, parse, render

app = Chalice(app_name="usps")
app.log.setLevel(logging.INFO)
logging.getLogger("chalicelib").setLevel(logging.INFO)

S3_BUCKET = config.require("APP_BUCKET_NAME")
DIGEST_TO = config.require("DIGEST_TO")
DIGEST_FROM = config.require("DIGEST_FROM")

# SES drops this object into the bucket when a receipt rule is created.
SKIP_KEYS = {"AMAZON_SES_SETUP_NOTIFICATION"}


def process_raw_email(raw: bytes) -> dict:
    """Parse -> classify -> send. Returns a small summary dict for logging/tests."""
    digest = parse.parse_digest(raw)
    app.log.info(
        "parsed: %s mail announced, %s scans kept, %s ads dropped, %s packages",
        digest.announced_mail,
        len(digest.scans),
        len(digest.dropped_ads),
        len(digest.packages),
    )

    classifications = classify.classify_all(digest.scans, digest.campaign_senders)
    message = render.build_message(digest, classifications, DIGEST_FROM, DIGEST_TO)

    ses = boto3.client("ses")
    response = ses.send_raw_email(
        Source=DIGEST_FROM,
        Destinations=[DIGEST_TO],
        RawMessage={"Data": message.as_bytes()},
    )
    app.log.info("sent %s to %s", response["MessageId"], DIGEST_TO)

    return {
        "message_id": response["MessageId"],
        "scans": len(digest.scans),
        "ads_dropped": len(digest.dropped_ads),
        "packages": len(digest.packages),
        "hidden": digest.hidden_mail_count,
    }


@app.on_s3_event(bucket=S3_BUCKET, events=["s3:ObjectCreated:*"])
def handle_s3_email(event):
    if event.key in SKIP_KEYS:
        app.log.info("skipping SES setup notification object")
        return

    s3 = boto3.client("s3")
    try:
        raw = s3.get_object(Bucket=event.bucket, Key=event.key)["Body"].read()
    except ClientError as exc:
        # Duplicate S3 notifications are normal; a missing key is not an error.
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            app.log.info("key %s already gone — duplicate event, ignoring", event.key)
            return
        raise

    app.log.info("processing s3://%s/%s (%s bytes)", event.bucket, event.key, len(raw))
    result = process_raw_email(raw)

    # NOTE: the S3 object is deliberately NOT deleted. The 90-day lifecycle rule
    # expires it. Retention is intentional — these accumulate as the regression
    # corpus. news-ai-summary deletes after processing; this project must not.
    return result
