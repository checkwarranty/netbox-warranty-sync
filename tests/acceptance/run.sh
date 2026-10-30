#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
docker compose up -d
ready=false
for attempt in $(seq 1 120); do
  if curl --fail --silent http://127.0.0.1:18082/login/ >/dev/null; then ready=true; break; fi
  sleep 5
done
$ready || { docker compose logs --tail=100; exit 1; }
docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c "exec(open('/opt/cw-testing/seed.py').read())"
docker compose cp netbox:/tmp/cw-netbox-auth.json /tmp/cw-netbox-auth.json
python3 runtime.py
