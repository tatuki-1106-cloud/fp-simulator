"""FP UNIV互換の簡易インポートと近似警告."""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from fp_simulator.engine.models import (
    Account,
    Household,
    HousingCostSchedule,
    Income,
    Insurance,
    InvestmentSchedule,
    Loan,
    Member,
    OwnedHousingPlan,
    Relationship,
    SocialInsuranceType,
)


@dataclass
class ImportPreview:
    """インポート前に表示する差分と警告."""

    additions: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


def _yen(value: Any, unit: str = "yen") -> int:
    amount = int(float(value or 0))
    if unit in {"10k_yen", "万円"}:
        return amount * 10_000
    return amount


def _date(value: Any, default: datetime.date) -> datetime.date:
    if not value:
        return default
    return datetime.date.fromisoformat(str(value)[:10])


def _items(payload: dict[str, Any], section: str) -> list[dict[str, Any]]:
    value = payload.get(section, [])
    if not isinstance(value, list):
        raise TypeError(f"「{section}」はJSON配列で指定してください")
    if not all(isinstance(item, dict) for item in value):
        raise TypeError(f"「{section}」の各要素はJSONオブジェクトで指定してください")
    return value


def _object(payload: dict[str, Any], section: str) -> dict[str, Any] | None:
    value = payload.get(section)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"「{section}」はJSONオブジェクトで指定してください")
    return value


