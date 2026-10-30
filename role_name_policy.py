"""Rules for newly created bot character names; existing names are unchanged."""


def validate_new_role_name(name):
    if not isinstance(name, str):
        raise ValueError('新角色名格式无效')
    if '@' in name:
        raise ValueError('新角色名不能包含 @，该符号会影响客户端按名字申请组队')
    return name


def safe_role_names(names):
    return tuple(name for name in names if isinstance(name, str) and '@' not in name)
