"""
Counterparty Resolver (Phase 3)
===============================
Maps what the user typed ("swiggy", "Stripe Inc", "my friend Gautam") onto a
canonical counterparty in the warehouse.

This is V1's resolver repointed at a derived dimension. The contract is
unchanged and was the right one:

    MATCH      -- one confident canonical
    AMBIGUOUS  -- several plausible; ask which
    NOT_FOUND  -- no such counterparty; say so rather than inventing spend

What changed is the candidate source. V1 searched `SELECT vendor_name FROM
vendors`. The V2 schema has no vendor table, so candidates come from
`merchant_dim` -- the vocabulary derived at ingest -- scoped to one entity.

BUGS.md B06 is fixed here. V1 stripped legal suffixes when generating acronyms
but not before fuzzy scoring, so 'Stripe Inc' scored 63 against
'Stripe Payments' and was reported NOT_FOUND -- a false negative on the exact
entity-permutation problem the resolver exists to solve. Adding a suffix made
matching *worse*. Both sides are now normalised before scoring.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

import config
import db
from enrichment import normalise_name

MATCH = "MATCH"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"
NONE = "NONE"

# A confident single winner.
STRONG_SCORE = 88
# Plausible, but close enough to a rival that guessing would be wrong in
# finance. Below STRONG and above this, we ask instead of assuming.
WEAK_SCORE = 70
# Minimum lead over the runner-up before a fuzzy top hit counts as confident.
MIN_GAP = 8


@dataclass
class Resolution:
    status: str
    entity: Optional[str] = None
    candidates: list = field(default_factory=list)
    confidence: float = 0.0
    method: str = ""
    kind: Optional[str] = None
    stats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status, "resolved": self.entity,
            "candidates": self.candidates, "confidence": self.confidence,
            "method": self.method, "counterparty_kind": self.kind, "stats": self.stats,
        }


def load_vocabulary(con, entity_id: str, kind: str = None) -> list:
    """
    Candidate counterparties for one entity: the replacement for V1's
    `SELECT vendor_name FROM vendors`.

    Returns [{name, kind, txn_count, total_debit, total_credit,
              first_month, last_month, active_months}]
    """
    sql = f"""
        SELECT merchant_norm AS name, counterparty_kind AS kind, txn_count,
               COALESCE(total_debit, 0)  AS total_debit,
               COALESCE(total_credit, 0) AS total_credit,
               first_month, last_month, active_months
        FROM {config.TABLE_MERCHANT_DIM}
        WHERE entity_id = ? AND merchant_norm <> ?
    """
    params = [entity_id, config.UNKNOWN_MERCHANT]
    if kind:
        sql += " AND counterparty_kind = ?"
        params.append(kind)
    sql += " ORDER BY txn_count DESC"
    return db.query_df(con, sql, params).to_dict("records")


def _score(a: str, b: str) -> float:
    """
    Similarity between two already-normalised names.

    token_set_ratio is included because it tolerates added or dropped words --
    the exact failure in B06, where an extra 'INC' sank WRatio below threshold.
    """
    return max(fuzz.WRatio(a, b), fuzz.token_set_ratio(a, b))


def _acronyms(name: str) -> list:
    """'HDFC BANK LIMITED' -> ['hb', 'hbl']. Lets 'HDFC' style shorthand resolve."""
    words = [w for w in re.sub(r"[^A-Za-z0-9\s]", " ", name).lower().split()
             if w not in {s.lower() for s in config.LEGAL_SUFFIXES}]
    out = []
    if len(words) >= 2:
        out.append("".join(w[0] for w in words))
    allw = re.sub(r"[^A-Za-z0-9\s]", " ", name).lower().split()
    if len(allw) >= 2:
        full = "".join(w[0] for w in allw)
        if full not in out:
            out.append(full)
    return out


def resolve_merchant(con, entity_id: str, name: str, kind: str = None,
                     vocabulary: list = None) -> Resolution:
    """Resolves a counterparty name against one entity's vocabulary."""
    if not name or not str(name).strip():
        return Resolution(status=NONE, confidence=1.0, method="empty")

    vocab = vocabulary if vocabulary is not None else load_vocabulary(con, entity_id, kind)
    if not vocab:
        return Resolution(status=NOT_FOUND, confidence=0.0, method="empty_vocabulary")

    by_name = {v["name"]: v for v in vocab}
    raw = str(name).strip()
    query = normalise_name(raw)
    if not query:
        return Resolution(status=NONE, confidence=1.0, method="empty")

    def hit(n, conf, method):
        v = by_name.get(n, {})
        return Resolution(MATCH, n, [n], conf, method, v.get("kind"), _stats(v))

    # 1. exact canonical
    if query in by_name:
        return hit(query, 1.0, "exact")

    # 2. explicit brand/legal alias (BUNDL TECHNOLOGIES -> SWIGGY)
    aliased = config.MERCHANT_ALIASES.get(query)
    if aliased and aliased in by_name:
        return hit(aliased, 0.98, "alias")

    # 3. case-insensitive exact against the raw input
    for n in by_name:
        if n.lower() == raw.lower():
            return hit(n, 1.0, "exact_ci")

    # 4. acronym (unique only -- an ambiguous acronym must be asked about)
    acro = [n for n in by_name if query.lower() in _acronyms(n)]
    if len(acro) == 1:
        return hit(acro[0], 0.96, "acronym")
    if len(acro) > 1:
        return Resolution(AMBIGUOUS, None, acro[:5], 0.9, "acronym_ambiguous")

    # 5. whole-word containment ('swiggy' -> 'SWIGGY INSTAMART')
    contained = []
    for n in by_name:
        words = n.split()
        if query == n or query in words or f" {query} " in f" {n} ":
            contained.append(n)
    if len(contained) == 1:
        return hit(contained[0], 0.95, "contained")
    if len(contained) > 1:
        ranked = sorted(contained, key=lambda n: -by_name[n]["txn_count"])
        return Resolution(AMBIGUOUS, None, ranked[:5], 0.9, "contained_ambiguous")

    # 6. substring
    partial = [n for n in by_name if query in n]
    if len(partial) == 1:
        return hit(partial[0], 0.92, "substring")
    if len(partial) > 1:
        ranked = sorted(partial, key=lambda n: -by_name[n]["txn_count"])
        return Resolution(AMBIGUOUS, None, ranked[:5], 0.9, "substring_ambiguous")

    # 7. fuzzy over normalised names (B06: suffixes are gone from both sides)
    scored = sorted(((n, _score(query, n)) for n in by_name), key=lambda x: -x[1])
    top, top_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0

    if top_score >= STRONG_SCORE and (top_score - second_score) >= MIN_GAP:
        return hit(top, round(top_score / 100, 2), "fuzzy")

    if top_score >= WEAK_SCORE:
        # Plausible but not confident. In finance, guessing which vendor the
        # user meant is worse than asking -- so this is AMBIGUOUS, not MATCH.
        close = [n for n, s in scored if s >= WEAK_SCORE][:5]
        return Resolution(AMBIGUOUS, None, close, round(top_score / 100, 2),
                          "fuzzy_ambiguous")

    return Resolution(NOT_FOUND, None, [n for n, _ in scored[:3]], 0.0, "no_match")


