# CheckWarranty &bull; NetBox DCIM Hardware Warranty Sync

> **Dry-run and API credits:** `--dry-run` prevents writes to the target ITAM,
> but still sends device serials to CheckWarranty and may consume live API credits.
> Use a sandbox API key and `TEST-` serials for acceptance testing.

Automate server and network hardware warranty tracking in [NetBox](https://netboxoss.dev/) using the [CheckWarranty B2B API](https://checkwarranty.info/docs/api).

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python: 3.8+](https://img.shields.io/badge/Python-3.8%2B-green.svg)
![Integration: NetBox](https://img.shields.io/badge/DCIM-NetBox-blueviolet.svg)

---

## Overview

Data center teams and sysadmins track hundreds of rack servers (Dell PowerEdge, HPE ProLiant, Lenovo ThinkSystem), but warranty expiration dates and support SLA tiers (ProSupport Plus, Care Pack NBD) are often missing or stored in disconnected vendor spreadsheets.

**`checkwarranty-netbox-sync`** bridges this gap:
1. Discovers hardware devices in NetBox (Dell, HPE, Lenovo, Apple).
2. Looks up live OEM warranty contracts, support SLAs, and factory hardware specs via the CheckWarranty REST API.
3. Automatically updates NetBox device records and custom fields:
   - **`warranty_expiration`**: Exact expiration date (`YYYY-MM-DD`).
   - **`warranty_status`**: Active / Expired.
   - **`support_tier`**: Vendor service level (e.g. *ProSupport Plus 24x7 4Hr*, *HPE Foundation Care*).
   - **`warranty_start_date`**: Original ship/contract date.
   - **`factory_specs`**: Factory CPU, RAM, and storage configuration.

---

## Workflow Diagram

```
┌──────────────────┐       1. Fetch devices        ┌────────────────────────┐
│   NetBox DCIM    │ ◄───────────────────────────  │ checkwarranty-netbox   │
│   (Self-hosted)  │                               │    (CLI or Script)     │
└────────┬─────────┘                               └───────────┬────────────┘
         │ 2. Return serials & custom fields                   │
         └──────────────────────────────────────────►          │ 3. Lookup warranty & SLA
                                                               ▼
                                                   ┌────────────────────────┐
                                                   │   CheckWarranty API    │
                                                   │ (Dell, HPE, Lenovo...) │
                                                   └───────────┬────────────┘
         ┌─────────────────┐ 5. Update custom fields           │
         │   NetBox DCIM   │ ◄─────────────────────────────────┘ 4. Return verified data
         │   (Enriched)    │   (Expiration, SLA, Status, Specs)
         └─────────────────┘
```

---

## Deployment Options

You can run this integration in two ways:
* **Option A: Standalone CLI / Docker** (recommended for scheduled cron jobs, Kubernetes cronjobs, or CI/CD pipelines).
* **Option B: NetBox In-App Custom Script** (run directly from the NetBox Web UI without running a separate server).

---

## Prerequisites: NetBox Custom Fields

In NetBox, navigate to **Customization &rarr; Custom Fields &rarr; Add** and create the following fields assigned to the **DCIM > Device** content type:

| Field Name | Type | Label | Required? |
| :--- | :--- | :--- | :--- |
| `warranty_expiration` | Date | Warranty Expiration | No |
| `warranty_status` | Selection / Text | Warranty Status | No |
| `support_tier` | Text | Support SLA Tier | No |
| `warranty_start_date` | Date | Warranty Start Date | No |
| `factory_specs` | Text | Factory Specifications | No |

---

## Option A: Quick Start (Standalone CLI)

### 1. Clone & Setup

```bash
cd integrations/netbox
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your NetBox and CheckWarranty credentials:

```ini
NETBOX_URL=https://netbox.yourcompany.com
NETBOX_API_TOKEN=your_netbox_api_token

CHECKWARRANTY_API_KEY=cw_live_your_api_key_here
CHECKWARRANTY_API_URL=https://checkwarranty.info/api/v1
```

### 3. Run Dry-Run Simulation

Verify device matching and field extraction without modifying your NetBox database:

```bash
python sync.py --dry-run
```

### 4. Execute Live Synchronization

```bash
python sync.py
```

---

## Option B: NetBox Web UI Custom Script

If you prefer running syncs directly from the NetBox web interface:

1. Copy [`netbox_custom_script.py`](./netbox_custom_script.py) into your NetBox `SCRIPTS_ROOT` directory:
   ```bash
   cp netbox_custom_script.py /opt/netbox/netbox/scripts/checkwarranty_sync.py
   ```
2. Open NetBox Web UI &rarr; **Customization &rarr; Custom Scripts**.
3. Select **CheckWarranty Hardware Warranty Sync**.
4. Configure `CHECKWARRANTY_API_KEY` and optionally `CHECKWARRANTY_API_URL` in the NetBox worker environment. Credentials and API URLs are never collected in job forms.
5. Select filters and use **Plan only** first. Disable it to allow API requests (which may consume credits); **Dry Run** independently prevents writes. The script restricts devices to the current user’s view/change permissions and rotates batches using the NetBox cache.

Logs and direct links to updated devices will appear in real time in the NetBox execution view.

---

## CLI Options & Filters

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--dry-run` | `False` | Simulate execution without updating NetBox records. |
| `--limit` | `50` | Maximum number of devices to fetch and inspect. |
| `--force` | `False` | Force check even if an active warranty date is already recorded. |
| `--recheck-days` | `0` | Re-query devices whose warranty expires within N days (or is expired). |
| `--manufacturer` | `None` | Filter devices by manufacturer slug (e.g. `dell`, `hpe`, `lenovo`). |
| `--site` | `None` | Filter devices by NetBox site slug (e.g. `dc-frankfurt`, `nyc-01`). |
| `--role` | `None` | Filter devices by NetBox role slug (e.g. `server`, `blade`). |
| `--brand` | `auto` | Override brand auto-detection (`dell`, `hp`, `lenovo`, `apple`). |
| `--verbose` | `False` | Enable debug logs for troubleshooting. |

---

## Credit Protection & Smart Caching

To prevent unnecessary B2B API credit usage:
* **Active Warranty Protection:** Devices with a valid warranty date in the future are skipped by default.
* **Smart Renewal Buffer (`--recheck-days`):** Set `--recheck-days 30` in monthly cron jobs to only re-query servers approaching contract renewal.
* **Idempotency Keys:** Each lookup uses one idempotency key. New scheduled invocations create new keys; this is not a cross-run scan cache.

---

## Docker & Scheduled Cron

Run as an isolated container:

```bash
docker build -t checkwarranty-netbox-sync .
docker run --rm --env-file .env checkwarranty-netbox-sync --limit 100
```

Or using `docker-compose`:

```bash
docker-compose run --rm checkwarranty-netbox-sync
```

To run as a weekly cron job on Linux:

```bash
0 2 * * 0 /usr/bin/docker run --rm --env-file /opt/netbox-sync/.env checkwarranty-netbox-sync --recheck-days 30
```

---

## License

MIT &copy; CheckWarranty Team

## Installable CLI (1.0.1)

Build and install locally with `python -m pip install .`, or install the wheel
from a GitHub release. PyPI publication is pending account/publisher setup.
The CLI command is `checkwarranty-netbox-sync`.
Use `--plan` to discover candidates without sending requests to CheckWarranty.
`--dry-run` still queries CheckWarranty but suppresses writes to the target ITAM.

Warranty start, purchase and manufacture dates are distinct. This connector never
populates a purchase date from a warranty start. Exact expiration dates use the
configured expiration custom field (Snipe-IT), NetBox date custom field, or a
`[CheckWarranty] expiry=YYYY-MM-DD` marker in GLPI warranty notes. Native month
intervals are written only when they reproduce the exact calendar expiration.
A new CLI run creates a new idempotency key; this is not a cross-run cache.

Scheduled runs rotate through target assets using `--state-file` (default:
`~/.local/state/checkwarranty/netbox.json`). Run one scheduler process per
state file. Plan/dry-run do not advance it. For Docker, preserve `/state` using a
named volume, for example `-v checkwarranty-netbox-state:/state`; the supplied
Compose file does this. The cursor stores only a target URL hash and an offset,
not API keys. Removing it restarts discovery at the beginning.
