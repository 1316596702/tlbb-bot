import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import random
import struct
import sys
import time
from pathlib import Path

from protocol_auth import load_adapter, checked_packet
from version_profile import configured_host
from bot_log_retention import HourWindowFileHandler
from leader_shout import load_settings as load_shout_settings, chat_body
from hunt_teams import plan_for_account
from role_name_policy import validate_new_role_name
import managed_leader_state
import hunt_supply
import precise_position


OLD_VERSION = 0x671
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
ROUTE_TABLE_PATH = APP_DIR / "scene_routes.json"
OUTDOOR_MAPS_PATH = APP_DIR / "outdoor_maps.json"
SAFE_MAPS_PATH = APP_DIR / "safe_maps.json"
MANAGED_ACCOUNTS_PATH = APP_DIR / "managed_bot_accounts.json"
PORTABLE_ENTRIES_PATH = APP_DIR / "portable_entries.json"
HUNT_PLAN_PATH = APP_DIR / "hunt_plan.json"
HUNT_SCENES = frozenset((3, 4, 5, 8, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35))
MAX_POSITION_ABS = 10000.0


def make_packet(packet_id: int, state: int, body: bytes = b"") -> bytes:
    return (
        struct.pack("<H", packet_id)
        + len(body).to_bytes(3, "little")
        + bytes((state & 0xFF,))
        + body
    )


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes, bytes]:
    header = await reader.readexactly(6)
    size = int.from_bytes(header[2:5], "little")
    body = await reader.readexactly(size)
    return int.from_bytes(header[:2], "little"), header[5], body, header + body


