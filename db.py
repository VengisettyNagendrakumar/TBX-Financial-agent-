"""
Database Management & Anchor Date Engine
========================================
Handles DuckDB in-memory initialization, CSV loading, dynamic anchor date
resolution, and query execution.
"""

import os
import duckdb
import pandas as pd
from datetime import datetime, date
from dateutil.relativedelta import relativedelta #from dateutil.relativedelta import relativedelta This is used for date arithmetic. anchor_date - relativedelta(months=1)means: Go back exactly one month. This is much safer for month calculations than simply subtracting 30 days.
import config

def get_db_connection(): #Create the database and load all your CSV data into it.
    """Returns a DuckDB connection with all CSV tables loaded."""
    con = duckdb.connect(database=":memory:")
    load_all_tables(con)
    return con

def load_all_tables(con):
    """Loads all CSV tables specified in config.py into DuckDB."""
    for table_name, meta in config.SCHEMA_CONFIG.items():
        csv_path = os.path.join(config.DATA_DIR, meta["file"]).replace("\\", "/") #C:/Hackathon/Bessemer Hackathon/data/transactions.csv
        if os.path.exists(csv_path):
            con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_csv_auto('{csv_path}');")#read_csv_auto(...) This is a DuckDB function. It reads the CSV and automatically detects its structure/types. SELECT * FROM read_csv_auto(...) means Take everything from this CSV.so overall this line says "Read this CSV and create a DuckDB table from it."
        else:
            print(f"Warning: {csv_path} not found. Table {table_name} not loaded.")

def get_anchor_date(con) -> date: #What date should the application consider as the latest date in the dataset?
    """
    Trap #3 Solution:
    Finds the maximum date in the transactions / vendor_payouts table.
    All relative dates ('last month', 'this quarter', 'YTD') are anchored
    to this date rather than datetime.now().
    """
    try:#Find the latest date from vendor payouts AND transactions, then choose whichever is later.
        res = con.execute(f"""
            SELECT MAX(p_date) FROM (
                SELECT MAX({config.SCHEMA_CONFIG['vendor_payouts']['date_col']}) as p_date FROM {config.TABLE_PAYOUTS} 
                SELECT MAX({config.SCHEMA_CONFIG['transactions']['date_col']}) as p_date FROM {config.TABLE_TRANSACTIONS}
            )
        """).fetchone()
         # SELECT MAX({config.SCHEMA_CONFIG['vendor_payouts']['date_col']}) as p_date FROM {config.TABLE_PAYOUTS} #SELECT MAX(payout_date)FROM vendor_payouts
        # SELECT MAX({config.SCHEMA_CONFIG['transactions']['date_col']}) as p_date FROM {config.TABLE_TRANSACTIONS} SELECT MAX(transaction_date)FROM transactions
        #suppose we have Vendor payouts maximum = May 31 Transactions maximum    = May 30 UNION ALL puts those two results together: may 31 and may 30 SELECT MAX(p_date) i.e may 31

        
        if res and res[0]:
            val = res[0]
            if isinstance(val, str):
                return datetime.strptime(val, "%Y-%m-%d").date()
            elif isinstance(val, (datetime, date)):
                return val if isinstance(val, date) else val.date() #if its date time just return date  else directly return it if its just date
    except Exception as e:
        print(f"Notice: Using fallback anchor date due to: {e}")
    
    # Fallback to current date if table is empty
    return date.today()