def _stats(v: dict) -> dict:
    if not v:
        return {}
    first, last = v.get("first_month"), v.get("last_month")
    return {
        "txn_count": int(v.get("txn_count") or 0),
        "total_debit": round(float(v.get("total_debit") or 0), 2),
        "total_credit": round(float(v.get("total_credit") or 0), 2),
        "first_month": str(first)[:10] if first is not None else None,
        "last_month": str(last)[:10] if last is not None else None,
        "active_months": int(v.get("active_months") or 0),
    }


def resolve_person(con, entity_id: str, name: str = None) -> Resolution:
    """
    Resolves an individual, for "how much did my friend pay me".

    With no name there is nothing to resolve -- 'my friend' is not in the data.
    Returning the candidate people as AMBIGUOUS is what lets the agent ask
    "which one?" instead of guessing or refusing.
    """
    people = load_vocabulary(con, entity_id, kind=config.KIND_PERSON)
    if not people:
        return Resolution(NOT_FOUND, None, [], 0.0, "no_people")

    if not name or not str(name).strip() or str(name).strip().lower() in {
            "friend", "my friend", "someone", "a friend", "somebody", "person"}:
        ranked = sorted(people, key=lambda p: -float(p.get("total_credit") or 0))
        return Resolution(AMBIGUOUS, None, [p["name"] for p in ranked[:5]], 0.5,
                          "person_unspecified", config.KIND_PERSON,
                          {"candidate_count": len(people)})

    return resolve_merchant(con, entity_id, name, vocabulary=people)


def needs_clarification(res: Resolution) -> bool:
    return res.status in (AMBIGUOUS, NOT_FOUND)


def describe(res: Resolution) -> str:
    """User-facing phrasing for a resolution that could not produce one answer."""
    if res.status == MATCH:
        return f"Interpreted as **{res.entity}** ({int(res.confidence * 100)}% confidence)."
    if res.status == AMBIGUOUS:
        opts = ", ".join(f"**{c}**" for c in res.candidates)
        return f"That could mean several counterparties: {opts}. Which did you mean?"
    if res.status == NOT_FOUND:
        near = f" Closest names on record: {', '.join(res.candidates)}." if res.candidates else ""
        return f"I have no transactions for that counterparty.{near}"
    return ""
