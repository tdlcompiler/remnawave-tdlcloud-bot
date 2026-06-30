from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import or_, select
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
    remnawave_uuid: str
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

            user_ids = self._collect_user_ids(devices)
            user_id_to_uuid = await self._load_user_uuid_map(api, user_ids) if user_ids else {}

        hwid_to_panel_uuids: dict[str, set[str]] = {}
        unresolved_user_ids: set[int] = set()

        for device in devices:
            hwid = self._extract_hwid(device)
            if not hwid:
                continue

            panel_uuid = self._resolve_panel_uuid_for_device(device, user_id_to_uuid, unresolved_user_ids)
            if not panel_uuid:
                continue

            hwid_to_panel_uuids.setdefault(hwid, set()).add(panel_uuid)

        duplicate_hwid_map = {
            hwid: panel_uuids for hwid, panel_uuids in hwid_to_panel_uuids.items() if len(panel_uuids) > 1
        }

        if not duplicate_hwid_map:
            return HwidConflictScanResult(
                scanned_devices=len(devices),
                duplicate_hwids=0,
                unique_panel_users_in_conflicts=0,
                unmatched_panel_users=len(unresolved_user_ids),
                conflicts=[],
            )

        all_conflict_uuids = {panel_uuid for panel_uuids in duplicate_hwid_map.values() for panel_uuid in panel_uuids}
        uuid_metadata = await self._load_uuid_metadata(db, all_conflict_uuids)
        unresolved_uuids = all_conflict_uuids - set(uuid_metadata.keys())

        conflicts: list[HwidConflict] = []

        sorted_conflicts = sorted(
            duplicate_hwid_map.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )

        for hwid, panel_uuids in sorted_conflicts:
            accounts: list[HwidConflictAccount] = []
            for panel_uuid in sorted(panel_uuids):
                account = uuid_metadata.get(panel_uuid)
                if not account:
                    accounts.append(
                        HwidConflictAccount(
                            remnawave_uuid=panel_uuid,
                            user_label='Аккаунт не найден в БД бота',
                        )
                    )
                    continue

                accounts.append(account)

            conflicts.append(HwidConflict(hwid=hwid, accounts=accounts))

        return HwidConflictScanResult(
            scanned_devices=len(devices),
            duplicate_hwids=len(duplicate_hwid_map),
            unique_panel_users_in_conflicts=len(all_conflict_uuids),
            unmatched_panel_users=len(unresolved_uuids) + len(unresolved_user_ids),
            conflicts=conflicts,
        )

    async def _load_user_uuid_map(
        self,
        api: Any,
        user_ids: set[int],
    ) -> dict[int, str]:
        """
        Загружает сопоставление panel userId -> panel uuid.

        Порядок:
        1) Если у API уже есть get_user_by_id — используем его с ограничением параллелизма.
        2) Иначе делаем один проход по всем пользователям панели и строим индекс по user.id.
        """
        if not user_ids:
            return {}

        getter = getattr(api, 'get_user_by_id', None)
        if callable(getter):
            semaphore = asyncio.Semaphore(20)

            async def fetch_one(user_id: int) -> tuple[int, str | None]:
                async with semaphore:
                    try:
                        user = await getter(user_id)
                    except Exception as error:
                        logger.warning(
                            'Не удалось получить пользователя панели по userId',
                            user_id=user_id,
                            error=error,
                        )
                        return user_id, None

                    if not user:
                        return user_id, None
                    return user_id, getattr(user, 'uuid', None)

            results = await asyncio.gather(*(fetch_one(user_id) for user_id in sorted(user_ids)))
            return {user_id: uuid for user_id, uuid in results if uuid}

        # Fallback: один полный проход по пользователям панели.
        # Это медленнее, но работает даже без отдельного endpoint by-id.
        try:
            users = await api.get_all_users_stream(size=500, enrich_happ_links=False)
        except Exception as error:
            logger.error('Не удалось загрузить список пользователей панели', error=error)
            return {}

        result: dict[int, str] = {}
        wanted_ids = set(user_ids)

        for user in users:
            user_id = getattr(user, 'id', None)
            user_uuid = getattr(user, 'uuid', None)
            if user_id is None or not user_uuid:
                continue
            if user_id in wanted_ids:
                result[int(user_id)] = str(user_uuid)

        return result

    async def _load_uuid_metadata(
        self,
        db: AsyncSession,
        panel_uuids: set[str],
    ) -> dict[str, HwidConflictAccount]:
        if not panel_uuids:
            return {}

        panel_uuids_list = list(panel_uuids)
        metadata: dict[str, HwidConflictAccount] = {}

        subscriptions_query = (
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .join(Subscription.user, isouter=True)
            .where(
                or_(
                    Subscription.remnawave_uuid.in_(panel_uuids_list),
                    User.remnawave_uuid.in_(panel_uuids_list),
                ),
            )
        )
        subscriptions_result = await db.execute(subscriptions_query)
        subscriptions = subscriptions_result.scalars().unique().all()

        grouped_subscriptions: dict[str, list[Subscription]] = {}
        grouped_users: dict[str, User] = {}

        for subscription in subscriptions:
            panel_uuid = self._extract_panel_uuid(subscription)
            if not panel_uuid:
                continue
            grouped_subscriptions.setdefault(panel_uuid, []).append(subscription)
            if subscription.user and panel_uuid not in grouped_users:
                grouped_users[panel_uuid] = subscription.user

        unresolved_uuids = set(panel_uuids_list) - set(grouped_subscriptions.keys())
        if unresolved_uuids:
            users_query = (
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .where(User.remnawave_uuid.in_(list(unresolved_uuids)))
            )
            users_result = await db.execute(users_query)
            users = users_result.scalars().unique().all()

            for user in users:
                panel_uuid = (user.remnawave_uuid or '').strip()
                if not panel_uuid:
                    continue

                grouped_subscriptions.setdefault(panel_uuid, []).extend(user.subscriptions or [])
                if panel_uuid not in grouped_users:
                    grouped_users[panel_uuid] = user

        for panel_uuid, subscriptions_for_uuid in grouped_subscriptions.items():
            if not subscriptions_for_uuid:
                continue

            user = grouped_users.get(panel_uuid) or next((sub.user for sub in subscriptions_for_uuid if sub.user), None)
            best_subscription = self._pick_best_subscription(subscriptions_for_uuid)
            subscription_statuses = tuple(sorted({sub.status for sub in subscriptions_for_uuid if sub.status}))
            active_paid_count = sum(
                1
                for sub in subscriptions_for_uuid
                if sub.status == SubscriptionStatus.ACTIVE.value and not bool(getattr(sub, 'is_trial', False))
            )
            active_trial_count = sum(
                1
                for sub in subscriptions_for_uuid
                if sub.status == SubscriptionStatus.ACTIVE.value and bool(getattr(sub, 'is_trial', False))
            )

            account = HwidConflictAccount(
                remnawave_uuid=panel_uuid,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else None,
                user_label=self._build_user_label(user) if user else 'Аккаунт не найден в БД бота',
                subscription_id=best_subscription.id if best_subscription else None,
                subscription_status=best_subscription.status if best_subscription else None,
                subscription_statuses=subscription_statuses,
                subscription_count=len(subscriptions_for_uuid),
                active_paid_count=active_paid_count,
                active_trial_count=active_trial_count,
                tariff_name=best_subscription.tariff.name if best_subscription and best_subscription.tariff else None,
            )
            self._upsert_best_account(metadata, account)

        return metadata

    def _resolve_panel_uuid_for_device(
        self,
        device: dict[str, Any],
        user_id_to_uuid: dict[int, str],
        unresolved_user_ids: set[int],
    ) -> str:
        """
        Возвращает UUID панели для HWID-девайса.

        Приоритет:
        1) userId -> uuid через карту
        2) старые поля userUuid/userUUID/user_uuid, если они ещё приходят
        """
        user_id = self._extract_panel_user_id(device)
        if user_id is not None:
            panel_uuid = user_id_to_uuid.get(user_id)
            if panel_uuid:
                return panel_uuid
            unresolved_user_ids.add(user_id)

        return self._extract_panel_user_uuid(device)

    def _collect_user_ids(self, devices: list[dict[str, Any]]) -> set[int]:
        user_ids: set[int] = set()
        for device in devices:
            user_id = self._extract_panel_user_id(device)
            if user_id is not None:
                user_ids.add(user_id)
        return user_ids

    def _upsert_best_account(
        self,
        metadata: dict[str, HwidConflictAccount],
        candidate: HwidConflictAccount,
    ) -> None:
        existing = metadata.get(candidate.remnawave_uuid)
        if not existing or self._account_priority(candidate) > self._account_priority(existing):
            metadata[candidate.remnawave_uuid] = candidate

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
            value = device.get(key)
            user_id = self._normalize_int(value)
            if user_id is not None:
                return user_id

        # Если вдруг поле осталось в старом виде, но внутри лежит строка-число.
        value = device.get('userId')
        if value is not None:
            user_id = self._normalize_int(value)
            if user_id is not None:
                return user_id

        return None

    def _extract_panel_user_uuid(self, device: dict[str, Any]) -> str:
        for key in ('userUuid', 'userUUID', 'user_uuid'):
            value = self._normalize_str(device.get(key))
            if value:
                return value
        return ''

    def _extract_panel_uuid(self, subscription: Subscription) -> str:
        panel_uuid = self._normalize_str(subscription.remnawave_uuid)
        if panel_uuid:
            return panel_uuid
        if subscription.user:
            return self._normalize_str(subscription.user.remnawave_uuid)
        return ''

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