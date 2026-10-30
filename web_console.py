import atexit
import ctypes
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
import uuid
from collections import Counter
from functools import wraps
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

import pymysql
from flask import Flask, jsonify, render_template, request
from embedded_role_names import ROLE_NAMES
from role_name_policy import safe_role_names, validate_new_role_name
SAFE_ROLE_NAMES = safe_role_names(ROLE_NAMES)
from equipment_templates import search_roles, load_template, validate_template, insert_equipment
from offline_durability import ensure_full_durability
from leader_shout import load_settings as load_shout_settings, validate_settings as validate_shout_settings, save_settings as save_shout_settings
from managed_speaker import sync_allowlist as sync_speaker_allowlist
from hunt_teams import load_teams, save_teams, plan_for_id, plan_for_account, accounts_for, atomic_json
from server_profiles import bind_profile, server_key
from version_profile import configured_host
from process_scope import owns_process
from managed_import import query_candidates, validate_prefix


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
KERNEL32.OpenProcess.restype = ctypes.c_void_p
KERNEL32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
KERNEL32.GetExitCodeProcess.restype = ctypes.c_int
KERNEL32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
KERNEL32.TerminateProcess.restype = ctypes.c_int
KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
KERNEL32.CloseHandle.restype = ctypes.c_int


DATA_FILE = BASE_DIR / "web_accounts.json"
MANAGED_ACCOUNTS_FILE = BASE_DIR / "managed_bot_accounts.json"
LOG_DIR = BASE_DIR / "botlogs"
BOT_SCRIPT = BASE_DIR / "simple_bot.py"
BOT_EXECUTABLE = BASE_DIR / "TLBBBotEngine.exe"
PROGRESSION_TEMPLATES_FILE = BASE_DIR / "role_progression_templates.json"
SERVER_CONFIG_FILE = BASE_DIR / "server_config.json"
HUNT_PLAN_FILE = BASE_DIR / "hunt_plan.json"
HUNT_SCENE_IDS = (3, 4, 5, 8, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35)
DEFAULT_HUNT_SCENE_IDS = (3, 4, 5, 8, 18, 19, 20, 21, 24, 25, 26, 27, 30, 31, 32, 33)
HUNT_SCENE_NAMES = {
    3: "嵩山", 4: "太湖", 5: "镜湖", 8: "敦煌",
    18: "雁南", 19: "雁北", 20: "草原", 21: "辽西",
    22: "长白山", 23: "黄龙府",
    24: "洱海", 25: "苍山", 26: "石林", 27: "玉溪",
    28: "南诏", 29: "苗疆", 30: "西湖", 31: "龙泉",
    32: "武夷", 33: "梅岭", 34: "南海", 35: "琼州",
}
DEFAULT_ABILITY_IDS = (1, 4, 26, 38, 46, 47, 48, 50, 51, 57, 58, 59, 60, 61, 62, 63, 64)
DEFAULT_SERVER_CONFIG = {
    "login_host": "127.0.0.1",
    "login_port": 6500,
    "game_host": "",
    "game_port": None,
    "db_host": "127.0.0.1",
    "db_port": 3306,
    "db_user": "",
    "db_password": "",
    "enable_database_writes": False,
    "enable_server_provisioning": False,
    "supported_login_hosts": [],
}


