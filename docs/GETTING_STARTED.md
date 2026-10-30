# 本地开发

1. Windows 上创建虚拟环境，安装根目录 requirements.txt；不随仓库分发第三方 vendor 目录。
2. 执行 `python tools/init_local.py`。只创建空本地配置，不覆盖现有文件，不生成账号或角色。
3. 执行 `python web_console.py`，在本机查看页面。数据库预览需填写自己的测试库连接。
4. 执行 `python -m unittest discover -s tests -v` 和 `python tools/check_release.py`。

## 配置

本地 `server_config.json` 默认连接回环地址，数据库用户名与密码为空。文件被 .gitignore 排除。请使用测试专用数据库用户，按功能授予权限，避免沿用系统 root 凭据。

- `enable_database_writes`: 默认 false。允许开始对接数据库写入，但不会解除装备模块的独立阻断，也不会替你验证表结构。
- `enable_server_provisioning`: 默认 false。预留服务端管理功能开关，打开后仍需实现白名单接口。
- `supported_login_hosts`: 默认空列表。仅在完成版本验证后，在本地文件中填写适配服务器地址；它替代代码中绑定具体部署地址的判断，不是兼容性检测。更换服务器时网页会清空旧服务器的许可开关，重新编辑本地配置后重启本地控制中心。

认证示例在 `examples/local_auth.py.example`，复制为本地 `local_auth.py` 后实现，不能使用空口令包或缓存会话冒充认证。直接调试引擎：

```powershell
python simple_bot.py --account YOUR_TEST_ACCOUNT --login-host YOUR_TEST_HOST --auth-profile local_auth --skip-auto-equip
```

此命令实际连接游戏服务，只能在独立测试环境运行。先验证正确/错误密码，再验证角色列表与进场。公开副本默认没有可用认证实现。

## 需要自备的数据

地图路径规划使用 `hunt_navigation/manifest.json` 和配套地图数据；`scene_routes.json`、`world_walk_routes.json`、`portable_entries.json`、`role_progression_templates.json` 及装备定义也依赖服务端版本。仓库不附带从游戏资源中导出的数据。

部分页面上会显示历史实现支持的功能或编号，不能据此判断目标服已经具备该能力。空模板只用于本地启动与接口研究，不具备实际赶路或刷马的数据。

## 当前平台边界

网页控制中心依赖 Windows `kernel32`，刷马补刷协调依赖 `msvcrt`。不承诺直接在 Linux 启动控制中心。Lua 在目标 Linux 服务端执行，Python 控制中心与游戏服务端可以位于不同机器。
