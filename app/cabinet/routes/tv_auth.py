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

async def submit_tv_auth_data(
    token: str, 
    sub_url: Optional[str] = None, 
    lk_token: Optional[str] = None,
    refresh_token: Optional[str] = None  # <--- Добавьте этот аргумент
) -> bool:
    """Сохраняем данные, присланные с мобильного устройства."""
    # Получаем текущие данные токена из кэша
    raw_data = await redis_client.get(f"tv_auth:{token}")
    if not raw_data:
        return False
        
    # Обновляем данные, добавляя refresh_token
    payload = {
        "status": "completed",
        "sub_url": sub_url,
        "lk_token": lk_token,
        "refresh_token": refresh_token  # <--- Сохраняем в кэш
    }
    
    # Сохраняем обратно в Redis с тем же TTL
    await redis_client.setex(
        f"tv_auth:{token}",
        TV_AUTH_TOKEN_TTL,
        json.dumps(payload)
    )
    return True

async def consume_tv_auth_token(token: str) -> Dict:
    """Забираем данные и сразу удаляем токен (единоразовое использование)."""
    raw_data = await redis_client.get(f"tv_auth:{token}")
    if not raw_data:
        return {}
        
    data = json.loads(raw_data)
    
    # Удаляем токен, чтобы избежать повторного использования (и 410 ошибки позже)
    await redis_client.delete(f"tv_auth:{token}")
    
    return data

@router.post('/submit')
async def submit_tv_data(request: TVAuthSubmitRequest):
    """Эндпоинт для телефона: передача данных на ТВ."""
    success = await submit_tv_auth_data(
        token=request.token,
        sub_url=request.sub_url,
        lk_token=request.lk_token,
        refresh_token=request.refresh_token  # <--- Передаем в сервис
    )
    if not success:
        raise HTTPException(status_code=404, detail="Token expired or invalid")
    return {"status": "ok"}

@router.post('/poll', response_model=TVAuthPollResponse)
async def poll_tv_token(request: TVAuthSubmitRequest):
    """Эндпоинт для ТВ: ожидание данных от телефона."""
    data = await poll_tv_auth_token(request.token)
    
    if not data:
        # Важно: 410 заставит мобильное приложение прекратить поллинг
        raise HTTPException(status_code=410, detail="Token expired or already consumed")
    
    if data.get('status') == 'pending':
        return TVAuthPollResponse(status='pending')
    
    # Если статус completed, забираем данные и удаляем токен из Redis/Cache
    consumed = await consume_tv_auth_token(request.token)
    
    return TVAuthPollResponse(
        status='completed',
        sub_url=consumed.get('sub_url'),
        lk_token=consumed.get('lk_token'),
        refresh_token=consumed.get('refresh_token')  # <--- Возвращаем на ТВ
    )