def decode_name(raw: bytes) -> str:
    raw = raw.split(b"\0", 1)[0]
    for encoding in ("gb18030", "utf-8", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.hex()


def valid_position(x: float, y: float) -> bool:
    return (
        math.isfinite(x)
        and math.isfinite(y)
        and abs(x) <= MAX_POSITION_ABS
        and abs(y) <= MAX_POSITION_ABS
    )


def hunt_position_arrived(position, destination):
    # Pinned LuaFnGetWorldPos uses cvttss2si on both coordinates, so its
    # integer result represents a cell rather than the exact float position.
    return (tuple(map(math.trunc,position)) == tuple(map(math.trunc,destination))
            or math.dist(position,destination) <= 1.0)


def move_destination(body: bytes) -> tuple[float, float] | None:
    if len(body) >= 22 and (len(body) - 14) % 8 == 0:
        position_offset = len(body) - 8
    elif len(body) >= 27 and (len(body) - 19) % 8 == 0:
        position_offset = len(body) - 13
    else:
        return None
    x, y = struct.unpack_from("<ff", body, position_offset)
    return (x, y) if valid_position(x, y) else None


@dataclasses.dataclass
class LoginResult:
    guid64: bytes
    role_name: str
    game_host: str
    game_port: int
    key: bytes
    server_id: int


@dataclasses.dataclass
class SceneMonster:
    obj_id: int
    x: float
    y: float
    raw: bytes
    seen_at: float


class SimpleBot:
    def __init__(
        self,
        account: str,
        auth_profile: str,
        auto_fight: bool = False,
        auto_team: bool = True,
        outdoor_only: bool = True,
        create_role_name: str | None = None,
        create_role_sex: int = 0,
        heal_slot: int | None = None,
        heal_guid: int | None = None,
        heal_world: int = 10,
        login_host: str = "127.0.0.1",
        login_port: int = 6500,
        game_host_override: str | None = None,
        game_port_override: int | None = None,
        auto_equip_weapon: bool = True,
        combat_skill_id: int = 281,
    ) -> None:
        self.account = account
        self.auth_profile = auth_profile
        self.guid64 = b""
        self.role_name = ""
        self.scene_id = 0
        self.scene_resource_id = None
        self.x = 0.0
        self.y = 0.0
        self.client_state = 0x7B
        self.login_host = login_host
        self.login_port = login_port
        self.game_host_override = game_host_override
        self.game_port_override = game_port_override
        self.auto_equip_weapon = auto_equip_weapon
        self.combat_skill_id = combat_skill_id
        self.send_lock = asyncio.Lock()
        self.online_since = 0.0
        self.obj_id = 0
        self.monsters: dict[int, SceneMonster] = {}
        self.initialized_monsters: set[int] = set()
        self.combat_prepared_scene: int | None = None
        # ObjID 0 is a real, attackable local object in scene 166.  Use an
        # out-of-band uint32 value for "no target" instead of conflating it
        # with that object.
        self.last_locked_id = 0xFFFFFFFF
        self.auto_fight = auto_fight
        self.outdoor_only = outdoor_only
        if create_role_name is not None:
            validate_new_role_name(create_role_name)
        self.create_role_name = create_role_name
        self.create_role_sex = create_role_sex
        self.create_role_sent = False
        self.outdoor_maps: set[int] = set()
        self.safe_maps: set[int] = {0, 1, 2, *range(9, 18), 246}
        self.combat_paused_scene: int | None = None
        self.combat_task: asyncio.Task | None = None
        self.equip_task: asyncio.Task | None = None
        self.starter_weapon_slot: int | None = None
        self.healing_gourd_slots: set[int] = set()
        self.healing_gourds: dict[int, tuple[int, int]] = {}
        if heal_slot is not None and heal_guid is not None:
            self.healing_gourd_slots.add(heal_slot)
            self.healing_gourds[heal_slot] = (heal_guid, heal_world)
            logging.info(
                "已从启动参数载入仙丹葫芦 bag_slot=%d guid=%d world=%d",
                heal_slot,
                heal_guid,
                heal_world,
            )
        self.hp_percent: int | None = None
        self.current_hp: int | None = None
        self.max_hp: int | None = None
        self.heal_task: asyncio.Task | None = None
        self.last_heal_at = 0.0
        self.move_handle = 1
        self.first_swing = True
        self.target_started_at = 0.0
        self.attack_attempts = 0
        self.attack_confirmed = False
        self.rejected_targets: dict[int, float] = {}
        self.auto_team = auto_team
        self.in_team = False
        self.leader_obj_id = 0
        self.leader_guid = 0
        self.leader_scene_id: int | None = None
        self.leader_transit_scene_id: int | None = None
        self.script_teleport_pending = False
        self.script_teleport_fallback_sent = False
        self.script_teleport_watchdog_task: asyncio.Task | None = None
        self.leader_x: float | None = None
        self.leader_y: float | None = None
        self.leader_target_id = 0
        self.leader_target_seen_at = 0.0
        self.leader_target_last_broadcast_at = 0.0
        self.unseen_leader_attack_attempts = 0
        self.last_unseen_leader_attack_at = 0.0
        self.last_unseen_leader_move_at = 0.0
        self.team_follow_requested = False
        self.formal_team_follow = False
        self.combat_paused_for_follow = False
        self.scene_change_pending = False
        self.follow_transition_until = 0.0
        self.pending_scene_body: bytes | None = None
        self.leader_missing_since: float | None = None
        self.self_return_pending = False
        self.death_recovery_stage: str | None = None
        self.death_recovery_task: asyncio.Task | None = None
        self.players: dict[int, tuple[float, float]] = {}
        self.scene_routes: dict[str, dict] = {}
        self.portable_entries: dict[int, tuple[int, int, int, int]] = {}
        self.hunt_role: str | None = None
        self.hunt_team_id: str | None = None
        self.hunt_leader_account: str | None = None
        self.hunt_mode: str | None = None
        self.hunt_scenes: frozenset[int] = frozenset()
        self.hunt_leader_role = ""
        self.hunt_leader_guid = 0
        self.hunt_members: list[tuple[str, str, int]] = []
        self.team_invite_region_id: int | None = None
        self.hunt_member_count = 0
        self.hunt_confirmed_member_guids: set[int] = set()
        self.team_member_guids: set[int] = set()
        self.last_team_application: dict[tuple[int, int], float] = {}
        self.hunt_last_invite_at = 0.0
        self.hunt_last_query_at = 0.0
        self.hunt_query_event: asyncio.Event | None = None
        self.hunt_last_response_at = 0.0
        self.hunt_target: SceneMonster | None = None
        self.hunt_waypoint: SceneMonster | None = None
        self.hunt_scene_started_at = 0.0
        self.hunt_expected_scene_id: int | None = None
        self.hunt_last_scene_id: int | None = None
        self.hunt_no_target_since = 0.0
        self.hunt_query_pending = False
        self.hunt_travel_active = False
        self.hunt_mount_wait_until = 0.0
        self.hunt_follow_requested_at = 0.0
        self.hunt_move_steps = 0
        self.hunt_move_event: asyncio.Event | None = None
        self.hunt_position_sequence = 0
        self.hunt_position_pending: tuple | None = None
        self.hunt_position_query_lock = asyncio.Lock()
        self.hunt_current_position = None
        self.hunt_pending_follow_invite_at = None
        self.hunt_safe_gather_active = False
        self.hunt_follow_sequence = 0
        self.hunt_follow_pending: tuple | None = None
        self.hunt_walk_destination: int | None = None
        self.hunt_follow_reinvite_signature: tuple | None = None
        self.hunt_follow_reinvite_at = 0.0
        self.hunt_health_sequence = 0
        self.hunt_health_pending: tuple | None = None
        self.hunt_health_checked_at = 0.0
        self.hunt_health_state = "UNKNOWN"
        self.hunt_interaction_target_id: int | None = None
        self.repair_task: asyncio.Task | None = None
        self.repair_due = True
        self.mount_state_event: asyncio.Event | None = None
        self.is_mounted: bool | None = None
        self.return_task: asyncio.Task | None = None
        self.last_combat_at = 0.0
        if configured_host(APP_DIR, login_host):
            try:
                plan = plan_for_account(HUNT_PLAN_PATH, self.account) or {}
                managed = json.loads(MANAGED_ACCOUNTS_PATH.read_text(encoding="utf-8"))
                plan_scenes = frozenset(int(item) for item in plan.get("scene_ids", []))
                mode = plan.get("mode")
                if (
                    plan.get("format") == 1 and plan.get("enabled") is True
                    and self.account in managed
                    and plan_scenes and plan_scenes.issubset(HUNT_SCENES)
                    and ((mode == "single" and len(plan_scenes) == 1)
                         or (mode == "random" and len(plan_scenes) >= 2))
                ):
                    self.hunt_team_id = plan["id"]
                    self.hunt_leader_account = plan["leader"]
                    self.hunt_mode = mode
                    self.hunt_scenes = plan_scenes
                    if self.account == plan.get("leader"):
                        self.hunt_role = "leader"
                        self.hunt_leader_guid = int(plan["leader_guid"])
                        self.hunt_members = [
                            (item["account"], item["role"], int(item["guid"]))
                            for item in plan["members"]
                        ]
                    elif self.account in {item["account"] for item in plan["members"]}:
                        self.hunt_role = "member"
                        self.hunt_leader_role = plan["leader_role"]
                        self.hunt_leader_guid = int(plan["leader_guid"])
                    if self.hunt_role:
                        logging.info(
                            "已载入刷马模式 role=%s mode=%s maps=%s account=%s",
                            self.hunt_role, self.hunt_mode, sorted(self.hunt_scenes), self.account,
                        )
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                logging.warning("刷马配置未启用：%s", exc)
        try:
            portable_document = json.loads(PORTABLE_ENTRIES_PATH.read_text(encoding="utf-8"))
            if portable_document.get("format") != 1:
                raise ValueError("不支持的入口表格式")
            for scene_id, entry in portable_document["routes"].items():
                category, index = int(entry["category"]), int(entry["index"])
                x, y = int(entry["x"]), int(entry["y"])
                if 1 <= category <= 6 and 1 <= index <= 255 and valid_position(x, y):
                    self.portable_entries[int(scene_id)] = (category, index, x, y)
            logging.info("已加载正常传送入口 scenes=%d", len(self.portable_entries))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logging.warning("正常传送入口表加载失败，暂停对应跨图：%s", exc)
        if OUTDOOR_MAPS_PATH.exists():
            try:
                outdoor_document = json.loads(OUTDOOR_MAPS_PATH.read_text(encoding="utf-8"))
                self.outdoor_maps = {int(scene_id) for scene_id in outdoor_document["scene_ids"]}
                logging.info("已加载野外地图白名单 scenes=%d", len(self.outdoor_maps))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logging.warning("野外地图白名单加载失败，为安全起见暂停自动战斗: %s", exc)
        if SAFE_MAPS_PATH.exists():
            try:
                safe_document = json.loads(SAFE_MAPS_PATH.read_text(encoding="utf-8"))
                self.safe_maps = {int(scene_id) for scene_id in safe_document["scene_ids"]}
                logging.info("已加载非战斗地图黑名单 scenes=%d", len(self.safe_maps))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logging.warning("非战斗地图黑名单加载失败，使用内置城市和门派列表: %s", exc)
        if ROUTE_TABLE_PATH.exists():
            try:
                route_document = json.loads(ROUTE_TABLE_PATH.read_text(encoding="utf-8"))
                self.scene_routes = route_document.get("routes", {})
                logging.info(
                    "已加载传送路线 routes=%d portals=%d",
                    len(self.scene_routes),
                    route_document.get("portal_count", 0),
                )
            except (OSError, ValueError, TypeError) as exc:
                logging.warning("传送路线表加载失败: %s", exc)

    def can_auto_combat(self) -> bool:
        return self.auto_fight and (
            not self.outdoor_only or self.scene_id not in self.safe_maps
        )

    async def send_game_packet(
        self, writer: asyncio.StreamWriter, packet_id: int, body: bytes = b""
    ) -> int:
        """Serialize game packets so their one-byte state never collides."""
        async with self.send_lock:
            state = self.client_state
            packet = make_packet(packet_id, state, body)
            if packet_id in (0x0073, 0x0099, 0x0231):
                logging.info(
                    "战斗发包 packet=0x%04X state=0x%02X size=%d body=%s wire=%s",
                    packet_id,
                    state,
                    len(body),
                    body.hex(),
                    packet.hex(),
                )
            writer.write(packet)
            await writer.drain()
            self.client_state = (state + 1) & 0xFF
            return state

    def remember_monster(self, body: bytes) -> SceneMonster | None:
        # This engine writes GCNewMonster as one packed 0x74-byte structure.
        # Offline disassembly and live packets establish ObjID@0 and X/Y@40/44.
        if len(body) != 0x74:
            return None
        obj_id = struct.unpack_from("<I", body, 0)[0]
        # The treasure-cave client capture attacks local ObjID 0.  Other maps
        # have historically used zero as an empty/invalid object notification.
        if obj_id == 0 and self.scene_id != 166:
            return None
        x, y = struct.unpack_from("<ff", body, 40)
        if not valid_position(x, y):
            return None
        monster = SceneMonster(obj_id, x, y, body, time.monotonic())
        self.monsters[obj_id] = monster
        return monster

    async def lock_nearest(self, writer: asyncio.StreamWriter) -> None:
        now = time.monotonic()
        candidates = [
            monster
            for monster in self.monsters.values()
            if self.rejected_targets.get(monster.obj_id, 0.0) <= now
        ]
        if not candidates:
            return
        target = min(
            candidates,
            key=lambda item: math.hypot(item.x - self.x, item.y - self.y),
        )
        await self.lock_target(writer, target)

    async def lock_target(
        self, writer: asyncio.StreamWriter, target: SceneMonster
    ) -> None:
        now = time.monotonic()
        if target.obj_id == self.last_locked_id:
            return
        distance = math.hypot(target.x - self.x, target.y - self.y)
        # The normal treasure-cave capture sends only CGSetLockObj (0x0073)
        # before CGUseAbility (0x0099).  0x009A/0x0293 are server-to-client
        # object notifications; echoing them back produces GCErrorAttack=-3
        # (0x0297 fdffffff) and prevents combat from starting.
        await self.send_game_packet(writer, 0x0073, struct.pack("<I", target.obj_id))
        self.last_locked_id = target.obj_id
        self.first_swing = True
        self.target_started_at = now
        self.attack_attempts = 0
        self.attack_confirmed = False
        logging.info(
            "攻击准备并锁定目标 obj=%d pos=(%.2f, %.2f) distance=%.2f",
            target.obj_id,
            target.x,
            target.y,
            distance,
        )

    async def accept_team_invite(
        self, writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        if len(body) < 37:
            logging.warning("组队邀请包过短 size=%d", len(body))
            return
        inviter_guid = struct.unpack_from("<I", body, 0)[0]
        inviter_name = decode_name(body[4:34])
        if self.hunt_role == "leader":
            logging.info("刷马队长忽略外部组队邀请 inviter=%s", inviter_name)
            return
        if self.hunt_role == "member" and inviter_name != self.hunt_leader_role:
            logging.info("刷马队员忽略非指定队长邀请 inviter=%s", inviter_name)
            return
        # Customized CGTeamRetInvite is 41 bytes. The server handler consumes
        # Return and inviter GUID; the remaining compatibility fields are zero.
        reply = struct.pack("<BII8I", 1, inviter_guid, 0, *([0] * 8))
        await self.send_game_packet(writer, 0x007F, reply)
        logging.info("已自动同意组队邀请 inviter=%s guid=%d", inviter_name, inviter_guid)

    def managed_leader_identity(self) -> dict | None:
        if (self.hunt_role not in ("leader", "member") or not getattr(self,"hunt_team_id",None)
                or not getattr(self,"hunt_leader_account",None) or not self.hunt_leader_guid):
            return None
        return managed_leader_state.identity(self.login_host, self.login_port,
            self.hunt_team_id, self.hunt_leader_account, self.hunt_leader_guid)

    def refresh_managed_leader_scene(self) -> dict | None:
        if self.hunt_role != "member":
            return None
        expected = self.managed_leader_identity()
        if expected is None:
            return None
        state = managed_leader_state.read(APP_DIR, expected, time.time())
        if state is not None and state["ready"]:
            destination = state["scene"]
            if destination != self.leader_scene_id:
                logging.info("受管队长实时地图已核对 leader=%s old=%s current=%d self_scene=%d",
                             self.hunt_leader_account, self.leader_scene_id, destination, self.scene_id)
                self.leader_scene_id = destination
                self.leader_transit_scene_id = None
                if destination != self.scene_id:
                    self.leader_obj_id = 0
                    self.leader_x = self.leader_y = None
            if destination == self.scene_id and state["obj_id"] not in (0, 0xFFFFFFFF):
                self.leader_obj_id = state["obj_id"]
        return state

    def recent_hunt_position(self) -> tuple | None:
        cache = getattr(self, "hunt_current_position", None)
        if cache is None or cache[:2] != (self.scene_id, self.obj_id):
            return None
        if not 0 <= time.time() - cache[3] <= 5.:
            return None
        return cache[2]

    async def publish_managed_leader_state(self, writer: asyncio.StreamWriter) -> None:
        expected = self.managed_leader_identity()
        if self.hunt_role != "leader" or expected is None:
            return
        last_error = 0.
        previous_state = None
        last_position_query = -100.
        while True:
            if (self.scene_id in self.safe_maps and self.in_team
                    and self.leader_guid == self.hunt_leader_guid
                    and not self.scene_change_pending and not self.self_return_pending
                    and self.death_recovery_stage is None
                    and self.current_hp is not None and self.current_hp > 0
                    and time.monotonic() - last_position_query >= 2.):
                last_position_query = time.monotonic()
                await self.query_hunt_position(writer)
            ready = (self.in_team and self.leader_guid == self.hunt_leader_guid
                     and self.obj_id not in (0, 0xFFFFFFFF)
                     and not self.scene_change_pending and not self.self_return_pending
                     and self.hunt_expected_scene_id is None
                     and self.death_recovery_stage is None
                     and self.current_hp is not None and self.current_hp > 0)
            current = (self.scene_id, bool(ready), self.in_team, self.leader_guid,
                       self.scene_change_pending, self.hunt_expected_scene_id)
            if current != previous_state:
                logging.info("受管队长地图状态 scene=%d ready=%s team=%s leader_guid=%d changing=%s expected=%s", *current)
                previous_state = current
            try:
                position = self.recent_hunt_position()
                stamp = self.hunt_current_position[3] if position is not None else None
                managed_leader_state.publish(APP_DIR, expected, self.scene_id, bool(ready),
                                             self.obj_id, time.time(), position, stamp)
            except (OSError, ValueError) as error:
                if time.monotonic() - last_error >= 30.:
                    logging.warning("受管队长地图状态保存失败：%s", error)
                    last_error = time.monotonic()
            await asyncio.sleep(1.)

    async def accept_team_follow(self, writer: asyncio.StreamWriter) -> None:
        state = self.refresh_managed_leader_scene()
        if state is not None and not state["ready"]:
            logging.info("受管队长正在过图或尚未就绪，等待再接受跟随")
            return
        if (self.hunt_role == "member" and self.leader_scene_id is not None
                and self.leader_scene_id != self.scene_id):
            logging.info("刷马队员与队长分图，先沿普通出口追赶，再接受跟随")
            return
        if (self.hunt_role == "member" and self.managed_leader_identity() is not None
                and self.scene_id in self.safe_maps):
            leader_position = managed_leader_state.recent_position(state, time.time())
            own_position = self.recent_hunt_position()
            if (leader_position is None or own_position is None
                    or math.dist(own_position, leader_position) > 6.):
                self.hunt_pending_follow_invite_at = time.monotonic()
                logging.info("安全地图收到跟随邀请，先靠近本队队长再同意")
                return
            self.hunt_pending_follow_invite_at = None
        # CGAskTeamFollow (0x01F6) can only be sent by the leader. A member
        # receives GCAskTeamFollow and answers with CGReturnTeamFollow.
        self.team_follow_requested = True
        await self.send_game_packet(writer, 0x00C4, b"\x01")
        logging.info("已自动同意队长的组队跟随请求")

    async def accept_team_application(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        if self.hunt_role != "leader" or not self.in_team or self.leader_guid != self.hunt_leader_guid:
            return
        # Same-scene and cross-scene samples have 125- and 33-byte fixed parts.
        # The two variable names follow the applicant/requested-member GUIDs;
        # the server reply key is the last four bytes in both variants.
        if len(body) < 33:
            logging.warning("拒绝解析过短入队申请 size=%d", len(body))
            return
        source_size, target_size = body[8], body[9]
        if (
            not (1 <= source_size <= 35 and 1 <= target_size <= 35)
            or len(body) - source_size - target_size not in (33, 125)
        ):
            logging.warning("拒绝解析异常入队申请 size=%d names=%d/%d", len(body), source_size, target_size)
            return
        applicant_guid, requested_guid = struct.unpack_from("<II", body, 0)
        key = struct.unpack_from("<I", body, len(body) - 4)[0]
        eligible_targets = self.team_member_guids | {
            self.hunt_leader_guid, *(item[2] for item in self.hunt_members)
        }
        if requested_guid not in eligible_targets or applicant_guid in (0, 0xFFFFFFFF, self.hunt_leader_guid):
            return
        now = time.monotonic()
        identity = (applicant_guid, key)
        if now - self.last_team_application.get(identity, 0.0) < 2.0:
            return
        self.last_team_application[identity] = now
        count = max(self.hunt_member_count, len(self.team_member_guids))
        approve = count < 6
        reply = struct.pack("<BII8I", int(approve), applicant_guid, key, *([0] * 8))
        await self.send_game_packet(writer, 0x022F, reply)
        logging.info(
            "刷马队长%s入队申请 guid=%d key=%d known_members=%d",
            "已同意" if approve else "满队拒绝", applicant_guid, key, count,
        )

    async def request_follow_scene_change(
        self, writer: asyncio.StreamWriter, target_scene: int
    ) -> None:
        # Captured customized CGAskChangeScene layout is source H, target H,
        # portal/config token I, route token I. Route 6 -> 2 is the Wuliang
        # Mountain to Dali portal captured from the normal client.
        route = self.scene_routes.get(f"{self.scene_id}:{target_scene}")
        if self.scene_change_pending:
            return
        if route is None:
            # Portable teleports and many courier-NPC destinations are absent
            # from ScenePosDefineEx. The team-follow handler still accepts the
            # same compatibility fields used by ordinary configured routes.
            compat_e = 1695
            client_mem_bits = 0x453F1F00
            logging.info(
                "路线表无记录，使用通用跟随迁移 source=%d target=%d",
                self.scene_id,
                target_scene,
            )
        else:
            compat_e = int(route.get("compat_e", 1695))
            client_mem_bits = int(route.get("client_mem_bits", 0x453F1F00))
        body = struct.pack(
            "<HHII", self.scene_id, target_scene, compat_e, client_mem_bits
        )
        await self.send_game_packet(writer, 0x006B, body)
        self.scene_change_pending = True
        logging.info("已申请跟随跨图 source=%d target=%d", self.scene_id, target_scene)

    def known_arrival(self, target_scene: int) -> tuple[int, int] | None:
        arrivals = set()
        for route in self.scene_routes.values():
            if route.get("target_scene") != target_scene:
                continue
            for portal in route.get("portals", []):
                x, y = portal.get("target_x"), portal.get("target_y")
                if (
                    isinstance(x, (int, float))
                    and isinstance(y, (int, float))
                    and math.isfinite(x)
                    and math.isfinite(y)
                    and float(x).is_integer()
                    and float(y).is_integer()
                ):
                    arrivals.add((int(x), int(y)))
        return next(iter(arrivals)) if len(arrivals) == 1 else None

    async def request_script_teleport(
        self, writer: asyncio.StreamWriter, target_scene: int
    ) -> None:
        if self.hunt_role in ("leader", "member"):
            logging.info("刷马角色等待普通组队跟随过图 target=%d", target_scene)
            return
        if not configured_host(APP_DIR, self.login_host):
            logging.warning("非测试服，未请求脚本传送 target=%d", target_scene)
            return
        try:
            managed = json.loads(MANAGED_ACCOUNTS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            managed = []
        if not isinstance(managed, list) or self.account not in managed:
            logging.warning("账号不在受管名单，未请求脚本传送 target=%d", target_scene)
            return
        if target_scene == 2:
            await self.follow_leader_to_dali(writer)
            return
        portable_entry = self.portable_entries.get(self.leader_transit_scene_id)
        if portable_entry and self.scene_id <= 2:
            self.script_teleport_pending = True
            self.script_teleport_fallback_sent = False
            self.script_teleport_watchdog_task = asyncio.create_task(
                self.watch_script_teleport(target_scene)
            )
            await self.request_portable_teleport(writer)
            return
        script_scene = target_scene
        if portable_entry:
            script_scene = self.leader_transit_scene_id
            arrival = portable_entry[2:4]
        elif self.leader_transit_scene_id == target_scene:
            arrival = self.known_arrival(target_scene)
        else:
            arrival = None
        if arrival is None:
            logging.warning("目标地图缺少唯一入口坐标，未请求脚本传送 target=%d", target_scene)
            return
        function_name = b"Jiaren"
        payload = (
            struct.pack("<IB", 9998496, len(function_name))
            + function_name
            + struct.pack("<BIIII", 4, 1, script_scene, *arrival)
        )
        await self.send_game_packet(writer, 0x0042, payload)
        self.script_teleport_pending = True
        self.script_teleport_fallback_sent = False
        self.script_teleport_watchdog_task = asyncio.create_task(
            self.watch_script_teleport(target_scene)
        )
        logging.info(
            "已请求服务端脚本传送 entry=%d target=%d arrival=(%d,%d)",
            script_scene,
            target_scene,
            *arrival,
        )

    async def follow_leader_to_dali(self, writer: asyncio.StreamWriter) -> None:
        if self.scene_id == 2 or self.self_return_pending:
            return
        self.self_return_pending = True
        # This entry can run in the packet reader; keep response waits in a
        # separate task so the reader can dispatch the mount-state reply.
        self.return_task = asyncio.create_task(self.return_to_dali_unmounted(writer))

    async def query_mount_state(self, writer: asyncio.StreamWriter) -> bool:
        event = asyncio.Event()
        self.mount_state_event = event
        self.is_mounted = None
        name = b"Jiaren"
        body = struct.pack("<IB", 9998496, len(name)) + name + struct.pack("<BI", 1, 5)
        try:
            await self.send_game_packet(writer, 0x0042, body)
            await asyncio.wait_for(event.wait(), 6.0)
        except asyncio.TimeoutError:
            logging.warning("骑乘状态查询超时，暂停本次操作")
            return False
        finally:
            if self.mount_state_event is event:
                self.mount_state_event = None
        return self.is_mounted is not None

    async def return_to_dali_unmounted(self, writer: asyncio.StreamWriter) -> None:
        sent = False
        try:
            if not await self.query_mount_state(writer):
                return
            self.hunt_travel_active = self.is_mounted is True
            await self.set_hunt_travel(writer, False)
            if self.formal_team_follow or self.death_recovery_stage is not None:
                return
            if not await self.query_mount_state(writer) or self.is_mounted:
                logging.warning("回城前尚未确认下坐骑，暂不使用回城技能")
                return
            body = struct.pack("<fIIIIff", -1.0, 22, 0xFFFFFFFF,
                               self.obj_id, 0xFFFFFFFF, -1.0, -1.0)
            self.script_teleport_watchdog_task = asyncio.create_task(self.watch_script_teleport(2))
            await self.send_game_packet(writer, 0x0099, body)
            sent = True
            logging.info("已确认非骑乘状态，使用返回大理技能 skill=22")
        finally:
            if not sent:
                self.self_return_pending = False
            self.return_task = None

    async def request_portable_teleport(
        self, writer: asyncio.StreamWriter
    ) -> bool:
        portable_entry = self.portable_entries.get(self.leader_transit_scene_id)
        if (
            self.script_teleport_fallback_sent
            or self.scene_id > 2
            or portable_entry is None
        ):
            return False
        category, index = portable_entry[:2]
        function_name = b"Teleport"
        payload = (
            struct.pack("<IB", 990001, len(function_name))
            + function_name
            + struct.pack("<BII", 2, category, index)
        )
        await self.send_game_packet(writer, 0x0042, payload)
        self.script_teleport_fallback_sent = True
        logging.info(
            "已使用正常传送入口 Teleport(%d,%d) target=%d",
            category, index, self.leader_scene_id,
        )
        return True

    async def watch_script_teleport(
        self, target_scene: int, delay: float = 20.0
    ) -> None:
        try:
            await asyncio.sleep(delay)
            if self.scene_id != target_scene:
                self.script_teleport_pending = False
                self.self_return_pending = False
                self.scene_change_pending = False
                self.pending_scene_body = None
                if self.hunt_expected_scene_id == target_scene:
                    self.hunt_expected_scene_id = None
                logging.warning(
                    "服务端传送等待超时 target=%d，留在当前地图 map=%d",
                    target_scene,
                    self.scene_id,
                )
        finally:
            if self.script_teleport_watchdog_task is asyncio.current_task():
                self.script_teleport_watchdog_task = None

    async def handle_team_scene_update(
        self, writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        # Team-member arrival carries GUID, ObjID, source scene and destination
        # scene at fixed offsets on this server. A departed member uses a
        # different, shorter form and has no destination to follow.
        if (
            len(body) < 68 or body[0] != 8 or not self.in_team
            or not 0 <= body[15] <= 35 or len(body) != 68 + body[15]
        ):
            return
        member_guid = struct.unpack_from("<I", body, 1)[0]
        member_obj_id = struct.unpack_from("<I", body, 7)[0]
        source_scene, target_scene = struct.unpack_from("<HH", body, 11)
        if self.hunt_role == "leader":
            signature = self.hunt_follow_reinvite_signature
            if (signature is not None and signature[0] == self.scene_id
                    and member_guid in signature[1]
                    and member_guid in self.team_member_guids
                    and target_scene == self.scene_id
                    and member_obj_id not in (0, 0xFFFFFFFF)):
                self.hunt_follow_reinvite_signature = None
                logging.info("晚入队成员已到达当前地图，允许补发跟随邀请 guid=%d scene=%d",
                             member_guid, target_scene)
        if self.leader_guid:
            if member_guid != self.leader_guid:
                return
        elif self.leader_obj_id not in (0, 0xFFFFFFFF) and member_obj_id == self.leader_obj_id:
            self.leader_guid = member_guid
        else:
            return
        if target_scene == 0xFFFF:
            return
        self.leader_scene_id = target_scene
        self.leader_transit_scene_id = source_scene
        if member_obj_id not in (0, 0xFFFFFFFF):
            self.leader_obj_id = member_obj_id
        if target_scene != self.scene_id:
            self.leader_x = None
            self.leader_y = None
        logging.info(
            "队长位置更新 guid=%d obj=%d source=%d target=%d",
            member_guid,
            member_obj_id,
            source_scene,
            target_scene,
        )
        if (
            target_scene == self.scene_id
            or self.scene_change_pending
            or self.self_return_pending
            or self.formal_team_follow
            or self.death_recovery_stage is not None
            or self.script_teleport_pending
        ):
            return
        self.mark_leader_missing(writer, scene_departure=True)
        await self.request_script_teleport(writer, target_scene)

    async def enter_changed_scene(
        self, writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        # Customized GCNotifyChangeScene begins source H, target H, X/Y float,
        # direction B and flag B. CGEnterScene is target H, enter-type B, X/Y.
        if len(body) < 14:
            logging.warning("切图通知包过短 size=%d", len(body))
            return
        source_scene, target_scene = struct.unpack_from("<HH", body, 0)
        target_x, target_y = struct.unpack_from("<ff", body, 4)
        enter_type = 1
        if self.self_return_pending:
            logging.info(
                "收到自身回城通知 source=%d target=%d，准备申请场景迁移",
                source_scene,
                target_scene,
            )
            self.scene_change_pending = True
            self.self_return_pending = False
            # The normal client does not enter immediately. After the return
            # notification it first requests migration with CGReqChangeScene,
            # then confirms entry only after the server accepts that request.
            self.pending_scene_body = body
            request = struct.pack(
                "<HHII", source_scene, target_scene, 1595, 0x453F1F00
            )
            await self.send_game_packet(writer, 0x006B, request)
            logging.info(
                "已按正常客户端回城序列申请迁移 source=%d target=%d",
                source_scene,
                target_scene,
            )
            return
        elif self.death_recovery_stage == "to_hell" and target_scene == 77:
            self.scene_change_pending = True
            self.pending_scene_body = body
            self.death_recovery_stage = "entering_hell"
            request = struct.pack(
                "<HHII", source_scene, target_scene, 1622, 0x453F1F00
            )
            await self.send_game_packet(writer, 0x006B, request)
            logging.info("出窍后已申请进入地府 source=%d target=%d", source_scene, target_scene)
            return
        elif self.death_recovery_stage == "to_luoyang" and target_scene == 0:
            self.scene_change_pending = True
            self.pending_scene_body = body
            self.death_recovery_stage = "entering_luoyang"
            request = struct.pack(
                "<HHII", source_scene, target_scene, 1605, 0x453F1F00
            )
            await self.send_game_packet(writer, 0x006B, request)
            logging.info("孟婆回城已申请进入洛阳 source=%d target=%d", source_scene, target_scene)
            return
        elif self.script_teleport_pending and target_scene in (
            self.leader_scene_id, self.leader_transit_scene_id,
            self.hunt_expected_scene_id,
        ):
            self.script_teleport_pending = False
            self.pending_scene_body = body
            await self.request_follow_scene_change(writer, target_scene)
            logging.info("服务端已发来脚本传送通知 target=%d", target_scene)
            return
        elif not self.scene_change_pending:
            logging.info(
                "收到场景切换通知 source=%d target=%d，先申请迁移",
                source_scene,
                target_scene,
            )
            self.pending_scene_body = body
            await self.request_follow_scene_change(writer, target_scene)
            return
        self.scene_change_pending = True
        # Team-follow notification is broadcast before the world/scene manager
        # finishes moving the follower. The normal client confirms roughly one
        # second later; replying immediately hits CGEnterSceneHandler FALSE.
        await asyncio.sleep(1.0)
        reply = struct.pack("<HBff", target_scene, enter_type, target_x, target_y)
        await self.send_game_packet(writer, 0x0015, reply)
        self.scene_id = target_scene
        self.x, self.y = target_x, target_y
        self.monsters.clear()
        self.initialized_monsters.clear()
        self.combat_prepared_scene = None
        self.last_locked_id = 0xFFFFFFFF
        self.leader_target_id = 0
        self.unseen_leader_attack_attempts = 0
        self.last_unseen_leader_attack_at = 0.0
        self.last_unseen_leader_move_at = 0.0
        logging.info(
            "已发送进入目标地图确认 source=%d target=%d type=%d pos=(%.2f, %.2f)",
            source_scene,
            target_scene,
            enter_type,
            target_x,
            target_y,
        )

    async def move_towards(
        self, writer: asyncio.StreamWriter, target: SceneMonster, stop_distance: float = 2.5,
        full_path: bool = False,
    ) -> bool:
        move_scene = self.scene_id
        self.hunt_move_waiting_for_team = False
        self.hunt_move_recovered = False
        managed_member = getattr(self,"hunt_role",None) == "member"
        if managed_member:
            if self.formal_team_follow or self.team_follow_requested:
                return False
            full_path = True
        if full_path:
            position = await self.query_hunt_position(writer)
            if position is None or self.scene_id != move_scene or self.scene_change_pending:
                logging.warning("刷马移动未取得当前真实坐标，暂停当前路线")
                return False
            position = await self.resolve_hunt_start(writer,position)
            if position is None:
                return False
            self.x, self.y = position
            if (getattr(self,"hunt_role",None)=="leader" and not self.hunt_combat_active()
                    and not getattr(self,"hunt_unstuck_active",False)
                    and await self.try_hunt_unstuck(writer,position)):
                self.hunt_move_recovered = True
                return False
        if not valid_position(self.x, self.y) or not valid_position(target.x, target.y):
            logging.warning(
                "拒绝异常移动坐标 from=(%.2f,%.2f) to=(%.2f,%.2f)",
                self.x, self.y, target.x, target.y,
            )
            return False
        dx, dy = target.x - self.x, target.y - self.y
        distance = math.hypot(dx, dy)
        if distance <= stop_distance + 0.25:
            return True
        travel_distance = distance - stop_distance
        ratio = (travel_distance if full_path else min(travel_distance, 8.0)) / distance
        target_x = self.x + dx * ratio
        target_y = self.y + dy * ratio
        nodes = [(target_x,target_y)]
        if full_path:
            from hunt_navigation import plan_hunt_path
            try:
                nodes = await asyncio.to_thread(plan_hunt_path,APP_DIR,self.scene_id,
                                                (self.x,self.y),(target.x,target.y),stop_distance)
            except (OSError,ValueError,KeyError) as error:
                logging.warning("刷马寻路失败，重新选点：%s", error)
                return False
            if (self.scene_id != move_scene or self.scene_change_pending
                    or getattr(self,"death_recovery_stage",None) is not None
                    or getattr(self,"current_hp",None) == 0):
                return False
            if managed_member and (self.formal_team_follow or self.team_follow_requested):
                return False
            if (getattr(self,"hunt_walk_destination",None) is not None
                    or (getattr(self,"hunt_role",None) == "leader"
                        and not self.hunt_combat_active())):
                current_team = await self.query_hunt_follow(writer)
                if (self.scene_id != move_scene or self.scene_change_pending
                        or not self.hunt_follow_ready(current_team)):
                    self.hunt_move_waiting_for_team = True
                    logging.info("发起刷马移动前队伍状态已变化，重新核对跟随名单")
                    return False
            if not nodes:
                return True
            target_x, target_y = nodes[-1]
        # Captured engine layout: HandleID, ObjID, current position, node count,
        # one path control byte, then one target position.
        body = struct.pack(
            "<IIffBB",
            self.move_handle,
            self.obj_id,
            self.x,
            self.y,
            len(nodes),
            0,
        ) + b"".join(struct.pack("<ff", x,y) for x,y in nodes)
        arrival = asyncio.Event() if full_path else None
        if arrival is not None:
            logging.info("刷马寻路节点 count=%d nodes=%s", len(nodes), nodes)
            self.hunt_move_event = arrival
            self.hunt_follow_dirty = False
        self.hunt_precise_sample=None
        await self.send_game_packet(writer, 0x0231, body)
        self.move_handle = (self.move_handle + 100) & 0xFFFFFFFF
        logging.info(
            "向目标移动 obj=%d from=(%.2f, %.2f) to=(%.2f, %.2f)",
            target.obj_id,
            self.x,
            self.y,
            target_x,
            target_y,
        )
        if arrival is not None:
            logging.info("刷马连续移动，核对服务器真实坐标 destination=(%.2f,%.2f)", target_x, target_y)
            deadline = time.monotonic() + 60.0
            last_progress = time.monotonic()
            progress_position = (self.x, self.y)
            missing_positions = 0
            try:
                while time.monotonic() < deadline:
                    await asyncio.sleep(1.0)
                    if (self.scene_id != move_scene or self.scene_change_pending
                            or getattr(self,"death_recovery_stage",None) is not None
                            or getattr(self,"current_hp",None) == 0):
                        return False
                    if managed_member and (self.formal_team_follow or self.team_follow_requested):
                        return False
                    position = await self.query_hunt_position(writer)
                    if position is None:
                        missing_positions += 1
                        if missing_positions >= 3:
                            logging.warning("刷马真实坐标连续查询失败，暂停当前路线")
                            return False
                        continue
                    missing_positions = 0
                    self.x, self.y = position
                    if (getattr(self,"hunt_role",None)=="leader" and not self.hunt_combat_active()
                            and getattr(self,"hunt_follow_dirty",False)):
                        self.hunt_follow_dirty=False
                        status=await self.query_hunt_follow(writer)
                        if self.scene_id!=move_scene or self.scene_change_pending:return False
                        if not self.hunt_follow_ready(status):
                            from hunt_navigation import load_path_map
                            try:
                                def hold_nodes():
                                    mesh=load_path_map(APP_DIR,move_scene)
                                    anchor=mesh.routing_position(position)
                                    triangle=mesh.locate(anchor)[0]
                                    center=mesh.centers[triangle]
                                    if math.dist(anchor,center)<.25:
                                        center=max(mesh.points[triangle],key=lambda p:math.dist(anchor,p))
                                    distance=math.dist(anchor,center)
                                    point=tuple(anchor[k]+(center[k]-anchor[k])*min(.5,distance)/distance for k in (0,1))
                                    return [anchor,point] if anchor!=position else [point]
                                pause_nodes=await asyncio.to_thread(hold_nodes)
                            except (OSError,ValueError,KeyError) as error:
                                logging.warning("成员跟随变化，未取得安全停步点：%s",error)
                                return False
                            if self.scene_id!=move_scene or self.scene_change_pending:return False
                            hold=struct.pack("<IIffBB",self.move_handle,self.obj_id,
                                             *position,len(pause_nodes),0)
                            hold+=b"".join(struct.pack("<ff",*p) for p in pause_nodes)
                            await self.send_game_packet(writer,0x0231,hold)
                            self.move_handle=(self.move_handle+100)&0xFFFFFFFF
                            self.hunt_move_waiting_for_team=True
                            logging.warning("赶路中队员尚未就绪，已暂停移动，等待重新跟随 scene=%d",move_scene)
                            return False
                    remaining = math.hypot(target_x - self.x, target_y - self.y)
                    if hunt_position_arrived(position,(target_x,target_y)):
                        logging.info("刷马真实坐标确认到达 pos=(%.2f,%.2f)", self.x, self.y)
                        return True
                    if math.hypot(self.x - progress_position[0], self.y - progress_position[1]) > 0.5:
                        progress_position = position
                        last_progress = time.monotonic()
                    elif time.monotonic() - last_progress >= 8.0:
                        logging.warning("刷马移动8秒无进展，重新选点 actual=(%.2f,%.2f) destination=(%.2f,%.2f)",
                                        self.x, self.y, target_x, target_y)
                        return False
                logging.warning("刷马移动60秒未收到到达反馈，暂停当前路线")
                return False
            finally:
                if self.hunt_move_event is arrival:
                    self.hunt_move_event = None
        # This server persists the requested movement but does not echo the
        # operator's own GCCharMove. After the two-second travel allowance,
        # use the accepted destination as our local position estimate.
        self.x, self.y = target_x, target_y
        return True

    async def use_normal_attack(
        self, writer: asyncio.StreamWriter, target: SceneMonster
    ) -> None:
        if (self.hunt_role == "member"
                and (self.formal_team_follow or self.team_follow_requested)):
            return
        self.last_combat_at = time.monotonic()
        # Start directly with the first skill of the role's first xinfa. The
        # previous -1 basic-attack primer caused a visible normal swing before
        # the school skill and is unnecessary on this engine.
        skill_id = self.combat_skill_id
        body = struct.pack(
            "<fIIIIff",
            -1.0,
            skill_id,
            0xFFFFFFFF,
            self.obj_id,
            target.obj_id,
            -1.0,
            -1.0,
        )
        await self.send_game_packet(writer, 0x0099, body)
        self.first_swing = False
        self.attack_attempts += 1
        logging.info(
            "门派技能攻击 obj=%d skill=%d",
            target.obj_id,
            skill_id,
        )

    def handle_death_notice(self, writer: asyncio.StreamWriter, packet_id: int, body: bytes) -> None:
        if packet_id == 0x0047:
            # Current client GCCharSkill_Missed: this notification is not death.
            if len(body) != 17:
                logging.warning("技能未命中通知长度异常 size=%d，未修改死亡状态", len(body))
            else:
                logging.debug("技能未命中通知 body=%s", body.hex())
            return
        if packet_id != 0x0158:
            return
        # Current client GCDie contains two U32 fields. Their semantic roles
        # are not proven by its no-op handler, so do not infer self death from
        # either field. Keep the established own-HP=0 recovery condition.
        if len(body) != 8:
            logging.warning("死亡相关通知长度异常 size=%d，未修改死亡状态", len(body))
            return
        logging.info("死亡相关通知 packet=0x0158 self=%d hp=%s body=%s",
                     self.obj_id, self.current_hp, body.hex())
        if self.obj_id not in (0, 0xFFFFFFFF) and self.current_hp == 0:
            self.begin_death_recovery(writer, "self_hp_zero_with_death_notice")

    def begin_death_recovery(
        self, writer: asyncio.StreamWriter, source: str
    ) -> None:
        if self.death_recovery_stage is not None:
            return
        self.death_recovery_stage = "dead"
        self.repair_due = True
        self.last_locked_id = 0xFFFFFFFF
        self.leader_target_id = 0
        self.hunt_travel_active = False
        self.hunt_follow_requested_at = 0.0
        self.hunt_target = None
        self.hunt_interaction_target_id = None
        if self.hunt_move_event is not None:
            self.hunt_move_event.set()
        self.hunt_waypoint = None
        self.hunt_expected_scene_id = None
        self.hunt_query_pending = False
        if self.heal_task:
            self.heal_task.cancel()
        if self.death_recovery_task is None:
            self.death_recovery_task = asyncio.create_task(
                self.recover_from_death(writer)
            )
        logging.info("检测到角色死亡 source=%s，3秒后自动出窍", source)

    async def recover_from_death(self, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(3.0)
            if self.death_recovery_stage != "dead":
                return
            await self.send_game_packet(writer, 0x0274, struct.pack("<I", 0))
            self.death_recovery_stage = "to_hell"
            logging.info("角色死亡，已自动点击出窍")
        finally:
            if self.death_recovery_task is asyncio.current_task():
                self.death_recovery_task = None

    def begin_hell_recovery(self, writer: asyncio.StreamWriter) -> None:
        if self.death_recovery_stage in {
            "in_hell", "to_luoyang", "entering_luoyang"
        }:
            return
        self.death_recovery_stage = "in_hell"
        if self.death_recovery_task is not None:
            self.death_recovery_task.cancel()
        self.death_recovery_task = asyncio.create_task(
            self.visit_mengpo_and_return_luoyang(writer)
        )
        logging.info("已进入地府，准备前往孟婆")

    async def visit_mengpo_and_return_luoyang(
        self, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.sleep(2.0)
            # Captured route from the hell spawn (160, 38) to Mengpo. Split
            # the long walk into short accepted path requests.
            destination_x, destination_y = 14.82, 28.36
            move_attempts = 0
            while self.scene_id == 77 and move_attempts < 8:
                distance = math.hypot(destination_x - self.x, destination_y - self.y)
                if distance <= 3.0:
                    break
                move_attempts += 1
                step = min(18.0, distance)
                ratio = step / distance
                waypoint = SceneMonster(
                    0,
                    self.x + (destination_x - self.x) * ratio,
                    self.y + (destination_y - self.y) * ratio,
                    b"",
                    time.monotonic(),
                )
                await self.move_towards(writer, waypoint, stop_distance=0.0)
                # move_towards updates the optimistic local position as soon
                # as it sends the request. Wait for the authoritative server
                # movement packet before deciding that Mengpo was reached.
                await asyncio.sleep(2.2)
            if self.scene_id != 77:
                return
            distance = math.hypot(destination_x - self.x, destination_y - self.y)
            if distance > 4.0:
                logging.warning(
                    "前往孟婆失败，未进入交互距离 pos=(%.2f, %.2f) distance=%.2f",
                    self.x,
                    self.y,
                    distance,
                )
                return
            # normal-death-revive capture: CGSetLockObj targets Mengpo's
            # stable scene object id 320 (0x00000140), not zero.
            mengpo_obj_id = 320
            await self.send_game_packet(
                writer, 0x0073, struct.pack("<I", mengpo_obj_id)
            )
            await self.send_game_packet(
                writer, 0x0305, struct.pack("<II", 0, 0xFFFFFFFF)
            )
            await asyncio.sleep(0.5)
            self.death_recovery_stage = "to_luoyang"
            await self.send_game_packet(
                writer,
                0x00E0,
                bytes.fromhex("c92c01000000000000000000ffffffff"),
            )
            logging.info("已与孟婆交互并选择回到洛阳")
        finally:
            if self.death_recovery_task is asyncio.current_task():
                self.death_recovery_task = None

    def mark_leader_missing(
        self, writer: asyncio.StreamWriter, scene_departure: bool = False
    ) -> None:
        if not self.in_team or not self.leader_obj_id:
            return
        if scene_departure or (
            self.leader_scene_id is not None
            and self.leader_scene_id != self.scene_id
        ):
            self.leader_x = None
            self.leader_y = None
        self.players.pop(self.leader_obj_id, None)
        if self.leader_missing_since is None:
            self.leader_missing_since = time.monotonic()
            logging.info("队长离开当前视野，等待队伍地图更新")

    def mark_leader_present(self) -> None:
        self.leader_missing_since = None

    async def send_hunt_invites(self, writer: asyncio.StreamWriter) -> None:
        now = time.monotonic()
        if (
            self.obj_id in (0, 0xFFFFFFFF)
            or self.team_invite_region_id is None
            or now - self.hunt_last_invite_at < 12.0
        ):
            return
        self.hunt_last_invite_at = now
        for _, role, target_guid in self.hunt_members:
            if target_guid in self.hunt_confirmed_member_guids:
                continue
            encoded = role.encode("gb18030")
            if not 1 <= len(encoded) <= 30:
                logging.warning("刷马队员角色名长度异常，未邀请 role=%s", role)
                continue
            # Captured on this client: source ObjID U32, destination GUID U32,
            # name length U8, GB18030 name, invitation region U16.
            body = (
                struct.pack("<IIB", self.obj_id, target_guid, len(encoded))
                + encoded + struct.pack("<H", self.team_invite_region_id)
            )
            await self.send_game_packet(writer, 0x0040, body)
            logging.info(
                "刷马队长已发送组队邀请 role=%s guid=%d region=%d",
                role, target_guid, self.team_invite_region_id,
            )

    async def request_hunt_scene(self, writer: asyncio.StreamWriter) -> None:
        other_scenes = self.hunt_scenes - {self.scene_id}
        # Avoid the previous destination when possible, without reintroducing
        # the current scene when only one other selected map remains.
        choices = sorted((other_scenes - {self.hunt_last_scene_id}) or other_scenes or self.hunt_scenes)
        target_scene = random.choice(choices)
        self.hunt_last_scene_id = target_scene
        await self.walk_to_hunt_scene(writer,target_scene)

    async def query_hunt_follow(self, writer: asyncio.StreamWriter) -> dict | None:
        self.hunt_follow_sequence = (self.hunt_follow_sequence + 1) & 0x7FFFFFFF
        sequence,scene,obj = self.hunt_follow_sequence,self.scene_id,self.obj_id
        response = asyncio.get_running_loop().create_future()
        pending = (sequence,scene,obj,response)
        self.hunt_follow_pending = pending
        try:
            name = b"Jiaren"
            body = (struct.pack("<IB",9998496,len(name))+name
                    +struct.pack("<BIIII",4,7,sequence,scene,obj))
            await self.send_game_packet(writer,0x0042,body)
            return await asyncio.wait_for(response,timeout=2.5)
        except asyncio.TimeoutError:
            return None
        finally:
            if self.hunt_follow_pending is pending:
                self.hunt_follow_pending = None
            if not response.done():
                response.cancel()

    def hunt_follow_ready(self, status: dict | None) -> bool:
        from hunt_world_walk import followed_team_ready
        required = self.team_member_guids | {self.hunt_leader_guid} | {m[2] for m in self.hunt_members}
        if self.hunt_pickup_excludes_human():
            required = self.hunt_pickup_guids
            return followed_team_ready(status,required,len(required))
        return followed_team_ready(status,required,max(self.hunt_member_count,len(required)))

    async def reinvite_hunt_follow(self, writer: asyncio.StreamWriter, status: dict) -> None:
        from hunt_world_walk import follow_restart_safe
        if self.hunt_combat_active():
            return
        required = self.team_member_guids | {self.hunt_leader_guid} | {m[2] for m in self.hunt_members}
        if self.hunt_pickup_excludes_human():
            required = self.hunt_pickup_guids
        missing = tuple(sorted(required-status["guids"]))
        signature = (self.scene_id,missing)
        if (not missing or status["missing"]
                or time.monotonic()-self.hunt_follow_reinvite_at < 15.):
            return
        scene = self.scene_id
        position = await self.query_hunt_position(writer)
        if position is None or self.scene_id != scene or self.scene_change_pending:
            return
        # Ending follow inside an exit would let its timer transfer the captain
        # alone; only refresh the request while standing clear of scene areas.
        if not follow_restart_safe(APP_DIR,scene,position):
            return
        self.x,self.y = position
        self.hunt_follow_reinvite_at = time.monotonic()
        await self.send_game_packet(writer,0x0064)
        deadline = time.monotonic()+5.
        while time.monotonic()<deadline:
            current = await self.query_hunt_follow(writer)
            if self.scene_id != scene or self.scene_change_pending:
                return
            if current is not None and current["follow"] == 0:
                self.formal_team_follow = False
                break
            await asyncio.sleep(.1)
        else:
            logging.warning("补发跟随邀请前未确认旧跟随结束，继续等待")
            return
        await asyncio.sleep(.3)
        if self.scene_id != scene or self.scene_change_pending:
            return
        await self.send_game_packet(writer,0x01F6)
        self.hunt_follow_requested_at = time.monotonic()
        self.hunt_follow_reinvite_signature = signature
        logging.info("已保持坐骑重新发起组队跟随，请新入队玩家同意 guids=%s scene=%d；未就绪时15秒后重试",missing,scene)

    async def walk_to_hunt_scene(self, writer: asyncio.StreamWriter, destination: int, pickup: bool = False) -> bool:
        from hunt_world_walk import world_route
        self.hunt_walk_destination = destination
        self.hunt_target = None
        self.hunt_waypoint = None
        last_wait_log, last_leg, failures, transition_started = 0., None, 0, 0.
        checked_entry = None
        logging.info("刷马队长开始沿普通出口前往地图 source=%d target=%d",self.scene_id,destination)
        try:
            while self.scene_id != destination or self.scene_change_pending:
                if pickup:
                    latest=getattr(self,"hunt_member_locations",{}).get(self.hunt_pickup_target_guid)
                    if (self.hunt_pickup_target_guid not in self.team_member_guids
                            or (latest is not None and latest[0] == self.scene_id)):
                        if self.hunt_pickup_target_guid in self.team_member_guids:
                            self.hunt_pickup_wait_guid = self.hunt_pickup_target_guid
                        self.hunt_follow_dirty = True
                        logging.info("接人途中真人已到当前地图或离队，结束接人赶路并恢复完整跟随检查 scene=%d",self.scene_id)
                        return False
                    if latest is None or latest[0]!=destination:
                        logging.info("接人途中真人位置变化，暂停并重新核对")
                        return False
                    if not self.hunt_human_stationary(self.hunt_pickup_target_guid):
                        logging.info("接人途中真人开始移动或坐标已过期，暂停接人并重新计时")
                        return False
                if self.death_recovery_stage is not None or self.current_hp == 0:
                    await asyncio.sleep(1.)
                    continue
                if self.scene_change_pending or self.self_return_pending:
                    if not transition_started:
                        transition_started = time.monotonic()
                    if time.monotonic()-transition_started > 30.:
                        logging.warning("普通出口进场确认超时，暂停本次赶路")
                        self.scene_change_pending = False
                        self.self_return_pending = False
                        self.pending_scene_body = None
                        await asyncio.sleep(10.)
                        return False
                    await asyncio.sleep(.5)
                    continue
                transition_started = 0.
                if (not self.in_team or self.leader_guid not in (0,self.hunt_leader_guid)):
                    logging.info("赶路期间队伍或队长变化，等待重新组队")
                    return False
                entry=(self.scene_id,self.obj_id,getattr(self,"hunt_scene_started_at",None))
                if (not pickup and getattr(self,"hunt_mode",None)=="random" and self.scene_id in self.hunt_scenes
                        and entry != checked_entry):
                    found=await self.check_hunt_on_route(writer)
                    if found is None:
                        await asyncio.sleep(.5)
                        continue
                    if found:
                        logging.info("沿途已勾选地图发现马贼，暂停赶路优先刷当前地图 scene=%d obj=%d",
                                     self.scene_id,self.hunt_target.obj_id)
                        return False
                    checked_entry=entry
                source = self.scene_id
                await self.set_hunt_travel(writer,True)
                if not self.hunt_travel_active:
                    self.hunt_progress_position = None
                    await asyncio.sleep(1.)
                    continue
                status = await self.query_hunt_follow(writer)
                if self.scene_id != source:
                    continue
                if not self.hunt_follow_ready(status):
                    self.hunt_progress_position = None
                    if not pickup and await self.pick_up_real_member(writer,status):
                        return False
                    if status is not None and status["follow"] == 1:
                        await self.reinvite_hunt_follow(writer,status)
                    if time.monotonic()-last_wait_log >= 10.:
                        logging.info("跨图赶路等待队员全部接受跟随并到达当前地图 scene=%d",self.scene_id)
                        last_wait_log = time.monotonic()
                    await asyncio.sleep(1.)
                    continue
                self.formal_team_follow = True
                position = await self.query_hunt_position(writer)
                if position is None or self.scene_id != source:
                    await asyncio.sleep(1.)
                    continue
                self.x,self.y = position
                if await self.try_hunt_unstuck(writer,position):
                    continue
                try:
                    route = await asyncio.to_thread(world_route,APP_DIR,source,position,destination,status["level"])
                except (OSError,ValueError,KeyError) as error:
                    logging.warning("普通出口路线不可用：%s",error)
                    if not pickup:
                        await self.recover_unmapped_hunt_scene(writer,source)
                    await asyncio.sleep(10.)
                    return False
                if self.scene_id != source or self.scene_change_pending:
                    continue
                if not self.hunt_follow_ready(status):
                    continue  # Membership can change while position/path requests are pending.
                if not route:
                    continue
                leg = route[0]
                identity = (source,leg["target_scene"],leg["script_id"])
                if identity != last_leg:
                    logging.info("跨图赶路路线 maps=%s；当前出口 source=%d target=%d pos=(%.2f,%.2f)",
                                 [source]+[e["target_scene"] for e in route],source,leg["target_scene"],
                                 leg["source_x"],leg["source_y"])
                    last_leg = identity
                portal = SceneMonster(0,leg["source_x"],leg["source_y"],b"",time.monotonic())
                arrived = await self.move_towards(writer,portal,stop_distance=0.,full_path=True)
                if self.scene_id != source:
                    failures = 0
                    continue
                if not arrived:
                    if self.hunt_move_waiting_for_team:
                        continue
                    failures += 1
                    if failures >= 3:
                        logging.warning("前往普通出口连续移动失败，暂停本次赶路 scene=%d",source)
                        await asyncio.sleep(10.)
                        return False
                    continue
                failures = 0
                left,top,right,bottom = leg["bounds"]
                if not (left <= self.x <= right and top <= self.y <= bottom):
                    continue  # A long route may take more than one four-node packet.
                callback = leg.get("confirmation")
                if callback:
                    status = await self.query_hunt_follow(writer)
                    if self.scene_id != source:
                        continue
                    if not self.hunt_follow_ready(status):
                        await asyncio.sleep(1.)
                        continue
                    name = callback.encode("ascii")
                    body = struct.pack("<IB",leg["script_id"],len(name))+name+bytes([0])
                    await self.send_game_packet(writer,0x0042,body)
                    logging.info("已在普通出口确认通行 script=%d function=%s",leg["script_id"],callback)
                deadline = time.monotonic()+25.
                while self.scene_id == source and time.monotonic()<deadline:
                    if self.death_recovery_stage is not None:
                        break
                    await asyncio.sleep(.5)
                if self.scene_id == source and self.death_recovery_stage is None:
                    logging.warning("到达普通出口但未能过图，请检查队员通行条件 source=%d target=%d",source,leg["target_scene"])
                    await asyncio.sleep(10.)
                    return False
            self.hunt_scene_started_at = time.monotonic()
            self.hunt_no_target_since = 0.
            self.hunt_target = None
            self.hunt_waypoint = None
            self.hunt_query_pending = False
            self.hunt_last_query_at = 0.
            self.hunt_interaction_target_id = None
            logging.info("刷马队长沿普通出口到达目的地图 scene=%d",destination)
            return True
        finally:
            self.hunt_walk_destination = None

    async def recover_unmapped_hunt_scene(self, writer, scene):
        if (self.scene_id!=scene or self.scene_change_pending or self.self_return_pending
                or self.death_recovery_stage is not None or self.current_hp==0):return
        try:
            manifest=json.loads((APP_DIR/'hunt_navigation/manifest.json').read_text(encoding='utf8'))
        except (OSError,ValueError):return
        if str(scene) in manifest.get('maps',{}):return
        logging.warning("误入未配置刷马步行路线的地图，使用正常下坐骑回城流程重新集合 scene=%d",scene)
        await self.follow_leader_to_dali(writer)

    async def query_hunt_target(self, writer: asyncio.StreamWriter) -> None:
        context=self.scene_id,self.obj_id
        await self.maintain_hunt_supply(writer)
        if (context!=(self.scene_id,self.obj_id) or self.scene_change_pending
                or self.self_return_pending):return
        now = time.monotonic()
        exclusions = [obj for obj,until in sorted(self.rejected_targets.items(),key=lambda item:item[1],reverse=True)
                      if until > now][:3]
        exclusions += [0]*(3-len(exclusions))
        name = b"Jiaren"
        body = (struct.pack("<IB", 9998496, len(name)) + name
                + struct.pack("<BIIII", 4, 3, *exclusions))
        self.hunt_last_query_at = time.monotonic()
        self.hunt_query_pending = True
        await self.send_game_packet(writer, 0x0042, body)
        logging.info("刷马队长查询当前地图马贼 scene=%d", self.scene_id)

    async def query_hunt_supply(self, writer, spawn=False):
        self.hunt_supply_sequence=(getattr(self,"hunt_supply_sequence",0)+1)&0x7fffffff
        sequence,scene,obj=self.hunt_supply_sequence,self.scene_id,self.obj_id
        future=asyncio.get_running_loop().create_future()
        self.hunt_supply_pending=(sequence,scene,obj,future)
        try:
            name=b"Jiaren"
            body=struct.pack("<IB",9998496,len(name))+name
            body+=struct.pack("<BIIIII",5,14,sequence,scene,obj,int(spawn))
            await self.send_game_packet(writer,0x0042,body)
            return await asyncio.wait_for(future,2.5)
        except asyncio.TimeoutError:return None
        finally:
            self.hunt_supply_pending=None
            if not future.done():future.cancel()

    async def maintain_hunt_supply(self, writer):
        if (getattr(self,"hunt_role",None)!="leader" or self.scene_id not in self.hunt_scenes
                or not configured_host(APP_DIR, self.login_host)
                or not self.in_team or self.leader_guid!=self.hunt_leader_guid
                or self.scene_change_pending or self.self_return_pending
                or self.death_recovery_stage is not None or self.current_hp==0
                or self.hunt_combat_active()):return
        config=APP_DIR/'hunt_supply.json'
        if not config.exists():return
        try:
            if json.loads(config.read_text(encoding='utf8')).get('enabled') is not True:return
            scene,obj=self.scene_id,self.obj_id
            result=await self.query_hunt_supply(writer)
            if (result is None or result['status']!='OBSERVED' or self.scene_id!=scene
                    or self.obj_id!=obj or self.scene_change_pending):return
            token=await asyncio.to_thread(hunt_supply.observe,APP_DIR,self.login_host,self.login_port,
                self.hunt_team_id,self.hunt_leader_guid,self.hunt_scenes,scene,result['count'],time.time())
            if token is None:return
            spawned=False
            try:
                if (self.scene_id==scene and self.obj_id==obj and not self.scene_change_pending
                        and not self.self_return_pending and self.current_hp!=0
                        and self.death_recovery_stage is None and not self.hunt_combat_active()):
                    result=None
                    result=await self.query_hunt_supply(writer,spawn=True)
                    spawned=result is not None and result['status']=='SPAWNED'
                    logging.info("刷马地图30分钟未查询到存活马贼，申请补一波 scene=%d result=%s",
                                 scene,result)
            finally:
                # An unanswered mutation could have succeeded. Keep the full
                # cooldown rather than reissuing an uncertain request.
                uncertain=result is None
                await asyncio.to_thread(hunt_supply.complete,APP_DIR,self.login_host,self.login_port,
                                         token,spawned or uncertain,time.time(),
                                         result['count'] if result is not None else None)
        except (OSError,ValueError,KeyError,TypeError,TimeoutError) as exc:
            if time.monotonic()-getattr(self,"hunt_supply_error_at",0.)>=30.:
                self.hunt_supply_error_at=time.monotonic()
                logging.warning("马贼补刷检查失败，未继续补刷：%s",exc)

    async def check_hunt_on_route(self, writer: asyncio.StreamWriter) -> bool | None:
        scene=self.scene_id
        event=asyncio.Event();self.hunt_query_event=event
        try:
            await self.query_hunt_target(writer)
            await asyncio.wait_for(event.wait(),2.5)
            if self.scene_id != scene or self.scene_change_pending:
                return None
            return self.hunt_target is not None
        except asyncio.TimeoutError:
            logging.warning("沿途马贼查询未获确认，暂缓出发 scene=%d",scene)
            return None
        finally:
            if self.hunt_query_event is event:
                self.hunt_query_event=None

    def record_hunt_member_location(self, body: bytes, position_packet: bool = False) -> None:
        if self.hunt_role!="leader":return
        if position_packet:
            if len(body)!=21:return
            guid,high,scene,x,y=struct.unpack_from("<IIIff",body)
            if high!=0 or scene>=0xFFFF or not valid_position(x,y):return
            position=(x,y)
        else:
            if (len(body)<68 or body[0] not in (0,8) or body[15]>35
                    or len(body)!=68+body[15]):return
            guid=struct.unpack_from("<I",body,1)[0]
            scene=struct.unpack_from("<H",body,13)[0]
            if scene==0xFFFF:return
            previous=getattr(self,"hunt_member_locations",{}).get(guid)
            position=previous[1] if previous is not None and previous[0]==scene else None
        if guid not in self.team_member_guids:return
        if not hasattr(self,"hunt_member_locations"):self.hunt_member_locations={}
        self.hunt_member_locations[guid]=(scene,position)
        if not hasattr(self,"hunt_member_stationary"):self.hunt_member_stationary={}
        previous=self.hunt_member_stationary.get(guid)
        now=time.monotonic()
        if position_packet:
            if (previous is None or previous[0]!=scene or math.dist(previous[1],position)>=2.
                    or not 0. <= now-previous[3] <= 30.):
                self.hunt_member_stationary[guid]=(scene,position,now,now)
            else:
                self.hunt_member_stationary[guid]=(scene,previous[1],previous[2],now)
        elif previous is not None and previous[0]!=scene:
            self.hunt_member_stationary.pop(guid,None)
        if guid == getattr(self,"hunt_pickup_target_guid",None):
            self.hunt_follow_dirty=True

    def hunt_pickup_excludes_human(self) -> bool:
        if getattr(self,"hunt_pickup_guids",None) is None:
            return False
        guid=getattr(self,"hunt_pickup_target_guid",None)
        # Same-map approach in finish_real_pickup intentionally keeps the bots
        # together while walking towards the waiting human.
        if guid is None:
            return True
        latest=getattr(self,"hunt_member_locations",{}).get(guid)
        return (guid in self.team_member_guids and latest is not None
                and latest[0] != self.scene_id and self.hunt_human_stationary(guid))

    def hunt_human_stationary(self, guid: int) -> bool:
        sample=getattr(self,"hunt_member_stationary",{}).get(guid)
        latest=getattr(self,"hunt_member_locations",{}).get(guid)
        now=time.monotonic()
        return (sample is not None and latest is not None and sample[0]==latest[0]
                and now-sample[2]>=120. and 0. <= now-sample[3] <= 30.)

    async def pick_up_real_member(self, writer: asyncio.StreamWriter, status: dict | None) -> bool:
        if (getattr(self,"hunt_role",None)!="leader" or self.hunt_combat_active() or status is None
                or getattr(self,"hunt_pickup_guids",None) is not None):return False
        managed={self.hunt_leader_guid}|{m[2] for m in self.hunt_members}
        humans=self.team_member_guids-managed-status["guids"]
        locations=getattr(self,"hunt_member_locations",{})
        now=time.monotonic()
        candidates=[(guid,locations[guid]) for guid in sorted(humans) if guid in locations
                    and self.hunt_human_stationary(guid)
                    and 0<=locations[guid][0]<=35 and locations[guid][0]!=self.scene_id]
        if not candidates:
            self.hunt_pickup_signature=None
            return False
        guid,(destination,_) = candidates[0]
        signature=(guid,destination)
        if signature!=getattr(self,"hunt_pickup_signature",None):
            self.hunt_pickup_signature=signature;self.hunt_pickup_since=now
        if now<getattr(self,"hunt_pickup_retry_at",0.):return False
        self.hunt_pickup_guids=managed;self.hunt_pickup_target_guid=guid
        old_destination=self.hunt_walk_destination
        try:
            logging.warning("真人队员在异地图原地停留已满2分钟，带假人队员沿普通出口返回接人 guid=%d source=%d target=%d",
                            guid,self.scene_id,destination)
            arrived=await self.walk_to_hunt_scene(writer,destination,pickup=True)
        finally:
            self.hunt_pickup_guids=None;self.hunt_pickup_target_guid=None
            self.hunt_walk_destination=old_destination
        if not arrived:
            self.hunt_pickup_retry_at=time.monotonic()+15.
            return True
        self.hunt_pickup_wait_guid=guid
        self.hunt_target=None;self.hunt_waypoint=None
        self.hunt_query_pending=False;self.hunt_last_query_at=0.
        self.hunt_no_target_since=0.;self.hunt_pickup_signature=None
        logging.info("已返回真人所在地图，等待真人同意跟随后恢复刷马 scene=%d guid=%d",self.scene_id,guid)
        return True

    async def finish_real_pickup(self, writer: asyncio.StreamWriter) -> bool:
        guid=getattr(self,"hunt_pickup_wait_guid",None)
        if guid is None:return False
        if guid not in self.team_member_guids:
            self.hunt_pickup_wait_guid=None
            return False
        scene,obj=self.scene_id,self.obj_id
        if await self.ensure_hunt_travel_ready(writer):
            self.hunt_pickup_wait_guid=None
            logging.info("真人已重新进入跟随，恢复刷马 guid=%d",guid)
            return False
        if (self.scene_id!=scene or self.obj_id!=obj or self.scene_change_pending
                or self.self_return_pending or self.death_recovery_stage is not None
                or self.current_hp==0):return True
        latest=getattr(self,"hunt_member_locations",{}).get(guid)
        if latest is None or latest[0]!=self.scene_id or latest[1] is None:return True
        position=await self.query_hunt_position(writer)
        if position is None or math.dist(position,latest[1])<=5.:return True
        managed={self.hunt_leader_guid}|{m[2] for m in self.hunt_members}
        status=await self.query_hunt_follow(writer)
        current=getattr(self,"hunt_member_locations",{}).get(guid)
        if (self.scene_id!=scene or self.obj_id!=obj or self.scene_change_pending
                or self.self_return_pending or self.death_recovery_stage is not None
                or self.current_hp==0 or current!=latest):return True
        from hunt_world_walk import followed_team_ready
        if not self.hunt_travel_active or not followed_team_ready(status,managed,len(managed)):
            return True
        self.hunt_pickup_guids=managed
        try:
            logging.info("已回到真人地图，带假人队员靠近真人 guid=%d pos=%s",guid,latest[1])
            await self.move_towards(writer,SceneMonster(0,*latest[1],b"",time.monotonic()),
                                    stop_distance=4.,full_path=True)
        finally:self.hunt_pickup_guids=None
        return True

    async def gather_hunt_member_safe(self, writer: asyncio.StreamWriter) -> bool:
        if (self.hunt_role != "member" or self.scene_id not in self.safe_maps
                or not self.in_team or self.leader_guid != self.hunt_leader_guid):
            return False
        if (self.scene_change_pending or self.self_return_pending
                or self.death_recovery_stage is not None or self.current_hp == 0):
            return True
        scene = self.scene_id
        state = self.refresh_managed_leader_scene()
        if state is None or not state["ready"]:
            return True
        if state["scene"] != scene:
            return False
        leader_position = managed_leader_state.recent_position(state, time.time())
        if leader_position is None:
            return True
        status = await self.query_hunt_follow(writer)
        if self.scene_id != scene or self.scene_change_pending or status is None:
            return True
        if status["follow"] == 1:
            self.formal_team_follow = True
            return False
        # Native state confirms the old follow is inactive before independent movement.
        self.formal_team_follow = False
        self.team_follow_requested = False
        position = await self.query_hunt_position(writer)
        state = self.refresh_managed_leader_scene()
        leader_position = managed_leader_state.recent_position(state, time.time())
        if (position is None or state is None or not state["ready"] or state["scene"] != scene
                or leader_position is None or self.scene_id != scene or self.scene_change_pending
                or self.self_return_pending or self.death_recovery_stage is not None
                or self.current_hp == 0 or not self.in_team or self.leader_guid != self.hunt_leader_guid):
            return True
        self.x, self.y = position
        distance = math.dist(position, leader_position)
        if distance <= 6.:
            pending = getattr(self, "hunt_pending_follow_invite_at", None)
            if pending is not None:
                self.hunt_pending_follow_invite_at = None
                if 0 <= time.monotonic() - pending <= 20.:
                    self.team_follow_requested = True
                    await self.send_game_packet(writer, 0x00C4, b"\x01")
                    logging.info("安全地图已靠近队长，回复组队跟随邀请 distance=%.2f", distance)
            return True
        if time.monotonic() < getattr(self, "hunt_safe_gather_retry_at", 0.):
            return True
        self.hunt_safe_gather_retry_at = time.monotonic() + 2.
        logging.info("安全地图队员主动靠近队长 scene=%d from=%s leader=%s distance=%.2f",
                     scene, position, leader_position, distance)
        self.hunt_safe_gather_active = True
        try:
            await self.move_towards(writer, SceneMonster(0, *leader_position, b"", time.monotonic()),
                                    stop_distance=4., full_path=True)
        finally:
            self.hunt_safe_gather_active = False
        return True

    async def recover_hunt_member_step(self, writer: asyncio.StreamWriter) -> bool:
        if (self.hunt_role != "member" or not self.in_team
                or self.leader_guid != self.hunt_leader_guid):
            self.hunt_member_split_signature=None
            return False
        state=self.refresh_managed_leader_scene()
        if state is not None and not state["ready"]:
            return True
        destination=self.leader_scene_id
        if destination is None or not 0<=destination<=35 or destination==self.scene_id:
            self.hunt_member_split_signature=None
            self.hunt_member_portal_wait_until=0.
            return False
        source=self.scene_id;signature=(source,destination,self.leader_guid)
        now=time.monotonic()
        if signature != getattr(self,"hunt_member_split_signature",None):
            self.hunt_member_split_signature=signature
            self.hunt_member_split_since=now
            self.hunt_member_portal_wait_until=0.
            logging.warning("刷马队员与队长分图，等待正常过图后尝试追赶 source=%d target=%d",source,destination)
        if (self.scene_change_pending and not self.self_return_pending
                and now-self.hunt_member_split_since>=30.):
            # A valid reply from the old scene proves that the role is still
            # there. Do not clear a timed-out transition on elapsed time alone.
            position=await self.query_hunt_position(writer)
            if (position is not None and self.scene_id==source
                    and self.leader_scene_id==destination and self.in_team
                    and self.leader_guid==self.hunt_leader_guid):
                self.scene_change_pending=False;self.pending_scene_body=None
                self.script_teleport_pending=False
                logging.warning("队员过图等待超时，已确认仍在原地图，改走普通出口 source=%d target=%d",source,destination)
        if (now-self.hunt_member_split_since<10. or self.scene_change_pending
                or self.self_return_pending or self.death_recovery_stage is not None
                or self.current_hp==0 or now<getattr(self,"hunt_member_recovery_retry_at",0.)
                or now<getattr(self,"hunt_member_portal_wait_until",0.)):
            return True
        def context_valid():
            latest=self.refresh_managed_leader_scene()
            if latest is not None and not latest["ready"]:return False
            return (self.in_team and self.leader_guid==self.hunt_leader_guid
                    and self.leader_scene_id==destination and self.scene_id==source
                    and not self.scene_change_pending and not self.self_return_pending
                    and self.death_recovery_stage is None and self.current_hp!=0)
        status=await self.query_hunt_follow(writer)
        if status is None or not context_valid():
            return True
        if status["follow"]==1:
            await self.send_game_packet(writer,0x0064)
            deadline=time.monotonic()+5.
            while time.monotonic()<deadline:
                status=await self.query_hunt_follow(writer)
                if not context_valid():return True
                if status is not None and status["follow"]==0:break
                await asyncio.sleep(.1)
            else:
                self.hunt_member_recovery_retry_at=time.monotonic()+10.
                logging.warning("队员旧跟随尚未解除，暂不独立追赶")
                return True
        self.formal_team_follow=False;self.team_follow_requested=False
        position=await self.query_hunt_position(writer)
        if position is None or not context_valid():return True
        self.x,self.y=position
        from hunt_world_walk import world_route
        try:
            route=await asyncio.to_thread(world_route,APP_DIR,source,position,destination,status["level"])
        except (OSError,ValueError,KeyError) as error:
            self.hunt_member_recovery_retry_at=time.monotonic()+10.
            logging.warning("队员普通出口追赶路线不可用：%s",error)
            await self.recover_unmapped_hunt_scene(writer,source)
            return True
        if not context_valid() or not route:return True
        leg=route[0]
        logging.info("刷马队员沿普通出口追赶队长 source=%d next=%d target=%d",
                     source,leg["target_scene"],destination)
        portal=SceneMonster(0,leg["source_x"],leg["source_y"],b"",now)
        arrived=await self.move_towards(writer,portal,stop_distance=0.,full_path=True)
        if not context_valid():return True
        if not arrived:
            self.hunt_member_recovery_retry_at=time.monotonic()+5.
            return True
        left,top,right,bottom=leg["bounds"]
        if not left<=self.x<=right or not top<=self.y<=bottom:return True
        # Confirm the native member-follow flag again immediately before an exit.
        status=await self.query_hunt_follow(writer)
        if status is None or status["follow"]!=0 or not context_valid():return True
        callback=leg.get("confirmation")
        if callback:
            name=callback.encode("ascii")
            body=struct.pack("<IB",leg["script_id"],len(name))+name+bytes([0])
            await self.send_game_packet(writer,0x0042,body)
        self.hunt_member_portal_wait_until=time.monotonic()+25.
        logging.info("队员已到达普通出口，等待正常过图 source=%d next=%d",source,leg["target_scene"])
        return True

    async def query_hunt_health(self, writer: asyncio.StreamWriter, target_id: int) -> dict | None:
        self.hunt_health_sequence = (self.hunt_health_sequence+1)&0x7FFFFFFF
        sequence,scene,obj = self.hunt_health_sequence,self.scene_id,self.obj_id
        response = asyncio.get_running_loop().create_future()
        pending = (sequence,scene,obj,target_id,response)
        self.hunt_health_pending = pending
        try:
            name = b"Jiaren"
            body = (struct.pack("<IB",9998496,len(name))+name
                    +struct.pack("<BIIII",4,8,target_id,sequence,scene))
            await self.send_game_packet(writer,0x0042,body)
            return await asyncio.wait_for(response,timeout=2.5)
        except asyncio.TimeoutError:
            return None
        finally:
            if self.hunt_health_pending is pending:
                self.hunt_health_pending = None
            if not response.done():
                response.cancel()

    def finish_hunt_target(self, target_id: int, reason: str) -> None:
        if self.hunt_target is None or self.hunt_target.obj_id != target_id:
            return
        logging.info("刷马目标结束 obj=%d reason=%s，立即寻找下一只",target_id,reason)
        self.rejected_targets[target_id] = time.monotonic()+8.
        self.hunt_target = None
        self.hunt_waypoint = None
        self.hunt_query_pending = False
        self.hunt_last_query_at = 0.
        self.hunt_interaction_target_id = None
        self.hunt_health_checked_at = 0.
        self.hunt_health_state = "UNKNOWN"
        self.attack_confirmed = False
        self.attack_attempts = 0
        self.first_swing = True
        self.last_locked_id = 0xFFFFFFFF

    async def resolve_hunt_start(self, writer, reported):
        from hunt_navigation import load_path_map
        scene,obj=self.scene_id,self.obj_id
        try:
            mesh=await asyncio.to_thread(load_path_map,APP_DIR,scene)
        except (OSError,ValueError,KeyError) as exc:
            logging.warning("寻路起点核对缺少有效地图数据：%s",exc)
            return None
        try:
            await asyncio.to_thread(mesh.routing_position,reported)
            return reported
        except ValueError:
            pass
        raw=getattr(self,'guid64',b'')
        if (getattr(self,'hunt_role',None) not in ('leader','member')
                or getattr(self,'login_host',None) not in precise_position.TEST_HOSTS
                or len(raw)!=8 or any(v!=math.trunc(v) for v in reported)):
            return None
        guid=struct.unpack('<Q',raw)[0]
        if not 0<guid<0xffffffff:return None
        cached=getattr(self,'hunt_precise_sample',None)
        if (cached is not None and cached[:4]==(scene,obj,guid,reported)
                and 0.<=time.monotonic()-cached[5]<=5.):
            return cached[4]
        if time.monotonic()-getattr(self,'hunt_precise_attempt_at',0.)<20.:
            return None
        self.hunt_precise_attempt_at=time.monotonic()
        try:
            exact=await asyncio.to_thread(precise_position.query,APP_DIR,self.login_host,scene,obj,guid)
            fresh=await self.query_hunt_position(writer)
            if (self.scene_id!=scene or self.obj_id!=obj or self.scene_change_pending
                    or self.self_return_pending or self.death_recovery_stage is not None
                    or self.current_hp==0 or fresh!=reported
                    or tuple(math.trunc(v) for v in exact)!=tuple(reported)):
                return None
            # Verify the observed float in the real mesh. Never turn a target
            # destination or the nearest triangle into an alleged position.
            await asyncio.to_thread(mesh.locate,exact)
            logging.info("障碍边缘整数坐标无法寻路，已核对服务端真实小数起点 scene=%d reported=%s exact=%s",
                         scene,reported,exact)
            self.hunt_current_position=(scene,obj,exact,time.time())
            self.hunt_precise_sample=(scene,obj,guid,reported,exact,time.monotonic())
            return exact
        except Exception as exc:
            logging.warning("障碍边缘精确起点核对失败，未放宽寻路校验：%s",exc)
            return None

    async def query_hunt_position(self, writer: asyncio.StreamWriter) -> tuple[float, float] | None:
        if not hasattr(self, "hunt_position_query_lock"):
            self.hunt_position_query_lock = asyncio.Lock()
        async with self.hunt_position_query_lock:
            scene, obj = self.scene_id, self.obj_id
            position = await self.query_hunt_position_once(writer)
            if position is not None and (scene, obj) == (self.scene_id, self.obj_id):
                self.hunt_current_position = (scene, obj, position, time.time())
                return position
            return None

    async def query_hunt_position_once(self, writer: asyncio.StreamWriter) -> tuple[float, float] | None:
        self.hunt_position_sequence = (self.hunt_position_sequence + 1) & 0x7FFFFFFF
        sequence, scene, obj = self.hunt_position_sequence, self.scene_id, self.obj_id
        response = asyncio.get_running_loop().create_future()
        pending = (sequence, scene, obj, response)
        self.hunt_position_pending = pending
        try:
            name = b"Jiaren"
            body = (struct.pack("<IB", 9998496, len(name)) + name
                    + struct.pack("<BIIII", 4, 6, sequence, scene, obj))
            await self.send_game_packet(writer, 0x0042, body)
            return await asyncio.wait_for(response, timeout=2.5)
        except asyncio.TimeoutError:
            return None
        finally:
            if self.hunt_position_pending is pending:
                self.hunt_position_pending = None
            if not response.done():
                response.cancel()

    async def automatic_repair(self, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5.0)
        last_request = 0.0
        while True:
            await asyncio.sleep(1.0)
            if not configured_host(APP_DIR, self.login_host):
                return
            try:
                settings = json.loads((APP_DIR / "repair_settings.json").read_text(encoding="utf-8"))
                managed = json.loads(MANAGED_ACCOUNTS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if self.account not in managed:
                return
            enabled = settings.get("enabled") is True
            if not enabled and settings.get("audit") is not True:
                continue
            now = time.monotonic()
            interval = max(60, min(3600, int(settings.get("interval_seconds", 300))))
            if not self.repair_due and now-last_request < interval:
                continue
            if self.death_recovery_stage is not None or self.current_hp == 0 or self.scene_change_pending:
                continue
            if enabled and (self.hunt_move_event is not None or self.formal_team_follow
                            or now < self.hunt_mount_wait_until or now-self.last_combat_at < 10.0
                            or self.attack_confirmed or self.heal_task is not None):
                continue
            name = b"Jiaren"
            body = (struct.pack("<IB", 9998496, len(name)) + name
                    + struct.pack("<BIIII", 4, 4, 1 if enabled else 0, 0, 0))
            await self.send_game_packet(writer, 0x0042, body)
            last_request = now
            self.repair_due = False
            logging.info("自动修理请求 mode=%s，等待服务端耐久反馈", "repair" if enabled else "audit")

    def handle_maintenance_message(self, message: str) -> bool:
        if message.startswith('JBS1:'):
            try:
                parts=message.split(':')
                if len(parts)!=6:raise ValueError('field count')
                obj,scene,seq,count=map(int,parts[1:5]);status=parts[5]
                pending=getattr(self,'hunt_supply_pending',None)
                if (pending is None or (seq,scene,obj)!=pending[:3]
                        or scene!=self.scene_id or obj!=self.obj_id):return True
                if not 0<=count<=100000 or status not in ('OBSERVED','SPAWNED','ALIVE','COOLDOWN','FAILED','DENIED'):
                    raise ValueError('invalid supply result')
                if not pending[3].done():pending[3].set_result(dict(count=count,status=status))
            except ValueError:logging.warning('忽略异常马贼补刷反馈')
            return True
        if message.startswith('JCH1:'):
            try:
                fields=message.split(':')
                if len(fields)!=7:raise ValueError('field count')
                obj,scene,seq,team,guild,sect=map(int,fields[1:])
                pending=getattr(self,'shout_context_pending',None)
                if (pending is None or (seq,scene,obj)!=pending[:3]
                        or self.scene_id!=scene or self.obj_id!=obj):return True
                if any(v< -1 or v>0xFFFFFFFF for v in (team,guild,sect)):raise ValueError('invalid membership')
                if not pending[3].done():pending[3].set_result(dict(team=team,guild=guild,sect=sect))
            except ValueError:logging.warning('喊话身份反馈无效')
            return True
        if message.startswith("JHT1:"):
            try:
                fields = message.split(":")
                if len(fields) != 8:
                    raise ValueError("field count")
                obj,scene,sequence,target = map(int,fields[1:5])
                pending = self.hunt_health_pending
                if (pending is None or (sequence,scene,obj,target) != pending[:4]
                        or scene != self.scene_id or obj != self.obj_id):
                    return True
                if fields[5] in ("MISSING","UNKNOWN"):
                    result = {"state":fields[5]}
                else:
                    hp = int(fields[5]);x,y = map(float,fields[6:8])
                    if hp < 0 or hp > 0x7FFFFFFF or not valid_position(x,y):
                        raise ValueError("invalid health or position")
                    result = dict(state="DEAD" if hp==0 else "ALIVE",hp=hp,x=x,y=y)
                if not pending[4].done():
                    pending[4].set_result(result)
            except ValueError:
                logging.warning("马贼生命反馈格式无效")
            return True
        if message.startswith("JRF1:"):
            try:
                fields = message.split(":")
                if len(fields) != 9:
                    raise ValueError("field count")
                obj,scene,sequence,follow,count,missing,level = map(int,fields[1:8])
                guids = set(map(int,fields[8].split(","))) if fields[8] else set()
                pending = self.hunt_follow_pending
                if (pending is None or (sequence,scene,obj) != pending[:3]
                        or scene != self.scene_id or obj != self.obj_id):
                    return True
                if (follow not in (0,1) or not 0 <= missing <= count <= 6
                        or len(guids) != count-missing or not 1 <= level <= 200
                        or any(g <= 0 or g >= 0xFFFFFFFF for g in guids)):
                    raise ValueError("invalid followed team")
                if not pending[3].done():
                    pending[3].set_result(dict(follow=follow,count=count,missing=missing,level=level,guids=guids))
            except ValueError:
                logging.warning("组队跟随真实状态反馈格式无效")
            return True
        if message.startswith("JRP1:"):
            fields = message.split(":")
            try:
                if len(fields) != 6:
                    raise ValueError("field count")
                obj, scene, sequence = map(int, fields[1:4])
                x, y = map(float, fields[4:6])
                pending = self.hunt_position_pending
                if (pending is None or (sequence, scene, obj) != pending[:3]
                        or scene != self.scene_id or obj != self.obj_id
                        or not valid_position(x, y)):
                    return True
                if not pending[3].done():
                    pending[3].set_result((x, y))
            except ValueError:
                logging.warning("刷马真实坐标反馈格式无效")
            return True
        if message.startswith("JRR1:"):
            logging.info("自动修理服务端反馈 %s", message)
            return True
        if message.startswith("JRS1:"):
            fields = message.split(":")
            valid = (len(fields) == 4 and fields[1] == str(self.obj_id)
                     and fields[2] == str(self.scene_id) and fields[3] in ("0", "1"))
            if valid:
                self.is_mounted = fields[3] == "1"
                logging.info("服务端骑乘状态 mounted=%s", self.is_mounted)
            else:
                logging.warning("骑乘状态反馈无效 %s", message)
            if self.mount_state_event is not None and (valid or message.startswith("JRS1:ERROR:")):
                self.mount_state_event.set()
            return True
        return False

    def handle_hunt_message(self, message: str) -> None:
        if self.hunt_role != "leader" or not message.startswith("JRH1:"):
            return
        parts = message.split(":")
        try:
            if len(parts) != 5:
                raise ValueError("field count")
            scene_id, obj_id = int(parts[1]), int(parts[2])
            x, y = float(parts[3]), float(parts[4])
            if scene_id != self.scene_id or obj_id < 0 or not valid_position(x, y):
                raise ValueError("invalid scene, id or coordinates")
        except ValueError as exc:
            logging.warning("忽略异常刷马查询反馈 message=%s error=%s", message, exc)
            return
        now = time.monotonic()
        self.hunt_query_pending = False
        self.hunt_last_response_at = now
        event=getattr(self,"hunt_query_event",None)
        if event is not None:
            event.set()
        if obj_id > 0 and self.rejected_targets.get(obj_id, 0.0) <= now:
            if self.hunt_target is None or self.hunt_target.obj_id != obj_id:
                self.hunt_move_steps = 0
                self.hunt_interaction_target_id = None
                self.attack_attempts = 0
                self.attack_confirmed = False
                self.target_started_at = now
                self.hunt_health_checked_at = 0.
                self.hunt_health_state = "UNKNOWN"
            self.hunt_target = SceneMonster(obj_id, x, y, b"", now)
            self.hunt_waypoint = None
            self.hunt_no_target_since = 0.0
            logging.info("刷马发现目标 scene=%d obj=%d pos=(%.2f,%.2f)", scene_id, obj_id, x, y)
        else:
            self.hunt_target = None
            if self.hunt_no_target_since == 0.0:
                self.hunt_no_target_since = now
            if valid_position(x, y) and (x != 0.0 or y != 0.0):
                if (
                    self.hunt_waypoint is None
                    or math.hypot(self.hunt_waypoint.x - self.x, self.hunt_waypoint.y - self.y) <= 3.0
                    or now - self.hunt_waypoint.seen_at > 30.0
                ):
                    self.hunt_waypoint = SceneMonster(0, x, y, b"", now)
                    logging.info("当前未找到马贼，巡查点=(%.2f,%.2f)", x, y)
            else:
                self.hunt_waypoint = None
                logging.info("当前地图无马贼且无可用巡查点 scene=%d", scene_id)

    async def refresh_hunt_mount(self, writer: asyncio.StreamWriter, force: bool = False) -> bool:
        if (force or self.is_mounted is None
                or getattr(self,"hunt_mount_checked_scene",None) != self.scene_id
                or time.monotonic()-getattr(self,"hunt_mount_checked_at",0.) >= 5.):
            if not await self.query_mount_state(writer):
                self.hunt_travel_active = False
                return False
            self.hunt_mount_checked_at = time.monotonic()
            self.hunt_mount_checked_scene = self.scene_id
        self.hunt_travel_active = self.is_mounted is True
        return True

    async def set_hunt_travel(self, writer: asyncio.StreamWriter, travel: bool) -> None:
        scene,obj=self.scene_id,self.obj_id
        def current():
            return (self.scene_id==scene and self.obj_id==obj
                    and not self.scene_change_pending
                    and (not travel or not getattr(self,"self_return_pending",False))
                    and getattr(self,"death_recovery_stage",None) is None
                    and getattr(self,"current_hp",None)!=0)
        if not current():return
        mount = struct.pack(
            "<fIIIIff", -1.0, 21, 0xFFFFFFFF,
            self.obj_id, 0xFFFFFFFF, -1.0, -1.0,
        )
        if travel:
            if not await self.refresh_hunt_mount(writer) or not current():
                return
            if not self.hunt_travel_active:
                # Mount casting is blocked by formal follow. Finish that state first.
                if self.formal_team_follow or self.hunt_follow_requested_at:
                    from hunt_world_walk import follow_restart_safe
                    scene = self.scene_id
                    position = await self.query_hunt_position(writer)
                    if (position is None or self.scene_id != scene or self.scene_change_pending
                            or not follow_restart_safe(APP_DIR,scene,position)):
                        logging.warning("补上坐骑前位置未确认或靠近出口，暂不解除跟随")
                        return
                    await self.set_hunt_travel(writer,False)
                    if self.formal_team_follow or not current():
                        return
                now = time.monotonic()
                if now-getattr(self,"hunt_mount_retry_at",0.) < 8.:
                    return
                self.hunt_mount_retry_at = now
                self.hunt_mount_wait_until = time.monotonic() + 4.0
                await self.send_game_packet(writer, 0x0099, mount)
                logging.info("刷马队长请求上坐骑 skill=21，等待4秒施法期间停止移动")
                await asyncio.sleep(4.0)
                if not current():return
                if not await self.refresh_hunt_mount(writer,force=True) or not self.hunt_travel_active:
                    logging.warning("上坐骑未获服务端确认，暂停移动，稍后重试")
                    return
            if not current():return
            now = time.monotonic()
            if (
                (self.scene_id in self.hunt_scenes or self.hunt_walk_destination is not None
                 or getattr(self,"hunt_pickup_wait_guid",None) is not None)
                and now - self.hunt_scene_started_at >= 3.0
                and not self.formal_team_follow
                and now - self.hunt_follow_requested_at >= 8.0
            ):
                await self.send_game_packet(writer, 0x01F6)
                self.hunt_follow_requested_at = now
                logging.info("刷马队长请求开启正式组队跟随")
                await asyncio.sleep(1.0)
        else:
            if self.formal_team_follow or self.hunt_follow_requested_at:
                await self.send_game_packet(writer, 0x0064)
                self.hunt_follow_requested_at = 0.0
                logging.info("刷马队长请求停止正式组队跟随")
                stop_deadline = time.monotonic() + 5.0
                while self.formal_team_follow and time.monotonic() < stop_deadline:
                    await asyncio.sleep(0.1)
                    if not current():return
                if self.formal_team_follow:
                    logging.warning("尚未收到退出跟随确认，暂停下坐骑和攻击")
                    return
                logging.info("刷马队长已收到退出跟随确认")
            if not await self.refresh_hunt_mount(writer) or not current():
                return
            if self.hunt_travel_active:
                self.hunt_mount_wait_until = time.monotonic() + 4.0
                await self.send_game_packet(writer, 0x0099, mount)
                logging.info("刷马队长请求下坐骑 skill=21，等待4秒后再攻击")
                await asyncio.sleep(4.0)
                if not current():return
                if not await self.refresh_hunt_mount(writer,force=True) or self.hunt_travel_active:
                    logging.warning("下坐骑未获服务端确认，暂停攻击和回城")

    def hunt_combat_active(self) -> bool:
        target = getattr(self,"hunt_target",None)
        return (target is not None
                and getattr(self,"hunt_interaction_target_id",None) == target.obj_id
                and getattr(self,"hunt_health_state",None) == "ALIVE")

    async def try_hunt_unstuck(self, writer: asyncio.StreamWriter, position: tuple) -> bool:
        if self.hunt_combat_active() or getattr(self,"hunt_unstuck_active",False):
            self.hunt_progress_position = None
            return False
        now=time.monotonic();scene=self.scene_id
        previous=getattr(self,"hunt_progress_position",None)
        if (previous is None or getattr(self,"hunt_progress_scene",None)!=scene
                or math.dist(previous,position)>=2.):
            self.hunt_progress_position=position;self.hunt_progress_scene=scene
            self.hunt_progress_at=now
            return False
        if (now-self.hunt_progress_at<120.
                or now-getattr(self,"hunt_unstuck_last_at",0.)<30.):
            return False
        status=await self.query_hunt_follow(writer)
        if (self.scene_id!=scene or self.scene_change_pending or not self.hunt_follow_ready(status)):
            self.hunt_progress_position=None
            return False
        if not self.hunt_travel_active:
            return False
        from hunt_world_walk import hunt_recovery_step
        self.hunt_unstuck_last_at=now
        try:
            point=await asyncio.to_thread(hunt_recovery_step,APP_DIR,scene,position)
        except (OSError,ValueError,KeyError) as error:
            logging.warning("停留超过2分钟，未找到安全防卡路线：%s",error)
            return False
        if self.scene_id!=scene or self.scene_change_pending:
            return False
        logging.warning("赶路停留超过2分钟，尝试防卡短移 scene=%d from=%s to=%s",scene,position,point)
        self.hunt_unstuck_active=True
        try:
            arrived=await self.move_towards(writer,SceneMonster(0,*point,b"",now),stop_distance=0.,full_path=True)
        finally:
            self.hunt_unstuck_active=False
        if arrived:
            self.hunt_progress_position=(self.x,self.y);self.hunt_progress_at=time.monotonic()
            logging.info("防卡短移完成，重新计算原路线")
        return arrived

    async def ensure_hunt_travel_ready(self, writer: asyncio.StreamWriter) -> bool:
        if self.hunt_combat_active():
            self.hunt_progress_position = None
            return False
        scene = self.scene_id
        await self.set_hunt_travel(writer, True)
        if not self.hunt_travel_active:
            self.hunt_progress_position = None
            self.defer_hunt_search()
            return False
        status = await self.query_hunt_follow(writer)
        if self.scene_id != scene or self.scene_change_pending:
            return False
        if self.hunt_follow_ready(status):
            self.formal_team_follow = True
            return True
        if await self.pick_up_real_member(writer,status):
            self.defer_hunt_search()
            return False
        if status is not None and status["follow"] == 1:
            await self.reinvite_hunt_follow(writer, status)
        now = time.monotonic()
        self.hunt_progress_position = None
        self.defer_hunt_search()
        if now-getattr(self,"hunt_follow_wait_log_at",0.) >= 10.:
            logging.info("同地图刷马等待队员全部进入跟随，暂不出发 scene=%d", scene)
            self.hunt_follow_wait_log_at = now
        return False

    def defer_hunt_search(self) -> None:
        # Waiting for the party is not time spent searching an empty map.
        if getattr(self,"hunt_no_target_since",0.):
            self.hunt_no_target_since=time.monotonic()

    async def hunt_loop(self, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(2.0)
        expected_members = len(self.hunt_members) + 1
        while True:
            now = time.monotonic()
            if self.death_recovery_stage is not None or self.current_hp == 0:
                await asyncio.sleep(1.0)
                continue
            if self.scene_change_pending or self.self_return_pending or self.hunt_expected_scene_id is not None:
                await asyncio.sleep(0.5)
                continue
            if self.in_team and (
                self.leader_obj_id not in (0, self.obj_id)
                or self.leader_guid not in (0, self.hunt_leader_guid)
            ):
                logging.warning("刷马角色当前不是队长，暂停自动刷马 leader=%d", self.leader_obj_id)
                await asyncio.sleep(5.0)
                continue
            selected_guids = {item[2] for item in self.hunt_members}
            if (
                self.hunt_member_count < expected_members
                and not selected_guids.issubset(self.hunt_confirmed_member_guids)
            ):
                self.defer_hunt_search()
                await self.send_hunt_invites(writer)
                await asyncio.sleep(2.0)
                continue
            if await self.finish_real_pickup(writer):
                await asyncio.sleep(.5)
                continue
            if self.scene_id not in self.hunt_scenes:
                await self.request_hunt_scene(writer)
                await asyncio.sleep(2.0)
                continue
            if self.hunt_no_target_since and now - self.hunt_no_target_since > 90.0:
                self.hunt_target = None
                self.hunt_waypoint = None
                if self.hunt_mode == "single":
                    self.hunt_no_target_since = now
                    self.hunt_last_query_at = 0.0
                    logging.info("单地图刷马：继续巡查 scene=%d", self.scene_id)
                else:
                    self.hunt_no_target_since = 0.0
                    await self.request_hunt_scene(writer)
                    await asyncio.sleep(2.0)
                continue
            if self.hunt_query_pending:
                if now - self.hunt_last_query_at > 10.0:
                    logging.warning("刷马目标查询超时 scene=%d", self.scene_id)
                    self.hunt_query_pending = False
                else:
                    await asyncio.sleep(0.5)
                    continue
            if self.hunt_target is None and now - self.hunt_last_query_at > 8.0:
                await self.query_hunt_target(writer)
                await asyncio.sleep(0.5)
                continue
            target = self.hunt_target
            if target is not None:
                if now-self.hunt_health_checked_at >= 2.:
                    health = await self.query_hunt_health(writer,target.obj_id)
                    self.hunt_health_checked_at = time.monotonic()
                    self.hunt_health_state = health["state"] if health else "UNKNOWN"
                    if self.hunt_target is None or self.hunt_target.obj_id != target.obj_id:
                        continue
                    if health is None or health["state"] == "UNKNOWN":
                        await asyncio.sleep(.5)
                        continue
                    if health["state"] in ("DEAD","MISSING"):
                        self.finish_hunt_target(target.obj_id,health["state"])
                        await self.query_hunt_target(writer)
                        continue
                    target.x,target.y = health["x"],health["y"]
                if self.hunt_health_state != "ALIVE":
                    await asyncio.sleep(.5)
                    continue
                now = time.monotonic()
                distance = math.hypot(target.x - self.x, target.y - self.y)
                if self.hunt_combat_active():
                    self.hunt_progress_position = None
                if distance > 5.0 and not self.hunt_combat_active():
                    if self.hunt_move_steps >= 60:
                        logging.warning("刷马目标久未到达，暂时跳过 obj=%d", target.obj_id)
                        self.rejected_targets[target.obj_id] = now + 60.0
                        self.hunt_target = None
                        self.hunt_move_steps = 0
                        continue
                    if not await self.ensure_hunt_travel_ready(writer):
                        await asyncio.sleep(0.5)
                        continue
                    arrived = await self.move_towards(writer, target, stop_distance=3.5, full_path=True)
                    if not arrived:
                        if self.hunt_move_waiting_for_team or self.hunt_move_recovered:
                            await asyncio.sleep(.5)
                            continue
                        self.rejected_targets[target.obj_id] = time.monotonic() + 30.0
                        self.hunt_target = None
                        continue
                    self.hunt_move_steps += 1
                    continue
                await self.lock_target(writer, target)
                if self.hunt_interaction_target_id != target.obj_id:
                    # Bandit capture: lock 0x062A, then default-event body
                    # 2a060000ffffffff. The event explicitly carries ObjID.
                    await self.send_game_packet(
                        writer, 0x0305, struct.pack("<II", target.obj_id, 0xFFFFFFFF)
                    )
                    self.hunt_interaction_target_id = target.obj_id
                    logging.info("刷马队长已请求交互 obj=%d distance=%.2f", target.obj_id, distance)
                await self.set_hunt_travel(writer, False)
                if (self.formal_team_follow or self.hunt_travel_active
                        or self.is_mounted is not False):
                    await asyncio.sleep(.5)
                    continue
                # A teammate may kill the bandit during the dismount cast.
                if time.monotonic()-self.hunt_health_checked_at >= 2.:
                    continue
                if distance > 15.0:
                    # Chase an already engaged bandit on foot without starting follow.
                    await self.move_towards(writer,target,stop_distance=12.,full_path=True)
                    await asyncio.sleep(.5)
                    continue
                if not self.attack_confirmed and self.attack_attempts >= 2:
                    if now - self.target_started_at > 4.0:
                        logging.warning("刷马目标攻击未获确认，暂时跳过 obj=%d", target.obj_id)
                        self.rejected_targets[target.obj_id] = now + 30.0
                        self.hunt_target = None
                    await asyncio.sleep(0.5)
                    continue
                await self.use_normal_attack(writer, target)
                await asyncio.sleep(1.2)
                continue
            waypoint = self.hunt_waypoint
            if waypoint is not None and math.hypot(waypoint.x - self.x, waypoint.y - self.y) > 3.0:
                if not await self.ensure_hunt_travel_ready(writer):
                    await asyncio.sleep(0.5)
                    continue
                arrived = await self.move_towards(writer, waypoint, stop_distance=2.0, full_path=True)
                if not arrived:
                    if self.hunt_move_waiting_for_team or self.hunt_move_recovered:
                        await asyncio.sleep(.5)
                        continue
                    self.hunt_waypoint = None
                    self.hunt_last_query_at = 0.0
                continue
            await asyncio.sleep(0.5)

    async def combat_loop(self, writer: asyncio.StreamWriter) -> None:
        if self.hunt_role:
            gather_deadline = time.monotonic() + 65.0
            while self.scene_id != 2:
                if time.monotonic() >= gather_deadline:
                    logging.warning("刷马启动集合暂未完成，继续等待并重试 scene=%d", self.scene_id)
                    gather_deadline = time.monotonic() + 30.0
                if (
                    self.death_recovery_stage is None
                    and not self.scene_change_pending
                    and not self.self_return_pending
                ):
                    logging.info("刷马启动：先返回大理集合 scene=%d", self.scene_id)
                    await self.follow_leader_to_dali(writer)
                await asyncio.sleep(0.5)
            logging.info("刷马启动集合完成，当前位置大理")
        if self.hunt_role == "leader":
            await self.hunt_loop(writer)
            return
        await asyncio.sleep(1.0)
        while True:
            now = time.monotonic()
            if self.hunt_role == "member" and (
                not self.in_team or self.leader_guid != self.hunt_leader_guid
            ):
                await asyncio.sleep(0.5)
                continue
            if await self.recover_hunt_member_step(writer):
                await asyncio.sleep(.5)
                continue
            if await self.gather_hunt_member_safe(writer):
                await asyncio.sleep(.5)
                continue
            if not self.can_auto_combat():
                self.last_locked_id = 0xFFFFFFFF
                self.leader_target_id = 0
                self.first_swing = True
                self.attack_attempts = 0
                self.attack_confirmed = False
                if self.combat_paused_scene != self.scene_id:
                    logging.info("安全地图待机 map=%d：停止移动、锁怪和攻击", self.scene_id)
                    self.combat_paused_scene = self.scene_id
                await asyncio.sleep(1.0)
                continue
            if self.combat_paused_scene is not None:
                logging.info("离开非战斗地图 map=%d：恢复自动移动和打怪", self.scene_id)
                self.combat_paused_scene = None
            if (
                self.scene_change_pending
                or self.self_return_pending
                or self.death_recovery_stage is not None
                or time.monotonic() < self.follow_transition_until
            ):
                await asyncio.sleep(0.25)
                continue
            if (self.formal_team_follow
                    or (self.hunt_role == "member" and self.team_follow_requested)):
                if not self.combat_paused_for_follow:
                    logging.info("正式组队跟随中，等待退出后再攻击")
                    self.combat_paused_for_follow = True
                await asyncio.sleep(0.5)
                continue
            if self.combat_paused_for_follow:
                logging.info("已退出正式组队跟随，恢复攻击")
                self.combat_paused_for_follow = False
            self.monsters = {
                obj_id: monster
                for obj_id, monster in self.monsters.items()
                if now - monster.seen_at < 180.0
            }
            if (
                self.in_team
                and not self.formal_team_follow
                and self.leader_obj_id
                and self.leader_x is not None
                and self.leader_y is not None
                and valid_position(self.leader_x, self.leader_y)
            ):
                leader_distance = math.hypot(
                    self.leader_x - self.x, self.leader_y - self.y
                )
                nearby_monster = any(
                    self.rejected_targets.get(monster.obj_id, 0.0) <= now
                    and math.hypot(monster.x - self.x, monster.y - self.y) <= 15.0
                    for monster in self.monsters.values()
                )
                if leader_distance > 10.0 and not nearby_monster:
                    leader = SceneMonster(
                        self.leader_obj_id,
                        self.leader_x,
                        self.leader_y,
                        b"",
                        time.monotonic(),
                    )
                    await self.move_towards(writer, leader, stop_distance=4.0)
                    logging.info("正在跟随队长 obj=%d", self.leader_obj_id)
                    await asyncio.sleep(2.0)
                    continue
            if (
                self.in_team
                and self.leader_target_id not in (0, 0xFFFFFFFF)
                and self.leader_target_id not in self.monsters
                and (
                    now - self.leader_target_seen_at < 5.0
                    or (
                        self.last_locked_id == self.leader_target_id
                        and self.attack_confirmed
                        and now - self.leader_target_last_broadcast_at < 2.0
                    )
                )
            ):
                leader_distance = None
                if (
                    self.leader_x is not None
                    and self.leader_y is not None
                    and valid_position(self.leader_x, self.leader_y)
                ):
                    leader_distance = math.hypot(
                        self.leader_x - self.x, self.leader_y - self.y
                    )
                    if (
                        leader_distance > 0.75
                        and now - self.last_unseen_leader_move_at >= 1.5
                    ):
                        leader = SceneMonster(
                            self.leader_obj_id, self.leader_x, self.leader_y, b"", now
                        )
                        await self.move_towards(writer, leader, stop_distance=0.5)
                        self.last_unseen_leader_move_at = now
                if (
                    leader_distance is not None
                    and leader_distance <= 3.0
                    and (
                        self.attack_confirmed
                        or self.unseen_leader_attack_attempts < 2
                    )
                    and now - self.last_unseen_leader_attack_at >= 1.2
                ):
                    # The leader broadcast supplies an authoritative target ID,
                    # but no position. Try the same ID after approaching; the
                    # server decides whether it is in range and attackable.
                    if self.last_locked_id != self.leader_target_id:
                        await self.send_game_packet(
                            writer, 0x0073, struct.pack("<I", self.leader_target_id)
                        )
                        self.last_locked_id = self.leader_target_id
                        self.target_started_at = now
                        self.attack_attempts = 0
                        self.attack_confirmed = False
                    target = SceneMonster(
                        self.leader_target_id, self.x, self.y, b"", now
                    )
                    await self.use_normal_attack(writer, target)
                    self.unseen_leader_attack_attempts += 1
                    self.last_unseen_leader_attack_at = now
                    logging.info(
                        "视野外协攻尝试 target=%d attempts=%d",
                        self.leader_target_id,
                        self.unseen_leader_attack_attempts,
                    )
                await asyncio.sleep(0.5)
                continue
            if self.in_team:
                if (
                    self.leader_target_id in (0, 0xFFFFFFFF)
                    or now - self.leader_target_last_broadcast_at > 5.0
                ):
                    self.last_locked_id = 0xFFFFFFFF
                    self.attack_confirmed = False
                    await asyncio.sleep(0.5)
                    continue
                target = self.monsters.get(self.leader_target_id)
                if target is None or self.rejected_targets.get(target.obj_id, 0.0) > now:
                    await asyncio.sleep(0.5)
                    continue
                await self.lock_target(writer, target)
            else:
                if not self.monsters:
                    self.last_locked_id = 0xFFFFFFFF
                    await asyncio.sleep(1.0)
                    continue
                target = self.monsters.get(self.last_locked_id)
                if (
                    target is None
                    or self.rejected_targets.get(target.obj_id, 0.0) > now
                ):
                    await self.lock_nearest(writer)
                    target = self.monsters.get(self.last_locked_id)
            if target is None:
                await asyncio.sleep(0.5)
                continue
            distance = math.hypot(target.x - self.x, target.y - self.y)
            if distance > 15.0:
                await self.move_towards(writer, target, stop_distance=12.0)
                await asyncio.sleep(2.0)
            elif self.attack_confirmed:
                # This customized engine only maintains a few swings after the
                # initial request, and using medicine interrupts that state.
                # Keep submitting the learned first-xinfa attack at a modest
                # cadence until death/removal or a new leader target arrives.
                await self.use_normal_attack(writer, target)
                await asyncio.sleep(1.2)
            elif self.attack_attempts >= 2:
                if time.monotonic() - self.target_started_at >= 4.0:
                    logging.warning("目标不可攻击，临时跳过 obj=%d", target.obj_id)
                    self.rejected_targets[target.obj_id] = time.monotonic() + 60.0
                    self.last_locked_id = 0xFFFFFFFF
                    self.first_swing = True
                await asyncio.sleep(0.5)
            else:
                await self.use_normal_attack(writer, target)
                await asyncio.sleep(0.6 if self.attack_attempts > 1 else 0.5)

    async def login(self) -> LoginResult:
        adapter = load_adapter(self.auth_profile)
        self.login_stage = "connect"
        logging.info("连接登录服务器 %s:%d", self.login_host, self.login_port)
        reader, writer = await asyncio.open_connection(self.login_host, self.login_port)
        try:
            writer.write(checked_packet(adapter.build_hello(self.account)))
            await writer.drain()
            packet_id, _, _, challenge = await read_packet(reader)
            if packet_id != 0x0137:
                raise RuntimeError(f"登录握手包错误: 0x{packet_id:04x}")

            auth = checked_packet(adapter.build_auth(self.account, challenge))
            writer.write(auth)
            await writer.drain()
            logging.info("已发送认证包")
            self.login_stage = "auth"

            client_state = 0x7D
            while True:
                packet_id, state, body, _ = await read_packet(reader)
                logging.debug("LOGIN S->C id=0x%04x state=0x%02x size=%d", packet_id, state, len(body))
                if packet_id == 0x00DF:
                    self.login_stage = "wait_login_ready"
                    logging.info("认证响应 size=%d header=%s", len(body), body[:4].hex())
                    logging.info("账号认证成功")
                elif packet_id == 0x022A:
                    self.login_stage = "character_list"
                    request = self.account.encode("ascii")[:50].ljust(50, b"\0")
                    writer.write(make_packet(0x02C7, client_state, request))
                    await writer.drain()
                    client_state = (client_state + 1) & 0xFF
                    logging.info("请求角色列表")
                elif packet_id == 0x013F:
                    if len(body) < 63:
                        raise RuntimeError(f"角色列表过短: {len(body)}")
                    # Packet offset 61 in the complete frame is body offset 55.
                    self.guid64 = body[55:63]
                    self.role_name = decode_name(body[63:95])
                    if self.guid64 == b"\0" * 8:
                        if not self.create_role_name:
                            raise RuntimeError("账号没有可用角色")
                        if self.create_role_sent:
                            continue
                        role_name = self.create_role_name.encode("gb18030")
                        if len(role_name) > 35:
                            raise RuntimeError("待创建角色名超过35字节")
                        writer.write(make_packet(0x0308, client_state))
                        client_state = (client_state + 1) & 0xFF
                        # Use the exact normal-client creation template. Some
                        # server builds couple this template to first-login
                        # inventory initialization; cosmetic normalization can
                        # be performed after the role has been created.
                        hair_model = 2 if self.create_role_sex == 0 else 3
                        face_model = 1 if self.create_role_sex == 0 else 100
                        create_body = (
                            role_name.ljust(36, b"\0")
                            + struct.pack(
                                "<IIIIB",
                                hair_model,
                                face_model,
                                5,
                                0,
                                self.create_role_sex,
                            )
                        )
                        writer.write(make_packet(0x020A, client_state, create_body))
                        await writer.drain()
                        client_state = (client_state + 1) & 0xFF
                        self.create_role_sent = True
                        logging.info(
                            "已请求服务端创建角色 name=%s sex=%d hair=%d face=%d",
                            self.create_role_name,
                            self.create_role_sex,
                            hair_model,
                            face_model,
                        )
                        continue
                    logging.info(
                        "选择角色 name=%s guid=%s",
                        self.role_name,
                        self.guid64.hex(),
                    )
                    self.login_stage = "character_login"
                    writer.write(make_packet(0x01AA, client_state, self.guid64))
                    await writer.drain()
                    client_state = (client_state + 1) & 0xFF
                elif packet_id == 0x0309:
                    result = int.from_bytes(body[:4], "little") if len(body) >= 4 else -1
                    logging.info("创建角色结果 result=%d", result)
                    if result != 1:
                        raise RuntimeError(f"服务端创建角色失败 result={result}")
                elif packet_id == 0x0026:
                    if len(body) < 36:
                        raise RuntimeError(f"游戏地址包过短: {len(body)}")
                    server_id = int.from_bytes(body[0:4], "little")
                    game_port = int.from_bytes(body[4:6], "little")
                    game_host = body[8:32].split(b"\0", 1)[0].decode("ascii")
                    key = body[32:36]
                    logging.info(
                        "取得游戏服务器 %s:%d key=%s server_id=%d",
                        game_host,
                        game_port,
                        key.hex(),
                        server_id,
                    )
                    if self.game_host_override:
                        logging.info(
                            "使用控制台游戏服地址覆盖 %s:%d -> %s:%d",
                            game_host,
                            game_port,
                            self.game_host_override,
                            self.game_port_override or game_port,
                        )
                        game_host = self.game_host_override
                    if self.game_port_override:
                        game_port = self.game_port_override
                    return LoginResult(
                        self.guid64,
                        self.role_name,
                        game_host,
                        game_port,
                        key,
                        server_id,
                    )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)

    def connect_body(self, result: LoginResult) -> bytes:
        account = self.account.encode("ascii")[:50].ljust(50, b"\0")
        body = b"".join(
            (
                result.key,
                result.guid64[:4],
                struct.pack("<H", result.server_id),
                account,
                struct.pack("<I", 0),
                struct.pack("<I", OLD_VERSION),
                struct.pack("<I", 0),
                struct.pack("<I", 0),
                bytes(32),
            )
        )
        if len(body) != 108:
            raise AssertionError(len(body))
        return body

    async def heartbeat_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(10)
            state = await self.send_game_packet(writer, 0x017A, b"\0\0")
            logging.debug("发送心跳 state=0x%02x", state)

    async def leader_shout_loop(self, writer: asyncio.StreamWriter) -> None:
        last_sent=0.;signature=None;last_error=0.
        while True:
            await asyncio.sleep(1.)
            try:
                settings=load_shout_settings(APP_DIR,self.account)
                plan=plan_for_account(HUNT_PLAN_PATH,self.account) or {}
            except (OSError,ValueError) as error:
                if time.monotonic()-last_error>=30.:
                    logging.warning("队长喊话配置读取失败，暂停喊话：%s",error)
                    last_error=time.monotonic()
                continue
            current=(settings['enabled'],settings['leader'],settings['channel'],
                     settings['text'],settings['interval_seconds'])
            if current!=signature:
                signature=current;last_sent=time.monotonic()
                logging.info("队长喊话设置已更新 enabled=%s channel=%s interval=%ds",
                             settings['enabled'],settings['channel'],settings['interval_seconds'])
            if (not settings['enabled'] or settings['leader']!=self.account
                    or not plan.get('enabled') or plan.get('leader')!=self.account
                    or self.hunt_role!='leader' or not self.in_team
                    or self.leader_guid!=self.hunt_leader_guid or self.scene_change_pending
                    or self.self_return_pending or self.death_recovery_stage is not None
                    or self.current_hp==0):
                continue
            if time.monotonic()-last_sent<settings['interval_seconds']:continue
            context=None
            if settings['channel'] in ('team','guild','sect'):
                try:context=await self.query_shout_context(writer)
                except (OSError,ConnectionError):
                    logging.warning("喊话连接已断开，停止喊话任务");return
                if context is None:
                    last_sent=time.monotonic()
                    logging.warning("喊话频道身份查询未确认，本轮暂停")
                    continue
            try:body=chat_body(settings['channel'],settings['text'],context)
            except ValueError as error:
                last_sent=time.monotonic();logging.warning("本轮未发送喊话：%s",error)
                continue
            if self.scene_change_pending or self.death_recovery_stage is not None:continue
            try:await self.send_game_packet(writer,0x02B2,body)
            except (OSError,ConnectionError):
                logging.warning("喊话连接已断开，停止喊话任务");return
            last_sent=time.monotonic()
            logging.info("队长已发送喊话请求 channel=%s text=%s",settings['channel'],settings['text'])

    async def query_shout_context(self, writer: asyncio.StreamWriter) -> dict | None:
        sequence=(getattr(self,'shout_sequence',0)+1)&0x7FFFFFFF;self.shout_sequence=sequence
        scene,obj=self.scene_id,self.obj_id
        future=asyncio.get_running_loop().create_future();pending=(sequence,scene,obj,future)
        self.shout_context_pending=pending
        name=b'Jiaren'
        body=struct.pack('<IB',9998496,len(name))+name+struct.pack('<BIIII',4,13,sequence,scene,obj)
        try:
            await self.send_game_packet(writer,0x0042,body)
            return await asyncio.wait_for(future,2.5)
        except asyncio.TimeoutError:return None
        finally:
            if self.shout_context_pending is pending:self.shout_context_pending=None

    async def auto_equip_starter_weapon(
        self, writer: asyncio.StreamWriter
    ) -> None:
        # Confirmed by two normal-client captures: the first byte is the
        # current backpack position and the following U16 is zero. A weapon in
        # position 9 produced 09 00 00; a fresh weapon in position 1 produced
        # 01 00 00, both with a successful 0x0262 response.
        # Inventory position is not fixed. GCDetailEquipList (0x02DC) supplies
        # each item's current backpack slot; wait for the starter cleaver row.
        for _ in range(4):
            if self.starter_weapon_slot is not None:
                break
            await asyncio.sleep(0.5)
        if self.starter_weapon_slot is None:
            # Fresh characters receive their starter weapon in the second
            # backpack cell. Backpack positions are zero-based, so its
            # protocol position is 1. The lightweight bot does not request the
            # full inventory list, therefore use that creation default.
            self.starter_weapon_slot = 1
            logging.info("未收到背包明细，使用新手武器默认位置 bag_slot=1")
        slot = self.starter_weapon_slot
        body = struct.pack("<BH", slot, 0)
        state = await self.send_game_packet(writer, 0x0276, body)
        logging.info(
            "已发送自动穿戴新手武器 packet=0x0276 bag_slot=%d state=0x%02x",
            slot,
            state,
        )

    def read_hp_percent(self, body: bytes) -> int | None:
        """Read current/max HP from this server's extended 0x0140 packet."""
        if len(body) < 27 or struct.unpack_from("<I", body, 0)[0] != self.obj_id:
            return None
        previous_hp = self.current_hp

        # This engine uses a fixed 23-byte attribute-mask header.  In the full
        # snapshot all mask bytes are set and values begin with level, HP, MP,
        # max HP and max MP (five U32 values).
        if len(body) >= 100:
            current_hp = struct.unpack_from("<I", body, 27)[0]
            max_hp = struct.unpack_from("<I", body, 35)[0]
            if 0 < max_hp < 100_000_000 and current_hp <= max_hp:
                self.current_hp = current_hp
                self.max_hp = max_hp
        else:
            flags = struct.unpack_from("<I", body, 4)[0]
            if flags & (1 << 1):
                value_offset = 23 + (4 if flags & (1 << 0) else 0)
                if value_offset + 4 <= len(body):
                    current_hp = struct.unpack_from("<I", body, value_offset)[0]
                    if current_hp < 100_000_000:
                        self.current_hp = current_hp

        if previous_hp is not None and self.current_hp is not None and self.current_hp < previous_hp:
            self.last_combat_at = time.monotonic()
        if self.current_hp is None or not self.max_hp:
            return None
        return min(100, (self.current_hp * 100) // self.max_hp)

    async def auto_heal(self, writer: asyncio.StreamWriter) -> None:
        try:
            now = time.monotonic()
            if now < self.hunt_mount_wait_until:
                return
            if self.hp_percent is None or self.hp_percent >= 80:
                return
            if now - self.last_heal_at < 10.0 or not self.healing_gourds:
                return
            slot = min(self.healing_gourds)
            item_guid, item_world = self.healing_gourds[slot]
            # Captured on this engine with 31000001: bag slot, item type,
            # item-world field, item GUID, self ObjID, three -1.0 target
            # coordinates/direction values, empty pet GUID, target-item 0xff.
            body = struct.pack(
                "<BIIIIfffIIB",
                slot,
                31000001,
                item_world,
                item_guid,
                self.obj_id,
                -1.0,
                -1.0,
                -1.0,
                0,
                0,
                0xFF,
            )
            state = await self.send_game_packet(writer, 0x00DD, body)
            self.last_heal_at = time.monotonic()
            logging.info(
                "血量低于80%%，已使用仙丹葫芦 hp=%d slot=%d guid=%d "
                "packet=0x00DD state=0x%02x",
                self.hp_percent,
                slot,
                item_guid,
                state,
            )
        finally:
            self.heal_task = None

    async def request_initial_bag(self, writer: asyncio.StreamWriter) -> None:
        # Normal client sends an initialization burst before GCItemList. The
        # server does not emit starter items for a role that skips this burst.
        obj = struct.pack("<I", self.obj_id)
        init = [
            # The capture also contained neighbor queries for ObjIDs 139/143.
            # They are scene-specific and must never be replayed on entry.
            (0x037C, bytes.fromhex("7c03")), (0x0199, b""), (0x0199, b""),
            (0x0042, bytes.fromhex("f79e0d00084f6e4f70656e55490100000000")),
            (0x0042, bytes.fromhex("f89e0d00084f6e4f70656e55490100000000")),
            (0x0003, b""), (0x009A, bytes.fromhex("d63a0000")),
            (0x0322, bytes.fromhex("d63a0000")), (0x0293, bytes.fromhex("d63a0000")),
            (0x02C5, bytes.fromhex("01000000")), (0x022B, bytes.fromhex("40ed1500")),
            (0x00FB, bytes.fromhex("d63a0000")), (0x012F, bytes.fromhex("d63a0000")),
            (0x00BF, bytes.fromhex("d63a0000")), (0x0380, bytes.fromhex("01000000d63a0000")),
            (0x0506, bytes.fromhex("00000000d63a0000")), (0x01BF, bytes.fromhex("d63a0000")),
            (0x01A5, bytes.fromhex("d63a0000")), (0x007E, b""), (0x01E7, bytes.fromhex("d63a0000")),
            (0x0442, bytes.fromhex("ff00")), (0x00CC, bytes.fromhex("0300000000ffff0301")),
            (0x0253, bytes.fromhex("502e91dc67410000")), (0x04E2, bytes.fromhex("d63a0000")),
            (0x035C, b""), (0x03C6, b""), (0x03D7, bytes.fromhex("000000000000000001")),
            (0x0405, bytes.fromhex("00000000006300000001")), (0x03F3, bytes.fromhex("01000000")),
            (0x04F0, bytes.fromhex("00000000")), (0x0541, bytes.fromhex("0102")),
            (0x0068, bytes.fromhex("60ee150002000000")), (0x008B, bytes.fromhex("01")),
            (0x008B, bytes.fromhex("02")), (0x0059, bytes.fromhex("d63a000000000000000000000000000000000000")),
            (0x0017, b""), (0x02D9, bytes.fromhex("0000000000")), (0x0173, bytes.fromhex("d63a0000")),
            (0x02CF, b""), (0x019F, bytes.fromhex("01")), (0x0400, bytes.fromhex("00000000")),
            (0x005F, bytes.fromhex("02000000000000000000000000")), (0x0182, bytes.fromhex("00")),
            (0x03CB, bytes.fromhex("ffffffff")), (0x018D, b""), (0x0434, bytes.fromhex("00")),
            (0x0199, b""), (0x0042, bytes.fromhex("8d950d0013557064617465526564456e76656c6f70654d4400d903140000")),
            (0x03D9, bytes.fromhex("1fbc0d007cfcffff000000000000000000000000")),
        ]
        for packet_id, body in init:
            if self.obj_id:
                body = body.replace(bytes.fromhex("d63a0000"), obj)
            await self.send_game_packet(writer, packet_id, body)
        logging.info("已发送正常客户端初始化序列，共%d包，末尾请求背包列表", len(init))

    async def game(self, result: LoginResult) -> None:
        logging.info("连接游戏服务器 %s:%d", result.game_host, result.game_port)
        reader, writer = await asyncio.open_connection(result.game_host, result.game_port)
        heartbeat_task = None
        shout_task = None
        leader_state_task = None
        try:
            writer.write(make_packet(0x0290, 0x7B, self.connect_body(result)))
            await writer.drain()
            logging.info("发送 CGConnect")

            while True:
                packet_id, state, body, _ = await read_packet(reader)
                logging.debug("GAME S->C id=0x%04x state=0x%02x size=%d", packet_id, state, len(body))
                if packet_id == 0x0283:
                    if len(body) < 14:
                        raise RuntimeError("GCConnect body too short")
                    self.scene_id, self.scene_resource_id = struct.unpack_from("<HH", body, 0)
                    self.x, self.y = struct.unpack_from("<ff", body, 4)
                    estate = int.from_bytes(body[12:14], "little")
                    logging.info(
                        "准备进场 map=%d pos=(%.2f, %.2f) estate=%d",
                        self.scene_id,
                        self.x,
                        self.y,
                        estate,
                    )
                    enter_body = struct.pack("<HBff", self.scene_id, 0, self.x, self.y)
                    writer.write(make_packet(0x0015, 0x7C, enter_body))
                    await writer.drain()
                    self.client_state = 0x7D
                elif packet_id == 0x0058:
                    ret = None
                    if len(body) == 33:
                        ret = body[0]
                        self.scene_id, self.scene_resource_id = struct.unpack_from("<HH", body, 1)
                        self.x, self.y = struct.unpack_from("<ff", body, 5)
                        self.obj_id = struct.unpack_from("<I", body, 13)[0]
                        self.team_invite_region_id = struct.unpack_from("<H", body, 31)[0]
                        if (
                            ret == 0
                            and self.scene_id in (
                                self.leader_scene_id, self.hunt_expected_scene_id
                            )
                            and self.script_teleport_watchdog_task is not None
                        ):
                            self.script_teleport_watchdog_task.cancel()
                            self.script_teleport_watchdog_task = None
                        logging.info(
                            "进场确认 ret=%d self_obj=%d scene=%d resource=%d raw=%s",
                            ret,
                            self.obj_id,
                            self.scene_id,
                            self.scene_resource_id,
                            body.hex(),
                        )
                    if self.scene_change_pending:
                        self.scene_change_pending = False
                        self.pending_scene_body = None
                        logging.info("跨地图进场完成 map=%d，恢复自动战斗", self.scene_id)
                    if ret == 0 and self.scene_id == self.hunt_expected_scene_id:
                        self.hunt_expected_scene_id = None
                        self.hunt_scene_started_at = time.monotonic()
                        self.hunt_no_target_since = 0.0
                        self.hunt_target = None
                        self.hunt_waypoint = None
                        self.hunt_move_steps = 0
                        self.hunt_query_pending = False
                        self.hunt_last_query_at = 0.0
                        logging.info("刷马队长进入搜索地图 scene=%d", self.scene_id)
                    elif ret not in (None, 0) and self.hunt_expected_scene_id is not None:
                        logging.warning("刷马换图被服务器拒绝 ret=%d", ret)
                        self.hunt_expected_scene_id = None
                        self.script_teleport_pending = False
                    # A successful entry invalidates any missing-leader timer
                    # inherited from the previous scene. A fresh removal event
                    # in this scene may start a new confirmation period.
                    self.mark_leader_present()
                    self.online_since = time.monotonic()
                    logging.info(
                        "角色已进入游戏 name=%s map=%d pos=(%.2f, %.2f)",
                        result.role_name,
                        self.scene_id,
                        self.x,
                        self.y,
                    )
                    # On this engine GCNotifyChangeScene(hell) can arrive just
                    # before the self-death packet.  In that ordering the
                    # generic scene-change path completes while recovery is
                    # still "dead" (or "to_hell"), so requiring only
                    # "entering_hell" leaves the bot stranded in hell.
                    if self.scene_id == 77:
                        self.begin_hell_recovery(writer)
                    elif self.scene_id == 0 and self.death_recovery_stage == "entering_luoyang":
                        self.death_recovery_stage = None
                        logging.info("死亡恢复流程完成，角色已回到洛阳")
                    # Send the normal initialization burst for every session;
                    # it is what causes the server to publish GCItemList.
                    await self.request_initial_bag(writer)
                    if heartbeat_task is None:
                        heartbeat_task = asyncio.create_task(self.heartbeat_loop(writer))
                    if self.hunt_role=='leader' and shout_task is None:
                        shout_task=asyncio.create_task(self.leader_shout_loop(writer))
                    if self.hunt_role=='leader' and leader_state_task is None:
                        leader_state_task=asyncio.create_task(self.publish_managed_leader_state(writer))
                    if self.repair_task is None:
                        self.repair_task = asyncio.create_task(self.automatic_repair(writer))
                    if self.auto_equip_weapon and self.equip_task is None:
                        self.equip_task = asyncio.create_task(
                            self.auto_equip_starter_weapon(writer)
                        )
                    elif not self.auto_equip_weapon:
                        logging.info("角色已有装备，已跳过上线自动穿戴")
                    if self.auto_fight and self.combat_task is None:
                        self.combat_task = asyncio.create_task(self.combat_loop(writer))
                elif packet_id == 0x02DC:
                    # Captured item row: slot U16, compatibility U32,
                    # item-world U32, item GUID U32, then item type U32.
                    if len(body) >= 18:
                        bag_slot = struct.unpack_from("<H", body, 0)[0]
                        item_world = struct.unpack_from("<I", body, 6)[0]
                        item_guid = struct.unpack_from("<I", body, 10)[0]
                        item_type = struct.unpack_from("<I", body, 14)[0]
                        if item_type == 10100100:
                            self.starter_weapon_slot = bag_slot
                            logging.info(
                                "发现背包新手武器 item_type=%d bag_slot=%d",
                                item_type,
                                bag_slot,
                            )
                        elif item_type == 31000001:
                            self.healing_gourd_slots.add(bag_slot)
                            self.healing_gourds[bag_slot] = (item_guid, item_world)
                            logging.info(
                                "发现仙丹葫芦 item_type=%d bag_slot=%d guid=%d total=%d",
                                item_type,
                                bag_slot,
                                item_guid,
                                len(self.healing_gourd_slots),
                            )
                elif packet_id == 0x0140:
                    logging.info(
                        "角色基础属性原始包 self=%d size=%d body=%s",
                        self.obj_id,
                        len(body),
                        body.hex(),
                    )
                    hp_percent = self.read_hp_percent(body)
                    if hp_percent is not None:
                        self.hp_percent = hp_percent
                        logging.info(
                            "自身血量更新 hp=%d/%d (%d%%)",
                            self.current_hp,
                            self.max_hp,
                            hp_percent,
                        )
                        if self.current_hp == 0:
                            self.begin_death_recovery(writer, "self_hp_zero")
                        elif (
                            hp_percent < 80
                            and self.death_recovery_stage is None
                            and self.heal_task is None
                            and time.monotonic() - self.last_heal_at >= 10.0
                        ):
                            self.heal_task = asyncio.create_task(
                                self.auto_heal(writer)
                            )
                elif packet_id in (0x0047, 0x0158):
                    self.handle_death_notice(writer, packet_id, body)
                elif packet_id == 0x0262 and body:
                    equip_result = body[0]
                    if equip_result == 1:
                        logging.info("新手武器自动穿戴成功")
                    else:
                        logging.warning(
                            "新手武器自动穿戴失败 result=%d body=%s",
                            equip_result,
                            body.hex(),
                        )
                elif packet_id == 0x0141:
                    monster = self.remember_monster(body)
                    if monster:
                        distance = math.hypot(monster.x - self.x, monster.y - self.y)
                        logging.info(
                            "发现场景目标 obj=%d pos=(%.2f, %.2f) distance=%.2f",
                            monster.obj_id,
                            monster.x,
                            monster.y,
                            distance,
                        )
                        if self.can_auto_combat() and not self.in_team and self.hunt_role is None:
                            await self.lock_nearest(writer)
                    else:
                        logging.warning("异常 GCNewMonster size=%d body=%s", len(body), body.hex())
                elif packet_id == 0x0090 and len(body) >= 4:
                    removed_id = struct.unpack_from("<I", body, 0)[0]
                    self.monsters.pop(removed_id, None)
                    self.players.pop(removed_id, None)
                    if removed_id == self.last_locked_id:
                        self.last_locked_id = 0xFFFFFFFF
                        self.first_swing = True
                        self.attack_confirmed = False
                        self.attack_attempts = 0
                    if removed_id == self.leader_target_id:
                        self.leader_target_id = 0
                    if removed_id == self.leader_obj_id:
                        self.mark_leader_missing(writer)
                    if self.hunt_target and removed_id == self.hunt_target.obj_id:
                        self.hunt_target = None
                elif packet_id == 0x012A and len(body) == 26:
                    source_id = struct.unpack_from("<I", body, 0)[0]
                    target_id = struct.unpack_from("<I", body, 12)[0]
                    if target_id == self.obj_id and source_id != self.obj_id:
                        self.last_combat_at = time.monotonic()
                    if (
                        self.in_team
                        and self.leader_obj_id not in (0, 0xFFFFFFFF)
                        and source_id == self.leader_obj_id
                        and target_id not in (0, 0xFFFFFFFF, self.obj_id, self.leader_obj_id)
                    ):
                        previous_target = self.leader_target_id
                        if previous_target and previous_target != target_id:
                            if self.last_locked_id == previous_target:
                                self.last_locked_id = 0xFFFFFFFF
                                self.first_swing = True
                                self.attack_attempts = 0
                                self.attack_confirmed = False
                            logging.info(
                                "队长已切换目标 old=%d new=%d",
                                previous_target,
                                target_id,
                            )
                        self.leader_target_id = target_id
                        self.leader_target_last_broadcast_at = time.monotonic()
                        if previous_target != target_id:
                            self.leader_target_seen_at = time.monotonic()
                            self.unseen_leader_attack_attempts = 0
                            self.last_unseen_leader_attack_at = 0.0
                            self.last_unseen_leader_move_at = 0.0
                            self.rejected_targets.pop(target_id, None)
                            if target_id not in self.monsters:
                                logging.info("队长目标 obj=%d 暂不在视野，先靠近队长", target_id)
                        logging.info(
                            "队长攻击目标 target=%d，准备协助 raw=%s",
                            target_id,
                            body.hex(),
                        )
                    elif source_id == self.leader_obj_id and target_id in (0, 0xFFFFFFFF):
                        logging.debug("忽略队长广播中的空目标 raw=%s", body.hex())
                    if source_id == self.obj_id and target_id == self.last_locked_id:
                        if not self.attack_confirmed:
                            logging.info("服务端确认进入战斗 target=%d", target_id)
                        self.attack_confirmed = True
                elif packet_id == 0x0297 and len(body) == 4:
                    logging.warning(
                        "服务端拒绝攻击 code=%d target=%d formal_follow=%s",
                        struct.unpack_from("<i", body, 0)[0],
                        self.last_locked_id,
                        self.formal_team_follow,
                    )
                elif packet_id == 0x02FD and len(body) >= 6:
                    message_size = struct.unpack_from("<H", body, 4)[0]
                    if 6 + message_size <= len(body):
                        message = body[6:6 + message_size].decode("gb18030", "replace")
                        if message.startswith("JRH1:"):
                            self.handle_hunt_message(message)
                        elif self.handle_maintenance_message(message):
                            continue
                        if self.script_teleport_pending:
                            logging.warning("服务端脚本传送反馈：%s", message)
                            if message == "目标服务器无法响应":
                                if not await self.request_portable_teleport(writer):
                                    self.script_teleport_pending = False
                                    if self.script_teleport_watchdog_task:
                                        self.script_teleport_watchdog_task.cancel()
                                        self.script_teleport_watchdog_task = None
                elif packet_id == 0x0094 and len(body) >= 5:
                    text_size = struct.unpack_from("<I", body, 1)[0]
                    if body[0] == 8 and 0 < text_size <= 1024 and 5 + text_size <= len(body):
                        message = body[5:5 + text_size].decode("gb18030", "replace")
                        if message.startswith("JRH1:"):
                            self.handle_hunt_message(message)
                        else:
                            self.handle_maintenance_message(message)
                elif packet_id == 0x0316 and len(body) >= 17:
                    moving_id = struct.unpack_from("<I", body, 0)[0]
                    if moving_id == self.obj_id:
                        move_x, move_y = struct.unpack_from("<ff", body, 9)
                        if valid_position(move_x, move_y):
                            self.x, self.y = move_x, move_y
                            logging.info("移动到达 pos=(%.2f, %.2f)", self.x, self.y)
                            if self.hunt_move_event is not None:
                                self.hunt_move_event.set()
                        else:
                            logging.warning("忽略异常到达坐标 obj=%d body=%s", moving_id, body.hex())
                elif packet_id == 0x0056 and len(body) >= 22:
                    moving_id = struct.unpack_from("<I", body, 0)[0]
                    destination = move_destination(body)
                    if destination is None:
                        logging.warning(
                            "忽略异常移动路径 obj=%d size=%d body=%s",
                            moving_id, len(body), body[:48].hex(),
                        )
                    elif moving_id == self.obj_id:
                        if self.hunt_move_event is None:
                            self.x, self.y = destination
                            logging.info("移动更新 pos=(%.2f, %.2f)", self.x, self.y)
                    elif moving_id == self.leader_obj_id:
                        self.leader_x, self.leader_y = destination
                elif packet_id == 0x0021 and len(body) >= 16:
                    player_id = struct.unpack_from("<I", body, 0)[0]
                    player_x, player_y = struct.unpack_from("<ff", body, 8)
                    if valid_position(player_x, player_y):
                        self.players[player_id] = (player_x, player_y)
                        if player_id == self.leader_obj_id:
                            self.leader_x, self.leader_y = player_x, player_y
                            self.mark_leader_present()
                elif packet_id == 0x005A and len(body) >= 20:
                    self.record_hunt_member_location(body,position_packet=True)
                    member_guid = struct.unpack_from("<I", body, 0)[0]
                    if member_guid == self.leader_guid and (
                        self.leader_scene_id is None
                        or self.leader_scene_id == self.scene_id
                    ):
                        team_x, team_y = struct.unpack_from("<ff", body, 12)
                        if valid_position(team_x, team_y):
                            self.leader_x, self.leader_y = team_x, team_y
                elif packet_id == 0x01C8 and self.auto_team:
                    await self.accept_team_invite(writer, body)
                elif packet_id == 0x0284 and self.auto_team:
                    await self.accept_team_application(writer, body)
                elif packet_id == 0x0234 and len(body) >= 11:
                    member_count = body[10]
                    if member_count < self.hunt_member_count:
                        self.hunt_confirmed_member_guids.clear()
                        self.team_member_guids.clear()
                        getattr(self,"hunt_member_locations",{}).clear()
                        getattr(self,"hunt_member_stationary",{}).clear()
                    self.hunt_member_count = member_count
                    if member_count and len(body) >= 27:
                        # Customized GCTeamList header is 15 bytes. The first
                        # TEAM_LIST_ENTRY is the leader; GUID is entry +0 and
                        # ObjID is entry +8.
                        self.in_team = True
                        leader_guid = struct.unpack_from("<I", body, 15)[0]
                        if leader_guid != self.leader_guid:
                            self.leader_scene_id = None
                            self.leader_transit_scene_id = None
                            self.leader_obj_id = 0
                        self.leader_guid = leader_guid
                        self.team_member_guids.add(leader_guid)
                        listed_obj_id = struct.unpack_from("<I", body, 23)[0]
                        if listed_obj_id not in (0, 0xFFFFFFFF):
                            self.leader_obj_id = listed_obj_id
                        known_pos = self.players.get(self.leader_obj_id)
                        if known_pos:
                            self.leader_x, self.leader_y = known_pos
                            self.mark_leader_present()
                        logging.info(
                            "已识别队长 guid=%d obj=%d members=%d",
                            self.leader_guid,
                            self.leader_obj_id,
                            member_count,
                        )
                    elif member_count:
                        logging.warning("队伍列表过短 size=%d members=%d", len(body), member_count)
                    elif self.in_team or self.leader_obj_id:
                        self.in_team = False
                        self.hunt_member_count = 0
                        self.hunt_confirmed_member_guids.clear()
                        self.team_member_guids.clear()
                        getattr(self,"hunt_member_locations",{}).clear()
                        getattr(self,"hunt_member_stationary",{}).clear()
                        self.leader_obj_id = 0
                        self.leader_guid = 0
                        self.leader_scene_id = None
                        self.leader_transit_scene_id = None
                        self.leader_target_id = 0
                        self.formal_team_follow = False
                        self.mark_leader_present()
                        logging.info("已离开队伍或队伍已解散")
                elif packet_id == 0x00B3 and self.auto_team:
                    await self.accept_team_follow(writer)
                elif packet_id == 0x011F and len(body) >= 14:
                    await self.enter_changed_scene(writer, body)
                elif packet_id == 0x0203 and len(body) >= 1:
                    change_result = body[0]
                    logging.info(
                        "队员场景迁移结果 result=%d body=%s",
                        change_result,
                        body.hex(),
                    )
                    if (
                        change_result == 0
                        and self.scene_change_pending
                        and self.pending_scene_body is not None
                    ):
                        await self.enter_changed_scene(writer, self.pending_scene_body)
                    elif change_result != 0 and self.scene_change_pending:
                        self.scene_change_pending = False
                        self.pending_scene_body = None
                        logging.warning(
                            "通用跟随迁移被服务端拒绝 result=%d，已解除等待状态",
                            change_result,
                        )
                        self.mark_leader_missing(writer)
                elif packet_id == 0x015A and len(body) >= 5:
                    follow_result = body[0]
                    member_guid = struct.unpack_from("<I", body, 1)[0]
                    was_formal_follow = self.formal_team_follow
                    result_names = {
                        0: "拒绝跟随",
                        1: "进入跟随",
                        2: "退出跟随",
                        3: "跟随标记",
                    }
                    self.formal_team_follow = follow_result in (1, 3)
                    if self.hunt_role=="leader" and follow_result in (0,2):
                        self.hunt_follow_dirty=True
                    if follow_result in (0, 2):
                        self.team_follow_requested = False
                    if follow_result == 2 and was_formal_follow:
                        self.follow_transition_until = time.monotonic() + 3.0
                    logging.info(
                        "组队跟随结果 result=%d(%s) guid=%d formal=%s",
                        follow_result,
                        result_names.get(follow_result, "未知"),
                        member_guid,
                        self.formal_team_follow,
                    )
                elif packet_id == 0x0223 and len(body) >= 5:
                    follow_leader = struct.unpack_from("<I", body, 0)[0]
                    follow_count = body[4]
                    # This customized engine may serialize GUID as 4 or 8 bytes;
                    # the list itself is sufficient proof that formal follow is active.
                    expected_4 = 5 + follow_count * 4
                    expected_8 = 5 + follow_count * 8
                    valid_list = len(body) in (expected_4, expected_8)
                    self.formal_team_follow = valid_list and follow_count > 0
                    self.team_follow_requested = self.formal_team_follow
                    if follow_leader:
                        self.leader_obj_id = follow_leader
                    logging.info(
                        "收到正式跟随名单 leader=%d members=%d size=%d formal=%s",
                        follow_leader,
                        follow_count,
                        len(body),
                        self.formal_team_follow,
                    )
                elif packet_id == 0x0238 and len(body) == 7:
                    player_id = struct.unpack_from("<I", body, 0)[0]
                    have_team, is_leader, _ = body[4:7]
                    if player_id == self.obj_id:
                        self.in_team = bool(have_team)
                        if not self.in_team:
                            self.hunt_member_count = 0
                            self.hunt_confirmed_member_guids.clear()
                            self.team_member_guids.clear()
                            getattr(self,"hunt_member_locations",{}).clear()
                            getattr(self,"hunt_member_stationary",{}).clear()
                            self.leader_obj_id = 0
                            self.leader_guid = 0
                            self.leader_scene_id = None
                            self.leader_transit_scene_id = None
                            self.leader_target_id = 0
                            self.team_follow_requested = False
                            self.formal_team_follow = False
                            self.mark_leader_present()
                    elif have_team and is_leader:
                        self.in_team = True
                        if self.leader_obj_id != player_id:
                            self.leader_guid = 0
                            self.leader_scene_id = None
                            self.leader_transit_scene_id = None
                        self.leader_obj_id = player_id
                        logging.info("已识别队长 obj=%d", player_id)
                elif packet_id == 0x0319:
                    logging.info("队伍结果 type=%d size=%d body=%s", body[0] if body else -1, len(body), body.hex())
                    if self.hunt_role == "leader" and len(body) >= 15 and body[0] == 0:
                        joined_guid = struct.unpack_from("<I", body, 1)[0]
                        team_id = struct.unpack_from("<H", body, 5)[0]
                        if joined_guid not in (0, 0xFFFFFFFF) and team_id != 0xFFFF:
                            self.team_member_guids.add(joined_guid)
                            self.hunt_follow_dirty=True
                            self.hunt_follow_reinvite_signature = None
                            logging.info("服务器确认队伍成员入队 guid=%d team=%d", joined_guid, team_id)
                            if joined_guid in {item[2] for item in self.hunt_members}:
                                self.hunt_confirmed_member_guids.add(joined_guid)
                                logging.info("服务器确认刷马队员入队 guid=%d", joined_guid)
                    if self.hunt_role=="leader":
                        self.record_hunt_member_location(body)
                        if len(body)==24 and body[0] in (1,7):
                            guid=struct.unpack_from("<I",body,1)[0]
                            getattr(self,"hunt_member_locations",{}).pop(guid,None)
                            getattr(self,"hunt_member_stationary",{}).pop(guid,None)
                            if body[0]==1 and guid in self.team_member_guids:
                                self.team_member_guids.discard(guid)
                                self.hunt_member_count=max(len(self.team_member_guids),self.hunt_member_count-1)
                                self.hunt_follow_dirty=True
                    await self.handle_team_scene_update(writer, body)
                else:
                    logging.info(
                        "场景包 id=0x%04x size=%d head=%s",
                        packet_id,
                        len(body),
                        body[:48].hex(),
                    )
        finally:
            if leader_state_task:
                leader_state_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await leader_state_task
            if self.script_teleport_watchdog_task:
                self.script_teleport_watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.script_teleport_watchdog_task
            if self.heal_task:
                self.heal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.heal_task
            if self.death_recovery_task:
                self.death_recovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.death_recovery_task
            if self.equip_task:
                self.equip_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.equip_task
            if self.combat_task:
                self.combat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.combat_task
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if shout_task:
                shout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shout_task
            if self.repair_task:
                self.repair_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.repair_task
                self.repair_task = None
            if self.return_task:
                self.return_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.return_task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def run(self) -> None:
        result = None
        for attempt in range(1, 4):
            try:
                result = await asyncio.wait_for(self.login(), timeout=45.0)
                break
            except (TimeoutError, ConnectionError, asyncio.IncompleteReadError) as exc:
                logging.warning(
                    "登录未完成 attempt=%d/3 stage=%s error=%s",
                    attempt, getattr(self, "login_stage", "unknown"), type(exc).__name__,
                )
                if attempt < 3:
                    logging.info("等待服务器清理旧连接，8秒后重新登录")
                    await asyncio.sleep(8.0)
            except RuntimeError as exc:
                logging.error("登录失败：%s", exc)
                return
        if result is None:
            logging.error("登录重试已用尽，已停止；请检查登录服与账号在线状态")
            return
        try:
            await self.game(result)
        except asyncio.IncompleteReadError as exc:
            logging.error(
                "游戏连接已关闭 map=%d target_map=%s pending_script=%s received=%d expected=%d",
                self.scene_id,
                self.leader_scene_id,
                self.script_teleport_pending,
                len(exc.partial),
                exc.expected,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="TLBB 简易独立机器人")
    parser.add_argument("--account", required=True)
    parser.add_argument("--auth-profile", default="local_auth", help="本地认证适配模块名")
    parser.add_argument("--log", type=Path, default=Path("simple-bot.log"))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--auto-fight", action="store_true", help="自动锁定、靠近并普通攻击")
    parser.add_argument(
        "--fight-all-scenes",
        action="store_true",
        help="允许在主城等非野外地图移动和打怪（默认仅野外）",
    )
    parser.add_argument("--no-team", action="store_true", help="关闭自动同意组队和跟随队长")
    parser.add_argument("--create-role", help="账号无角色时由服务端创建指定名字的角色")
    parser.add_argument("--create-sex", type=int, choices=(0, 1), default=0)
    parser.add_argument("--heal-slot", type=int)
    parser.add_argument("--heal-guid", type=int)
    parser.add_argument("--heal-world", type=int, default=10)
    parser.add_argument("--login-host", default="127.0.0.1")
    parser.add_argument("--login-port", type=int, default=6500)
    parser.add_argument("--game-host", default="")
    parser.add_argument("--game-port", type=int)
    parser.add_argument("--skip-auto-equip", action="store_true")
    parser.add_argument("--combat-skill", type=int, default=281)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        handlers=([HourWindowFileHandler(args.log)]
                  + ([] if getattr(sys, "frozen", False) else [logging.StreamHandler()])),
    )
    asyncio.run(
        SimpleBot(
            args.account,
            args.auth_profile,
            args.auto_fight,
            auto_team=not args.no_team,
            outdoor_only=not args.fight_all_scenes,
            create_role_name=args.create_role,
            create_role_sex=args.create_sex,
            heal_slot=args.heal_slot,
            heal_guid=args.heal_guid,
            heal_world=args.heal_world,
            login_host=args.login_host,
            login_port=args.login_port,
            game_host_override=args.game_host or None,
            game_port_override=args.game_port,
            auto_equip_weapon=not args.skip_auto_equip,
            combat_skill_id=args.combat_skill,
        ).run()
    )


if __name__ == "__main__":
    main()
