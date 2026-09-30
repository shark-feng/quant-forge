"""配置加载：YAML + 覆盖合并 + 环境变量 + 成本场景。

优先级（后者覆盖前者）：
    默认值 < base.yaml < 附加文件/策略文件 < 显式 overrides < 环境变量 AQS__A__B__C
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from ..core.exceptions import ConfigError
from ..core.logging import get_logger
from .schema import BaseConfig, RiskConfig, _deep_merge, construct

__all__ = [
    "ENV_PROJECT_ROOT",
    "PROJECT_ROOT",
    "discover_project_root",
    "resolve_project_root",
    "clear_project_root_cache",
    "resolve_path",
    "load_yaml",
    "parse_override",
    "parse_overrides",
    "env_overrides",
    "load_base_config",
    "load_strategy_config",
    "load_risk_config",
    "load_cost_scenario",
]

logger = get_logger("config.loader")

ENV_PROJECT_ROOT = "AQS_PROJECT_ROOT"
"""环境变量：显式指定项目根目录（优先级最高）。"""

_ROOT_MARKERS = ("pyproject.toml", ".git", "setup.cfg")
_MAX_LEVELS = 6
_root_cache: dict[str, Path] = {}


def discover_project_root(start: Path, *, max_levels: int = _MAX_LEVELS) -> Path | None:
    """自 ``start`` 向上查找包含 ``pyproject.toml`` / ``.git`` / ``setup.cfg`` 的目录。

    找到返回该目录；``max_levels`` 层内未找到返回 ``None``。
    """
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for _ in range(max_levels + 1):
        for marker in _ROOT_MARKERS:
            if (current / marker).exists():
                return current
        if current.parent == current:
            break
        current = current.parent
    return None


def resolve_project_root(
    *,
    start: Path | None = None,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
) -> Path:
    """解析项目根目录（缺陷修复 #5）。

    优先级：
      1. 环境变量 ``AQS_PROJECT_ROOT``（目录不存在时告警并继续下一策略）；
      2. 自包目录向上查找 ``pyproject.toml`` / ``.git`` / ``setup.cfg``；
      3. 兜底：``src/aqs/config/loader.py`` 的 ``parents[3]``（保持历史行为）。
    """
    source = os.environ if env is None else env
    cache_key = f"{start}|{source.get(ENV_PROJECT_ROOT, '')}"
    if use_cache and cache_key in _root_cache:
        return _root_cache[cache_key]

    resolved: Path | None = None
    raw = source.get(ENV_PROJECT_ROOT)
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            if not (candidate / "pyproject.toml").exists():
                logger.warning(
                    "%s=%s 下没有 pyproject.toml，仍按环境变量处理（请确认这是项目根目录）",
                    ENV_PROJECT_ROOT,
                    candidate,
                )
            resolved = candidate.resolve()
        else:
            logger.warning("%s=%s 不是有效目录，忽略该环境变量", ENV_PROJECT_ROOT, raw)

    if resolved is None:
        resolved = discover_project_root(start or Path(__file__).resolve().parent)
    if resolved is None:
        resolved = Path(__file__).resolve().parents[3]

    if use_cache:
        _root_cache[cache_key] = resolved
    return resolved


def clear_project_root_cache() -> None:
    """清空项目根目录解析缓存（测试用）。"""
    _root_cache.clear()


PROJECT_ROOT: Path = resolve_project_root()
"""仓库根目录（等价于 ``resolve_project_root()``，进程启动时解析一次）。"""


def resolve_path(path: str | Path | None, *, root: Path | None = None) -> Path | None:
    """把相对路径解析为基于项目根目录的绝对路径。"""
    if path is None:
        return None
    p = Path(path)
    if p.is_absolute():
        return p
    return (root or PROJECT_ROOT) / p


def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 文件，返回字典。文件不存在时报 :class:`ConfigError`。"""
    p = resolve_path(path)
    assert p is not None
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{p}", value=type(data).__name__)
    return data


