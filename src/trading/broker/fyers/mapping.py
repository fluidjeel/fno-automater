"""Map between canonical Layer 2 contracts and Fyers v3 payloads."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trading.broker.ports import BrokerError, BrokerFunds
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.order import OrderCommand, OrderEvent, OrderIdentity
from trading.domain.contracts.portfolio import PendingOrderSummary, PositionRecord
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
    OptionType,
    OrderState,
    OrderType,
    ReasonCode,
    Side,
    TimeInForce,
)
from trading.domain.primitives import Currency, Money, Price, TickSize

_INR = Currency.INR

__all__ = [
    "build_place_order_payload",
    "contract_from_fyers_symbol",
    "map_fyers_order_state",
    "parse_funds",
    "parse_order_event",
    "parse_pending_order",
    "parse_positions",
    "to_fyers_symbol",
]

_FYERS_PREFIX: dict[Exchange, str] = {
    Exchange.NSE: "NSE",
    Exchange.NFO: "NSE",
    Exchange.MCX: "MCX",
}

_FYERS_ORDER_TYPE: dict[OrderType, int] = {
    OrderType.LIMIT: 1,
    OrderType.MARKET: 2,
    OrderType.STOP: 3,
    OrderType.STOP_LIMIT: 4,
}

_FYERS_VALIDITY: dict[TimeInForce, str] = {
    TimeInForce.DAY: "DAY",
    TimeInForce.IOC: "IOC",
}

# Fyers order status codes (verified against official enum catalog).
_STATUS_CANCELLED = 1
_STATUS_TRADED = 2
_STATUS_TRANSIT = 4
_STATUS_REJECTED = 5
_STATUS_PENDING = 6
_STATUS_EXPIRED = 7


def to_fyers_symbol(contract: ContractRef) -> str:
    """Resolve the Fyers trading symbol for one contract."""
    if contract.broker_token is not None:
        return contract.broker_token
    prefix = _FYERS_PREFIX.get(contract.exchange)
    if prefix is None:
        raise BrokerError(f"unsupported exchange for Fyers: {contract.exchange}")
    return f"{prefix}:{contract.symbol}"


def contract_from_fyers_symbol(
    symbol: str,
    *,
    fy_token: str | None = None,
) -> ContractRef:
    """Best-effort ContractRef from a broker-reported symbol."""
    if ":" not in symbol:
        raise BrokerError(f"invalid Fyers symbol: {symbol}")
    prefix, local = symbol.split(":", 1)
    broker_token = fy_token or symbol
    if prefix == "MCX":
        exchange = Exchange.MCX
        asset_class = AssetClass.COMMODITY
    elif local.endswith(("CE", "PE")):
        exchange = Exchange.NFO
        asset_class = AssetClass.EQUITY_INDEX
    elif "FUT" in local:
        exchange = Exchange.NFO if prefix == "NSE" else Exchange.MCX
        asset_class = (
            AssetClass.EQUITY_INDEX
            if exchange is Exchange.NFO
            else AssetClass.COMMODITY
        )
    else:
        exchange = Exchange.NSE
        asset_class = AssetClass.EQUITY_STOCK
    option_type = (
        OptionType.CALL
        if local.endswith("CE")
        else OptionType.PUT
        if local.endswith("PE")
        else None
    )
    instrument_kind = (
        InstrumentKind.OPTION
        if option_type is not None
        else InstrumentKind.FUTURE
        if "FUT" in local
        else InstrumentKind.STOCK
    )
    expiry: date | None = None
    strike: Decimal | None = None
    if option_type is not None:
        parsed_strike, parsed_expiry = _parse_option_symbol(local)
        if parsed_strike is None or parsed_expiry is None:
            raise BrokerError(f"cannot parse option symbol: {symbol}")
        strike = parsed_strike
        expiry = parsed_expiry
    elif instrument_kind is InstrumentKind.FUTURE:
        expiry = _parse_future_expiry(local)
    return ContractRef(
        exchange=exchange,
        symbol=local,
        instrument_kind=instrument_kind,
        asset_class=asset_class,
        underlying=_underlying_from_symbol(local),
        broker_token=broker_token,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
    )


def build_place_order_payload(
    command: OrderCommand,
    *,
    client_order_id: str,
    product_type: str,
    offline_order: bool = False,
) -> dict[str, object]:
    """Build a Fyers `/orders/sync` place-order body."""
    if command.order_type not in _FYERS_ORDER_TYPE:
        raise BrokerError(f"unsupported order type: {command.order_type}")
    payload: dict[str, object] = {
        "symbol": to_fyers_symbol(command.contract),
        "qty": command.quantity_contracts,
        "type": _FYERS_ORDER_TYPE[command.order_type],
        "side": 1 if command.side is Side.BUY else -1,
        "productType": product_type,
        "limitPrice": _price_as_float(command.limit_price),
        "stopPrice": _price_as_float(command.trigger_price),
        "disclosedQty": 0,
        "validity": _FYERS_VALIDITY[command.time_in_force],
        "offlineOrder": offline_order,
        "orderTag": client_order_id,
        "isSliceOrder": False,
    }
    return payload


def map_fyers_order_state(
    status: int,
    *,
    filled_qty: int,
    requested_qty: int,
) -> OrderState:
    """Map a Fyers status code to canonical OrderState."""
    if status == _STATUS_TRADED:
        if filled_qty <= 0:
            return OrderState.ACKNOWLEDGED
        return (
            OrderState.PARTIAL
            if filled_qty < requested_qty
            else OrderState.FILLED
        )
    terminal = {
        _STATUS_PENDING: OrderState.ACKNOWLEDGED,
        _STATUS_TRANSIT: OrderState.SUBMITTING,
        _STATUS_CANCELLED: OrderState.CANCELLED,
        _STATUS_REJECTED: OrderState.REJECTED,
        _STATUS_EXPIRED: OrderState.EXPIRED,
    }
    return terminal.get(status, OrderState.UNKNOWN)


def parse_order_event(
    row: dict[str, Any],
    *,
    identity: OrderIdentity,
    command: OrderCommand,
    attempt_number: int,
    event_id: str,
    received_at: datetime,
    sent_at: datetime | None = None,
) -> OrderEvent:
    """Map one Fyers orderbook row to an OrderEvent."""
    status = _int_field(row, "status")
    filled_qty = _int_field(row, "filledQty")
    requested_qty = command.quantity_contracts
    state = map_fyers_order_state(
        status,
        filled_qty=filled_qty,
        requested_qty=requested_qty,
    )
    traded_price = row.get("tradedPrice")
    average_fill_price: Price | None = None
    if filled_qty > 0 and traded_price is not None:
        tick = command.limit_price.tick if command.limit_price is not None else None
        average_fill_price = _price_from_broker_value(traded_price, tick=tick)
    broker_order_id = _text_field(row, "id")
    broker_time = _parse_broker_time(row.get("orderDateTime"))
    reason_code = (
        ReasonCode.BROKER_REJECTED
        if state is OrderState.REJECTED
        else ReasonCode.ORDER_TIMEOUT
        if state is OrderState.UNKNOWN
        else None
    )
    acknowledged = (
        requested_qty
        if state.is_working
        else min(filled_qty, requested_qty)
    )
    return OrderEvent.model_validate(
        {
            "event_id": event_id,
            "identity": {
                **identity.model_dump(mode="python"),
                "broker_order_id": broker_order_id,
            },
            "command": command.model_dump(mode="python"),
            "state": state,
            "attempt_number": attempt_number,
            "acknowledged_quantity": acknowledged,
            "filled_quantity": filled_qty,
            "average_fill_price": average_fill_price,
            "sent_at": sent_at,
            "received_at": received_at,
            "broker_time": broker_time,
            "raw_broker_status": str(status),
            "raw_payload_ref": f"fyers://orders/{broker_order_id}",
            "reason_code": reason_code,
            "reason_detail": _text_field(row, "message"),
        }
    )


def parse_pending_order(row: dict[str, Any]) -> PendingOrderSummary:
    """Map one working Fyers orderbook row to PendingOrderSummary."""
    symbol = _text_field(row, "symbol")
    fy_token = row.get("fyToken")
    contract = contract_from_fyers_symbol(
        symbol,
        fy_token=fy_token if isinstance(fy_token, str) else None,
    )
    side = Side.BUY if _int_field(row, "side") == 1 else Side.SELL
    status = _int_field(row, "status")
    filled_qty = _int_field(row, "filledQty")
    qty = _int_field(row, "qty")
    state = map_fyers_order_state(
        status,
        filled_qty=filled_qty,
        requested_qty=qty,
    )
    outstanding = max(qty - filled_qty, 0)
    client_id = _text_field(row, "client_id", default="")
    return PendingOrderSummary(
        internal_order_id=client_id or _text_field(row, "id"),
        intent_id=client_id or _text_field(row, "id"),
        contract=contract,
        side=side,
        quantity_contracts=outstanding if outstanding > 0 else qty,
        state=state,
    )


def parse_funds(
    payload: dict[str, Any],
    *,
    account_id: str,
    as_of: datetime,
) -> BrokerFunds:
    """Map Fyers `/funds` response to BrokerFunds."""
    fund_limit = payload.get("fund_limit")
    if not isinstance(fund_limit, list):
        raise BrokerError("funds response missing fund_limit")
    by_id: dict[int, Decimal] = {}
    for row in fund_limit:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        amount = row.get("equityAmount", row.get("amount"))
        if isinstance(row_id, int) and not isinstance(row_id, bool):
            by_id[row_id] = _decimal_from_json(amount)
    equity = by_id.get(1)
    utilized = by_id.get(2)
    available = by_id.get(10)
    if equity is None or utilized is None or available is None:
        raise BrokerError("funds response missing required fund_limit ids")
    return BrokerFunds(
        account_id=account_id,
        as_of=as_of,
        equity=Money.of(str(equity), _INR),
        margin_used=Money.of(str(utilized), _INR),
        margin_available=Money.of(str(available), _INR),
    )


def parse_positions(
    payload: dict[str, Any],
    *,
    strategy_id: str,
    as_of: datetime,
) -> tuple[PositionRecord, ...]:
    """Map Fyers `/positions` netPositions to canonical position records."""
    rows = payload.get("netPositions")
    if not isinstance(rows, list):
        raise BrokerError("positions response missing netPositions")
    positions: list[PositionRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        net_qty = _int_field(row, "netQty")
        if net_qty == 0:
            continue
        side_code = _int_field(row, "side")
        if side_code == 0:
            continue
        side = Side.BUY if side_code == 1 else Side.SELL
        symbol = _text_field(row, "symbol")
        fy_token = row.get("fyToken")
        contract = contract_from_fyers_symbol(
            symbol,
            fy_token=fy_token if isinstance(fy_token, str) else None,
        )
        avg = row.get("netAvg", row.get("avgPrice"))
        average_price = _price_from_broker_value(avg, tick=TickSize.of("0.05"))
        unrealized = row.get("unrealized_profit", row.get("pl", 0))
        position_id = _text_field(row, "id", default=symbol)
        positions.append(
            PositionRecord(
                trade_id=f"FYERS:{position_id}",
                strategy_id=strategy_id,
                contract=contract,
                side=side,
                quantity_contracts=abs(net_qty),
                average_price=average_price,
                unrealized_pnl=Money.of(str(_decimal_from_json(unrealized)), _INR),
            )
        )
    return tuple(positions)


_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_OPTION_COMPACT = re.compile(r"^([A-Z]+)(\d{2})(\d{1,2})(\d{2})(\d+)$")
_OPTION_MONTH = re.compile(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d+)$")


def _parse_option_symbol(local: str) -> tuple[Decimal | None, date | None]:
    """Parse strike and expiry from common Fyers option symbol encodings."""
    body = local[:-2] if local.endswith(("CE", "PE")) else local
    compact = _OPTION_COMPACT.match(body)
    if compact is not None:
        yy, month, day, strike_text = (
            compact.group(2),
            int(compact.group(3)),
            int(compact.group(4)),
            compact.group(5),
        )
        return Decimal(strike_text), date(2000 + int(yy), month, day)
    month_match = _OPTION_MONTH.match(body)
    if month_match is not None:
        yy = month_match.group(2)
        month_name = month_match.group(3)
        strike_text = month_match.group(4)
        month_num = _MONTHS.get(month_name)
        if month_num is None:
            return None, None
        # Month-name symbols omit the day; use the 1st as a placeholder identity.
        return Decimal(strike_text), date(2000 + int(yy), month_num, 1)
    return None, None


def _parse_future_expiry(local: str) -> date | None:
    """Parse expiry from symbols like NIFTY26SEPFUT or CRUDEOIL26OCTFUT."""
    match = re.search(r"(\d{2})([A-Z]{3})FUT$", local)
    if match is None:
        return None
    month_num = _MONTHS.get(match.group(2))
    if month_num is None:
        return None
    return date(2000 + int(match.group(1)), month_num, 1)


def _underlying_from_symbol(symbol: str) -> str:
    for suffix in ("CE", "PE", "FUT"):
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)].rstrip("0123456789")
    if symbol.endswith("-EQ"):
        return symbol[: -len("-EQ")]
    return symbol


def _price_as_float(price: Price | None) -> float:
    if price is None:
        return 0.0
    return float(price.value)


def _price_from_broker_value(value: object, *, tick: TickSize | None = None) -> Price:
    amount = _decimal_from_json(value)
    resolved_tick = tick if tick is not None else _infer_tick(amount)
    return Price.snap(amount, resolved_tick)


def _infer_tick(amount: Decimal) -> TickSize:
    exponent = amount.normalize().as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if places == 0:
        return TickSize.of("1")
    return TickSize.of(str(Decimal(1).scaleb(-places)))


def _decimal_from_json(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        return Decimal(0)
    if isinstance(value, (int, float, Decimal, str)):
        return Decimal(str(value))
    raise BrokerError(f"invalid numeric broker value: {value!r}")


def _int_field(row: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = row.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    return int(Decimal(str(value)))


def _text_field(row: dict[str, Any], key: str, *, default: str = "") -> str:
    value = row.get(key, default)
    return value if isinstance(value, str) and value else default


def _parse_broker_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
