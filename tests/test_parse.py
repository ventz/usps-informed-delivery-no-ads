"""Regression tests pinned to the real 7/10-8/7 2026 corpus.

The assertions encode findings that were NOT obvious from a single sample and
that a naive parser gets wrong:
  - scan suffixes vary (-066/-067/-068); the ad filter must be suffix-agnostic
  - `content-` can appear with no paired `mailer-`
  - zero-mailpiece days have no attachment parts at all
  - ad-only days exist and must not render as an empty/blank digest
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chalicelib import classify, parse, render  # noqa: E402

SAMPLES = ROOT / "samples"


def load(name):
    path = SAMPLES / f"{name}.eml"
    if not path.exists():
        pytest.skip(f"{path} missing — run: uv run make_fixture.py")
    return parse.parse_digest(path.read_bytes())


# --- ad filter -------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "mailer-1202017988.jpg", "content-1202017988.jpg",
    "MAILER-123.JPG", "content-1201879456.jpg",
])
def test_ad_filenames_are_ads(name):
    assert parse.is_ad_attachment(name)


@pytest.mark.parametrize("name", [
    "2989868880-068.jpg",  # common 2026 suffix
    "1006624496-066.jpg",  # 7/17 — proves we must not key on -068
    "1002338133-067.jpg",  # 7/13
    "1123485347-058.jpg",  # 2023-era
])
def test_scan_filenames_are_not_ads(name):
    assert not parse.is_ad_attachment(name)


# --- per-digest behaviour --------------------------------------------------

def test_typical_day_keeps_scan_drops_ads():
    d = load("2026-08-07")
    assert [s.filename for s in d.scans] == ["2989868880-068.jpg"]
    assert len(d.dropped_ads) == 2
    assert d.announced_mail == 2
    assert d.hidden_mail_count == 1
    assert d.campaign_senders == ["save-select homes"]


def test_ad_only_day_yields_zero_scans_but_reports_loss():
    """8/5: every mailpiece displaced by an advertiser."""
    d = load("2026-08-05")
    assert d.scans == []
    assert len(d.dropped_ads) == 4
    assert d.announced_mail == 2
    assert d.hidden_mail_count == 2
    assert set(d.campaign_senders) == {"Lands' End", "save-select homes"}

    html = render.build_html(d, [])
    # The loss must be visible, not silently rendered as an empty digest.
    assert "did not provide a scan" in html
    assert "Lands&#x27; End" in html or "Lands' End" in html


def test_no_mail_day_has_no_attachments_and_still_parses():
    d = load("2026-08-02")
    assert d.scans == []
    assert d.dropped_ads == []
    assert d.announced_mail == 0
    assert d.hidden_mail_count == 0
    assert len(d.packages) == 2


def test_best_case_day_keeps_every_suffix_variant():
    d = load("2026-07-10")
    assert len(d.scans) == 3
    assert d.dropped_ads == []
    assert d.hidden_mail_count == 0


# --- packages --------------------------------------------------------------

def test_package_fields_and_tracking_dedup():
    d = load("2026-08-07")
    assert len(d.packages) == 1
    pkg = d.packages[0]
    assert pkg.sender == "SHIPFUSION INC"
    # tracking appears twice in the source; exactly one value survives
    assert pkg.tracking == "9261290335949247070387"
    assert pkg.status == "Expected 1-2 Days"
    assert "Aug 08" in pkg.eta


def test_empty_package_sections_are_skipped():
    d = load("2026-08-05")
    assert len(d.packages) == 1
    assert d.packages[0].status == "Awaiting From Sender"
    assert d.packages[0].eta is None


def test_multiple_packages_in_one_section():
    d = load("2026-08-02")
    assert {p.sender for p in d.packages} == {"AMAZON", "EXPRESS SCRIPTS PHARMACY"}
    assert all(p.tracking for p in d.packages)
    # Guards the fragmentation bug: USPS emits "Expected"/"Today" as separate
    # nodes, and without _rejoin_fragments every status silently becomes None.
    assert all(p.status == "Expected Today" for p in d.packages)


def test_cid_sender_mapping_uses_nearest_preceding_from():
    """USPS states the sender next to the scan — parse it, don't infer it.

    Mirrors the real 8/6 structure: a campaign that supplied no replacement
    image falls back to the genuine scan, rendered under its FROM: heading.
    """
    html = """
      <div><span>FROM:</span><span>USPS HR</span>
        <img src="cid:mailer-1202018058.jpg"><img src="cid:content-1202018058.jpg"></div>
      <div><span>FROM:</span><span>Example Bank</span>
        <img src="cid:2989542530-068.jpg"><img src="cid:content-1201908387.jpg"></div>
      <div>FROM: save-select homes<img src="cid:mailer-1202017988.jpg"></div>
    """
    mapping = parse.map_cid_senders(html)
    assert mapping["2989542530-068.jpg"] == "Example Bank"
    assert mapping["mailer-1202018058.jpg"] == "USPS HR"
    assert mapping["mailer-1202017988.jpg"] == "save-select homes"  # inline "FROM: x" form


def test_plain_mailpiece_does_not_inherit_the_campaign_above_it():
    """A genuine scan must not be published under the advertiser's name.

    Mirrors the real markup — a `mail-campaign` block with its FROM:, then a
    `sg-mailpiece` block that carries the genuine scan and NO FROM: at all.
    Under the old document-order walk the scan inherited "Tea Collection", so a
    real letter went out labelled as the campaign above it. The scan must stay
    unlisted and fall through to vision.
    """
    html = """
      <td id="campaign-div-id"><div id="mail-campaign">
        <p>FROM:</p><p>Tea Collection</p>
        <img src="cid:mailer-1201999728.jpg"><img src="cid:content-1201999728.jpg"></div></td>
      <td id="mailpiece-div-id"><div id="sg-mailpiece">
        <img src="cid:2990275948-068.jpg"></div></td>
    """
    mapping = parse.map_cid_senders(html)
    assert mapping["mailer-1201999728.jpg"] == "Tea Collection"
    assert "2990275948-068.jpg" not in mapping


def test_saturation_campaign_block_is_scoped_too():
    """`sat-campaign` is a third block kind; its FROM: must not leak outward."""
    html = """
      <div id="sg-mailpiece"><img src="cid:2989868880-068.jpg"></div>
      <div id="sat-campaign">FROM: save-select homes
        <img src="cid:mailer-1202017988.jpg"></div>
    """
    mapping = parse.map_cid_senders(html)
    assert mapping == {"mailer-1202017988.jpg": "save-select homes"}


def test_campaign_block_keeps_its_own_fallback_scan():
    """The 8/6 shape must still resolve: no replacement image, so the campaign
    block contains the REAL scan plus an orphan ride-along ad."""
    html = """
      <div id="mail-campaign"><span>FROM:</span><span>Example Bank</span>
        <img src="cid:2989542530-068.jpg"><img src="cid:content-1201908387.jpg"></div>
    """
    assert parse.map_cid_senders(html)["2989542530-068.jpg"] == "Example Bank"


def test_listed_sender_overrides_vision_in_render():
    d = load("2026-07-10")
    for scan in d.scans:
        scan.listed_sender = "Example Bank"
    cls = [classify.Classification(sender="P.O. Box 12345, Springfield, IL") for _ in d.scans]
    html = render.build_html(d, cls)
    assert "Example Bank" in html
    assert "P.O. Box 12345" not in html

    # The text/plain alternative must apply the same precedence — it used to
    # print the vision guess while the HTML showed USPS's own label.
    text = render.build_text(d, cls)
    assert "Example Bank" in text
    assert "P.O. Box 12345" not in text


def test_sender_with_a_scan_is_excluded_from_the_missing_list():
    """The 8/6 shape: Example Bank supplied the scan, so it is NOT 'replaced'."""
    d = load("2026-08-05")
    d.campaign_senders = ["USPS HR", "Example Bank", "save-select homes"]
    d.scans[:] = [parse.Scan("2989542530-068.jpg", "image/jpeg", b"x", "c0", "Example Bank")]

    assert d.senders_without_scans == ["USPS HR", "save-select homes"]
    html = render.build_html(d, [classify.Classification()])
    assert "USPS HR" in html and "save-select homes" in html
    # the sender that DID provide a scan must not appear in the notice
    notice = html.split("did not provide a scan")[1]
    assert "Example Bank" not in notice


def test_split_labels_are_rejoined():
    lines = ["Expected", "Today", "2", "item(s)", "FROM:", "AMAZON"]
    assert parse._rejoin_fragments(lines) == ["Expected Today", "2 item(s)", "FROM:", "AMAZON"]
    # a label that already arrives whole must survive untouched
    assert parse._rejoin_fragments(["Expected This Week"]) == ["Expected This Week"]


# --- dates -----------------------------------------------------------------

@pytest.mark.parametrize("name,iso", [
    ("2026-08-07", "2026-08-07"),
    ("2026-08-05", "2026-08-05"),
    ("2026-08-02", "2026-08-02"),
    ("2026-07-10", "2026-07-10"),
])
def test_dates(name, iso):
    assert load(name).digest_date.isoformat() == iso


# --- rendering -------------------------------------------------------------

def test_no_ad_creative_reaches_the_output():
    """The core guarantee: advertiser assets never appear in the rebuilt email."""
    for name in ("2026-08-07", "2026-08-05"):
        d = load(name)
        msg = render.build_message(d, [classify.Classification() for _ in d.scans],
                                   "a@b.c", "d@e.f")
        blob = msg.as_bytes()
        for ad in d.dropped_ads:
            assert ad.encode() not in blob, f"{ad} leaked into {name}"


def test_scans_are_inline_cid_attachments():
    d = load("2026-07-10")
    msg = render.build_message(d, [classify.Classification() for _ in d.scans],
                               "a@b.c", "d@e.f")
    cids = [p.get("Content-ID") for p in msg.walk() if p.get_content_type().startswith("image/")]
    assert len(cids) == 3
    html = render.build_html(d, [classify.Classification() for _ in d.scans])
    for scan in d.scans:
        assert f"cid:{scan.cid}" in html
        assert f"<{scan.cid}>" in cids


def test_html_is_inline_styled_and_utf8():
    d = load("2026-08-07")
    html = render.build_html(d, [classify.Classification()])
    assert "<style" not in html          # Gmail strips <style> unreliably
    assert 'charset="utf-8"' in html
    assert "display:flex" not in html    # no flexbox in email clients


def test_html_carries_its_accessibility_semantics():
    """Pins the a11y work — exactly the kind of thing a refactor reverts silently.

    Headings matter here specifically because Gmail, Apple Mail and Outlook.com
    strip <style> blocks and ARIA but PRESERVE heading tags, so they are the only
    structural semantics that survive into a mail body.
    """
    d = load("2026-07-10")
    cls = [classify.Classification(mail_type="bill") for _ in d.scans]
    html = render.build_html(d, cls)

    assert "<h1 " in html and "<h2 " in html and "<h3 " in html
    assert "margin:0" in html                      # or client UA defaults wreck spacing
    assert 'lang="en"' in html and 'dir="ltr"' in html
    assert html.count('lang="en"') >= 2            # also on the body table: Gmail drops <html>
    assert 'alt="USPS envelope scan"' in html      # not a duplicate of the sender heading
    assert "Scan of mail from" not in html
    assert "#6b7280" not in html                   # failed AA on the page background
    assert render.MUTED == "#5b6270"
    assert "text-decoration:none" not in html      # links must look like links


def test_subject_summarizes_counts():
    assert "2 mailpieces" in render.subject_for(load("2026-08-07"))
    assert "2 packages" in render.subject_for(load("2026-08-02"))


# --- classification ordering ----------------------------------------------

def test_important_mail_sorts_above_junk():
    bill = classify.Classification(mail_type="bill")
    junk = classify.Classification(mail_type="credit_offer")
    assert sorted([junk, bill], key=lambda c: c.sort_key)[0] is bill
    assert bill.is_important and junk.is_junk


@pytest.mark.parametrize("mail_type,expected", [
    ("bill", True), ("statement", True), ("financial", True), ("medical", True),
    ("credit_offer", False), ("catalog", False), ("political", False),
    ("personal", False), ("unknown", False),
])
def test_actionable_is_derived_not_model_supplied(mail_type, expected):
    """Guards the flakiness fix: identical input must give identical output.

    `actionable` used to be a schema field and flipped True/False across runs on
    the same scan. It is now a pure function of mail_type.
    """
    assert classify.Classification(mail_type=mail_type).actionable is expected
    assert "actionable" not in classify.SCHEMA["required"]


# --- MIME safety ------------------------------------------------------------

def test_attachment_filename_cannot_inject_mime_headers():
    """Confirmed live: get_filename() percent-decodes RFC 2231, so a crafted
    `filename*=utf-8''x%0D%0A...` put a real CRLF into Content-Disposition on the
    message SES sends. The dangerous payload is a forged `Content-Type: text/html`
    rendering attacker HTML inside a digest the reader trusts."""
    raw = (
        b"From: a@b.c\r\nTo: d@e.f\r\nSubject: x\r\n"
        b'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/html\r\n\r\n<html></html>\r\n"
        b"--B\r\nContent-Type: image/jpeg\r\n"
        b"Content-Disposition: inline; filename*=utf-8''ev%0D%0AX-Injected:%20yes\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\neHg=\r\n--B--\r\n"
    )
    d = parse.parse_digest(raw)
    assert d.scans, "the crafted part should still be parsed as a scan"
    assert "\r" not in d.scans[0].filename and "\n" not in d.scans[0].filename
    blob = render.build_message(d, [classify.Classification()], "a@b.c", "d@e.f").as_bytes()
    # The residual text may survive INSIDE the quoted filename, which is inert.
    # What must not happen is it starting a header line of its own.
    assert not any(ln.startswith(b"X-Injected") for ln in blob.splitlines())


def test_content_type_is_sanitized_at_the_boundary():
    """render.py validated at the use site; classify.py builds a data: URL from
    the same value and had no check, so normalize once where it enters."""
    raw = (
        b"From: a@b.c\r\nTo: d@e.f\r\nSubject: x\r\n"
        b'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/html\r\n\r\n<html></html>\r\n"
        b'--B\r\nContent-Type: image/jp"eg\r\n\tevil: 1\r\n'
        b'Content-Disposition: inline; filename="x.jpg"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\neHg=\r\n--B--\r\n"
    )
    d = parse.parse_digest(raw)
    assert d.scans and d.scans[0].content_type == "image/jpeg"


def test_attachment_count_and_size_are_capped():
    """One paid vision call per scan, and an S3 retry redoes them all."""
    parts = b"".join(
        b"--B\r\nContent-Type: image/jpeg\r\n"
        + f'Content-Disposition: inline; filename="{i}-068.jpg"\r\n'.encode()
        + b"Content-Transfer-Encoding: base64\r\n\r\neHg=\r\n"
        for i in range(40)
    )
    raw = (b"From: a@b.c\r\nTo: d@e.f\r\nSubject: x\r\n"
           b'Content-Type: multipart/related; boundary="B"\r\n\r\n'
           b"--B\r\nContent-Type: text/html\r\n\r\n<html></html>\r\n"
           + parts + b"--B--\r\n")
    assert len(parse.parse_digest(raw).scans) == parse.MAX_SCANS


def test_tracking_regex_is_ascii_only():
    """`\\d` is Unicode-aware without re.ASCII — Arabic-Indic digits matched and
    produced a dead tracking link."""
    assert not parse.TRACKING_RE.search("٠" * 22)
    assert parse.TRACKING_RE.search("9261290335949247070387")


def test_malformed_content_type_cannot_forge_headers():
    d = parse.Digest(announced_mail=1)
    d.scans.append(parse.Scan("x.jpg", 'image/jp"eg\r\nX-Injected: yes', b"x", "c0"))
    blob = render.build_message(d, [classify.Classification()], "a@b.c", "d@e.f").as_bytes()
    assert not any(ln.startswith(b"X-Injected") for ln in blob.splitlines())
    assert b"Content-Type: image/jpeg" in blob   # fell back to the safe subtype
