"""Small HTTP client for configured multimodal chat-completions providers.

Protocol reference: https://help.aliyun.com/zh/model-studio/vision
Keys are read only from the explicit config/environment, never printed.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import Settings


class ModelError(RuntimeError):
    pass


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError("模型没有返回合法 JSON；原始响应不包含在日志中") from exc
    if not isinstance(value, dict):
        raise ModelError("模型 JSON 必须是对象")
    return value


def complete_json(settings: Settings, prompt: str, *, frames: list[dict] | None = None,
                  timeout: int = 180, text_only: bool = False) -> dict:
    if not settings.model_configured:
        raise ModelError("model_not_configured：请配置 .env 中的模型地址/名称，或使用已审核的事实与文案 JSON")
    parsed = urlparse(settings.base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelError("模型地址不能携带账号、密码、查询参数或 fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ModelError("模型地址须为 HTTPS；本机模型允许 localhost HTTP")
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for frame in frames or []:
        path = Path(frame["path"])
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ModelError("抽帧不存在或超过 4 MB，请先重新预处理视频")
        parts.append({"type": "text", "text": f"frame_id={frame['id']}, time={frame.get('timestamp_seconds', 0)}s"})
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}})
    body = {
        "model": (settings.text_model or settings.model) if text_only else settings.model,
        "messages": [{"role": "user", "content": parts if frames else prompt}],
        "temperature": 0.3,
        "max_tokens": 5000,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.api_key:
        headers["Authorization"] = "Bearer " + settings.api_key
    url = settings.base_url
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(16 * 1024 * 1024)
        result = json.loads(payload)
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ModelError("模型输出被截断，请缩短输入或调整模型输出上限")
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise ModelError("模型响应缺少文本内容")
    except HTTPError as exc:
        raise ModelError(f"模型 HTTP {exc.code}：请核对地址、模型权限、配额与输入格式") from None
    except (URLError, TimeoutError, OSError):
        raise ModelError("模型连接失败或超时；请检查配置与网络") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError("模型返回结构与 chat-completions 协议不一致") from None
    return parse_json_object(content)
