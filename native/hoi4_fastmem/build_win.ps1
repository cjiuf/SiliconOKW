# 编译 hoi4_fastmem Rust PyO3 扩展并部署到 calling/hoi4_fastmem.pyd
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

# 使用纯 ASCII 的 CARGO_TARGET_DIR，规避 MinGW ld 无法打开含中文路径的问题，
# 同时避免在仓库目录内产生大量编译中间产物。
$targetDir = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $env:LOCALAPPDATA "hoi4_fastmem_target" }
$env:CARGO_TARGET_DIR = $targetDir

Push-Location $here
try {
    Write-Host "[hoi4_fastmem] Building release cdylib (target=$targetDir)..."
    cargo build --release
    if ($LASTEXITCODE -ne 0) {
        throw "cargo build 失败 (exit $LASTEXITCODE)"
    }
    $dll = Join-Path $targetDir "release\hoi4_fastmem.dll"
    if (-not (Test-Path $dll)) {
        throw "未找到构建产物: $dll"
    }
    $dest = Join-Path $repoRoot "calling\hoi4_fastmem.pyd"
    Copy-Item -Force $dll $dest
    Write-Host "[hoi4_fastmem] Deployed: $dest ($((Get-Item $dest).Length) bytes)"
} finally {
    Pop-Location
}
