"""
Synthetic Financial Dataset Generator
Generates realistic financial data reflecting the TBX - BVP Hackathon Problem Statement:
- vendor_list.csv (with intentional variants and ambiguous names)
- chart_of_accounts.csv (income/expense/asset accounts)
- transactions.csv (ledger entries across 4 months)
- vendor_payouts.csv (outbound payouts across all months + intentional spend spike anomaly)
- reconciliation_status.csv (reconciliation states: Reconciled, Unreconciled, Pending, Disputed)
"""

import os
import random
import pandas as pd
from datetime import datetime, timedelta

def generate_datasets(output_dir="data", seed=42):
    random.seed(seed)
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. Vendor List (Includes tricky ambiguous variants)
    vendors = [
        {"vendor_id": "V001", "vendor_name": "Amazon Web Services, Inc.", "category": "Cloud Infrastructure"},
        {"vendor_id": "V002", "vendor_name": "Amazon Logistics LLC", "category": "Shipping & Fulfillment"},
        {"vendor_id": "V003", "vendor_name": "Acme Corporation", "category": "Enterprise Software"},
        {"vendor_id": "V004", "vendor_name": "CloudScale Technologies", "category": "DevOps & Monitoring"},
        {"vendor_id": "V005", "vendor_name": "Cloudflare Inc.", "category": "Security & CDN"},
        {"vendor_id": "V006", "vendor_name": "Stripe Payments", "category": "Payment Processing"},
        {"vendor_id": "V007", "vendor_name": "Salesforce.com Inc.", "category": "Sales & CRM"},
        {"vendor_id": "V008", "vendor_name": "Deloitte Advisory", "category": "Audit & Legal"},
        {"vendor_id": "V009", "vendor_name": "WeWork Global", "category": "Office & Facilities"},
        {"vendor_id": "V010", "vendor_name": "Google Cloud Platform", "category": "Cloud Infrastructure"},
        {"vendor_id": "V011", "vendor_name": "Datadog Inc.", "category": "Observability"},
        {"vendor_id": "V012", "vendor_name": "Slack Technologies", "category": "Productivity"}
    ]
    df_vendors = pd.DataFrame(vendors)
    df_vendors.to_csv(os.path.join(output_dir, "vendor_list.csv"), index=False)
    
    # 2. Chart of Accounts
    accounts = [
        {"account_id": "ACC-6001", "account_name": "Software & SaaS Subscriptions", "account_type": "Expense"},
        {"account_id": "ACC-6002", "account_name": "Hosting & Infrastructure", "account_type": "Expense"},
        {"account_id": "ACC-6003", "account_name": "Legal & Professional Fees", "account_type": "Expense"},
        {"account_id": "ACC-6004", "account_name": "Office Rent & Facilities", "account_type": "Expense"},
        {"account_id": "ACC-6005", "account_name": "Shipping & Logistics", "account_type": "Expense"},
        {"account_id": "ACC-6006", "account_name": "Payment Gateway Fees", "account_type": "Expense"},
        {"account_id": "ACC-2001", "account_name": "Accounts Payable", "account_type": "Liability"},
        {"account_id": "ACC-1001", "account_name": "Operating Cash Account", "account_type": "Asset"}
    ]
    df_accounts = pd.DataFrame(accounts)
    df_accounts.to_csv(os.path.join(output_dir, "chart_of_accounts.csv"), index=False)

    # 3. Vendor Payouts: Guarantee consistent distribution across Feb, Mar, Apr, and May 2024
    # Anchor date: 2024-05-31
    months = [
        (datetime(2024, 2, 1), datetime(2024, 2, 28)),
        (datetime(2024, 3, 1), datetime(2024, 3, 31)),
        (datetime(2024, 4, 1), datetime(2024, 4, 30)), # April (Last Month relative to May)
        (datetime(2024, 5, 1), datetime(2024, 5, 30))  # May (Current Month)
    ]

    payouts = []
    payout_id_counter = 1001 #used to generate ids pay-1001 pay-1002 etc
    
    baseline_spend = {
        "V001": (12000, 1500), # AWS ~12k (mean amount,std deviation)
        "V002": (4500, 800),   # Amazon Logistics ~4.5k
        "V003": (6200, 600),   # Acme Corp ~6.2k
        "V004": (3500, 400),   # CloudScale ~3.5k
        "V005": (1800, 200),   # Cloudflare ~1.8k
        "V006": (8500, 1200),  # Stripe ~8.5k
        "V007": (9000, 500),   # Salesforce ~9k
        "V008": (15000, 3000), # Deloitte ~15k
        "V009": (11000, 500),  # WeWork ~11k
        "V010": (7000, 1000),  # GCP ~7k
        "V011": (2200, 300),   # Datadog ~2.2k
        "V012": (1500, 150)    # Slack ~1.5k
    }

    # Ensure EVERY vendor has payouts in EVERY month (Feb, Mar, Apr, May)
    for vendor in vendors:
        v_id = vendor["vendor_id"]
        mean_amt, std_amt = baseline_spend[v_id]
        
        for m_start, m_end in months: #for every vendor the 4 motnths 
            m_days = (m_end - m_start).days
            # 1 to 2 payouts per month
            for _ in range(random.randint(1, 2)):
                p_date = m_start + timedelta(days=random.randint(1, m_days)) #create a random date in the month i.e April 1 + 12 days
                amt = round(max(300.0, random.gauss(mean_amt, std_amt)), 2) #random.gauss(mean_amt, std_amt) generates a random number from a Gaussian/normal distribution. might genrate 11,323
                status = random.choices(["Completed", "Pending", "Failed"], weights=[0.90, 0.07, 0.03])[0] #random.choices() returns a list. so we take the first element of the list to get the selected status.
                #[0.90, 0.07, 0.03] means in 100 90 are completed and 7 are pending 3 failed Because real financial systems generally don't have every payout in the same status.
                payouts.append({
                    "payout_id": f"PAY-{payout_id_counter}",
                    "payout_date": p_date.strftime("%Y-%m-%d"),
                    "vendor_id": v_id,
                    "amount": amt,
                    "currency": "USD",
                    "status": status,
                    "description": f"Payout to {vendor['vendor_name']} for monthly services"
                })
                payout_id_counter += 1

    # INTENTIONAL SPEND SPIKE (ANOMALY):
    # Acme Corp regular is ~6,200. $58,500 enterprise license spike on May 24, 2024!
    payouts.append({ #You intentionally inject an anomaly.Normal Acme spending: ~$6,200. Anomalous spike: $58,500 on May 24, 2024. so model will show that
        "payout_id": f"PAY-{payout_id_counter}",
        "payout_date": "2024-05-24",
        "vendor_id": "V003", # Acme Corp
        "amount": 58500.00,
        "currency": "USD",
        "status": "Completed",
        "description": "Acme Corp Enterprise Global License Renewal (Spike Anomaly)"
    })
    payout_id_counter += 1

    # Ensure AWS also has a specific payout on the anchor date (2024-05-31)
    payouts.append({  #It gives your application a known reference point for questions involving: current month, last month, and anchor date. This is useful for testing and validation.
        "payout_id": f"PAY-{payout_id_counter}",
        "payout_date": "2024-05-31",
        "vendor_id": "V001",
        "amount": 13420.50,
        "currency": "USD",
        "status": "Completed",
        "description": "AWS Monthly Compute charges for May"
    })
    payout_id_counter += 1

    df_payouts = pd.DataFrame(payouts).sort_values("payout_date")
    df_payouts.to_csv(os.path.join(output_dir, "vendor_payouts.csv"), index=False)

    # 4. General Transactions (Ledger Entries)
    transactions = []
    txn_id_counter = 50001
    
    vendor_account_map = { #it says Which accounting account should each vendor's transaction belong to?
                        #   V001 AWS
                        #     ↓
                        #     ACC-6002
                        #     ↓
                        #     Hosting & Infrastructure
        "V001": "ACC-6002", "V002": "ACC-6005", "V003": "ACC-6001",
        "V004": "ACC-6002", "V005": "ACC-6002", "V006": "ACC-6006",
        "V007": "ACC-6001", "V008": "ACC-6003", "V009": "ACC-6004",
        "V010": "ACC-6002", "V011": "ACC-6001", "V012": "ACC-6001"
    }

    for p in payouts:
        transactions.append({
            "transaction_id": f"TXN-{txn_id_counter}",
            "transaction_date": p["payout_date"],
            "vendor_id": p["vendor_id"],
            "account_id": vendor_account_map[p["vendor_id"]],
            "amount": p["amount"],
            "transaction_type": "Debit",
            "payout_id": p["payout_id"],
            "description": p["description"]
        })
        txn_id_counter += 1

    # Incidental transactions
    for m_start, m_end in months:
        m_days = (m_end - m_start).days
        for _ in range(15):
            t_date = m_start + timedelta(days=random.randint(1, m_days)) #Random date within the month.
            v_entry = random.choice(vendors)
            v_id = v_entry["vendor_id"]
            amt = round(random.uniform(200.0, 4500.0), 2) #Unlike the payout generation, you're using uniform(), which gives a value from a roughly uniform distribution across that range.
            transactions.append({
                "transaction_id": f"TXN-{txn_id_counter}",
                "transaction_date": t_date.strftime("%Y-%m-%d"),
                "vendor_id": v_id,
                "account_id": vendor_account_map[v_id],
                "amount": amt,
                "transaction_type": "Debit", #not every transcations are realted to vendors some transactions will not be vendors also suppose office expenses like that 
                "payout_id": None, #This transaction does not correspond to a vendor payout. payout and 
                "description": f"Incidental expense for {v_entry['vendor_name']}"
            })
            txn_id_counter += 1

    df_txns = pd.DataFrame(transactions).sort_values("transaction_date")
    df_txns.to_csv(os.path.join(output_dir, "transactions.csv"), index=False)

    # 5. Reconciliation Status
    reconciliations = []
    status_choices = ["Reconciled", "Unreconciled", "Pending", "Disputed"]
    status_weights = [0.70, 0.18, 0.08, 0.04]

    for t in transactions:
        is_recent_may = t["transaction_date"] >= "2024-05-15"
        if is_recent_may: #for recent transactions in May, we want to bias the status towards Pending or Unreconciled, since they may not have been reconciled yet. This simulates real-world scenarios where recent transactions are often still under review.
            st = random.choices(status_choices, weights=[0.25, 0.45, 0.22, 0.08])[0]
        else:
            st = random.choices(status_choices, weights=status_weights)[0]
            
        reconciliations.append({
            "transaction_id": t["transaction_id"],
            "reconciliation_status": st,
            "reconciled_date": (datetime.strptime(t["transaction_date"], "%Y-%m-%d") + timedelta(days=random.randint(1, 5))).strftime("%Y-%m-%d") if st == "Reconciled" else None,
            "notes": "Matched with bank feed" if st == "Reconciled" else ("Awaiting bank confirmation" if st == "Pending" else "Discrepancy in invoice total" if st == "Disputed" else "Unmatched in ledger")
        })

    df_recon = pd.DataFrame(reconciliations)
    df_recon.to_csv(os.path.join(output_dir, "reconciliation_status.csv"), index=False)

    print(f"Generated synthetic dataset successfully in '{output_dir}':")
    print(f"  - vendor_list.csv: {len(df_vendors)} vendors")
    print(f"  - chart_of_accounts.csv: {len(df_accounts)} accounts")
    print(f"  - vendor_payouts.csv: {len(df_payouts)} payouts")
    print(f"  - transactions.csv: {len(df_txns)} transactions")
    print(f"  - reconciliation_status.csv: {len(df_recon)} records")
    print(f"  - Anchor Date: {df_payouts['payout_date'].max()}")

