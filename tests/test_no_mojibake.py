"""乱码机械守护（把「不要用 PowerShell 文本管道处理中文 UTF-8」变成会亮的红灯）。

背景：本项目已**三次**因为 ``Get-Content -Raw | -replace | Set-Content`` 这类
PowerShell 文本往返，把含中文的 UTF-8 文件（`README.md`、`docs/03_acceptance_report.md`、
`tools/diag_determinism.py`）写成 GBK 乱码而整文件不可用。

纪律写在 `docs/DEVELOPMENT.md`，但纪律依赖人的记忆力；本模块把它变成机械检查。

**能力边界（如实说明）**：这是针对「已知失败模式」的回归守护，不是通用乱码检测器。
GBK 误读后的文本**仍是合法 UTF-8**，字节级检查抓不到它，因此必须依赖特征串比对。
特征串取自本会话真实产生过的损坏文本。

刻意**不做**的检查：用「中文连续长度」之类的启发式判乱码 ——
正常中文技术文档本身就有长中文串，该启发式无法区分二者，只会制造假红灯。
"""

from __future__ import annotations

from pathlib import Path

from tests.tools import PROJECT_ROOT

SCAN_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".txt", ".json", ".toml", ".cfg"}
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".tmp_tests",
    ".tmp_diag",
    ".venv",
    "node_modules",
    "reports",
    "data",
}

# 本会话真实损坏文本的片段（全部以 \u 转义书写，避免本模块把自己判为乱码）。
# 原始正常文本：「诊断脚本：定位…」「哈希…」
CORRUPTION_SAMPLES = (
    "\u7487\u5a43\u67c7\u9474\u6c2d\u6e70",  # 璇婃柇鑴氭湰
    "\u951b\u6c2c\u757e\u6d63\u5d83",        # 锛氬畾浣嶃
    "\u5c7d\u6431\u752f\u5c84",              # 屽搱甯岄
    "\u6434\u5fcb",                          # 搴忋
)

# 特征字：所有出现在损坏样本中的罕见字（常见汉字一律不收，避免误报）
MOJIBAKE_SIGNATURE_CHARS = tuple(sorted({ch for s in CORRUPTION_SAMPLES for ch in s}))


def _detect_corruption(text: str) -> list[str]:
    """返回文本中命中的损坏样本列表（空列表表示未命中）。"""
    return [sample for sample in CORRUPTION_SAMPLES if sample in text]


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        out.append(path)
    return out


def _is_self(path: Path) -> bool:
    return path.resolve() == Path(__file__).resolve()


# --------------------------------------------------------------------------- #
# 1. 字节级检查
# --------------------------------------------------------------------------- #
def test_repository_has_scannable_files():
    """哨兵：扫描目标不能为空（否则本模块会变成假绿灯）。"""
    files = _iter_files()
    assert len(files) > 50, f"可扫描文件过少（{len(files)}），检查 SKIP_DIRS 是否过滤过猛"
    assert any(f.name == "README.md" for f in files)
    assert any(f.suffix == ".py" for f in files)


def test_all_text_files_decode_as_utf8():
    """所有文本文件必须能按 UTF-8 解码（字节级损坏的第一道防线）。"""
    failures: list[str] = []
    for path in _iter_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")
    assert not failures, "以下文件不是合法 UTF-8（很可能被文本管道写坏）：\n" + "\n".join(failures)


def test_no_replacement_character():
    """不得出现 U+FFFD（解码失败的痕迹）。"""
    bad: list[str] = []
    for path in _iter_files():
        if "\ufffd" in path.read_text(encoding="utf-8"):
            bad.append(str(path.relative_to(PROJECT_ROOT)))
    assert not bad, f"以下文件包含替换字符 U+FFFD：{bad}"


# --------------------------------------------------------------------------- #
# 2. 已知损坏特征串
# --------------------------------------------------------------------------- #
def test_no_known_corruption_samples():
    """不得出现本会话真实产生过的乱码片段。"""
    bad: list[str] = []
    for path in _iter_files():
        if _is_self(path):
            continue
        hits = _detect_corruption(path.read_text(encoding="utf-8"))
        if hits:
            bad.append(f"{path.relative_to(PROJECT_ROOT)}: {hits}")
    assert not bad, "以下文件疑似 GBK/UTF-8 编码损坏：\n" + "\n".join(bad)


def test_no_rare_signature_char_clusters():
    """同一文件命中 ≥3 个罕见特征字即判为损坏（比整串匹配更早发现部分损坏）。"""
    bad: list[str] = []
    for path in _iter_files():
        if _is_self(path):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [ch for ch in MOJIBAKE_SIGNATURE_CHARS if ch in text]
        if len(hits) >= 3:
            bad.append(f"{path.relative_to(PROJECT_ROOT)}: {''.join(hits)}")
    assert not bad, "以下文件疑似乱码（罕见特征字聚集）：\n" + "\n".join(bad)


# --------------------------------------------------------------------------- #
# 3. 检测器灵敏度（用同一份检测逻辑，避免「测试的是另一个实现」）
# --------------------------------------------------------------------------- #
def test_detector_flags_corrupted_text():
    """把损坏样本混进正常中文里，检测器必须命中。"""
    normal = "策略在每日收盘后生成信号，组合层据此计算目标权重，风控在撮合前做最后校验。"
    assert _detect_corruption(normal) == [], "正常中文不应被误判"
    for sample in CORRUPTION_SAMPLES:
        assert _detect_corruption(normal + sample + normal), f"损坏样本未被检出：{sample!r}"


def test_signature_chars_are_not_common_characters():
    """特征字集合不得包含常用汉字（否则会对正常文档误报）。"""
    common = "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感"
    overlap = set(common) & set(MOJIBAKE_SIGNATURE_CHARS)
    assert not overlap, f"特征字集合含常用汉字，会误报：{''.join(sorted(overlap))}"


def test_documented_rule_exists():
    """纪律本身必须写在开发合同里（防止测试与文档脱节）。"""
    doc = (PROJECT_ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "PowerShell" in doc
    assert "乱码" in doc
    assert "Get-Content" in doc or "Set-Content" in doc
