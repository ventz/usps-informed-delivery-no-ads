# Authenticating the sender

The ingestion gate in `app._accept` matches `Return-Path` against `ALLOWED_FORWARDERS`.
That is a useful layer, but it is worth being precise about what it proves: **nothing,
cryptographically.**

`Return-Path` is the SMTP envelope sender — `MAIL FROM`. It is *asserted* by whoever opens
the connection, not verified. SES receiving listens on your domain's MX for the open
internet, so anyone who learns `SES_RECIPIENT` can open a session, claim to be an
allow-listed address, and hand over a fully attacker-authored "digest". The Lambda would
parse it and mail it onward from `DIGEST_FROM` — a sender you trust, arriving unattended,
looking exactly like every other digest.

The allow-list raises the bar (you must also learn the address). It does not authenticate.

## What SES already gives you

SES stamps its own `Authentication-Results` header before writing the message to S3:

```
Authentication-Results: amazonses.com;
  spf=pass (spfCheck: domain of example.tld designates 203.0.113.9 as permitted sender)
  client-ip=203.0.113.9; envelope-from=you+caf_=usps=m.example.tld@example.tld;
  helo=mail-ed1-f49.google.com;
  dkim=pass header.i=@email.informeddelivery.usps.com;
  dmarc=pass header.from=email.informeddelivery.usps.com;
```

Three things make this trustworthy:

- **It has an authserv-id.** The leading `amazonses.com;` is a real trust anchor (RFC 8601),
  not a vendor substring in free text. Prefer it over `Received-SPF`, which has none.
- **SES prepends its trace headers.** A forged copy placed in the message body sits
  structurally *below* SES's, and `msg.get()` returns the first occurrence. Real digests
  arrive with three `Return-Path` headers for this same reason, and the ordering holds.
- **It carries `envelope-from=`**, so the verdict and the identity you allow-list can be
  cross-checked against each other rather than trusted separately.

> **Watch out:** this header is long and *folded* across multiple lines. Reading it with
> line-based tools shows only `Authentication-Results: amazonses.com;` and it looks empty.
> Parse it with Python's `email` library, which unfolds it (~350 characters).

Note that `X-SES-SPF-Verdict`, `X-SES-DKIM-Verdict` and `X-SES-DMARC-Verdict` are **not**
stamped on received mail — absent on every message measured. Do not key on them.

## Two options

### C — require `spf=pass`, cross-checked against `envelope-from=`

Authenticates the **forwarder**: proof that the host which handed the message to SES was
authorised by the domain it claimed.

| | |
|---|---|
| **Pro** | Closes the forgery path — a stranger can no longer assert an allow-listed address |
| **Pro** | Verified `pass` on every real digest measured, on both forwarding paths |
| **Pro** | Works with hand-forwarding *and* auto-forwarding |
| **Con** | SPF binds a **domain, not a mailbox**. With a `@gmail.com` entry, `spf=pass` only means "some Gmail user" — prefer full-address entries once this is on |
| **Con** | Says nothing about USPS. It proves who relayed it, not who wrote it |
| **Con** | Breaks if a future forwarding hop relays without rewriting the envelope |

### D — require `dkim=pass` for USPS's domain

Authenticates the **original sender**, cryptographically and end to end. DKIM signs the
message itself, so the signature survives a forward that leaves the bytes alone.

| | |
|---|---|
| **Pro** | Strongest available. Forging it requires USPS's private key, not merely knowing an address |
| **Pro** | Proves the digest genuinely came from USPS — the thing you actually care about |
| **Pro** | Independent of who forwarded it, so the allow-list becomes a convenience rather than a security control |
| **Con** | **Rejects hand-forwarded mail.** A Forward button rewrites the body (`---------- Forwarded message ----------`) and breaks the signature |
| **Con** | Therefore needs an explicit escape hatch for manual testing |
| **Con** | Breaks if USPS changes signing domains, and you would only find out from a rejection log |

Measured across the corpus:

| Forwarding path | `spf` | USPS `dkim=pass` |
|---|---|---|
| Auto-forward filter (envelope rewritten) | pass | **yes** |
| Forward button (body rewritten) | pass | **no** |

## Recommendation

**Run either one log-only first.** Record the verdict and the derived forwarder on every
message, change nothing, and watch for a `WOULD-REJECT` line. A CloudWatch metric filter on
that string is worth more than reading logs by hand.

Time-box it — a week or two, not indefinitely. The point is to catch a forwarding mode that
has not been observed yet, not to build confidence in SPF or DKIM themselves.

Whichever you enforce, **fail closed**: a missing `Authentication-Results` should be a
rejection. That is safe here precisely because nothing outside the Lambda calls the gate, so
there is no local path that needs the leniency.

A belt-and-braces alternative that never touches the Lambda: SES **receipt IP filters**
(`aws ses create-receipt-filter`) allow-list sender IPs at the receiving layer — before S3,
before Lambda, before spend. More moving parts, but arbitrary internet senders never reach
your code at all.

## Status

**Neither is implemented.** This is a deliberate deferral, not an oversight. The gate as
shipped fails closed on every path it does check; see the README's
[Security](../README.md#security) section for what it does today.