def calculate_relative_date_range(period: str, anchor_date: date):
    """
    Calculates exact start and end dates relative to anchor_date:
    - 'last_month'
    - 'this_month'
    - 'two_months_ago'
    - 'last_quarter'
    - 'ytd'
    """
    if not period or not anchor_date:
        return None, None
        
    period = period.lower().strip()
    
    if period in ["last_month", "previous_month"]:
        # First day of previous month to last day of previous month
        first_of_this_month = anchor_date.replace(day=1) #anchor date is may 31 it will replace with may 1
        last_of_prev_month = first_of_this_month - relativedelta(days=1) #last day of previous month
        first_of_prev_month = last_of_prev_month.replace(day=1)
        return first_of_prev_month.strftime("%Y-%m-%d"), last_of_prev_month.strftime("%Y-%m-%d") #so lat month 2024-04-01 → 2024-04-30

    elif period in ["this_month", "current_month"]:
        first_of_this_month = anchor_date.replace(day=1)
        return first_of_this_month.strftime("%Y-%m-%d"), anchor_date.strftime("%Y-%m-%d")

    elif period in ["two_months_ago", "month_before_last"]:
        first_of_this_month = anchor_date.replace(day=1)
        prev_1 = first_of_this_month - relativedelta(months=1) #last month
        prev_2_start = (prev_1 - relativedelta(months=1)).replace(day=1) #two months ago start date
        prev_2_end = prev_1 - relativedelta(days=1) #two months ago end date
        return prev_2_start.strftime("%Y-%m-%d"), prev_2_end.strftime("%Y-%m-%d")

    elif period in ["ytd", "year_to_date"]:
        ytd_start = date(anchor_date.year, 1, 1)
        return ytd_start.strftime("%Y-%m-%d"), anchor_date.strftime("%Y-%m-%d")

    elif period in ["last_quarter", "q1", "q2", "q3", "q4"]:
        # Standard quarter mapping
        current_quarter = (anchor_date.month - 1) // 3 + 1 #for may month =5 Because we want the months to be grouped neatly into blocks of 3 starting from zero: (5 - 1) // 3 + 1 i.e  4 // 3 + 1 i.e 1 + 1 = 2 so current quarter is 2
        target_q = current_quarter - 1 if period == "last_quarter" else int(period.replace("q", "")) #if q3 goes to q4 if asks last quarter gives 4 by replacing q with empty
        target_year = anchor_date.year if target_q >= 1 else anchor_date.year - 1 #Handle previous year's Q4 #is trgaet quarter is less than 1 then it means we are in q1 and last quarter is q4 of previous year so target year = anchor date year -1
        target_q = 4 if target_q < 1 else target_q 
        
        q_start_month = (target_q - 1) * 3 + 1 #gives quarteer starter months like for q1 jan and for q2 april and for q3 july and for q4 october
        q_start = date(target_year, q_start_month, 1)
        q_end = (q_start + relativedelta(months=3)) - relativedelta(days=1) #Q1 = January 1 → March 31 from start date add 3 months  and after adding 3 months subtract 1 day so it will be that quarter 
        return q_start.strftime("%Y-%m-%d"), q_end.strftime("%Y-%m-%d")
        
    return None, None

def get_all_vendor_names(con) -> list:
    """Returns list of canonical vendor names from DB."""
    try:
        df = con.execute(f"SELECT {config.SCHEMA_CONFIG['vendors']['name_col']} FROM {config.TABLE_VENDORS}").df()
        return df[config.SCHEMA_CONFIG['vendors']['name_col']].dropna().tolist()
    except Exception:
        return []

# db.py And it has 4 main responsibilities:

# Create the DuckDB database.
# Load all CSV files into DuckDB.
# Find the dataset's anchor date.
# Convert phrases like "last month" / "YTD" / "last quarter" into exact dates.
# Give the system the list of valid vendor names.




# BLOCK 12 — get_anchor_date()

# Now we reach one of the most important functions in your entire project.

# def get_anchor_date(con) -> date:

# This function answers:

# "What date should the application consider as the latest date in the dataset?"

# Why does the application need this?

# Because your data is historical.

# Your dataset might end on:

# 2024-05-31

# But your computer's real date could be:

# 2026-09-04

# If the application used:

# date.today()

# for everything, then:

# "last month"

# would mean August 2026.

# But there may be no August 2026 data.

# Therefore your application finds the latest date actually present in the dataset.

# That becomes the anchor date.