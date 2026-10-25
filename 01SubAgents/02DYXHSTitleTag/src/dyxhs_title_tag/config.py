"""Explicit configuration, without shell evaluation or credential discovery."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex


ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = path or ROOT / ".env"
    if env_path.is_file():
        for line_number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key.replace("_", "").isalnum():
                raise ValueError(f"配置文件第 {line_number} 行格式错误")
            parts = shlex.split(value, comments=True, posix=True)
            values[key] = " ".join(parts)
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class Settings:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    text_model: str = ""
    xhs_mcp_url: str = ""

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        env = load_env(env_file)
        return cls(
            base_url=env.get("DYXHS_LLM_BASE_URL", "").rstrip("/"),
            api_key=env.get("DYXHS_LLM_API_KEY", ""),
            model=env.get("DYXHS_LLM_MODEL", ""),
            text_model=env.get("DYXHS_TEXT_MODEL", ""),
            xhs_mcp_url=env.get("DYXHS_XHS_MCP_URL", ""),
        )

    @property
    def model_configured(self) -> bool:
        return bool(self.base_url and self.model)
