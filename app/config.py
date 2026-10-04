from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    model_mode: Literal["scripted", "real"] = "scripted"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.8-max"
    llm_api_key: SecretStr = SecretStr("")
    llm_request_timeout_seconds: int = Field(default=60, ge=1, le=300)
    llm_max_output_tokens: int = Field(default=1500, ge=1, le=32768)
    llm_reasoning_effort: Literal["", "low", "medium", "high", "max"] = ""
    data_dir: Path = Path("data")
    max_model_calls: int = Field(default=30, ge=1, le=100)
    max_total_tokens: int = Field(default=60000, ge=1)
    run_timeout_seconds: int = Field(default=180, ge=1, le=900)
