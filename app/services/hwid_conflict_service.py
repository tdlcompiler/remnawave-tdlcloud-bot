from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
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


@dataclass(slots=True)
class HwidConflictAccount:
    remnawave_uuid: str
    user_id: int | None = None
    telegram_id: int | None = None
    user_label: str = 'Неизвестный аккаунт'
    subscription_id: int | None = None
    subscription_status: str | None = None
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
    telegram_targets: dict[int, set[str]] = field(default_factory=dict)

    @property
    def has_conflicts(self) -> bool:
        return self.duplicate_hwids > 0

    @property
    def telegram_targets_count(self) -> int:
        return len(self.telegram_targets)


class HwidConflictService:
    """Поиск конфликтов HWID (одно устройство в нескольких подписках)."""

    def __init__(self) -> None:
        self._remnawave_service = RemnaWaveService()

    async def scan_conflicts(self, db: AsyncSession) -> HwidConflictScanResult:
        async with self._remnawave_service.get_api_client() as api:
            devices_data = await api.get_all_hwid_devices()

        devices = devices_data.get('devices') or []
        hwid_to_panel_uuids: dict[str, set[str]] = {}

        for device in devices:
            hwid = self._extract_hwid(device)
            panel_uuid = self._extract_panel_user_uuid(device)

            if not hwid or not panel_uuid:
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
                unmatched_panel_users=0,
                conflicts=[],
                telegram_targets={},
            )

        all_conflict_uuids = {panel_uuid for panel_uuids in duplicate_hwid_map.values() for panel_uuid in panel_uuids}
        uuid_metadata = await self._load_uuid_metadata(db, all_conflict_uuids)
        unresolved_uuids = all_conflict_uuids - set(uuid_metadata.keys())

        conflicts: list[HwidConflict] = []
        telegram_targets: dict[int, set[str]] = {}

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
                if account.telegram_id:
                    telegram_targets.setdefault(account.telegram_id, set()).add(hwid)

            conflicts.append(HwidConflict(hwid=hwid, accounts=accounts))

        return HwidConflictScanResult(
            scanned_devices=len(devices),
            duplicate_hwids=len(duplicate_hwid_map),
            unique_panel_users_in_conflicts=len(all_conflict_uuids),
            unmatched_panel_users=len(unresolved_uuids),
            conflicts=conflicts,
            telegram_targets=telegram_targets,
        )

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
            .where(
                Subscription.remnawave_uuid.in_(panel_uuids_list),
                Subscription.status.in_(_ACTIVE_STATUSES),
            )
        )
        subscriptions_result = await db.execute(subscriptions_query)
        subscriptions = subscriptions_result.scalars().unique().all()

        for subscription in subscriptions:
            panel_uuid = (subscription.remnawave_uuid or '').strip()
            if not panel_uuid:
                continue

            user = subscription.user
            if not user:
                continue

            account = HwidConflictAccount(
                remnawave_uuid=panel_uuid,
                user_id=user.id,
                telegram_id=user.telegram_id,
                user_label=self._build_user_label(user),
                subscription_id=subscription.id,
                subscription_status=subscription.status,
                tariff_name=subscription.tariff.name if subscription.tariff else None,
            )
            self._upsert_best_account(metadata, account)

        unresolved_uuids = set(panel_uuids_list) - set(metadata.keys())
        if not unresolved_uuids:
            return metadata

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

            best_subscription = self._pick_best_subscription(user.subscriptions or [])
            account = HwidConflictAccount(
                remnawave_uuid=panel_uuid,
                user_id=user.id,
                telegram_id=user.telegram_id,
                user_label=self._build_user_label(user),
                subscription_id=best_subscription.id if best_subscription else None,
                subscription_status=best_subscription.status if best_subscription else None,
                tariff_name=best_subscription.tariff.name if best_subscription and best_subscription.tariff else None,
            )
            self._upsert_best_account(metadata, account)

        return metadata

    def _upsert_best_account(
        self,
        metadata: dict[str, HwidConflictAccount],
        candidate: HwidConflictAccount,
    ) -> None:
        existing = metadata.get(candidate.remnawave_uuid)
        if not existing or self._account_priority(candidate) > self._account_priority(existing):
            metadata[candidate.remnawave_uuid] = candidate

    def _account_priority(self, account: HwidConflictAccount) -> tuple[int, int, int, int]:
        return (
            _STATUS_PRIORITY.get(account.subscription_status or '', 0),
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

    def _extract_panel_user_uuid(self, device: dict[str, Any]) -> str:
        for key in ('userUuid', 'userUUID', 'user_uuid'):
            value = self._normalize_str(device.get(key))
            if value:
                return value
        return ''

    def _normalize_str(self, value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    def _safe_timestamp(self, value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value.timestamp())
        except Exception:
            return 0.0
