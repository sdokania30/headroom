"""Signal -> order routing.

Thin by design. It turns a signal into an intent, asks the risk engine, and hands
the result to a broker. It holds no opinion of its own: if you find yourself
adding strategy logic here, it belongs in the sentiment engine, and if you find
yourself adding limits here, they belong in the risk engine.

Every outcome - approved or rejected - is journalled before the order goes out.
"""

from __future__ import annotations

import logging

from ..config import ExecutionConfig, RiskConfig
from ..models import Direction, OrderAck, OrderIntent, Side, Signal, utcnow
from ..utils import Journal
from .base import Broker
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
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._exec = exec_cfg
        self._risk_cfg = risk_cfg
        self._journal = journal

    async def handle(
        self, signal: Signal, price: float | None, security_id: str, segment: str
    ) -> OrderAck | None:
        decision = self._risk.evaluate(signal, price)
        if not decision.approved:
            log.info("skip %s: %s", signal.symbol, decision.reason)
            self._record(signal, decision.reason, None, price)
            return None

        assert price is not None  # guaranteed by the risk engine's price gate
        intent = OrderIntent(
            uid=signal.uid,
            symbol=signal.symbol,
            security_id=security_id,
            exchange_segment=segment,
            side=Side.BUY if signal.direction is Direction.BULLISH else Side.SELL,
            quantity=decision.quantity,
            order_type=self._exec.order_type,
            product_type=self._exec.product_type,
            price=price,
            stop_loss_pct=self._risk_cfg.stop_loss_pct,
            take_profit_pct=self._risk_cfg.take_profit_pct,
            tag=signal.uid[:20],
        )

        self._risk.on_order_sent()
        ack = await self._broker.place(intent)

        if ack.ok:
            fill_price = ack.avg_price or price
            self._risk.on_fill(signal, ack.filled_quantity or decision.quantity, fill_price)
        else:
            self._risk.on_reject(ack.error or ack.status)

        self._record(signal, ack.status, ack, price, intent)
        return ack

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
                "quantity": intent.quantity if intent else 0,
                "order_id": ack.order_id if ack else "",
                "fill_price": ack.avg_price if ack else 0.0,
                "risk": self._risk.snapshot(),
            }
        )
