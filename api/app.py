"""
VAG EPC REST API — порт 8572
Аутентификация: заголовок X-Api-Key (или ?api_key=...)
Swagger UI: GET /docs

Parts:
  GET /api/v1/part/<number>                        — информация о детали (название, статус)
  GET /api/v1/part/<number>/cross                  — OEM-кросс номера
  GET /api/v1/part/<number>/supersessions          — цепочка замен (старые и новые номера)
  GET /api/v1/part/<number>/vehicles               — автомобили, использующие деталь
  GET /api/v1/part/<number>/price                  — прайс (VAG ETKA)
  GET /api/v1/part/<number>/images                 — URL фото и схем

Vehicles:
  GET /api/v1/vehicles                             — поиск моделей авто
  GET /api/v1/catalogs                             — список каталогов (AU, VW, SK, SE, PO, ML)

Catalog navigation (по структуре каталога):
  GET /api/v1/vehicle/<epis_typ>/groups            — главные группы (Hauptgruppen)
  GET /api/v1/vehicle/<epis_typ>/subgroups         — подгруппы/схемы (?catalog=AU&hg=1)
  GET /api/v1/vehicle/<epis_typ>/parts             — запчасти схемы (?catalog=AU&hg_ug=11&bildtafel2=110010)

VIN:
  GET /api/v1/vin/<vin>                            — расшифровка VIN (модели, комплектации)
"""

import os
import re
import secrets
import sqlite3
from functools import wraps

import mysql.connector
import requests
from flask import Flask, request, jsonify, Response, g