def load_server_config() -> dict:
    config = DEFAULT_SERVER_CONFIG.copy()
    if SERVER_CONFIG_FILE.exists():
        try:
            saved = json.loads(SERVER_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update({key: saved[key] for key in config if key in saved})
        except (OSError, ValueError):
            pass
    return config


def apply_server_config(config: dict) -> None:
    # Require literal booleans: JSON strings must never enable mutations.
    for flag in ("enable_database_writes", "enable_server_provisioning"):
        if type(config.get(flag, False)) is not bool:
            raise ValueError("功能开关必须使用 JSON 布尔值")
    global SERVER_CONFIG, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
    SERVER_CONFIG = config
    DB_HOST = config["db_host"]
    DB_PORT = int(config["db_port"])
    DB_USER = config["db_user"]
    DB_PASSWORD = config["db_password"]


apply_server_config(load_server_config())
CHAR_TEMPLATE_AID = 123
LOG_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
from public_policy import install_policy
install_policy(app, lambda: SERVER_CONFIG)
lock = threading.RLock()


@dataclass
class BotProcess:
    account: str
    process: object
    log_path: Path


running: dict[str, BotProcess] = {}
status_cache: dict[str, dict] = {}


class AttachedProcess:
    """Small Popen-compatible wrapper for a bot surviving a Web restart."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        handle = KERNEL32.OpenProcess(0x1000, False, self.pid)
        if not handle:
            return 1
        try:
            exit_code = ctypes.c_ulong()
            if not KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return 1
            return None if exit_code.value == 259 else exit_code.value
        finally:
            KERNEL32.CloseHandle(handle)

    def terminate(self) -> None:
        handle = KERNEL32.OpenProcess(0x0001, False, self.pid)
        if handle:
            try:
                KERNEL32.TerminateProcess(handle, 1)
            finally:
                KERNEL32.CloseHandle(handle)

    kill = terminate

    def wait(self, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.05)
        return self.poll()


def scan_bot_processes(account_filter: str | None = None) -> list[tuple[str, int]]:
    """Return every live bot process, optionally restricted to one account."""
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object {(($_.Name -like 'python*') -and $_.CommandLine -like '*simple_bot.py*') "
        "-or $_.Name -eq 'TLBBBotEngine.exe'} | "
        "Select-Object ProcessId,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not result.stdout.strip():
        return []
    rows = json.loads(result.stdout)
    if isinstance(rows, dict):
        rows = [rows]
    found: list[tuple[str, int]] = []
    for row in rows:
        if not owns_process(row, BASE_DIR):
            continue
        command = str(row.get("CommandLine") or "")
        match = re.search(r'(?:^|\s)--account\s+(?:"([^"]+)"|(\S+))', command)
        if not match:
            continue
        account = match.group(1) or match.group(2)
        if account_filter is None or account == account_filter:
            found.append((account, int(row["ProcessId"])))
    return found


def recover_running_processes() -> None:
    """Reattach bots which outlived a previous Web console process."""
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object {(($_.Name -like 'python*') -and $_.CommandLine -like '*simple_bot.py*') "
        "-or $_.Name -eq 'TLBBBotEngine.exe'} | "
        "Select-Object ProcessId,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not result.stdout.strip():
            return
        rows = json.loads(result.stdout)
        if isinstance(rows, dict):
            rows = [rows]
        recovered = 0
        for row in rows:
            if not owns_process(row, BASE_DIR):
                continue
            command = str(row.get("CommandLine") or "")
            match = re.search(r"(?:^|\s)--account\s+(?:\"([^\"]+)\"|(\S+))", command)
            if not match:
                continue
            account = match.group(1) or match.group(2)
            pid = int(row["ProcessId"])
            process = AttachedProcess(pid)
            if process.poll() is None:
                running[account] = BotProcess(
                    account,
                    process,
                    LOG_DIR / f"{safe_name(account)}.log",
                )
                recovered += 1
        if recovered:
            app.logger.info("已重新接管 %d 个在线机器人进程", recovered)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        app.logger.warning("扫描在线机器人进程失败: %s", exc)


def safe_name(account: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", account)


def encode_role_name(name: str) -> bytes:
    """Encode a visible role name for the server's latin1 byte-storage column."""
    return name.encode("gb18030")


def load_accounts() -> list[str]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return [str(item) for item in data if isinstance(item, str)]
    except (OSError, ValueError):
        return []


def save_accounts(accounts: list[str]) -> None:
    atomic_json(DATA_FILE,sorted(set(accounts)))


def load_managed_accounts() -> set[str]:
    if not MANAGED_ACCOUNTS_FILE.exists():
        return set()
    try:
        data = json.loads(MANAGED_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return {str(item) for item in data if isinstance(item, str)}
    except (OSError, ValueError, TypeError):
        return set()


def save_managed_accounts(accounts: set[str]) -> None:
    atomic_json(MANAGED_ACCOUNTS_FILE,sorted(accounts))


def is_managed_bot(account: str) -> bool:
    return account in load_managed_accounts()


HUNT_LOCK = threading.RLock()
IMPORT_PREVIEWS = {}


def serialized_hunt(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with HUNT_LOCK:
            return function(*args, **kwargs)
    return wrapped


def hunt_api(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with HUNT_LOCK:
            try:
                return function(*args, **kwargs)
            except ValueError as exc:
                return jsonify(ok=False,message=str(exc)),400
            except OSError as exc:
                return jsonify(ok=False,message=f'队伍配置读写失败：{exc}'),500
    return wrapped


def empty_hunt_plan():
    return {"format":1,"enabled":False,"leader":"","leader_role":"",
            "leader_guid":0,"members":[],"mode":"random",
            "single_scene_id":None,"scene_ids":list(DEFAULT_HUNT_SCENE_IDS)}


def load_hunt_plan(team_id=None,account=None):
    if account is not None:
        return plan_for_account(HUNT_PLAN_FILE,account) or empty_hunt_plan()
    return plan_for_id(HUNT_PLAN_FILE,team_id) or empty_hunt_plan()


def team_is_running(plan):
    return any(a in running and running[a].process.poll() is None for a in accounts_for(plan))


def role_identities_for_accounts(accounts: list[str]) -> dict[str, tuple[str, int]]:
    placeholders = ",".join(["%s"] * len(accounts))
    connection = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        charset="latin1", connect_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT accname, charguid, HEX(charname) AS name_hex FROM tlbbdb_main.t_char "
                f"WHERE BINARY accname IN ({placeholders})", accounts,
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    result = {}
    for row in rows:
        account = str(row["accname"])
        raw_name = bytes.fromhex(str(row["name_hex"] or ""))
        name = raw_name.split(b"\0", 1)[0].decode("gb18030", "strict")
        if name:
            result[account] = (name, int(row["charguid"]))
    return result


def batch_create_accounts(payload: dict) -> dict:
    prefix = str(payload.get("prefix", "bot")).strip()
    role_prefix = str(payload.get("role_prefix", "Bot")).strip()
    validate_new_role_name(role_prefix)
    password = str(payload.get("password", "2"))
    sex_option = str(payload.get("sex", "random"))
    # The second base hairstyle (model pair 2/3) is disabled because it can
    # make the head mesh disappear on this client build.
    hair_option = "style1"
    menpai_option = str(payload.get("menpai", "random"))
    name_source = str(payload.get("name_source", "builtin"))
    try:
        start = int(payload.get("start", 1))
        count = int(payload.get("count", 10))
        level = int(payload.get("level", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("起始编号和数量必须是整数") from exc
    if not re.fullmatch(r"[A-Za-z0-9_]{1,24}", prefix):
        raise ValueError("账号前缀只能使用字母、数字和下划线")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", role_prefix):
        raise ValueError("角色前缀只能使用字母、数字和下划线")
    if name_source not in ("builtin", "prefix"):
        raise ValueError("角色名来源选项不正确")
    if start < 0 or count < 1 or count > 100:
        raise ValueError("单次创建数量必须是 1 到 100")
    if len(password) < 1 or len(password) > 32:
        raise ValueError("密码长度必须是 1 到 32")
    if level < 1 or level > 119:
        raise ValueError("角色等级必须是 1 到 119")
    if sex_option not in ("random", "0", "1"):
        raise ValueError("性别选项不正确")
    if menpai_option != "random":
        try:
            menpai_value = int(menpai_option)
        except ValueError as exc:
            raise ValueError("门派选项不正确") from exc
        if menpai_value not in (*range(9), 10):
            raise ValueError("门派编号必须是 0 到 8 或 10")
    else:
        menpai_value = -1
    rng = random.SystemRandom()
    available_role_names = list(SAFE_ROLE_NAMES)
    rng.shuffle(available_role_names)
    try:
        progression_document = json.loads(
            PROGRESSION_TEMPLATES_FILE.read_text(encoding="utf-8")
        )
        progression_templates = {
            int(school): template
            for school, template in progression_document.items()
            if str(school).isdigit()
            and template.get("skills")
            and template.get("xinfa")
        }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"无法加载门派技能模板: {exc}") from exc
    if not progression_templates:
        raise RuntimeError("门派技能模板为空")
    if menpai_value >= 0:
        if menpai_value not in progression_templates:
            available = ",".join(str(value) for value in sorted(progression_templates))
            raise ValueError(
                f"门派 {menpai_value} 暂无完整技能模板；当前可用门派：{available}"
            )
    available_schools = tuple(sorted(progression_templates))

    equipment_template = None
    equipment_guid = payload.get("equipment_source_guid")
    if equipment_guid not in (None, ""):
        if not configured_host(BASE_DIR, SERVER_CONFIG["login_host"]):
            raise ValueError("复制装备仅允许创建到测试服")
        try:
            equipment_guid = int(equipment_guid)
        except (TypeError, ValueError) as exc:
            raise ValueError("请搜索并选择装备模板角色") from exc
        equipment_template = load_template(BASE_DIR, equipment_guid)
        validate_template(BASE_DIR, equipment_template, level,
                          available_schools if menpai_value < 0 else (menpai_value,))

    connection = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=8,
        read_timeout=20,
        write_timeout=20,
    )
    created: list[str] = []
    existing: list[str] = []
    try:
        with connection.cursor() as cursor:
            # Account IDs are owned by MySQL AUTO_INCREMENT. Refuse to create
            # anything if its metadata is behind the existing rows; never
            # guess or manually reuse an account ID.
            cursor.execute("SELECT COALESCE(MAX(id),0) FROM web.account")
            max_account_id = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA='web' AND TABLE_NAME='account'"
            )
            auto_row = cursor.fetchone()
            next_account_id = int(auto_row[0]) if auto_row and auto_row[0] else 1
            if next_account_id <= max_account_id:
                raise RuntimeError(
                    "账号ID自增计数异常，已停止创建且未写入任何数据："
                    f"MAX(id)={max_account_id}, AUTO_INCREMENT={next_account_id}"
                )
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA='tlbbdb_main' AND TABLE_NAME='t_char' "
                "AND COLUMN_NAME<>'aid' ORDER BY ORDINAL_POSITION"
            )
            columns = [row[0] for row in cursor.fetchall()]
            if not columns:
                raise RuntimeError("无法读取角色表结构")
            cursor.execute(
                "SELECT aid FROM tlbbdb_main.t_char WHERE aid=%s", (CHAR_TEMPLATE_AID,)
            )
            if cursor.fetchone() is None:
                raise RuntimeError("一级角色模板不存在")
            # The game server and the Web creator share this allocator. Lock
            # it before reading MAX(charguid), then hold the row lock until all
            # new characters and the counter update commit atomically. This
            # prevents a normal player and a bot from receiving the same GUID.
            cursor.execute(
                "SELECT maxcharguid FROM tlbbdb_main.t_var "
                "WHERE worldid=10 FOR UPDATE"
            )
            counter_row = cursor.fetchone()
            if counter_row is None:
                raise RuntimeError("角色GUID计数器 t_var.worldid=10 不存在，已停止创建")
            counter_before = int(counter_row[0])
            cursor.execute(
                "SELECT COALESCE(MAX(charguid),100000000) FROM tlbbdb_main.t_char"
            )
            table_max_guid = int(cursor.fetchone()[0])
            if table_max_guid > counter_before:
                cursor.execute(
                    "UPDATE tlbbdb_main.t_var SET maxcharguid=%s WHERE worldid=10",
                    (table_max_guid,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("修复角色GUID计数器失败，已停止创建")
                counter_before = table_max_guid
            next_guid = max(counter_before, table_max_guid) + 1

            quoted_columns = ",".join(f"`{name}`" for name in columns)
            select_expressions = []
            for name in columns:
                if name == "charname":
                    # t_char.charname is a latin1 byte container for GB18030
                    # names. UNHEX avoids all connection collation conversions.
                    select_expressions.append("CONVERT(UNHEX(%s) USING latin1)")
                elif name in (
                    "accname", "charguid", "charname", "level", "sex", "menpai",
                    "hairmodel", "facemodel"
                ):
                    select_expressions.append("%s")
                else:
                    select_expressions.append(f"`{name}`")
            clone_sql = (
                f"INSERT INTO tlbbdb_main.t_char ({quoted_columns}) "
                f"SELECT {','.join(select_expressions)} FROM tlbbdb_main.t_char WHERE aid=%s"
            )

            for number in range(start, start + count):
                account = f"{prefix}{number}@game.sohu.com"
                if len(account) > 50:
                    raise ValueError(f"编号 {number} 生成的账号过长")
                cursor.execute("SELECT 1 FROM web.account WHERE name=%s", (account,))
                account_exists = cursor.fetchone() is not None
                cursor.execute(
                    "SELECT 1 FROM tlbbdb_main.t_char WHERE accname=%s LIMIT 1", (account,)
                )
                char_exists = cursor.fetchone() is not None
                if account_exists or char_exists:
                    existing.append(account)
                    continue
                role_name = f"{role_prefix}{number}"
                if name_source == "builtin":
                    while available_role_names:
                        candidate = available_role_names.pop()
                        cursor.execute(
                            "SELECT 1 FROM tlbbdb_main.t_char "
                            "WHERE BINARY charname=UNHEX(%s) LIMIT 1",
                            (encode_role_name(candidate).hex(),),
                        )
                        if cursor.fetchone() is None:
                            role_name = candidate
                            break
                    else:
                        role_name = f"{role_prefix}{number}_{next_guid}"
                validate_new_role_name(role_name)
                if len(role_name.encode("gb18030")) > 50:
                    raise ValueError(f"角色名 {role_name} 过长")
                cursor.execute(
                    "INSERT INTO web.account (name,password) VALUES (%s,%s)",
                    (account, hashlib.md5(password.encode("utf-8")).hexdigest()),
                )
                if name_source == "prefix":
                    cursor.execute(
                        "SELECT 1 FROM tlbbdb_main.t_char "
                        "WHERE BINARY charname=UNHEX(%s) LIMIT 1",
                        (encode_role_name(role_name).hex(),),
                    )
                    if cursor.fetchone() is not None:
                        role_name = f"{role_prefix}{number}_{next_guid}"
                values = []
                selected_sex = rng.randrange(2) if sex_option == "random" else int(sex_option)
                selected_menpai = (
                    rng.choice(available_schools) if menpai_value < 0 else menpai_value
                )
                selected_hair_style = 0
                # CharHairGeo uses adjacent sex-specific model pairs:
                # style 1 => 0/1, style 2 => 2/3.
                selected_hair_model = selected_hair_style * 2 + selected_sex
                selected_face_model = 1 if selected_sex == 0 else 100
                for name in columns:
                    if name == "accname":
                        values.append(account)
                    elif name == "charguid":
                        values.append(next_guid)
                    elif name == "charname":
                        values.append(encode_role_name(role_name).hex())
                    elif name == "level":
                        values.append(level)
                    elif name == "sex":
                        values.append(selected_sex)
                    elif name == "menpai":
                        values.append(selected_menpai)
                    elif name == "hairmodel":
                        values.append(selected_hair_model)
                    elif name == "facemodel":
                        values.append(selected_face_model)
                values.append(CHAR_TEMPLATE_AID)
                cursor.execute(clone_sql, values)

                progression = progression_templates[selected_menpai]
                cursor.executemany(
                    "INSERT INTO tlbbdb_main.t_skill "
                    "(charguid,skid,sktime,dbversion,isvalid) "
                    "VALUES (%s,%s,NULL,0,NULL)",
                    [(next_guid, int(skill_id)) for skill_id in progression["skills"]],
                )
                if cursor.rowcount <= 0:
                    raise RuntimeError(f"门派 {selected_menpai} 技能初始化失败")
                cursor.executemany(
                    "INSERT INTO tlbbdb_main.t_xinfa "
                    "(charguid,xinfaid,xinfalvl,dbversion) "
                    "VALUES (%s,%s,%s,0)",
                    [
                        (next_guid, int(row["id"]), int(row["level"]))
                        for row in progression["xinfa"]
                    ],
                )
                if cursor.rowcount <= 0:
                    raise RuntimeError(f"门派 {selected_menpai} 心法初始化失败")
                if equipment_template is not None:
                    insert_equipment(cursor, equipment_template, next_guid, encode_role_name(role_name))
                cursor.executemany(
                    "INSERT INTO tlbbdb_main.t_ability "
                    "(charguid,abid,ablvl,abexp,dbversion,isvalid) "
                    "VALUES (%s,%s,1,0,0,1)",
                    [(next_guid, ability_id) for ability_id in DEFAULT_ABILITY_IDS],
                )
                next_guid += 1
                created.append(account)
            if created:
                # The game server allocates manually-created character GUIDs
                # from t_var. Keep it in sync with characters inserted here,
                # otherwise its next GUID can collide with a Web-created role.
                cursor.execute(
                    "UPDATE tlbbdb_main.t_var "
                    "SET maxcharguid=GREATEST(maxcharguid,%s) WHERE worldid=10",
                    (next_guid - 1,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("角色GUID计数器同步失败，整批创建已回滚")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    accounts = load_accounts()
    accounts.extend(created)
    accounts.extend(existing)
    save_accounts(accounts)
    managed_accounts = load_managed_accounts()
    managed_accounts.update(created)
    save_managed_accounts(managed_accounts)
    return {"created": created, "existing": existing,
            "equipment_source": equipment_template["role"]["charname"] if equipment_template else None,
            "equipment_per_role": len(equipment_template["items"]) if equipment_template else 0}


def log_tail(path: Path, max_bytes: int = 24_000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def log_increment(account: str, path: Path) -> tuple[str, dict | None]:
    """Read one login session once, then only bytes appended since last poll."""
    if not path.exists():
        return "", status_cache.get(account)
    size = path.stat().st_size
    cached = status_cache.get(account)
    if cached and size >= cached.get("offset", 0):
        with path.open("rb") as handle:
            handle.seek(cached["offset"])
            data = handle.read()
        cached["offset"] = size
        return data.decode("utf-8", errors="replace"), cached

    # On Web restart, locate the latest login within the existing log. This is
    # done only once; normal status refreshes consume appended bytes only.
    max_session_bytes = 64 * 1024 * 1024
    with path.open("rb") as handle:
        handle.seek(max(0, size - max_session_bytes))
        data = handle.read()
    marker = "角色已进入游戏 name=".encode("utf-8")
    start = data.rfind(marker)
    if start >= 0:
        line_start = data.rfind(b"\n", 0, start)
        data = data[line_start + 1 :]
    cached = {"offset": size}
    status_cache[account] = cached
    return data.decode("utf-8", errors="replace"), cached


def parse_status(account: str) -> dict:
    item = running.get(account)
    log_path = item.log_path if item else LOG_DIR / f"{safe_name(account)}.log"
    text, cached = log_increment(account, log_path)
    alive = bool(item and item.process.poll() is None)
    if item and not alive:
        running.pop(account, None)

    result = dict(cached.get("result", {})) if cached else {}
    defaults = {
        "account": account,
        "running": alive,
        "phase": "已停止",
        "role": "",
        "map": None,
        "x": None,
        "y": None,
        "target": None,
        "combat": False,
        "team": False,
        "leader": None,
        "managed": is_managed_bot(account),
    }
    for key, value in defaults.items():
        result.setdefault(key, value)
    result["account"] = account
    result["running"] = alive
    result["managed"] = is_managed_bot(account)
    plan = load_hunt_plan(account=account)
    result["hunt_team_id"] = plan.get("id")
    result["hunt_team_name"] = plan.get("name")
    result["hunt_role"] = (
        "刷马队长" if plan.get("enabled") and account == plan.get("leader") else
        "刷马队员" if plan.get("enabled") and account in {
            item.get("account") for item in plan.get("members", []) if isinstance(item, dict)
        } else None
    )
    patterns = [
        (r"账号认证成功", "认证成功"),
        (r"角色已进入游戏 name=(.*?) map=(\d+) pos=\(([-.\d]+), ([-.\d]+)\)", "游戏中"),
        (r"安全地图待机 map=(\d+)", "安全地图待机"),
        (r"进入野外地图 map=(\d+)", "游戏中"),
        (r"向目标移动 obj=(\d+)", "正在移动"),
        (r"服务端确认进入战斗 target=(\d+)", "自动打怪"),
    ]
    if alive:
        if result["phase"] == "已停止":
            result["phase"] = "正在连接"
        if "场景包 id=0x0306" in text or "发送心跳" in text:
            result["phase"] = "游戏中"
    for line in text.splitlines():
        for pattern, phase in patterns:
            match = re.search(pattern, line)
            if not match:
                continue
            result["phase"] = phase
            if phase == "游戏中":
                if len(match.groups()) == 4:
                    result.update(
                        role=match.group(1),
                        map=int(match.group(2)),
                        x=float(match.group(3)),
                        y=float(match.group(4)),
                    )
                else:
                    result["map"] = int(match.group(1))
            elif phase == "安全地图待机":
                result["map"] = int(match.group(1))
                result["combat"] = False
            elif phase == "正在移动":
                result["target"] = int(match.group(1))
            elif phase == "自动打怪":
                result["target"] = int(match.group(1))
                result["combat"] = True
        move = re.search(r"(?:移动更新|移动到达) pos=\(([-.\d]+), ([-.\d]+)\)", line)
        if move:
            result["x"], result["y"] = float(move.group(1)), float(move.group(2))
        if "目标不可攻击" in line:
            result["combat"] = False
        joined = re.search(r"已自动同意组队邀请 inviter=(.*?) guid=", line)
        if joined:
            result["team"] = True
        leader = re.search(r"已识别队长 obj=(\d+)", line)
        if leader:
            result["team"] = True
            result["leader"] = int(leader.group(1))
        if (
            "已离开队伍" in line
            or "队伍已解散" in line
            or "场景包 id=0x0234 size=15" in line
        ):
            result["team"] = False
            result["leader"] = None
    if cached is not None:
        cached["result"] = dict(result)
    cache_path=BASE_DIR/'managed_role_cache.json'
    if not alive and cache_path.exists():
        try:
            entry=json.loads(cache_path.read_text(encoding='utf-8')).get(account,{})
            if entry.get('role'):result['role']=entry['role']
        except (OSError,ValueError,AttributeError):pass
    return result


def ensure_starter_weapon(account: str) -> tuple[str, bool]:
    """Give an offline, empty Web-created role a starter blade in bag slot 1."""
    connection = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        charset="latin1",
        autocommit=False,
        connect_timeout=8,
        read_timeout=20,
        write_timeout=20,
    )
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT charguid FROM tlbbdb_main.t_char "
                "WHERE BINARY accname=%s ORDER BY aid DESC LIMIT 1 FOR UPDATE",
                (account,),
            )
            role = cursor.fetchone()
            if not role:
                connection.rollback()
                return "账号还没有角色，未执行武器穿戴", False
            charguid = int(role["charguid"])
            cursor.execute(
                "SELECT aid,pos FROM tlbbdb_main.t_iteminfo "
                "WHERE charguid=%s AND isvalid=1 AND pos=240 "
                "LIMIT 1",
                (charguid,),
            )
            if cursor.fetchone():
                connection.rollback()
                return "角色已装备武器，已跳过自动穿戴", False

            cursor.execute(
                "SELECT aid,pos FROM tlbbdb_main.t_iteminfo "
                "WHERE charguid=%s AND isvalid=1 AND itemtype=10100100 "
                "AND pos BETWEEN 0 AND 119 LIMIT 1",
                (charguid,),
            )
            if cursor.fetchone():
                connection.rollback()
                return "背包已有新手武器，允许自动穿戴", True
            cursor.execute(
                "SELECT aid FROM tlbbdb_main.t_iteminfo "
                "WHERE charguid=%s AND isvalid=1 AND pos=1 LIMIT 1",
                (charguid,),
            )
            if cursor.fetchone():
                connection.rollback()
                return "背包第二格已有其他物品，已跳过自动穿戴", False

            cursor.execute(
                "SELECT guid FROM tlbbdb_main.t_iteminfo WHERE guid>0 "
                "ORDER BY guid DESC LIMIT 1 FOR UPDATE"
            )
            guid_row = cursor.fetchone()
            new_guid = int(guid_row["guid"] if guid_row else 0) + 1
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA='tlbbdb_main' AND TABLE_NAME='t_iteminfo' "
                "AND COLUMN_NAME<>'aid' ORDER BY ORDINAL_POSITION"
            )
            columns = [row["COLUMN_NAME"] for row in cursor.fetchall()]
            select_parts = []
            params = []
            for column in columns:
                if column == "charguid":
                    select_parts.append("%s")
                    params.append(charguid)
                elif column == "guid":
                    select_parts.append("%s")
                    params.append(new_guid)
                elif column == "pos":
                    select_parts.append("1")
                else:
                    select_parts.append(f"`{column}`")
            quoted = ",".join(f"`{column}`" for column in columns)
            cursor.execute(
                f"INSERT INTO tlbbdb_main.t_iteminfo ({quoted}) "
                f"SELECT {','.join(select_parts)} FROM tlbbdb_main.t_iteminfo "
                "WHERE itemtype=10100100 AND isvalid=1 ORDER BY aid DESC LIMIT 1",
                tuple(params),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("数据库里没有可用的大砍刀模板")
        connection.commit()
        return f"已向背包第二格补发大砍刀（物品GUID {new_guid}）", True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_healing_gourds(account: str, minimum: int = 2) -> str:
    """Ensure an offline role owns at least ``minimum`` healing gourds."""
    connection = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        charset="latin1", autocommit=False, connect_timeout=8,
        read_timeout=20, write_timeout=20,
    )
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT charguid FROM tlbbdb_main.t_char "
                "WHERE BINARY accname=%s ORDER BY aid DESC LIMIT 1 FOR UPDATE",
                (account,),
            )
            role = cursor.fetchone()
            if not role:
                connection.rollback()
                return "账号还没有角色，首次创建角色后下次启动会补发仙丹葫芦"
            charguid = int(role["charguid"])
            cursor.execute(
                "SELECT COUNT(*) AS amount FROM tlbbdb_main.t_iteminfo "
                "WHERE charguid=%s AND itemtype=31000001 AND isvalid=1",
                (charguid,),
            )
            amount = int(cursor.fetchone()["amount"])
            missing = max(0, minimum - amount)
            if missing == 0:
                connection.rollback()
                return f"仙丹葫芦数量充足（{amount} 个）"

            cursor.execute(
                "SELECT pos FROM tlbbdb_main.t_iteminfo "
                "WHERE charguid=%s AND isvalid=1 AND pos BETWEEN 0 AND 119",
                (charguid,),
            )
            occupied = {int(row["pos"]) for row in cursor.fetchall()}
            free_positions = [pos for pos in range(120) if pos not in occupied]
            if len(free_positions) < missing:
                raise RuntimeError("背包空位不足，无法补发仙丹葫芦")

            cursor.execute(
                "SELECT guid FROM tlbbdb_main.t_iteminfo WHERE guid>0 "
                "ORDER BY guid DESC LIMIT 1 FOR UPDATE"
            )
            guid_row = cursor.fetchone()
            next_guid = int(guid_row["guid"] if guid_row else 0) + 1
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA='tlbbdb_main' AND TABLE_NAME='t_iteminfo' "
                "AND COLUMN_NAME<>'aid' ORDER BY ORDINAL_POSITION"
            )
            columns = [row["COLUMN_NAME"] for row in cursor.fetchall()]
            quoted = ",".join(f"`{column}`" for column in columns)
            created = []
            for offset in range(missing):
                new_guid = next_guid + offset
                new_pos = free_positions[offset]
                select_parts, params = [], []
                for column in columns:
                    if column == "charguid":
                        select_parts.append("%s"); params.append(charguid)
                    elif column == "guid":
                        select_parts.append("%s"); params.append(new_guid)
                    elif column == "pos":
                        select_parts.append("%s"); params.append(new_pos)
                    else:
                        select_parts.append(f"`{column}`")
                cursor.execute(
                    f"INSERT INTO tlbbdb_main.t_iteminfo ({quoted}) "
                    f"SELECT {','.join(select_parts)} FROM tlbbdb_main.t_iteminfo "
                    "WHERE itemtype=31000001 AND isvalid=1 ORDER BY aid DESC LIMIT 1",
                    tuple(params),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("数据库里没有可用的仙丹葫芦模板")
                created.append((new_guid, new_pos))
        connection.commit()
        detail = "、".join(f"格{pos}/GUID {guid}" for guid, pos in created)
        return f"已补发 {len(created)} 个仙丹葫芦（{detail}）"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_healing_gourd_reference(account: str) -> dict[str, int] | None:
    """Load the exact database item reference needed by CGUseItem."""
    connection = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        charset="latin1", connect_timeout=8, read_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT i.pos,i.guid,i.world,i.p1 FROM tlbbdb_main.t_char c "
                "JOIN tlbbdb_main.t_iteminfo i ON i.charguid=c.charguid "
                "WHERE BINARY c.accname=%s AND i.itemtype=31000001 "
                "AND i.isvalid=1 AND i.p1>0 ORDER BY i.aid LIMIT 1",
                (account,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {key: int(row[key]) for key in ("pos", "guid", "world", "p1")}
    finally:
        connection.close()


def get_first_xinfa_skill(account: str) -> int:
    """Return the first skill of the role's first school xinfa."""
    connection = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        charset="latin1", connect_timeout=8, read_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT menpai FROM tlbbdb_main.t_char "
                "WHERE BINARY accname=%s LIMIT 2", (account,)
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise RuntimeError(f"账号角色数量不是1：{len(rows)}")
            school = str(int(rows[0]["menpai"]))
    finally:
        connection.close()
    document = json.loads(PROGRESSION_TEMPLATES_FILE.read_text(encoding="utf-8"))
    template = document.get(school)
    if not template or not template.get("skills"):
        raise RuntimeError(f"门派 {school} 没有技能模板")
    school_skills = [int(skill) for skill in template["skills"] if 280 <= int(skill) < 1000]
    if not school_skills:
        raise RuntimeError(f"门派 {school} 没有可用的第一心法技能")
    return school_skills[0]


@serialized_hunt
def start_bot(account: str) -> tuple[bool, str]:
    if SERVER_CONFIG.get("enable_database_writes") is not True:
        return False, "源码公开版默认关闭启动前数据库维护；请先完成版本适配，阅读 docs/ADAPTING.md"
    account = account.strip()
    if not account or len(account) > 50:
        return False, "账号格式不正确"
    with lock:
        current = running.get(account)
        if current and current.process.poll() is None:
            return False, "该账号已经运行"
        managed = is_managed_bot(account)
        gourd = None
        try:
            combat_skill = get_first_xinfa_skill(account)
        except Exception as exc:
            return False, f"启动前读取门派攻击技能失败：{exc}"
        allow_auto_equip = False
        if managed:
            try:
                durability_result = ensure_full_durability(BASE_DIR, SERVER_CONFIG, account)
            except Exception as exc:
                return False, f"启动前补满装备耐久失败：{exc}"
            try:
                weapon_result, allow_auto_equip = ensure_starter_weapon(account)
            except Exception as exc:
                return False, f"启动前补发新手武器失败：{exc}"
            try:
                gourd_result = ensure_healing_gourds(account)
            except Exception as exc:
                return False, f"启动前补发仙丹葫芦失败：{exc}"
            try:
                gourd = get_healing_gourd_reference(account)
            except Exception as exc:
                return False, f"启动前读取仙丹葫芦失败：{exc}"
            if not gourd:
                return False, "启动前未找到可用的仙丹葫芦"
        else:
            durability_result = "非受管账号，未修改装备耐久"
            weapon_result = "非受管账号，数据库写入保护已启用"
            gourd_result = "未检查或补发背包道具"
        log_path = LOG_DIR / f"{safe_name(account)}.log"
        with log_path.open("a", encoding="utf-8") as startup_log:
            startup_log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] INFO {durability_result}\n")
        command = [
            str(BOT_EXECUTABLE) if getattr(sys, "frozen", False) else sys.executable,
        ]
        if not getattr(sys, "frozen", False):
            command.extend(["-u", str(BOT_SCRIPT)])
        command.extend([
            "--account",
            account,
            "--auto-fight",
            "--log",
            str(log_path),
            "--login-host",
            str(SERVER_CONFIG["login_host"]),
            "--login-port",
            str(SERVER_CONFIG["login_port"]),
            "--combat-skill",
            str(combat_skill),
        ])
        if gourd:
            command.extend(
                [
                    "--heal-slot", str(gourd["pos"]),
                    "--heal-guid", str(gourd["guid"]),
                    "--heal-world", str(gourd["world"]),
                ]
            )
        if not allow_auto_equip:
            command.append("--skip-auto-equip")
        if SERVER_CONFIG.get("game_host"):
            command.extend(["--game-host", str(SERVER_CONFIG["game_host"])])
        if SERVER_CONFIG.get("game_port"):
            command.extend(["--game-port", str(SERVER_CONFIG["game_port"])])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=(LOG_DIR / f"{safe_name(account)}.error.log").open("ab"),
            creationflags=creationflags,
        )
        running[account] = BotProcess(account, process, log_path)
        accounts = load_accounts()
        accounts.append(account)
        save_accounts(accounts)
    return True, f"已启动；{durability_result}；{weapon_result}；{gourd_result}"


@serialized_hunt
def stop_bot(account: str) -> tuple[bool, str]:
    with lock:
        item = running.pop(account, None)
        pids: set[int] = set()
        if item and item.process.poll() is None:
            pids.add(int(item.process.pid))
        try:
            pids.update(pid for _, pid in scan_bot_processes(account))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            app.logger.warning("扫描账号进程失败 account=%s error=%s", account, exc)
        if not pids:
            return False, "该账号没有运行"
        for pid in sorted(pids):
            process = AttachedProcess(pid)
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        time.sleep(0.2)
        try:
            remaining = [pid for _, pid in scan_bot_processes(account)]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return False, f"已发送停止指令，但复核进程失败：{exc}"
        if remaining:
            return False, f"停止失败，仍有机器人进程：{remaining}"
    return True, f"已停止并确认退出，共关闭 {len(pids)} 个机器人进程"


@app.get("/")
def index():
    return render_template("index.html",role_name_count=len(SAFE_ROLE_NAMES))


@app.get("/api/server-config")
def api_get_server_config():
    return jsonify(SERVER_CONFIG)


@app.post("/api/server-config")
@hunt_api
def api_save_server_config():
    data = request.get_json(silent=True) or {}
    config = {}
    for key in ("login_host", "game_host", "db_host", "db_user", "db_password"):
        config[key] = str(data.get(key, "")).strip()
    if not config["login_host"] or not config["db_host"] or not config["db_user"]:
        return jsonify(ok=False, message="登录服务器、数据库地址和数据库账号不能为空"), 400
    try:
        config["login_port"] = int(data.get("login_port"))
        config["db_port"] = int(data.get("db_port"))
        game_port = data.get("game_port")
        config["game_port"] = int(game_port) if str(game_port or "").strip() else None
    except (TypeError, ValueError):
        return jsonify(ok=False, message="端口必须是数字"), 400
    ports = [config["login_port"], config["db_port"]]
    if config["game_port"] is not None:
        ports.append(config["game_port"])
    if any(port < 1 or port > 65535 for port in ports):
        return jsonify(ok=False, message="端口范围必须是 1 到 65535"), 400
    if bool(config["game_host"]) != (config["game_port"] is not None):
        return jsonify(ok=False, message="游戏服覆盖地址和端口必须同时填写或同时留空"), 400
    global LOG_DIR
    switching=server_key(config)!=server_key(SERVER_CONFIG)
    if switching and (any(item.process.poll() is None for item in running.values()) or scan_bot_processes()):
        return jsonify(ok=False,message='切换服务器前请先停止所有假人，避免角色名单和配置混用'),409
    old=SERVER_CONFIG.copy()
    # A server switch must not inherit the previous server's mutation opt-ins.
    for flag in ("enable_database_writes", "enable_server_provisioning"):
        config[flag] = False if switching else old.get(flag, False)
    config["supported_login_hosts"] = [] if switching else old.get("supported_login_hosts", [])
    try:
        directory=bind_profile(BASE_DIR,config)
        atomic_json(SERVER_CONFIG_FILE,config)
    except Exception:
        bind_profile(BASE_DIR,old)
        raise
    apply_server_config(config);LOG_DIR=directory
    if switching:
        running.clear();IMPORT_PREVIEWS.clear()
    return jsonify(ok=True,message='服务器设置已保存，已载入该服务器的角色和队伍配置',config=config)


@app.get("/api/bots")
@hunt_api
def api_bots():
    with lock:
        return jsonify([parse_status(account) for account in load_accounts()])


@app.get("/api/equipment-roles")
def api_equipment_roles():
    try:
        return jsonify(ok=True, roles=search_roles(BASE_DIR, request.args.get("q", "")))
    except (ValueError, OSError, pymysql.MySQLError) as exc:
        return jsonify(ok=False, message=f"装备模板查询失败：{exc}"), 400


@app.get("/api/hunt-teams")
@hunt_api
def api_get_hunt_teams():
    teams=load_teams(HUNT_PLAN_FILE)
    return jsonify(ok=True,teams=[dict(p,running=team_is_running(p),
        shout=load_shout_settings(BASE_DIR,p['leader'])) for p in teams],
        accounts=sorted(load_managed_accounts() & set(load_accounts())),
        maps=[dict(id=i,name=HUNT_SCENE_NAMES[i]) for i in HUNT_SCENE_IDS],
        default_scene_ids=list(DEFAULT_HUNT_SCENE_IDS))


@app.get("/api/hunt-plan")
@hunt_api
def api_get_hunt_plan():
    return jsonify({**load_hunt_plan(request.args.get('team_id')),
        "accounts":sorted(load_managed_accounts() & set(load_accounts())),
        "maps":[dict(id=i,name=HUNT_SCENE_NAMES[i]) for i in HUNT_SCENE_IDS]})


@app.post("/api/hunt-plan")
@app.post("/api/hunt-teams")
@app.put("/api/hunt-teams/<team_id>")
@hunt_api
def api_save_hunt_plan(team_id=None):
    data=request.get_json(silent=True)
    if not isinstance(data,dict):raise ValueError('队伍设置格式无效')
    teams=load_teams(HUNT_PLAN_FILE)
    if team_id is None and request.path=='/api/hunt-plan':
        previous=load_hunt_plan(data.get('team_id'));team_id=previous.get('id')
    else:
        previous=load_hunt_plan(team_id) if team_id else empty_hunt_plan()
    if team_id is not None and team_is_running(previous):
        return jsonify(ok=False,message='请先停止这支队伍，再修改队长、队员或地图'),409
    if team_id is None:team_id='team_'+uuid.uuid4().hex[:16]
    enabled=data.get('enabled',True)
    if type(enabled) is not bool:raise ValueError('队伍启用状态无效')
    name=data.get('name',previous.get('name','刷马队伍'))
    if not isinstance(name,str) or not 1<=len(name.strip())<=40:raise ValueError('队伍名称须为1至40个字符')
    name=name.strip()
    leader=data.get('leader',previous.get('leader',''))
    if not isinstance(leader,str):raise ValueError('队长账号格式不正确')
    leader=leader.strip()
    members=data.get('members',[m['account'] for m in previous.get('members',[])])
    mode=data.get('mode',previous.get('mode','random'))
    if mode not in ("single", "random"):
        return jsonify(ok=False, message="刷马地图模式不正确"), 400
    try:
        single_scene_id = int(data.get("single_scene_id", previous.get("single_scene_id"))) if mode == "single" else None
        scene_ids = (
            [single_scene_id] if mode == "single" else
            [int(item) for item in data.get("scene_ids", previous.get("scene_ids", list(DEFAULT_HUNT_SCENE_IDS)))]
        )
    except (TypeError, ValueError):
        return jsonify(ok=False, message="刷马地图编号不正确"), 400
    if (
        len(scene_ids) != len(set(scene_ids))
        or not set(scene_ids).issubset(HUNT_SCENE_IDS)
        or (mode == "single" and len(scene_ids) != 1)
        or (mode == "random" and not 2 <= len(scene_ids) <= len(HUNT_SCENE_IDS))
    ):
        return jsonify(ok=False, message="请选择有效的刷马地图；随机模式至少选择两张"), 400
    if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
        return jsonify(ok=False, message="队员账号格式不正确"), 400
    members = [item.strip() for item in members]
    selected = [leader, *members]
    if not configured_host(BASE_DIR, SERVER_CONFIG["login_host"]):
        return jsonify(ok=False, message="请先在本地版本配置中明确声明已适配的服务器"), 400
    if not 2 <= len(selected) <= 6 or any(not item for item in selected):
        return jsonify(ok=False, message="请选择 1 名队长和 1 至 5 名队员"), 400
    if len(set(selected)) != len(selected):
        return jsonify(ok=False, message="同一个账号不能重复选择"), 400
    allowed = load_managed_accounts() & set(load_accounts())
    if any(item not in allowed for item in selected):
        return jsonify(ok=False, message="只能选择控制中心中的受管假人"), 400
    if any((item in running and running[item].process.poll() is None) for item in selected):
        return jsonify(ok=False, message="请先停止已选假人，再保存刷马队伍"), 409
    for other in teams:
        if other['id']!=team_id and set(selected).intersection(accounts_for(other)):
            return jsonify(ok=False,message=f"账号已分配给队伍“{other['name']}”，请先从原队伍移除"),409
    try:
        identities = role_identities_for_accounts(selected)
    except Exception as exc:
        app.logger.exception("读取刷马角色名失败")
        return jsonify(ok=False, message=f"读取角色名失败：{exc}"), 500
    if any(item not in identities for item in selected):
        return jsonify(ok=False, message="已选账号缺少可用角色"), 400
    plan = {
        "format":1,"id":team_id,"name":name,"enabled":enabled,
        "leader":leader,"leader_role":identities[leader][0],"leader_guid":identities[leader][1],
        "members":[dict(account=a,role=identities[a][0],guid=identities[a][1]) for a in members],
        "mode":mode,"single_scene_id":single_scene_id,"scene_ids":scene_ids,
    }
    updated=[p for p in teams if p['id']!=team_id]+[plan]
    # Keep existing card order when editing a team.
    if any(p['id']==team_id for p in teams):updated=[plan if p['id']==team_id else p for p in teams]
    save_teams(HUNT_PLAN_FILE,updated)
    return jsonify(ok=True,message='队伍已保存，启动后生效',plan=plan)


@app.delete("/api/hunt-teams/<team_id>")
@hunt_api
def api_delete_hunt_team(team_id):
    plan=load_hunt_plan(team_id)
    if team_is_running(plan):return jsonify(ok=False,message='请先停止这支队伍再删除配置'),409
    save_teams(HUNT_PLAN_FILE,[p for p in load_teams(HUNT_PLAN_FILE) if p['id']!=team_id])
    return jsonify(ok=True,message='队伍配置已删除，账号和角色保留')


@app.get("/api/leader-shout")
@app.get("/api/hunt-teams/<team_id>/shout")
@hunt_api
def api_get_leader_shout(team_id=None):
    plan=load_hunt_plan(team_id or request.args.get("team_id"))
    settings=load_shout_settings(BASE_DIR,plan.get("leader",""))
    return jsonify(ok=True,settings=settings,leader=plan.get('leader',''))


@app.post("/api/leader-shout")
@app.post("/api/hunt-teams/<team_id>/shout")
@hunt_api
def api_save_leader_shout(team_id=None):
    try:
        plan=load_hunt_plan(team_id or request.args.get("team_id"))
        settings=validate_shout_settings(request.get_json(silent=True),leader=plan.get('leader',''))
        if settings['enabled'] and (not plan.get('enabled') or not is_managed_bot(settings['leader'])):
            raise ValueError('请先保存并启用受管假人的刷马队伍')
        if settings['enabled'] and settings['channel']=='horn':
            sync_speaker_allowlist(BASE_DIR,SERVER_CONFIG)
        save_shout_settings(BASE_DIR,settings)
    except ValueError as exc:return jsonify(ok=False,message=str(exc)),400
    except Exception as exc:return jsonify(ok=False,message=f'保存喊话设置失败：{exc}'),500
    return jsonify(ok=True,message='喊话设置已保存，运行中的队长将自动读取',settings=settings)


@app.post("/api/hunt-plan/start")
@app.post("/api/hunt-teams/<team_id>/start")
@hunt_api
def api_start_hunt_plan(team_id=None):
    plan = load_hunt_plan(team_id or request.args.get("team_id"))
    if not plan.get("enabled"):
        return jsonify(ok=False, message="请先配置刷马队伍"), 400
    selected = [item["account"] for item in plan["members"]] + [plan["leader"]]
    if any((item in running and running[item].process.poll() is None) for item in selected):
        return jsonify(ok=False, message="队伍中已有假人运行，请先停止后统一启动"), 409
    started = []
    try:
        supply=BASE_DIR/'hunt_supply.json'
        if supply.exists() and json.loads(supply.read_text(encoding='utf8')).get('enabled') is True:
            sync_speaker_allowlist(BASE_DIR,SERVER_CONFIG)
    except Exception as exc:
        return jsonify(ok=False,message=f'同步受管假人补刷权限失败：{exc}'),409
    for account in selected:
        ok, message = start_bot(account)
        if not ok:
            for launched in reversed(started):
                stop_bot(launched)
            return jsonify(ok=False, message=f"{account} 启动失败：{message}；已回退本次启动"), 409
        started.append(account)
    return jsonify(ok=True, message=f"已启动 {len(started)} 名刷马假人")


@app.post("/api/hunt-plan/stop")
@app.post("/api/hunt-teams/<team_id>/stop")
@hunt_api
def api_stop_hunt_plan(team_id=None):
    plan = load_hunt_plan(team_id or request.args.get("team_id"))
    selected = [plan.get("leader", "")] + [item["account"] for item in plan.get("members", [])]
    stopped = []
    for account in selected:
        if account:
            ok, _ = stop_bot(account)
            if ok:
                stopped.append(account)
    return jsonify(ok=True, message=f"已停止 {len(stopped)} 名刷马假人")


@app.get("/api/managed-scope")
@hunt_api
def api_get_managed_scope():
    return jsonify(ok=True,server_key=server_key(SERVER_CONFIG),db_host=DB_HOST,db_port=DB_PORT)


@app.get("/api/managed-roles")
@hunt_api
def api_preview_managed_roles():
    prefix=validate_prefix(request.args.get('prefix','bot'))
    try:roles=query_candidates(SERVER_CONFIG,prefix)
    except Exception as exc:
        return jsonify(ok=False,message=f'读取当前服务器角色失败：{exc}'),400
    counts=Counter(r['account'] for r in roles)
    eligible={r['account']:r['guid'] for r in roles if counts[r['account']]==1}
    now=time.monotonic()
    for token in list(IMPORT_PREVIEWS):
        if IMPORT_PREVIEWS[token]['expires']<now:del IMPORT_PREVIEWS[token]
    if len(IMPORT_PREVIEWS)>=64:IMPORT_PREVIEWS.clear()
    token=uuid.uuid4().hex
    IMPORT_PREVIEWS[token]=dict(server=server_key(SERVER_CONFIG),prefix=prefix,
        candidates=eligible,expires=now+300)
    existing=set(load_accounts())
    return jsonify(ok=True,token=token,server=f"{DB_HOST}:{DB_PORT}",prefix=prefix,
        roles=[dict(r,importable=counts[r['account']]==1,listed=r['account'] in existing) for r in roles])


@app.post("/api/managed-roles/import")
@hunt_api
def api_import_managed_roles():
    data=request.get_json(silent=True)
    if not isinstance(data,dict):raise ValueError('导入设置格式无效')
    token=data.get('token');preview=IMPORT_PREVIEWS.get(token) if isinstance(token,str) else None
    if not preview or preview['expires']<time.monotonic() or preview['server']!=server_key(SERVER_CONFIG):
        raise ValueError('预览已失效或服务器已切换，请重新获取角色')
    selected=validate_account_selection(data.get('accounts'))
    if any(a not in preview['candidates'] for a in selected):raise ValueError('只能导入本次预览中可用的账号')
    try:fresh=query_candidates(SERVER_CONFIG,preview['prefix'])
    except Exception as exc:return jsonify(ok=False,message=f'导入前核对数据库失败：{exc}'),400
    counts=Counter(r['account'] for r in fresh)
    current={r['account']:r['guid'] for r in fresh if counts[r['account']]==1}
    if any(current.get(a)!=preview['candidates'][a] for a in selected):
        raise ValueError('所选角色已变化，请重新获取角色')
    accounts=set(load_accounts());managed=load_managed_accounts()
    cache_path=BASE_DIR/'managed_role_cache.json'
    cache=json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    if not isinstance(cache,dict):raise ValueError('角色缓存格式无效')
    cache.update({r['account']:r for r in fresh if r['account'] in selected})
    before={p:p.read_bytes() if p.exists() else None for p in (DATA_FILE,MANAGED_ACCOUNTS_FILE,cache_path)}
    try:
        save_managed_accounts(managed|set(selected));save_accounts(list(accounts|set(selected)))
        atomic_json(cache_path,cache)
    except Exception:
        for p,raw in before.items():
            if raw is None:p.unlink(missing_ok=True)
            else:p.write_bytes(raw)
        raise
    del IMPORT_PREVIEWS[token]
    return jsonify(ok=True,message=f'已导入 {len(selected)} 个受管账号（新增显示 {len(set(selected)-accounts)} 个），未修改服务端数据')


def validate_account_selection(accounts):
    if (not isinstance(accounts,list) or not 1<=len(accounts)<=2000
            or any(not isinstance(a,str) or not a for a in accounts) or len(set(accounts))!=len(accounts)):
        raise ValueError('请选择有效且不重复的账号')
    return accounts


@app.post("/api/bots/batch-remove")
@hunt_api
def api_batch_remove_bots():
    data=request.get_json(silent=True)
    if not isinstance(data,dict):raise ValueError('批量移除设置格式无效')
    selected=validate_account_selection(data.get('accounts'))
    current=set(load_accounts())
    if any(a not in current for a in selected):raise ValueError('所选账号已变化，请刷新后重试')
    plans=load_teams(HUNT_PLAN_FILE);assigned={a for plan in plans for a in accounts_for(plan)}
    if assigned.intersection(selected):return jsonify(ok=False,message='所选账号仍在刷马队伍中，请先编辑或删除对应队伍配置'),409
    if (any(a in running and running[a].process.poll() is None for a in selected)
            or set(selected).intersection(a for a,pid in scan_bot_processes())):
        return jsonify(ok=False,message='请先停止所选假人，再批量移除'),409
    # The managed whitelist remains available for subsequent re-import. No database writes.
    save_accounts(sorted(current-set(selected)))
    return jsonify(ok=True,message=f'已从控制中心移除 {len(selected)} 个账号，服务端账号、角色和装备保留')


@app.post("/api/bots")
@hunt_api
def api_add_bot():
    account = str((request.get_json(silent=True) or {}).get("account", "")).strip()
    accounts = load_accounts()
    if not account or len(account) > 50:
        return jsonify(ok=False, message="请输入正确账号"), 400
    accounts.append(account)
    save_accounts(accounts)
    return jsonify(ok=True)


@app.post("/api/bots/batch-create")
@hunt_api
def api_batch_create_bots():
    try:
        result = batch_create_accounts(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    except Exception as exc:
        app.logger.exception("batch account creation failed")
        return jsonify(ok=False, message=f"批量创建失败：{exc}"), 500
    return jsonify(
        ok=True,
        message=f"创建 {len(result['created'])} 个，跳过 {len(result['existing'])} 个"
                + (f"；{result['repair_warning']}" if result.get("repair_warning") else ""),
        **result,
    )


@app.post("/api/bots/<path:account>/start")
def api_start_bot(account: str):
    ok, message = start_bot(account)
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/bots/<path:account>/stop")
def api_stop_bot(account: str):
    ok, message = stop_bot(account)
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.delete("/api/bots/<path:account>")
@hunt_api
def api_delete_bot(account: str):
    if plan_for_account(HUNT_PLAN_FILE,account):
        return jsonify(ok=False,message='请先从刷马队伍配置中移除该账号'),409
    stop_bot(account)
    save_accounts([item for item in load_accounts() if item != account])
    return jsonify(ok=True)


@app.get("/api/bots/<path:account>/logs")
def api_logs(account: str):
    path = LOG_DIR / f"{safe_name(account)}.log"
    return jsonify(log=log_tail(path, 80_000))


def stop_all() -> None:
    for account in list(running):
        stop_bot(account)


atexit.register(stop_all)


if __name__ == "__main__":
    marker=BASE_DIR/'server_scope.json'
    if marker.exists() and json.loads(marker.read_text(encoding='utf-8'))['active']!=server_key(SERVER_CONFIG) and scan_bot_processes():
        raise RuntimeError('服务器配置已改变，请先停止原服务器假人再打开控制中心')
    LOG_DIR=bind_profile(BASE_DIR,SERVER_CONFIG)
    recover_running_processes()
    if getattr(sys, "frozen", False) and os.environ.get("TLBB_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8787/")).start()
    app.run(host="127.0.0.1", port=8787, debug=False, threaded=True)
