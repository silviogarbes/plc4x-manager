"""
Pydantic v2 models for PLC4X Manager FastAPI.
Request/response validation for all API endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


# =============================================
# Auth models
# =============================================

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128, description="Username")
    password: str = Field(..., min_length=1, max_length=256, description="Password")


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    expiresIn: int
    plants: Optional[List[str]] = None


class TokenVerifyResponse(BaseModel):
    username: str
    authenticated: bool


class ChangePasswordRequest(BaseModel):
    password: str = Field(..., min_length=4, max_length=256, description="New password (min 4 chars)")


# =============================================
# Device models
# =============================================

class TagCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9._-]{1,128}$')
    address: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = Field(None, max_length=256)
    alarmThresholds: Optional[Dict[str, Any]] = None

    @field_validator('alias')
    @classmethod
    def alias_no_spaces(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("alias cannot be empty")
        return v


class CalculatedTagCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9._-]{1,128}$')
    formula: str = Field(..., min_length=1, max_length=1024)
    description: Optional[str] = Field(None, max_length=256)


class OEEConfig(BaseModel):
    enabled: bool = False
    plannedTime: Optional[float] = Field(None, ge=0)
    targetCycles: Optional[float] = Field(None, ge=0)
    goodPartsTag: Optional[str] = None
    totalPartsTag: Optional[str] = None
    runningTag: Optional[str] = None
    downtimeTag: Optional[str] = None


class DeviceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9._-]{1,128}$')
    connectionString: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = Field(None, max_length=256)
    enabled: bool = True
    allowWrite: bool = False
    pollInterval: int = Field(default=5, ge=1, le=3600)
    plant: Optional[str] = Field(None, max_length=128)
    tags: List[TagCreate] = Field(default_factory=list)
    calculatedTags: List[CalculatedTagCreate] = Field(default_factory=list)
    oeeConfig: Optional[OEEConfig] = None


class DeviceUpdate(BaseModel):
    connectionString: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = Field(None, max_length=256)
    enabled: Optional[bool] = None
    allowWrite: Optional[bool] = None
    pollInterval: Optional[int] = Field(None, ge=1, le=3600)
    plant: Optional[str] = Field(None, max_length=128)
    oeeConfig: Optional[OEEConfig] = None


# =============================================
# Logbook models
# =============================================

class LogbookEntry(BaseModel):
    message: str = Field(..., min_length=1, max_length=2048)
    category: Optional[str] = Field(None, max_length=64)
    device: Optional[str] = Field(None, max_length=128)
    tag: Optional[str] = Field(None, max_length=128)
    severity: Optional[str] = Field(None, pattern=r'^(info|warning|critical)$')
    timestamp: Optional[datetime] = None

    @field_validator('timestamp', mode='before')
    @classmethod
    def set_timestamp(cls, v: Any) -> datetime:
        if v is None:
            return datetime.now(timezone.utc)
        return v


class LogbookEntryResponse(BaseModel):
    id: Optional[str] = None
    timestamp: str
    user: Optional[str] = None
    message: str
    category: Optional[str] = None
    device: Optional[str] = None
    tag: Optional[str] = None
    severity: Optional[str] = None


# =============================================
# Audit models
# =============================================

class AuditEntry(BaseModel):
    timestamp: str
    user: Optional[str] = None
    action: str
    ip: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class AuditListResponse(BaseModel):
    entries: List[AuditEntry]
    total: int


# =============================================
# Generic response models
# =============================================

class MessageResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    error: str
    locked: Optional[bool] = None
    retryAfter: Optional[int] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# =============================================
# Formula validation
# =============================================

class FormulaTestRequest(BaseModel):
    formula: str = Field(..., min_length=1, max_length=1024)
    values: Optional[Dict[str, float]] = None


class FormulaTestResponse(BaseModel):
    valid: bool
    result: Optional[Union[float, int, bool]] = None
    error: Optional[str] = None
