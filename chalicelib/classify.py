"""Vision classification of real envelope scans.

Only ever runs on scans that survived the ad filter. The scan is the ONLY place a
real mailpiece's sender exists — campaign pieces put `FROM: x` in the body text,
real scans put nothing, so this is where the sender comes from.

OpenAI Responses API (NOT chat.completions). Image content type is `input_image`.
Strict JSON schema requires additionalProperties:false and every property listed
in `required`.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")

# Ordering drives the rendered layout: actionable mail first, junk last.
TYPE_ORDER = [
    "bill",
    "statement",
    "medical",
    "insurance",
    "financial",
    "government",
    "legal",
    "personal",
    "package_notice",
    "retail_promo",
    "catalog",
    "credit_offer",
    "political",
    "junk",
    "unknown",
]
IMPORTANT_TYPES = {
    "bill", "statement", "medical", "insurance", "financial",
    "government", "legal", "personal",
}
JUNK_TYPES = {"credit_offer", "political", "catalog", "retail_promo", "junk"}

# `actionable` is DERIVED, never asked of the model. When it was a schema field
# it flipped True/False across runs on one and the same scan — a
# free-floating opinion with nothing in the email to anchor it. Mail that needs
# a response is a property of its type, so deriving it makes the chip stable.
ACTIONABLE_TYPES = {"bill", "statement", "medical", "insurance", "financial",
                    "government", "legal"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sender": {
            "type": "string",
            "description": "Organization or person the mail is from, as printed. 'Unknown' if illegible.",
        },
        "recipient": {
            "type": "string",
            "description": "Addressee as printed. Empty string if not legible.",
        },
        "sender_source": {
            "type": "string",
            "enum": ["return_address", "logo", "candidate_match", "address_only", "unknown"],
            "description": (
                "Where the sender came from. Forces explicit consideration of the "
                "candidate list — without it the model silently ignores candidates "
                "and falls back to the bare address."
            ),
        },
        "mail_type": {"type": "string", "enum": TYPE_ORDER},
        "summary": {
            "type": "string",
            "description": "One short phrase describing the piece. No speculation beyond what is visible.",
        },
    },
    "required": ["sender", "recipient", "sender_source", "mail_type", "summary"],
}

PROMPT = (
    "This is a USPS Informed Delivery grayscale scan of the outside of a piece of mail. "
    "Identify the sender from the RETURN ADDRESS or company logo.\n\n"
    "Critically: do NOT treat the postage permit imprint as the sender. The block in the "
    "upper right reading like 'PRESORTED / FIRST-CLASS MAIL / US POSTAGE PAID / <name>' "
    "names whoever paid the postage — usually a bulk mailing house, not the actual sender. "
    "Ignore that name unless it also appears as the return address.\n\n"
    "Much bulk mail is deliberately anonymous: a bare PO Box with no company name. If the "
    "return address has no organization name, report the address itself as the sender "
    "(e.g. 'P.O. Box 12345, Springfield, IL') rather than substituting the permit-imprint name.\n\n"
    "Classify what kind of mail it is. mail_type is the field that matters most — be "
    "decisive about it. Judge only from what is visible: if the scan is too faint or "
    "cropped to tell, use sender 'Unknown' and mail_type 'unknown' rather than guessing. "
    "Do not invent account numbers or amounts."
)


@dataclass
class Classification:
    sender: str = "Unknown"
    recipient: str = ""
    sender_source: str = "unknown"
    mail_type: str = "unknown"
    summary: str = ""

    @property
    def actionable(self) -> bool:
        """Derived from mail_type — see ACTIONABLE_TYPES for why."""
        return self.mail_type in ACTIONABLE_TYPES

    @property
    def type_label(self) -> str:
        """Human-facing type heading; empty when we genuinely don't know."""
        return "" if self.mail_type == "unknown" else self.mail_type.replace("_", " ")

    @property
    def is_important(self) -> bool:
        return self.mail_type in IMPORTANT_TYPES

    @property
    def is_junk(self) -> bool:
        return self.mail_type in JUNK_TYPES

    @property
    def sort_key(self) -> tuple[int, int]:
        try:
            rank = TYPE_ORDER.index(self.mail_type)
        except ValueError:
            rank = len(TYPE_ORDER)
        return (0 if self.actionable else 1, rank)


