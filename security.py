"""
Data Protection Helpers
=======================
Masking and redaction applied at the boundary between the warehouse and
anything that leaves the process (the UI, the LLM API, CSV exports).

Two distinct concerns:

  mask_account_number  -- the schema marks account_number sensitive; it must
                          never be rendered raw, even to the account's owner.

  redact_for_llm       -- narration strings embed account numbers and personal
                          names ("NEFT/000483399203/ICIC/PARESH VIKRANT GHASE").
                          Sending them to a third-party model would leak PII, so
                          descriptions are redacted before egress.

Phase 5 extends this with TLS termination and the UTR blind index. The pieces
here are the ones Phase 2 already needs.
"""

import re
import hmac
import hashlib

import config

_DIGIT_RUN = re.compile(rf"\d{{{config.PII_MIN_DIGIT_RUN},}}")


def mask_account_number(value) -> str:
    """'50200013729069' -> 'XXXXXXXXXX9069'. Never returns the raw value."""
    if value is None:
        return ""
    s = str(value).strip()
    keep = config.ACCOUNT_NUMBER_VISIBLE_SUFFIX
    if len(s) <= keep:
        return "X" * len(s)
    return "X" * (len(s) - keep) + s[-keep:]


def redact_for_llm(text) -> str:
    """
    Strips identifiers from free text before it reaches the model.

    Long digit runs (account numbers, UTR references, phone numbers) are
    replaced with a placeholder. The merchant name survives, which is all the
    model needs to write an answer.
    """
    if text is None:
        return ""
    return _DIGIT_RUN.sub("[REDACTED]", str(text))


def redact_records(records: list, fields=("description", "utr_number", "account_number")) -> list:
    """Applies redaction to the named fields of a list of dicts, in place-safe form."""
    out = []
    for r in records:
        c = dict(r)
        for f in fields:
            if f in c and c[f] is not None:
                c[f] = (mask_account_number(c[f]) if f == "account_number"
                        else redact_for_llm(c[f]))
        out.append(c)
    return out


def utr_blind_index(plaintext_utr: str) -> str:
    """
    Deterministic HMAC so an encrypted UTR column stays equality-searchable
    without decrypting every row.

    Returns None when no pepper is configured, which is the honest state when
    the organisers supply ciphertext without a key: UTR search is then
    unavailable rather than silently wrong.
    """
    if not config.UTR_BLIND_INDEX_PEPPER or not plaintext_utr:
        return None
    return hmac.new(
        config.UTR_BLIND_INDEX_PEPPER.encode(),
        str(plaintext_utr).strip().upper().encode(),
        hashlib.sha256,
    ).hexdigest()


def utr_search_available() -> bool:
    return bool(config.UTR_BLIND_INDEX_PEPPER)
