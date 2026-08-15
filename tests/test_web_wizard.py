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
    assert "@media (max-width: 720px)" in r.text
    assert "min-height: 44px" in r.text
    assert "overflow-x: auto" in r.text

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
    assert "家族 / イベント" in r.text
    assert "主なイベント" in r.text
    assert "家族の年齢と主なライフイベント" in r.text
    assert "住宅購入" in r.text
    assert "ファミリーカー" in r.text
    # 重要指標カードと収支内訳グラフ(生涯全体)
    assert 'class="summary-cards"' in r.text
    assert "card-value" in r.text
    assert 'id="incomeChart"' in r.text
    assert 'id="expenseChart"' in r.text
    assert "年次収入構成" in r.text
    assert "生涯の支出内訳" in r.text
    # 円グラフのカテゴリラベルはJSON内でUnicodeエスケープされて出力される
    assert "\\u4e57\\u308a\\u7269\\u95a2\\u9023" in r.text  # 乗り物関連

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
    # 月次表は主要8列＋内訳・計算根拠列に削減され、詳細列は展開内の金額内訳へ移動
    assert "内訳・計算根拠" in r.text
    assert "金額内訳" in r.text
    assert "<th>乗り物売却</th>" not in r.text
    assert "<th>車検</th>" not in r.text
    assert "<th>iDeCo受取</th>" not in r.text
    # 2026/01は乗り物購入・住宅頭金が発生する月ではないが、iDeCo/NISA残高があれば表示される
    # 2026/04は住宅頭金が発生する月
    assert "住宅頭金" in r.text
    assert "社会保険料" in r.text
    assert "2026年の集計" in r.text
    assert "税・社会保険合計" in r.text
    assert "翌年 →" in r.text
    # 年ナビゲーションはセレクトボックス＋前後ボタン方式
    assert 'class="year-nav"' in r.text
    assert '<select name="year"' in r.text
    assert '<option value="2026" selected>2026年</option>' in r.text
    assert '<option value="2027" >2027年</option>' in r.text or '<option value="2027">2027年</option>' in r.text
    # 60年分の年リンクが個別アンカーとして並ばないこと
    assert r.text.count("simulate/monthly?year=") <= 4  # 前年・翌年リンクのみ
    assert "標準報酬月額" in r.text
    assert "公式出典" in r.text
    assert "日本年金機構: 厚生年金保険料率" in r.text
    assert 'rel="noopener noreferrer"' in r.text
    assert "公式出典なし" in r.text

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
    assert "児童手当" in r.text
    assert "自動計算" in r.text
    assert "遺族年金の受給判定根拠" in r.text
    assert "追加必要保障額" in r.text
    assert "簡易判定" in r.text
    assert "日本年金機構: 遺族基礎年金" in r.text
    r = await client.get(
        f"/households/{household_id}/disaster"
        f"?deceased_member_id={husband_id}&death_age=40"
        "&living_expense_reduction_rate=1.1"
    )
    assert r.status_code == 400


async def _new_household(client: AsyncClient, name: str = "テスト世帯") -> str:
    """テスト用の世帯を作成しIDを返す."""
    r = await client.post(
        "/households/new", data={"name": name, "base_year": 2026, "base_month": 1}
    )
    assert r.status_code == 303
    return r.headers["location"].split("/")[2]


