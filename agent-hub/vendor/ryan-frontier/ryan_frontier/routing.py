"""Deterministic provider routing plans, with no provider calls or reservations.

Model strength and maximum effort are explicit operator attestations. Model names
are never ranked. Subscription allowance and API cash are separate account types.
Usage is a frozen input: creating a plan does not consume, reserve, or refresh it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class RoutingConfigError(ValueError):
    pass


def _number(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise RoutingConfigError(f"{name} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RoutingConfigError(f"{name} must be numeric") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise RoutingConfigError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingConfigError(f"{name} must be a nonempty string")
    return value


def _names(value: Any, name: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise RoutingConfigError(f"{name} must be a list")
    return frozenset(_name(item, name) for item in value)


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise RoutingConfigError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True)
class AllowancePool:
    id: str
    capacity: Decimal
    used: Decimal
    weight: Decimal


@dataclass(frozen=True)
class CashPool:
    id: str
    budget: Decimal
    spent: Decimal


@dataclass(frozen=True)
class Provider:
    id: str
    kind: str
    model: str
    strongest_model: str
    effort: str
    highest_effort: str
    supported_efforts: frozenset[str]
    capabilities: frozenset[str]
    verified: bool
    enabled: bool = True
    allowance_pool: str | None = None
    cash_pool: str | None = None
    reserve_for_connection: bool = False


@dataclass(frozen=True)
class RoutingPolicy:
    providers: tuple[Provider, ...]
    allowance_pools: Mapping[str, AllowancePool]
    api_cash_pools: Mapping[str, CashPool]
    policy_hash: str
    _canonical_json: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowance_pools", MappingProxyType(dict(self.allowance_pools)))
        object.__setattr__(self, "api_cash_pools", MappingProxyType(dict(self.api_cash_pools)))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RoutingPolicy:
        try:
            canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            config = json.loads(canonical)
        except (TypeError, ValueError):
            raise RoutingConfigError("Policy must contain finite JSON values") from None
        if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise RoutingConfigError("Routing schema_version must be 1")
        allowances: dict[str, AllowancePool] = {}
        cash: dict[str, CashPool] = {}
        providers: list[Provider] = []
        for name in ("allowance_pools", "api_cash_pools", "providers"):
            if not isinstance(config.get(name, []), list):
                raise RoutingConfigError(f"{name} must be a list")
        for record in config.get("allowance_pools", []):
            if not isinstance(record, dict):
                raise RoutingConfigError("Allowance pools must be objects")
            key = _name(record.get("id"), "Allowance pool id")
            if key in allowances:
                raise RoutingConfigError(f"Duplicate allowance pool: {key}")
            allowances[key] = AllowancePool(
                key, _number(record.get("capacity"), "capacity", positive=True),
                _number(record.get("used", 0), "used"),
                _number(record.get("weight", 1), "weight", positive=True),
            )
        for record in config.get("api_cash_pools", []):
            if not isinstance(record, dict):
                raise RoutingConfigError("Cash pools must be objects")
            key = _name(record.get("id"), "Cash pool id")
            if key in cash:
                raise RoutingConfigError(f"Duplicate API cash pool: {key}")
            cash[key] = CashPool(key, _number(record.get("budget"), "budget"),
                                 _number(record.get("spent", 0), "spent"))
        seen: set[str] = set()
        for record in config.get("providers", []):
            if not isinstance(record, dict):
                raise RoutingConfigError("Providers must be objects")
            key = _name(record.get("id"), "Provider id")
            if key in seen:
                raise RoutingConfigError(f"Duplicate provider: {key}")
            seen.add(key)
            kind = record.get("kind")
            if kind not in {"subscription", "api", "local"}:
                raise RoutingConfigError(f"Unknown provider kind: {kind}")
            provider = Provider(
                id=key, kind=kind,
                model=_name(record.get("model"), "model"),
                strongest_model=_name(record.get("strongest_model"), "strongest_model"),
                effort=_name(record.get("effort"), "effort"),
                highest_effort=_name(record.get("highest_effort"), "highest_effort"),
                supported_efforts=_names(record.get("supported_efforts", []), "supported_efforts"),
                capabilities=_names(record.get("capabilities", []), "capabilities"),
                verified=_boolean(record.get("verified", False), "verified"),
                enabled=_boolean(record.get("enabled", True), "enabled"),
                allowance_pool=record.get("allowance_pool"),
                cash_pool=record.get("cash_pool"),
                reserve_for_connection=_boolean(record.get("reserve_for_connection", False), "reserve_for_connection"),
            )
            if provider.allowance_pool is not None and not isinstance(provider.allowance_pool, str):
                raise RoutingConfigError("allowance_pool must be a pool id")
            if provider.cash_pool is not None and not isinstance(provider.cash_pool, str):
                raise RoutingConfigError("cash_pool must be a pool id")
            if kind == "subscription" and (provider.allowance_pool not in allowances or provider.cash_pool is not None):
                raise RoutingConfigError("Subscription providers require an allowance pool and cannot spend API cash")
            if kind == "api" and (provider.cash_pool not in cash or provider.allowance_pool is not None):
                raise RoutingConfigError("API providers require a cash pool and cannot spend subscription allowance")
            if kind == "local" and (provider.allowance_pool is not None or provider.cash_pool is not None):
                raise RoutingConfigError("Local providers cannot reference remote funding pools")
            providers.append(provider)
        return cls(tuple(providers), allowances, cash,
                   hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical)

    @classmethod
    def from_file(cls, path: str | Path) -> RoutingPolicy:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)


@dataclass(frozen=True)
class RoutingRequest:
    capabilities: frozenset[str]
    allowance_units: Decimal | int | float = 1
    estimated_api_cost: Decimal | int | float | None = None
    allow_api_cash: bool = False
    include_connection_reserve: bool = False
    local_only: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.capabilities, str):
            raise RoutingConfigError("capabilities must be a collection, not one string")
        capabilities = frozenset(self.capabilities)
        if not capabilities or any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise RoutingConfigError("At least one explicit capability is required")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "allowance_units", _number(self.allowance_units, "allowance_units", positive=True))
        if self.estimated_api_cost is not None:
            object.__setattr__(self, "estimated_api_cost", _number(self.estimated_api_cost, "estimated_api_cost"))
        for flag in ("allow_api_cash", "include_connection_reserve", "local_only"):
            _boolean(getattr(self, flag), flag)


@dataclass(frozen=True)
class RoutingPlan:
    policy_hash: str
    provider_id: str | None
    model: str | None
    effort: str | None
    kind: str | None
    funding_pool: str | None
    normalized_utilization: str | None
    reasons: tuple[str, ...]

    @property
    def abstained(self) -> bool:
        return self.provider_id is None

    @property
    def selected(self) -> bool:
        return self.provider_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash, "abstained": self.abstained,
            "provider_id": self.provider_id, "model": self.model, "effort": self.effort,
            "kind": self.kind, "funding_pool": self.funding_pool,
            "normalized_utilization": self.normalized_utilization,
            "reasons": list(self.reasons),
            "execution": "plan_only_no_provider_call_or_funds_reserved",
        }


class RoutingPlanner:
    def __init__(self, policy: RoutingPolicy) -> None:
        self.policy = policy

    def plan(self, request: RoutingRequest) -> RoutingPlan:
        candidates: list[tuple[int, Decimal, str, Provider, str | None]] = []
        rejections: list[str] = []
        for provider in sorted(self.policy.providers, key=lambda item: item.id):
            reason: str | None = None
            score = Decimal(0)
            pool_id: str | None = None
            priority = {"subscription": 0, "local": 1, "api": 2}[provider.kind]
            if not provider.enabled:
                reason = "disabled"
            elif request.local_only and provider.kind != "local":
                reason = "local-only request"
            elif provider.reserve_for_connection and not request.include_connection_reserve:
                reason = "reserved for the connection"
            elif not provider.verified:
                reason = "strongest-model and highest-effort attestation is unverified"
            elif provider.model != provider.strongest_model:
                reason = "configured model differs from the attested strongest model"
            elif provider.effort != provider.highest_effort:
                reason = "configured effort would downgrade the attested highest effort"
            elif provider.highest_effort not in provider.supported_efforts:
                reason = "attested highest effort is unsupported"
            elif not request.capabilities <= provider.capabilities:
                reason = "requested capabilities are unverified or unsupported"
            elif provider.kind == "subscription":
                pool = self.policy.allowance_pools[provider.allowance_pool]
                pool_id = pool.id
                projected = pool.used + request.allowance_units
                if projected > pool.capacity:
                    reason = "subscription allowance exhausted"
                else:
                    # Equalize utilization relative to capacity and explicit weight.
                    score = projected / (pool.capacity * pool.weight)
            elif provider.kind == "api":
                cash = self.policy.api_cash_pools[provider.cash_pool]
                pool_id = cash.id
                if not request.allow_api_cash:
                    reason = "API cash spending is not enabled for this request"
                elif request.estimated_api_cost is None:
                    reason = "API cost estimate is missing"
                elif cash.spent + request.estimated_api_cost > cash.budget:
                    reason = "API cash budget exhausted"
                else:
                    score = (cash.spent + request.estimated_api_cost) / cash.budget if cash.budget else Decimal(0)
            if reason:
                rejections.append(f"{provider.id}: {reason}")
            else:
                candidates.append((priority, score, provider.id, provider, pool_id))
        if not candidates:
            return RoutingPlan(self.policy.policy_hash, None, None, None, None, None, None,
                               tuple(rejections) or ("No providers configured",))
        _, score, _, provider, pool_id = min(candidates, key=lambda item: item[:3])
        return RoutingPlan(self.policy.policy_hash, provider.id, provider.model, provider.effort,
                           provider.kind, pool_id, str(score), tuple(rejections))
