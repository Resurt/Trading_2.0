from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from trade_core.broker_gateway import (
    InstrumentRef,
    OrderPlacementRequest,
    OrderStateRequest,
)
from trade_core.infra.tbank.config import TBankBrokerConfig, TBankEnvironment
from trade_core.infra.tbank.deadlines import deadline_for
from trade_core.infra.tbank.errors import (
    BrokerErrorKind,
    BrokerGatewayError,
    map_error_info,
)
from trade_core.infra.tbank.gateway import TBankBrokerGateway
from trade_core.infra.tbank.headers import capture_response_headers
from trade_core.infra.tbank.idempotency import OrderIdempotencyStore
from trade_core.infra.tbank.protocols import JsonPayload, UnaryCallResult
from trade_core.infra.tbank.retry import ExponentialBackoff
from trade_core.infra.tbank.secrets import (
    FULL_ACCESS_TOKEN_ENV,
    FULL_ACCESS_TOKEN_FILE_ENV,
    LEGACY_DEV_TOKEN_ENV,
    READONLY_TOKEN_ENV,
    READONLY_TOKEN_FILE_ENV,
    TBankTokenBundle,
    load_tbank_tokens,
)


class FakeBrokerException(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: str | None = None,
        error_code: int | None = None,
        headers: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.headers = headers or {}


class FakeUnaryClient:
    def __init__(self, results: list[UnaryCallResult | Exception]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    async def call_unary(
        self,
        method_name: str,
        payload: JsonPayload,
        *,
        metadata: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> UnaryCallResult:
        self.calls.append(
            {
                "method_name": method_name,
                "payload": payload,
                "metadata": metadata,
                "timeout_seconds": timeout_seconds,
            }
        )
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def config(max_retry_attempts: int = 3) -> TBankBrokerConfig:
    return TBankBrokerConfig(
        environment=TBankEnvironment.SANDBOX,
        max_retry_attempts=max_retry_attempts,
        backoff_initial_seconds=0.0,
        backoff_max_seconds=0.0,
    )


def tokens() -> TBankTokenBundle:
    return TBankTokenBundle(
        full_access_token="full-access-token-for-tests",
        readonly_token="readonly-token-for-tests",
    )


def instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="sber-instrument-uid",
        ticker="SBER",
        class_code="TQBR",
    )


def test_secret_loading_prefers_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    full_token_file = tmp_path / "full"
    readonly_token_file = tmp_path / "readonly"
    full_token_file.write_text("full-from-file\n", encoding="utf-8")
    readonly_token_file.write_text("readonly-from-file\n", encoding="utf-8")

    monkeypatch.setenv(FULL_ACCESS_TOKEN_FILE_ENV, str(full_token_file))
    monkeypatch.setenv(READONLY_TOKEN_FILE_ENV, str(readonly_token_file))
    monkeypatch.setenv(FULL_ACCESS_TOKEN_ENV, "full-from-env")
    monkeypatch.setenv(READONLY_TOKEN_ENV, "readonly-from-env")
    monkeypatch.delenv(LEGACY_DEV_TOKEN_ENV, raising=False)

    bundle = load_tbank_tokens()

    assert bundle.full_access_token == "full-from-file"
    assert bundle.readonly_token == "readonly-from-file"


def test_secret_loading_supports_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FULL_ACCESS_TOKEN_FILE_ENV, "missing-full-file")
    monkeypatch.setenv(READONLY_TOKEN_FILE_ENV, "missing-readonly-file")
    monkeypatch.setenv(FULL_ACCESS_TOKEN_ENV, "full-from-env")
    monkeypatch.setenv(READONLY_TOKEN_ENV, "readonly-from-env")
    monkeypatch.delenv(LEGACY_DEV_TOKEN_ENV, raising=False)

    bundle = load_tbank_tokens()

    assert bundle.full_access_token == "full-from-env"
    assert bundle.readonly_token == "readonly-from-env"


def test_retry_logic_retries_transient_broker_errors() -> None:
    fake_client = FakeUnaryClient(
        [
            FakeBrokerException("temporary", status_code="UNAVAILABLE"),
            UnaryCallResult(data={"ok": True}),
        ]
    )
    gateway = TBankBrokerGateway(
        config=config(max_retry_attempts=2),
        tokens=tokens(),
        unary_client=fake_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    response = asyncio.run(
        gateway.get_order_state(
            OrderStateRequest(account_id="account-1", request_order_id=None, exchange_order_id="42")
        )
    )

    assert response.data == {"ok": True}
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0]["timeout_seconds"] == deadline_for("GetOrderState").seconds


def test_idempotent_order_id_generation_reuses_uuid_for_client_key() -> None:
    fake_client = FakeUnaryClient(
        [
            UnaryCallResult(data={"status": "posted"}),
            UnaryCallResult(data={"status": "posted"}),
        ]
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=fake_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )
    request = OrderPlacementRequest(
        account_id="account-1",
        instrument=instrument(),
        side="buy",
        order_type="limit",
        lot_qty=1,
        price=Decimal("300.10"),
        time_in_force="day",
        client_order_key="strategy-run-1:SBER:entry-1",
    )

    asyncio.run(gateway.post_order(request))
    asyncio.run(gateway.post_order(request))

    first_payload = fake_client.calls[0]["payload"]
    second_payload = fake_client.calls[1]["payload"]
    assert isinstance(first_payload, dict)
    assert isinstance(second_payload, dict)
    first_order_id = first_payload["request_order_id"]
    second_order_id = second_payload["request_order_id"]
    assert first_order_id == second_order_id
    assert str(UUID(str(first_order_id))) == first_order_id
    assert fake_client.calls[0]["timeout_seconds"] == deadline_for("PostOrder").seconds


def test_header_capture_normalizes_service_headers() -> None:
    headers = capture_response_headers(
        {
            "X-Tracking-Id": "tracking-1",
            "x-app-name": "Resurt.Trading_2_0",
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": ["99"],
            "x-ratelimit-reset": "60",
        }
    )

    assert headers.tracking_id == "tracking-1"
    assert headers.app_name == "Resurt.Trading_2_0"
    assert headers.ratelimit_limit == "100"
    assert headers.ratelimit_remaining == "99"
    assert headers.ratelimit_reset == "60"


def test_gateway_response_contains_captured_headers() -> None:
    fake_client = FakeUnaryClient(
        [
            UnaryCallResult(
                data={"orders": []},
                headers={
                    "x-tracking-id": "tracking-2",
                    "x-ratelimit-limit": "100",
                    "x-ratelimit-remaining": "98",
                    "x-ratelimit-reset": "30",
                },
            )
        ]
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=fake_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    response = asyncio.run(
        gateway.get_order_state(
            OrderStateRequest(account_id="account-1", request_order_id=None, exchange_order_id="42")
        )
    )

    assert response.headers["x_tracking_id"] == "tracking-2"
    assert response.headers["x_ratelimit_remaining"] == "98"


def test_error_mapping_maps_tbank_codes_and_statuses() -> None:
    auth_error = map_error_info(None, 40003)
    rate_limit = map_error_info("RESOURCE_EXHAUSTED", 80002)
    unavailable = map_error_info("UNAVAILABLE", None)

    assert auth_error.kind is BrokerErrorKind.UNAUTHENTICATED
    assert not auth_error.retryable
    assert rate_limit.kind is BrokerErrorKind.RESOURCE_EXHAUSTED
    assert rate_limit.retryable
    assert unavailable.kind is BrokerErrorKind.UNAVAILABLE
    assert unavailable.retryable


def test_gateway_maps_non_retryable_errors() -> None:
    fake_client = FakeUnaryClient(
        [
            FakeBrokerException(
                "invalid request",
                status_code="INVALID_ARGUMENT",
                error_code=30028,
                headers={"x-tracking-id": "tracking-error"},
            )
        ]
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=fake_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    with pytest.raises(BrokerGatewayError) as exc_info:
        asyncio.run(
            gateway.get_order_state(
                OrderStateRequest(
                    account_id="account-1",
                    request_order_id=None,
                    exchange_order_id="bad",
                )
            )
        )

    assert exc_info.value.kind is BrokerErrorKind.INVALID_ARGUMENT
    assert not exc_info.value.retryable
    assert exc_info.value.headers.tracking_id == "tracking-error"
    assert len(fake_client.calls) == 1


def test_stream_gap_recovery_backfills_recent_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeUnaryClient([UnaryCallResult(data={"candles": [{"close_price": "1"}]})])
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=fake_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )
    monkeypatch.setenv("TBANK_STREAM_INSTRUMENT_IDS", "uid-sber")
    monkeypatch.setenv("TBANK_GAP_RECOVERY_TIMEFRAMES", "5m")
    monkeypatch.setenv("TBANK_GAP_RECOVERY_LOOKBACK_MINUTES", "5")

    asyncio.run(gateway.recover_after_stream_gap("candles"))

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["method_name"] == "GetCandles"
    payload = fake_client.calls[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["interval"] == "5m"
    assert payload["instrument"] == {
        "instrument_id": "uid-sber",
        "instrument_uid": None,
        "class_code": None,
        "ticker": None,
    }


def test_order_stream_gap_recovery_refreshes_open_and_known_orders() -> None:
    request_order_id = uuid4()
    idempotency_store = OrderIdempotencyStore()
    idempotency_store.remember("candidate-1:entry", request_order_id)
    fake_client = FakeUnaryClient(
        [
            UnaryCallResult(data={"orders": []}),
            UnaryCallResult(data={"status": "observed"}),
        ]
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=fake_client,
        idempotency_store=idempotency_store,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    asyncio.run(gateway.recover_after_stream_gap("OrderStateStream", account_id="account-1"))

    assert [call["method_name"] for call in fake_client.calls] == [
        "GetOrders",
        "GetOrderState",
    ]
    state_payload = fake_client.calls[1]["payload"]
    assert isinstance(state_payload, dict)
    assert state_payload["account_id"] == "account-1"
    assert state_payload["request_order_id"] == str(request_order_id)
