"""
llm/pool.py —— 模型池：成员、可用性状态、错误归因

这一层只回答三个问题，不做任何网络调用：
  1. 池子里有哪几个大语言模型（ModelSpec，成员来自 .env 的 LLM_MODEL_POOL）
  2. 现在该轮到谁（ModelRouter.candidates 的顺序）
  3. 某个模型为什么不能用、什么时候能再用（FailureKind → ModelStatus + cooldown）

刻意不记「还剩多少额度、什么时候到期」：百炼会不定期送额度，这类外部状态写进
配置必然过期。某模型还有没有额度，只由**真实返回的错误**判定：

  - 额度用尽（403 AllocationQuota / insufficient_quota）→ **移出池**（REMOVED），
    不再重试、也不按时间回归，只能靠 `--reset` 找回
  - 其它错误（限流 / 5xx / 模型不存在）→ 进**冷却队列**，到点自动回候选
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

import config

log = logging.getLogger("llm.pool")


# ============================================================
# 错误归因：把各家的异常收敛成 6 种可决策的原因
# ============================================================
class FailureKind(str, Enum):
    """一次调用失败的性质，决定「换模型」还是「直接抛」。"""

    QUOTA = "quota"                # 额度用尽 → 换模型，冷却后自动回来试
    RATE_LIMIT = "rate_limit"      # 触发限流 → 换模型，短冷却后可用
    MODEL_INVALID = "model_invalid"  # 模型不存在/无权限 → 换模型，长冷却
    TRANSIENT = "transient"        # 5xx / 超时 / 连接断 → 换模型，短冷却
    AUTH = "auth"                  # API key 失效 → 换模型没用，直接抛
    CLIENT = "client"              # 请求本身有问题（400/422）→ 换模型没用，直接抛

    @property
    def switchable(self) -> bool:
        """换下一个模型是否可能成功。"""
        return self not in (FailureKind.AUTH, FailureKind.CLIENT)


_QUOTA_RE = re.compile(
    r"insufficient_quota|allocated\s*quota|allocationquota|free_?tier"
    r"|quota\s*(has\s*been\s*)?(exceeded|exhausted|used\s*up)"
    r"|exceeded\s+your\s+current\s+quota|arrearage|overdue"
    r"|额度|余额不足|配额",
    re.I,
)
_RATE_RE = re.compile(r"rate\s*limit|throttl|too\s*many\s*requests|concurrency", re.I)
_MODEL_RE = re.compile(
    r"model_not_found|model\s+not\s+found|does\s+not\s+exist\s+or\s+you\s+do\s+not\s+have\s+access"
    r"|unknown\s+model|unsupported\s+model|no\s+permission\s+to\s+(use|access)",
    re.I,
)
_AUTH_RE = re.compile(
    r"invalid_api_key|invalid\s+api\s*key|incorrect\s+api\s*key|unauthorized|authentication",
    re.I,
)
_TIMEOUT_TYPES = {
    "APITimeoutError",
    "APIConnectionError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "TimeoutException",
    "TimeoutError",
}


def extract_error_fields(exc: BaseException) -> tuple[int | None, str, str]:
    """从 openai / httpx / 普通异常里抠出 (status_code, code, message)，各家写法都要兜住。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)

    code, message = "", ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            message = str(err.get("message") or "")
    code = code or str(getattr(exc, "code", "") or "")
    message = message or str(getattr(exc, "message", "") or "") or str(exc)
    return (int(status) if status else None), code, message


def classify_error(exc: BaseException) -> tuple[FailureKind, str]:
    """把异常归因成 FailureKind + 一句人话原因。

    判定顺序很重要：DashScope 的「免费额度用尽」是 403 + AllocationQuota.FreeTierOnly，
    如果先判鉴权就会把它误判成 key 失效，从而整池停摆。
    """
    status, code, message = extract_error_fields(exc)
    hay = f"{code} {message}"
    typename = type(exc).__name__
    reason = f"{status or '-'} {code or typename}: {message[:160]}"

    if status and status >= 500:
        return FailureKind.TRANSIENT, reason
    if _QUOTA_RE.search(hay):
        return FailureKind.QUOTA, reason
    if _MODEL_RE.search(hay) or (status == 404 and not _AUTH_RE.search(hay)):
        return FailureKind.MODEL_INVALID, reason
    if _AUTH_RE.search(hay) and status in (401, 403):
        return FailureKind.AUTH, reason
    if status == 429 or _RATE_RE.search(hay):
        return FailureKind.RATE_LIMIT, reason
    if status in (400, 422):
        return FailureKind.CLIENT, reason
    if typename in _TIMEOUT_TYPES or isinstance(exc, TimeoutError):
        return FailureKind.TRANSIENT, reason
    if status is None:
        # 没拿到状态码，通常是网络层断掉
        return FailureKind.TRANSIENT, reason
    return FailureKind.CLIENT, reason


