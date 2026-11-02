# -*- coding: utf-8 -*-
"""km_grader —— DeepSeek 智能评卷（简答题 / 填空题）

================================================================================
定位：给"简答题不自动判分"补一条可选路径。用户自评始终保留，AI 只是辅参，
最终以用户为准（前端可一键覆盖 AI 结论）。

安全约束（本模块最要紧的部分）：
  * API Key **绝不写日志、绝不进异常文本、绝不出现在返回值里**；
    所有对外错误信息都会把 key 替换成掩码。
  * 只用标准库 `urllib.request`，不引入第三方依赖 —— 避免框架已知缺陷 B1
    （声明 pip_dependencies 的插件在缺依赖机器上会被静默跳过加载）。
  * 只发送评卷必需的内容（题干/参考答案/考生作答/解析），不外发题库以外的信息。

可测性：HTTP 传输通过 `transport` 注入，单测不需要联网、不需要真实 key。

本模块 [不依赖 Flask / 框架]，可独立单测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30

#: 允许的最大题干/答案长度（防超长 prompt 烧 token）
MAX_PROMPT_FIELD = 4000

#: 评分到 verdict 的默认分界（模型没给 verdict 时用）
SCORE_CORRECT = 80
SCORE_PARTIAL = 50

VERDICT_VALUES = ("correct", "partial", "incorrect")

#: transport(url, headers, payload_dict, timeout) -> (http_status, response_text)
Transport = Callable[[str, Dict[str, str], Dict[str, Any], int], Tuple[int, str]]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class GraderError(Exception):
    """评卷错误基类。"""


class GraderNotConfigured(GraderError):
    """未配置 API Key / 未启用。"""


class GraderNetworkError(GraderError):
    """网络层失败（超时、DNS、连接被拒等）。"""


class GraderResponseError(GraderError):
    """接口返回不可用（非 200、结构不符、内容不可解析）。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def mask_key(key: str) -> str:
    """把 key 变成可安全展示的掩码：`sk-****1d71`。"""
    if not key:
        return ""
    text = str(key)
    if len(text) <= 8:
        return "*" * len(text)
    return text[:3] + "****" + text[-4:]


def redact(text: Any, key: str) -> str:
    """把文本里出现的 key 替换成掩码（错误信息外发前的最后一道闸）。"""
    out = str(text or "")
    if key and key in out:
        out = out.replace(key, mask_key(key))
    return out


@dataclass
class GradeResult:
    """评卷结果。"""

    score: Optional[int] = None
    verdict: str = "incorrect"
    comment: str = ""
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "comment": self.comment,
            "model": self.model,
            "usage": self.usage,
        }


# ---------------------------------------------------------------------------
# 默认传输：urllib（stdlib，免第三方依赖）
# ---------------------------------------------------------------------------
def urllib_transport(url: str, headers: Dict[str, str],
                     payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
    import urllib.error
    import urllib.request

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return int(exc.code), detail
    except Exception as exc:  # URLError / timeout / ssl ...
        raise GraderNetworkError(f"网络请求失败：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "你是严谨的医学考试阅卷老师。请依据参考答案给考生作答打分。\n"
    "评分要点：核心要点是否命中、表述是否准确、有无明显错误概念。\n"
    "只输出一个 JSON 对象，不要解释、不要代码块围栏，格式：\n"
    '{"score": 0-100 的整数, "verdict": "correct|partial|incorrect", '
    '"comment": "80 字以内中文点评，指出缺漏或肯定要点"}\n'
    "判定口径：score>=80 记 correct；50<=score<80 记 partial；否则 incorrect。"
)

_QTYPE_HINT = {
    "essay": "简答题（主观论述，按要点覆盖度给分）",
    "gap": "填空题（按空位逐个比对，意思对即可，注意同义表述）",
    "gap_filling": "填空题（按空位逐个比对，意思对即可，注意同义表述）",
}


def _clip(text: Any, limit: int = MAX_PROMPT_FIELD) -> str:
    out = str(text if text is not None else "").strip()
    if len(out) > limit:
        out = out[:limit] + "…（已截断）"
    return out


def build_messages(*, question: str, reference: str, user_answer: str,
                   qtype_label: str = "", analysis: str = "") -> list:
    hint = _QTYPE_HINT.get(qtype_label, "")
    parts = [f"题型：{qtype_label or '主观题'}" + (f"（{hint}）" if hint else "")]
    parts.append(f"题干：\n{_clip(question)}")
    parts.append(f"参考答案：\n{_clip(reference) or '（题库未提供参考答案）'}")
    if analysis:
        parts.append(f"参考解析：\n{_clip(analysis)}")
    parts.append(f"考生作答：\n{_clip(user_answer) or '（未作答）'}")
    parts.append("请按前述 JSON 格式输出评分结果。")
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    # 去掉 ```json ... ``` 围栏
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    for candidate in (cleaned,):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    match = _JSON_BLOCK_RE.search(cleaned)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _coerce_score(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(float(value)))))
    text = str(value).strip()
    match = re.search(r"-?\d+(\.\d+)?", text)
    if not match:
        return None
    try:
        return max(0, min(100, int(round(float(match.group(0))))))
    except ValueError:
        return None


