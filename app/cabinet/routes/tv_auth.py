import structlog
from fastapi import APIRouter, HTTPException, Request, status
from app.cabinet.schemas.auth import (
    TVAuthTokenResponse, 
    TVAuthSubmitRequest, 
    TVAuthPollResponse
)
from app.services.tv_auth_service import (
    create_tv_auth_token,
    submit_tv_auth_data,
    poll_tv_auth_token,
    consume_tv_auth_token,
    TV_AUTH_TOKEN_TTL
)
from app.cabinet.ip_utils import get_client_ip
from app.utils.cache import RateLimitCache

logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/auth/tv', tags=['TV Auth'])

@router.post('/request', response_model=TVAuthTokenResponse)
async def request_tv_token(request: Request):
    """Эндпоинт для ТВ: получение нового токена для QR-кода."""
    client_ip = get_client_ip(request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'tv_request', limit=5, window=60):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    token = await create_tv_auth_token()
    return TVAuthTokenResponse(token=token, expires_in=TV_AUTH_TOKEN_TTL)

@router.post('/submit')
async def submit_tv_data(request: TVAuthSubmitRequest):
    """Эндпоинт для телефона: передача данных на ТВ."""
    success = await submit_tv_auth_data(
        token=request.token,
        sub_url=request.sub_url,
        lk_token=request.lk_token
    )
    if not success:
        raise HTTPException(status_code=404, detail="Token expired or invalid")
    return {"status": "ok"}

@router.post('/poll', response_model=TVAuthPollResponse)
async def poll_tv_token(request: TVAuthSubmitRequest): # Используем тот же формат где есть token
    """Эндпоинт для ТВ: ожидание данных от телефона."""
    data = await poll_tv_auth_token(request.token)
    
    if not data:
        raise HTTPException(status_code=410, detail="Token expired")
    
    if data.get('status') == 'pending':
        return TVAuthPollResponse(status='pending')
    
    # Если статус completed, забираем данные и удаляем токен
    consumed = await consume_tv_auth_token(request.token)
    return TVAuthPollResponse(
        status='completed',
        sub_url=consumed.get('sub_url'),
        lk_token=consumed.get('lk_token')
    )
