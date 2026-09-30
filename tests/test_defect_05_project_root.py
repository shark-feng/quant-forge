"""回归缺陷 #5：``PROJECT_ROOT`` 硬编码（``parents[3]``）。

原缺陷：``PROJECT_ROOT = Path(__file__).resolve().parents[3]`` —— 包被安装到
site-packages 后会指向错误目录，且相对配置路径全部失效。

修复后优先级：
1. 环境变量 ``AQS_PROJECT_ROOT``（无效目录则告警并继续）；
2. 自包目录向上查找 ``pyproject.toml`` / ``.git`` / ``setup.cfg``；
3. 兜底 ``parents[3]``（保持历史行为）。
"""

from __future__ import annotations

from pathlib import Path

from tests.compat import approx
from tests.tools import PROJECT_ROOT as TESTS_ROOT
from tests.tools import workspace_tmp

from aqs.config.loader import (
    ENV_PROJECT_ROOT,
    PROJECT_ROOT,
    clear_project_root_cache,
    discover_project_root,
    resolve_path,
    resolve_project_root,
)


def setup_function() -> None:
    clear_project_root_cache()


def teardown_function() -> None:
    clear_project_root_cache()


def test_environment_variable_takes_priority():
    env = {ENV_PROJECT_ROOT: str(TESTS_ROOT)}
    assert resolve_project_root(env=env) == TESTS_ROOT.resolve()


def test_invalid_environment_variable_is_ignored():
    env = {ENV_PROJECT_ROOT: str(TESTS_ROOT / "__not_exists__")}
    resolved = resolve_project_root(env=env)
    assert resolved.exists()
    assert (resolved / "pyproject.toml").exists()


def test_discovery_walks_up_to_pyproject():
    with workspace_tmp("root") as tmp:
        root = tmp / "a" / "b" / "c"
        root.mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        deep = root / "d" / "e"
        deep.mkdir(parents=True)

        assert discover_project_root(deep) == root
        clear_project_root_cache()
        assert resolve_project_root(start=deep, env={}) == root


def test_discovery_stops_at_max_levels():
    with workspace_tmp("root") as tmp:
        root = tmp
        (root / "pyproject.toml").write_text("x", encoding="utf-8")
        deep = root
        for i in range(8):
            deep = deep / f"l{i}"
        deep.mkdir(parents=True)
        # 8 层深、只允许 3 层 → 找不到
        assert discover_project_root(deep, max_levels=3) is None


def test_discovery_accepts_git_marker_only():
    with workspace_tmp("root") as tmp:
        (tmp / ".git").mkdir()
        assert discover_project_root(tmp) == tmp


def test_default_project_root_is_repo_root():
    assert PROJECT_ROOT.exists()
    assert (PROJECT_ROOT / "pyproject.toml").exists()
    assert (PROJECT_ROOT / "src" / "aqs").is_dir()


def test_resolve_path_uses_resolved_root():
    resolved = resolve_path("configs/base.yaml")
    assert resolved is not None
    assert resolved.is_absolute()
    assert resolved.exists()
    assert resolved.parent.name == "configs"


def test_resolve_path_accepts_explicit_root():
    with workspace_tmp("root") as tmp:
        target = resolve_path("configs/base.yaml", root=tmp)
        assert target is not None
        assert target.parent == tmp / "configs"


def test_resolve_path_passthrough_for_absolute_and_none():
    assert resolve_path(None) is None
    absolute = TESTS_ROOT / "pyproject.toml"
    assert resolve_path(absolute) == absolute


def test_cache_can_be_cleared():
    first = resolve_project_root(env={})
    clear_project_root_cache()
    second = resolve_project_root(env={})
    assert first == second
    assert first.exists()


def test_package_installed_layout_still_resolves():
    """模拟「包被安装到别处」：向上找不到标记时退化为兜底路径而不抛异常。"""
    with workspace_tmp("root") as tmp:
        isolated = tmp / "site-packages" / "aqs" / "config"
        isolated.mkdir(parents=True)
        # 该目录在仓库工作区内，向上查找会命中真实仓库根（说明是「找标记」而非「数层数」）
        assert discover_project_root(isolated) == TESTS_ROOT.resolve()
        resolved = resolve_project_root(start=isolated, env={}, use_cache=False)
        assert isinstance(resolved, Path)
        assert (resolved / "pyproject.toml").exists()
