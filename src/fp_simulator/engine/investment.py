"""iDeCo・NISAの計算(純粋関数).

iDeCo: 掛金の所得控除(小規模企業共済等掛金控除)、運用、受取時課税
  - 一時金受取: 退職所得(退職所得控除は加入年数ベース)として分離課税
  - 年金受取: 公的年金等に係る雑所得として総合課税(cashflow側で合算)
NISA: 非課税枠内での運用(運用益非課税)
  - つみたて投資枠/成長投資枠を分離管理(つみたて枠の超過分は成長枠へ振替)
  - 売却時は簿価按分で生涯枠を消費解除し、翌年に非課税枠が復活する
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace

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
    contribution_months: int = 0  # 拠出月数(退職所得控除の加入年数計算用)
    lump_sum_paid: bool = False  # 一時金受取済みフラグ


def ideco_contribution_years(account: IdecoAccount, prior_years: int) -> int:
    """退職所得控除に使う加入年数(1年未満切上げ、最低1年)を返す."""
    years = prior_years + -(-account.contribution_months // 12)
    return max(1, years)


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
    return replace(
        account,
        balance=new_balance,
        total_contributions=account.total_contributions + contribution,
        contribution_months=account.contribution_months + (1 if contribution > 0 else 0),
    )


def nisa_annual_limit(store: ParameterStore, date: datetime.date) -> int:
    """NISAの年間投資上限(つみたて+成長の合計)."""
    return store.get("NISA.年間投資上限", date)


def nisa_lifetime_limit(store: ParameterStore, date: datetime.date) -> int:
    """NISAの生涯非課税保有限度額."""
    return store.get("NISA.非課税保有限度額", date)


@dataclass
class NisaAccount:
    """NISA口座(つみたて投資枠・成長投資枠を分離管理).

    生涯非課税枠は簿価(取得価額)ベースで消費し、売却分の簿価は翌年に復活する。
    """

    tsumitate_balance: int = 0  # つみたて投資枠の評価額
    growth_balance: int = 0  # 成長投資枠の評価額
    tsumitate_cost: int = 0  # つみたて投資枠の簿価(生涯枠の消費)
    growth_cost: int = 0  # 成長投資枠の簿価(生涯枠の消費)
    pending_restore_tsumitate: int = 0  # 当年売却した簿価(翌年つみたて枠へ復活)
    pending_restore_growth: int = 0  # 当年売却した簿価(翌年成長枠へ復活)
    year_invested_tsumitate: int = 0  # 当年のつみたて枠投資額
    year_invested_growth: int = 0  # 当年の成長枠投資額
    tracking_year: int = 0  # 年間枠・枠復活の追跡年

    @property
    def balance(self) -> int:
        """評価額合計."""
        return self.tsumitate_balance + self.growth_balance

    @property
    def total_invested(self) -> int:
        """生涯非課税枠の消費額(簿価+当年売却の未復活分)."""
        return (
            self.tsumitate_cost
            + self.growth_cost
            + self.pending_restore_tsumitate
            + self.pending_restore_growth
        )


def withdrawal_amount(balance: int, requested: int) -> int:
    """口座残高を超えない取崩額を返す."""
    if balance <= 0 or requested <= 0:
        return 0
    return min(balance, requested)


def _nisa_rollover_year(account: NisaAccount, year: int) -> NisaAccount:
    """年が変わったら年間投資枠をリセットし、前年売却分の簿価を生涯枠へ復活させる."""
    if account.tracking_year == year:
        return account
    return replace(
        account,
        pending_restore_tsumitate=0,
        pending_restore_growth=0,
        year_invested_tsumitate=0,
        year_invested_growth=0,
        tracking_year=year,
    )


def nisa_monthly_step(
    store: ParameterStore,
    date: datetime.date,
    account: NisaAccount,
    tsumitate_monthly: int,
    growth_monthly: int = 0,
    annual_return_rate: float = 0.0,
) -> NisaAccount:
    """NISA口座の1ヶ月の変動(年間枠・生涯枠・枠復活を考慮).

    つみたて枠の希望額が枠上限を超える分は成長投資枠へ振り替えて投資する。
    """
    account = _nisa_rollover_year(account, date.year)

    # 運用益(月次複利)
    monthly_rate = annual_return_rate / 12
    tsumitate_balance = int(account.tsumitate_balance * (1 + monthly_rate))
    growth_balance = int(account.growth_balance * (1 + monthly_rate))

    tsumitate_annual = store.get("NISA.つみたて枠.年間上限", date)
    growth_annual = store.get("NISA.成長投資枠.年間上限", date)
    lifetime_limit = nisa_lifetime_limit(store, date)
    growth_lifetime = store.get("NISA.成長投資枠.生涯上限", date)

    lifetime_remaining = max(0, lifetime_limit - account.total_invested)

    # つみたて投資枠
    tsumitate_investable = max(
        0,
        min(
            tsumitate_monthly,
            tsumitate_annual - account.year_invested_tsumitate,
            lifetime_remaining,
        ),
    )
    lifetime_remaining -= tsumitate_investable

    # つみたて枠に収まらない分は成長投資枠へ振替
    growth_request = growth_monthly + (tsumitate_monthly - tsumitate_investable)
    growth_lifetime_remaining = max(
        0, growth_lifetime - (account.growth_cost + account.pending_restore_growth)
    )
    growth_investable = max(
        0,
        min(
            growth_request,
            growth_annual - account.year_invested_growth,
            growth_lifetime_remaining,
            lifetime_remaining,
        ),
    )

    return replace(
        account,
        tsumitate_balance=tsumitate_balance + tsumitate_investable,
        growth_balance=growth_balance + growth_investable,
        tsumitate_cost=account.tsumitate_cost + tsumitate_investable,
        growth_cost=account.growth_cost + growth_investable,
        year_invested_tsumitate=account.year_invested_tsumitate + tsumitate_investable,
        year_invested_growth=account.year_invested_growth + growth_investable,
    )


def nisa_withdraw(account: NisaAccount, requested: int) -> tuple[NisaAccount, int]:
    """NISA口座から取り崩す(非課税).

    つみたて枠・成長枠から評価額按分で取り崩し、簿価も按分で減らす。
    減らした簿価は翌年の生涯枠復活として記録する。

    Returns:
        (更新後の口座, 実際の取崩額)
    """
    total = account.balance
    withdrawal = withdrawal_amount(total, requested)
    if withdrawal <= 0:
        return account, 0

    tsumitate_part = withdrawal * account.tsumitate_balance // total
    growth_part = withdrawal - tsumitate_part

    tsumitate_cost_sold = (
        account.tsumitate_cost * tsumitate_part // account.tsumitate_balance
        if account.tsumitate_balance > 0
        else 0
    )
    growth_cost_sold = (
        account.growth_cost * growth_part // account.growth_balance
        if account.growth_balance > 0
        else 0
    )

    updated = replace(
        account,
        tsumitate_balance=account.tsumitate_balance - tsumitate_part,
        growth_balance=account.growth_balance - growth_part,
        tsumitate_cost=account.tsumitate_cost - tsumitate_cost_sold,
        growth_cost=account.growth_cost - growth_cost_sold,
        pending_restore_tsumitate=account.pending_restore_tsumitate + tsumitate_cost_sold,
        pending_restore_growth=account.pending_restore_growth + growth_cost_sold,
    )
    return updated, withdrawal
