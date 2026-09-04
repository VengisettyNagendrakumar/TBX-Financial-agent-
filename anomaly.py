"""
Statistical Anomaly Detection Module
====================================
Satisfies the Bonus Requirement:
"Simple anomaly callouts, for example flagging a vendor payout that looks
unusually large compared to history while answering the original question."
"""

import pandas as pd
import config

def detect_anomalies(con, vendor_name: str, result_df: pd.DataFrame, query_info: dict = None) -> list:
    """
    Checks if any individual vendor payout in the queried window is a statistical outlier
    compared to the vendor's historical baseline.
    
    Returns:
        list of alert strings
    """
    alerts = []
    if not vendor_name:
        return alerts

    try:
        v_table = config.TABLE_VENDORS
        p_table = config.TABLE_PAYOUTS
        v_id = config.SCHEMA_CONFIG['vendors']['id_col']
        v_name = config.SCHEMA_CONFIG['vendors']['name_col']
        p_vid = config.SCHEMA_CONFIG['vendor_payouts']['vendor_id_col']
        p_amt = config.SCHEMA_CONFIG['vendor_payouts']['amount_col']
        p_date = config.SCHEMA_CONFIG['vendor_payouts']['date_col']
        
        # 1. Historical baseline statistics for this vendor (excluding queried window to prevent contamination)
        start_date = query_info.get("start_date") if query_info else None
        end_date = query_info.get("end_date") if query_info else None
        
        if start_date and end_date:
            stats_query = f"""
            SELECT 
                AVG(p.{p_amt}) AS mean_spend,
                STDDEV_SAMP(p.{p_amt}) AS std_spend,
                COUNT(p.{p_amt}) AS sample_count
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE v.{v_name} = ? AND p.{p_date} NOT BETWEEN ? AND ?
            """
            stats = con.execute(stats_query, [vendor_name, start_date, end_date]).df()
        else:
            stats_query = f"""
            SELECT 
                AVG(p.{p_amt}) AS mean_spend,
                STDDEV_SAMP(p.{p_amt}) AS std_spend,
                COUNT(p.{p_amt}) AS sample_count
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE v.{v_name} = ?
            """
            stats = con.execute(stats_query, [vendor_name]).df()
        
        # Fallback to overall baseline if window exclusion leaves fewer than 2 records
        if stats.empty or stats["mean_spend"].iloc[0] is None or int(stats["sample_count"].iloc[0] or 0) < 2:
            stats_query_fallback = f"""
            SELECT 
                AVG(p.{p_amt}) AS mean_spend,
                STDDEV_SAMP(p.{p_amt}) AS std_spend,
                COUNT(p.{p_amt}) AS sample_count
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE v.{v_name} = ?
            """
            stats = con.execute(stats_query_fallback, [vendor_name]).df()

        if stats.empty or stats["mean_spend"].iloc[0] is None:
            return alerts
            
        mean = float(stats["mean_spend"].iloc[0])
        std = float(stats["std_spend"].iloc[0]) if stats["std_spend"].iloc[0] is not None else (0.3 * mean)
        count = int(stats["sample_count"].iloc[0])
        threshold = mean + (2.0 * std)

        # 2. Get individual payouts in the current scope using parameter binding
        if start_date and end_date:
            individual_query = f"""
            SELECT p.{p_date} AS payout_date, p.{p_amt} AS amount
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE v.{v_name} = ? AND p.{p_date} BETWEEN ? AND ?
            ORDER BY p.{p_amt} DESC
            """
            payouts_df = con.execute(individual_query, [vendor_name, start_date, end_date]).df()
        else:
            individual_query = f"""
            SELECT p.{p_date} AS payout_date, p.{p_amt} AS amount
            FROM {p_table} p
            JOIN {v_table} v ON p.{p_vid} = v.{v_id}
            WHERE v.{v_name} = ?
            ORDER BY p.{p_amt} DESC
            """
            payouts_df = con.execute(individual_query, [vendor_name]).df()

        # Check individual payouts for outlier
        for _, row in payouts_df.iterrows():
            val = float(row["amount"])
            if val > threshold and val > (1.8 * mean) and count >= 2:
                multiplier = round(val / mean, 1)
                p_date_str = str(row['payout_date']).split()[0]
                alerts.append(
                    f"⚠️ **Anomaly Alert**: Payout on **{p_date_str}** of **${val:,.2f}** is **{multiplier}x** "
                    f"higher than {vendor_name}'s historical average of **${mean:,.2f}** (std: ${std:,.2f})."
                )
    except Exception as e:
        print(f"Anomaly check notice: {e}")
        
    return alerts
