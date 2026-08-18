"""USPS Informed Delivery cleaner — S3-triggered Lambda.

forward -> SES (SES_RECIPIENT) -> S3 -> here -> clean email -> DIGEST_TO

All addresses/bucket names come from the environment (.chalice/config.json in
Lambda, .env locally). Nothing environment-specific is hardcoded.

Output is EMAIL ONLY. There is deliberately no output bucket and no web surface;
see CLAUDE.md "CRITICAL CONSTRAINT". Do not add one.
"""

import email
import email.policy
import email.utils
import logging

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

# Anyone who learns SES_RECIPIENT can drop a fully attacker-authored "digest"
# into the bucket, and it would be parsed and mailed onward from DIGEST_FROM —
# a trusted, unattended email. Escaping stops HTML/header injection, but not a
# forged sender name over an arbitrary full-width image. So we allow-list the
# address(es) that forward to us.
#
# Defaults to DIGEST_TO: the common setup forwards from the same mailbox the
# clean digest comes back to, so the safe value needs no extra configuration.
# Set ALLOWED_FORWARDERS when the forwarding address differs. Comma-separated;
# an entry is either a full address (matched exactly) or a bare "@example.tld"
# (matched against the domain, including its subdomains). "*" disables the
# check entirely. Deliberately NOT a substring match — "@example.tld" as a
# substring would also accept "attacker@example.tld.evil.example".
ALLOWED_FORWARDERS = {
    a.strip().lower()
    for a in (config.get("ALLOWED_FORWARDERS") or DIGEST_TO).split(",")
    if a.strip()
}
if "*" in ALLOWED_FORWARDERS:
    ALLOWED_FORWARDERS = set()

# Reject before parsing rather than risk OOM on a hostile body.
MAX_RAW_BYTES = 15 * 1024 * 1024


def _forwarder(msg) -> str:
    """The address that forwarded this to us, normalised for allow-list matching.

    Any `+tag` is dropped, because Gmail's auto-forward filter does not send as
    you@gmail.com — it rewrites the envelope sender to
    `you+caf_=local=domain.tld@gmail.com`. Matching the untagged address lets one
    allow-list entry cover both hand- and auto-forwarded mail. Note the tag sits
    on the FORWARDING account, which need not be DIGEST_TO: forwarding from
    you@yourdomain.tld into a Gmail inbox still needs an explicit entry.
    """
    header = msg.get("Return-Path") or msg.get("From") or ""
    address = email.utils.parseaddr(str(header))[1].lower().strip()
    local, at, domain = address.rpartition("@")
    if at and "+" in local:
        address = f"{local.split('+', 1)[0]}@{domain}"
    return address


def _is_allowed(address: str) -> bool:
    """Exact address match, or domain match for a bare `@example.tld` entry."""
    domain = address.rpartition("@")[2]
    for entry in ALLOWED_FORWARDERS:
        if entry.startswith("@"):
            suffix = entry[1:]
            if domain == suffix or domain.endswith("." + suffix):
                return True
        elif address == entry:
            return True
    return False


def _accept(raw: bytes) -> bool:
    """Cheap authenticity gate. Never raises — a raise means an S3 retry."""
    if len(raw) > MAX_RAW_BYTES:
        app.log.warning("rejecting: %s bytes exceeds cap", len(raw))
        return False
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        app.log.warning("rejecting: unparseable message")
        return False
    # Fail OPEN on a missing verdict header so local fixtures still work; SES
    # always stamps these on mail it receives.
    for header in ("X-SES-Spam-Verdict", "X-SES-Virus-Verdict"):
        if (msg.get(header) or "PASS").upper() != "PASS":
            app.log.warning("rejecting: %s=%s", header, msg.get(header))
            return False
    if ALLOWED_FORWARDERS:
        # SPF/DKIM here attest to the FORWARDER's domain, not USPS's, since the
        # mail reaches us forwarded — so the forwarder allow-list is the real
        # identity check.
        forwarder = _forwarder(msg)
        if not _is_allowed(forwarder):
            app.log.warning("rejecting: unrecognised forwarder %r", forwarder)
            return False
    return True


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
    # Match on the basename so a prefixed key still matches.
    if event.key.rsplit("/", 1)[-1] in SKIP_KEYS:
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
    if not _accept(raw):
        return {"rejected": True}          # return, don't raise — a raise retries
    result = process_raw_email(raw)

    # NOTE: the S3 object is deliberately NOT deleted. The 90-day lifecycle rule
    # expires it. Retention is intentional — these accumulate as the regression
    # corpus. news-ai-summary deletes after processing; this project must not.
    return result
