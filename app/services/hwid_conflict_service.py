from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Subscription, SubscriptionStatus, User
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)
_STATUS_PRIORITY = {
    SubscriptionStatus.ACTIVE.value: 3,
    SubscriptionStatus.TRIAL.value: 2,
    SubscriptionStatus.LIMITED.value: 1,
}

_HWID_CONFLICT_REPORT_CACHE: HwidConflictScanResult | None = None


@dataclass(slots=True)
class HwidConflictAccount:
    remnawave_id: int
    user_id: int | None = None
    telegram_id: int | None = None
    user_label: str = 'Неизвестный аккаунт'
    subscription_id: int | None = None
    subscription_status: str | None = None
    subscription_statuses: tuple[str, ...] = ()
    subscription_count: int = 0
    active_paid_count: int = 0
    active_trial_count: int = 0
    tariff_name: str | None = None


@dataclass(slots=True)
class HwidConflict:
    hwid: str
    accounts: list[HwidConflictAccount]


@dataclass(slots=True)
class HwidConflictScanResult:
    scanned_devices: int
    duplicate_hwids: int
    unique_panel_users_in_conflicts: int
    unmatched_panel_users: int
    conflicts: list[HwidConflict]

    @property
    def has_conflicts(self) -> bool:
        return self.duplicate_hwids > 0


