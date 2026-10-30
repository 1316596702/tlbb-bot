"""Offline-save checks require a target-version implementation."""
class RoleStillOnlineError(ValueError):
    pass


def require_role_offline(ssh, charguid):
    raise RuntimeError("离线状态与缓存保存接口尚未适配，拒绝数据库维护")


def wait_for_role_offline(ssh, charguid, *args, **kwargs):
    raise RuntimeError("离线状态与缓存保存接口尚未适配，拒绝数据库维护")
