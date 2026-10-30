"""Restore equipment durability before managed test accounts log in."""
import datetime as dt
import hashlib
import json
from pathlib import Path

import paramiko
import pymysql
from server_online_guard import require_role_offline, wait_for_role_offline
from offline_soul_life import refill_soul_life

# Verified against the pinned ShareMemory1 DB decoder and Server1 setter.
# p5 bits 16..23 -> _ITEM+0x58 (maximum durability).
# p6 bits  0..7  -> _ITEM+0x5a (repair failures), preserved.
# p6 bits  8..15 -> _ITEM+0x5b (current durability).
# p6 bits 16..31 -> _ITEM+0x5c (partial wear accumulator).
SERVER_SHA256 = "6558fa7a1b298341eae8a3056b6a6a28e8ebcbf2df81699af3a6c2d7483dd639"
EQUIPMENT_POSITIONS = (240,241,242,243,244,245,246,247,251,252,253,254,255,257,258,277)
REPAIR_POINTS = {0,1,2,3,4,5,6,7,11,12,13,14,15,17,18,37}


def verify_codec_server(base, config):
    manifest = json.loads((base / "offline_durability_codec.json").read_text(encoding="utf-8"))
    expected = {"/home/tlbb1/Server/Server1": SERVER_SHA256,
                "/home/tlbb1/Server/ShareMemory1": manifest["share_memory_sha256"]}
    soul_definitions = json.loads((base / "soul_life_definitions.json").read_text(encoding="utf-8"))
    expected["/home/tlbb1/Public/Config/KFSBase.txt"] = soul_definitions["kfs_base_sha256"]
    if manifest.get("server_sha256") != SERVER_SHA256 or manifest.get("enabled") is not True:
        raise ValueError("上线补耐久版本校验未启用")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(config["db_host"], username=config["db_user"], password=config["db_password"],
                    timeout=8, auth_timeout=8, banner_timeout=8)
        _, stdout, stderr = ssh.exec_command(
            "sha256sum /home/tlbb1/Server/Server1 /home/tlbb1/Server/ShareMemory1 "
            "/home/tlbb1/Public/Config/KFSBase.txt", timeout=15)
        hashes = {}
        for line in stdout.read().decode("ascii").splitlines():
            digest, path = line.split(maxsplit=1)
            hashes[path] = digest
        if hashes != expected:
            raise ValueError("服务端版本与已验证耐久编码不一致，未修改装备")
    except Exception:
        ssh.close()
        raise
    return ssh


def refill_parameter(item):
    if int(item["itemtype"]) // 10_000_000 != 1:
        raise ValueError("物品不是装备，未修改耐久")
    if item["p5"] is None or item["p6"] is None:
        raise ValueError("装备耐久参数缺失，未修改")
    maximum = (int(item["p5"]) & 0xFFFFFFFF) >> 16 & 0xFF
    original = int(item["p6"]) & 0xFFFFFFFF
    if maximum == 0:
        return int(item["p6"]), None
    changed = (original & 0xFF) | (maximum << 8)
    return changed, {"maximum": maximum, "before": (original >> 8) & 0xFF,
                     "wear_before": original >> 16, "repair_failures": original & 0xFF}