async def _add_member(client: AsyncClient, household_id: str, name: str = "たろう") -> str:
    """テスト用のメンバーを追加し、そのIDを返す."""
    from fp_simulator.db.database import get_household

    r = await client.post(
        f"/households/{household_id}/members",
        data={
            "name": name,
            "relationship": "世帯主",
            "birth_date": "1996-04-01",
            "gender": "男",
            "life_expectancy_age": 90,
            "prefecture": "東京都",
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    return household.members[-1].id


async def test_wizard_forms_show_field_hints(client: AsyncClient) -> None:
    """共通の入力ガイド(field-hint)が主要な入力フォームに表示される."""
    household_id = await _new_household(client)
    for path in ("members", "incomes", "pensions", "expenses", "accounts", "loans", "education"):
        r = await client.get(f"/households/{household_id}/{path}")
        assert r.status_code == 200
        assert 'class="field-hint"' in r.text, f"{path} is missing field-hint guidance"


async def test_invalid_member_submission_preserves_values_and_shows_error(
    client: AsyncClient,
) -> None:
    """不正な入力(生年月日不正)を送信した際、エラーバナーと入力値が保持される."""
    household_id = await _new_household(client)
    r = await client.post(
        f"/households/{household_id}/members",
        data={
            "name": "不正太郎",
            "relationship": "世帯主",
            "birth_date": "not-a-date",
            "gender": "男",
            "life_expectancy_age": 90,
            "prefecture": "東京都",
        },
    )
    assert r.status_code == 400
    assert 'class="form-error"' in r.text
    # 送信済みの値(氏名)がフォームに保持されていること
    assert 'value="不正太郎"' in r.text


async def test_members_edit_flow_updates_in_place(client: AsyncClient) -> None:
    """編集リンクからのGETでプレフィルされ、POSTで新規追加ではなく上書き更新される."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id, "たろう")

    r = await client.get(f"/households/{household_id}/members?edit_id={member_id}")
    assert r.status_code == 200
    assert 'value="たろう"' in r.text
    assert ">更新<" in r.text
    assert f'value="{member_id}"' in r.text

    r = await client.post(
        f"/households/{household_id}/members",
        data={
            "name": "たろう(改名)",
            "relationship": "世帯主",
            "birth_date": "1996-04-01",
            "gender": "男",
            "life_expectancy_age": 95,
            "prefecture": "大阪府",
            "edit_id": member_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.members) == 1
    assert household.members[0].id == member_id
    assert household.members[0].name == "たろう(改名)"
    assert household.members[0].life_expectancy_age == 95


async def test_pensions_edit_and_delete(client: AsyncClient) -> None:
    """年金レコードの編集(上書き更新)と削除が機能する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)
    r = await client.post(
        f"/households/{household_id}/pensions",
        data={
            "member_id": member_id,
            "kokumin_months": 480,
            "kousei_months": 456,
            "avg_standard_remuneration": 300000,
            "start_age": 65,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    pension_id = household.pension_records[0].id
    assert pension_id  # 新規保存分はidが必ず付与される

    r = await client.get(f"/households/{household_id}/pensions?edit_id={pension_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text

    r = await client.post(
        f"/households/{household_id}/pensions",
        data={
            "member_id": member_id,
            "kokumin_months": 480,
            "kousei_months": 400,
            "avg_standard_remuneration": 350000,
            "start_age": 65,
            "edit_id": pension_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.pension_records) == 1
    assert household.pension_records[0].id == pension_id
    assert household.pension_records[0].kousei_months == 400

    r = await client.post(f"/households/{household_id}/pensions/{pension_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.pension_records == []


async def test_expenses_delete_guards_against_event_type_rows(client: AsyncClient) -> None:
    """生活費削除ルートはevent_typeが「生活費」の行のみ削除する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)
    r = await client.post(
        f"/households/{household_id}/expenses",
        data={"name": "生活費", "monthly_amount": 200000, "start_age": 0, "end_age": 0},
    )
    assert r.status_code == 303
    r = await client.post(
        f"/households/{household_id}/events",
        data={
            "event_type": "葬儀費",
            "name": "葬儀費用",
            "member_id": member_id,
            "monthly_amount": 500000,
            "cycle": "once",
            "yearly_month": 1,
            "start_age": 70,
            "start_month": 1,
            "end_age": 0,
            "end_month": 12,
            "annual_raise_rate": 0.0,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    living_expense_id = next(e.id for e in household.expenses if e.event_type == "生活費")
    event_expense_id = next(e.id for e in household.expenses if e.event_type == "葬儀費")

    # 生活費の削除ルートでイベント型の支出は削除されない
    r = await client.post(f"/households/{household_id}/expenses/{event_expense_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert any(e.id == event_expense_id for e in household.expenses)

    # イベントの削除ルートで生活費は削除されない
    r = await client.post(f"/households/{household_id}/events/{living_expense_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert any(e.id == living_expense_id for e in household.expenses)

    # 正しいルートであればそれぞれ削除される
    r = await client.post(f"/households/{household_id}/expenses/{living_expense_id}/delete")
    assert r.status_code == 303
    r = await client.post(f"/households/{household_id}/events/{event_expense_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.expenses == []


async def test_accounts_ideco_nisa_edit_and_delete(client: AsyncClient) -> None:
    """Q11の口座・iDeCo・NISAの編集(上書き)・削除がそれぞれ独立して機能する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)

    r = await client.post(
        f"/households/{household_id}/accounts",
        data={"name": "普通預金", "balance": 1000000, "interest_rate": 0.0},
    )
    assert r.status_code == 303
    r = await client.post(
        f"/households/{household_id}/ideco",
        data={
            "member_id": member_id,
            "initial_balance": 0,
            "monthly_contribution": 23000,
            "receive_start_age": 65,
            "monthly_withdrawal": 0,
            "withdrawal_tax_rate": 0,
            "annual_return_rate": 0.03,
        },
    )
    assert r.status_code == 303
    r = await client.post(
        f"/households/{household_id}/nisa",
        data={
            "member_id": member_id,
            "initial_balance": 0,
            "monthly_investment": 30000,
            "annual_return_rate": 0.03,
        },
    )
    assert r.status_code == 303

    household = await get_household(household_id)
    account_id = household.accounts[0].id
    ideco_id = household.ideco_plans[0].id
    nisa_id = household.nisa_plans[0].id

    # 口座編集(上書き)
    r = await client.get(f"/households/{household_id}/accounts?edit_account_id={account_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text
    r = await client.post(
        f"/households/{household_id}/accounts",
        data={"name": "普通預金(更新)", "balance": 2000000, "interest_rate": 0.001, "edit_id": account_id},
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.accounts) == 1
    assert household.accounts[0].name == "普通預金(更新)"
    assert household.accounts[0].balance == 2000000

    # iDeCo編集(上書き)
    r = await client.post(
        f"/households/{household_id}/ideco",
        data={
            "member_id": member_id,
            "initial_balance": 0,
            "monthly_contribution": 12000,
            "receive_start_age": 60,
            "monthly_withdrawal": 0,
            "withdrawal_tax_rate": 0,
            "annual_return_rate": 0.02,
            "edit_id": ideco_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.ideco_plans) == 1
    assert household.ideco_plans[0].monthly_contribution == 12000

    # NISA編集(上書き)
    r = await client.post(
        f"/households/{household_id}/nisa",
        data={
            "member_id": member_id,
            "initial_balance": 0,
            "monthly_investment": 50000,
            "annual_return_rate": 0.05,
            "edit_id": nisa_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.nisa_plans) == 1
    assert household.nisa_plans[0].monthly_investment == 50000

    # それぞれ独立に削除できる
    r = await client.post(f"/households/{household_id}/accounts/{account_id}/delete")
    assert r.status_code == 303
    r = await client.post(f"/households/{household_id}/ideco/{ideco_id}/delete")
    assert r.status_code == 303
    r = await client.post(f"/households/{household_id}/nisa/{nisa_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.accounts == []
    assert household.ideco_plans == []
    assert household.nisa_plans == []


async def test_loans_edit_and_vehicle_referenced_delete_guard(client: AsyncClient) -> None:
    """ローンの編集(上書き)と、乗り物から参照中のローンは削除されないガードを確認する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)
    r = await client.post(
        f"/households/{household_id}/loans",
        data={
            "member_id": member_id,
            "name": "住宅ローン",
            "principal": 30000000,
            "annual_rate": 0.01,
            "years": 35,
            "repayment_type": "元利均等",
            "start_year": 2026,
            "start_month": 4,
            "bonus_amount": 0,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    loan_id = household.loans[0].id

    r = await client.get(f"/households/{household_id}/loans?edit_id={loan_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text

    r = await client.post(
        f"/households/{household_id}/loans",
        data={
            "member_id": member_id,
            "name": "住宅ローン(借換)",
            "principal": 25000000,
            "annual_rate": 0.008,
            "years": 30,
            "repayment_type": "元利均等",
            "start_year": 2026,
            "start_month": 4,
            "bonus_amount": 0,
            "edit_id": loan_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.loans) == 1
    assert household.loans[0].name == "住宅ローン(借換)"
    assert household.loans[0].principal == 25000000

    # 乗り物からローンを参照させる
    r = await client.post(
        f"/households/{household_id}/vehicles",
        data={
            "name": "マイカー",
            "vehicle_type": "新車",
            "ownership_start_year": 2026,
            "ownership_start_month": 1,
            "ownership_end_year": 2035,
            "ownership_end_month": 12,
            "purchase_price": 3000000,
            "loan_id": loan_id,
        },
    )
    assert r.status_code == 303

    # 参照中のローンは削除されず、理由が表示される
    r = await client.post(f"/households/{household_id}/loans/{loan_id}/delete")
    assert r.status_code == 200
    assert "乗り物から参照中のため削除できません" in r.text
    household = await get_household(household_id)
    assert any(loan.id == loan_id for loan in household.loans)

    # 参照している乗り物を削除すればローンも削除できる
    vehicle_id = household.vehicles[0].id
    r = await client.post(f"/households/{household_id}/vehicles/{vehicle_id}/delete")
    assert r.status_code == 303
    r = await client.post(f"/households/{household_id}/loans/{loan_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.loans == []


async def test_education_edit_and_delete(client: AsyncClient) -> None:
    """教育費プランの編集(上書き)と削除が機能する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)
    r = await client.post(
        f"/households/{household_id}/education",
        data={"member_id": member_id, "path": "公立"},
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    plan_id = household.education_plans[0].id

    r = await client.get(f"/households/{household_id}/education?edit_id={plan_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text

    r = await client.post(
        f"/households/{household_id}/education",
        data={"member_id": member_id, "path": "私立", "include_lessons": "1", "edit_id": plan_id},
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.education_plans) == 1
    assert household.education_plans[0].path == "私立"
    assert household.education_plans[0].include_lessons is True

    r = await client.post(f"/households/{household_id}/education/{plan_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.education_plans == []


async def test_vehicles_and_insurance_edit_flow(client: AsyncClient) -> None:
    """乗り物・保険の編集リンクからのプレフィルと上書き更新を確認する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id)

    r = await client.post(
        f"/households/{household_id}/vehicles",
        data={
            "name": "ファミリーカー",
            "vehicle_type": "中古車",
            "ownership_start_year": 2026,
            "ownership_start_month": 1,
            "ownership_end_year": 2030,
            "ownership_end_month": 12,
            "purchase_price": 2000000,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    vehicle_id = household.vehicles[0].id
    r = await client.get(f"/households/{household_id}/vehicles?edit_id={vehicle_id}")
    assert r.status_code == 200
    assert 'value="ファミリーカー"' in r.text
    assert ">更新<" in r.text
    r = await client.post(
        f"/households/{household_id}/vehicles",
        data={
            "name": "ファミリーカー(買替)",
            "vehicle_type": "新車",
            "ownership_start_year": 2026,
            "ownership_start_month": 1,
            "ownership_end_year": 2030,
            "ownership_end_month": 12,
            "purchase_price": 2500000,
            "edit_id": vehicle_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.vehicles) == 1
    assert household.vehicles[0].name == "ファミリーカー(買替)"

    r = await client.post(
        f"/households/{household_id}/insurance",
        data={
            "name": "定期生命保険",
            "insurance_type": "死亡保障",
            "insured_member_id": member_id,
            "payer_member_id": member_id,
            "monthly_premium": 10000,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    insurance_id = household.insurances[0].id
    r = await client.get(f"/households/{household_id}/insurance?edit_id={insurance_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text
    r = await client.post(
        f"/households/{household_id}/insurance",
        data={
            "name": "定期生命保険(増額)",
            "insurance_type": "死亡保障",
            "insured_member_id": member_id,
            "payer_member_id": member_id,
            "monthly_premium": 15000,
            "edit_id": insurance_id,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert len(household.insurances) == 1
    assert household.insurances[0].monthly_premium == 15000


async def test_childcare_leave_crud_validation_and_simulation(client: AsyncClient) -> None:
    """Q2の産休育休CRUD、入力エラー保持、結果表示を確認する."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client)
    member_id = await _add_member(client, household_id, "はなこ")
    r = await client.post(
        f"/households/{household_id}/incomes",
        data={
            "member_id": member_id,
            "name": "会社員",
            "social_insurance_type": "給与(厚生年金)",
            "start_age": 0,
            "end_age": 60,
            "monthly_amount": 300000,
            "bonus_amount": 0,
            "retirement_allowance": 0,
            "retirement_age": 60,
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    income_id = household.incomes[0].id

    r = await client.post(
        f"/households/{household_id}/childcare-leaves",
        data={
            "income_id": income_id,
            "child_birth_date": "2026-01-01",
            "maternity_leave_start": "2026-01-01",
            "maternity_leave_end": "2026-01-31",
            "paternity_leave_start": "",
            "paternity_leave_end": "",
            "childcare_leave_start": "2026-02-01",
            "childcare_leave_end": "2026-02-20",
        },
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    leave_id = household.childcare_leaves[0].id

    r = await client.get(f"/households/{household_id}/incomes")
    assert r.status_code == 200
    assert "産休・育休の設定" in r.text
    assert "2026-01-31" in r.text
    assert f"leave_edit_id={leave_id}" in r.text

    r = await client.get(f"/households/{household_id}/incomes?leave_edit_id={leave_id}")
    assert r.status_code == 200
    assert ">更新<" in r.text
    assert 'value="2026-01-01"' in r.text

    r = await client.post(
        f"/households/{household_id}/childcare-leaves",
        data={
            "income_id": income_id,
            "child_birth_date": "2026-01-01",
            "maternity_leave_start": "2026-02-10",
            "maternity_leave_end": "2026-02-05",
            "paternity_leave_start": "",
            "paternity_leave_end": "",
            "childcare_leave_start": "",
            "childcare_leave_end": "",
            "edit_id": leave_id,
        },
    )
    assert r.status_code == 400
    assert "must not precede start" in r.text
    assert 'value="2026-02-10"' in r.text

    r = await client.get(f"/households/{household_id}/simulate")
    assert r.status_code == 200
    assert "休業給付" in r.text
    r = await client.get(f"/households/{household_id}/simulate/monthly?year=2026")
    assert r.status_code == 200
    assert "出産手当金" in r.text

    r = await client.post(f"/households/{household_id}/childcare-leaves/{leave_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.childcare_leaves == []


async def test_simulation_input_error_is_rendered_as_bad_request(client: AsyncClient) -> None:
    """世帯主未設定などの不完全データを500ではなく入力エラーとして表示する."""
    household_id = await _new_household(client, "未設定世帯")

    r = await client.get(f"/households/{household_id}/simulate")

    assert r.status_code == 400
    assert "シミュレーションを実行できません" in r.text
    assert "世帯主が見つかりません" in r.text


async def test_income_edit_and_delete_keep_childcare_leave_links_consistent(
    client: AsyncClient,
) -> None:
    """収入の対象者変更と削除で紐づく産休育休設定を整合させる."""
    from fp_simulator.db.database import get_household

    household_id = await _new_household(client, "収入リンク世帯")
    first_member_id = await _add_member(client, household_id, "一郎")
    second_member_id = await _add_member(client, household_id, "二郎")
    income_data = {
        "member_id": first_member_id,
        "name": "給与",
        "social_insurance_type": "給与(厚生年金)",
        "start_age": 0,
        "end_age": 60,
        "monthly_amount": 300000,
        "bonus_amount": 0,
        "retirement_allowance": 0,
        "retirement_age": 60,
    }
    r = await client.post(f"/households/{household_id}/incomes", data=income_data)
    assert r.status_code == 303
    household = await get_household(household_id)
    income_id = household.incomes[0].id

    r = await client.post(
        f"/households/{household_id}/childcare-leaves",
        data={
            "income_id": income_id,
            "child_birth_date": "2026-01-01",
            "maternity_leave_start": "2026-01-01",
            "maternity_leave_end": "2026-01-10",
            "paternity_leave_start": "",
            "paternity_leave_end": "",
            "childcare_leave_start": "",
            "childcare_leave_end": "",
        },
    )
    assert r.status_code == 303

    r = await client.post(
        f"/households/{household_id}/incomes",
        data={**income_data, "member_id": second_member_id, "edit_id": income_id},
    )
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.childcare_leaves[0].member_id == second_member_id

    r = await client.post(f"/households/{household_id}/incomes/{income_id}/delete")
    assert r.status_code == 303
    household = await get_household(household_id)
    assert household.incomes == []
    assert household.childcare_leaves == []
