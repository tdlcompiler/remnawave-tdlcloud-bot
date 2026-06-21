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

async def submit_tv_auth_data(token: str, sub_url: str | None = None, lk_token: str | None = None) -> bool:
    """Принимает данные от телефона и обновляет состояние токена."""
    key = cache_key(TV_AUTH_PREFIX, token)
    data: Any = await cache.get(key)

    if not data or not isinstance(data, dict):
        return False

    if data.get('status') != 'pending':
        return False

    data['status'] = 'completed'
    data['sub_url'] = sub_url
    data['lk_token'] = lk_token
    data['submitted_at'] = datetime.now(UTC).isoformat()

    # Обновляем TTL, чтобы ТВ успел забрать данные
    await cache.set(key, data, expire=TV_AUTH_COMPLETED_TTL)
    return True

async def poll_tv_auth_token(token: str) -> dict[str, Any] | None:
    """Возвращает текущее состояние токена."""
    key = cache_key(TV_AUTH_PREFIX, token)
    return await cache.get(key)

async def consume_tv_auth_token(token: str) -> dict[str, Any] | None:
    """Атомарно забирает и удаляет данные (чтобы нельзя было использовать дважды)."""
    key = cache_key(TV_AUTH_PREFIX, token)
    return await cache.getdel(key)
