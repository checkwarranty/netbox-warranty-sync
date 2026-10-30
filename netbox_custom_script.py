"""
CheckWarranty - NetBox In-App Custom Script
Deploy this script to NetBox's SCRIPTS_ROOT directory (e.g. /opt/netbox/netbox/scripts/)
to allow operators to run warranty synchronizations directly from the NetBox Web UI.

Features:
- Web UI execution with progress logs and links to updated devices.
- Filtering by manufacturer (Dell, HPE, Lenovo, Apple).
- Dry-run prevents writes; API lookups may still consume live credits.
- API key is read from the worker environment, never stored in job form data.
- Smart recheck filter (only refresh expired or expiring soon).
"""

import os
import uuid
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, date

try:
    from extras.scripts import Script, StringVar, BooleanVar, IntegerVar, MultiObjectVar
    from dcim.models import Device, Manufacturer
    NETBOX_INSTALLED = True
except ImportError:
    # Allow file to be imported in external test environments without NetBox installed
    NETBOX_INSTALLED = False
    Script = object
    StringVar = BooleanVar = IntegerVar = MultiObjectVar = None
    Device = Manufacturer = None


class CheckWarrantySyncScript(Script):
    class Meta:
        name = "CheckWarranty Hardware Warranty Sync"
        description = "Query vendor APIs via CheckWarranty and populate device warranty expiration dates & specs."
        field_order = ["api_url", "dry_run", "force", "recheck_days", "manufacturers"]

    if NETBOX_INSTALLED:
        api_url = StringVar(
            default="https://checkwarranty.info/api/v1",
            description="CheckWarranty API Base URL",
            required=False
        )
        dry_run = BooleanVar(
            default=True,
            description="Simulate sync without saving changes to NetBox"
        )
        force = BooleanVar(
            default=False,
            description="Force recheck even if active warranty is already recorded"
        )
        recheck_days = IntegerVar(
            default=30,
            description="Recheck warranty if expiring within N days (or already expired)"
        )
        manufacturers = MultiObjectVar(
            model=Manufacturer,
            required=False,
            description="Filter devices by manufacturer (Leave empty for Dell, HPE, Lenovo, Apple)"
        )

    def _query_checkwarranty(self, base_url: str, api_key: str, serial: str, brand: str = None):
        headers = {
            "X-API-Key": api_key.strip(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
            "User-Agent": "CheckWarranty-NetBox-Script/1.0",
        }
        url = f"{base_url.rstrip('/')}/devices/lookup" if brand else f"{base_url.rstrip('/')}/devices/auto-detect"
        payload = {"serialNumber": serial}
        if brand:
            payload["brand"] = brand

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_body = resp.read().decode("utf-8")
                res = json.loads(res_body) if res_body else {}
                if res.get("status") in ("in_progress", "pending") and res.get("batchId"):
                    return self._poll_batch(base_url, api_key, res["batchId"])
                return res
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            self.log_warning(f"CheckWarranty HTTP {e.code} for serial {serial}")
            return None
        except Exception as e:
            self.log_failure(f"CheckWarranty connection error: {e}")
            return None

    def _poll_batch(self, base_url: str, api_key: str, batch_id: str, retries: int = 5):
        headers = {
            "X-API-Key": api_key.strip(),
            "Accept": "application/json",
            "User-Agent": "CheckWarranty-NetBox-Script/1.0",
        }
        url = f"{base_url.rstrip('/')}/devices/scans/{batch_id}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        for _ in range(retries):
            time.sleep(4)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    if res.get("status") in ("completed", "partial"):
                        items = res.get("items", [])
                        return items[0] if items else None
                    if res.get("status") in ("failed", "error", "not_found"):
                        return res
            except Exception:
                continue
        return None

    def run(self, data, commit):
        if not NETBOX_INSTALLED:
            raise RuntimeError("NetBox dependencies are not available in this environment.")

        api_key = os.environ.get("CHECKWARRANTY_API_KEY", "")
        api_url = data.get("api_url") or "https://checkwarranty.info/api/v1"
        dry_run = data.get("dry_run", True)
        force = data.get("force", False)
        recheck_days = data.get("recheck_days", 30)
        selected_mfg = data.get("manufacturers")

        query = Device.objects.exclude(serial="").exclude(serial__isnull=True)
        if selected_mfg:
            query = query.filter(device_type__manufacturer__in=selected_mfg)

        devices = list(query[:100])
        self.log_info(f"Discovered {len(devices)} candidate devices with serial numbers in NetBox.")

        updated_count = 0
        skipped_count = 0

        for dev in devices:
            serial = dev.serial.strip()
            existing_exp = dev.custom_field_data.get("warranty_expiration")

            if existing_exp and not force:
                try:
                    exp_date = datetime.strptime(str(existing_exp).split("T")[0], "%Y-%m-%d").date()
                    if (exp_date - date.today()).days > recheck_days:
                        skipped_count += 1
                        continue
                except ValueError:
                    pass

            mfg_name = dev.device_type.manufacturer.name.lower() if dev.device_type and dev.device_type.manufacturer else ""
            brand = None
            if "dell" in mfg_name:
                brand = "dell"
            elif any(k in mfg_name for k in ("hp", "hpe", "hewlett")):
                brand = "hp"
            elif any(k in mfg_name for k in ("lenovo", "ibm")):
                brand = "lenovo"
            elif "apple" in mfg_name:
                brand = "apple"

            self.log_info(f"Looking up {dev.name or dev.serial} ({dev.serial})...")
            res = self._query_checkwarranty(api_url, api_key, serial, brand)

            if not res or res.get("status") == "not_found":
                self.log_warning(f"Device {serial} not found in vendor database.")
                continue

            warranty = res.get("warranty", {}) or {}
            end_date_str = warranty.get("endDate")
            if not end_date_str:
                self.log_warning(f"No warranty end date returned for {serial}.")
                continue

            clean_end_date = end_date_str.split("T")[0]
            status = warranty.get("status")
            is_active = True if status == "active" else False if status == "expired" else None
            tier = warranty.get("coverageType") or warranty.get("description") or ""

            dev.custom_field_data["warranty_expiration"] = clean_end_date
            if is_active is not None:
                dev.custom_field_data["warranty_status"] = "Active" if is_active else "Expired"
            if tier:
                dev.custom_field_data["support_tier"] = tier[:100]

            self.log_success(f"[{'DRY-RUN' if dry_run else 'COMMITTED'}] Device {dev.name or dev.serial}: warranty until {clean_end_date} ({tier})")

            if commit and not dry_run:
                dev.full_clean()
                dev.save()

            updated_count += 1

        self.log_info(f"Sync complete. Updated: {updated_count}, Skipped: {skipped_count}")
