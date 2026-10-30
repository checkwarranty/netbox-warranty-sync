import json
from users.models import User, Token
from dcim.models import Device, DeviceType, DeviceRole, Manufacturer, Site
from extras.models import CustomField
from django.contrib.contenttypes.models import ContentType

user = User.objects.create_superuser(username="acceptance", email="test@example.invalid", password="acceptance-only-password")
token = Token(user=user)
token.save()
manufacturer = Manufacturer.objects.create(name="Dell", slug="acceptance-dell")
type_ = DeviceType.objects.create(manufacturer=manufacturer, model="PowerEdge acceptance", slug="poweredge-acceptance")
role = DeviceRole.objects.create(name="Acceptance", slug="acceptance")
site = Site.objects.create(name="Acceptance", slug="acceptance")
for name, type_name in (("warranty_expiration", "date"), ("warranty_start_date", "date"), ("warranty_status", "text"), ("support_tier", "text"), ("factory_specs", "text")):
    field = CustomField.objects.create(name=name, type=type_name)
    field.object_types.set([ContentType.objects.get_for_model(Device).pk])
for kind in ("ENDONLY", "FULL", "SPECS", "NOTFOUND", "ASYNC"):
    Device.objects.create(name="acceptance-"+kind.lower(), serial="TEST-"+kind, device_type=type_, role=role, site=site)
import importlib.util
spec = importlib.util.spec_from_file_location("cw_custom", "/opt/cw-script.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
script = module.CheckWarrantySyncScript()
from types import SimpleNamespace
script.request = SimpleNamespace(user=user)
for name in ("log_info", "log_warning", "log_success", "log_failure"):
    setattr(script, name, lambda *args, **kwargs: None)
script.run({"plan_only": False, "dry_run": False, "force": True}, commit=True)
assert Device.objects.get(serial="TEST-SPECS").custom_field_data["factory_specs"] == "CPU: Xeon"
# Script permissions must restrict a runnable script to authorized devices.
from users.models import ObjectPermission
other_site = Site.objects.create(name="Other tenant", slug="other-tenant")
other = Device.objects.create(name="other-tenant", serial="TEST-OTHER", device_type=type_, role=role, site=other_site)
limited = User.objects.create_user(username="limited", password="acceptance-only-password")
permission = ObjectPermission.objects.create(name="Limited devices", actions=["view", "change"], constraints={"site_id": site.pk})
permission.object_types.set([ContentType.objects.get_for_model(Device).pk])
permission.users.add(limited)
script.request = SimpleNamespace(user=limited)
script.run({"plan_only": False, "dry_run": False, "force": True}, commit=True)
other.refresh_from_db()
assert not other.custom_field_data.get("warranty_expiration"), "Cross-tenant Custom Script write"
other.serial = ""
other.save()
# Reset mapped state so the external CLI independently verifies writes.
Device.objects.update(custom_field_data={})
auth = token.get_auth_header_prefix() + token.token
from pathlib import Path
Path("/tmp/cw-netbox-auth.json").write_text(json.dumps({"token": auth.removeprefix("Bearer ").removeprefix("Token ")}))
print("Acceptance data seeded")
