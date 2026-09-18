"""Fetch diesel (PDL) prices for Orange NSW (2800) from the NSW Fuel API
and append them to data/prices.db, exporting data/prices.json alongside.

Usage:
    python scripts/fetch_prices.py            # only fetches at 9am/9pm Sydney time
    python scripts/fetch_prices.py --force     # fetches regardless of local time
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "prices.db"
JSON_PATH = REPO_ROOT / "data" / "prices.json"
# GitHub Pages is configured to deploy from /dashboard, so a copy of the
# exported JSON also needs to live inside that folder for the static page
# to be able to fetch it.
DASHBOARD_JSON_PATH = REPO_ROOT / "dashboard" / "data" / "prices.json"

TOKEN_URL = "https://api.onegov.nsw.gov.au/oauth/client_credential/accesstoken"
PRICES_URL = "https://api.onegov.nsw.gov.au/FuelPriceCheck/v1/fuel/prices/location"

FUEL_TYPE = "PDL"
NAMED_LOCATION = "ORANGE"
POSTCODE = "2800"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
SCHEDULED_HOURS = (9, 21)


def get_access_token(client_id: str, client_secret: str) -> str:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.get(
        TOKEN_URL,
        params={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {credentials}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_prices(access_token: str, client_id: str) -> dict:
    now_sydney = datetime.now(SYDNEY_TZ)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "apikey": client_id,
        "transactionid": str(uuid.uuid4()),
        # NSW Fuel API requires dd/MM/yyyy HH:mm:ss in local (Sydney) time.
        # Verify against the Postman collection on the api.nsw.gov.au product
        # page if the API rejects this format.
        "requesttimestamp": now_sydney.strftime("%d/%m/%Y %H:%M:%S"),
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "fueltype": FUEL_TYPE,
        "namedlocation": NAMED_LOCATION,
        "postcode": POSTCODE,
    }
    resp = requests.post(PRICES_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            day_of_week TEXT NOT NULL,
            station_code TEXT,
            station_name TEXT,
            brand TEXT,
            address TEXT,
            price_cpl REAL NOT NULL
        )
        """
    )
    conn.commit()


def insert_rows(conn: sqlite3.Connection, payload: dict) -> int:
    stations_by_code = {
        str(s.get("code")): s for s in payload.get("stations", [])
    }
    prices = [
        p for p in payload.get("prices", []) if p.get("fueltype") == FUEL_TYPE
    ]

    now_sydney = datetime.now(SYDNEY_TZ)
    timestamp = now_sydney.isoformat()
    day_of_week = now_sydney.strftime("%A")

    rows = []
    for p in prices:
        code = str(p.get("stationcode"))
        station = stations_by_code.get(code, {})
        rows.append(
            (
                timestamp,
                day_of_week,
                code,
                station.get("name"),
                station.get("brand"),
                station.get("address"),
                p.get("price"),
            )
        )

    conn.executemany(
        """
        INSERT INTO prices
            (timestamp, day_of_week, station_code, station_name, brand, address, price_cpl)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def export_json(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT timestamp, day_of_week, station_code, station_name, brand, "
        "address, price_cpl FROM prices ORDER BY timestamp"
    )
    rows = [dict(r) for r in cur.fetchall()]
    payload = json.dumps(rows, indent=2)
    JSON_PATH.write_text(payload, encoding="utf-8")
    DASHBOARD_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_JSON_PATH.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fetch regardless of current Sydney local time (for manual testing).",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    now_sydney = datetime.now(SYDNEY_TZ)
    if not args.force and now_sydney.hour not in SCHEDULED_HOURS:
        print(
            f"Sydney local time is {now_sydney.strftime('%H:%M')} "
            f"(not {SCHEDULED_HOURS}); skipping fetch."
        )
        return 0

    client_id = os.environ.get("CLIENT_ID")
    client_secret = os.environ.get("CLIENT_SECRET")
    if not client_id or not client_secret:
        print("CLIENT_ID / CLIENT_SECRET environment variables are required.", file=sys.stderr)
        return 1

    access_token = get_access_token(client_id, client_secret)
    payload = fetch_prices(access_token, client_id)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        inserted = insert_rows(conn, payload)
        export_json(conn)
    finally:
        conn.close()

    print(f"Inserted {inserted} PDL price rows for Orange/2800 at {now_sydney.isoformat()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
