"""Digest + classifications -> a clean, self-contained HTML email.

Email-client constraints drive every choice here (see CLAUDE.md gotchas):
  - inline `style=` attributes only; Gmail strips <style> blocks unreliably
  - table-based layout; no flexbox/grid
  - scans referenced as `cid:` and attached inline, so nothing is hosted anywhere
    and Gmail never shows a "load remote images" prompt
"""

from __future__ import annotations

import email.policy
import re
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

BG = "#f4f5f7"
CARD = "#ffffff"
INK = "#1c1e21"
MUTED = "#6b7280"
BORDER = "#e5e7eb"
ACCENT = "#1a4d8f"
WARN_BG = "#fff8e6"
WARN_BORDER = "#f0c36d"
WARN_INK = "#7a5b12"

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _date_label(digest) -> str:
    if digest.digest_date:
        return digest.digest_date.strftime("%A, %B %-d")
    return digest.date_text or "Today"


def _short_date(digest) -> str:
    if digest.digest_date:
        return digest.digest_date.strftime("%a, %b %-d")
    return digest.date_text or "today"


def subject_for(digest) -> str:
    bits = []
    if digest.announced_mail:
        bits.append(f"{digest.announced_mail} mailpiece{'s' if digest.announced_mail != 1 else ''}")
    if digest.announced_packages:
        bits.append(f"{digest.announced_packages} package{'s' if digest.announced_packages != 1 else ''}")
    tail = ", ".join(bits) if bits else "nothing expected"
    return f"Mail for {_short_date(digest)} — {tail}"


TRACKING_URL = "https://tools.usps.com/go/TrackConfirmAction?tLabels={}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _chip(text: str, fg: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;margin:0 6px 0 0;'
        f'font-size:11px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;'
        f'color:{fg};background:{bg};border-radius:10px;">{escape(text)}</span>'
    )


def _section_title(text: str) -> str:
    return (
        f'<tr><td style="padding:26px 24px 8px 24px;font:600 12px {FONT};'
        f'letter-spacing:.08em;text-transform:uppercase;color:{MUTED};">{escape(text)}</td></tr>'
    )


def _package_row(pkg) -> str:
    sender = escape(pkg.sender or "Unknown sender")
    lines = [
        f'<div style="font:600 15px {FONT};color:{INK};">{sender}</div>'
    ]
    meta = []
    if pkg.status:
        meta.append(escape(pkg.status))
    if pkg.eta:
        meta.append(escape(pkg.eta))
    if meta:
        lines.append(
            f'<div style="font:400 13px {FONT};color:{ACCENT};margin-top:3px;">'
            f'{" · ".join(meta)}</div>'
        )
    if pkg.tracking:
        lines.append(
            f'<div style="margin-top:4px;"><a href="{TRACKING_URL.format(escape(pkg.tracking))}" '
            f'style="font:400 12px ui-monospace,SFMono-Regular,Menlo,monospace;'
            f'color:{MUTED};text-decoration:none;border-bottom:1px dotted {BORDER};">'
            f'{escape(pkg.tracking)}</a></div>'
        )
    return (
        f'<tr><td style="padding:10px 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="padding:12px 14px;background:{CARD};border:1px solid {BORDER};'
        f'border-radius:8px;">{"".join(lines)}</td></tr></table></td></tr>'
    )


def _scan_block(scan, cls) -> str:
    action = _chip("action needed", "#7c2d12", "#ffedd5") if cls.actionable else ""
    # USPS's own FROM: label wins over anything vision inferred — it is stated
    # fact from the source email, not a guess off a grayscale envelope.
    sender = escape(getattr(scan, "listed_sender", None) or cls.sender or "Unknown sender")

    # Sender and type read as stacked headings above the scan rather than as a
    # small chip — the two facts you want at a glance are "who" and "what kind".
    # Suppressed entirely when the type is unknown: a giant "UNKNOWN" heading is
    # louder than the information it carries.
    type_heading = ""
    if cls.type_label:
        color = "#7f1d1d" if cls.is_important else (MUTED if cls.is_junk else ACCENT)
        type_heading = (
            f'<div style="font:700 12px {FONT};letter-spacing:.09em;'
            f'text-transform:uppercase;color:{color};margin-bottom:3px;">'
            f'{escape(cls.type_label)}</div>'
        )
    action_row = f'<div style="margin-top:8px;">{action}</div>' if action else ""

    summary = ""
    if cls.summary:
        summary = (
            f'<div style="font:400 13px {FONT};color:{MUTED};margin-top:4px;">'
            f'{escape(cls.summary)}</div>'
        )
    recipient = ""
    if cls.recipient:
        recipient = (
            f'<div style="font:400 12px {FONT};color:{MUTED};margin-top:2px;">'
            f'To: {escape(cls.recipient)}</div>'
        )

    return (
        f'<tr><td style="padding:10px 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{CARD};border:1px solid {BORDER};border-radius:8px;">'
        f'<tr><td style="padding:14px 14px 10px 14px;">'
        f'{type_heading}'
        f'<div style="font:700 19px {FONT};color:{INK};line-height:1.25;">{sender}</div>'
        f'{summary}{recipient}{action_row}'
        f'</td></tr>'
        f'<tr><td style="padding:0 14px 14px 14px;">'
        f'<img src="cid:{scan.cid}" alt="Scan of mail from {sender}" width="100%" '
        f'style="display:block;width:100%;max-width:100%;height:auto;'
        f'border:1px solid {BORDER};border-radius:6px;"></td></tr>'
        f'</table></td></tr>'
    )


