"""No built-in deployment addresses; supported hosts are a local opt-in."""
import json
from pathlib import Path


def configured_host(base, host):
    try:
        document = json.loads((Path(base) / 'server_config.json').read_text(encoding='utf-8'))
        hosts = document.get('supported_login_hosts', [])
        return (isinstance(host, str) and isinstance(hosts, list)
                and all(isinstance(item, str) and item for item in hosts)
                and host in hosts)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
