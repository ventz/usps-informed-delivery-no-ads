"""Tests for the ingestion gate in `app._accept`.

The gate is the only thing standing between "anyone who learns SES_RECIPIENT"
and a fully attacker-authored digest mailed onward from DIGEST_FROM as trusted,
unattended email — so its failure modes are worth pinning:

  - it must not reject mail that Gmail's auto-forward filter re-enveloped as
    `you+caf_=local=domain@gmail.com`. That regression silently dropped the
    2026-08-17 digest in production.
  - it must still reject an unknown forwarder, and a domain entry must not leak
    into a lookalike domain.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build(return_path, *, spam="PASS", virus="PASS"):
    return (
        f"Return-Path: <{return_path}>\r\n"
        f"X-SES-Spam-Verdict: {spam}\r\n"
        f"X-SES-Virus-Verdict: {virus}\r\n"
        "From: USPS <usps@example.invalid>\r\n"
        "Subject: Your Daily Digest\r\n"
        "\r\n"
        "body\r\n"
    ).encode()


def with_from(value):
    """A message with no Return-Path, exercising the From: fallback."""
    return (
        "X-SES-Spam-Verdict: PASS\r\n"
        "X-SES-Virus-Verdict: PASS\r\n"
        f"From: {value}\r\n"
        "\r\n"
        "body\r\n"
    ).encode()


def load_app(monkeypatch, allowed):
    """Re-import app.py with a given ALLOWED_FORWARDERS, since it reads at import."""
    for key, value in {
        "APP_BUCKET_NAME": "bucket",
        "DIGEST_TO": "you@yourmail.tld",
        "DIGEST_FROM": "no-reply@domainViaSES.tld",
        "OPENAI_API_KEY": "sk-test",
    }.items():
        monkeypatch.setenv(key, value)
    # Empty, not deleted: config.load_dotenv() would otherwise read the
    # developer's real .env back in and the "unset" case would test their box.
    monkeypatch.setenv("ALLOWED_FORWARDERS", allowed or "")
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def test_defaults_to_digest_to(monkeypatch):
    """Unset must not mean "accept anything" — the safe default needs no config."""
    app = load_app(monkeypatch, None)
    assert app.ALLOWED_FORWARDERS == {"you@yourmail.tld"}
    assert app._accept(build("you@yourmail.tld"))
    assert not app._accept(build("attacker@evil.example"))


def test_gmail_auto_forward_plus_tag_is_stripped(monkeypatch):
    """The 2026-08-17 production regression: Gmail auto-forward re-envelopes."""
    app = load_app(monkeypatch, None)
    assert app._accept(build("you+caf_=usps=domainviases.tld@yourmail.tld"))


def test_domain_entry_allows_any_sender_on_that_domain(monkeypatch):
    app = load_app(monkeypatch, "youremail@gmail.com,@customdomain.tld")
    assert app._accept(build("youremail@gmail.com"))
    assert app._accept(build("someone+caf_=usps=x.tld@customdomain.tld"))
    assert not app._accept(build("attacker@evil.example"))


def test_domain_entry_does_not_match_lookalike_domain(monkeypatch):
    """A domain entry must match the domain, not appear anywhere inside it.

    The gate used substring matching until 2026-08-18, which accepted exactly
    this. Keep the boundary honest.
    """
    app = load_app(monkeypatch, "@customdomain.tld")
    assert not app._accept(build("attacker@customdomain.tld.evil.example"))


def test_star_disables_the_check(monkeypatch):
    app = load_app(monkeypatch, "*")
    assert app.ALLOWED_FORWARDERS == set()
    assert app._accept(build("anyone@anywhere.example"))


@pytest.mark.parametrize("verdict", ["FAIL", "GRAY", "PROCESSING_FAILED"])
def test_ses_verdicts_still_reject(monkeypatch, verdict):
    app = load_app(monkeypatch, None)
    assert not app._accept(build("you@yourmail.tld", spam=verdict))
    assert not app._accept(build("you@yourmail.tld", virus=verdict))


def test_oversized_body_rejected_before_parsing(monkeypatch):
    app = load_app(monkeypatch, "*")
    assert not app._accept(b"x" * (app.MAX_RAW_BYTES + 1))


def test_domain_entry_matches_subdomains(monkeypatch):
    """The docs promise "including its subdomains" — pin the positive limb."""
    app = load_app(monkeypatch, "@customdomain.tld")
    assert app._accept(build("you@mail.customdomain.tld"))
    assert app._accept(build("you@customdomain.tld"))


def test_explicit_list_replaces_the_digest_to_default(monkeypatch):
    """Setting the key REPLACES the default; it does not extend it.

    Worth pinning because "extend the default" is the intuitive reading, and
    quietly adding DIGEST_TO back would widen the gate without anyone noticing.
    """
    app = load_app(monkeypatch, "@customdomain.tld")
    assert app.ALLOWED_FORWARDERS == {"@customdomain.tld"}
    assert not app._accept(build("you@yourmail.tld"))


def test_missing_return_path_rejected(monkeypatch):
    """The From: fallback was removed; absent Return-Path must fail closed.

    From is wholly attacker-authored, and policy.default normalises a lot into
    a plain address. All of these once resolved to an allowed address through
    the fallback; none may be accepted now.
    """
    app = load_app(monkeypatch, None)
    for value in (
        "you@yourmail.tld",
        '"You, Person" <you@yourmail.tld>',
        "=?utf-8?q?you=40yourmail=2Etld?=",          # RFC 2047, no addr-spec
        "=?utf-8?q?you=2Bx=40yourmail=2Etld?=",      # encoded +tag
        "<@relay.evil.example:you@yourmail.tld>",    # obs-route
        "undisclosed:you@yourmail.tld;",             # group syntax
    ):
        assert not app._accept(with_from(value)), value


def test_ambiguous_return_path_fails_closed(monkeypatch):
    """Where parseaddr can't resolve one address, the result must be rejection."""
    app = load_app(monkeypatch, None)
    assert not app._accept(build("you@yourmail.tld, attacker@evil.example"))
    assert not app._accept(build("not-an-address"))


