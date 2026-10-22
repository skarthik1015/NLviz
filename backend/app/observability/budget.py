"""Persistent monthly ledger with atomic reservation admission across workers.

Postgres uses the existing app pool; SQLite is a local single-host fallback.
No prompts, identities or credentials are persisted in either ledger.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from uuid import uuid4

from .pricing import money


class BudgetExceeded(RuntimeError):
    """Hosted-model monthly budget exhausted; local models remain available."""


@dataclass(frozen=True)
class Reservation:
    call_id: str
    month: str
    amount: Decimal


class SpendStore:
    def __init__(self, path: str | Path, *, pool=None, clock=None):
        self.pool = pool
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if pool is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS llm_spend_months ("
                        "month TEXT PRIMARY KEY, spent TEXT NOT NULL, reserved TEXT NOT NULL)")
            cur.execute("CREATE TABLE IF NOT EXISTS llm_spend_calls ("
                        "call_id TEXT PRIMARY KEY, month TEXT NOT NULL, amount TEXT NOT NULL, "
                        "settled INTEGER NOT NULL DEFAULT 0)")

    @contextmanager
    def _transaction(self):
        if self.pool is not None:
            with self.pool.acquire() as conn:
                with conn.cursor() as cur:
                    yield cur
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn.cursor()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _execute(self, cur, sql, params):
        cur.execute(sql.replace("?", "%s") if self.pool is not None else sql, params)

    def month(self) -> str:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Spend clock must be timezone aware")
        return now.astimezone(timezone.utc).strftime("%Y-%m")

    def _month_row(self, cur, month):
        self._execute(cur, "INSERT INTO llm_spend_months (month,spent,reserved) "
                      "VALUES (?, '0', '0') ON CONFLICT (month) DO NOTHING", (month,))
        self._execute(cur, "SELECT spent,reserved FROM llm_spend_months WHERE month=?" +
                      (" FOR UPDATE" if self.pool is not None else ""), (month,))
        row = cur.fetchone()
        return Decimal(row["spent"]), Decimal(row["reserved"])

    def reserve(self, amount: Decimal, cap: Decimal | None) -> Reservation:
        amount = money(amount)
        cap = None if cap is None else money(cap)
        reservation = Reservation(str(uuid4()), self.month(), amount)
        with self._transaction() as cur:
            spent, reserved = self._month_row(cur, reservation.month)
            if cap is not None and (spent + reserved >= cap or spent + reserved + amount > cap):
                raise BudgetExceeded("Hosted-model monthly budget exhausted; use a local model or increase the cap")
            self._execute(cur, "UPDATE llm_spend_months SET reserved=? WHERE month=?",
                          (str(reserved + amount), reservation.month))
            self._execute(cur, "INSERT INTO llm_spend_calls (call_id,month,amount) VALUES (?,?,?)",
                          (reservation.call_id, reservation.month, str(amount)))
        return reservation

    def settle(self, reservation: Reservation, actual: Decimal | None) -> bool:
        """Exactly once; missing usage conservatively charges the full reservation.

        An actual cost above the reservation is retained truthfully, even above cap.
        The integration must supply a valid token upper bound to prevent that case.
        """
        actual = reservation.amount if actual is None else money(actual)
        with self._transaction() as cur:
            spent, reserved = self._month_row(cur, reservation.month)
            self._execute(cur, "SELECT amount,settled FROM llm_spend_calls WHERE call_id=? AND month=?",
                          (reservation.call_id, reservation.month))
            row = cur.fetchone()
            if row is None:
                raise ValueError("Unknown spend reservation")
            if row["settled"]:
                return False
            self._execute(cur, "UPDATE llm_spend_months SET spent=?,reserved=? WHERE month=?",
                          (str(spent + actual), str(reserved - Decimal(row["amount"])), reservation.month))
            self._execute(cur, "UPDATE llm_spend_calls SET settled=1 WHERE call_id=?", (reservation.call_id,))
        return True

    def snapshot(self) -> dict[str, str]:
        month = self.month()
        with self._transaction() as cur:
            spent, reserved = self._month_row(cur, month)
        return {"month": month, "spent_usd": str(spent), "reserved_usd": str(reserved)}