def ensure_full_durability(base_dir, config, account):
    raise RuntimeError("此版本尚未适配上线维护；请在独立测试环境验证后实现此入口")
    base = Path(base_dir)
    if config["db_host"] not in ():
        raise ValueError("上线补耐久仅允许连接测试服")
    managed = json.loads((base / "managed_bot_accounts.json").read_text(encoding="utf-8"))
    if account not in managed:
        raise ValueError("非受管账号，未修改装备")
    definitions = json.loads((base / "repair_equipment_definitions.json").read_text(encoding="utf-8"))
    soul_definitions = json.loads((base / "soul_life_definitions.json").read_text(encoding="utf-8"))
    soul_maxima = soul_definitions["maximum_life_by_itemtype"]
    connection = pymysql.connect(host=config["db_host"], port=int(config["db_port"]),
        user=config["db_user"], password=config["db_password"], charset="latin1",
        autocommit=False, connect_timeout=8, read_timeout=20, write_timeout=20,
        cursorclass=pymysql.cursors.DictCursor)
    ssh = None
    try:
        # A killed bot process can leave a role in the scene briefly. Wait outside
        # the write transaction so logout can finish saving the same database rows.
        with connection.cursor() as cursor:
            cursor.execute("SELECT charguid FROM tlbbdb_main.t_char WHERE BINARY accname=%s", (account,))
            pending_roles = cursor.fetchall()
        connection.rollback()
        if len(pending_roles) != 1:
            raise ValueError("账号角色数量不是1，未修改装备耐久")
        ssh = verify_codec_server(base, config)
        wait_for_role_offline(ssh, pending_roles[0]["charguid"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
                           "WHERE (TABLE_SCHEMA='web' AND TABLE_NAME='account') OR "
                           "(TABLE_SCHEMA='tlbbdb_main' AND TABLE_NAME IN ('t_char','t_iteminfo'))")
            engines = cursor.fetchall()
            if len(engines) != 3 or any(r["ENGINE"].lower() != "innodb" for r in engines):
                raise ValueError("相关数据表不支持事务，未修改耐久")
            cursor.execute("SELECT name,is_online,is_lock FROM web.account WHERE BINARY name=%s FOR UPDATE", (account,))
            status = cursor.fetchone()
            if status is None or status["is_online"] != 0 or status["is_lock"] != 0:
                raise ValueError("账号仍在线或正在登录，未修改装备耐久；请离线后再启动")
            cursor.execute("SELECT charguid,accname,charname FROM tlbbdb_main.t_char WHERE BINARY accname=%s FOR UPDATE", (account,))
            roles = cursor.fetchall()
            if len(roles) != 1:
                raise ValueError("账号角色数量不是1，未修改装备耐久")
            role = roles[0]
            require_role_offline(ssh, role["charguid"])
            cursor.execute("SELECT * FROM tlbbdb_main.t_iteminfo WHERE charguid=%s AND isvalid=1 "
                           "AND itemtype BETWEEN 10000000 AND 19999999 AND (pos BETWEEN 0 AND 99 OR pos IN ("
                           + ",".join("%s" for _ in EQUIPMENT_POSITIONS) + ")) ORDER BY pos FOR UPDATE",
                           (role["charguid"], *EQUIPMENT_POSITIONS))
            items = cursor.fetchall()
            if len({i["pos"] for i in items}) != len(items):
                raise ValueError("装备栏有重复物品，未修改耐久")
            updates = []
            for item in items:
                definition = definitions.get(str(item["itemtype"]))
                if definition is None:
                    raise ValueError(f"物品 {item['itemtype']} 没有装备定义，未修改耐久")
                if int(definition["point"]) not in REPAIR_POINTS or int(definition["max"]) <= 0:
                    continue
                new_p6, info = refill_parameter(item)
                changed = dict(p6=new_p6, creator=item["creator"])
                info = info or {}
                if int(definition["point"]) == 18:
                    maximum = soul_maxima.get(str(item["itemtype"]))
                    if maximum is None:
                        raise ValueError("武魂缺少寿命上限定义，未修改")
                    changed["creator"], info["soul_life"] = refill_soul_life(item, maximum)
                if new_p6 != int(item["p6"]) or changed["creator"] != item["creator"]:
                    updates.append((item, changed, info))
            if not updates:
                connection.rollback()
                return "已检查装备耐久及武魂寿命，无需补满"
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            account_key = hashlib.sha256(account.encode("utf-8")).hexdigest()[:12]
            directory = base / "durability_backups"
            directory.mkdir(exist_ok=True)
            backup_path = directory / f"{stamp}_{account_key}.json"
            backup_path.write_text(json.dumps({"test_host": config["db_host"], "account": account,
                "encoding": connection.encoding, "role": role, "items_before": items,
                "changes": [{"aid": item["aid"], "guid": item["guid"], "pos": item["pos"],
                             "p6_before": item["p6"], "p6_after": new["p6"],
                             "creator_before": item["creator"], "creator_after": new["creator"],
                             **info} for item, new, info in updates]},
                ensure_ascii=True, indent=2, default=str), encoding="utf-8")
            cases = " ".join("WHEN %s THEN %s" for _ in updates)
            parameters = [value for item,new,info in updates for value in (item["aid"],new["p6"])]
            creator_parameters = [value for item,new,info in updates for value in (item["aid"],new["creator"])]
            ids = [item["aid"] for item,new,info in updates]
            cursor.execute("UPDATE tlbbdb_main.t_iteminfo SET p6=CASE aid " + cases
                           + " END, creator=CASE aid " + cases
                           + " END WHERE charguid=%s AND isvalid=1 AND aid IN ("
                           + ",".join("%s" for _ in ids) + ")", (*parameters,*creator_parameters,role["charguid"],*ids))
            if cursor.rowcount != len(updates):
                raise RuntimeError("装备状态改变，耐久更新已回滚")
            cursor.execute("SELECT * FROM tlbbdb_main.t_iteminfo WHERE aid IN ("
                           + ",".join("%s" for _ in ids) + ")", ids)
            after_rows = {r["aid"]:r for r in cursor.fetchall()}
            for item, changed, info in updates:
                after = after_rows.get(item["aid"])
                expected = dict(item, **changed)
                if after != expected:
                    raise RuntimeError("耐久更新校验不通过，已回滚")
                if "repair_failures" in info and (int(after["p6"]) & 0xFF) != info["repair_failures"]:
                    raise RuntimeError("维修次数校验不通过，已回滚")
            cursor.execute("SELECT is_online,is_lock FROM web.account WHERE BINARY name=%s", (account,))
            final_status = cursor.fetchone()
            if final_status["is_online"] != 0 or final_status["is_lock"] != 0:
                raise RuntimeError("账号状态改变，耐久更新已回滚")
            require_role_offline(ssh, role["charguid"])
        connection.commit()
        durability_count = sum(new["p6"] != item["p6"] for item,new,info in updates)
        soul_count = sum(new["creator"] != item["creator"] for item,new,info in updates)
        return f"上线前已补满 {durability_count} 件装备耐久、{soul_count} 件武魂寿命，备份：{backup_path.name}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        if ssh is not None:
            ssh.close()