def _verdict_from_score(score: Optional[int]) -> str:
    if score is None:
        return "incorrect"
    if score >= SCORE_CORRECT:
        return "correct"
    if score >= SCORE_PARTIAL:
        return "partial"
    return "incorrect"


# ---------------------------------------------------------------------------
# 评分器
# ---------------------------------------------------------------------------
class DeepSeekGrader:
    """DeepSeek 评卷客户端。"""

    def __init__(self, api_key: str, *,
                 base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL,
                 timeout: int = DEFAULT_TIMEOUT,
                 transport: Optional[Transport] = None) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        try:
            self.timeout = max(5, min(120, int(timeout)))
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self._transport = transport or urllib_transport

    # ---------------------------------------------------------------- 状态
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def masked_key(self) -> str:
        return mask_key(self.api_key)

    # ---------------------------------------------------------------- 评卷
    def grade(self, *, question: str, reference: str, user_answer: str,
              qtype_label: str = "", analysis: str = "") -> GradeResult:
        """给一份主观作答评分。

        :raises GraderNotConfigured: 未配置 key
        :raises GraderNetworkError: 网络失败
        :raises GraderResponseError: 接口/内容不可用（错误信息已脱敏）
        """
        if not self.configured:
            raise GraderNotConfigured("未配置 DeepSeek API Key")

        payload = {
            "model": self.model,
            "messages": build_messages(
                question=question, reference=reference, user_answer=user_answer,
                qtype_label=qtype_label, analysis=analysis),
            "temperature": 0.1,
            "max_tokens": 600,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}/chat/completions"

        try:
            status, body = self._transport(url, headers, payload, self.timeout)
        except GraderError as exc:
            raise GraderNetworkError(redact(str(exc), self.api_key)) from None
        except Exception as exc:  # noqa: BLE001
            raise GraderNetworkError(
                redact(f"评卷请求失败：{type(exc).__name__}: {exc}", self.api_key)) from None

        if status != 200:
            raise GraderResponseError(
                redact(f"DeepSeek 接口返回 HTTP {status}：{str(body)[:300]}", self.api_key))

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise GraderResponseError("DeepSeek 返回的不是合法 JSON") from None

        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise GraderResponseError("DeepSeek 返回缺少 choices")
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content") or ""
        if not str(content).strip():
            raise GraderResponseError("DeepSeek 返回内容为空")

        parsed = _extract_json(str(content))
        usage = data.get("usage") or {}
        model_name = str(data.get("model") or self.model)

        if parsed is None:
            # 模型没按 JSON 输出：保留原文作为点评，分数留空由调用方决定
            return GradeResult(
                score=None, verdict="incorrect",
                comment=_clip(content, 400), model=model_name,
                usage=usage if isinstance(usage, dict) else {},
                raw_text=str(content)[:2000],
            )

        score = _coerce_score(parsed.get("score"))
        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in VERDICT_VALUES:
            verdict = _verdict_from_score(score)
        comment = _clip(parsed.get("comment") or "", 500)
        return GradeResult(
            score=score, verdict=verdict, comment=comment, model=model_name,
            usage=usage if isinstance(usage, dict) else {},
            raw_text=str(content)[:2000],
        )

    def ping(self) -> GradeResult:
        """连通性自检（用极小请求验证 key 与网络）。"""
        return self.grade(
            question="请判断：1+1 是否等于 2？",
            reference="等于 2",
            user_answer="等于 2",
            qtype_label="简答题",
        )


__all__ = [
    "DEFAULT_BASE_URL", "DEFAULT_MODEL", "DEFAULT_TIMEOUT",
    "SCORE_CORRECT", "SCORE_PARTIAL", "VERDICT_VALUES",
    "Transport", "GradeResult",
    "GraderError", "GraderNotConfigured", "GraderNetworkError", "GraderResponseError",
    "DeepSeekGrader", "mask_key", "redact", "build_messages", "urllib_transport",
]
