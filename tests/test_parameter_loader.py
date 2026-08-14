"""パラメータローダーの検証テスト(ゴールデンテスト基盤).

税制改正のたびに「改正前後で変わるべき値・変わらないべき値」を固定し、
意図しない影響を検知する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import ParameterStore, get_store, reset_store


@pytest.fixture()
def store() -> ParameterStore:
    """実際の parameters/ ディレクトリを読み込んだストア."""
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


class TestParameterLoader:
    """パラメータローダーの基本動作."""

    def test_load_all_yaml_files(self, store: ParameterStore) -> None:
        """全YAMLが読み込まれ、パスが存在する."""
        paths = store.list_paths()
        assert "所得税.基礎控除.控除額" in paths
        assert "住民税.所得割.税率" in paths
        assert "社会保険.厚生年金.料率" in paths
        assert "年金.老齢基礎年金.満額" in paths

    def test_temporal_resolution(self, store: ParameterStore) -> None:
        """適用日に応じて正しい値が返る(時系列解決)."""
        # 基礎控除: 2019年は38万、2020年は48万、2025年は58万
        assert store.get("所得税.基礎控除.控除額", datetime.date(2019, 1, 1)) == 380000
        assert store.get("所得税.基礎控除.控除額", datetime.date(2020, 1, 1)) == 480000
        assert store.get("所得税.基礎控除.控除額", datetime.date(2025, 1, 1)) == 580000

    def test_source_is_recorded(self, store: ParameterStore) -> None:
        """全パラメータに出典URLが記録されている."""
        for path in store.list_paths():
            source = store.get_source(path, datetime.date.today())
            assert source.startswith("http"), f"{path} に出典がありません"

    def test_snapshot_contains_all(self, store: ParameterStore) -> None:
        """snapshot() が全パラメータを含む."""
        snap = store.snapshot(datetime.date.today())
        assert len(snap) == len(store.list_paths())
        for entry in snap.values():
            assert "value" in entry and "source" in entry

    def test_missing_path_raises(self, store: ParameterStore) -> None:
        with pytest.raises(KeyError):
            store.get("存在しない.パラメータ", datetime.date.today())


class TestGoldenMaster:
    """税制改正の回帰テスト(ゴールデンマスタ).

    既知の制度値を固定し、改正やリファクタで意図せず変わった場合に検知する。
    """

    def test_income_tax_basic_deduction_2024(self, store: ParameterStore) -> None:
        """2024年時点の基礎控除は48万円."""
        assert store.get("所得税.基礎控除.控除額", datetime.date(2024, 6, 1)) == 480000

    def test_income_tax_basic_deduction_2025(self, store: ParameterStore) -> None:
        """2025年(令和7年度改正)の基礎控除は58万円."""
        assert store.get("所得税.基礎控除.控除額", datetime.date(2025, 1, 1)) == 580000

    def test_resident_tax_rate(self, store: ParameterStore) -> None:
        """住民税の所得割は10%(変更されると多くの計算が壊れる)."""
        assert store.get("住民税.所得割.税率", datetime.date(2025, 1, 1)) == 0.10

    def test_health_insurance_is_prefecture_dependent(self, store: ParameterStore) -> None:
        """健康保険料率は都道府県別."""
        rates = store.get("社会保険.健康保険.料率", datetime.date(2025, 4, 1))
        assert isinstance(rates, dict)
        assert rates["東京都"] != rates["大阪府"]  # 都道府県で異なることを確認

    def test_pension_basic_full_amount_2025(self, store: ParameterStore) -> None:
        """2025年度の老齢基礎年金満額."""
        assert store.get("年金.老齢基礎年金.満額", datetime.date(2025, 4, 1)) == 831700

    def test_pension_early_reduction_rate(self, store: ParameterStore) -> None:
        """繰上げ受給の減額率(2022年4月以降は月0.4%)."""
        assert store.get("年金.繰上げ.減額率", datetime.date(2025, 1, 1)) == 0.004
