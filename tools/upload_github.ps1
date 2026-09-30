<#
.SYNOPSIS
    把本仓库同步到 GitHub（quant-forge）。

.DESCRIPTION
    本机开发环境**没有外网**（对外 HTTPS 全部不可达），因此推送必须在联网机器上执行。
    本脚本把推送流程固化为可重复的步骤，并内置了防覆盖保护：

    1. 前置检查（Git 身份、远端、待提交内容、密钥扫描）；
    2. `git fetch` + **`git pull --rebase`**（避免覆盖远端已有内容）；
    3. 跑一次测试（可选，默认跑）；
    4. `git push`；
    5. 打印结果与仓库 URL。

.PARAMETER Message
    提交信息。省略时由多个文件变更自动生成。

.PARAMETER SkipTests
    跳过测试直接提交（不推荐；仅在你已单独跑过测试时使用）。

.PARAMETER NoCommit
    只做 fetch/rebase/push，不创建新提交（用于推送已有提交）。

.PARAMETER Force
    使用 `--force-with-lease` 推送（仅在明确知道远端被改写时使用）。

.PARAMETER NonInteractive
    非交互模式：命中疑似密钥时不询问，直接中止退出。
    用于 CI / 计划任务 / 远程执行等无人值守场景（避免 Read-Host 永久挂起）。

.EXAMPLE
    pwsh -File tools\upload_github.ps1 -Message "feat(M2): 工程化文件与 AKShare runbook"

.EXAMPLE
    pwsh -File tools\upload_github.ps1 -NoCommit

.EXAMPLE
    pwsh -File tools\upload_github.ps1 -NonInteractive -SkipTests
