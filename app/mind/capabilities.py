"""Provider-neutral intent, capability and user-decision contracts."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Intent(Contract):
    objective: str = Field(min_length=3, max_length=8000)
    capability: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    provider: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')


class CapabilitySpec(Contract):
    capability: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    provider: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    aliases: list[str] = Field(default_factory=list, max_length=32)
    operations: list[str] = Field(min_length=1)
    requirements: list[str] = Field(default_factory=list)
    status: str


class DecisionReceipt(Contract):
    decision: str
    scope: str
    source: str
    created_at: datetime
    expires_at: datetime | None = None
