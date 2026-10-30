"""Authentication extension point. No captured sessions or credentials included."""
import importlib
import re


def load_adapter(module_name):
    if not isinstance(module_name, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module_name):
        raise RuntimeError("认证适配模块名无效")
    try:
        adapter = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError("请先实现本地认证模块，参阅 docs/ADAPTING.md；本项目不附带认证凭据") from exc
    if any(not callable(getattr(adapter, name, None)) for name in ('build_hello', 'build_auth')):
        raise RuntimeError("认证模块必须实现 build_hello(account) 和 build_auth(account, challenge)")
    return adapter


def checked_packet(value):
    if not isinstance(value, bytes) or not 6 <= len(value) <= 65536:
        raise RuntimeError("认证模块必须返回长度有效的完整 bytes 数据包")
    return value