def parse_override(pair: str) -> tuple[list[str], Any]:
    """解析 ``"costs.commission_rate=0.0005"`` 形式的命令行覆盖。"""
    if "=" not in pair:
        raise ConfigError(f"覆盖参数格式应为 a.b.c=value，收到：{pair!r}")
    key, _, raw = pair.partition("=")
    key = key.strip()
    if not key:
        raise ConfigError(f"覆盖参数缺少键名：{pair!r}")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:  # pragma: no cover - 罕见
        raise ConfigError(f"无法解析覆盖值：{raw!r}") from exc
    return key.split("."), value


def _assign(nested: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    node = nested
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


def parse_overrides(pairs: Iterable[str]) -> dict[str, Any]:
    """把多个 ``a.b.c=value`` 合并为嵌套字典。"""
    out: dict[str, Any] = {}
    for pair in pairs:
        keys, value = parse_override(pair)
        _assign(out, keys, value)
    return out


def env_overrides(prefix: str = "AQS__", env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """从环境变量读取覆盖项，如 ``AQS__DATA__MIN_LIST_DAYS=30``。"""
    source = os.environ if env is None else env
    out: dict[str, Any] = {}
    for key, raw in source.items():
        if not key.startswith(prefix) or len(key) == len(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:  # pragma: no cover
            value = raw
        _assign(out, path, value)
    return out


def load_base_config(
    path: str | Path | None = "configs/base.yaml",
    *,
    overrides: Mapping[str, Any] | Iterable[str] | None = None,
    extra_files: Sequence[str | Path] | None = None,
    use_env: bool = True,
    apply_cost_scale: float | None = None,
) -> BaseConfig:
    """加载主配置。

    Args:
        path: 主配置文件；``None`` 表示完全使用默认值。
        overrides: 嵌套字典，或 ``["a.b=1", ...]`` 形式的字符串序列。
        extra_files: 依次叠加的附加 YAML（例如策略文件、成本场景）。
        use_env: 是否读取 ``AQS__*`` 环境变量。
        apply_cost_scale: 若不空，则对所有成本项整体缩放到该倍数。
    """
    data: dict[str, Any] = {}
    if path is not None:
        data = load_yaml(path)
    for extra in extra_files or ():
        data = _deep_merge(data, load_yaml(extra))

    if overrides is not None:
        over = dict(overrides) if isinstance(overrides, Mapping) else parse_overrides(overrides)
        data = _deep_merge(data, over)
    if use_env:
        data = _deep_merge(data, env_overrides())

    config = construct(BaseConfig, data)
    if apply_cost_scale is not None:
        config = config.with_cost_scale(apply_cost_scale)
    return config


def load_strategy_config(path: str | Path) -> dict[str, Any]:
    """读取策略配置段（``strategy:``），返回其中的内容。"""
    raw = load_yaml(path)
    section = raw.get("strategy", raw)
    if not isinstance(section, dict):
        raise ConfigError(f"策略配置格式错误：{path}")
    return section


def load_risk_config(path: str | Path | None = "configs/risk.yaml") -> RiskConfig:
    """读取 RMS 配置（``risk:`` 段）。"""
    if path is None:
        return RiskConfig()
    raw = load_yaml(path)
    section = raw.get("risk", raw)
    return construct(RiskConfig, section)


def load_cost_scenario(scenario_file: str | Path, name: str) -> dict[str, Any]:
    """读取成本敏感性场景的 overlay（见 ``configs/costs.yaml``）。"""
    raw = load_yaml(scenario_file)
    scenarios = raw.get("scenarios", {})
    if name not in scenarios:
        raise ConfigError(f"场景不存在：{name}；可用：{sorted(scenarios)}", path=str(scenario_file))
    overlay = scenarios[name].get("overlay", {})
    if not isinstance(overlay, dict):
        raise ConfigError(f"场景 {name} 的 overlay 必须是映射")
    return overlay
