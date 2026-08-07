"""Company-funded payroll: shift settlement, unpaid wages and repayment (R29).

Owns every coin movement between a company account and an employee:

- ``settle_shift`` pays one shift's ``wage_due`` all-or-nothing: company money
  >= due -> paid (dual ledgers, shared trace_id); otherwise the whole due is
  recorded as unpaid on both the contract and the company (never partial).
- ``repay_contract`` pays back as much accumulated unpaid wage as the company
  currently holds, in the same atomic transaction as the shift settlement.

The service never creates events itself beyond ``wage_repaid`` (the caller
publishes ``wage_paid`` / ``wage_unpaid`` with the shift context); all rows
commit with the caller's transaction.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyTransaction,
    EmploymentContract,
    WorkShift,
)
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.world_engine.engine import WorldEngine


class PayrollService:
    def __init__(self, engine: WorldEngine) -> None:
        self.engine = engine

    def settle_shift(
            self,
            session: Session,
            world: World,
            shift: WorkShift,
            contract: EmploymentContract,
            company: Company,
            agent: Agent,
            wage_due: int,
            trace_id: str,
    ) -> str:
        """Pay ``wage_due`` from company to agent; return wage_paid/wage_unpaid.

        Idempotent per shift: a shift whose payroll_status is already
        ``paid``/``unpaid`` is never touched again.
        """
        if shift.payroll_status != "not_due":
            return "wage_paid" if shift.payroll_status == "paid" else "wage_unpaid"
        if wage_due <= 0:
            shift.payroll_status = "paid"
            shift.wage_paid = 0
            return "wage_paid"
        if company.money >= wage_due:
            company.money -= wage_due
            agent.money += wage_due
            shift.wage_paid = wage_due
            shift.payroll_status = "paid"
            company.consecutive_loss_days = 0
            session.add(CompanyTransaction(
                world_id=world.world_id, company_id=company.company_id, type="wage_payment",
                amount=-wage_due, balance_after=company.money, related_agent_id=agent.agent_id,
                reference_type="shift", reference_id=shift.shift_id, reason="正式班次工资",
                world_time=world.world_time, trace_id=trace_id,
            ))
            session.add(Transaction(
                world_id=world.world_id, agent_id=agent.agent_id, type="work_wage",
                amount=wage_due, balance_after=agent.money, item_id=None, quantity=None,
                reason=f"{company.name}正式班次工资", world_time=world.world_time, trace_id=trace_id,
            ))
            logger.info(
                "Payroll completed world={} shift={} company={} agent={} due={} paid={} status=paid trace={}",
                world.world_id, shift.shift_id, company.company_id, agent.agent_id,
                wage_due, wage_due, trace_id,
            )
            return "wage_paid"
        shift.payroll_status = "unpaid"
        contract.unpaid_wage += wage_due
        company.unpaid_wage_total += wage_due
        company.consecutive_loss_days += 1
        logger.info(
            "Payroll unpaid world={} shift={} company={} agent={} due={} paid=0 status=unpaid trace={}",
            world.world_id, shift.shift_id, company.company_id, agent.agent_id, wage_due, trace_id,
        )
        return "wage_unpaid"

    def repay_contract(
            self,
            session: Session,
            world: World,
            contract: EmploymentContract,
            company: Company,
            agent: Agent,
            trace_id: str,
    ) -> int:
        """Repay min(contract.unpaid_wage, company.money); return amount paid.

        Runs inside the caller's transaction; publishes ``wage_repaid``.
        """
        amount = min(contract.unpaid_wage, company.money)
        if amount <= 0:
            return 0
        contract.unpaid_wage -= amount
        company.unpaid_wage_total -= amount
        company.money -= amount
        agent.money += amount
        session.add(CompanyTransaction(
            world_id=world.world_id, company_id=company.company_id, type="wage_payment",
            amount=-amount, balance_after=company.money, related_agent_id=agent.agent_id,
            reference_type="employment", reference_id=contract.employment_id,
            reason="补发欠薪", world_time=world.world_time, trace_id=trace_id,
        ))
        session.add(Transaction(
            world_id=world.world_id, agent_id=agent.agent_id, type="work_wage",
            amount=amount, balance_after=agent.money, item_id=None, quantity=None,
            reason=f"{company.name}补发欠薪", world_time=world.world_time, trace_id=trace_id,
        ))
        runtime = self.engine.get_runtime(world.world_id)
        if runtime is not None:
            runtime.event_bus.publish(session, world.world_time, "wage_repaid", {
                "employment_id": contract.employment_id,
                "company_id": company.company_id,
                "agent_id": agent.agent_id,
                "amount": amount,
            }, trace_id)
        logger.info(
            "Payroll repaid world={} employment={} company={} agent={} amount={} trace={}",
            world.world_id, contract.employment_id, company.company_id, agent.agent_id,
            amount, trace_id,
        )
        return amount
