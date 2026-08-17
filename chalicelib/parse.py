"""Informed Delivery MIME -> structured Digest.

Deliberately parses the TEXT RENDERING of the HTML body, not the HTML structure.
The digest HTML is deeply-nested table soup that USPS rewrites freely, but the
visible labels ("Expected Today", "FROM:", "N item(s)") were stable across the
entire 7/10-8/7 2026 corpus and even the 2023-era format. Text is the stable
contract here; CSS selectors are not.

Ad-stripping is a filename deny-list, never an LLM judgment: USPS "interactive
campaign" creative is always `mailer-<id>.jpg` / `content-<id>.jpg`. Everything
else that is an image is a real envelope scan. Do NOT key on the `-068` suffix —
the corpus also contains -066, -067 and (2023) -058.
"""

from __future__ import annotations

import email
import email.policy
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup, NavigableString

AD_PREFIXES = ("mailer-", "content-")

# Bounds on what one email can cost us. classify_all makes ONE paid vision call
# per surviving scan, and a Lambda timeout mid-loop is retried twice by S3 — so
# an email stuffed with image parts is a 3x cost-amplification lever, and a huge
# body can OOM the 512MB function. The real corpus peaks at 6 scans.
MAX_SCANS = 12
MAX_SCAN_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

MAIL_SECTIONS = ("Expected Today", "Expected Tomorrow", "Expected This Week")
PACKAGE_SECTIONS = (
    "Expected Today",
    "Expected Tomorrow",
    "Expected 1-2 Days",
    "Awaiting From Sender",
    "Outbound",
    "Delivered",
)
FOOTER_MARKERS = (
    "Refer via Email",
    "You may have more mail or packages",
    "*These images represent mail pieces",
)
EMPTY_MARKERS = ("No packages are available to display.", "No mail is available to display.")

COUNTS_RE = re.compile(
    r"You have\s+(\d+)\s+mailpiece\(s\)\s+and\s+(\d+)\s+inbound package\(s\)", re.I
)
DATE_RE = re.compile(
    r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})"
)
SUBJECT_DATE_RE = re.compile(r"Daily Digest for\s+\w{3},\s*(\d{1,2})/(\d{1,2})")
# USPS tracking numbers in this corpus are 20-22 digits. Bounded to avoid
# swallowing the long numeric IMb strings that appear elsewhere in the body.
TRACKING_RE = re.compile(r"\b(\d{20,26})\b", re.ASCII)
ETA_RE = re.compile(r"Estimated Delivery on:\s*(.+?)\s*$", re.I)


@dataclass
class Scan:
    """A real envelope scan — the thing the user actually wants to see."""

    filename: str
    content_type: str
    data: bytes
    cid: str = ""
    listed_sender: str | None = None  # USPS's own FROM: label for this scan, if any


@dataclass
class Package:
    sender: str | None = None
    tracking: str | None = None
    status: str | None = None
    eta: str | None = None


@dataclass
class Digest:
    subject: str = ""
    sent_at: datetime | None = None
    digest_date: date | None = None
    date_text: str = ""
    announced_mail: int = 0
    announced_packages: int = 0
    scans: list[Scan] = field(default_factory=list)
    campaign_senders: list[str] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    dropped_ads: list[str] = field(default_factory=list)

    @property
    def hidden_mail_count(self) -> int:
        """Mailpieces announced but with no scan we can show.

        On days USPS sells the slot this is > 0 — that is the whole point of the
        project, so it is surfaced in the rendered email rather than swallowed.
        """
        return max(0, self.announced_mail - len(self.scans))

    @property
    def senders_without_scans(self) -> list[str]:
        """Campaign senders whose real scan we never got.

        Now exact, because `map_cid_senders` tells us which sender each surviving
        scan belongs to. Previously the renderer listed ALL campaign senders,
        which wrongly implicated the one sender that DID supply a scan.
        """
        have = {s.listed_sender for s in self.scans if s.listed_sender}
        return [s for s in dict.fromkeys(self.campaign_senders) if s not in have]


