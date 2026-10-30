import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import HTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import checkwarranty_netbox_sync as sync
from mock_api import Handler, REQUESTS

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
auth = json.loads(pathlib.Path("/tmp/cw-netbox-auth.json").read_text())
os.environ.update(NETBOX_URL="http://127.0.0.1:18082", NETBOX_API_TOKEN=auth["token"], CHECKWARRANTY_API_KEY="acceptance-sandbox-only", CHECKWARRANTY_API_URL="http://127.0.0.1:"+str(server.server_port)+"/api/v1")
args = argparse.Namespace(dry_run=False, plan=True, limit=100, force=True, recheck_days=30, brand=None, manufacturer=None, site=None, role=None, sync_specs=True, verbose=False)
sync.run_sync(args)
assert not REQUESTS, "Plan mode must not call B2B API"
args.plan = False
with patch.object(sync.time, "sleep"):
    sync.run_sync(args)
client = sync.NetBoxClient(os.environ["NETBOX_URL"], auth["token"])
rows = {row["serial"]: row for row in client.get_devices(total_limit=100)}
assert rows["TEST-ENDONLY"]["custom_fields"]["warranty_expiration"] == "2027-01-05"
assert not rows["TEST-ENDONLY"]["custom_fields"].get("warranty_start_date")
assert rows["TEST-FULL"]["custom_fields"]["warranty_start_date"] == "2024-01-01"
assert "Xeon" in rows["TEST-SPECS"]["custom_fields"]["factory_specs"]
assert not rows["TEST-NOTFOUND"]["custom_fields"].get("warranty_expiration")
assert rows["TEST-ASYNC"]["custom_fields"]["warranty_expiration"] == "2027-01-05"
assert len(REQUESTS) == 5
server.shutdown()
print("NetBox 4.7 REST acceptance passed (v2 token, plan, full/partial/not-found/async)")
