import secrets
from datetime import UTC, datetime
import json
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
    key = cache_key(TV_AUTH_PREFIX, token) # Используем один и тот же генератор ключа!
    
    # Сначала проверяем существование
    exists = await cache.get(key)
    if not exists:
        return False
        
    payload = {
        "status": "completed",
        "sub_url": sub_url,
        "lk_token": lk_token,
        "refresh_token": refresh_token
    }
    
    # Кладем СЛОВАРЬ, а не строку JSON
    await cache.set(key, payload, expire=TV_AUTH_TOKEN_TTL)
    return True

async def consume_tv_auth_token(token: str) -> dict:
    key = cache_key(TV_AUTH_PREFIX, token)
    data = await cache.get(key)
    if not data:
        return {}
    
    # Если вдруг в кэше строка (на всякий случай), распарсим. 
    # Но если везде класть dict, то data уже будет словарем.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except:
            pass
            
    await cache.delete(key)
    return data

async def poll_tv_auth_token(token: str) -> dict[str, Any] | None:
    """Возвращает текущее состояние токена."""
    key = cache_key(TV_AUTH_PREFIX, token)
    return await cache.get(key)
