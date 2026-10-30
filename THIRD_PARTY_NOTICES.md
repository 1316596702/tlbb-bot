# 外部依赖与内容边界

仓库不附带 vendor、DLL、EXE、客户端资源、服务端程序、抓包或数据库转储。Python 依赖由开发者自行安装；安装包中的原版权与许可证应保留。

| 依赖 | 用途 | 上游与许可核对入口 |
| --- | --- | --- |
| Flask | 本地网页与 HTTP API | https://github.com/pallets/flask |
| PyMySQL | 数据库驱动 | https://github.com/PyMySQL/PyMySQL |
| Paramiko | 版本相关维护代码的 SSH 依赖 | https://github.com/paramiko/paramiko |

上表不是将依赖重新许可为本项目许可证。制作二进制发行包时，按实际安装版本整理依赖和传递依赖的许可证，不以源码目录这份清单代替发行包审计。

LICENSE 文本取自 PolyForm Project 官方发布页面： https://polyformproject.org/licenses/noncommercial/1.0.0.txt 。非商业条款仅用于有权按此许可发布的项目内容。
