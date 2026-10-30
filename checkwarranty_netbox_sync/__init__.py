#!/usr/bin/env python3
"""
CheckWarranty - NetBox DCIM Hardware Warranty & Specifications Synchronizer
Synchronizes hardware warranty expiration dates, SLA tiers, and factory specs into NetBox DCIM.
Zero external dependencies required (uses standard library urllib/json).
"""

import os
import calendar
import sys
import argparse
import logging
import time
import uuid
import json
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, date
import urllib.request
import urllib.error
import urllib.parse

# Attempt to load .env if python-dotenv is available, otherwise read OS environment
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("checkwarranty-netbox")


def detect_brand(serial: str, model_name: str = "", manufacturer_name: str = "") -> Optional[str]:
    """Infer manufacturer from manufacturer name, model name, or serial number syntax."""
    mfg_lower = (manufacturer_name or "").strip().lower()
    mod_lower = (model_name or "").strip().lower()
    s_upper = (serial or "").strip().upper()

    # 1. NetBox manufacturer field check
    if any(k in mfg_lower for k in ("dell", "poweredge")):
        return "dell"
    if any(k in mfg_lower for k in ("hp", "hpe", "hewlett", "proliant")):
        return "hp"
    if any(k in mfg_lower for k in ("lenovo", "thinksystem", "thinkserver", "ibm")):
        return "lenovo"
    if any(k in mfg_lower for k in ("apple", "mac")):
        return "apple"

    # 2. Model name check (e.g. PowerEdge R740, ProLiant DL380, ThinkSystem SR650)
    if any(k in mod_lower for k in ("poweredge", "precision", "optiplex", "latitude", "dell")):
        return "dell"
    if any(k in mod_lower for k in ("proliant", "apollo", "synergy", "elitebook", "zbook", "hewlett", "hp")):
        return "hp"
    if any(k in mod_lower for k in ("thinksystem", "thinkserver", "thinkpad", "lenovo", "system x")):
        return "lenovo"
    if any(k in mod_lower for k in ("macbook", "mac pro", "mac mini", "apple")):
        return "apple"

    # 3. Serial syntax heuristics
    if len(s_upper) == 7 and s_upper.isalnum():
        return "dell"
    if len(s_upper) == 8 and s_upper.isalnum():
        return "lenovo"
    if len(s_upper) == 10 and (s_upper.startswith("5CG") or s_upper.startswith("5CD") or s_upper.startswith("CND") or s_upper.startswith("CZ")):
        return "hp"
    if len(s_upper) == 12 and s_upper.startswith("C02"):
        return "apple"

    return None


def format_iso_to_date(iso_str: Optional[str]) -> Optional[str]:
    """Convert ISO 8601 timestamp string to YYYY-MM-DD for NetBox date custom fields."""
    if not isinstance(iso_str, str) or not iso_str:
        return None
    try:
        clean = iso_str.split("T")[0]
        datetime.strptime(clean, "%Y-%m-%d")
        return clean
    except ValueError:
        return None


def should_recheck_warranty(existing_date_str: Optional[Any], recheck_days: int) -> bool:
    """
    Determine if a warranty expiration should be refreshed.
    Returns True if:
    - There is no existing warranty date
    - The warranty is already expired
    - The warranty expires within recheck_days from today
    """
    if not existing_date_str:
        return True
    try:
        clean = str(existing_date_str).split(" ")[0].split("T")[0]
        exp_date = datetime.strptime(clean, "%Y-%m-%d").date()
        today = date.today()
        days_remaining = (exp_date - today).days
        return days_remaining <= recheck_days
    except ValueError:
        return True


