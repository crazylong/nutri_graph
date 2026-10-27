"""
nutri_graph —— FastAPI 入口（只做装配，不放业务逻辑）

职责边界：
  这里只做三件事 —— 配置日志、挂 router、定义 lifespan。
  具体端点都在 api/ 下，一个域一个文件：
    api/auth.py     手机号验证码登录 / 令牌 / 注销
    api/admin.py    管理员（概览、候选审核），整个 router 挂 require_admin
    api/chat.py     主问答
    api/health.py   存储探活
    api/system.py   运行时状态（模型池）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import config
from api.admin import router as admin_router
from api.auth import router as auth_router
from api.chat import router as chat_router
from api.health import router as health_router
from api.kg import router as kg_router
from api.system import router as system_router

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup_report()
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan)

for _router in (auth_router, admin_router, chat_router, kg_router, health_router, system_router):
    app.include_router(_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
