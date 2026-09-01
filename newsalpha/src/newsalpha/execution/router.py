"""Signal -> order routing.

Thin by design. It resolves the instrument, asks the risk engine, sends the entry,
confirms it actually filled, and hands the resulting position to the manager that
will close it. It holds no opinion of its own: strategy logic belongs in the
sentiment engine and limits belong in the risk engine.

Two steps here exist because skipping them produces silent, expensive failures:

* **Instrument resolution.** NSE filings carry a symbol, not a Dhan securityId.
  Without resolution the order is unroutable, and the failure looks like a quiet
  day rather than a bug.
* **Fill confirmation.** A broker ack is not a fill. Registering a position off
  the ack means the manager may later try to close shares that were never bought.

Every outcome - approved, rejected, unfilled - is journalled.
"""

from __future__ import annotations

import logging

from ..config import ExecutionConfig, RiskConfig
from ..ingest.instruments import Instrument, InstrumentMaster
from ..models import Direction, OrderAck, OrderIntent, Side, Signal, utcnow
from ..utils import Journal
from .base import Broker
from .positions import PositionManager
from .risk import RiskEngine

log = logging.getLogger(__name__)


class OrderRouter:
    def __init__(
        self,
        broker: Broker,
        risk: RiskEngine,
        exec_cfg: ExecutionConfig,
        risk_cfg: RiskConfig,
        journal: Journal | None = None,
        positions: PositionManager | None = None,
        instruments: InstrumentMaster | None = None,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._exec = exec_cfg
        self._risk_cfg = risk_cfg
        self._journal = journal
        self._positions = positions
        self._instruments = instruments

    async def handle(
        self, signal: Signal, price: float | None, security_id: str, segment: str
    ) -> OrderAck | None:
        instrument = self._resolve(signal.symbol, security_id, segment)
        if instrument is None and not security_id:
            log.info("skip %s: no securityId and symbol not in instrument master", signal.symbol)
            self._record(signal, "unresolved instrument", None, price)
            return None

        if instrument is not None and not instrument.tradable_intraday:
            # Trade-to-trade series settle without intraday netting, so an
            # intraday strategy cannot exit the same day. Not tradable here.
            log.info(
                "skip %s: series %s is not intraday-tradable", signal.symbol, instrument.series
            )
            self._record(signal, f"series {instrument.series} not intraday", None, price)
            return None

        decision = self._risk.evaluate(signal, price)
        if not decision.approved:
            log.info("skip %s: %s", signal.symbol, decision.reason)
            self._record(signal, decision.reason, None, price)
            return None

        assert price is not None  # guaranteed by the risk engine's price gate

        quantity = decision.quantity
        order_price = price
        if instrument is not None:
            quantity = instrument.round_quantity(quantity)
            order_price = instrument.round_price(price)
            if quantity <= 0:
                reason = f"sized below one lot ({instrument.lot_size})"
                log.info("skip %s: %s", signal.symbol, reason)
                self._record(signal, reason, None, price)
                return None

        intent = OrderIntent(
            uid=signal.uid,
            symbol=signal.symbol,
            security_id=instrument.security_id if instrument else security_id,
            exchange_segment=instrument.exchange_segment if instrument else segment,
            side=Side.BUY if signal.direction is Direction.BULLISH else Side.SELL,
            quantity=quantity,
            order_type=self._exec.order_type,
            product_type=self._exec.product_type,
            price=order_price,
            stop_loss_pct=self._risk_cfg.stop_loss_pct,
            take_profit_pct=self._risk_cfg.take_profit_pct,
            tag=signal.uid[:20],
        )

        self._risk.on_order_sent()
        ack = await self._broker.place(intent)
        if ack.ok:
            ack = await self._broker.confirm(ack, self._exec.fill_confirm_timeout_s)

        if ack.ok:
            fill_price = ack.avg_price or order_price
            filled = ack.filled_quantity or quantity
            self._risk.on_fill(signal, filled, fill_price)
            if self._positions is not None:
                self._positions.register(intent, ack)
        else:
            self._risk.on_reject(ack.error or ack.status)

        self._record(signal, ack.status, ack, price, intent)
        return ack

    def _resolve(self, symbol: str, security_id: str, segment: str) -> Instrument | None:
        if self._instruments is None:
            return None
        return self._instruments.enrich(symbol, security_id, segment)

    def _record(
        self,
        signal: Signal,
        outcome: str,
        ack: OrderAck | None,
        price: float | None,
        intent: OrderIntent | None = None,
    ) -> None:
        if not self._journal:
            return
        self._journal.write(
            {
                "type": "decision",
                "at": utcnow(),
                "uid": signal.uid,
                "symbol": signal.symbol,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "materiality": signal.materiality,
                "rationale": signal.rationale,
                "engine": signal.engine,
                "reference_price": price,
                "outcome": outcome,
                "security_id": intent.security_id if intent else "",
                "quantity": intent.quantity if intent else 0,
                "order_id": ack.order_id if ack else "",
                "fill_price": ack.avg_price if ack else 0.0,
                "risk": self._risk.snapshot(),
            }
        )
