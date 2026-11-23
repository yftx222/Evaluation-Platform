from typing import Self
from uuid import uuid4

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVAL_WORKER_", env_file=".env", extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=160)
    slots: int = Field(4, ge=1, le=256)
    global_slots: int = Field(16, ge=1, le=4096)
    batch: int = Field(4, ge=1, le=128)
    poll_s: float = Field(1, gt=0, le=60)
    lease_s: float = Field(30, gt=0, le=3600)
    heartbeat_s: float = Field(5, gt=0, le=600)
    shutdown_s: float = Field(5, gt=0, le=120)

    @model_validator(mode="after")
    def heartbeats(self) -> Self:
        if self.heartbeat_s * 3 >= self.lease_s:
            raise ValueError("心跳间隔必须小于租期的三分之一")
        return self