def _schedule_items(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    items = [value] if isinstance(value, dict) else value
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise TypeError(f"「{field_name}」はJSONオブジェクトまたは配列で指定してください")
    return [dict(item) for item in items]


def _member_name(item: dict[str, Any]) -> str:
    return str(item.get("member") or item.get("member_name") or "")


def _validate_payload(payload: dict[str, Any]) -> None:
    for section in ("members", "incomes", "loans", "insurance", "accounts"):
        _items(payload, section)
    housing = _object(payload, "housing")
    if housing is not None:
        _schedule_items(housing.get("cost_schedules"), "housing.cost_schedules")
    for account in _items(payload, "accounts"):
        _schedule_items(
            account.get("contribution_schedule"),
            "accounts.contribution_schedule",
        )
    unsupported = payload.get("unsupported", [])
    if not isinstance(unsupported, list):
        raise TypeError("「unsupported」はJSON配列で指定してください")


def preview_fp_univ_import(
    payload: dict[str, Any], household: Household
) -> ImportPreview:
    """FP UNIV形式のJSONを検査し、変換内容と近似を列挙する."""
    _validate_payload(payload)
    preview = ImportPreview(payload=payload)
    known = {
        "members",
        "incomes",
        "loans",
        "housing",
        "insurance",
        "accounts",
        "unsupported",
    }
    for key in payload:
        if key not in known:
            preview.warnings.append(f"未対応のトップレベル項目「{key}」は取り込みません")

    existing_names = {member.name for member in household.members}
    for item in _items(payload, "members"):
        name = str(item.get("name", "")).strip()
        if not name:
            preview.warnings.append("名前のない家族は取り込めません")
        elif name in existing_names:
            preview.updates.append(f"家族「{name}」を既存メンバーへ反映")
        else:
            preview.additions.append(f"家族「{name}」を追加")

    member_ids = {member.name: member.id for member in household.members}
    for item in _items(payload, "incomes"):
        name = _member_name(item)
        amount = _yen(item.get("monthly_amount"), item.get("unit", "yen"))
        existing = next(
            (
                income
                for income in household.incomes
                if income.member_id == member_ids.get(name)
                and income.name == item.get("name", "給与")
            ),
            None,
        )
        target = preview.updates if existing else preview.additions
        target.append(f"収入「{name or '不明'}」月額{amount:,}円")
        if item.get("annual_amount") is not None:
            preview.warnings.append("年額収入は12分の1に換算して月額へ近似します")
        if item.get("bonus") is not None:
            preview.warnings.append("賞与の支給月は6月・12月として取り込みます")

    for item in _items(payload, "loans"):
        member_id = member_ids.get(_member_name(item))
        existing = next(
            (
                loan
                for loan in household.loans
                if loan.member_id == member_id
                and loan.name == item.get("name", "住宅ローン")
            ),
            None,
        )
        target = preview.updates if existing else preview.additions
        target.append(f"ローン「{item.get('name', '住宅ローン')}」を反映")
        if item.get("rate_schedule"):
            preview.warnings.append("ローンの変動金利は年月ごとの金利変更として取り込みます")

    housing = _object(payload, "housing")
    if housing:
        preview.updates.append("所有住宅の基本情報を更新")
        if housing.get("management_fee"):
            preview.warnings.append("管理費は住宅コストスケジュールへ変換します")

    for item in _items(payload, "insurance"):
        insured_id = member_ids.get(
            str(item.get("insured_member") or item.get("member") or "")
        )
        payer_id = member_ids.get(
            str(item.get("payer_member") or item.get("member") or "")
        )
        existing = next(
            (
                insurance
                for insurance in household.insurances
                if insurance.insured_member_id == insured_id
                and insurance.payer_member_id == payer_id
                and insurance.name == item.get("name", "保険")
            ),
            None,
        )
        target = preview.updates if existing else preview.additions
        target.append(f"保険「{item.get('name', '保険')}」を反映")
        if item.get("payment_frequency", "monthly") != "monthly":
            preview.warnings.append("月払以外の保険料は支払周期を保持して取り込みます")

    for item in _items(payload, "accounts"):
        member_id = member_ids.get(_member_name(item))
        existing = next(
            (
                account
                for account in household.accounts
                if account.member_id == member_id
                and account.name == item.get("name", "口座")
            ),
            None,
        )
        target = preview.updates if existing else preview.additions
        target.append(f"口座「{item.get('name', '口座')}」を反映")
        if item.get("monthly_contribution") is not None or item.get(
            "contribution_schedule"
        ):
            preview.warnings.append("積立は一般口座の積立スケジュールへ変換します")

    for item in payload.get("unsupported", []):
        preview.warnings.append(f"未対応項目「{item}」は取り込みません")
    return preview


def _merge_member(
    item: dict[str, Any],
    existing: Member | None,
    default_date: datetime.date,
) -> Member:
    data = existing.model_dump() if existing else {
        "id": str(uuid.uuid4()),
        "name": str(item.get("name", "")).strip(),
        "relationship": Relationship.OTHER,
        "birth_date": default_date,
    }
    for field_name in (
        "name",
        "relationship",
        "gender",
        "life_expectancy_age",
        "disability_grade",
        "dependent_until_age",
        "dependent_until_event",
        "prefecture",
    ):
        if field_name in item:
            data[field_name] = item[field_name]
    if "birth_date" in item:
        data["birth_date"] = _date(item["birth_date"], default_date)
    if data.get("relationship") not in {value.value for value in Relationship}:
        data["relationship"] = Relationship.OTHER
    return Member.model_validate(data)


def _investment_schedules(item: dict[str, Any]) -> list[InvestmentSchedule]:
    unit = item.get("unit", "yen")
    raw_schedules = _schedule_items(
        item.get("contribution_schedule"),
        "accounts.contribution_schedule",
    )
    if item.get("monthly_contribution") is not None:
        raw_schedules.insert(
            0,
            {
                "monthly_amount": item["monthly_contribution"],
                "start_age": item.get("start_age", 0),
                "end_age": item.get("end_age"),
                "start_year": item.get("start_year"),
                "start_month": item.get("start_month", 1),
                "end_year": item.get("end_year"),
                "end_month": item.get("end_month", 12),
                "annual_raise_rate": item.get("annual_raise_rate", 0),
                "annual_return_rate": item.get("annual_return_rate"),
            },
        )
    schedules = []
    for raw in raw_schedules:
        data = dict(raw)
        amount = data.pop("amount", data.get("monthly_amount", 0))
        data["monthly_amount"] = _yen(amount, data.pop("unit", unit))
        schedules.append(InvestmentSchedule.model_validate(data))
    return schedules


def apply_fp_univ_import(
    payload: dict[str, Any], household: Household
) -> ImportPreview:
    """プレビュー済みデータを世帯へ反映する."""
    preview = preview_fp_univ_import(payload, household)
    default_date = datetime.date(
        household.assumptions.base_year, household.assumptions.base_month, 1
    )
    names_to_ids = {member.name: member.id for member in household.members}

    for item in _items(payload, "members"):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        existing = next(
            (member for member in household.members if member.name == name),
            None,
        )
        member = _merge_member(item, existing, default_date)
        if existing is None:
            household.members.append(member)
        else:
            household.members = [
                member if current.id == existing.id else current
                for current in household.members
            ]
        names_to_ids[name] = member.id

    for item in _items(payload, "incomes"):
        member_id = names_to_ids.get(_member_name(item))
        if member_id is None:
            preview.warnings.append(
                f"収入の対象者「{item.get('member')}」が見つかりません"
            )
            continue
        name = item.get("name", "給与")
        existing = next(
            (
                income
                for income in household.incomes
                if income.member_id == member_id and income.name == name
            ),
            None,
        )
        data = existing.model_dump() if existing else {
            "id": str(uuid.uuid4()),
            "member_id": member_id,
            "name": name,
            "monthly_amount": 0,
        }
        unit = item.get("unit", "yen")
        for field_name in (
            "start_age",
            "start_month",
            "end_age",
            "end_month",
            "annual_raise_rate",
            "retirement_age",
        ):
            if field_name in item:
                data[field_name] = item[field_name]
        if "monthly_amount" in item:
            data["monthly_amount"] = _yen(item["monthly_amount"], unit)
        if "annual_amount" in item:
            data["monthly_amount"] = _yen(item["annual_amount"], unit) // 12
        if "bonus" in item:
            data["bonus_amount"] = _yen(item["bonus"], unit)
            data["bonus_months"] = [6, 12] if item["bonus"] else []
        if "retirement_allowance" in item:
            data["retirement_allowance"] = _yen(item["retirement_allowance"], unit)
        if "social_insurance_type" in item:
            social = item["social_insurance_type"]
            data["social_insurance_type"] = (
                social
                if social in {value.value for value in SocialInsuranceType}
                else SocialInsuranceType.KYOSAI_KOSEI
            )
        income = Income.model_validate(data)
        if existing:
            household.incomes = [
                income if current.id == existing.id else current
                for current in household.incomes
            ]
        else:
            household.incomes.append(income)

    for item in _items(payload, "loans"):
        member_id = names_to_ids.get(_member_name(item))
        if member_id is None:
            preview.warnings.append(
                f"ローンの対象者「{item.get('member')}」が見つかりません"
            )
            continue
        name = item.get("name", "住宅ローン")
        existing = next(
            (
                loan
                for loan in household.loans
                if loan.member_id == member_id and loan.name == name
            ),
            None,
        )
        data = existing.model_dump() if existing else {
            "id": str(uuid.uuid4()),
            "member_id": member_id,
            "name": name,
            "principal": 0,
            "annual_rate": 0,
            "years": 35,
        }
        unit = item.get("unit", "yen")
        for field_name in (
            "annual_rate",
            "years",
            "repayment_type",
            "start_year",
            "start_month",
            "deferment_months",
        ):
            if field_name in item:
                data[field_name] = item[field_name]
        if "principal" in item:
            data["principal"] = _yen(item["principal"], unit)
        if "bonus_amount" in item:
            data["bonus_amount"] = _yen(item["bonus_amount"], unit)
            data["bonus_months"] = item.get("bonus_months", [6, 12])
        if "rate_schedule" in item:
            data["rate_schedule"] = item["rate_schedule"]
            data["is_variable_rate"] = bool(item["rate_schedule"])
        loan = Loan.model_validate(data)
        if existing:
            household.loans = [
                loan if current.id == existing.id else current
                for current in household.loans
            ]
        else:
            household.loans.append(loan)

    housing = _object(payload, "housing")
    if housing:
        data = household.owned_housing.model_dump() if household.owned_housing else {
            "property_price": 0,
            "down_payment": 0,
            "purchase_year": household.assumptions.base_year,
            "purchase_month": household.assumptions.base_month,
        }
        unit = housing.get("unit", "yen")
        for field_name in (
            "property_price",
            "down_payment",
            "annual_property_tax",
            "annual_repair_cost",
        ):
            if field_name in housing:
                data[field_name] = _yen(housing[field_name], unit)
        for field_name in ("purchase_year", "purchase_month"):
            if field_name in housing:
                data[field_name] = housing[field_name]
        schedules = [
            HousingCostSchedule.model_validate(
                {
                    **raw,
                    "amount": _yen(raw.get("amount"), raw.get("unit", unit)),
                }
            )
            for raw in _schedule_items(
                housing.get("cost_schedules"),
                "housing.cost_schedules",
            )
        ]
        if housing.get("management_fee") is not None:
            schedules.append(
                HousingCostSchedule(
                    cost_type="管理費",
                    amount=_yen(housing["management_fee"], unit),
                    start_year=int(data["purchase_year"]),
                    start_month=int(data["purchase_month"]),
                )
            )
        if "cost_schedules" in housing or "management_fee" in housing:
            data["cost_schedules"] = schedules
        household.owned_housing = OwnedHousingPlan.model_validate(data)

    for item in _items(payload, "accounts"):
        member_id = names_to_ids.get(_member_name(item))
        name = item.get("name", "口座")
        existing = next(
            (
                account
                for account in household.accounts
                if account.member_id == member_id and account.name == name
            ),
            None,
        )
        data = existing.model_dump() if existing else {
            "id": str(uuid.uuid4()),
            "name": name,
            "member_id": member_id,
        }
        if "balance" in item:
            data["balance"] = _yen(item["balance"], item.get("unit", "yen"))
        if "interest_rate" in item:
            data["interest_rate"] = item["interest_rate"]
        if "monthly_contribution" in item or "contribution_schedule" in item:
            data["investment_schedules"] = _investment_schedules(item)
        account = Account.model_validate(data)
        if existing:
            household.accounts = [
                account if current.id == existing.id else current
                for current in household.accounts
            ]
        else:
            household.accounts.append(account)

    for item in _items(payload, "insurance"):
        insured_id = names_to_ids.get(
            str(item.get("insured_member") or item.get("member") or "")
        )
        payer_id = names_to_ids.get(
            str(item.get("payer_member") or item.get("member") or "")
        )
        if insured_id is None or payer_id is None:
            preview.warnings.append(
                f"保険「{item.get('name', '保険')}」の対象者が見つかりません"
            )
            continue
        name = item.get("name", "保険")
        existing = next(
            (
                insurance
                for insurance in household.insurances
                if insurance.insured_member_id == insured_id
                and insurance.payer_member_id == payer_id
                and insurance.name == name
            ),
            None,
        )
        data = existing.model_dump() if existing else {
            "id": str(uuid.uuid4()),
            "name": name,
            "insured_member_id": insured_id,
            "payer_member_id": payer_id,
            "monthly_premium": 0,
        }
        unit = item.get("unit", "yen")
        for field_name in (
            "insurance_type",
            "start_year",
            "start_month",
            "end_year",
            "end_month",
            "surrender_value_rate",
            "payment_frequency",
            "payment_month",
            "payment_interval_years",
        ):
            if field_name in item:
                data[field_name] = item[field_name]
        for field_name in ("monthly_premium", "death_benefit", "payment_amount"):
            if field_name in item:
                data[field_name] = (
                    _yen(item[field_name], unit)
                    if item[field_name] is not None
                    else None
                )
        insurance = Insurance.model_validate(data)
        if existing:
            household.insurances = [
                insurance if current.id == existing.id else current
                for current in household.insurances
            ]
        else:
            household.insurances.append(insurance)
    return preview


def parse_import_json(raw: str) -> dict[str, Any]:
    """画面入力のJSONを検証して辞書へ変換する."""
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("インポートデータはJSONオブジェクトで指定してください")
    _validate_payload(parsed)
    return parsed