def _hidden_notice(digest) -> str:
    """Surface ad displacement rather than silently hiding it.

    If we just dropped the ads, a day where USPS replaced every scan would render
    as an empty digest and look like a parser bug. Naming the advertisers makes
    the loss legible.
    """
    n = digest.hidden_mail_count
    if n <= 0:
        return ""
    who = ""
    # Exact now: only senders whose scan we never received. Listing every
    # campaign wrongly implicated the sender that DID supply the scan shown above.
    missing = digest.senders_without_scans
    if missing:
        names = ", ".join(escape(s) for s in missing)
        who = f' Replaced by advertising from: <strong>{names}</strong>.'
    piece = "mailpiece" if n == 1 else "mailpieces"
    return (
        f'<tr><td style="padding:10px 24px 4px 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="padding:12px 14px;background:{WARN_BG};border:1px solid {WARN_BORDER};'
        f'border-radius:8px;font:400 13px {FONT};color:{WARN_INK};">'
        f'USPS did not provide a scan for <strong>{n} {piece}</strong>.{who}'
        f'</td></tr></table></td></tr>'
    )


def build_html(digest, classifications) -> str:
    pairs = sorted(zip(digest.scans, classifications), key=lambda p: p[1].sort_key)

    rows = [
        f'<tr><td style="padding:24px 24px 0 24px;">'
        f'<div style="font:700 24px {FONT};color:{INK};">{escape(_date_label(digest))}</div>'
        f'<div style="font:400 14px {FONT};color:{MUTED};margin-top:4px;">'
        f'{_plural(digest.announced_mail, "mailpiece")} · '
        f'{_plural(digest.announced_packages, "package")}</div>'
        f'</td></tr>'
    ]

    if digest.packages:
        rows.append(_section_title("Packages"))
        rows.extend(_package_row(p) for p in digest.packages)

    if pairs:
        rows.append(_section_title("Mail"))
        rows.extend(_scan_block(s, c) for s, c in pairs)
    elif digest.announced_mail:
        rows.append(_section_title("Mail"))

    rows.append(_hidden_notice(digest))

    if not digest.packages and not pairs and not digest.announced_mail:
        rows.append(
            f'<tr><td style="padding:20px 24px;font:400 15px {FONT};color:{MUTED};">'
            f'Nothing expected today.</td></tr>'
        )

    footer_bits = []
    if digest.dropped_ads:
        n = len(digest.dropped_ads)
        footer_bits.append(f"{n} advertisement image{'s' if n != 1 else ''} removed")
    footer_bits.append("Rebuilt from the USPS Informed Delivery digest")
    rows.append(
        f'<tr><td style="padding:22px 24px 26px 24px;border-top:1px solid {BORDER};'
        f'font:400 12px {FONT};color:{MUTED};">{escape(" · ".join(footer_bits))}</td></tr>'
    )

    return (
        '<!doctype html><html lang="en" dir="ltr"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(subject_for(digest))}</title></head>'
        f'<body style="margin:0;padding:0;background:{BG};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{BG};padding:20px 0;"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:600px;max-width:100%;background:{BG};border-radius:12px;">'
        f'{"".join(rows)}'
        '</table></td></tr></table></body></html>'
    )


def build_text(digest, classifications) -> str:
    out = [_date_label(digest), ""]
    out.append(f"{digest.announced_mail} mailpiece(s), {digest.announced_packages} package(s)")
    out.append("")

    if digest.packages:
        out.append("PACKAGES")
        for p in digest.packages:
            meta = " · ".join(x for x in (p.status, p.eta) if x)
            out.append(f"  - {p.sender or 'Unknown sender'}" + (f" ({meta})" if meta else ""))
            if p.tracking:
                out.append(f"    {p.tracking}")
        out.append("")

    pairs = sorted(zip(digest.scans, classifications), key=lambda p: p[1].sort_key)
    if pairs:
        out.append("MAIL")
        for _, c in pairs:
            flag = " [action needed]" if c.actionable else ""
            out.append(f"  - {c.sender} ({c.mail_type}){flag}")
            if c.summary:
                out.append(f"    {c.summary}")
        out.append("")

    if digest.hidden_mail_count > 0:
        who = ", ".join(digest.senders_without_scans)
        line = f"USPS did not provide a scan for {digest.hidden_mail_count} mailpiece(s)."
        if who:
            line += f" Replaced by advertising from: {who}."
        out.append(line)
        out.append("")

    if digest.dropped_ads:
        out.append(f"{len(digest.dropped_ads)} advertisement image(s) removed.")
    return "\n".join(out)


def build_message(digest, classifications, sender: str, recipient: str) -> MIMEMultipart:
    # policy=SMTP VALIDATES header assignment: any CR/LF in a header value raises
    # instead of silently emitting a forged header. That permanently forecloses
    # the injection class that `parse._safe_filename` fixes by hand — including
    # future headers nobody has thought of yet.
    root = MIMEMultipart("related", policy=email.policy.SMTP)
    root["Subject"] = subject_for(digest)
    root["From"] = sender
    root["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(build_text(digest, classifications), "plain", "utf-8"))
    alt.attach(MIMEText(build_html(digest, classifications), "html", "utf-8"))
    root.attach(alt)

    for scan in digest.scans:
        # Both of these are spliced into MIME headers, and both originate in the
        # source email. parse._safe_filename() already strips CR/LF, but the
        # subtype comes straight off a malformed Content-Type; validate it here
        # so a crafted part can't forge headers on our outbound message.
        subtype = scan.content_type.split("/", 1)[-1]
        if not re.fullmatch(r"[a-z0-9.+-]{1,32}", subtype or ""):
            subtype = "jpeg"
        img = MIMEImage(scan.data, _subtype=subtype)
        img.add_header("Content-ID", f"<{scan.cid}>")
        img.add_header("Content-Disposition", "inline", filename=scan.filename)
        root.attach(img)

    return root
