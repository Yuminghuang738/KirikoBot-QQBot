"""Security-relevant behaviour: dashboard auth exemptions and output escaping."""
from __future__ import annotations

import logging
import os

import pytest

from conftest import APP_DIR

PROJECT_DIR = os.path.dirname(APP_DIR)


class TestAuthExemptions:
    """The healthcheck must not sit behind the dashboard password.

    Enabling dashboard auth made `curl -f /` return 401, which marked the
    container unhealthy — the compose healthcheck and the auth exemption have
    to stay in sync.
    """

    def test_healthz_is_exempt(self):
        import dashboard_auth

        assert "healthz" in dashboard_auth._EXEMPT_ENDPOINTS

    def test_static_files_are_exempt(self):
        """贴图和面板资源由浏览器同源加载，必须免鉴权。"""
        import dashboard_auth

        assert "static" in dashboard_auth._EXEMPT_ENDPOINTS

    def test_no_inbound_webhook_exemption_remains(self):
        """入站 webhook 已被出站 WebSocket 网关取代，不该再留豁免。

        这条是有意写成「不存在」的断言：豁免列表是鉴权的白名单，多一个词就是
        多一条免密通路。以前有个 `receive`（OneBot webhook 自带 HMAC 签名），
        现在没有那个路由了，留着只会让人以为还有个回调端点要保护。
        """
        import dashboard_auth

        assert "receive" not in dashboard_auth._EXEMPT_ENDPOINTS
        assert not hasattr(dashboard_auth, "_WEBHOOK_ENDPOINTS")

    def test_main_exposes_the_health_route(self):
        src = open(os.path.join(APP_DIR, "main.py"), encoding="utf-8").read()
        assert '@app.route("/healthz")' in src

    def test_main_has_no_webhook_route(self):
        """断言的是**路由装饰器**不存在，不是字符串不出现 ——
        main.py 里有意留了几处 tombstone 注释解释这条路为什么删了。"""
        src = open(os.path.join(APP_DIR, "main.py"), encoding="utf-8").read()
        for pattern in ('@app.route("/webhook"', "@app.route('/webhook'",
                        '@app.route("/webhook/', "@app.route('/webhook/"):
            assert pattern not in src, f"main.py 仍有 webhook 路由：{pattern}"

    def test_compose_healthcheck_hits_the_exempt_endpoint(self):
        compose = open(os.path.join(PROJECT_DIR, "docker-compose.yml"), encoding="utf-8").read()
        assert "/healthz" in compose
        assert "http://localhost:5000/healthz" in compose



class TestLogEscaping:
    """Log messages carry QQ nicknames and message bodies into the dashboard."""

    @pytest.mark.parametrize("level,name", [
        (logging.INFO, "robot_server"),
        (logging.WARNING, "main"),
        (logging.ERROR, "main"),
        (logging.INFO, "think"),
    ])
    def test_display_escapes_html(self, level, name):
        from log_stream import SSELogHandler

        handler = SSELogHandler()
        record = logging.LogRecord(
            name, level, __file__, 1,
            "user=<img src=x onerror=alert(1)>", None, None,
        )
        handler.emit(record)
        entry = handler.read()[0]

        assert "<img" not in entry["display"]
        assert "onerror=alert(1)>" not in entry["display"]
        assert "&lt;img" in entry["display"]
        # The raw message is still available for non-HTML consumers.
        assert "<img" in entry["msg"]
