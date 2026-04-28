#!/usr/bin/env python
"""Fix data integrity issues in SQLite before migration to PostgreSQL."""
import sqlite3
import sys

def fix_sqlite_data(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    print(f"Fixing data integrity issues in {db_path}...")
    
    # 1. Delete orphan system_components (product_id not in products)
    orphan_components = conn.execute("""
        SELECT sc.id, sc.system_id, sc.product_id 
        FROM system_components sc 
        LEFT JOIN products p ON sc.product_id = p.id 
        WHERE p.id IS NULL
    """).fetchall()
    
    if orphan_components:
        print(f"  Deleting {len(orphan_components)} orphan system_components...")
        for oc in orphan_components:
            print(f"    - id={oc['id']}, system_id={oc['system_id']}, product_id={oc['product_id']}")
        conn.execute("DELETE FROM system_components WHERE product_id NOT IN (SELECT id FROM products)")
        conn.commit()
    
    # 2. Fix pending_offers numeric overflow (waste_pct, margin_pct as REAL with too many decimals)
    # PostgreSQL has NUMERIC(5,4) which means 4 decimal places max
    bad_offers = conn.execute("""
        SELECT id, waste_pct, margin_pct FROM pending_offers 
        WHERE waste_pct > 1.0 OR margin_pct > 1.0
    """).fetchall()
    
    if bad_offers:
        print(f"  Fixing {len(bad_offers)} pending_offers with bad decimal values...")
        for bo in bad_offers:
            print(f"    - id={bo['id']}, waste_pct={bo['waste_pct']}, margin_pct={bo['margin_pct']}")
        # Cap values at 1.0 (100%)
        conn.execute("UPDATE pending_offers SET waste_pct = MIN(waste_pct, 1.0) WHERE waste_pct > 1.0")
        conn.execute("UPDATE pending_offers SET margin_pct = MIN(margin_pct, 1.0) WHERE margin_pct > 1.0")
        conn.commit()
    
    # 3. Delete orphan price_history (product_id not in products)
    orphan_history = conn.execute("""
        SELECT ph.id, ph.product_id 
        FROM price_history ph 
        LEFT JOIN products p ON ph.product_id = p.id 
        WHERE p.id IS NULL
    """).fetchall()
    
    if orphan_history:
        print(f"  Deleting {len(orphan_history)} orphan price_history records...")
        conn.execute("DELETE FROM price_history WHERE product_id NOT IN (SELECT id FROM products)")
        conn.commit()
    
    # 4. Delete orphan order_lines (offer_id not in pending_offers)
    orphan_lines = conn.execute("""
        SELECT ol.id, ol.offer_id 
        FROM order_lines ol 
        LEFT JOIN pending_offers po ON ol.offer_id = po.id 
        WHERE po.id IS NULL
    """).fetchall()
    
    if orphan_lines:
        print(f"  Deleting {len(orphan_lines)} orphan order_lines...")
        conn.execute("DELETE FROM order_lines WHERE offer_id NOT IN (SELECT id FROM pending_offers)")
        conn.commit()
    
    print("Done!")
    conn.close()

if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else './fassa_ops.db'
    fix_sqlite_data(db_path)
