"""税制パラメータローダー.

時系列YAMLパラメータを読み込み、適用日に応じた値を解決する。
OpenFisca方式: 各パラメータは適用開始日(from)と出典(source)を必須とし、
計算日に有効な最新の値を返す。
"""

from __future__ import annotations

import datetime
import decimal
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass(frozen=True)
class ParameterValue:
    """1つのパラメータの時系列値."""

    from_date: datetime.date
    value: Any
    source: str


@dataclass
class Parameter:
    """1つのパラメータ(時系列の値のリスト)."""

    path: str
    description: str
    values: list[ParameterValue] = field(default_factory=list)

    def at(self, date: datetime.date) -> Any:
        """指定日に有効な値を返す. 見つからなければ最古の値."""
        applicable = [v for v in self.values if v.from_date <= date]
        if not applicable:
            # 日付より前のエントリがなければ最古の値
            return min(self.values, key=lambda v: v.from_date).value
        # 適用可能なもののうち最新
        return max(applicable, key=lambda v: v.from_date).value

    def source_at(self, date: datetime.date) -> str:
        """指定日に有効な値の出典を返す."""
        applicable = [v for v in self.values if v.from_date <= date]
        if not applicable:
            return min(self.values, key=lambda v: v.from_date).source
        return max(applicable, key=lambda v: v.from_date).source


class ParameterStore:
    """全パラメータの格納・解決."""

    def __init__(self) -> None:
        self._params: dict[str, Parameter] = {}

    def load_dir(self, dir_path: str | pathlib.Path) -> None:
        """ディレクトリ内の全YAMLを読み込む."""
        dir_path = pathlib.Path(dir_path)
        for yaml_file in sorted(dir_path.glob("*.yaml")):
            self.load_file(yaml_file)

    def load_file(self, file_path: str | pathlib.Path) -> None:
        """1つのYAMLファイルを読み込む."""
        with open(file_path, encoding="utf-8") as f:
            entries = yaml.safe_load(f)
        if not entries:
            return
        for entry in entries:
            self._add_entry(entry)

    def _add_entry(self, entry: dict[str, Any]) -> None:
        path = entry["path"]
        description = entry.get("description", "")
        param = self._params.setdefault(path, Parameter(path=path, description=description))
        for v in entry.get("values", []):
            from_date = datetime.date.fromisoformat(str(v["from"]))
            param.values.append(
                ParameterValue(from_date=from_date, value=v["value"], source=v["source"])
            )
        # 日付順にソート
        param.values.sort(key=lambda v: v.from_date)

    def get(self, path: str, date: datetime.date) -> Any:
        """パスと日付でパラメータ値を解決."""
        if path not in self._params:
            raise KeyError(f"パラメータが見つかりません: {path}")
        return self._params[path].at(date)

    def get_source(self, path: str, date: datetime.date) -> str:
        """パラメータの出典を取得."""
        if path not in self._params:
            raise KeyError(f"パラメータが見つかりません: {path}")
        return self._params[path].source_at(date)

    def list_paths(self) -> list[str]:
        """全パラメータパスを返す."""
        return sorted(self._params.keys())

    def snapshot(self, date: datetime.date) -> dict[str, Any]:
        """指定日時点の全パラメータと出典を返す(再現性・トレーサビリティ用)."""
        return {
            path: {
                "value": self._params[path].at(date),
                "source": self._params[path].source_at(date),
            }
            for path in self.list_paths()
        }


# シングルトン的な使い方を想定(アプリ起動時にロード)
_store: ParameterStore | None = None


def get_store(parameters_dir: str | pathlib.Path | None = None) -> ParameterStore:
    """ParameterStoreを取得(初回はロード)."""
    global _store
    if _store is None:
        _store = ParameterStore()
        if parameters_dir is None:
            # 環境変数 FP_PARAMETERS_DIR があればそれを使う(本番用)
            import os
            env_dir = os.environ.get("FP_PARAMETERS_DIR")
            if env_dir:
                parameters_dir = pathlib.Path(env_dir)
            else:
                # デフォルト: リポジトリルートの parameters/
                root = pathlib.Path(__file__).resolve().parents[3]
                parameters_dir = root / "parameters"
        _store.load_dir(parameters_dir)
    return _store


def reset_store() -> None:
    """テスト用にストアをリセット."""
    global _store
    _store = None