#>
[CmdletBinding()]
param(
    [string]$Message = "",
    [switch]$SkipTests,
    [switch]$NoCommit,
    [switch]$Force,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Fail([string]$text) {
    Write-Host "!! $text" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------- #
# 0. 定位仓库根目录
# --------------------------------------------------------------------------- #
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root
Write-Host "仓库根目录：$root"

if (-not (Test-Path (Join-Path $root ".git"))) {
    Fail "当前目录不是 Git 仓库（缺少 .git）：$root"
}

$env:GIT_TERMINAL_PROMPT = "0"   # 不让 git 卡在交互式凭据输入

# --------------------------------------------------------------------------- #
# 1. 前置检查
# --------------------------------------------------------------------------- #
Write-Step "检查 Git 身份与远端"

$name = git config user.name
$email = git config user.email
if (-not $name -or -not $email) {
    Fail "未配置 Git 身份。请先执行：
    git config --global user.name  `"shark-feng`"
    git config --global user.email `"434387081@qq.com`""
}
Write-Host "  user.name  = $name"
Write-Host "  user.email = $email"

$remoteUrl = git remote get-url origin 2>$null
if (-not $remoteUrl) {
    Fail "未配置远端 origin。请先执行：
    git remote add origin https://github.com/shark-feng/quant-forge.git"
}
Write-Host "  origin     = $remoteUrl"

$branch = git rev-parse --abbrev-ref HEAD
Write-Host "  当前分支   = $branch"

# --------------------------------------------------------------------------- #
# 2. 密钥扫描（提交前）
# --------------------------------------------------------------------------- #
Write-Step "密钥扫描"

$secretPattern = 'api[_-]?key|apikey|secret|token|password|passwd|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}'

# git grep 默认只扫**受版本控制**的文件，因此：
#   (1) 不需要（也不应该）把路径列表拼进命令行 —— 文件多时会超出命令行长度上限；
#   (2) 新增文件在 `git add` 之前尚未受控，必须另外逐文件扫描（见下）。
$hits = git grep -n -I -E $secretPattern 2>$null
$hits = @($hits)

$trackedCount = @(git ls-files).Count
Write-Host "  已扫描受控文件：$trackedCount 个"

$untracked = @(git ls-files --others --exclude-standard)
if ($untracked.Count -gt 0) {
    Write-Host "  另扫描未跟踪文件：$($untracked.Count) 个"
    foreach ($file in $untracked) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
        $m = Select-String -LiteralPath $file -Pattern $secretPattern -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($m) {
            $hits += ($m | ForEach-Object { "$file`:$($_.LineNumber): $($_.Line.Trim())" })
        }
    }
}

if ($hits.Count -gt 0) {
    Write-Host "命中以下疑似敏感串：" -ForegroundColor Yellow
    $hits | Select-Object -First 40 | ForEach-Object { Write-Host "  $_" }
    if ($NonInteractive) {
        Fail "命中疑似密钥且处于非交互模式（-NonInteractive），已中止。"
    }
    $answer = Read-Host "确认继续提交？(yes/no)"
    if ($answer -ne "yes") { Fail "已取消。" }
} else {
    Write-Host "  未发现疑似密钥。"
}

# --------------------------------------------------------------------------- #
# 3. 测试
# --------------------------------------------------------------------------- #
if (-not $SkipTests) {
    Write-Step "运行测试"
    $env:PYTHONPATH = "$root\src;$root"
    $env:PYTHONIOENCODING = "utf-8"
    # 测试运行器会向 stderr 写日志；在 $ErrorActionPreference="Stop" 下把原生命令的 stderr
    # 重定向进变量可能被当成终止性错误。这里临时降级为 Continue，只依据退出码判定成败。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & python "$root\tests\run_tests.py" 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $output | Select-Object -Last 6 | ForEach-Object { Write-Host "  $_" }
    if ($code -ne 0) {
        Fail "测试未通过（退出码 $code），已中止推送。"
    }
    Write-Host "  测试通过。"
} else {
    Write-Host "  已跳过测试（-SkipTests）。"
}

# --------------------------------------------------------------------------- #
# 4. 提交
# --------------------------------------------------------------------------- #
if (-not $NoCommit) {
    Write-Step "创建提交"
    git add -A
    $staged = git diff --cached --name-only
    if (-not $staged) {
        Write-Host "  没有待提交的变更。"
    } else {
        Write-Host "  待提交文件（$($staged.Count) 个）："
        $staged | Select-Object -First 30 | ForEach-Object { Write-Host "    $_" }
        if (-not $Message) {
            $Message = "chore: sync working tree ($($staged.Count) files)"
        }
        git commit -m $Message
        if ($LASTEXITCODE -ne 0) { Fail "提交失败。" }
    }
}

# --------------------------------------------------------------------------- #
# 5. fetch + rebase（防覆盖）
# --------------------------------------------------------------------------- #
Write-Step "拉取远端并 rebase（避免覆盖远端已有内容）"
git fetch origin $branch
if ($LASTEXITCODE -ne 0) {
    Fail "无法访问远端 $remoteUrl。请确认联网与凭据（HTTPS 需 PAT，或使用 SSH 远端）。"
}

$localAhead = [int](git rev-list --count "origin/$branch..HEAD" 2>$null)
Write-Host "  本地领先 origin/$branch：$localAhead 个提交"
if ($localAhead -eq 0) {
    Write-Host "  没有需要推送的提交。" -ForegroundColor Yellow
}

git pull --rebase origin $branch
if ($LASTEXITCODE -ne 0) {
    Fail "rebase 失败（可能存在冲突）。请手动解决后重新运行本脚本（-NoCommit）。"
}

# --------------------------------------------------------------------------- #
# 6. 推送
# --------------------------------------------------------------------------- #
Write-Step "推送到 GitHub"
if ($Force) {
    git push --force-with-lease origin $branch
} else {
    git push origin $branch
}
if ($LASTEXITCODE -ne 0) { Fail "推送失败。" }

# --------------------------------------------------------------------------- #
# 7. 结果
# --------------------------------------------------------------------------- #
Write-Step "完成"
$head = git rev-parse --short HEAD
Write-Host "  HEAD        = $head"
Write-Host "  远端地址    = $remoteUrl"
$web = $remoteUrl -replace '\.git$', ''
Write-Host "  仓库主页    = $web" -ForegroundColor Green
git log --oneline -3 | ForEach-Object { Write-Host "    $_" }
