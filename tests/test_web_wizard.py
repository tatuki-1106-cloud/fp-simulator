"""Webウィザードの統合テスト."""

from __future__ import annotations

import os
import tempfile

import pytest
from httpx import ASGITransport, AsyncClient

# テスト用に一時DBを使う
os.environ["FP_DB_PATH"] = tempfile.mktemp(suffix=".db")

from fp_simulator.db.database import init_db
from fp_simulator.web.main import app


@pytest.fixture()
async def client() -> AsyncClient:
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_household_and_full_flow(client: AsyncClient) -> None:
    """世帯作成→家族→収入→年金→支出→資産→シミュレーションの一連の流れ."""
    # 1. 世帯作成
    r = await client.post(
        "/households/new", data={"name": "テスト世帯", "base_year": 2026, "base_month": 1}
    )
    assert r.status_code == 303
    location = r.headers["location"]
    household_id = location.split("/")[2]

    # 2. 家族追加(世帯主)
    r = await client.post(
        f"/households/{household_id}/members",
        data={
            "name": "たろう",
            "relationship": "世帯主",
            "birth_date": "1996-04-01",
            "gender": "男",
            "life_expectancy_age": 90,
            "prefecture": "東京都",
        },
    )
    assert r.status_code == 303

    # メンバーIDを取得するため一覧を確認
    r = await client.get(f"/households/{household_id}/members")
    assert r.status_code == 200
    assert "たろう" in r.text

    # 3. 収入追加
    from fp_simulator.db.database import get_household

    household = await get_household(household_id)
    husband_id = household.members[0].id

    r = await client.post(
        f"/households/{household_id}/incomes",
        data={
            "member_id": husband_id,
            "name": "会社員",
            "social_insurance_type": "給与(厚生年金)",
            "start_age": 29,
            "end_age": 60,
            "monthly_amount": 300000,
            "bonus_amount": 500000,
            "retirement_allowance": 20000000,
            "retirement_age": 60,
        },
    )
    assert r.status_code == 303

    # 4. 年金追加
    r = await client.post(
        f"/households/{household_id}/pensions",
        data={
            "member_id": husband_id,
            "kokumin_months": 480,
            "kousei_months": 456,
            "avg_standard_remuneration": 300000,
            "start_age": 65,
        },
    )
    assert r.status_code == 303

    # 5. 支出追加
    r = await client.post(
        f"/households/{household_id}/expenses",
        data={"name": "生活費", "monthly_amount": 200000, "start_age": 0, "end_age": 0},
    )
    assert r.status_code == 303

    # 6. 資産追加
    r = await client.post(
        f"/households/{household_id}/accounts",
        data={"name": "普通預金", "balance": 3000000, "interest_rate": 0.0},
    )
    assert r.status_code == 303

    # 6. Q6所有住宅を保存
    r = await client.post(
        f"/households/{household_id}/housing",
        data={
            "property_price": 40000000,
            "down_payment": 5000000,
            "purchase_year": 2026,
            "purchase_month": 4,
            "annual_property_tax": 120000,
            "annual_repair_cost": 60000,
        },
    )
    assert r.status_code == 303
    r = await client.get(f"/households/{household_id}/housing")
    assert r.status_code == 200
    assert "所有住宅（MVP）" in r.text
    assert "40,000,000円" in r.text
    invalid_housing = await client.post(
        f"/households/{household_id}/housing",
        data={
            "property_price": 10000000,
            "down_payment": 10000001,
            "purchase_year": 2026,
            "purchase_month": 4,
            "annual_property_tax": 0,
            "annual_repair_cost": 0,
        },
    )
    assert invalid_housing.status_code == 400

    # Q7乗り物を追加
    vehicle_response = await client.post(
        f"/households/{household_id}/vehicles",
        data={
            "name": "ファミリーカー",
            "vehicle_type": "中古車",
            "ownership_start_year": 2026,
            "ownership_start_month": 1,
            "ownership_end_year": 2030,
            "ownership_end_month": 12,
            "purchase_price": 2000000,
            "monthly_maintenance": 20000,
            "annual_tax_repair": 120000,
            "replacement_cycle_years": 3,
            "sale_price": 500000,
            "inspection_cost": 100000,
            "inspection_cycle_years": 2,
            "replacement_loan_principal": 1000000,
            "replacement_loan_annual_rate": 0.01,
            "replacement_loan_years": 2,
            "replacement_loan_fee": 20000,
            "replacement_loan_repayment_type": "元利均等",
        },
    )
    assert vehicle_response.status_code == 303
    r = await client.get(f"/households/{household_id}/vehicles")
    assert r.status_code == 200
    assert "ファミリーカー" in r.text
    assert "1,000,000円" in r.text

    # Q8ライフイベントを追加・表示・削除
    event_response = await client.post(
        f"/households/{household_id}/events",
        data={
            "event_type": "結婚援助",
            "name": "子どもの結婚援助",
            "member_id": husband_id,
            "monthly_amount": 1000000,
            "cycle": "once",
            "yearly_month": 1,
            "start_age": 35,
            "start_month": 4,
            "end_age": 0,
            "end_month": 12,
            "start_date": "2035-04-01",
            "end_date": "2035-04-01",
            "annual_raise_rate": 0.02,
            "disaster_amount": 300000,
        },
    )
    assert event_response.status_code == 303
    r = await client.get(f"/households/{household_id}/events")
    assert r.status_code == 200
    assert "子どもの結婚援助" in r.text
    assert "結婚援助" in r.text
    assert "たろう" in r.text
    assert "2035-04-01" in r.text

    # Q10保険を追加し、保障分析を表示
    insurance_response = await client.post(
        f"/households/{household_id}/insurance",
        data={
            "name": "定期生命保険",
            "insurance_type": "死亡保障",
            "insured_member_id": husband_id,
            "payer_member_id": husband_id,
            "monthly_premium": 10000,
            "start_year": 2026,
            "start_month": 1,
            "end_year": 2060,
            "end_month": 12,
            "death_benefit": 10000000,
            "surrender_value_rate": 0.8,
        },
    )
    assert insurance_response.status_code == 303
    r = await client.get(f"/households/{household_id}/insurance")
    assert r.status_code == 200
    assert "保障分析" in r.text
    assert "10,000,000円" in r.text
    invalid_insurance = await client.post(
        f"/households/{household_id}/insurance",
        data={
            "name": "不正な保険",
            "insurance_type": "死亡保障",
            "insured_member_id": "missing-member",
            "payer_member_id": husband_id,
            "monthly_premium": 10000,
        },
    )
    assert invalid_insurance.status_code == 400
    stored_household = await get_household(household_id)
    insurance_id = stored_household.insurances[0].id
    delete_response = await client.post(
        f"/households/{household_id}/insurance/{insurance_id}/delete"
    )
    assert delete_response.status_code == 303
    r = await client.get(f"/households/{household_id}/insurance")
    assert "定期生命保険" not in r.text

    for path, label in [
        ("housing", "Q6. 住まい"),
        ("vehicles", "Q7. 乗り物"),
        ("events", "Q8. ライフイベント"),
    ]:
        r = await client.get(f"/households/{household_id}/{path}")
        assert r.status_code == 200
        assert label in r.text

    # 7. シミュレーション実行
    r = await client.get(f"/households/{household_id}/simulate")
    assert r.status_code == 200
    assert "シミュレーション結果" in r.text
    assert "貯蓄残高推移" in r.text
    assert "年次キャッシュフロー" in r.text
    # 残高が表示されている
    assert "最低貯蓄残高" in r.text
    assert "金融資産合計" in r.text
    assert "固定資産税" in r.text
    assert "修繕費" in r.text
    assert "乗り物売却" in r.text
    assert "乗り物購入" in r.text
    assert "表示範囲" in r.text

    r = await client.get(f"/households/{household_id}/simulate?display_range=1")
    assert r.status_code == 200
    assert "display_range=3" in r.text
    assert f"/households/{household_id}/simulate/monthly?year=2026" in r.text
    assert f"/households/{household_id}/simulate/monthly?year=2027" not in r.text

    # CSV/Excelエクスポート
    csv_response = await client.get(
        f"/households/{household_id}/export.csv?granularity=yearly"
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "年" in csv_response.content.decode("utf-8-sig")
    assert "固定資産税" in csv_response.content.decode("utf-8-sig")
    assert "乗り物売却" in csv_response.content.decode("utf-8-sig")
    xlsx_response = await client.get(
        f"/households/{household_id}/export.xlsx?granularity=monthly"
    )
    assert xlsx_response.status_code == 200
    assert xlsx_response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert xlsx_response.content[:2] == b"PK"

    # 年次行から月次明細へドリルダウン
    r = await client.get(f"/households/{household_id}/simulate/monthly?year=2026")
    assert r.status_code == 200
    assert "2026年 月次キャッシュフロー" in r.text
    assert "月末残高" in r.text
    assert "2026/01" in r.text
    assert "計算根拠" in r.text
    assert "社会保険料" in r.text
    assert "2026年の集計" in r.text
    assert "税・社会保険合計" in r.text
    assert "翌年 →" in r.text
    assert "標準報酬月額" in r.text

    # 現行プランと前提変更プランを比較
    r = await client.get(
        f"/households/{household_id}/compare"
        "?alternative_name=積極運用&alternative_inflation_rate=0.02"
        "&alternative_investment_return_rate=0.03"
    )
    assert r.status_code == 200
    assert "プラン比較" in r.text
    assert "積極運用" in r.text
    assert "最低貯蓄残高" in r.text
    assert "年次比較" in r.text

    # 保存済みプラン同士を比較
    from fp_simulator.db.database import get_household, save_plan_snapshot

    current = await get_household(household_id)
    plan_a = await save_plan_snapshot(household_id, current, "保存基準")
    plan_b = await save_plan_snapshot(household_id, current, "保存変更")
    r = await client.get(
        f"/households/{household_id}/compare"
        f"?plan_a_id={plan_a['id']}&plan_b_id={plan_b['id']}"
    )
    assert r.status_code == 200
    assert "保存基準" in r.text
    assert "保存変更" in r.text
    assert "保存済みプランを比較" in r.text
    assert "差分（比較先 − 比較元）" in r.text
    assert "収支差分" in r.text

    # シミュレーション期間が異なるプラン同士でも500にならない
    current_shorter = current.model_copy(deep=True)
    current_shorter.members[0].life_expectancy_age = 50
    plan_short = await save_plan_snapshot(household_id, current_shorter, "短命プラン")
    r = await client.get(
        f"/households/{household_id}/compare"
        f"?plan_a_id={plan_a['id']}&plan_b_id={plan_short['id']}"
    )
    assert r.status_code == 200

    # 保存プランの月次ドリルダウンで plan_id が維持される
    r = await client.get(
        f"/households/{household_id}/simulate/monthly?year=2026&plan_id={plan_a['id']}"
    )
    assert r.status_code == 200
    assert "保存基準" in r.text

    # 万が一シナリオ
    r = await client.get(f"/households/{household_id}/disaster")
    assert r.status_code == 200
    assert "万が一シナリオ" in r.text
    r = await client.get(
        f"/households/{household_id}/disaster"
        f"?deceased_member_id={husband_id}&death_age=40"
    )
    assert r.status_code == 200
    assert "生きる道" in r.text
    assert "万が一年末残高" in r.text
    assert "遺族年金（月額・世帯合計）" in r.text
    r = await client.get(
        f"/households/{household_id}/disaster"
        f"?deceased_member_id={husband_id}&death_age=40"
        "&living_expense_reduction_rate=1.1"
    )
    assert r.status_code == 400