if __name__ == "__main__":
    generate_datasets()
#anchor date as the "today" date of your dataset.Your generated financial data is historical data from around February → May 2024. The latest important date in the data is:2024-05-31So your application treats:2024-05-31 = today
# Reconciliation basically means: Checking whether a financial transaction has been properly matched/verified against the corresponding financial record.

#Why generate a reconciliation record for EVERY transaction?Your application needs to answer questions like: "Which transactions are still unreconciled?"

# For example, imagine your company paid AWS:

# Transaction

# Your company's bank/ledger says:

# AWS payment = $1,000

# Transaction:
# Amount = $1,000
# Vendor = AWS

# Now the company may have another record saying:

# AWS invoice/payout = $1,000

# That second record is what I meant by the matching record.

# Company transaction       AWS invoice/payout
#        $1,000       ↔          $1,000
#           │                       │
#           └────── MATCH ──────────┘

# Because the amounts and relevant details match, the transaction can be marked:

# Reconciled ✅


# Reconciled
# Transaction = $1,000
# Matching record = $1,000

# → Reconciled
# Unreconciled
# Transaction exists
# but matching record hasn't been found

# → Unreconciled
# Pending
# Matching/check is still being reviewed

# → Pending
# Disputed
# Something doesn't match
# and someone has flagged it

# → Disputed