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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CheckWarrantySyncScript(Script):
    class Meta:
        name = "CheckWarranty Hardware Warranty Sync"
        description = "Query vendor APIs via CheckWarranty and populate device warranty expiration dates & specs."
        field_order = ["plan_only", "dry_run", "force", "recheck_days", "manufacturers"]

    if NETBOX_INSTALLED:
        plan_only = BooleanVar(default=True, description="List candidates without API requests or credit use")
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
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as resp:
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
                with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as resp:
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
        api_url = os.environ.get("CHECKWARRANTY_API_URL", "https://checkwarranty.info/api/v1")
        plan_only = data.get("plan_only", True)
        if not plan_only and not api_key:
            raise RuntimeError("CHECKWARRANTY_API_KEY is not configured on the worker")
        dry_run = data.get("dry_run", True)
        force = data.get("force", False)
        recheck_days = data.get("recheck_days", 30)
        selected_mfg = data.get("manufacturers")

        request = getattr(self, "request", None)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            raise RuntimeError("Authenticated script request required")
        from django.core.cache import cache
        cursor_key = f"checkwarranty:script:cursor:{user.pk}"
        cursor = int(cache.get(cursor_key, 0))
        query = Device.objects.restrict(user, "view").exclude(serial="").exclude(serial__isnull=True)
        if not plan_only:
            query = query.restrict(user, "change")
        query = query.filter(pk__gt=cursor).order_by("pk")
        if selected_mfg:
            query = query.filter(device_type__manufacturer__in=selected_mfg)

        devices = list(query[:100])
        self.log_info(f"Discovered {len(devices)} candidate devices with serial numbers in NetBox.")

        updated_count = 0
        skipped_count = 0

        for dev in devices:
            if not plan_only:
                cache.set(cursor_key, dev.pk, timeout=None)
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
            if plan_only:
                self.log_info(f"[PLAN] {dev.name}: lookup required; no API request sent")
                continue
            res = self._query_checkwarranty(api_url, api_key, serial, brand)

            if not res or res.get("status") == "not_found":
                self.log_warning(f"Device {serial} not found in vendor database.")
                continue

            if res.get("status") != "completed":
                self.log_warning(f"Lookup did not complete for {serial}")
                continue
            warranty = res.get("warranty", {}) or {}
            details = warranty.get("details") or {}
            end_date_str = warranty.get("endDate")
            changes = {}
            if isinstance(end_date_str, str):
                clean_end_date = end_date_str.split("T")[0]
                try:
                    datetime.strptime(clean_end_date, "%Y-%m-%d")
                    changes["warranty_expiration"] = clean_end_date
                except ValueError:
                    pass
            if warranty.get("status") in ("active", "expired"):
                changes["warranty_status"] = warranty["status"].capitalize()
            tier = warranty.get("coverageType")
            if isinstance(tier, str) and tier:
                changes["support_tier"] = tier[:100]
            specs = []
            if isinstance(details, dict):
                for source, label in (("processor", "CPU"), ("memory", "RAM"), ("hardDrive", "Disk")):
                    value = details.get(source)
                    if isinstance(value, str) and value.strip().lower() not in ("", "n/a", "unknown", "none", "-"):
                        specs.append(label + ": " + value)
            if specs:
                changes["factory_specs"] = " | ".join(specs)
            if not changes:
                self.log_warning(f"No useful mapped data for {serial}")
                continue
            dev.custom_field_data.update(changes)

            self.log_success(f"[{'DRY-RUN' if dry_run else 'COMMITTED'}] Device {dev.name or dev.serial}: warranty/specs updated")

            if commit and not dry_run:
                dev.full_clean()
                dev.save()

            updated_count += 1

        if not devices and not plan_only:
            cache.set(cursor_key, 0, timeout=None)
        self.log_info(f"Sync complete. Updated: {updated_count}, Skipped: {skipped_count}")
