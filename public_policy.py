"""Conservative defaults for the public source preview."""
from flask import jsonify, request

# Database reads remain reads. Runtime startup can also write starter items.
WRITE_ENDPOINTS = frozenset({'api_batch_create_bots', 'api_start_bot', 'api_start_hunt_plan'})


def install_policy(app, config_provider):
    @app.before_request
    def public_preview_policy():
        config = config_provider()
        is_write = request.endpoint in WRITE_ENDPOINTS
        path_start = request.method == 'POST' and request.path.startswith('/api/') and request.path.endswith('/start')
        if (is_write or path_start) and config.get('enable_database_writes') is not True:
            return jsonify(ok=False, message='公开预览版默认关闭数据库写入和游戏启动，请先阅读 docs/ADAPTING.md 完成适配'), 409
        # No SSH or server file changes are silently authorized by a DB toggle.
        if request.method in ('POST', 'PUT', 'PATCH') and 'shout' in request.path:
            payload = request.get_json(silent=True) or {}
            if payload.get('enabled') and payload.get('channel') == 'horn' and config.get('enable_server_provisioning') is not True:
                return jsonify(ok=False, message='小喇叭须先实现服务端白名单适配'), 409
