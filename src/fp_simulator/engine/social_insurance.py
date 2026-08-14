"""社会保険料の月次計算(純粋関数).

厚生年金・健康保険(協会けんぽ・都道府県別)・介護保険(40〜64歳)・雇用保険。
標準報酬月額に基づき、労使折半(本人負担=1/2)で計算。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


# 標準報酬月額の等級テーブル(簡略版: 報酬月額の下限→標準報酬月額)
# 実際のテーブルは細かいが、MVPでは「報酬月額の範囲→標準報酬月額」の主要な区分を定義
# 出典: https://www.kyoukaikenpo.or.jp/g3/cat330/sb3130/hokenryou/
STANDARD_REMUNERATION_TABLE: list[tuple[int, int]] = [
    (0, 58000),
    (63000, 68000),
    (73000, 78000),
    (83000, 88000),
    (93000, 98000),
    (101000, 104000),
    (107000, 110000),
    (114000, 118000),
    (122000, 126000),
    (130000, 134000),
    (138000, 142000),
    (146000, 150000),
    (155000, 160000),
    (165000, 170000),
    (175000, 180000),
    (185000, 190000),
    (195000, 200000),
    (210000, 220000),
    (230000, 240000),
    (250000, 260000),
    (270000, 280000),
    (290000, 300000),
    (310000, 320000),
    (330000, 340000),
    (350000, 360000),
    (370000, 380000),
    (395000, 410000),
    (425000, 440000),
    (455000, 470000),
    (485000, 500000),
    (515000, 530000),
    (545000, 560000),
    (575000, 590000),
    (605000, 620000),
    (635000, 650000),
    (665000, 680000),
    (695000, 710000),
    (725000, 750000),
    (775000, 790000),
    (815000, 830000),
    (855000, 880000),
    (905000, 930000),
    (955000, 980000),
    (1005000, 1030000),
    (1055000, 1090000),
    (1115000, 1150000),
    (1175000, 1210000),
    (1235000, 1270000),
    (1295000, 1330000),
    (1355000, 1390000),
    (1415000, 1450000),
    (1475000, 1500000),
    (1550000, 1650000),  # 上限
]


def standard_remuneration(monthly_salary: int) -> int:
    """報酬月額から標準報酬月額を返す.

    テーブルは (区間下限, 標準報酬月額) のリスト。
    報酬月額が `lower` 以上で次の区間の `lower` 未満なら `std` を返す。
    """
    if monthly_salary <= 0:
        return 0
    # 最後の要素は上限。それ以外は「次の区間の下限未満」で判定
    for i, (lower, std) in enumerate(STANDARD_REMUNERATION_TABLE):
        if i + 1 < len(STANDARD_REMUNERATION_TABLE):
            next_lower = STANDARD_REMUNERATION_TABLE[i + 1][0]
            if lower <= monthly_salary < next_lower:
                return std
        else:
            if monthly_salary >= lower:
                return std
    return STANDARD_REMUNERATION_TABLE[-1][1]


@dataclass(frozen=True)
class SocialInsurancePremiums:
    """社会保険料の月額(本人負担分)."""

    pension: int  # 厚生年金
    health: int  # 健康保険
    nursing: int  # 介護保険
    employment: int  # 雇用保険

    @property
    def total(self) -> int:
        return self.pension + self.health + self.nursing + self.employment


def monthly_social_insurance(
    store: ParameterStore,
    date: datetime.date,
    monthly_salary: int,
    prefecture: str,
    age: int,
    is_employee: bool = True,
) -> SocialInsurancePremiums:
    """1ヶ月の社会保険料(本人負担)を計算.

    Args:
        store: パラメータストア
        date: 対象月
        monthly_salary: 報酬月額(額面)
        prefecture: 勤務先都道府県(健康保険料率)
        age: 当月末時点の年齢(介護保険判定用)
        is_employee: 被用者(会社員)か。Falseの場合は雇用保険なし
    """
    if monthly_salary <= 0:
        return SocialInsurancePremiums(0, 0, 0, 0)

    std = standard_remuneration(monthly_salary)
    split = store.get("社会保険.労使折半", date)  # 0.5

    # 厚生年金
    pension_rate = store.get("社会保険.厚生年金.料率", date)
    pension = int(std * pension_rate * split)

    # 健康保険(都道府県別)。端数は10銭未満切捨て→円四捨五入の簡易モデル
    health_rates = store.get("社会保険.健康保険.料率", date)
    health_rate = health_rates.get(prefecture, health_rates.get("東京都", 0.0991))
    health = round(std * health_rate * split)

    # 介護保険(40〜64歳)
    nursing = 0
    age_range = store.get("社会保険.介護保険.対象年齢", date)
    if age_range["min_age"] <= age <= age_range["max_age"]:
        nursing_rate = store.get("社会保険.介護保険.料率", date)
        nursing = int(std * nursing_rate * split)

    # 雇用保険(被用者のみ、報酬額ベース)
    employment = 0
    if is_employee:
        emp_rate = store.get("社会保険.雇用保険.労働者負担率", date)
        employment = int(monthly_salary * emp_rate)

    return SocialInsurancePremiums(pension, health, nursing, employment)
