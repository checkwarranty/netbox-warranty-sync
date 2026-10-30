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
for name in ("log_info", "log_warning", "log_success", "log_failure"):
    setattr(script, name, lambda *args, **kwargs: None)
script.run({"plan_only": False, "dry_run": False, "force": True}, commit=True)
assert Device.objects.get(serial="TEST-SPECS").custom_field_data["factory_specs"] == "CPU: Xeon"
# Reset mapped state so the external CLI independently verifies writes.
Device.objects.update(custom_field_data={})
auth = token.get_auth_header_prefix() + token.token
print(json.dumps({"token": auth.removeprefix("Bearer ").removeprefix("Token ")}))
