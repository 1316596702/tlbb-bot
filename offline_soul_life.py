"""Restore only the life word in the pinned KFS hexadecimal extension."""
import re


def refill_soul_life(item, maximum):
    maximum = int(maximum)
    if not 0 < maximum <= 32767:
        raise ValueError("武魂寿命上限无效，未修改")
    creator = item["creator"]
    if not isinstance(creator, str) or not re.fullmatch(r"[0-9a-fA-F]{56}", creator):
        raise ValueError("武魂扩展数据格式未确认，未修改寿命")
    # Native DecodeKfsData copies the 28 decoded bytes into _ITEM+0x10.
    # Native GetKfsLife/SetKfsLife access the signed word at _ITEM+0x14.
    before = int.from_bytes(bytes.fromhex(creator[8:12]), "little", signed=True)
    if not 0 <= before <= maximum:
        raise ValueError("武魂当前寿命超出已验证范围，未修改")
    if before == maximum:
        return creator, {"before": before, "maximum": maximum}
    changed = creator[:8] + maximum.to_bytes(2, "little").hex().upper() + creator[12:]
    return changed, {"before": before, "maximum": maximum}
