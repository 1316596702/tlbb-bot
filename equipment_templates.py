"""Read formal equipment templates; insert copies through a test transaction."""
import json
import secrets
from pathlib import Path

import pymysql

POSITIONS = (240, 241, 242, 243, 244, 245, 246, 247, 249, 250,
             251, 252, 253, 254, 255, 256, 257, 258, 277)
CRAFTED = {241, 242, 243, 244, 245, 246, 247, 251, 252, 253, 254, 255}
TEXT_FIELDS = {"creator", "fixattr", "var"}


def decode_name(value, encoding):
    return value.encode(encoding).decode("gb18030", "replace")


def source_connection(base_dir):
    raise RuntimeError("装备来源数据库适配尚未启用；请先验证目标版本与只读权限")
    config = json.loads((Path(base_dir) / "equipment_source_config.json").read_text(encoding="utf-8"))
    if config["host"] not in ():
        raise ValueError("装备来源地址不在已配置的正式服范围内")
    connection = pymysql.connect(host=config["host"], port=int(config["port"]),
        user=config["user"], password=config["password"], charset="latin1",
        autocommit=False, connect_timeout=8, read_timeout=20, write_timeout=20,
        cursorclass=pymysql.cursors.DictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        return connection
    except Exception:
        connection.close()
        raise


def search_roles(base_dir, query):
    query = str(query).strip()
    if not 1 <= len(query) <= 35:
        raise ValueError("请输入 1 到 35 个字的角色名或账号")
    escaped = query.replace("=", "==").replace("%", "=%").replace("_", "=_")
    pattern = ("%" + escaped + "%").encode("gb18030").hex()
    connection = source_connection(base_dir)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.charguid,c.accname,c.charname,c.level,c.menpai,"
                "(SELECT COUNT(*) FROM tlbbdb_main.t_iteminfo i WHERE i.charguid=c.charguid "
                "AND i.isvalid=1 AND i.pos IN (" + ",".join(map(str, POSITIONS)) + ")) AS equipment_count "
                "FROM tlbbdb_main.t_char c WHERE "
                "(BINARY c.charname LIKE UNHEX(%s) ESCAPE '=' OR BINARY c.accname LIKE UNHEX(%s) ESCAPE '=') "
                "ORDER BY c.charguid LIMIT 20", (pattern, pattern))
            rows = cursor.fetchall()
            for row in rows:
                row["charname"] = decode_name(row["charname"], connection.encoding)
            return rows
    finally:
        connection.rollback()
        connection.close()


def load_template(base_dir, charguid):
    connection = source_connection(base_dir)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT charguid,charname,level,menpai FROM tlbbdb_main.t_char WHERE charguid=%s", (charguid,))
            role = cursor.fetchone()
            if role is None:
                raise ValueError("装备模板角色不存在，请重新搜索")
            cursor.execute("SELECT * FROM tlbbdb_main.t_iteminfo WHERE charguid=%s AND isvalid=1 AND pos IN ("
                           + ",".join(map(str, POSITIONS)) + ") ORDER BY pos", (charguid,))
            items = cursor.fetchall()
            if not items:
                raise ValueError("所选角色没有可复制的已装备物品")
            if len({i["pos"] for i in items}) != len(items):
                raise ValueError("模板装备位置重复，已停止复制")
            for item in items:
                for field in TEXT_FIELDS:
                    value = item[field]
                    item[field] = value.encode(connection.encoding) if value is not None else None
            cursor.execute("SHOW COLUMNS FROM tlbbdb_main.t_iteminfo")
            schema = [(r["Field"], r["Type"]) for r in cursor.fetchall()]
            return {"role": {**role, "charname": decode_name(role["charname"], connection.encoding)},
                    "items": items, "schema": schema}
    finally:
        connection.rollback()
        connection.close()


def validate_template(base_dir, template, level, schools):
    document = json.loads((Path(base_dir) / "equipment_definitions.json").read_text(encoding="utf-8"))
    formal = document["127.0.0.1"]
    test = document["127.0.0.1"]
    for item in template["items"]:
        key = str(item["itemtype"])
        definition = test.get(key)
        if not definition or formal.get(key, {}).get("hash") != definition["hash"]:
            raise ValueError(f"物品 {key} 在两服的装备定义不一致或缺失，已停止创建")
        name = definition["name"]
        if level < definition["level"]:
            raise ValueError(f"{name} 要求 {definition['level']} 级，请提高创建等级")
        if str(definition["school"]) != "-1" and any(str(s) != str(definition["school"]) for s in schools):
            raise ValueError(f"{name} 有门派限制，请选择匹配的固定门派")
        point = int(definition["point"])
        allowed = {6: {246, 251}, 12: {252, 253}}.get(point, {240 + point})
        if item["pos"] not in allowed or item["world"] != 10 or item["server"] != 0:
            raise ValueError(f"{name} 的装备位置或服务器归属不适合当前测试服")


def insert_equipment(cursor, template, charguid, name_bytes):
    cursor.execute("SHOW COLUMNS FROM tlbbdb_main.t_iteminfo")
    schema = [(r[0], r[1]) for r in cursor.fetchall()]
    if schema != template["schema"]:
        raise ValueError("两服物品表结构不一致，整批创建已停止")
    columns = [name for name, _ in schema if name != "aid"]
    # Allocate and check all candidate GUIDs in one query. Never reuse source GUIDs.
    guids = set()
    while len(guids) < len(template["items"]):
        guids.add(1_500_000_000 + secrets.randbelow(500_000_000))
    guids = list(guids)
    cursor.execute("SELECT guid FROM tlbbdb_main.t_iteminfo WHERE guid IN (" + ",".join("%s" for _ in guids) + ")", guids)
    if cursor.fetchone() is not None:
        raise RuntimeError("新物品 GUID 冲突，整批已回滚，请重试")
    expressions = ["CONVERT(UNHEX(%s) USING latin1)" if c in TEXT_FIELDS else "%s" for c in columns]
    sql = "INSERT INTO tlbbdb_main.t_iteminfo (" + ",".join("`" + c + "`" for c in columns) + ") VALUES (" + ",".join(expressions) + ")"
    rows = []
    for guid, source in zip(guids, template["items"]):
        clone = dict(source, charguid=charguid, guid=guid)
        if source["pos"] in CRAFTED:
            clone["creator"] = name_bytes
        rows.append([clone[c].hex() if c in TEXT_FIELDS and clone[c] is not None else clone[c] for c in columns])
    cursor.executemany(sql, rows)
    if cursor.rowcount != len(rows):
        raise RuntimeError("装备复制数量不正确，整批已回滚")
    return len(rows)
