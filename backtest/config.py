"""Loads config/strategy.toml, expands the grid and computes the config hash."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import tomllib
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from risk.limits import RiskLimits
from strategy.factory import describe

from .costs import CostModel, Fees
from .windows import WindowSpec

MAX_GRID = 50


@dataclass(frozen=True)
class StudyConfig:
    path: Path
    raw: dict
    initial_equity: Decimal
    fees: Fees
    fee_stress: Decimal
    slippage: dict[str, Decimal]
    default_slippage: Decimal
    tradable: list[str]
    windows: WindowSpec
    selection_share: float
    min_positive: float
    min_p10_drawdown: float
    strategy: dict
    grid: dict[str, list]

    def costs(self, stressed: bool = False) -> CostModel:
        fees = self.fees.stressed(self.fee_stress) if stressed else self.fees
        return CostModel(fees, self.slippage, self.default_slippage)

    def cost_cases(self) -> dict[str, CostModel]:
        return {"base": self.costs(), "fee_stress": self.costs(stressed=True)}


def load_study_config(path: Path, project_root: Path) -> StudyConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    backtest, windows = raw["backtest"], raw["windows"]
    slippage_path = project_root / backtest["slippage_file"]
    slippage = {}
    if slippage_path.exists():
        frozen = json.loads(slippage_path.read_text(encoding="utf-8"))
        slippage = {symbol: Decimal(str(value)) for symbol, value in frozen["slippage"].items()}
    return StudyConfig(
        path=path,
        raw=raw,
        initial_equity=Decimal(backtest["initial_equity"]),
        fees=Fees(Decimal(backtest["fee_maker"]), Decimal(backtest["fee_taker"])),
        fee_stress=Decimal(backtest["fee_stress"]),
        slippage=slippage,
        default_slippage=Decimal(backtest["default_slippage"]),
        tradable=list(backtest["tradable"]),
        windows=WindowSpec(
            window_days=float(windows["window_days"]),
            step_hours=float(windows["step_hours"]),
            warmup_days=float(windows["warmup_days"]),
        ),
        selection_share=float(windows["selection_share"]),
        min_positive=float(windows["min_positive"]),
        min_p10_drawdown=float(windows["min_p10_drawdown"]),
        strategy=raw["strategy"],
        grid=raw.get("grid", {}),
    )


def _set_dotted(target: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def expand_grid(base: dict, grid: dict[str, list]) -> list[dict]:
    if not grid:
        return [copy.deepcopy(base)]
    keys = sorted(grid)
    combos = list(itertools.product(*(grid[key] for key in keys)))
    if len(combos) > MAX_GRID:
        raise ValueError(f"grid has {len(combos)} combinations; the limit is {MAX_GRID}")
    out = []
    for combo in combos:
        params = copy.deepcopy(base)
        for key, value in zip(keys, combo, strict=True):
            _set_dotted(params, key, value)
        out.append(params)
    return out


def config_hash(config: StudyConfig, params: dict, data_manifest: dict | None) -> tuple[str, dict]:
    """sha256 over everything that determines a result: strategy, costs, limits, windows and data files."""
    candle_files = {
        name: entry["sha256"]
        for name, entry in sorted(((data_manifest or {}).get("files") or {}).items())
        if name.startswith("candles/")
    }
    payload = {
        "strategy": describe(params),
        "fees": {"maker": str(config.fees.maker), "taker": str(config.fees.taker), "stress": str(config.fee_stress)},
        "slippage": {symbol: str(value) for symbol, value in sorted(config.slippage.items())},
        "default_slippage": str(config.default_slippage),
        "limits": {key: str(value) for key, value in asdict(RiskLimits()).items()},
        "initial_equity": str(config.initial_equity),
        "windows": asdict(config.windows),
        "selection_share": config.selection_share,
        "data": candle_files,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), payload