app = Flask(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
DB_CFG = {
    "host":     os.environ.get("MYSQL_HOST",     "mysql"),
    "port":     int(os.environ.get("MYSQL_PORT", "3306")),
    "user":     os.environ.get("MYSQL_USER",     "epc"),
    "password": os.environ.get("MYSQL_PASSWORD", "epc"),
    "database": os.environ.get("MYSQL_DB",       "vag_petka"),
    "charset":  "utf8mb4",
}

KEYS_DB   = os.environ.get("KEYS_DB",   "/data/vag_api_keys.sqlite")
PHP_URL   = os.environ.get("PHP_URL",   "http://nginx")   # nginx service in docker network
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://77.42.112.5:8572")

DEFAULT_LANG  = "DE"
DEFAULT_LIMIT = 100
MAX_LIMIT     = 500

MARKT_LABELS = {
    "BR": "Brazil", "CA": "FAW-VW", "CN": "FAW-VW", "CZ": "Local",
    "E": "Local", "MEX": "Mexico", "RA": "Argentina", "RDW": "Local",
    "SVW": "SAIC VW", "USA": "USA", "ZA": "South Africa",
}

CATALOGS = {
    "AU": "Audi", "VW": "Volkswagen", "SK": "Škoda",
    "SE": "SEAT", "PO": "Porsche", "ML": "VW Commercial",
}

# ─── Keys DB (SQLite) ────────────────────────────────────────────────────────
def _init_keys_db():
    os.makedirs(os.path.dirname(KEYS_DB), exist_ok=True)
    con = sqlite3.connect(KEYS_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value   TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            active      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            last_used   TEXT,
            requests    INTEGER DEFAULT 0
        )
    """)
    con.commit()
    # Create a default key if table is empty
    cur = con.execute("SELECT COUNT(*) FROM api_keys")
    if cur.fetchone()[0] == 0:
        default_key = "vag-" + secrets.token_hex(16)
        con.execute(
            "INSERT INTO api_keys (key_value, description) VALUES (?, ?)",
            (default_key, "default")
        )
        con.commit()
        print(f"[VAG API] Default API key created: {default_key}", flush=True)
    con.close()


def _check_api_key(key: str) -> bool:
    con = sqlite3.connect(KEYS_DB)
    cur = con.execute(
        "SELECT id FROM api_keys WHERE key_value=? AND active=1", (key,)
    )
    row = cur.fetchone()
    if row:
        con.execute(
            "UPDATE api_keys SET last_used=datetime('now'), requests=requests+1 WHERE key_value=?",
            (key,)
        )
        con.commit()
    con.close()
    return row is not None


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = (request.headers.get("X-Api-Key") or
               request.args.get("api_key", "")).strip()
        if not key:
            return jsonify({"error": "API key required",
                            "hint": "Pass X-Api-Key header or ?api_key=..."}), 401
        if not _check_api_key(key):
            return jsonify({"error": "Invalid or revoked API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ─── MySQL helpers ───────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = mysql.connector.connect(**DB_CFG)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def qry(sql: str, params=None) -> list[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def qry_one(sql: str, params=None) -> dict | None:
    rows = qry(sql, params)
    return rows[0] if rows else None


# ─── OEM number normalisation ────────────────────────────────────────────────
_CYR_MAP = str.maketrans(
    "АВСРТКМНВХЕОТ",
    "ABCPTKMNBXEOT"
)

def normalize(raw: str) -> str:
    """Strip spaces/dashes/dots/slashes, uppercase, transliterate Cyrillic."""
    s = raw.upper().translate(_CYR_MAP)
    s = re.sub(r"[\s\-\./\\]", "", s)
    return s


def _int(v, default=DEFAULT_LIMIT):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _limit(val, mx=MAX_LIMIT, default=DEFAULT_LIMIT):
    return min(max(1, _int(val, default)), mx)


# ─── HEALTH ─────────────────────────────────────────────────────────────────
@app.route("/api/v1/health")
def health():
    try:
        qry_one("SELECT 1")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    return jsonify({"status": "ok", "db": db_status})


# ─── CATALOGS ───────────────────────────────────────────────────────────────
@app.route("/api/v1/catalogs")
@require_api_key
def catalogs():
    rows = qry("SELECT DISTINCT catalog FROM data_overview ORDER BY catalog")
    result = [
        {"catalog": r["catalog"], "brand": CATALOGS.get(r["catalog"].upper(), r["catalog"])}
        for r in rows
    ]
    return jsonify({"count": len(result), "results": result})


# ─── VEHICLES ───────────────────────────────────────────────────────────────
@app.route("/api/v1/vehicles")
@require_api_key
def vehicles():
    catalog  = (request.args.get("catalog", "") or "").upper().strip()
    markt    = (request.args.get("markt", "") or "").upper().strip()
    modell   = (request.args.get("modell", "") or "").strip()
    year     = request.args.get("year")
    search   = (request.args.get("q", "") or "").strip()
    limit    = _limit(request.args.get("limit"), default=100)

    where = ["t.epis_typ > 0"]
    params: list = []

    if catalog:
        where.append("t.catalog = %s")
        params.append(catalog)
    if markt:
        where.append("t.markt = %s")
        params.append(markt)
    if modell:
        where.append("t.modell = %s")
        params.append(modell)
    if year:
        where.append("t.einsatz = %s")
        params.append(int(year))
    if search:
        where.append("(m.bezeichnung LIKE %s OR t.vbz_tabelle LIKE %s)")
        params += [f"%{search}%", f"%{search}%"]

    sql = f"""
        SELECT
            t.catalog, t.markt, t.modell, t.epis_typ,
            m.bezeichnung,
            REPLACE(t.vbz_tabelle, ' || ', ',') AS vbz,
            MIN(t.einsatz) AS year_from,
            IF(MAX(t.einsatz) > YEAR(CURDATE()), '', MAX(t.einsatz)) AS year_to
        FROM data_overview t
        JOIN data_overview m
            ON m.catalog=t.catalog AND m.markt=t.markt
            AND m.modell=t.modell AND m.epis_typ=0
        WHERE {' AND '.join(where)}
        GROUP BY t.catalog, t.markt, t.modell, t.epis_typ, m.bezeichnung, t.vbz_tabelle
        ORDER BY t.catalog, t.modell, year_from
        LIMIT %s
    """
    params.append(limit)
    rows = qry(sql, params)

    results = [
        {
            "catalog":    r["catalog"],
            "brand":      CATALOGS.get(r["catalog"].upper(), r["catalog"]),
            "markt":      r["markt"],
            "markt_name": MARKT_LABELS.get(r["markt"], r["markt"]),
            "modell":     r["modell"],
            "epis_typ":   r["epis_typ"],
            "name":       r["bezeichnung"],
            "vbz":        r["vbz"],
            "year_from":  r["year_from"],
            "year_to":    r["year_to"] if r["year_to"] else None,
        }
        for r in rows
    ]
    return jsonify({"count": len(results), "limit": limit, "results": results})


# ─── VIN ─────────────────────────────────────────────────────────────────────
@app.route("/api/v1/vin/<vin>")
@require_api_key
def vin_lookup(vin: str):
    vin = vin.upper().strip()
    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", vin):
        return jsonify({"error": "Invalid VIN format (17 alphanumeric chars, no I/O/Q)"}), 400

    rows = qry(
        """
        SELECT catalog, modell, epis_typ, einsatz, auslauf, markt,
               vkbz, motorkennbuchstable AS engine_code,
               getriebekkenbuchstable AS gearbox_code,
               prod_date, model_year
        FROM all_vincode
        WHERE vin = %s
        ORDER BY catalog, einsatz
        """,
        (vin,)
    )

    results = [
        {
            "catalog":      r["catalog"],
            "brand":        CATALOGS.get(r["catalog"].upper(), r["catalog"]),
            "modell":       r["modell"],
            "epis_typ":     r["epis_typ"],
            "year_from":    r["einsatz"],
            "year_to":      r["auslauf"] or None,
            "markt":        r["markt"],
            "markt_name":   MARKT_LABELS.get(r["markt"], r["markt"]),
            "vkbz":         r["vkbz"],
            "engine_code":  r["engine_code"],
            "gearbox_code": r["gearbox_code"],
            "prod_date":    r["prod_date"],
            "model_year":   r["model_year"],
        }
        for r in rows
    ]
    return jsonify({"vin": vin, "count": len(results), "results": results})


# ─── PART — base info ────────────────────────────────────────────────────────
@app.route("/api/v1/part/<path:number>")
@require_api_key
def part_info(number: str):
    num = normalize(number)
    if not num:
        return jsonify({"error": "Empty part number"}), 400

    lang = (request.args.get("lang", DEFAULT_LANG) or DEFAULT_LANG).upper()[:2]

    # Match both plain (≤11) and extended (>11) part numbers
    if len(num) <= 11:
        cond = "(s.teilenummer = %s OR s.teilenummer LIKE %s)"
        params = [num, num.ljust(11) + "%"]
    else:
        cond = "(s.teilenummer = %s OR s.teilenummer = %s)"
        params = [num[:11].rstrip(), num]

    rows = qry(
        f"""
        SELECT s.catalog, s.teilenummer, s.markt,
               s.entfallkennzeichen, s.entfalldatum,
               s.gruppen_count,
               d.kurztext AS short_text,
               REPLACE(d.text, ' || ', ' / ') AS name_text,
               REPLACE(d.synonyme, ' || ', ', ') AS synonyme
        FROM data_stamm s
        LEFT JOIN data_06 d
            ON d.catalog=s.catalog AND d.ts=s.ts_benennung AND d.lang2=%s
        WHERE {cond}
        ORDER BY s.catalog, s.teilenummer, s.markt
        """,
        [lang] + params
    )

    if not rows:
        return jsonify({"error": "Part not found", "number": num}), 404

    STATUS = {"O": "active", "L": "superseded", "F": "discontinued", "M": "referenced"}

    results = [
        {
            "catalog":      r["catalog"],
            "brand":        CATALOGS.get(r["catalog"].upper(), r["catalog"]),
            "number":       r["teilenummer"],
            "markt":        r["markt"],
            "markt_name":   MARKT_LABELS.get(r["markt"], r["markt"]),
            "status":       STATUS.get(r["entfallkennzeichen"], r["entfallkennzeichen"]),
            "discontinued_date": r["entfalldatum"] or None,
            "has_replacements": (r["gruppen_count"] or 0) > 0,
            "name":         r["name_text"] or r["short_text"],
            "short_name":   r["short_text"],
            "synonyms":     r["synonyme"],
        }
        for r in rows
    ]

    return jsonify({
        "number":  num,
        "count":   len(results),
        "results": results,
    })


# ─── PART — cross OEM ────────────────────────────────────────────────────────
@app.route("/api/v1/part/<path:number>/cross")
@require_api_key
def part_cross(number: str):
    num = normalize(number)
    if not num:
        return jsonify({"error": "Empty part number"}), 400

    limit = _limit(request.args.get("limit"), default=200, mx=500)

    try:
        resp = requests.post(
            f"{PHP_URL}/api_oem_variants.php",
            json={"numbers": [num]},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({"error": "Cross OEM lookup failed", "detail": str(e)}), 503

    part_data = data.get(num, {}) if isinstance(data, dict) else {}
    cross_str = part_data.get("cross_oem", "")
    vbz_str   = part_data.get("verkaufsbezeichnung", "")

    # cross_oem is a comma-separated string of "num, formatted_num, ..."
    cross_nums = [c.strip() for c in cross_str.split(",") if c.strip()] if cross_str else []
    # Deduplicate while preserving order
    seen = set()
    unique_cross = []
    for c in cross_nums:
        nc = normalize(c)
        if nc and nc not in seen:
            seen.add(nc)
            unique_cross.append(c)

    results = [{"number": c} for c in unique_cross[:limit]]

    return jsonify({
        "number":               num,
        "count":                len(results),
        "limit":                limit,
        "verkaufsbezeichnung":  vbz_str,
        "results":              results,
    })


# ─── PART — applicable vehicles ──────────────────────────────────────────────
@app.route("/api/v1/part/<path:number>/vehicles")
@require_api_key
def part_vehicles(number: str):
    num   = normalize(number)
    limit = _limit(request.args.get("limit"), default=200)
    markt = (request.args.get("markt", "") or "").upper().strip()

    if not num:
        return jsonify({"error": "Empty part number"}), 400

    markt_filter = "AND ow_typ.markt = %s" if markt else ""
    params = [num]
    if markt:
        params.append(markt)
    params.append(limit)

    rows = qry(
        f"""
        SELECT
            ow_typ.catalog,
            ow_typ.modell,
            ow_typ.epis_typ,
            ow_mod.bezeichnung,
            REPLACE(ow_typ.vbz_tabelle, ' || ', ',') AS vbz,
            MIN(ow_typ.einsatz)  AS year_from,
            IF(MAX(ow_typ.einsatz) > YEAR(CURDATE()), '', MAX(ow_typ.einsatz)) AS year_to,
            GROUP_CONCAT(DISTINCT ow_typ.markt ORDER BY ow_typ.markt) AS markets,
            CONCAT(MIN(motor.liter),'-',MAX(motor.liter))   AS liter,
            CONCAT(MIN(motor.ps),'-',MAX(motor.ps))         AS ps,
            GROUP_CONCAT(DISTINCT motor.zylinder ORDER BY motor.zylinder) AS cylinders,
            GROUP_CONCAT(DISTINCT motor.motorkennbuchstabe
                         ORDER BY motor.motorkennbuchstabe) AS engine_codes
        FROM data_overview ow_typ
        JOIN data_overview ow_mod
            ON ow_mod.catalog=ow_typ.catalog AND ow_mod.markt=ow_typ.markt
            AND ow_mod.modell=ow_typ.modell  AND ow_mod.epis_typ=0
        JOIN (
            SELECT DISTINCT catalog, epis_typ, dir_name
            FROM data_kat WHERE teilenummer_suche = %s
        ) kat ON kat.catalog=ow_typ.catalog AND kat.epis_typ=ow_typ.epis_typ
        JOIN data_vb motor
            ON motor.catalog=ow_typ.catalog AND motor.dir_name=kat.dir_name
            AND motor.typ=ow_typ.epis_typ
            AND (CONCAT(ow_typ.einsatz,'12') >= motor.einsatz OR motor.einsatz='')
            AND (CONCAT(ow_typ.einsatz,'01') <= motor.auslauf OR motor.auslauf='')
        WHERE
            (ow_typ.markt <> 'USA' AND kat.dir_name='R')
            OR (ow_typ.markt='USA' AND kat.dir_name='U')
            {markt_filter}
        GROUP BY ow_typ.catalog, ow_typ.modell, ow_mod.bezeichnung, ow_typ.vbz_tabelle
        ORDER BY ow_typ.catalog, ow_typ.modell, year_from
        LIMIT %s
        """,
        params,
    )

    results = [
        {
            "catalog":      r["catalog"],
            "brand":        CATALOGS.get(r["catalog"].upper(), r["catalog"]),
            "modell":       r["modell"],
            "epis_typ":     r["epis_typ"],
            "name":         r["bezeichnung"],
            "vbz":          r["vbz"],
            "year_from":    r["year_from"],
            "year_to":      r["year_to"] if r["year_to"] else None,
            "markets":      r["markets"],
            "engine": {
                "liter":        r["liter"],
                "ps":           r["ps"],
                "cylinders":    r["cylinders"],
                "engine_codes": r["engine_codes"],
            },
        }
        for r in rows
    ]

    return jsonify({
        "number":  num,
        "count":   len(results),
        "limit":   limit,
        "results": results,
    })


# ─── PART — price ────────────────────────────────────────────────────────────
@app.route("/api/v1/part/<path:number>/price")
@require_api_key
def part_price(number: str):
    num     = normalize(number)
    catalog = (request.args.get("catalog", "") or "").upper().strip()

    if not num:
        return jsonify({"error": "Empty part number"}), 400

    where  = ["teilenummer = %s"]
    params = [num]
    if catalog:
        where.append("catalog = %s")
        params.append(catalog)

    rows = qry(
        f"""
        SELECT catalog, teilenummer, einsatzdatum, preis, rabattgruppe, schatzpreis, mwst
        FROM data_fpreis
        WHERE {' AND '.join(where)}
        ORDER BY catalog
        """,
        params,
    )

    results = [
        {
            "catalog":        r["catalog"],
            "brand":          CATALOGS.get(r["catalog"].upper(), r["catalog"]),
            "number":         r["teilenummer"],
            "price":          float(r["preis"]) if r["preis"] else None,
            "price_est":      float(r["schatzpreis"]) if r["schatzpreis"] else None,
            "discount_group": r["rabattgruppe"],
            "vat_pct":        float(r["mwst"]) if r["mwst"] else None,
            "valid_from":     str(r["einsatzdatum"]) if r["einsatzdatum"] else None,
        }
        for r in rows
    ]

    return jsonify({
        "number":  num,
        "count":   len(results),
        "results": results,
    })


# ─── PART — images ───────────────────────────────────────────────────────────
@app.route("/api/v1/part/<path:number>/images")
@require_api_key
def part_images(number: str):
    num = normalize(number)
    if not num:
        return jsonify({"error": "Empty part number"}), 400

    vag_base = os.environ.get("VAG_BASE_URL", "http://77.42.112.5:3795")

    try:
        resp = requests.post(
            f"{PHP_URL}/tapino_enrich.php?format=json",
            json={"parts": [{"number": num, "numberUnformatted": num}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        foto = data.get("parts", [{}])[0].get("foto", [])
        # Rewrite internal URLs to public ones
        public_foto = [
            re.sub(r"http://nginx", vag_base, url)
            for url in foto
        ]
    except Exception as e:
        public_foto = []

    return jsonify({
        "number": num,
        "count":  len(public_foto),
        "results": [{"url": u} for u in public_foto],
    })


# ─── PART — supersessions (замены) ───────────────────────────────────────────
@app.route("/api/v1/part/<path:number>/supersessions")
@require_api_key
def part_supersessions(number: str):
    num = normalize(number)
    if not num:
        return jsonify({"error": "Empty part number"}), 400

    lang = (request.args.get("lang", DEFAULT_LANG) or DEFAULT_LANG).upper()[:2]

    # Current part rows with gruppen (contains successor numbers)
    if len(num) <= 11:
        cond  = "(s.teilenummer = %s OR s.teilenummer LIKE %s)"
        cparams = [num, num.ljust(11) + "%"]
    else:
        cond  = "(s.teilenummer = %s OR s.teilenummer = %s)"
        cparams = [num[:11].rstrip(), num]

    rows = qry(
        f"""
        SELECT s.catalog, s.teilenummer, s.markt,
               s.entfallkennzeichen, s.entfalldatum,
               s.gruppen_count, s.gruppen
        FROM data_stamm s
        WHERE {cond}
        ORDER BY s.catalog, s.teilenummer, s.markt
        """,
        cparams,
    )

    STATUS = {"O": "active", "L": "superseded", "F": "discontinued", "M": "referenced"}

    # Parse successors from gruppen field (format: num~qty~ts_bem~ts_moa~bem~moa || ...)
    successors = []
    seen_succ: set = set()
    for r in rows:
        if not r["gruppen_count"] or not r["gruppen"]:
            continue
        for entry in r["gruppen"].split(" || "):
            parts_f = entry.split("~")
            succ_num = parts_f[0].strip() if parts_f else ""
            if not succ_num:
                continue
            key = (r["catalog"], succ_num)
            if key in seen_succ:
                continue
            seen_succ.add(key)
            qty  = parts_f[1].strip() if len(parts_f) > 1 else ""
            note = parts_f[4].strip() if len(parts_f) > 4 else ""
            successors.append({
                "number":  succ_num,
                "catalog": r["catalog"],
                "qty":     qty or None,
                "note":    note or None,
            })

    # Predecessors: parts that list our number in their gruppen field (FULLTEXT)
    predecessors = []
    try:
        ft_query = " ".join("+" + w for w in num.split())
        pred_rows = qry(
            """
            SELECT s.catalog, s.teilenummer, s.markt,
                   s.entfallkennzeichen, s.entfalldatum, s.gruppen,
                   d.text AS name
            FROM data_stamm s
            LEFT JOIN data_06 d
                ON d.catalog = s.catalog AND d.ts = s.ts_benennung AND d.lang2 = %s
            WHERE MATCH(s.gruppen) AGAINST (%s IN BOOLEAN MODE)
            ORDER BY s.catalog, s.teilenummer, s.markt
            """,
            [lang, ft_query],
        )
        seen_pred: set = set()
        for r in pred_rows:
            pnum = r["teilenummer"].strip()
            key  = (r["catalog"], pnum)
            if key in seen_pred:
                continue
            # Verify our number actually appears in gruppen (FULLTEXT can overmatch)
            if r["gruppen"]:
                grp_nums = {
                    normalize(e.split("~")[0].strip())
                    for e in r["gruppen"].split(" || ")
                    if e.split("~")[0].strip()
                }
                if num not in grp_nums:
                    continue
            seen_pred.add(key)
            predecessors.append({
                "number":            pnum,
                "catalog":           r["catalog"],
                "name":              r["name"],
                "status":            STATUS.get(r["entfallkennzeichen"], r["entfallkennzeichen"]),
                "discontinued_date": r["entfalldatum"] or None,
            })
    except Exception:
        pass  # if FULLTEXT index missing, just return no predecessors

    return jsonify({
        "number":       num,
        "successors":   successors,
        "predecessors": predecessors,
    })


# ─── VEHICLE — main groups (Hauptgruppen) ────────────────────────────────────
@app.route("/api/v1/vehicle/<int:epis_typ>/groups")
@require_api_key
def vehicle_groups(epis_typ: int):
    catalog = (request.args.get("catalog", "") or "").upper().strip()
    markt   = (request.args.get("markt", "RDW") or "RDW").upper().strip()
    lang    = (request.args.get("lang", DEFAULT_LANG) or DEFAULT_LANG).upper()[:2]

    if not catalog:
        return jsonify({"error": "catalog parameter required (AU/VW/SK/SE/PO/ML)"}), 400

    # Get hauptgruppen_tabelle for this vehicle (prefer requested markt, fallback any)
    ov = qry_one(
        """
        SELECT hauptgruppen_tabelle
        FROM data_overview
        WHERE catalog = %s AND epis_typ = %s
        ORDER BY IF(markt = %s, 0, 1), markt
        LIMIT 1
        """,
        [catalog, epis_typ, markt],
    )
    if not ov:
        return jsonify({"error": "Vehicle type not found", "epis_typ": epis_typ}), 404

    # hauptgruppen_tabelle is a string of single-char group codes, e.g. "1234567"
    hg_letters = list(dict.fromkeys(ov["hauptgruppen_tabelle"] or ""))
    hg_letters = [c for c in hg_letters if c.strip()]

    if not hg_letters:
        return jsonify({"epis_typ": epis_typ, "catalog": catalog, "count": 0, "results": []})

    placeholders = ", ".join(["%s"] * len(hg_letters))
    hg_rows = qry(
        f"""
        SELECT h.hg, d.text AS name, d.kurztext AS short_name
        FROM data_hg h
        LEFT JOIN data_06 d
            ON d.catalog = h.catalog AND d.ts = h.hgts AND d.lang2 = %s
        WHERE h.catalog = %s AND h.hg IN ({placeholders})
        """,
        [lang, catalog] + hg_letters,
    )
    hg_by_code = {r["hg"]: r for r in hg_rows}

    results = [
        {
            "hg":   c,
            "name": (hg_by_code[c]["name"] or hg_by_code[c]["short_name"]) if c in hg_by_code else None,
        }
        for c in hg_letters
        if c in hg_by_code
    ]
    return jsonify({
        "epis_typ": epis_typ,
        "catalog":  catalog,
        "count":    len(results),
        "results":  results,
    })


# ─── VEHICLE — subgroups (Untergruppen/Bildtafeln) ────────────────────────────
@app.route("/api/v1/vehicle/<int:epis_typ>/subgroups")
@require_api_key
def vehicle_subgroups(epis_typ: int):
    catalog  = (request.args.get("catalog", "") or "").upper().strip()
    hg       = (request.args.get("hg", "") or "").strip()         # main group letter, e.g. "1"
    dir_name = (request.args.get("dir_name", "R") or "R").strip()

    if not catalog:
        return jsonify({"error": "catalog parameter required"}), 400

    where  = ["catalog = %s", "dir_name = %s", "epis_typ = %s", "bildtafel2 > 0"]
    params: list = [catalog, dir_name, str(epis_typ)]

    if hg:
        where.append("hg_ug LIKE %s")
        params.append(hg + "%")

    rows = qry(
        f"""
        SELECT DISTINCT hg_ug, bildtafel2
        FROM data_kat
        WHERE {' AND '.join(where)}
        ORDER BY hg_ug, bildtafel2
        LIMIT 500
        """,
        params,
    )

    results = [
        {
            "hg":         r["hg_ug"][0] if r["hg_ug"] else "",
            "hg_ug":      r["hg_ug"],
            "bildtafel2": r["bildtafel2"],
        }
        for r in rows
    ]
    return jsonify({
        "epis_typ": epis_typ,
        "catalog":  catalog,
        "hg":       hg or None,
        "count":    len(results),
        "results":  results,
    })


# ─── VEHICLE — parts in a diagram (Bildtafel) ────────────────────────────────
@app.route("/api/v1/vehicle/<int:epis_typ>/parts")
@require_api_key
def vehicle_parts(epis_typ: int):
    catalog    = (request.args.get("catalog", "") or "").upper().strip()
    hg_ug      = (request.args.get("hg_ug", "") or "").strip()
    bildtafel2 = request.args.get("bildtafel2")
    dir_name   = (request.args.get("dir_name", "R") or "R").strip()
    lang       = (request.args.get("lang", DEFAULT_LANG) or DEFAULT_LANG).upper()[:2]

    if not catalog:
        return jsonify({"error": "catalog parameter required"}), 400
    if not hg_ug or not bildtafel2:
        return jsonify({"error": "hg_ug and bildtafel2 are required"}), 400

    rows = qry(
        """
        SELECT k.id, k.teilenummer, k.teilenummer_suche, k.uou,
               k.bild_text_position, k.einsatzdatum, k.auslaufdatum,
               k.hg_ug, k.bildtafel2,
               SUBSTRING_INDEX(k.stk, ' || ', 1)       AS qty,
               REPLACE(k.benennung, ' || ', ' / ')      AS name_raw,
               REPLACE(k.bemerkung, ' || ', ' / ')      AS note_raw,
               d.text                                    AS name_ts
        FROM data_kat k
        LEFT JOIN data_06 d
            ON d.catalog = k.catalog AND d.lang2 = %s
            AND k.tsben != '' AND d.ts = SUBSTRING_INDEX(k.tsben, ' || ', 1)
        WHERE k.catalog = %s AND k.dir_name = %s
          AND k.epis_typ = %s AND k.hg_ug = %s AND k.bildtafel2 = %s
        ORDER BY k.id
        LIMIT 2000
        """,
        [lang, catalog, dir_name, str(epis_typ), hg_ug, bildtafel2],
    )

    results = []
    for r in rows:
        num = r["teilenummer"].strip() if r["teilenummer"] else None
        results.append({
            "id":           r["id"],
            "position":     r["bild_text_position"] or None,
            "number":       num,
            "number_search": r["teilenummer_suche"] or None,
            "uou":          r["uou"],
            "qty":          r["qty"] or None,
            "name":         r["name_ts"] or r["name_raw"] or None,
            "note":         r["note_raw"] or None,
            "date_from":    str(r["einsatzdatum"]) if r["einsatzdatum"] else None,
            "date_to":      str(r["auslaufdatum"]) if r["auslaufdatum"] else None,
        })

    return jsonify({
        "epis_typ":   epis_typ,
        "catalog":    catalog,
        "hg_ug":      hg_ug,
        "bildtafel2": bildtafel2,
        "count":      len(results),
        "results":    results,
    })


# ─── DOCS (Swagger UI) ───────────────────────────────────────────────────────
OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "VAG EPC REST API",
        "version": "1.0.0",
        "description": (
            "REST API для каталога запчастей VAG ETKA. "
            "Данные: Audi, Volkswagen, Škoda, SEAT, Porsche, VW Commercial.\n\n"
            "Аутентификация: заголовок `X-Api-Key` или параметр `?api_key=`."
        ),
    },
    "servers": [{"url": PUBLIC_URL}],
    "components": {
        "securitySchemes": {
            "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "ApiKeyQuery":  {"type": "apiKey", "in": "query",  "name": "api_key"},
        }
    },
    "security": [{"ApiKeyHeader": []}, {"ApiKeyQuery": []}],
    "paths": {
        "/api/v1/health": {
            "get": {
                "summary": "Health check",
                "security": [],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/v1/catalogs": {
            "get": {
                "summary": "Список каталогов (AU, VW, SK, SE, PO, ML)",
                "responses": {"200": {"description": "Список каталогов"}},
            }
        },
        "/api/v1/vehicles": {
            "get": {
                "summary": "Поиск моделей автомобилей",
                "parameters": [
                    {"name": "catalog", "in": "query", "schema": {"type": "string"}, "description": "AU / VW / SK / SE / PO / ML"},
                    {"name": "markt",   "in": "query", "schema": {"type": "string"}, "description": "Рынок: RDW / USA / BR ..."},
                    {"name": "modell",  "in": "query", "schema": {"type": "string"}, "description": "Код модели (напр. GOLF)"},
                    {"name": "year",    "in": "query", "schema": {"type": "integer"}, "description": "Год выпуска"},
                    {"name": "q",       "in": "query", "schema": {"type": "string"}, "description": "Поиск по названию"},
                    {"name": "limit",   "in": "query", "schema": {"type": "integer", "default": 100}},
                ],
                "responses": {"200": {"description": "Список моделей"}},
            }
        },
        "/api/v1/vin/{vin}": {
            "get": {
                "summary": "Расшифровка VIN (17 символов)",
                "parameters": [{"name": "vin", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "Список моделей/комплектаций для VIN"},
                    "400": {"description": "Неверный формат VIN"},
                },
            }
        },
        "/api/v1/part/{number}": {
            "get": {
                "summary": "Информация о детали (название, каталог, статус)",
                "parameters": [
                    {"name": "number", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Номер детали (OEM)"},
                    {"name": "lang",   "in": "query", "schema": {"type": "string", "default": "DE"}, "description": "Язык названия: DE / EN / RU"},
                ],
                "responses": {
                    "200": {"description": "Информация о детали"},
                    "404": {"description": "Деталь не найдена"},
                },
            }
        },
        "/api/v1/part/{number}/cross": {
            "get": {
                "summary": "OEM-кросс номера (все взаимозаменяемые номера)",
                "parameters": [
                    {"name": "number", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "limit",  "in": "query", "schema": {"type": "integer", "default": 200}},
                ],
                "responses": {"200": {"description": "Список кросс-номеров"}},
            }
        },
        "/api/v1/part/{number}/vehicles": {
            "get": {
                "summary": "Автомобили, использующие эту деталь",
                "parameters": [
                    {"name": "number", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "markt",  "in": "query", "schema": {"type": "string"}, "description": "Фильтр по рынку (RDW, USA, BR...)"},
                    {"name": "limit",  "in": "query", "schema": {"type": "integer", "default": 200}},
                ],
                "responses": {"200": {"description": "Список автомобилей"}},
            }
        },
        "/api/v1/part/{number}/price": {
            "get": {
                "summary": "Цена детали из ETKA",
                "parameters": [
                    {"name": "number",  "in": "path",  "required": True, "schema": {"type": "string"}},
                    {"name": "catalog", "in": "query", "schema": {"type": "string"}, "description": "AU / VW / SK ..."},
                ],
                "responses": {"200": {"description": "Цена"}},
            }
        },
        "/api/v1/part/{number}/images": {
            "get": {
                "summary": "Фото и схемы детали (с хотспотом)",
                "parameters": [
                    {"name": "number", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Список URL изображений"}},
            }
        },
        "/api/v1/part/{number}/supersessions": {
            "get": {
                "summary": "Цепочка замен детали (старые и новые номера)",
                "parameters": [
                    {"name": "number", "in": "path", "required": True, "schema": {"type": "string"}, "description": "OEM номер детали"},
                    {"name": "lang",   "in": "query", "schema": {"type": "string", "default": "DE"}, "description": "Язык: DE / EN"},
                ],
                "responses": {
                    "200": {"description": "predecessors (старые номера) и successors (новые номера)"},
                },
            }
        },
        "/api/v1/vehicle/{epis_typ}/groups": {
            "get": {
                "summary": "Главные группы (Hauptgruppen) для типа авто",
                "parameters": [
                    {"name": "epis_typ", "in": "path",  "required": True, "schema": {"type": "integer"}, "description": "Код типа авто (epis_typ)"},
                    {"name": "catalog",  "in": "query", "required": True, "schema": {"type": "string"},  "description": "AU / VW / SK / SE / PO / ML"},
                    {"name": "markt",    "in": "query", "schema": {"type": "string", "default": "RDW"}, "description": "Рынок (влияет на приоритет выбора)"},
                    {"name": "lang",     "in": "query", "schema": {"type": "string", "default": "DE"}},
                ],
                "responses": {"200": {"description": "Список главных групп с названиями"}},
            }
        },
        "/api/v1/vehicle/{epis_typ}/subgroups": {
            "get": {
                "summary": "Подгруппы/схемы каталога для типа авто",
                "parameters": [
                    {"name": "epis_typ", "in": "path",  "required": True, "schema": {"type": "integer"}},
                    {"name": "catalog",  "in": "query", "required": True, "schema": {"type": "string"}, "description": "AU / VW / SK ..."},
                    {"name": "hg",       "in": "query", "schema": {"type": "string"}, "description": "Фильтр по главной группе (1 символ, напр. '1')"},
                    {"name": "dir_name", "in": "query", "schema": {"type": "string", "default": "R"}, "description": "R = правый руль / U = USA"},
                ],
                "responses": {"200": {"description": "Список подгрупп (hg_ug + bildtafel2)"}},
            }
        },
        "/api/v1/vehicle/{epis_typ}/parts": {
            "get": {
                "summary": "Запчасти конкретной схемы (Bildtafel) авто",
                "parameters": [
                    {"name": "epis_typ",   "in": "path",  "required": True, "schema": {"type": "integer"}},
                    {"name": "catalog",    "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "hg_ug",      "in": "query", "required": True, "schema": {"type": "string"}, "description": "Код группы-подгруппы (напр. '11', '53')"},
                    {"name": "bildtafel2", "in": "query", "required": True, "schema": {"type": "integer"}, "description": "ID схемы из /subgroups"},
                    {"name": "dir_name",   "in": "query", "schema": {"type": "string", "default": "R"}},
                    {"name": "lang",       "in": "query", "schema": {"type": "string", "default": "DE"}},
                ],
                "responses": {"200": {"description": "Список позиций с номерами запчастей"}},
            }
        },
    },
}

SWAGGER_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>VAG EPC API — Swagger UI</title>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
  SwaggerUIBundle({
    url: '/openapi.json',
    dom_id: '#swagger-ui',
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    layout: 'BaseLayout',
    deepLinking: true,
  });
</script>
</body>
</html>"""


@app.route("/docs")
def docs():
    return SWAGGER_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/openapi.json")
def openapi():
    import json
    return Response(json.dumps(OPENAPI_SPEC, ensure_ascii=False, indent=2),
                    content_type="application/json; charset=utf-8")


@app.route("/")
def index():
    return jsonify({
        "service": "VAG EPC REST API",
        "version": "1.0.0",
        "docs":    f"{PUBLIC_URL}/docs",
        "health":  f"{PUBLIC_URL}/api/v1/health",
    })


# ─── Init ────────────────────────────────────────────────────────────────────
_init_keys_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
