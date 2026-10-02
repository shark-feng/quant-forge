"""跨 provider 契约测试包（M4-9）。

`provider_contract.py` 里的 `ProviderContractMixin` 不以 ``Test`` 开头，
故**自身不会被收集**（有守护用例断言这一点）；只有 `tests/test_provider_contract.py`
里的四个子类会被收集，从而让同一套断言在四个实现上各跑一遍。
"""
