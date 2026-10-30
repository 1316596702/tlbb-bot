"""Fresh, identity-bound location of a managed leader on this controller."""
import hashlib
import json
import math
from pathlib import Path
from hunt_teams import atomic_json


def identity(host, port, team_id, account, guid):
    return dict(host=host, port=int(port), team_id=team_id, account=account, guid=int(guid))


def state_path(base, expected):
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()[:32]
    return Path(base) / 'managed_leader_states' / (digest + '.json')


def publish(base, expected, scene, ready, obj_id, timestamp, position=None, position_at=None):
    path = state_path(base, expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, dict(format=1, identity=expected, scene=scene, ready=ready,
                          obj_id=obj_id, updated_at=timestamp,
                          position=position, position_at=position_at), replace_attempts=6)


def read(base, expected, now):
    try:
        data = json.loads(state_path(base, expected).read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('format') != 1 or data.get('identity') != expected
                or type(data.get('scene')) is not int or not 0 <= data['scene'] < 0xFFFF
                or type(data.get('ready')) is not bool
                or type(data.get('obj_id')) is not int or not 0 <= data['obj_id'] < 0xFFFFFFFF
                or type(data.get('updated_at')) not in (int, float)
                or not math.isfinite(data['updated_at'])
                or not 0 <= now - data['updated_at'] <= 10):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def recent_position(state, now):
    if state is None:
        return None
    position, stamp = state.get('position'), state.get('position_at')
    if (not isinstance(position, (list, tuple)) or len(position) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 10000 for v in position)
            or type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not 0 <= now - stamp <= 5):
        return None
    return tuple(position)
