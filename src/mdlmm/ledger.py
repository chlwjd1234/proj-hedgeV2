"""Decimal, self-financing portfolio accounting and weighted-average cost basis.

Shares/principal, earned fee assets and collateral are distinct balance sheets.
The benchmark contains NO hedge trading costs: it is a mathematical comparator.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .errors import ValidationError
from .util import D, dec

ZERO = D(0)


@dataclass
class CostBasis:
    quantity: D = ZERO
    basis: D = ZERO
    realized: D = ZERO

    def trade(self, delta: D, execution_price: D):
        delta, execution_price = dec(delta), dec(execution_price)
        if execution_price <= 0:
            raise ValidationError('cost-basis price must be positive')
        if delta >= 0:
            self.quantity += delta
            self.basis += delta * execution_price
        else:
            sold = -delta
            if sold > self.quantity + D('1e-45'):
                raise ValidationError('principal cost basis becomes short')
            avg = self.basis / self.quantity if self.quantity else ZERO
            self.realized += sold * (execution_price - avg)
            self.quantity -= sold
            self.basis -= sold * avg
            if abs(self.quantity) < D('1e-45'):
                self.quantity, self.basis = ZERO, ZERO

    def unrealized(self, price: D) -> D:
        return self.quantity * dec(price) - self.basis


@dataclass
class InventoryBenchmark:
    quantity: D
    cash: D
    initial_value: D
    last_price: D
    pnl: D = ZERO
    cash_flows: D = ZERO

    @classmethod
    def start(cls, quantity, total_value, price):
        q, value, s = dec(quantity), dec(total_value), dec(price)
        return cls(q, value - q*s, value, s)

    def mark_and_rebalance(self, price, target_quantity, external_flow=0):
        s, q, flow = dec(price), dec(target_quantity), dec(external_flow)
        self.pnl += self.quantity * (s-self.last_price)
        self.cash += flow - (q-self.quantity)*s
        self.cash_flows += flow
        self.quantity, self.last_price = q, s
        residual = self.quantity*s+self.cash-self.initial_value-self.cash_flows-self.pnl
        if abs(residual) > D('1e-35'):
            raise ValidationError(f'benchmark self-financing residual {residual}')
        return residual


COST_CATEGORIES = ('composition_cost', 'lp_execution_cost', 'priority_cost', 'base_transaction_cost',
                   'hedge_execution_cost', 'borrow_cost', 'data_cost')


@dataclass
class Ledger:
    initial_nav: D
    principal_sol: D
    principal_usdc: D
    cash_usdc: D
    price: D
    basis: CostBasis
    benchmark: InventoryBenchmark
    fee_sol: dict[str,D] = field(default_factory=lambda: {'base':ZERO,'variable':ZERO,'composition':ZERO})
    fee_usdc: dict[str,D] = field(default_factory=lambda: {'base':ZERO,'variable':ZERO,'composition':ZERO})
    income_at_earn: dict[str,D] = field(default_factory=lambda: {'base':ZERO,'variable':ZERO,'composition':ZERO})
    protocol_information: D = ZERO
    external_flows: D = ZERO
    collateral: D = ZERO
    hedge_mark_pnl: D = ZERO
    funding: D = ZERO
    costs: dict[str,D] = field(default_factory=lambda: {k:ZERO for k in COST_CATEGORIES})
    principal_pnl: D = ZERO
    _last_principal_value: D = ZERO

    @classmethod
    def start(cls, nav, sol, usdc, price) -> 'Ledger':
        nav, sol, usdc, price = map(dec, (nav,sol,usdc,price))
        if min(nav,sol,usdc) < 0 or price <= 0:
            raise ValidationError('invalid initial portfolio')
        deployed = sol*price+usdc
        if deployed > nav:
            raise ValidationError('LP allocation exceeds NAV')
        basis = CostBasis()
        basis.trade(sol,price)
        benchmark = InventoryBenchmark.start(sol, deployed, price)
        return cls(nav,sol,usdc,nav-deployed,price,basis,benchmark,_last_principal_value=deployed)

    def principal_transition(self, new_sol, new_usdc, price, *, external_principal_flow=0,
                             basis_price=None):
        """Mark old inventory first, then change inventory at event-time reference.

        Actual swap cost basis can be supplied independently. Non-swap share
        changes use reference-mark transfer accounting, not an invented trade.
        """
        new_sol, new_usdc, s, flow = map(dec,(new_sol,new_usdc,price,external_principal_flow))
        if min(new_sol,new_usdc) < 0 or s <= 0:
            raise ValidationError('negative principal balance/nonpositive mark')
        value = new_sol*s+new_usdc
        self.principal_pnl += value-self._last_principal_value-flow
        self.benchmark.mark_and_rebalance(s,new_sol,flow)
        delta = new_sol-self.principal_sol
        self.basis.trade(delta, s if basis_price is None else dec(basis_price))
        self.principal_sol,self.principal_usdc,self.price = new_sol,new_usdc,s
        self._last_principal_value = value
        if flow:
            # Positive flow enters portfolio externally; internal reallocation is
            # handled by reallocate(), not this argument.
            self.external_flows += flow

    def mark(self, price):
        self.principal_transition(self.principal_sol,self.principal_usdc,price)

    def earn_fee(self, category: str, sol=0, usdc=0, protocol_usdc=0):
        if category not in self.fee_sol:
            raise ValidationError('unknown fee category')
        x,y,p = map(dec,(sol,usdc,protocol_usdc))
        if min(x,y,p) < 0:
            raise ValidationError('negative fee receipt')
        self.fee_sol[category] += x
        self.fee_usdc[category] += y
        self.income_at_earn[category] += x*self.price+y
        # Informational only: LP fees were ALREADY net of protocol share.
        self.protocol_information += p

    def charge(self, category: str, usdc):
        if category not in self.costs:
            raise ValidationError('unknown cost category')
        cost = dec(usdc)
        if cost < 0:
            raise ValidationError('negative cost; rebates require separate income')
        if cost > self.cash_usdc:
            raise ValidationError('insufficient spendable USDC for cost')
        self.costs[category] += cost
        self.cash_usdc -= cost

    def transfer_collateral(self, amount):
        a = dec(amount)
        if a > self.cash_usdc or -a > self.collateral:
            raise ValidationError('collateral transfer exceeds spendable balance')
        self.cash_usdc -= a
        self.collateral += a

    def external_cash_flow(self, amount):
        amount = dec(amount)
        if self.cash_usdc + amount < 0:
            raise ValidationError('withdrawal exceeds cash')
        self.cash_usdc += amount
        self.external_flows += amount

    def reallocate(self, sol, usdc, *, acquisition_cost=0):
        """Internal principal/cash reallocation at current mark, not income."""
        sol,usdc = dec(sol),dec(usdc)
        new_value = sol*self.price+usdc
        old_value = self.principal_sol*self.price+self.principal_usdc
        cash_change = old_value-new_value
        if self.cash_usdc+cash_change < 0:
            raise ValidationError('insufficient cash for reallocation')
        # Mark-and-target without counting internal cash as PnL/external flow.
        self.benchmark.mark_and_rebalance(self.price,sol)
        self.basis.trade(sol-self.principal_sol,self.price)
        self.principal_sol,self.principal_usdc=sol,usdc
        self._last_principal_value=new_value
        self.cash_usdc += cash_change
        self.charge('lp_execution_cost',acquisition_cost)

    @property
    def fee_value(self):
        return {k: self.fee_sol[k]*self.price+self.fee_usdc[k] for k in self.fee_sol}

    def nav(self):
        return (self.principal_sol*self.price+self.principal_usdc+self.cash_usdc+self.collateral
                +sum(self.fee_value.values(),ZERO)+self.hedge_mark_pnl+self.funding)

    def report(self):
        fees = self.fee_value
        costs = sum(self.costs.values(),ZERO)
        pnl = self.principal_pnl+sum(fees.values(),ZERO)+self.hedge_mark_pnl+self.funding-costs
        residual = self.nav()-self.initial_nav-self.external_flows-pnl
        if abs(residual) > D('1e-30'):
            raise ValidationError(f'NAV accounting identity residual {residual}')
        lp_cost = sum((self.costs[k] for k in ('composition_cost','lp_execution_cost',
                                              'priority_cost','base_transaction_cost')), ZERO)
        fee_price_pnl = sum(fees.values(),ZERO)-sum(self.income_at_earn.values(),ZERO)
        matched_pnl = self.benchmark.pnl+fee_price_pnl
        surplus = self.principal_pnl+sum(fees.values(),ZERO)-matched_pnl-lp_cost
        return {'nav':self.nav(),'principal_sol':self.principal_sol,'principal_usdc':self.principal_usdc,
                'fee_sol_total':sum(self.fee_sol.values(),ZERO),'fee_usdc_total':sum(self.fee_usdc.values(),ZERO),'nav_pnl':pnl,'external_cash_flows':self.external_flows,
                'lp_principal_inventory_value':self.principal_sol*self.price+self.principal_usdc,
                'raw_lp_principal_pnl':self.principal_pnl,
                'lp_base_fee_value':fees['base'],'lp_variable_fee_value':fees['variable'],
                'lp_composition_fee_value':fees['composition'],
                'fee_income_at_earn':dict(self.income_at_earn),
                'fee_asset_price_pnl':sum(fees.values(),ZERO)-sum(self.income_at_earn.values(),ZERO),
                'protocol_cut_informational_only':self.protocol_information,
                'principal_inventory_benchmark_pnl':self.benchmark.pnl,
                'fee_inventory_benchmark_pnl':fee_price_pnl,
                'inventory_matched_benchmark_pnl':matched_pnl,
                'inventory_matched_surplus':surplus,
                'realized_sol_inventory_pnl':self.basis.realized,
                'unrealized_sol_inventory_pnl':self.basis.unrealized(self.price),
                'realized_unrealized_scope':'SOL_WEIGHTED_AVERAGE_COST_NOT_ALL_PRINCIPAL_PNL',
                'cash_usdc':self.cash_usdc,'hedge_collateral':self.collateral,
                'hedge_mark_pnl':self.hedge_mark_pnl,'funding':self.funding,
                'costs':dict(self.costs),'nav_accounting_residual':residual,
                'benchmark_is_executable_hedge':False,'lvr_label_used':False}