class HwidConflictService:
    """Поиск конфликтов HWID (одно устройство в нескольких подписках)."""

    def __init__(self) -> None:
        self._remnawave_service = RemnaWaveService()

    @staticmethod
    def get_cached_report() -> HwidConflictScanResult | None:
        return _HWID_CONFLICT_REPORT_CACHE

    @staticmethod
    def set_cached_report(report: HwidConflictScanResult | None) -> None:
        global _HWID_CONFLICT_REPORT_CACHE
        _HWID_CONFLICT_REPORT_CACHE = report

    @staticmethod
    def clear_cached_report() -> None:
        global _HWID_CONFLICT_REPORT_CACHE
        _HWID_CONFLICT_REPORT_CACHE = None

    async def scan_conflicts(self, db: AsyncSession) -> HwidConflictScanResult:
        async with self._remnawave_service.get_api_client() as api:
            devices_data = await api.get_all_hwid_devices()
            devices = devices_data.get('devices') or []

            if not devices:
                return HwidConflictScanResult(
                    scanned_devices=0,
                    duplicate_hwids=0,
                    unique_panel_users_in_conflicts=0,
                    unmatched_panel_users=0,
                    conflicts=[],
                )

        hwid_to_panel_ids: dict[str, set[int]] = {}
        unresolved_panel_ids: set[int] = set()

        for device in devices:
            hwid = self._extract_hwid(device)
            if not hwid:
                continue

            panel_id = self._extract_panel_user_id(device)
            if panel_id is None:
                continue

            hwid_to_panel_ids.setdefault(hwid, set()).add(panel_id)

        duplicate_hwid_map = {
            hwid: panel_ids for hwid, panel_ids in hwid_to_panel_ids.items() if len(panel_ids) > 1
        }

        if not duplicate_hwid_map:
            return HwidConflictScanResult(
                scanned_devices=len(devices),
                duplicate_hwids=0,
                unique_panel_users_in_conflicts=0,
                unmatched_panel_users=0,
                conflicts=[],
            )

        all_conflict_ids = {panel_id for panel_ids in duplicate_hwid_map.values() for panel_id in panel_ids}

        # Если сессия была "протухшей" или держала старую транзакцию слишком долго,
        # сначала сбрасываем её состояние и только потом идём в БД.
        await self._safe_db_rollback(db)

        panel_metadata = await self._load_panel_metadata(db, all_conflict_ids)
        unresolved_ids = all_conflict_ids - set(panel_metadata.keys())

        conflicts: list[HwidConflict] = []

        sorted_conflicts = sorted(
            duplicate_hwid_map.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )

        for hwid, panel_ids in sorted_conflicts:
            accounts: list[HwidConflictAccount] = []
            for panel_id in sorted(panel_ids):
                account = panel_metadata.get(panel_id)
                if not account:
                    accounts.append(
                        HwidConflictAccount(
                            remnawave_id=panel_id,
                            user_label='Аккаунт не найден в БД бота',
                        )
                    )
                    continue

                accounts.append(account)

            conflicts.append(HwidConflict(hwid=hwid, accounts=accounts))

        return HwidConflictScanResult(
            scanned_devices=len(devices),
            duplicate_hwids=len(duplicate_hwid_map),
            unique_panel_users_in_conflicts=len(all_conflict_ids),
            unmatched_panel_users=len(unresolved_ids) + len(unresolved_panel_ids),
            conflicts=conflicts,
        )

    async def _load_panel_metadata(
        self,
        db: AsyncSession,
        panel_ids: set[int],
    ) -> dict[int, HwidConflictAccount]:
        if not panel_ids:
            return {}

        metadata: dict[int, HwidConflictAccount] = {}
        panel_id_chunks = list(self._chunked(sorted(panel_ids), chunk_size=250))

        for chunk in panel_id_chunks:
            subscriptions = await self._execute_subscription_chunk_query(db, chunk)

            grouped_subscriptions: dict[int, list[Subscription]] = {}
            grouped_users: dict[int, User] = {}

            for subscription in subscriptions:
                panel_id = self._extract_panel_id(subscription)
                if panel_id is None:
                    continue

                grouped_subscriptions.setdefault(panel_id, []).append(subscription)

                if subscription.user and panel_id not in grouped_users:
                    grouped_users[panel_id] = subscription.user

            unresolved_ids = set(chunk) - set(grouped_subscriptions.keys())
            if unresolved_ids:
                users = await self._execute_user_chunk_query(db, unresolved_ids)

                for user in users:
                    panel_id = self._extract_user_panel_id(user)
                    if panel_id is None:
                        continue

                    grouped_subscriptions.setdefault(panel_id, []).extend(user.subscriptions or [])
                    if panel_id not in grouped_users:
                        grouped_users[panel_id] = user

            for panel_id, subscriptions_for_id in grouped_subscriptions.items():
                if not subscriptions_for_id:
                    continue

                user = grouped_users.get(panel_id) or next(
                    (sub.user for sub in subscriptions_for_id if sub.user),
                    None,
                )
                best_subscription = self._pick_best_subscription(subscriptions_for_id)
                subscription_statuses = tuple(sorted({sub.status for sub in subscriptions_for_id if sub.status}))
                active_paid_count = sum(
                    1
                    for sub in subscriptions_for_id
                    if sub.status == SubscriptionStatus.ACTIVE.value and not bool(getattr(sub, 'is_trial', False))
                )
                active_trial_count = sum(
                    1
                    for sub in subscriptions_for_id
                    if sub.status == SubscriptionStatus.ACTIVE.value and bool(getattr(sub, 'is_trial', False))
                )

                account = HwidConflictAccount(
                    remnawave_id=panel_id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else None,
                    user_label=self._build_user_label(user) if user else 'Аккаунт не найден в БД бота',
                    subscription_id=best_subscription.id if best_subscription else None,
                    subscription_status=best_subscription.status if best_subscription else None,
                    subscription_statuses=subscription_statuses,
                    subscription_count=len(subscriptions_for_id),
                    active_paid_count=active_paid_count,
                    active_trial_count=active_trial_count,
                    tariff_name=best_subscription.tariff.name if best_subscription and best_subscription.tariff else None,
                )
                self._upsert_best_account(metadata, account)

        return metadata

    async def _execute_subscription_chunk_query(self, db: AsyncSession, panel_ids: list[int]) -> list[Subscription]:
        if not panel_ids:
            return []

        statement = (
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .join(Subscription.user, isouter=True)
            .where(
                or_(
                    Subscription.remnawave_id.in_(panel_ids),
                    User.remnawave_id.in_(panel_ids),
                ),
            )
        )

        result = await self._execute_with_retry(db, statement, stage='subscriptions')
        return result.scalars().unique().all()

    async def _execute_user_chunk_query(self, db: AsyncSession, panel_ids: set[int]) -> list[User]:
        if not panel_ids:
            return []

        statement = (
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
            .where(User.remnawave_id.in_(list(panel_ids)))
        )

        result = await self._execute_with_retry(db, statement, stage='users')
        return result.scalars().unique().all()

    async def _execute_with_retry(self, db: AsyncSession, statement: Any, stage: str):
        """
        Один retry на случай закрытого/протухшего connection в asyncpg.
        """
        try:
            return await db.execute(statement)
        except (InterfaceError, OperationalError, DBAPIError) as error:
            logger.warning('DB execute failed, retrying once', stage=stage, error=error)
            await self._safe_db_rollback(db)
            return await db.execute(statement)

    async def _safe_db_rollback(self, db: AsyncSession) -> None:
        try:
            await db.rollback()
        except Exception as error:
            logger.debug('Не удалось выполнить rollback сессии', error=error)

    def _extract_panel_id(self, subscription: Subscription) -> int | None:
        panel_id = self._normalize_int(getattr(subscription, 'remnawave_id', None))
        if panel_id is not None:
            return panel_id
        if subscription.user:
            return self._normalize_int(getattr(subscription.user, 'remnawave_id', None))
        return None

    def _extract_user_panel_id(self, user: User) -> int | None:
        return self._normalize_int(getattr(user, 'remnawave_id', None))

    def _upsert_best_account(
        self,
        metadata: dict[int, HwidConflictAccount],
        candidate: HwidConflictAccount,
    ) -> None:
        existing = metadata.get(candidate.remnawave_id)
        if not existing or self._account_priority(candidate) > self._account_priority(existing):
            metadata[candidate.remnawave_id] = candidate

    def _account_priority(self, account: HwidConflictAccount) -> tuple[int, int, int, int, int, int]:
        return (
            _STATUS_PRIORITY.get(account.subscription_status or '', 0),
            1 if account.active_paid_count else 0,
            1 if account.active_trial_count else 0,
            1 if account.telegram_id else 0,
            1 if account.subscription_id else 0,
            1 if account.tariff_name else 0,
        )

    def _pick_best_subscription(self, subscriptions: list[Subscription]) -> Subscription | None:
        if not subscriptions:
            return None

        active_subscriptions = [sub for sub in subscriptions if sub.status in _ACTIVE_STATUSES]
        if not active_subscriptions:
            return subscriptions[0]

        return max(
            active_subscriptions,
            key=lambda sub: (
                _STATUS_PRIORITY.get(sub.status, 0),
                1 if not bool(getattr(sub, 'is_trial', False)) else 0,
                self._safe_timestamp(sub.end_date),
            ),
        )

    def _build_user_label(self, user: User) -> str:
        if user.username:
            return f'@{user.username}'
        if user.telegram_id:
            return user.full_name
        if user.email:
            return user.email
        return f'User {user.id}'

    def _extract_hwid(self, device: dict[str, Any]) -> str:
        for key in ('hwid', 'deviceId'):
            value = self._normalize_str(device.get(key))
            if value:
                return value
        return ''

    def _extract_panel_user_id(self, device: dict[str, Any]) -> int | None:
        for key in ('userId', 'user_id', 'userID'):
            user_id = self._normalize_int(device.get(key))
            if user_id is not None:
                return user_id
        return None

    def _normalize_str(self, value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    def _normalize_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _safe_timestamp(self, value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value.timestamp())
        except Exception:
            return 0.0

    def _chunked(self, values: list[int], chunk_size: int = 250):
        for start in range(0, len(values), chunk_size):
            yield values[start : start + chunk_size]
