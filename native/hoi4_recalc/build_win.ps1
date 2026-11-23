# 编译 hoi4_recalc Rust 独立离线重算 CLI 并部署到 tools/win/hoi4_recalc.exe
param(
    [string]$Proxy = ""
)
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $here)
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if (Test-Path $cargoBin) {
    $env:PATH = "$cargoBin;$env:PATH"
}
if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    $env:CARGO_HTTP_PROXY = $Proxy
}

# 使用纯 ASCII 的 CARGO_TARGET_DIR，规避 MinGW ld 无法打开含中文路径的问题
$targetDir = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $env:LOCALAPPDATA "hoi4_recalc_target" }
$env:CARGO_TARGET_DIR = $targetDir

Push-Location $here
try {
    Write-Host "[hoi4_recalc] Building release CLI (target=$targetDir)..."
    cargo build --release
    if ($LASTEXITCODE -ne 0) {
        throw "cargo build 失败 (exit $LASTEXITCODE)"
    }
    $exe = Join-Path $targetDir "release\hoi4_recalc.exe"
    if (-not (Test-Path $exe)) {
        throw "未找到构建产物: $exe"
    }
    $destDir = Join-Path $repoRoot "tools\win"
    if (-not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Path $destDir | Out-Null
    }
    $dest = Join-Path $destDir "hoi4_recalc.exe"
    Copy-Item -Force $exe $dest
    Write-Host "[hoi4_recalc] Deployed: $dest ($((Get-Item $dest).Length) bytes)"
} finally {
    Pop-Location
}
