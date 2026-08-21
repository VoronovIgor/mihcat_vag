#!/usr/bin/env python3
"""
Import ebay1.csv and ebay2.csv into MySQL database `my-k-type`.
Tables: vehicles, hsn_tsn, engine_codes
"""

import csv
import re
import sys
import mysql.connector

DB_CONFIG = {
    'host':     '127.0.0.1',
    'port':     8674,
    'user':     'root',
    'password': 'rootpassword',
    'database': 'my-k-type',
    'charset':  'utf8mb4',
}

FILES = {
    'ebay1': '/var/docker/mihcat/vag/source for K-Type/ebay1.csv',
    'ebay2': '/var/docker/mihcat/vag/source for K-Type/ebay2.csv',
}


def parse_period(period_str):
    """'1996/08-2002/10' → ('1996/08', '2002/10')"""
    if '-' in period_str:
        parts = period_str.split('-', 1)
        return parts[0].strip(), parts[1].strip()
    return period_str.strip(), ''


def parse_engine(engine_str):
    """'1896 ccm, 66 KW, 90 PS' → (1896, 66, 90)"""
    cc = kw = ps = None
    m = re.search(r'(\d+)\s*ccm', engine_str)
    if m: cc = int(m.group(1))
    m = re.search(r'(\d+)\s*KW', engine_str)
    if m: kw = int(m.group(1))
    m = re.search(r'(\d+)\s*PS', engine_str)
    if m: ps = int(m.group(1))
    return cc, kw, ps


def split_hsn_tsn(raw):
    """'7593|387<>7593|418' → [('7593','387'), ('7593','418')]"""
    pairs = []
    for entry in raw.split('<>'):
        entry = entry.strip()
        if '|' in entry:
            parts = entry.split('|', 1)
            pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


def split_engine_codes(raw):
    """'1Z,AGR,AHU,ALH' → ['1Z', 'AGR', 'AHU', 'ALH']"""
    return [c.strip() for c in raw.split(',') if c.strip()]


def import_file(cur, source, filepath):
    print(f"[{source}] Reading {filepath} ...")
    rows_v = []
    rows_h = []
    rows_e = []

    with open(filepath, encoding='utf-8-sig') as f:
        reader = csv.reader(f, delimiter=';')
        header = next(reader)

        for row in reader:
            if not row or not row[0].strip():
                continue

            k_type      = int(row[0].strip())
            brand       = row[1].strip() if len(row) > 1 else ''
            model       = row[2].strip() if len(row) > 2 else ''
            type_name   = row[3].strip() if len(row) > 3 else ''
            platform    = row[4].strip() if len(row) > 4 else ''
            period      = row[5].strip() if len(row) > 5 else ''
            engine_str  = row[6].strip() if len(row) > 6 else ''
            hsn_raw     = row[7].strip() if len(row) > 7 else ''
            years_str   = row[9].strip() if len(row) > 9 else ''

            # engine codes: ebay1 has them in col 13 (index 13), fallback col 10
            eng_codes_raw = ''
            if source == 'ebay1':
                eng_codes_raw = row[13].strip() if len(row) > 13 else ''
                if not eng_codes_raw:
                    eng_codes_raw = row[10].strip() if len(row) > 10 else ''

            year_from, year_to = parse_period(period)
            cc, kw, ps = parse_engine(engine_str)
            years_count = int(years_str) if years_str.isdigit() else None

            rows_v.append((
                k_type, source, brand, model, type_name, platform,
                year_from, year_to, cc, kw, ps, years_count,
                hsn_raw, eng_codes_raw
            ))

            for hsn, tsn in split_hsn_tsn(hsn_raw):
                rows_h.append((k_type, hsn, tsn))

            if source == 'ebay1':
                for code in split_engine_codes(eng_codes_raw):
                    rows_e.append((k_type, code))

    # Insert vehicles
    cur.executemany("""
        INSERT INTO vehicles
            (k_type, source, brand, model, type_name, platform,
             year_from, year_to, engine_cc, engine_kw, engine_ps, years_count,
             hsn_tsn_raw, engine_codes_raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, rows_v)
    print(f"[{source}] vehicles: {len(rows_v)} rows")

    # Insert hsn_tsn
    if rows_h:
        cur.executemany(
            "INSERT INTO hsn_tsn (k_type, hsn, tsn) VALUES (%s,%s,%s)",
            rows_h
        )
        print(f"[{source}] hsn_tsn:  {len(rows_h)} rows")

    # Insert engine_codes
    if rows_e:
        cur.executemany(
            "INSERT INTO engine_codes (k_type, engine_code) VALUES (%s,%s)",
            rows_e
        )
        print(f"[{source}] engine_codes: {len(rows_e)} rows")


def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor()

    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    cur.execute("TRUNCATE TABLE engine_codes")
    cur.execute("TRUNCATE TABLE hsn_tsn")
    cur.execute("TRUNCATE TABLE vehicles")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")

    for source, filepath in FILES.items():
        import_file(cur, source, filepath)
        conn.commit()

    # Summary
    cur.execute("SELECT source, COUNT(*) FROM vehicles GROUP BY source")
    print("\n=== Summary ===")
    for row in cur.fetchall():
        print(f"  vehicles [{row[0]}]: {row[1]}")
    cur.execute("SELECT COUNT(*) FROM hsn_tsn")
    print(f"  hsn_tsn total:      {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM engine_codes")
    print(f"  engine_codes total: {cur.fetchone()[0]}")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
