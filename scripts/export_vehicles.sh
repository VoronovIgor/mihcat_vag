#!/bin/bash
# Export all vehicles from local VAG database (vag_petka) to CSV.
#
# Join logic:
#   data_overview (epis_typ > 0)  — vehicle type per market
#   data_overview (epis_typ = 0)  — human-readable model name (same catalog+modell+markt)
#   data_vb                       — engine variants (joined by catalog+typ=epis_typ)
#
# One row = one unique (model × market × engine variant).

CONTAINER="mysql_vag"
DB="vag_petka"
MYSQL_USER="root"
MYSQL_PASS="rootpassword"
OUTPUT="/var/docker/mihcat/vag/scripts/vehicles_export.csv"

read -r -d '' SQL << 'ENDSQL'
SELECT
    ov.catalog AS brand_code,
    CASE ov.catalog
        WHEN 'AU' THEN 'Audi'
        WHEN 'VW' THEN 'Volkswagen'
        WHEN 'SK' THEN 'Skoda'
        WHEN 'SE' THEN 'SEAT'
        WHEN 'PO' THEN 'Porsche'
        WHEN 'ML' THEN 'VW Truck'
        ELSE ov.catalog
    END                             AS brand_name,
    ov.modell                                        AS model_code,
    COALESCE(names.bezeichnung, '')                  AS model_name,
    REPLACE(ov.vbz_tabelle, ' || ', ',')             AS verkaufsbezeichnung,
    ov.epis_typ,
    ov.markt                                                          AS market,
    MIN(ov.einsatz)                                                   AS model_year_from,
    IF(MAX(ov.einsatz) > YEAR(CURDATE()), '', MAX(ov.einsatz))       AS model_year_to,
    vb.motorkennbuchstabe                                             AS engine_code,
    vb.liter * 10                                                     AS engine_cc,
    vb.kw                                                             AS engine_kw,
    vb.ps                                                             AS engine_ps,
    COALESCE(vb.zylinder, '')                                         AS engine_cylinders,
    IF(vb.einsatz != '', CONCAT(LEFT(vb.einsatz,4),'/',RIGHT(vb.einsatz,2)), '')   AS engine_year_from,
    IF(vb.auslauf != '', CONCAT(LEFT(vb.auslauf,4),'/',RIGHT(vb.auslauf,2)), '')   AS engine_year_to
FROM data_overview ov
JOIN data_vb vb
    ON  vb.catalog = ov.catalog
    AND vb.typ     = ov.epis_typ
LEFT JOIN (
    SELECT catalog, modell, markt, MIN(bezeichnung) AS bezeichnung
    FROM data_overview
    WHERE epis_typ = 0
      AND bezeichnung != ''
      AND bezeichnung NOT LIKE '%>>%'
      AND bezeichnung NOT LIKE '%<<%'
    GROUP BY catalog, modell, markt
) names
    ON  names.catalog = ov.catalog
    AND names.modell  = ov.modell
    AND names.markt   = ov.markt
WHERE ov.epis_typ > 0
GROUP BY
    ov.catalog, ov.modell, names.bezeichnung, ov.vbz_tabelle, ov.epis_typ,
    ov.markt,
    vb.motorkennbuchstabe, vb.liter, vb.kw, vb.ps, vb.zylinder,
    vb.einsatz, vb.auslauf
UNION ALL

-- ML (VW Truck) has no data in data_vb — include without engine fields
SELECT
    ov.catalog                      AS brand_code,
    'VW Truck'                      AS brand_name,
    ov.modell                                        AS model_code,
    COALESCE(names.bezeichnung, '')                  AS model_name,
    REPLACE(ov.vbz_tabelle, ' || ', ',')             AS verkaufsbezeichnung,
    ov.epis_typ,
    ov.markt                                                          AS market,
    MIN(ov.einsatz)                                                   AS model_year_from,
    IF(MAX(ov.einsatz) > YEAR(CURDATE()), '', MAX(ov.einsatz))       AS model_year_to,
    ''  AS engine_code,
    ''  AS engine_cc,
    ''  AS engine_kw,
    ''  AS engine_ps,
    ''  AS engine_cylinders,
    ''  AS engine_year_from,
    ''  AS engine_year_to
FROM data_overview ov
LEFT JOIN (
    SELECT catalog, modell, markt, MIN(bezeichnung) AS bezeichnung
    FROM data_overview
    WHERE epis_typ = 0
      AND bezeichnung != ''
      AND bezeichnung NOT LIKE '%>>%'
      AND bezeichnung NOT LIKE '%<<%'
    GROUP BY catalog, modell, markt
) names
    ON  names.catalog = ov.catalog
    AND names.modell  = ov.modell
    AND names.markt   = ov.markt
WHERE ov.catalog = 'ML'
  AND ov.epis_typ > 0
GROUP BY
    ov.modell, ov.epis_typ, ov.vbz_tabelle, ov.markt, names.bezeichnung
ORDER BY brand_code, model_code, epis_typ, market;
ENDSQL

HEADER="brand_code;brand_name;model_code;model_name;verkaufsbezeichnung;epis_typ;market;model_year_from;model_year_to;engine_code;engine_cc;engine_kw;engine_ps;engine_cylinders;engine_year_from;engine_year_to"

echo "[$(date '+%H:%M:%S')] Starting export from $CONTAINER/$DB..."

echo "$HEADER" > "$OUTPUT"

docker exec "$CONTAINER" mysql \
    -u"$MYSQL_USER" -p"$MYSQL_PASS" "$DB" \
    --batch --silent \
    -e "$SQL" 2>/dev/null \
| awk 'BEGIN { FS="\t"; OFS=";" } {
    for (i=1; i<=NF; i++) {
        if ($i ~ /;|"/) {
            gsub(/"/, "\"\"", $i)
            $i = "\"" $i "\""
        }
    }
    $1=$1
    print
}' >> "$OUTPUT"

if [ $? -ne 0 ] || [ ! -s "$OUTPUT" ]; then
    echo "[$(date '+%H:%M:%S')] ERROR: export failed or file is empty"
    exit 1
fi

ROWS=$(( $(wc -l < "$OUTPUT") - 1 ))
SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo "[$(date '+%H:%M:%S')] Done: $ROWS rows, $SIZE"
echo "Output: $OUTPUT"

# Quick stats
echo ""
echo "Rows by brand:"
awk -F';' 'NR>1 {print $2}' "$OUTPUT" | sort | uniq -c | sort -rn
