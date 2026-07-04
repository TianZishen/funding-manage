$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "未找到 .venv，正在创建本地虚拟环境..."
    python -m venv (Join-Path $PSScriptRoot ".venv")
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
}

& $python (Join-Path $PSScriptRoot "web_app.py")
