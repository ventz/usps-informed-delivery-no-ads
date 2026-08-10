#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build synthetic Informed Delivery .eml fixtures in ../samples/.

WHY SYNTHETIC: raw MIME can't be pulled over the Superhuman MCP (it returns
rendered content, not bytes), and nothing has been forwarded to SES yet. So these
fixtures reproduce the *text rendering* and *attachment filenames* of the real
8/7, 8/5, 8/2 and 7/10 digests verbatim — which is exactly what parse.py consumes.

LIMITS — these do NOT prove:
  - that real USPS HTML flattens to this text (it did in the corpus, but the
    fixture wraps each line in a <div> rather than USPS's real table soup)
  - anything about Gmail's forward re-encoding
  - the vision path (image bytes are placeholders, not real scans)
Re-validate against a genuinely forwarded email before trusting the Lambda.

    uv run make_fixture.py
"""

import os
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"
SENDER = '"USPS Informed Delivery" <USPSInformeddelivery@email.informeddelivery.usps.com>'

FOOTER = [
    "Refer via Email",
    "Refer via Text",
    "You may have more mail or packages than are shown in your Daily Digest. To check, go to your Dashboard.",
    "*These images represent mail pieces that are sorted on USPS® automated equipment.",
    "If you no longer wish to receive daily email notifications, unsubscribe here.",
    "Copyright © 2026 United States Postal Service®. All Rights Reserved.",
]


def header_lines(weekday, day, month, year, mail, pkgs):
    return [
        "Informed Delivery",
        "COMING TO YOU SOON",
        "Hi, Ventz!",
        f"You have {mail} mailpiece(s) and {pkgs} inbound package(s) arriving soon.",
        weekday, str(day), month, str(year),
        str(mail), "Mailpiece(s)", str(pkgs), "Package(s)",
    ]


def frag(label, count):
    """Emit a section header the way real USPS HTML does.

    Observed in the genuinely-forwarded 8/6 digest: "Expected Today" arrives as
    two separate text nodes and the count as "2" + "item(s)", while "Expected
    This Week" arrives whole. Fixtures MUST reproduce this — the original
    one-node-per-label fixtures hid a parser bug that broke every section header.
    """
    parts = ["Expected", "Today"] if label == "Expected Today" else [label]
    return parts + [str(count), "item(s)"]


def empty(section):
    return frag(section, 0) + ["No packages are available to display."]


# --- Real observed digests -------------------------------------------------
# Text below mirrors what was read over MCP on 2026-08-07.

FIXTURES = {
    # 2 mailpieces: 1 real scan + 1 displaced by a Select Home Warranty campaign.
    "2026-08-07": dict(
        subject="Your Daily Digest for Fri, 8/7 is ready to view",
        date="Fri, 7 Aug 2026 11:44:42 +0000",
        lines=header_lines("Friday", 7, "August", 2026, 2, 1)
        + ["MAIL", "View Dashboard"] + frag("Expected Today", 1)
        + frag("Expected This Week", 1)
        + ["FROM:", "save-select homes", "Learn more about your mail ❯",
           "PACKAGES", "View Dashboard"]
        + empty("Expected Today")
        + frag("Expected 1-2 Days", 1)
        + ["FROM:", "SHIPFUSION INC",
           "9261290335949247070387", "9261290335949247070387",
           "Estimated Delivery on: Saturday, Aug 08"]
        + empty("Awaiting From Sender") + empty("Outbound") + FOOTER,
        attachments=[("2989868880-068.jpg", 31980),
                     ("mailer-1202017988.jpg", 4210),
                     ("content-1202017988.jpg", 35593)],
    ),
    # WORST CASE: 2 mailpieces, ZERO real scans — both slots sold.
    "2026-08-05": dict(
        subject="Your Daily Digest for Wed, 8/5 is ready to view",
        date="Wed, 5 Aug 2026 11:47:04 +0000",
        lines=header_lines("Wednesday", 5, "August", 2026, 2, 1)
        + ["MAIL", "View Dashboard"] + frag("Expected Today", 1)
        + ["FROM:", "Lands' End", "Learn more about your mail ❯"]
        + frag("Expected This Week", 1)
        + ["FROM:", "save-select homes", "Learn more about your mail ❯",
           "PACKAGES", "View Dashboard"]
        + empty("Expected Today") + empty("Expected 1-2 Days")
        + frag("Awaiting From Sender", 1)
        + ["FROM:", "SHIPFUSION INC",
           "9261290335949247070387", "9261290335949247070387"]
        + empty("Outbound") + FOOTER,
        attachments=[("mailer-1201976445.jpg", 3900), ("content-1201976445.jpg", 28110),
                     ("mailer-1202017988.jpg", 4210), ("content-1202017988.jpg", 35593)],
    ),
    # No mailpieces at all -> NO attachment parts whatsoever.
    "2026-08-02": dict(
        subject="Your Daily Digest for Sun, 8/2 is ready to view",
        date="Sun, 2 Aug 2026 11:22:56 +0000",
        lines=header_lines("Sunday", 2, "August", 2026, 0, 2)
        + ["MAIL", "View Dashboard"] + frag("Expected Today", 0)
        + ["PACKAGES", "View Dashboard"] + frag("Expected Today", 2)
        + ["FROM:", "AMAZON", "9361289725264307382388", "9361289725264307382388",
           "Estimated Delivery on: Sunday, Aug 02",
           "FROM:", "EXPRESS SCRIPTS PHARMACY",
           "9300189821800516098273", "9300189821800516098273"]
        + empty("Expected 1-2 Days") + empty("Awaiting From Sender") + empty("Outbound")
        + FOOTER,
        attachments=[],
    ),
    # Best case: 3 mailpieces, 3 real scans, zero ads. Also exercises -066/-067,
    # the suffixes that prove the ad filter must not key on "-068".
    "2026-07-10": dict(
        subject="Your Daily Digest for Fri, 7/10 is ready to view",
        date="Fri, 10 Jul 2026 11:54:42 +0000",
        lines=header_lines("Friday", 10, "July", 2026, 3, 0)
        + ["MAIL", "View Dashboard"] + frag("Expected Today", 3)
        + ["PACKAGES", "View Dashboard"]
        + empty("Expected Today") + empty("Expected 1-2 Days")
        + empty("Awaiting From Sender") + empty("Outbound") + FOOTER,
        attachments=[("2983313701-068.jpg", 30110),
                     ("1006624496-066.jpg", 28740),
                     ("1002338133-067.jpg", 26900)],
    ),
}


def build(name, spec):
    root = MIMEMultipart("related")
    root["Subject"] = spec["subject"]
    root["From"] = SENDER
    root["To"] = "<recipient@example.com>"
    root["Date"] = spec["date"]

    html = (
        '<html><head><meta charset="utf-8"></head><body>'
        + "".join(f"<div>{ln}</div>" for ln in spec["lines"])
        + "</body></html>"
    )
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("\n".join(spec["lines"]), "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)

    for filename, size in spec["attachments"]:
        img = MIMEImage(os.urandom(size), _subtype="jpeg")
        img.add_header("Content-ID", f"<{filename}>")
        img.add_header("Content-Disposition", "inline", filename=filename)
        root.attach(img)

    path = OUT / f"{name}.eml"
    path.write_bytes(root.as_bytes())
    real = sum(1 for f, _ in spec["attachments"]
               if not f.startswith(("mailer-", "content-")))
    ads = len(spec["attachments"]) - real
    print(f"  {path.name:<20} {real} scan(s), {ads} ad file(s)")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"writing fixtures -> {OUT}")
    for name, spec in FIXTURES.items():
        build(name, spec)


if __name__ == "__main__":
    main()
