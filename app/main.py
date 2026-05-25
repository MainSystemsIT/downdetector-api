from contextlib import asynccontextmanager
from secrets import compare_digest

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.models import (
    CriticalServiceItem,
    CriticalServicesResponse,
    ServiceStatus,
    ServiceStatusResponse,
)
from app.scraper import (
    fetch_critical_services,
    normalize_service,
    status_cache,
    status_label,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    from app.scraper import _reset_session

    _reset_session()


app = FastAPI(
    title="Downdetector API",
    description=(
        "API para verificar status de serviços via Downdetector. "
        "Ex.: `/youtube/status`, `/whatsapp/status`, `/instagram/is-up`."
    ),
    version="2.0.0",
    lifespan=lifespan,
)
bearer_scheme = HTTPBearer(auto_error=False)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    if not settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_TOKEN não configurado.",
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not compare_digest(credentials.credentials, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _get_status(service: str, refresh: bool) -> ServiceStatusResponse:
    try:
        slug = normalize_service(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result, cached = status_cache.get(slug, force_refresh=refresh)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao consultar o Downdetector: {exc}",
        ) from exc

    return ServiceStatusResponse(
        service=result.service,
        status=result.status,
        label=result.label,
        message=result.message,
        source_url=result.source_url,
        app_image_url=result.app_image_url,
        failure_graph_image_url=result.failure_graph_image_url,
        checked_at=result.checked_at,
        cached=cached,
    )


def _get_critical_services(count: int) -> CriticalServicesResponse:
    try:
        result = fetch_critical_services(limit=count)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao consultar serviços críticos no Downdetector: {exc}",
        ) from exc

    return CriticalServicesResponse(
        services=[
            CriticalServiceItem(
                service=service.service,
                status=service.status,
                label=service.label,
                message=service.message,
                source_url=service.source_url,
                app_image_url=service.app_image_url,
            )
            for service in result.services
        ],
        image_url=result.image_url,
        source_url=result.source_url,
        checked_at=result.checked_at,
        cached=False,
    )


@app.get(
    "/critical/services",
    response_model=CriticalServicesResponse,
    summary="Serviços mais críticos",
    description=(
        "Consulta a tela principal do Downdetector e retorna os serviços mais "
        "críticos com uma imagem JPEG dos cards lado a lado."
    ),
)
def critical_services(
    count: int | None = Query(
        None,
        ge=1,
        le=10,
        description="Quantidade máxima de serviços críticos retornados.",
    ),
    limit: int | None = Query(
        None,
        ge=1,
        le=10,
        include_in_schema=False,
    ),
    _auth: None = Depends(require_api_token),
) -> CriticalServicesResponse:
    return _get_critical_services(count or limit or 5)


@app.get(
    "/{service}/status",
    response_model=ServiceStatusResponse,
    summary="Status de um serviço",
    description=(
        "Consulta o Downdetector para o serviço informado. "
        "O slug deve corresponder ao usado no site (ex.: `youtube`, `whatsapp`, `nubank`)."
    ),
)
def service_status(
    service: str,
    refresh: bool = Query(
        False,
        description="Ignora o cache e busca o status novamente no Downdetector.",
    ),
    _auth: None = Depends(require_api_token),
) -> ServiceStatusResponse:
    return _get_status(service, refresh)


@app.get("/{service}/is-up", summary="Serviço está funcionando?")
def service_is_up(
    service: str,
    refresh: bool = Query(False, description="Ignora o cache."),
    _auth: None = Depends(require_api_token),
) -> dict[str, bool | str]:
    response = _get_status(service, refresh)
    is_up = response.status in (
        ServiceStatus.OK,
        ServiceStatus.WARNING,
        ServiceStatus.UNKNOWN,
    )
    return {
        "service": response.service,
        "up": is_up,
        "status": response.status.value,
        "label": status_label(response.status),
        "message": response.message,
    }