def _extract_json(response) -> str | None:
    """Responses API may emit a `reasoning` item before the `message` item."""
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for chunk in getattr(item, "content", []) or []:
            if text := getattr(chunk, "text", None):
                return text
    return getattr(response, "output_text", None)


def _candidate_hint(candidates) -> str:
    """Ground the guess in senders the digest itself names.

    The MAIL band lists `FROM:` for campaign pieces, and a campaign piece can
    also carry a real scan (observed 8/6: one financial piece had both). Offering those
    names lets an anonymous PO-Box envelope resolve to a real sender using
    evidence from the same email — not model recall.

    Phrased as an explicit decision procedure. A softer "if it plausibly belongs
    to one of them" wording was tried first and proved UNSTABLE — identical input
    returned the bank's name on one call and the bare PO Box on the next.
    """
    if not candidates:
        return ""
    names = "\n".join(f"  - {c}" for c in dict.fromkeys(candidates))
    return (
        f"\n\nCANDIDATE SENDERS. USPS listed these senders for mailpieces in this same "
        f"digest:\n{names}\n"
        "Work through this explicitly before answering:\n"
        "1. Read the return address on the scan.\n"
        "2. If it names a company, use that name; set sender_source='return_address'.\n"
        "3. If it is only a PO Box or street address with NO company name, check whether "
        "that address is a known mailing address for any candidate above. A large bank's "
        "PO Box in its home city is a strong match. If so, use the CANDIDATE'S name as the "
        "sender and set sender_source='candidate_match'.\n"
        "4. Only if no candidate matches, report the address itself and set "
        "sender_source='address_only'.\n"
        "Do not force a match — but do not skip step 3 either."
    )


def classify_scan(client, scan, candidates=None) -> Classification:
    """Classify one scan. Never raises — an unclassified scan still gets shown."""
    import json

    listed = getattr(scan, "listed_sender", None)
    if listed:
        # USPS states the sender in the markup (parse.map_cid_senders). Don't
        # re-derive a known fact, and never let a permit imprint override it.
        hint = (
            f"\n\nUSPS labels this mailpiece as FROM: {listed}. Treat that as the "
            f"authoritative sender: return it verbatim with "
            f"sender_source='return_address'. Spend your effort on mail_type, "
            f"actionable and summary instead."
        )
    else:
        hint = _candidate_hint(candidates)

    b64 = base64.b64encode(scan.data).decode("ascii")
    try:
        response = client.responses.create(
            model=MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": PROMPT + hint},
                        {
                            "type": "input_image",
                            "image_url": f"data:{scan.content_type};base64,{b64}",
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "mailpiece",
                    "schema": SCHEMA,
                    "strict": True,
                }
            },
            # Default effort left the candidate step skipped intermittently.
            reasoning={"effort": "medium"},
        )
        payload = _extract_json(response)
        if not payload:
            LOG.warning("no message content for %s", scan.filename)
            return Classification()
        return Classification(**json.loads(payload))
    except Exception:
        # A classification failure must not cost the user their mail image.
        LOG.exception("classification failed for %s", scan.filename)
        return Classification()


def classify_all(scans, candidates=None) -> list[Classification]:
    """Classify every scan; returns placeholders if OpenAI is unavailable.

    Per-scan calls rather than one batched call: the corpus maxes out at 3 scans
    per digest, and per-scan keeps image->result mapping unambiguous.
    """
    if not scans:
        return []
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        LOG.warning("OPENAI_API_KEY unset — skipping classification")
        return [Classification() for _ in scans]

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    return [classify_scan(client, s, candidates) for s in scans]