def test_garbage_body_rejected(monkeypatch):
    """message_from_bytes accepts almost anything; the empty forwarder rejects it."""
    app = load_app(monkeypatch, None)
    assert not app._accept(b"\xff\xfe\x00 not a message at all")


def test_missing_verdict_header_rejected(monkeypatch):
    """Fail CLOSED: absence means the message didn't arrive the way we think."""
    app = load_app(monkeypatch, None)
    for drop in ("X-SES-Spam-Verdict", "X-SES-Virus-Verdict"):
        raw = b"".join(
            line + b"\r\n"
            for line in build("you@yourmail.tld").split(b"\r\n")
            if not line.startswith(drop.encode())
        )
        assert not app._accept(raw), drop


def test_blank_allowed_forwarders_is_a_hard_error(monkeypatch):
    """A value that filters to nothing must NOT silently disable the gate.

    " " and "," are truthy enough to beat the DIGEST_TO default but filter to an
    empty set, which would skip the check with no log line — failing open just
    as invisibly as the 2026-08-17 outage failed closed.
    """
    for value in (" ", ",", ", ,"):
        with pytest.raises(RuntimeError, match="no usable entry"):
            load_app(monkeypatch, value)


def test_star_mixed_with_entries_is_a_hard_error(monkeypatch):
    with pytest.raises(RuntimeError, match='mixes "\\*"'):
        load_app(monkeypatch, "*,you@yourmail.tld")


def test_bare_at_entry_is_a_hard_error(monkeypatch):
    """A lone "@" would match every domain via the endswith(".") limb."""
    with pytest.raises(RuntimeError, match="bare"):
        load_app(monkeypatch, "@")


def test_matching_is_case_insensitive_and_whitespace_tolerant(monkeypatch):
    app = load_app(monkeypatch, " YOU@YourMail.TLD , @CustomDomain.TLD ")
    assert app._accept(build("You@YOURMAIL.tld"))
    assert app._accept(build("someone@MAIL.CustomDomain.tld"))


def test_null_return_path_rejected(monkeypatch):
    """Bounces and auto-replies arrive as <>; they must not slip through."""
    app = load_app(monkeypatch, None)
    raw = (
        "Return-Path: <>\r\n"
        "X-SES-Spam-Verdict: PASS\r\n"
        "X-SES-Virus-Verdict: PASS\r\n"
        "\r\n"
        "body\r\n"
    ).encode()
    assert not app._accept(raw)


def test_first_return_path_wins(monkeypatch):
    """SES stamps its own Return-Path first; a forged one in the body sits below.

    Real forwarded digests carry three (USPS -> Gmail -> custom domain -> SES),
    so this ordering is load-bearing, not incidental.
    """
    app = load_app(monkeypatch, None)
    raw = (
        "Return-Path: <you@yourmail.tld>\r\n"
        "Return-Path: <attacker@evil.example>\r\n"
        "X-SES-Spam-Verdict: PASS\r\n"
        "X-SES-Virus-Verdict: PASS\r\n"
        "\r\n"
        "body\r\n"
    ).encode()
    assert app._accept(raw)
    flipped = (
        "Return-Path: <attacker@evil.example>\r\n"
        "Return-Path: <you@yourmail.tld>\r\n"
        "X-SES-Spam-Verdict: PASS\r\n"
        "X-SES-Virus-Verdict: PASS\r\n"
        "\r\n"
        "body\r\n"
    ).encode()
    assert not app._accept(flipped)
