"""Versioned hunt plans shared by the controller and bot engine."""
import json
import re
import tempfile
import time
from pathlib import Path


def accounts_for(plan):
    return [plan.get('leader', '')] + [member['account'] for member in plan.get('members', [])]


def load_teams(path):
    path = Path(path)
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise ValueError('刷马队伍配置格式无效')
    if document.get('format') == 1:
        if not document.get('leader'):
            return []
        teams = [dict(document, id='legacy', name='刷马队伍 1')]
    elif document.get('format') == 2 and isinstance(document.get('teams'), list):
        teams = document['teams']
    else:
        raise ValueError('不支持的刷马队伍配置格式')
    identifiers, occupied = set(), set()
    for plan in teams:
        if not isinstance(plan, dict):
            raise ValueError('刷马队伍配置无效')
        identifier = plan.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', identifier):
            raise ValueError('队伍编号无效')
        if identifier in identifiers or plan.get('format') != 1 or type(plan.get('enabled')) is not bool:
            raise ValueError('队伍编号重复或配置无效')
        identifiers.add(identifier)
        members = plan.get('members')
        if not isinstance(members, list) or any(not isinstance(m, dict) or not isinstance(m.get('account'), str) for m in members):
            raise ValueError('队员配置无效')
        selected = accounts_for(plan)
        if not 2 <= len(selected) <= 6 or any(not isinstance(a, str) or not a for a in selected):
            raise ValueError('队伍需有一名队长和一至五名队员')
        if len(set(selected)) != len(selected) or occupied.intersection(selected):
            raise ValueError('同一账号不能加入多支刷马队伍')
        occupied.update(selected)
    return teams


def save_teams(path, teams):
    atomic_json(path, dict(format=2, teams=teams))


def atomic_json(path, document, replace_attempts=1):
    if type(replace_attempts) is not int or not 1 <= replace_attempts <= 6:
        raise ValueError("Invalid atomic replacement retry count")
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.stem + '_', suffix='.tmp', delete=False) as output:
        json.dump(document, output, ensure_ascii=False, indent=2)
        temporary = Path(output.name)
    try:
        for attempt in range(replace_attempts):
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt + 1 == replace_attempts:
                    raise
                time.sleep(0.025)
    finally:
        temporary.unlink(missing_ok=True)


def plan_for_account(path, account):
    return next((plan for plan in load_teams(path) if account in accounts_for(plan)), None)


def plan_for_id(path, identifier=None):
    teams = load_teams(path)
    if identifier is not None:
        plan = next((p for p in teams if p['id'] == identifier), None)
        if plan is None:
            raise ValueError('该刷马队伍不存在，请刷新页面')
        return plan
    if len(teams) > 1:
        raise ValueError('请指定要操作的刷马队伍')
    return teams[0] if teams else None