# ============================================================
# 模型规格
# ============================================================
@dataclass(frozen=True)
class ModelSpec:
    """池中一个模型的「身份」。

    只有名字：额度、到期时间属于控制台状态，随时会变（百炼不定期送），不写进配置。
    """

    name: str

    @classmethod
    def parse(cls, raw: str) -> "ModelSpec":
        return cls(name=str(raw).strip())

    def to_dict(self) -> dict:
        return {"name": self.name}


# ============================================================
# 运行时状态
# ============================================================
class ModelStatus(str, Enum):
    OK = "ok"                # 可用
    COOLING = "cooling"      # 限流/抖动，冷却后可再用
    INVALID = "invalid"      # 模型不存在/无权限，冷却后可再试
    REMOVED = "removed"      # 额度用尽，已移出 LLM_MODEL_POOL（不自动回归）


@dataclass
class ModelState:
    name: str
    status: ModelStatus = ModelStatus.OK
    cooldown_until: float = 0.0
    reason: str = ""
    failures: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_used: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def is_up(self, now: float) -> bool:
        if self.status is ModelStatus.REMOVED:
            return False  # 额度用尽：已移出池，不靠时间恢复
        return self.status == ModelStatus.OK or (
            self.cooldown_until > 0 and now >= self.cooldown_until
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "cooldown_until": self.cooldown_until,
            "reason": self.reason,
            "failures": self.failures,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ModelState":
        try:
            status = ModelStatus(d.get("status", "ok"))
        except ValueError:
            status = ModelStatus.OK
        return cls(
            name=name,
            status=status,
            cooldown_until=float(d.get("cooldown_until") or 0.0),
            reason=str(d.get("reason") or ""),
            failures=int(d.get("failures") or 0),
            calls=int(d.get("calls") or 0),
            prompt_tokens=int(d.get("prompt_tokens") or 0),
            completion_tokens=int(d.get("completion_tokens") or 0),
            last_used=float(d.get("last_used") or 0.0),
        )


# ============================================================
# 路由
# ============================================================
class ModelRouter:
    """负责「谁可用」以及状态持久化，线程安全。

    选择策略：
      1. 优先沿用上一次成功的模型（current）——避免同一进程内模型来回跳，
         换来换去会让输出风格和 JSON 稳定性抖动
      2. current 不可用时，按 .env 里 LLM_MODEL_POOL 的顺序取下一个可用的
      3. 冷却结束的模型自动回到候选（额度可能是百炼刚补的）
    """

    STATE_VERSION = 1

    def __init__(self, specs: Iterable[ModelSpec], state_path: str | os.PathLike | None = None):
        self.specs: list[ModelSpec] = list(specs)
        self._path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self._states: dict[str, ModelState] = {s.name: ModelState(name=s.name) for s in self.specs}
        self._current: str | None = None
        self._load()

    # ---------- 选择 ----------
    def _refresh(self, now: float) -> None:
        """冷却到期的模型自动恢复可用。

        注意额度用尽（REMOVED）不在恢复之列：那是「移出池」，只能靠 --reset 找回。
        """
        for spec in self.specs:
            st = self._states[spec.name]
            if st.status is ModelStatus.REMOVED:
                continue
            if st.status is not ModelStatus.OK and st.cooldown_until and now >= st.cooldown_until:
                log.info("模型 %s 冷却结束（原状态 %s），重新纳入候选", spec.name, st.status.value)
                if st.status is ModelStatus.INVALID and self._current:
                    log.info("模型恢复可用，回到池内优先级顺序（原当前 %s）", self._current)
                    self._current = None
                st.status = ModelStatus.OK
                st.cooldown_until = 0.0
                st.reason = ""
                st.failures = 0

    def candidates(self) -> list[ModelSpec]:
        """当前可尝试的模型，按尝试顺序排列。"""
        with self._lock:
            now = time.time()
            self._refresh(now)
            avail = [s for s in self.specs if self._states[s.name].is_up(now)]
            if self._current:
                cur = [s for s in avail if s.name == self._current]
                if cur:
                    avail = cur + [s for s in avail if s.name != self._current]
            return avail

    def next_spec(self) -> ModelSpec | None:
        cands = self.candidates()
        return cands[0] if cands else None

    def spec(self, name: str) -> ModelSpec | None:
        return next((s for s in self.specs if s.name == name), None)

    # ---------- 记账 ----------
    def mark_success(self, spec: ModelSpec, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        with self._lock:
            st = self._states[spec.name]
            st.status = ModelStatus.OK
            st.cooldown_until = 0.0
            st.reason = ""
            st.failures = 0
            st.calls += 1
            st.prompt_tokens += max(prompt_tokens, 0)
            st.completion_tokens += max(completion_tokens, 0)
            st.last_used = time.time()
            self._current = spec.name
            self._save()

    def mark_failure(self, spec: ModelSpec, kind: FailureKind, reason: str) -> None:
        with self._lock:
            now = time.time()
            st = self._states[spec.name]
            st.failures += 1
            st.reason = f"[{kind.value}] {reason}"

            if kind is FailureKind.QUOTA:
                # 额度用尽 = 从池里移除，不再重试、也不按时间回归；
                # 百炼补了额度后跑 `python -m scripts.check_llm_pool --reset` 让它回池
                st.status = ModelStatus.REMOVED
                st.cooldown_until = 0.0
                log.error("模型 %s 额度用尽，已移出 LLM_MODEL_POOL（--reset 可找回）", spec.name)
            elif kind is FailureKind.MODEL_INVALID:
                st.status = ModelStatus.INVALID
                st.cooldown_until = now + config.LLM_INVALID_COOLDOWN_H * 3600
            elif kind is FailureKind.RATE_LIMIT:
                st.status = ModelStatus.COOLING
                st.cooldown_until = now + config.LLM_RATE_LIMIT_COOLDOWN_S
            elif kind is FailureKind.TRANSIENT:
                st.status = ModelStatus.COOLING
                # 连续抖动时指数退避，最多 10 分钟
                backoff = min(config.LLM_RATE_LIMIT_COOLDOWN_S * (2 ** (st.failures - 1)), 600)
                st.cooldown_until = now + backoff
            # AUTH / CLIENT 不进冷却：换模型也没用，交给调用方直接抛

            if self._current == spec.name:
                self._current = None
            st.last_used = now
            if kind is not FailureKind.QUOTA:
                log.warning(
                    "模型 %s 标记为 %s，冷却至 %s：%s",
                    spec.name,
                    st.status.value,
                    datetime.fromtimestamp(st.cooldown_until).strftime("%m-%d %H:%M")
                    if st.cooldown_until
                    else "-",
                    reason,
                )
            self._save()

    def active(self) -> list[ModelSpec]:
        """还在池里的模型（含冷却中的，不含已移除的）——用于观测和兜底判断。"""
        with self._lock:
            return [s for s in self.specs if self._states[s.name].status is not ModelStatus.REMOVED]

    def reset(self) -> None:
        with self._lock:
            self._states = {s.name: ModelState(name=s.name) for s in self.specs}
            self._current = None
            if self._path and self._path.exists():
                try:
                    self._path.unlink()
                except OSError as e:
                    log.warning("删除状态文件失败：%s", e)
            log.info("模型池状态已重置（被移除的模型全部回池）")

    # ---------- 观测 ----------
    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            self._refresh(now)
            models = []
            for spec in self.specs:
                st = self._states[spec.name]
                models.append(
                    {
                        **spec.to_dict(),
                        "status": st.status.value,
                        "available": st.is_up(now),
                        "removed": st.status is ModelStatus.REMOVED,
                        "reason": st.reason,
                        "cooldown_until": (
                            datetime.fromtimestamp(st.cooldown_until).isoformat(timespec="seconds")
                            if st.cooldown_until
                            else None
                        ),
                        "calls": st.calls,
                        "tokens": st.total_tokens,
                        "failures": st.failures,
                    }
                )
            return {
                "current": self._current,
                "queue": [s.name for s in self.candidates()],
                "models": models,
            }

    # ---------- 持久化 ----------
    def _save(self) -> None:
        if not self._path:
            return
        payload = {
            "version": self.STATE_VERSION,
            "current": self._current,
            "models": {n: st.to_dict() for n, st in self._states.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)  # 原子替换，避免半截文件
        except OSError as e:
            log.warning("模型池状态落盘失败（不影响本次调用）：%s", e)

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("模型池状态文件不可读，按全新状态启动：%s", e)
            return
        for name, d in (payload.get("models") or {}).items():
            if name in self._states and isinstance(d, dict):
                self._states[name] = ModelState.from_dict(name, d)
        cur = payload.get("current")
        if cur in self._states:
            self._current = cur