def _clean(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(" ", " ").strip()


def is_ad_attachment(filename: str) -> bool:
    base = filename.rsplit("/", 1)[-1].lower()
    return base.startswith(AD_PREFIXES)


KNOWN_LABELS = tuple(dict.fromkeys(MAIL_SECTIONS + PACKAGE_SECTIONS))


def _rejoin_fragments(lines: list[str]) -> list[str]:
    """Re-join labels that USPS splits across elements.

    Real digests emit 'Expected' and 'Today' as separate nodes, and '2' /
    'item(s)' likewise, so naive line matching silently loses every section
    header. Found only when the first genuinely-forwarded email was parsed —
    hand-built fixtures had wrapped each label in a single element.
    """
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        for k in (4, 3, 2):  # longest first: "Expected This Week" before "Expected"
            if i + k <= n and " ".join(lines[i : i + k]) in KNOWN_LABELS:
                out.append(" ".join(lines[i : i + k]))
                i += k
                break
        else:
            if lines[i].isdigit() and i + 1 < n and lines[i + 1].startswith("item(s)"):
                out.append(f"{lines[i]} {lines[i + 1]}")
                i += 2
            else:
                out.append(lines[i])
                i += 1
    return out


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    raw = soup.get_text("\n")
    return _rejoin_fragments([c for c in (_clean(ln) for ln in raw.split("\n")) if c])


#: A mailpiece occupies its own `div`, whose id names the kind of piece:
#: `mail-campaign` (targeted campaign), `sat-campaign` (saturation ride-along),
#: `sg-mailpiece` (plain piece, no advertiser). The band wrappers around them
#: are `td`s ending in `-div-id`, so matching a `div` id by suffix picks out
#: exactly the per-piece blocks and never the band.
MAILPIECE_BLOCK_SUFFIXES = ("campaign", "mailpiece")


def is_mailpiece_block(tag: object) -> bool:
    return (
        getattr(tag, "name", None) == "div"
        and (tag.get("id") or "").strip().lower().endswith(MAILPIECE_BLOCK_SUFFIXES)
    )


def _sender_image_events(node):
    """Yield ('from', name) / ('img', cid) in document order beneath `node`."""
    awaiting_name = False
    for el in node.descendants:
        if isinstance(el, NavigableString):
            text = _clean(str(el))
            if not text:
                continue
            if text.startswith("FROM:"):
                rest = text[len("FROM:") :].strip()
                if rest:
                    awaiting_name = False
                    yield "from", rest
                else:
                    awaiting_name = True  # name lives in a sibling element
            elif awaiting_name:
                awaiting_name = False
                yield "from", text
        elif getattr(el, "name", None) == "img":
            src = (el.get("src") or "").strip()
            if src.lower().startswith("cid:"):
                yield "img", src[4:].strip().strip("<>")


def _map_by_document_order(soup) -> dict[str, str]:
    """Legacy fallback: nearest preceding FROM: wins.

    Only correct when every image really does sit under a FROM: heading. Used
    when the markup carries no recognisable per-piece blocks at all, where a
    positional guess still beats no sender.
    """
    mapping: dict[str, str] = {}
    last_from: str | None = None
    for kind, value in _sender_image_events(soup):
        if kind == "from":
            last_from = value
        elif last_from:
            mapping[value] = last_from
    return mapping


def map_cid_senders(html: str) -> dict[str, str]:
    """Map each inline image's Content-ID to the `FROM:` label of ITS mailpiece.

    A campaign mailpiece that supplies no replacement image falls back to the
    REAL grayscale scan, rendered directly beneath its `FROM:` heading (observed
    8/6: the financial sender's block contains 2989542530-068.jpg). So USPS states the
    sender in the markup — asking the vision model to infer it from an anonymous
    PO Box was guessing at something already known.

    The association is scoped to the enclosing per-piece `div`, NOT to document
    order. A plain `sg-mailpiece` block carries no FROM: at all, so under a
    document-order walk its scan inherited the previous campaign's advertiser —
    so a genuine letter was rendered and emailed under an advertiser's name. An
    image outside every block, or in a block with no FROM:, gets no listed
    sender and falls through to vision.

    It is the one place structural HTML reading beats text parsing, because the
    image->sender association is positional and vanishes in flattened text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    blocks = [
        b
        for b in soup.find_all("div")
        if is_mailpiece_block(b) and not any(is_mailpiece_block(p) for p in b.parents)
    ]
    if not blocks:
        return _map_by_document_order(soup)

    mapping: dict[str, str] = {}
    for block in blocks:
        sender: str | None = None
        cids: list[str] = []
        for kind, value in _sender_image_events(block):
            if kind == "from":
                sender = sender or value
            else:
                cids.append(value)
        if sender:
            mapping.update(dict.fromkeys(cids, sender))
    return mapping


def _truncate_at_footer(lines: list[str]) -> list[str]:
    for i, ln in enumerate(lines):
        if any(ln.startswith(m) for m in FOOTER_MARKERS):
            return lines[:i]
    return lines


def _region(lines: list[str], start_label: str, stop_label: str | None) -> list[str]:
    """Slice the lines belonging to a top-level band (MAIL / PACKAGES)."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln == start_label)
    except StopIteration:
        return []
    end = len(lines)
    if stop_label:
        for i in range(start + 1, len(lines)):
            if lines[i] == stop_label:
                end = i
                break
    return lines[start + 1 : end]


def _item_blocks(region: list[str], sections: tuple[str, ...]) -> list[tuple[str | None, list[str]]]:
    """Split a region into (section, block) pairs, one per `FROM:` item."""
    starts: list[int] = [i for i, ln in enumerate(region) if ln.startswith("FROM:")]
    if not starts:
        return []
    boundaries = set(starts) | {i for i, ln in enumerate(region) if ln in sections}

    blocks: list[tuple[str | None, list[str]]] = []
    for idx in starts:
        section = None
        for j in range(idx, -1, -1):
            if region[j] in sections:
                section = region[j]
                break
        end = len(region)
        for j in range(idx + 1, len(region)):
            if j in boundaries:
                end = j
                break
        blocks.append((section, region[idx:end]))
    return blocks


def _sender_from_block(block: list[str]) -> str | None:
    head = block[0]
    rest = head[len("FROM:") :].strip()
    if rest:
        return rest
    return block[1] if len(block) > 1 else None


def parse_mail_region(lines: list[str]) -> list[str]:
    """Campaign senders only.

    Real scans carry NO `FROM:` text — the sender exists solely inside the JPEG,
    which is exactly why the vision step earns its place. A `FROM:` in the MAIL
    band therefore identifies an advertiser that displaced a scan.
    """
    region = _region(lines, "MAIL", "PACKAGES")
    return [s for _, blk in _item_blocks(region, MAIL_SECTIONS) if (s := _sender_from_block(blk))]


def parse_package_region(lines: list[str]) -> list[Package]:
    region = _region(lines, "PACKAGES", None)
    packages: list[Package] = []
    for section, block in _item_blocks(region, PACKAGE_SECTIONS):
        if any(ln in EMPTY_MARKERS for ln in block):
            continue
        pkg = Package(sender=_sender_from_block(block), status=section)
        for ln in block:
            if pkg.tracking is None and (m := TRACKING_RE.search(ln)):
                pkg.tracking = m.group(1)  # appears twice per item; first wins
            if pkg.eta is None and (m := ETA_RE.search(ln)):
                pkg.eta = m.group(1)
        packages.append(pkg)
    return packages


def _extract_bodies(msg) -> tuple[str, str]:
    html_parts, text_parts = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        if ctype == "text/html":
            html_parts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
        elif ctype == "text/plain":
            text_parts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
    return "\n".join(html_parts), "\n".join(text_parts)


def _safe_filename(name: str) -> str:
    """Strip CR/LF/NUL out of an attachment filename at the boundary.

    `get_filename()` percent-decodes RFC 2231 parameters, so a source part
    carrying `filename*=utf-8''x%0D%0AX-Injected:%20yes` yields a string with a
    real CRLF in it. `MIMEImage.add_header` under compat32 does not validate
    header values, so that CRLF survives into the message SES sends — letting
    whoever wrote the source email inject MIME part headers into our output. The
    dangerous variant is a forged `Content-Type: text/html`, which would render
    attacker-authored HTML inside a digest the reader trusts.

    Sanitizing here rather than at the render site covers every consumer,
    including `is_ad_attachment` and the `map_cid_senders` filename join.
    """
    return re.sub(r"[\x00-\x1f\x7f]", "", name or "")[:120]


def _safe_content_type(ctype: str) -> str:
    """Normalize at the boundary so every consumer inherits the guarantee.

    `get_content_type()` does not sanitize: a malformed source part yields
    `image/jpeg"\tevil: 1"`, which still passes an `image/` prefix check. render.py
    validates before building the MIME header, but classify.py interpolates this
    into a `data:` URL, so the check belongs here rather than at each use site.
    """
    subtype = (ctype or "").split("/", 1)[-1]
    return f"image/{subtype}" if re.fullmatch(r"[a-z0-9.+-]{1,32}", subtype) else "image/jpeg"


def _extract_images(msg) -> tuple[list[Scan], list[str]]:
    scans, dropped, total = [], [], 0
    for part in msg.walk():
        ctype = part.get_content_type()
        if not ctype.startswith("image/"):
            continue
        name = _safe_filename(
            part.get_filename() or (part.get("Content-ID", "") or "").strip("<>")
        )
        if not name:
            continue
        if is_ad_attachment(name):
            dropped.append(name)
            continue
        if len(scans) >= MAX_SCANS:
            continue
        data = part.get_payload(decode=True)
        if not data or len(data) > MAX_SCAN_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            continue
        total += len(data)
        scans.append(Scan(filename=name, content_type=_safe_content_type(ctype), data=data))
    for i, s in enumerate(scans):
        s.cid = f"scan{i}@usps-digest"
    return scans, dropped


def _parse_date(lines: list[str], subject: str, sent_at: datetime | None):
    flat = " ".join(lines)
    if m := DATE_RE.search(flat):
        day, month_name, year = m.group(1), m.group(2), m.group(3)
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(f"{day} {month_name} {year}", fmt).date(), m.group(0)
            except ValueError:
                continue
    if m := SUBJECT_DATE_RE.search(subject):
        year = sent_at.year if sent_at else date.today().year
        try:
            return date(year, int(m.group(1)), int(m.group(2))), m.group(0)
        except ValueError:
            pass
    return (sent_at.date() if sent_at else None), ""


def parse_digest(raw: bytes) -> Digest:
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    sent_at = None
    if raw_date := msg.get("Date"):
        try:
            sent_at = email.utils.parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            pass

    subject = str(msg.get("Subject") or "")
    html, text = _extract_bodies(msg)
    if html:
        lines = html_to_lines(html)
    else:
        lines = _rejoin_fragments([_clean(l) for l in text.split("\n") if _clean(l)])
    lines = _truncate_at_footer(lines)

    scans, dropped = _extract_images(msg)
    if html:
        cid_senders = map_cid_senders(html)
        for scan in scans:
            scan.listed_sender = cid_senders.get(scan.filename)
    digest_date, date_text = _parse_date(lines, subject, sent_at)

    announced_mail = announced_packages = 0
    if m := COUNTS_RE.search(" ".join(lines)):
        announced_mail, announced_packages = int(m.group(1)), int(m.group(2))

    return Digest(
        subject=subject,
        sent_at=sent_at,
        digest_date=digest_date,
        date_text=date_text,
        announced_mail=announced_mail,
        announced_packages=announced_packages,
        scans=scans,
        campaign_senders=parse_mail_region(lines),
        packages=parse_package_region(lines),
        dropped_ads=dropped,
    )
