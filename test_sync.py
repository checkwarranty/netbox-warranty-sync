#!/usr/bin/env python3
"""
Unit tests for NetBox DCIM sync script.
Tests brand detection, recheck logic, date formatting, API mapping, and dry-run execution.
"""

import unittest
from unittest.mock import MagicMock, patch
import os
import sys
from datetime import date, timedelta
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from sync import (
    detect_brand,
    format_iso_to_date,
    should_recheck_warranty,
    NetBoxClient,
    CheckWarrantyClient,
    run_sync,
)


class TestNetBoxSync(unittest.TestCase):

    def test_detect_brand_by_manufacturer(self):
        self.assertEqual(detect_brand("XYZ1234", manufacturer_name="Dell Inc."), "dell")
        self.assertEqual(detect_brand("XYZ1234", manufacturer_name="HPE"), "hp")
        self.assertEqual(detect_brand("XYZ1234", manufacturer_name="Hewlett Packard Enterprise"), "hp")
        self.assertEqual(detect_brand("XYZ1234", manufacturer_name="Lenovo"), "lenovo")
        self.assertEqual(detect_brand("XYZ1234", manufacturer_name="Apple"), "apple")

    def test_detect_brand_by_model(self):
        self.assertEqual(detect_brand("XYZ1234", model_name="PowerEdge R740xd"), "dell")
        self.assertEqual(detect_brand("XYZ1234", model_name="ProLiant DL380 Gen10"), "hp")
        self.assertEqual(detect_brand("XYZ1234", model_name="ThinkSystem SR650 V2"), "lenovo")
        self.assertEqual(detect_brand("XYZ1234", model_name="Mac Pro Rack"), "apple")

    def test_detect_brand_by_serial(self):
        self.assertEqual(detect_brand("8HG76F2"), "dell")  # 7-char alphanumeric
        self.assertEqual(detect_brand("PF4A1234"), "lenovo")  # 8-char alphanumeric
        self.assertEqual(detect_brand("5CG1234567"), "hp")  # 10-char HP prefix
        self.assertEqual(detect_brand("C02G12345678"), "apple")  # 12-char Apple prefix
        self.assertIsNone(detect_brand("SOME-RANDOM-SWITCH-123"))

    def test_format_iso_to_date(self):
        self.assertEqual(format_iso_to_date("2026-12-31T23:59:59.000Z"), "2026-12-31")
        self.assertEqual(format_iso_to_date("2025-05-01"), "2025-05-01")
        self.assertIsNone(format_iso_to_date(None))
        self.assertIsNone(format_iso_to_date("invalid-date"))

    def test_should_recheck_warranty(self):
        today = date.today()
        # No existing warranty -> always recheck
        self.assertTrue(should_recheck_warranty(None, recheck_days=30))
        self.assertTrue(should_recheck_warranty("", recheck_days=30))

        # Expired warranty (yesterday) -> always recheck
        yesterday_str = (today - timedelta(days=1)).isoformat()
        self.assertTrue(should_recheck_warranty(yesterday_str, recheck_days=0))

        # Expiring in 10 days, recheck_days=30 -> recheck
        soon_str = (today + timedelta(days=10)).isoformat()
        self.assertTrue(should_recheck_warranty(soon_str, recheck_days=30))

        # Expiring in 90 days, recheck_days=30 -> do not recheck
        future_str = (today + timedelta(days=90)).isoformat()
        self.assertFalse(should_recheck_warranty(future_str, recheck_days=30))

    def test_netbox_client_url_clean(self):
        nb = NetBoxClient("https://netbox.company.internal/ ", "tok123")
        self.assertEqual(nb.base_url, "https://netbox.company.internal")
        self.assertEqual(nb.headers["Authorization"], "Token tok123")

    @patch("sync.NetBoxClient._request")
    def test_netbox_pagination(self, mock_request):
        # Simulate 2 pages of NetBox devices
        mock_request.side_effect = [
            {"results": [{"id": 1, "name": "srv-01", "serial": "8HG76F2"}], "next": "http://page2"},
            {"results": [{"id": 2, "name": "srv-02", "serial": "8HG76F3"}], "next": None},
        ]
        nb = NetBoxClient("https://netbox.company.internal", "tok123")
        devices = nb.get_devices(total_limit=10, page_size=1)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["name"], "srv-01")
        self.assertEqual(devices[1]["name"], "srv-02")

    @patch("sync.NetBoxClient._request")
    def test_netbox_update_custom_fields(self, mock_request):
        mock_request.return_value = {"id": 10, "name": "srv-01"}
        nb = NetBoxClient("https://netbox.company.internal", "tok123")
        success = nb.update_device_custom_fields(10, {"warranty_expiration": "2027-01-01"})
        self.assertTrue(success)
        mock_request.assert_called_once_with(
            "PATCH",
            "/api/dcim/devices/10/",
            data={"custom_fields": {"warranty_expiration": "2027-01-01"}}
        )

    @patch("sync.CheckWarrantyClient._request")
    def test_checkwarranty_lookup(self, mock_request):
        mock_request.return_value = {
            "status": "completed",
            "warranty": {
                    "endDate": "2027-06-15T00:00:00Z",
                    "status": "active",
                    "coverageType": "ProSupport Plus 24x7 4Hr",
                }
        }
        cw = CheckWarrantyClient("https://checkwarranty.info/api/v1", "cw_key")
        res = cw.lookup_device("8HG76F2", "dell")
        self.assertIsNotNone(res)
        self.assertEqual(res["warranty"]["coverageType"], "ProSupport Plus 24x7 4Hr")

    @patch.dict(os.environ, {
        "NETBOX_URL": "https://netbox.test",
        "NETBOX_API_TOKEN": "token_test",
        "CHECKWARRANTY_API_KEY": "cw_key_test",
    })
    @patch("sync.NetBoxClient.get_devices")
    @patch("sync.CheckWarrantyClient.lookup_device")
    @patch("sync.NetBoxClient.update_device_custom_fields")
    def test_run_sync_dry_run(self, mock_update, mock_lookup, mock_get_devices):
        mock_get_devices.return_value = [
            {
                "id": 101,
                "name": "r740-blade-01",
                "serial": "8HG76F2",
                "device_type": {"model": "PowerEdge R740", "manufacturer": {"name": "Dell"}},
                "custom_fields": {"warranty_expiration": None},
            }
        ]
        mock_lookup.return_value = {
            "status": "completed",
            "warranty": {
                    "startDate": "2024-01-10T00:00:00Z",
                    "endDate": "2027-01-10T00:00:00Z",
                    "status": "active",
                    "coverageType": "ProSupport 24x7",
                },

        }

        args = argparse.Namespace(
            dry_run=True,
            limit=10,
            force=False,
            recheck_days=0,
            brand=None,
            manufacturer=None,
            site=None,
            role=None,
            sync_specs=True,
            verbose=False,
        )

        run_sync(args)
        # Dry-run should NOT invoke update_device_custom_fields
        mock_update.assert_not_called()
        mock_lookup.assert_called_once_with("8HG76F2", "dell")


if __name__ == "__main__":
    unittest.main()
