"""M4-9：四个 provider 跑同一套契约（8 项 × 4 实现 = 32 条）。

检查项定义在 `tests/contracts/provider_contract.py::ProviderContractMixin` 里，
本模块的四个子类**只提供夹具**（继承，不重写）—— 因此运行器必须能收集继承的方法
（见 `tests/run_tests.py` 与 `tests/test_defect_10_test_hygiene.py` 的守护）。

**不用 `setUp`**：零依赖运行器只调用 `test_*` 方法本体，不调用 `setUp`；
夹具走 classmethod + 类级缓存，保证两套运行器结果一致。

AKShare 用例全程离线（`FakeAKShareClient` + `tests/fixtures/akshare/`），
不联网、不做任何真实抓取。
"""

from __future__ import annotations

from datetime import datetime, timezone

from tests.compat import skip
from tests.contracts.provider_contract import (
    END,
    START,
    ProviderContractMixin,
    temp_root,
    write_dataset,
)
from tests.fake_akshare import FakeAKShareClient

from aqs.config.schema import DataConfig
from aqs.data.akshare_provider import AKShareProvider
from aqs.data.cache import DataCache, parquet_available
from aqs.data.file_provider import CsvProvider, ParquetProvider
from aqs.data.ratelimit import RateLimiter
from aqs.data.synthetic_provider import SyntheticProvider

#: AKShare 夹具里的固定时钟：快照日确定 → effective_from 确定，测试可复现
AK_NOW = datetime(2022, 3, 5, 8, 0, 0, tzinfo=timezone.utc)


class TestSyntheticProviderContract(ProviderContractMixin):
    """合成数据源：唯一声明全部日频能力的实现（含历史指数成分、公告日财务）。"""

    tag = "synthetic"

    @classmethod
    def build_provider(cls):
        return SyntheticProvider(
            DataConfig(),
            n_symbols=8,
            seed=7,
            index_size=4,
            start=START,
            end=END,
        )


class TestCsvProviderContract(ProviderContractMixin):
    """CSV 目录数据源：能力由实际列推断（夹具刻意能力齐全）。"""

    tag = "csv"

    @classmethod
    def build_provider(cls):
        root = temp_root("csv")
        members_path = write_dataset(root, fmt="csv")
        return CsvProvider(
            root,
            index_members_path=members_path,
            fundamentals_path=root / "fundamentals.csv",
            config=DataConfig(provider="csv"),
        )


class TestParquetProviderContract(ProviderContractMixin):
    """Parquet 数据源：pyarrow 不可用时**显式跳过**（跳过不计入通过）。"""

    tag = "parquet"

    @classmethod
    def build_provider(cls):
        if not parquet_available():
            skip("未安装 pyarrow，无法验证 Parquet provider（跳过不计入通过）")
        root = temp_root("parquet")
        members_path = write_dataset(root, fmt="parquet")
        return ParquetProvider(
            root,
            index_members_path=members_path,
            fundamentals_path=root / "fundamentals.parquet",
            config=DataConfig(provider="parquet"),
        )


class TestAKShareProviderContract(ProviderContractMixin):
    """AKShare：用 FakeClient + fixtures 离线过契约（接口名与列名仍是候选值）。

    配置说明：``max_missing_ratio=1.0`` —— 契约里有一项是「请求不存在的标的必须返回
    空表 + warning 而不是抛异常」，而该源逐标的抓取、单标的失败即 100% 失败比例；
    「失败比例超阈值升级为错误」的行为已由 `tests/test_akshare_provider.py` 专门覆盖。
    """

    tag = "akshare"

    @classmethod
    def build_provider(cls):
        root = temp_root("akshare")
        client = FakeAKShareClient(
            index_snapshots={
                "000300": ["index_stock_cons_csindex_000300_20220301.csv"],
            }
        )
        return AKShareProvider(
            DataConfig(provider="akshare", max_missing_ratio=1.0),
            client=client,
            cache=DataCache(root / "cache", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0),
            sleeper=lambda _seconds: None,
            now=lambda: AK_NOW,
        )

    @classmethod
    def known_symbols(cls) -> list[str]:
        # AKShare 夹具里只有 600000；请求它才有行情，其它代码会走「无数据」路径
        return ["600000.SH"]
