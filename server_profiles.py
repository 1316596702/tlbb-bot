"""Keep the active root configuration scoped to one server at a time."""
import hashlib
import json
import re
from pathlib import Path
from hunt_teams import atomic_json

FILES = {'managed_role_cache.json': {}, 'web_accounts.json': [], 'managed_bot_accounts.json': [],
         'hunt_plan.json': {'format': 2, 'teams': []},
         'leader_shout.json': {'format': 2, 'leaders': {}}}


def server_key(config):
    identity = [str(config[k]).strip().lower() for k in ('login_host', 'db_host')]
    identity += [int(config['login_port']), int(config['db_port'])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def bind_profile(base, config):
    base = Path(base)
    marker = base / 'server_scope.json'
    key = server_key(config)
    metadata = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
    if metadata and (metadata.get('format')!=1 or any(not isinstance(metadata.get(k),str) or not re.fullmatch(r'[0-9a-f]{24}',metadata[k]) for k in ('active','legacy'))):
        raise ValueError('服务器管理配置标记无效')
    if metadata and metadata['active'] == key:
        return log_directory(base, metadata, key)
    original = {name: (base / name).read_bytes() if (base / name).exists() else None for name in FILES}
    old_marker = marker.read_bytes() if marker.exists() else None
    try:
        if metadata:
            previous = base / 'server_profiles' / metadata['active']
            previous.mkdir(parents=True, exist_ok=True)
            for name, default in FILES.items():
                raw = original[name]
                if raw is None:
                    atomic_json(previous / name, default)
                else:
                    (previous / name).write_bytes(raw)
            target = base / 'server_profiles' / key
            target.mkdir(parents=True, exist_ok=True)
            for name, default in FILES.items():
                document = json.loads((target / name).read_text(encoding='utf-8')) if (target / name).exists() else default
                atomic_json(base / name, document)
            metadata = dict(metadata, active=key)
        else:
            metadata = dict(format=1, active=key, legacy=key)
        atomic_json(marker, metadata)
    except Exception:
        for name, raw in original.items():
            if raw is None:
                (base / name).unlink(missing_ok=True)
            else:
                (base / name).write_bytes(raw)
        if old_marker is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_bytes(old_marker)
        raise
    return log_directory(base, metadata, key)


def log_directory(base, metadata, key):
    path = Path(base) / 'botlogs' if key == metadata['legacy'] else Path(base) / 'server_profiles' / key / 'botlogs'
    path.mkdir(parents=True, exist_ok=True)
    return path
