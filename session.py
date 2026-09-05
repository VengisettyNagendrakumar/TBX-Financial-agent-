"""
Session / Logged-in User
========================
Who "I" and "my" refer to.

Production would resolve this from an authenticated session. For the prototype
it is hardcoded here (overridable by env) so that:

  - "my balance" means ONE account, not a list of ten
  - "how much did I spend" is scoped to one customer
  - `entity_id` reaches the query layer from the backend and never from the
    model, which is the tenancy boundary described in ARCHITECTURE_V2.md §6.4

A customer may hold several accounts. PRIMARY_ACCOUNT_ID is the one balance
questions default to; spend questions still cover every account the customer
owns, because that is all their money.
"""

import os

import config

# Hardcoded logged-in user. Set TBX_ENTITY_ID / TBX_ACCOUNT_ID to override.
# Empty means "pick the busiest customer in the warehouse at startup", which
# keeps the demo working against freshly generated data.
HARDCODED_ENTITY_ID = os.getenv("TBX_ENTITY_ID", "")
HARDCODED_ACCOUNT_ID = os.getenv("TBX_ACCOUNT_ID", "")

DISPLAY_NAME = os.getenv("TBX_USER_NAME", "Demo User")


class Session:
    """The authenticated customer for this request."""

    def __init__(self, entity_id: str, account_id: str = None,
                 display_name: str = DISPLAY_NAME, account_count: int = 1,
                 bank_name: str = "", masked_number: str = ""):
        self.entity_id = entity_id
        self.account_id = account_id
        self.display_name = display_name
        self.account_count = account_count
        self.bank_name = bank_name
        self.masked_number = masked_number

    def __repr__(self):
        return (f"Session(entity={self.entity_id[:12]}…, "
                f"account={str(self.account_id)[:12]}…, "
                f"accounts={self.account_count})")


def _first_row(con, sql, params=None):
    df = con.execute(sql, params or []).df()
    return None if df.empty else df.iloc[0]


def load(con, entity_id: str = None) -> Session:
    """
    Resolves the logged-in customer and their primary account.

    Falls back to the busiest customer, and within them the account with the
    highest balance, so a fresh warehouse is immediately demonstrable without
    editing anything. Passing `entity_id` overrides the hardcoded customer but
    still resolves their primary account -- a Session without one makes
    "my balance" fall back to listing every account.
    """
    import security

    acct = config.SCHEMA_CONFIG["account"]
    bank = config.SCHEMA_CONFIG["bank"]

    entity_id = entity_id or HARDCODED_ENTITY_ID
    if entity_id:
        exists = con.execute(
            f"SELECT COUNT(*) FROM raw_account WHERE {acct['entity_col']} = ?",
            [entity_id]).fetchone()[0]
        if not exists:
            print(f"[session] TBX_ENTITY_ID {entity_id!r} not in this warehouse; "
                  f"falling back to the busiest customer.")
            entity_id = ""

    if not entity_id:
        row = _first_row(con, f"""
            SELECT entity_id FROM {config.TABLE_MERCHANT_DIM}
            GROUP BY 1 ORDER BY SUM(txn_count) DESC LIMIT 1
        """)
        if row is None:
            raise RuntimeError("No customers in the warehouse. Run build_warehouse.py.")
        entity_id = row["entity_id"]

    account_id = HARDCODED_ACCOUNT_ID
    if account_id:
        owned = con.execute(
            f"SELECT COUNT(*) FROM raw_account "
            f"WHERE {acct['id_col']} = ? AND {acct['entity_col']} = ?",
            [account_id, entity_id]).fetchone()[0]
        if not owned:
            print(f"[session] TBX_ACCOUNT_ID {account_id!r} is not owned by the "
                  f"active customer; falling back to their primary account.")
            account_id = ""

    primary = _first_row(con, f"""
        SELECT a.{acct['id_col']}      AS account_id,
               a.{acct['number_col']}  AS account_number,
               b.{bank['name_col']}    AS bank_name
        FROM raw_account a
        LEFT JOIN raw_bank b ON a.{acct['bank_code_col']} = b.{bank['code_col']}
        WHERE a.{acct['entity_col']} = ?
        {"AND a." + acct['id_col'] + " = ?" if account_id else ""}
        ORDER BY a.{acct['balance_col']} DESC LIMIT 1
    """, [entity_id, account_id] if account_id else [entity_id])

    count = con.execute(
        f"SELECT COUNT(*) FROM raw_account WHERE {acct['entity_col']} = ?",
        [entity_id]).fetchone()[0]

    return Session(
        entity_id=entity_id,
        account_id=primary["account_id"] if primary is not None else None,
        account_count=int(count),
        bank_name=primary["bank_name"] if primary is not None else "",
        masked_number=(security.mask_account_number(primary["account_number"])
                       if primary is not None else ""),
    )
