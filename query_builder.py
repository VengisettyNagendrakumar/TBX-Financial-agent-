"""
Deterministic Query Builder (Fully Parameterized)
=================================================
Generates secure, parameterized SQL queries for DuckDB based on parsed structured intent.
Eliminates SQL injection by using '?' parameter binding for all user/LLM inputs.
"""

import config
from db import calculate_relative_date_range

def format_display_sql(sql: str, params: list) -> str:
    """Formats a human-readable SQL query for the explainability audit drawer."""
    display = sql
    for param in params:
        if isinstance(param, str):
            formatted_val = f"'{param}'"
        elif param is None:
            formatted_val = "NULL"
        else:
            formatted_val = str(param)
        display = display.replace("?", formatted_val, 1)
    return display

def build_sql(intent: dict, resolved_vendor: str, anchor_date) -> dict:
    """
    Translates parsed intent into parameterized DuckDB SQL with '?' binding.
    
    Returns:
        dict: {
            "sql": str,              # Parameterized SQL string with '?' markers
            "params": list,          # Bound parameters list (prevents SQL injection)
            "display_sql": str,      # Formatted string for user-facing audit trail
            "query_type": str,
            "filters_applied": dict,
            "start_date": str or None,
            "end_date": str or None
        }
    """
    intent_type = intent.get("intent", "spend_summary")
    date_filter = intent.get("date_filter", {})
    recon_status = intent.get("reconciliation_status", "all")
    category = intent.get("category")
    
    # 1. Resolve date boundaries relative to anchor_date
    start_date = None
    end_date = None
    if isinstance(date_filter, dict):
        d_type = date_filter.get("type")
        if d_type == "relative":
            start_date, end_date = calculate_relative_date_range(
                date_filter.get("relative_value"), anchor_date
            )
        elif d_type == "absolute":
            start_date = date_filter.get("start_date")
            end_date = date_filter.get("end_date")

    # 2. Build WHERE filters and bound parameters for payouts
    payout_filters = []
    payout_params = []
    
    if resolved_vendor:
        payout_filters.append(f"v.{config.SCHEMA_CONFIG['vendors']['name_col']} = ?")
        payout_params.append(resolved_vendor)
        
    if start_date and end_date:
        payout_filters.append(f"p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']} BETWEEN ? AND ?")
        payout_params.extend([start_date, end_date])
        
    if category:
        payout_filters.append(f"LOWER(v.{config.SCHEMA_CONFIG['vendors']['category_col']}) LIKE ?")
        payout_params.append(f"%{category.lower()}%")

    where_payouts = " AND ".join(payout_filters) if payout_filters else "1=1"

    # 3. Build WHERE filters and bound parameters for transactions & reconciliation
    recon_filters = []
    recon_params = []
    
    if resolved_vendor:
        recon_filters.append(f"v.{config.SCHEMA_CONFIG['vendors']['name_col']} = ?")
        recon_params.append(resolved_vendor)
        
    if start_date and end_date:
        recon_filters.append(f"t.{config.SCHEMA_CONFIG['transactions']['date_col']} BETWEEN ? AND ?")
        recon_params.extend([start_date, end_date])
    
    if recon_status and recon_status != "all":
        valid_mapped = config.RECONCILIATION_VALUES.get(recon_status.lower(), [recon_status.lower()])
        status_conditions = [f"LOWER(r.{config.SCHEMA_CONFIG['reconciliation_status']['status_col']}) = ?" for _ in valid_mapped]
        recon_filters.append(f"({' OR '.join(status_conditions)})")
        recon_params.extend([val.lower() for val in valid_mapped])

    where_recon = " AND ".join(recon_filters) if recon_filters else "1=1"

    # 4. Construct SQL based on Intent
    v_table = config.TABLE_VENDORS
    p_table = config.TABLE_PAYOUTS
    t_table = config.TABLE_TRANSACTIONS
    r_table = config.TABLE_RECONCILIATION

    v_id = config.SCHEMA_CONFIG['vendors']['id_col']
    v_name = config.SCHEMA_CONFIG['vendors']['name_col']
    p_vid = config.SCHEMA_CONFIG['vendor_payouts']['vendor_id_col']
    t_vid = config.SCHEMA_CONFIG['transactions']['vendor_id_col']
    t_id = config.SCHEMA_CONFIG['transactions']['id_col']
    r_tid = config.SCHEMA_CONFIG['reconciliation_status']['txn_id_col']

    if intent_type == "latest_payment":
        active_params = payout_params
        limit_val = intent.get("limit") or 1
        sql = f"""
        SELECT 
            p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']} AS payout_date,
            v.{v_name} AS vendor_name,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']} AS amount,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['status_col']} AS status,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['desc_col']} AS description
        FROM {p_table} p
        JOIN {v_table} v ON p.{p_vid} = v.{v_id}
        WHERE {where_payouts}
        ORDER BY p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']} DESC
        LIMIT {limit_val}
        """

    elif intent_type == "spend_summary":
        active_params = payout_params
        if resolved_vendor:
            sql = f"""
            SELECT 
                v.{v_name} AS vendor_name,
                COUNT(p.{config.SCHEMA_CONFIG['vendor_payouts']['id_col']}) AS total_payouts,
                ROUND(SUM(p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']}), 2) AS total_spend,
                ROUND(AVG(p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']}), 2) AS average_payout,
                MIN(p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']}) AS earliest_payout,
                MAX(p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']}) AS latest_payout
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE {where_payouts}
            GROUP BY v.{v_name}
            """
        else:
            sql = f"""
            SELECT 
                v.{v_name} AS vendor_name,
                COUNT(p.{config.SCHEMA_CONFIG['vendor_payouts']['id_col']}) AS total_payouts,
                ROUND(SUM(p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']}), 2) AS total_spend,
                ROUND(AVG(p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']}), 2) AS average_payout
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE {where_payouts}
            GROUP BY v.{v_name}
            ORDER BY total_spend DESC
            LIMIT 20
            """

    elif intent_type == "reconciliation_audit":
        active_params = recon_params
        sql = f"""
        SELECT 
            t.{config.SCHEMA_CONFIG['transactions']['date_col']} AS transaction_date,
            v.{v_name} AS vendor_name,
            t.{config.SCHEMA_CONFIG['transactions']['amount_col']} AS amount,
            r.{config.SCHEMA_CONFIG['reconciliation_status']['status_col']} AS status,
            r.{config.SCHEMA_CONFIG['reconciliation_status']['notes_col']} AS notes,
            t.{config.SCHEMA_CONFIG['transactions']['desc_col']} AS description
        FROM {t_table} t
        JOIN {v_table} v ON t.{t_vid} = v.{v_id}
        JOIN {r_table} r ON t.{t_id} = r.{r_tid}
        WHERE {where_recon}
        ORDER BY t.{config.SCHEMA_CONFIG['transactions']['date_col']} DESC
        LIMIT 50
        """

    elif intent_type == "category_summary":
        active_params = payout_params
        sql = f"""
        SELECT 
            v.{config.SCHEMA_CONFIG['vendors']['category_col']} AS category,
            COUNT(p.{config.SCHEMA_CONFIG['vendor_payouts']['id_col']}) AS payout_count,
            ROUND(SUM(p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']}), 2) AS total_spend
        FROM {p_table} p
        JOIN {v_table} v ON p.{p_vid} = v.{v_id}
        WHERE {where_payouts}
        GROUP BY v.{config.SCHEMA_CONFIG['vendors']['category_col']}
        ORDER BY total_spend DESC
        """

    else:  # transaction_list / default details
        active_params = payout_params
        limit_val = intent.get("limit") or 50
        sql = f"""
        SELECT 
            p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']} AS payout_date,
            v.{v_name} AS vendor_name,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['amount_col']} AS amount,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['status_col']} AS status,
            p.{config.SCHEMA_CONFIG['vendor_payouts']['desc_col']} AS description
        FROM {p_table} p
        JOIN {v_table} v ON p.{p_vid} = v.{v_id}
        WHERE {where_payouts}
        ORDER BY p.{config.SCHEMA_CONFIG['vendor_payouts']['date_col']} DESC
        LIMIT {limit_val}
        """

    cleaned_sql = sql.strip()
    return {
        "sql": cleaned_sql,
        "params": active_params,
        "display_sql": format_display_sql(cleaned_sql, active_params),
        "query_type": intent_type,
        "filters_applied": {
            "vendor": resolved_vendor,
            "start_date": start_date,
            "end_date": end_date,
            "reconciliation_status": recon_status,
            "category": category
        },
        "start_date": start_date,
        "end_date": end_date
    }
