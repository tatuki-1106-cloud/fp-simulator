"""iDeCo・NISAの計算(純粋関数).

iDeCo: 掛金の所得控除(小規模企業共済等掛金控除)、運用、受取時課税
NISA: 非課税枠内での運用(運用益非課税)
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


def ideco_contribution_limit(
    store: ParameterStore, date: datetime.date, subscriber_type: int
) -> int:
    """iDeCoの月額掛金上限を返す.

    Args:
        subscriber_type: 1=自営業, 2=会社員(企業年金なし), 3=専業主婦等
    """
    key = {1: "iDeCo.掛金上限.第1号", 2: "iDeCo.掛金上限.第2号", 3: "iDeCo.掛金上限.第3号"}[subscriber_type]
    return store.get(key, date)


def ideco_annual_deduction(
    store: ParameterStore, date: datetime.date, monthly_contribution: int, subscriber_type: int
) -> int:
    """iDeCoの年間所得控除額(小規模企業共済等掛金控除)."""
    limit = ideco_contribution_limit(store, date, subscriber_type)
    return min(monthly_contribution, limit) * 12


@dataclass
class IdecoAccount:
    """iDeCo口座."""

    balance: int = 0  # 運用残高
    total_contributions: int = 0  # 累計拠出額


def ideco_monthly_step(
    store: ParameterStore,
    date: datetime.date,
    account: IdecoAccount,
    monthly_contribution: int,
    subscriber_type: int,
    annual_return_rate: float = 0.0,
) -> IdecoAccount:
    """iDeCo口座の1ヶ月の変動.

    Returns:
        更新後の口座
    """
    limit = ideco_contribution_limit(store, date, subscriber_type)
    contribution = min(monthly_contribution, limit)

    # 運用益(月次複利)
    monthly_rate = annual_return_rate / 12
    new_balance = int(account.balance * (1 + monthly_rate)) + contribution
    return IdecoAccount(
        balance=new_balance,
        total_contributions=account.total_contributions + contribution,
    )


def nisa_annual_limit(store: ParameterStore, date: datetime.date) -> int:
    """NISAの年間投資上限."""
    return store.get("NISA.年間投資上限", date)


def nisa_lifetime_limit(store: ParameterStore, date: datetime.date) -> int:
    """NISAの生涯非課税保有限度額."""
    return store.get("NISA.非課税保有限度額", date)


@dataclass
class NisaAccount:
    """NISA口座."""

    balance: int = 0
    total_invested: int = 0  # 累計投資額(非課税枠の消費)


def withdrawal_amount(balance: int, requested: int) -> int:
    """口座残高を超えない取崩額を返す."""
    if balance <= 0 or requested <= 0:
        return 0
    return min(balance, requested)


def nisa_monthly_step(
    store: ParameterStore,
    date: datetime.date,
    account: NisaAccount,
    monthly_investment: int,
    annual_return_rate: float = 0.0,
) -> NisaAccount:
    """NISA口座の1ヶ月の変動(非課税枠を考慮)."""
    annual_limit = nisa_annual_limit(store, date)
    lifetime_limit = nisa_lifetime_limit(store, date)

    # 投資可能額(年間枠・生涯枠)
    investable = min(
        monthly_investment,
        annual_limit // 12,
        lifetime_limit - account.total_invested,
    )
    if investable <= 0:
        # 運用益のみ
        monthly_rate = annual_return_rate / 12
        return NisaAccount(
            balance=int(account.balance * (1 + monthly_rate)),
            total_invested=account.total_invested,
        )

    monthly_rate = annual_return_rate / 12
    new_balance = int(account.balance * (1 + monthly_rate)) + investable
    return NisaAccount(
        balance=new_balance,
        total_invested=account.total_invested + investable,
    )
