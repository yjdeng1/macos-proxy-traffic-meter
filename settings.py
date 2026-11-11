"""User-local runtime configuration; no machine-specific defaults."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'config.json').read_text()) if (ROOT / 'config.json').exists() else {}
ZONE = ZoneInfo(CONFIG.get('timezone', 'UTC'))
PROXY_PORTS = tuple(int(port) for port in CONFIG.get('proxy_ports', [7890, 7897]))
if not PROXY_PORTS or any(not 1 <= port <= 65535 for port in PROXY_PORTS):
    raise ValueError('proxy_ports must contain TCP port numbers from 1 to 65535')
