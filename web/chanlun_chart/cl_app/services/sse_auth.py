"""SSE 端点鉴权：复用 Flask-Login 判定请求 cookie 是否为已登录会话。

SSE 端点是 Tornado 原生 handler(不在 Flask 请求上下文)，但 EventSource
same-origin 请求会自动带上 Flask 的会话 cookie(session / remember_token)。
这里用 test_request_context 临时进入 Flask 上下文 + preprocess_request 触发
Flask-Login 的 user_loader，从而等价于 @login_required 的判定。
"""
from flask_login import current_user


def is_request_authenticated(flask_app, cookie_header) -> bool:
    if not cookie_header:
        return False
    try:
        with flask_app.test_request_context(headers={"Cookie": cookie_header}):
            flask_app.preprocess_request()  # 触发 login_manager 从 cookie 恢复 user
            return bool(current_user.is_authenticated)
    except Exception:
        return False
