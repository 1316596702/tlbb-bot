"""Optional position provider. No server-memory reader is distributed."""
TEST_HOSTS = ()  # Explicitly disabled until a version-specific provider is tested.


def query(base, host, scene, obj, guid):
    raise ValueError("精确位置接口尚未适配；请使用目标服已验证的只读接口")
