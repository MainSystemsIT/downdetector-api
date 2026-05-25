import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

SERVICE_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ServiceStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    DOWN = "down"
    UNKNOWN = "unknown"


class ServiceStatusResponse(BaseModel):
    service: str = Field(description="Identificador do serviço no Downdetector")
    status: ServiceStatus
    label: str = Field(description="Rótulo legível em português")
    message: str = Field(description="Texto original exibido no Downdetector")
    source_url: str
    app_image_url: str | None = Field(
        default=None,
        description="URL da imagem/logo do aplicativo no Downdetector",
    )
    failure_graph_image_url: str | None = Field(
        default=None,
        description="Data URL JPEG do card do serviço na tela principal",
    )
    checked_at: datetime
    cached: bool = False


class CriticalServiceItem(BaseModel):
    service: str = Field(description="Identificador do serviço no Downdetector")
    status: ServiceStatus
    label: str = Field(description="Rótulo legível em português")
    message: str = Field(description="Texto do card exibido no Downdetector")
    source_url: str
    app_image_url: str | None = Field(
        default=None,
        description="URL da imagem/logo do aplicativo no Downdetector",
    )


class CriticalServicesResponse(BaseModel):
    services: list[CriticalServiceItem]
    image_url: str | None = Field(
        default=None,
        description="Data URL JPEG com os cards críticos lado a lado",
    )
    source_url: str
    checked_at: datetime
    message: str | None = Field(
        default=None,
        description="Mensagem informativa quando a consulta usa fallback/cache",
    )
    cached: bool = False
