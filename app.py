"""
nutri_graph —— FastAPI 入口（只做装配，不放业务逻辑）

职责边界：
  这里只做三件事 —— 配置日志、挂 router、定义 lifespan。
  具体端点都在 api/ 下，一个域一个文件：
    api/auth.py     手机号验证码登录 / 令牌 / 注销
    api/admin.py    管理员（概览、候选审核），整个 router 挂 require_admin
    api/food_import.py  公开演示：Excel 导入食物（不鉴权，只进 demo_food）
    api/chat.py     主问答
    api/history.py  历史会话与最近问法
    api/health.py   存储探活
    api/system.py   运行时状态（模型池）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import config
import db
from api.admin import router as admin_router
from api.admin_import import router as admin_import_router
from api.auth import router as auth_router
from api.chat import router as chat_router
from api.food_import import router as food_import_router
from api.health import router as health_router
from api.history import router as history_router
from api.kg import router as kg_router
from api.system import router as system_router
from security.sms import describe_config
from utils.logging_config import configure_logging

# configure_logging 会清掉根 logger 上的旧 handler，不要再留 basicConfig。
configure_logging()
log = logging.getLogger("nutri_graph")


def _startup_report() -> None:
    """启动时把关键配置状态说清楚，避免「以为配了其实没配」。"""
    log.info(
        "鉴权：AUTH_REQUIRED=%s  短信通道=%s  验证码长度=%d 有效期=%ds",
        config.AUTH_REQUIRED,
        config.SMS_PROVIDER,
        config.SMS_CODE_LENGTH,
        config.SMS_VALID_TIME_S,
    )
    if not config.JWT_SECRET:
        log.warning("JWT_SECRET 未配置：进程重启后所有令牌失效（开发可接受，生产必须配置）")
    elif len(config.JWT_SECRET) < 32:
        log.warning(
            "JWT_SECRET 只有 %d 字节，HS256 建议 >=32 字节（RFC 7518 §3.2），否则签名可被暴力破解",
            len(config.JWT_SECRET),
        )
    if config.SMS_PROVIDER == "console" and not config.DEBUG:
        log.warning(
            "SMS_PROVIDER=console 但 DEBUG=false：不会真正发短信，验证码只出现在日志里"
        )
    elif config.SMS_PROVIDER == "dypns":
        # 真发短信前把「能不能发」说清楚：缺凭据/缺 SDK 时 /auth/sms 一律 503，
        # 与其让用户点了半天才发现，不如启动就把缺的东西列出来。
        ok, missing, sdk_ok = describe_config()
        if ok and sdk_ok:
            log.info(
                "短信通道 dypns 就绪：签名=%s 模板=%s 验证码 %d 位 / %d 秒",
                config.SMS_SIGN_NAME,
                config.SMS_TEMPLATE_CODE,
                config.SMS_CODE_LENGTH,
                config.SMS_VALID_TIME_S,
            )
        else:
            log.error(
                "短信通道 dypns 不可用：%s%s 登录将无法发送验证码（/auth/sms 返回 503）",
                ("缺少配置 " + ", ".join(missing) + "；") if missing else "",
                "" if sdk_ok else "缺少 SDK alibabacloud_dypnsapi20170525；",
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup_report()
    # 池是惰性建的（测试没有 lifespan），这里只是提前建好 + 探活，
    # 让"连不上库"在启动日志里就暴露，而不是等第一个请求。
    ok = db.ping()
    log.info(
        "连接池启动探活：%s（并发上限 %s / 池上限 %s）",
        "通过" if ok else "失败",
        config.UVICORN_LIMIT_CONCURRENCY,
        config.PG_POOL_SIZE + config.PG_MAX_OVERFLOW,
    )
    try:
        yield
    finally:
        db.dispose()


app = FastAPI(title=config.APP_NAME, lifespan=lifespan)


@app.exception_handler(db.PoolBusy)
async def _pool_busy_handler(_request, exc: db.PoolBusy):
    """池打满：明确告诉调用方"忙"，而不是悬挂到超时后 500。"""
    log.warning("数据库连接池打满：%s", exc)
    # 包一层 detail：全站其它错误都是 {"detail": {"code": ..., "message": ...}}，
    # 前端按同一个形状解析，不为这一类错误开特例。
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "pool_busy", "message": "系统繁忙，请稍后再试"}},
    )

for _router in (
    auth_router, admin_router, admin_import_router, chat_router, history_router,
    kg_router, food_import_router, health_router, system_router,
):
    app.include_router(_router)


if __name__ == "__main__":
    import uvicorn

    # limit_concurrency 必须显式带：config.UVICORN_LIMIT_CONCURRENCY 只被日志读，
    # 不带这个参数时同步端点会进 anyio 默认约 40 个线程，池只有 15 条，
    # 多出来的请求会在 pool_timeout 后 503。生产环境这个值由 systemd 的
    # ExecStart 提供（deploy/systemd/nutri-graph.service），这里是本地跑法。
    uvicorn.run(
        "app:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        limit_concurrency=config.UVICORN_LIMIT_CONCURRENCY,
    )
