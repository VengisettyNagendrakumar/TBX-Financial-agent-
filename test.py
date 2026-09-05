import duckdb, config, os

con = duckdb.connect(":memory:")
for t, m in config.SCHEMA_CONFIG.items():
    p = os.path.join(config.DATA_DIR, m["file"]).replace("\\", "/")
    con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_csv_auto('{p}');")

# Add 40 synthetic vendors, each with one $1,000 payout in May.
con.execute("""
INSERT INTO vendors
SELECT 'V9' || i, 'Filler Vendor ' || i, 'Misc' FROM range(1, 41) t(i)
""")
con.execute("""
INSERT INTO vendor_payouts
SELECT 'PAY-9' || i, DATE '2024-05-15', 'V9' || i, 1000.00, 'USD', 'Completed', 'filler'
FROM range(1, 41) t(i)
""")

con.close()
