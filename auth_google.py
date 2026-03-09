from sba.integrations.google_tasks import build_service
import yaml
from pathlib import Path

config = yaml.safe_load(open(Path.home() / ".sba/config.yaml"))
build_service(config)
print("Auth OK")