class NetBoxClient:
    """Handles communication with the NetBox REST API using urllib."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.headers = {
            "Authorization": f"Token {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CheckWarranty-NetBox-Sync/1.0",
        }

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"

        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_body = resp.read().decode("utf-8")
                return json.loads(res_body) if res_body else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else ""
            logger.error(f"NetBox HTTP {e.code} error on {method} {url}: {err_body}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"NetBox connection error: {e}")
            raise

    def get_devices(
        self,
        total_limit: int = 100,
        page_size: int = 50,
        manufacturer: Optional[str] = None,
        site: Optional[str] = None,
        role: Optional[str] = None,
        has_serial: bool = True
    ) -> List[Dict[str, Any]]:
        """Fetch devices from NetBox with automatic pagination."""
        all_devices: List[Dict[str, Any]] = []
        offset = 0

        params: Dict[str, Any] = {
            "limit": page_size,
            "offset": offset,
        }
        if has_serial:
            # NetBox 3.3+ supports filtering devices with populated serial numbers
            params["has_serial"] = "true"
        if manufacturer:
            params["manufacturer"] = manufacturer
        if site:
            params["site"] = site
        if role:
            params["role"] = role

        while len(all_devices) < total_limit:
            params["limit"] = min(page_size, total_limit - len(all_devices))
            params["offset"] = offset

            res = self._request("GET", "/api/dcim/devices/", params=params)
            results = res.get("results", [])
            if not results:
                break

            all_devices.extend(results)
            offset += len(results)
            if not res.get("next") or len(results) < params["limit"]:
                break

        return all_devices

    def update_device_custom_fields(self, device_id: int, custom_fields: Dict[str, Any]) -> bool:
        """Update custom fields of a device in NetBox via PATCH."""
        path = f"/api/dcim/devices/{device_id}/"
        payload = {"custom_fields": custom_fields}
        try:
            res = self._request("PATCH", path, data=payload)
            return bool(res and res.get("id"))
        except Exception as e:
            logger.error(f"Failed to update NetBox device #{device_id}: {e}")
            return False


class CheckWarrantyClient:
    """Handles communication with the CheckWarranty B2B REST API using urllib."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CheckWarranty-NetBox-Sync/1.0",
        }

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None, extra_headers: Optional[Dict[str, str]] = None) -> Any:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                res_body = resp.read().decode("utf-8")
                return json.loads(res_body) if res_body else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else ""
            if e.code == 404:
                return None
            if e.code == 402:
                logger.error("CheckWarranty credit balance exhausted (HTTP 402). Top up credits at checkwarranty.info/dashboard/billing")
                sys.exit(2)
            logger.warning(f"CheckWarranty HTTP {e.code} for {path}: {err_body}")
            return None
        except urllib.error.URLError as e:
            logger.error(f"CheckWarranty connection error: {e}")
            return None

    def lookup_device(self, serial: str, brand: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Query CheckWarranty for a single serial number."""
        idempotency_key = str(uuid.uuid4())
        extra = {"Idempotency-Key": idempotency_key}

        if brand:
            payload = {"serialNumber": serial, "brand": brand}
            res = self._request("POST", "/devices/lookup", payload, extra_headers=extra)
        else:
            payload = {"serialNumber": serial}
            res = self._request("POST", "/devices/auto-detect", payload, extra_headers=extra)

        if not res:
            return None

        # Handle async 202 accepted response
        if res.get("status") in ("in_progress", "pending"):
            batch_id = res.get("batchId")
            if batch_id:
                return self._poll_batch_result(batch_id)

        return res

    def _poll_batch_result(self, batch_id: str, max_retries: int = 6) -> Optional[Dict[str, Any]]:
        """Poll batch status endpoint if scan returned 202 Accepted."""
        for _ in range(max_retries):
            time.sleep(4)
            res = self._request("GET", f"/devices/scans/{batch_id}")
            if not res:
                continue
            status = res.get("status")
            if status in ("completed", "partial"):
                items = res.get("items", [])
                return items[0] if items and isinstance(items[0], dict) else None
            if status in ("failed", "error", "not_found"):
                logger.warning(f"CheckWarranty async scan ended with status: {status}")
                return res
        return None



def extract_result(result):
    """Read the public B2B DTO, including specs-only completed results."""
    if not isinstance(result, dict) or result.get("status") not in (None, "completed"):
        return {}, {}
    warranty = result.get("warranty")
    if not isinstance(warranty, dict):
        return {}, {}
    details = warranty.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    specs = {}
    for source, target in (("processor", "cpu"), ("memory", "ram"), ("hardDrive", "storage"), ("display", "display")):
        value = details.get(source)
        if isinstance(value, str) and value.strip().lower() not in ("", "unknown", "n/a", "none", "not found", "-"):
            specs[target] = value.strip()
    device = result.get("device") or {}
    if isinstance(device, dict) and warranty.get("status") != "not_found":
        for source, target in (("model", "model"), ("productNumber", "product_number"), ("description", "description")):
            value = device.get(source)
            if isinstance(value, str) and value.strip() and not re.match(r"^(unknown( device)?|[a-z]+ device|n/a|none|-)$", value.strip(), re.I):
                specs[target] = value.strip()
    if warranty.get("status") == "not_found" and not specs:
        return {}, {}
    return warranty, specs


def exact_warranty_months(start, end):
    """Return whole calendar months only when the interval matches exactly."""
    start, end = format_iso_to_date(start), format_iso_to_date(end)
    if not start or not end:
        return None
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    months = (last.year - first.year) * 12 + last.month - first.month
    if months <= 0:
        return None
    day = min(first.day, calendar.monthrange(last.year, last.month)[1])
    return months if last.day == day else None


def run_sync(args: argparse.Namespace) -> None:
    netbox_url = os.getenv("NETBOX_URL")
    netbox_token = os.getenv("NETBOX_API_TOKEN")
    cw_key = os.getenv("CHECKWARRANTY_API_KEY")
    cw_url = os.getenv("CHECKWARRANTY_API_URL", "https://checkwarranty.info/api/v1")

    if not netbox_url or not netbox_token:
        logger.error("Missing NETBOX_URL or NETBOX_API_TOKEN in environment variables.")
        sys.exit(1)
    if not cw_key:
        logger.error("Missing CHECKWARRANTY_API_KEY in environment variables.")
        sys.exit(1)

    # NetBox Custom Field names (configurable via env)
    field_exp = os.getenv("NETBOX_FIELD_WARRANTY_EXPIRATION", "warranty_expiration")
    field_status = os.getenv("NETBOX_FIELD_WARRANTY_STATUS", "warranty_status")
    field_tier = os.getenv("NETBOX_FIELD_SUPPORT_TIER", "support_tier")
    field_start = os.getenv("NETBOX_FIELD_WARRANTY_START", "warranty_start_date")
    field_specs = os.getenv("NETBOX_FIELD_SPECS", "factory_specs")

    netbox = NetBoxClient(netbox_url, netbox_token)
    cw = CheckWarrantyClient(cw_url, cw_key)

    if args.limit <= 0:
        logger.error("--limit must be greater than 0.")
        sys.exit(1)

    logger.info(
        f"Starting NetBox Sync (dry_run={args.dry_run}, limit={args.limit}, force={args.force}, "
        f"recheck_days={args.recheck_days})"
    )

    devices = netbox.get_devices(
        total_limit=args.limit,
        manufacturer=args.manufacturer,
        site=args.site,
        role=args.role
    )
    logger.info(f"Fetched {len(devices)} devices from NetBox.")

    stats = {"processed": 0, "updated": 0, "skipped": 0, "not_found": 0, "errors": 0}

    for dev in devices:
        dev_id = dev.get("id")
        serial = (dev.get("serial") or "").strip()
        dev_name = dev.get("name") or f"Device #{dev_id}"

        # Extract manufacturer and model
        dev_type = dev.get("device_type") or {}
        mfg_name = (dev_type.get("manufacturer", {}).get("name") or "") if isinstance(dev_type.get("manufacturer"), dict) else ""
        model_name = dev_type.get("model") or ""

        custom_fields = dev.get("custom_fields") or {}
        existing_warranty = custom_fields.get(field_exp)

        if not dev_id or not serial:
            if args.verbose:
                logger.info(f"Skipping {dev_name} (ID #{dev_id}): serial is empty.")
            stats["skipped"] += 1
            continue

        # Check if already covered and not expiring soon
        if existing_warranty and not args.force:
            if not should_recheck_warranty(str(existing_warranty), args.recheck_days):
                if args.verbose:
                    logger.info(f"Skipping {dev_name} ({serial}) - warranty valid until {existing_warranty}")
                stats["skipped"] += 1
                continue

        brand = args.brand or detect_brand(serial, model_name, mfg_name)
        stats["processed"] += 1

        logger.info(f"Querying CheckWarranty: {dev_name} [Serial: {serial}, Brand: {brand or 'auto-detect'}]...")
        if getattr(args, "plan", False):
            logger.info("[PLAN] Lookup required; no CheckWarranty request sent")
            stats["skipped"] += 1
            continue
        result = cw.lookup_device(serial, brand)

        if not result or result.get("status") == "not_found":
            logger.warning(f"Device not found on vendor portal: {serial}")
            stats["not_found"] += 1
            continue

        warranty, specs = extract_result(result)

        end_date = format_iso_to_date(warranty.get("endDate"))
        start_date = format_iso_to_date(warranty.get("startDate"))
        status = warranty.get("status")
        is_active = True if status == "active" else False if status == "expired" else None
        coverage_type = warranty.get("coverageType") or warranty.get("description") or ""

        if not end_date and not start_date and not specs and not coverage_type and is_active is None:
            logger.warning(f"No warranty coverage or specifications found for {serial}")
            stats["not_found"] += 1
            continue

        # Build custom_fields update payload
        cf_update: Dict[str, Any] = {}
        if end_date:
            cf_update[field_exp] = end_date
        if start_date:
            cf_update[field_start] = start_date
        if is_active is not None:
            cf_update[field_status] = "Active" if is_active else "Expired"
        if coverage_type:
            cf_update[field_tier] = coverage_type[:100]  # limit length

        if args.sync_specs and specs:
            specs_summary = [key.upper() + ": " + value for key, value in specs.items()]
            if specs_summary:
                cf_update[field_specs] = " | ".join(specs_summary)

        if not cf_update:
            stats["skipped"] += 1
            continue

        logger.info(f" -> Plan update {dev_name} ({serial}): {cf_update}")

        if args.dry_run:
            stats["updated"] += 1
            continue

        success = netbox.update_device_custom_fields(dev_id, cf_update)
        if success:
            logger.info(f" -> Successfully updated NetBox device #{dev_id}")
            stats["updated"] += 1
        else:
            logger.error(f" -> Failed to update NetBox device #{dev_id}")
            stats["errors"] += 1

    logger.info("=" * 60)
    logger.info(
        f"NetBox Sync Complete. Processed: {stats['processed']}, "
        f"Updated: {stats['updated']}, Skipped: {stats['skipped']}, "
        f"Not Found: {stats['not_found']}, Errors: {stats['errors']}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize hardware warranty status from CheckWarranty to NetBox DCIM."
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without modifying NetBox.")
    parser.add_argument("--limit", type=int, default=50, help="Max devices to fetch and inspect (default: 50).")
    parser.add_argument("--force", action="store_true", help="Update warranty even if already populated.")
    parser.add_argument(
        "--recheck-days",
        type=int,
        default=0,
        help="Recheck warranty if expired or expiring within N days (default: 0)."
    )
    parser.add_argument("--brand", type=str, choices=["dell", "lenovo", "hp", "apple"], help="Override brand detection.")
    parser.add_argument("--manufacturer", type=str, help="Filter NetBox devices by manufacturer slug (e.g. dell, hpe).")
    parser.add_argument("--site", type=str, help="Filter NetBox devices by site slug.")
    parser.add_argument("--role", type=str, help="Filter NetBox devices by device role slug (e.g. server).")
    parser.add_argument("--sync-specs", action="store_true", default=True, help="Sync hardware specs into custom fields.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging.")

    parser.add_argument("--plan", action="store_true", help="List assets needing lookup without calling CheckWarranty or writing changes.")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    run_sync(args)


if __name__ == "__main__":
    main()
