import secrets
from datetime import UTC, datetime
from typing import Any
import structlog
from app.utils.cache import cache, cache_key

logger = structlog.get_logger(__name__)

TV_AUTH_TOKEN_TTL = 300  # 5 минут на сканирование
TV_AUTH_COMPLETED_TTL = 120  # 2 минуты после сканирования (окно для поллинга)
TV_AUTH_PREFIX = 'tv_auth'

async def create_tv_auth_token() -> str:
    """Создает токен для ТВ и сохраняет его в Redis со статусом pending."""
    token = secrets.token_urlsafe(24)
    key = cache_key(TV_AUTH_PREFIX, token)
    value: dict[str, Any] = {
        'status': 'pending',
        'created_at': datetime.now(UTC).isoformat(),
    }
    await cache.set(key, value, expire=TV_AUTH_TOKEN_TTL)
    return token

async def submit_tv_auth_data(
    token: str, 
    sub_url: str | None = None, 
    lk_token: str | None = None,
    refresh_token: str | None = None
) -> bool:
    """Сохраняем данные, присланные с мобильного устройства."""
    # Получаем текущие данные токена из кэша
    raw_data = await cache.get(f"tv_auth:{token}")
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
    await cache.setex(
        f"tv_auth:{token}",
        TV_AUTH_TOKEN_TTL,
        json.dumps(payload)
    )
    return True

async def consume_tv_auth_token(token: str) -> dict:
    """Забираем данные и сразу удаляем токен (единоразовое использование)."""
    raw_data = await cache.get(f"tv_auth:{token}")
    if not raw_data:
        return {}
        
    data = json.loads(raw_data)
    
    # Удаляем токен, чтобы избежать повторного использования (и 410 ошибки позже)
    await cache.delete(f"tv_auth:{token}")
    
    return data

async def poll_tv_auth_token(token: str) -> dict[str, Any] | None:
    """Возвращает текущее состояние токена."""
    key = cache_key(TV_AUTH_PREFIX, token)
    return await cache.get(key)
