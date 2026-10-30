# 系统结构与阅读入口

```text
templates/index.html → web_console.py → 本地假人子进程 simple_bot.py
                                            ├─ protocol_auth.py → 本地版本认证模块
                                            ├─ 游戏协议 → 登录/场景服务
                                            ├─ hunt_teams.py → 队伍计划
                                            ├─ hunt_world_walk.py → 地图连接路线
                                            ├─ hunt_navigation.py → 同地图路径规划
                                            ├─ leader_shout.py → 每位队长的喊话配置
                                            └─ 游戏脚本调用 → server/Jiaren.lua
```

`simple_bot.py` 是当前行为主入口，`login()` 负责登录选角，`game()` 负责场景消息；`hunt_loop()` 与 `combat_loop()` 推进刷马与战斗，`ensure_hunt_travel_ready()` 处理赶路准备。先按函数和消息编号定位，不需要通读全部文件。

`hunt_teams.py` 校验一队一队长、账号不能跨队占用，负责原子保存。`managed_leader_state.py` 负责受管队长的位置状态文件。`bot_log_retention.py` 提供日志时间窗口。

`process_scope.py` 按实际可执行路径和 Python 脚本绝对路径识别本目录进程，避免接管同机其他目录的假人。`version_profile.py` 从私有本地配置读取已适配地址名单，不内置部署地址。

`web_console.py` 包含版本相关的数据库访问。`public_policy.py` 默认拦截会创建角色或启动游戏维护的入口；这不是通用数据库安全层。`offline_durability.py` 和 `equipment_templates.py` 的关键入口还保留独立阻断，不能仅靠打开网页开关绕过版本验证。

位置查询和小喇叭远程白名单部署以明确报错的接口占位，未附带服务器内存读取器或自动远程部署器。调用方的异常分支需在目标版本中实测，不能把占位报错解释为正常位置或成功发送。

维护状态时应分清：队伍成员、组队跟随、骑乘、战斗、切图和寻路，不能用单个“正在运行”标志替代。赶路补跟随不应覆盖战斗；位置失效不能认定地图为空；目标死亡应及时清理旧攻击状态。
