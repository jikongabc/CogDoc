from __future__ import annotations

from collections.abc import Callable

from cogdoc.connectors.base import SourceConnector


class ConnectorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., SourceConnector]] = {}

    def register(
        self, connector_type: str, factory: Callable[..., SourceConnector]
    ) -> None:
        key = str(connector_type or "").strip().casefold()
        if not key:
            raise ValueError("connector_type is required")
        if key in self._factories:
            raise ValueError(f"connector already registered: {key}")
        self._factories[key] = factory

    def create(self, connector_type: str, **config) -> SourceConnector:
        key = str(connector_type or "").strip().casefold()
        try:
            factory = self._factories[key]
        except KeyError as exc:
            raise ValueError(f"unsupported connector type: {key}") from exc
        connector = factory(**config)
        if str(connector.connector_type).casefold() != key:
            raise ValueError("connector factory returned a mismatched type")
        return connector

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
