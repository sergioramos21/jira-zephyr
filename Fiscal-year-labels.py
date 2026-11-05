"""
fiscal_year_labels.py
Lee el campo Fiscal Year (customfield_10062, select list) y agrega la etiqueta
FY<valor> al campo Labels de cada issue. Ejemplo: "2026" -> FY2026.

Uso:
  python fiscal_year_labels.py                         # usa el JQL de la sección de configuración (dry-run)
  python fiscal_year_labels.py --apply                 # aplica sobre el JQL de la configuración
  python fiscal_year_labels.py --limit 5               # dry-run solo con las primeras 5 issues
  python fiscal_year_labels.py --project V3            # dry-run (no modifica nada)
  python fiscal_year_labels.py --project V3 --apply    # aplica los cambios
  python fiscal_year_labels.py --jql 'project = V3 AND created >= 2025-01-01' --apply

Requiere en .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

# =====================================================================
# CONFIGURACIÓN DE ALCANCE
# Define aquí el JQL que limita qué issues procesa el script.
# Se usa cuando no pasas --project ni --jql en la terminal.
# El script agrega automáticamente: AND cf[10062] is not EMPTY
#
# Ejemplos:
#   JQL = 'project = V3'
#   JQL = 'key in (V3-101, V3-102, V3-103)'          # prueba con tickets puntuales
#   JQL = 'project = V3 AND created >= 2025-01-01'
#   JQL = 'project = V3 AND "Fiscal Year" = 2026'
JQL = 'key in (V3-1, V3-2)'

# Máximo de issues a procesar (None = todas). Útil para pruebas.
LIMIT = None
# =====================================================================

FIELD_ID = "customfield_10062"
PAGE_SIZE = 100

load_dotenv()
BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.getenv("JIRA_EMAIL")
TOKEN = os.getenv("JIRA_API_TOKEN")

if not all([BASE_URL, EMAIL, TOKEN]):
    sys.exit("Faltan JIRA_BASE_URL, JIRA_EMAIL o JIRA_API_TOKEN en el .env")

session = requests.Session()
session.auth = (EMAIL, TOKEN)
session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


def request(method, url, **kwargs):
    """Request con reintentos ante rate limit (429) y errores 5xx."""
    for attempt in range(6):
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  ...{resp.status_code}, reintentando en {wait}s")
            time.sleep(wait)
            continue
        return resp
    return resp


def build_label(raw_value):
    """'2026' -> 'FY2026'; 'FY 2026' -> 'FY2026'; quita espacios."""
    value = re.sub(r"\s+", "", str(raw_value))
    if value.upper().startswith("FY"):
        value = value[2:]
    return f"FY{value}"


def search_issues(jql):
    """Itera todas las issues del JQL usando paginación por nextPageToken."""
    url = f"{BASE_URL}/rest/api/3/search/jql"
    next_token = None
    while True:
        body = {"jql": jql, "fields": [FIELD_ID, "labels"], "maxResults": PAGE_SIZE}
        if next_token:
            body["nextPageToken"] = next_token
        resp = request("POST", url, json=body)
        if resp.status_code != 200:
            sys.exit(f"Error en búsqueda ({resp.status_code}): {resp.text}")
        data = resp.json()
        for issue in data.get("issues", []):
            yield issue
        next_token = data.get("nextPageToken")
        if data.get("isLast", True) or not next_token:
            break


def add_label(issue_key, label):
    url = f"{BASE_URL}/rest/api/3/issue/{issue_key}"
    body = {"update": {"labels": [{"add": label}]}}
    return request("PUT", url, params={"notifyUsers": "false"}, json=body)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", help="Key del proyecto, ej. V3 (sobrescribe JQL del archivo)")
    group.add_argument("--jql", help="JQL personalizado (sobrescribe JQL del archivo)")
    parser.add_argument("--limit", type=int, default=LIMIT, help="Máximo de issues a procesar")
    parser.add_argument("--apply", action="store_true", help="Aplica cambios (sin esto es dry-run)")
    args = parser.parse_args()

    if args.jql:
        base_jql = args.jql
    elif args.project:
        base_jql = f'project = "{args.project}"'
    elif JQL.strip():
        base_jql = JQL
    else:
        sys.exit("Define JQL en la sección de configuración o usa --project / --jql")

    jql = f"({base_jql}) AND cf[10062] is not EMPTY ORDER BY key ASC"

    mode = "APLICANDO CAMBIOS" if args.apply else "DRY-RUN (no se modifica nada)"
    print(f"Modo: {mode}\nJQL: {jql}\nLímite: {args.limit or 'sin límite'}\n")

    # Muestra cuántas issues entran en el alcance antes de procesar
    count_resp = request("POST", f"{BASE_URL}/rest/api/3/search/approximate-count", json={"jql": jql.split(" ORDER BY")[0]})
    if count_resp.status_code == 200:
        print(f"Issues en el alcance (aprox.): {count_resp.json().get('count')}\n")
    elif count_resp.status_code == 400:
        sys.exit(f"JQL inválido: {count_resp.text}")

    log_name = f"fiscal_year_labels_{datetime.now():%Y%m%d_%H%M%S}.csv"
    counts = {"actualizado": 0, "ya_tenia": 0, "dry_run": 0, "error": 0}

    with open(log_name, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["issue", "fiscal_year", "label", "resultado", "detalle"])

        processed = 0
        for issue in search_issues(jql):
            if args.limit and processed >= args.limit:
                print(f"\nLímite de {args.limit} issues alcanzado.")
                break
            processed += 1
            key = issue["key"]
            fields = issue.get("fields", {})
            fy = fields.get(FIELD_ID)
            raw = fy.get("value") if isinstance(fy, dict) else fy
            if not raw:
                continue

            label = build_label(raw)
            current = fields.get("labels") or []

            if label in current:
                result, detail = "ya_tenia", ""
            elif not args.apply:
                result, detail = "dry_run", f"se agregaría {label}"
            else:
                resp = add_label(key, label)
                if resp.status_code == 204:
                    result, detail = "actualizado", ""
                else:
                    result, detail = "error", f"{resp.status_code} {resp.text[:200]}"

            counts[result] += 1
            writer.writerow([key, raw, label, result, detail])
            print(f"{key}: {raw} -> {label} [{result}] {detail}")

    print("\nResumen:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"Log: {log_name}")


if __name__ == "__main__":
    main()
