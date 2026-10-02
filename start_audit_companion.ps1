$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$server = Join-Path $root "server"
$python = Join-Path $server ".venv\Scripts\python.exe"
$uvicorn = Join-Path $server ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path -LiteralPath $server)) {
    throw "server 目录不存在。"
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "正在创建 Python 虚拟环境..."
    & py -3 -m venv (Join-Path $server ".venv")
    if ($LASTEXITCODE -ne 0) { throw "创建 Python 虚拟环境失败。" }
    & $python -m pip install -r (Join-Path $server "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败。" }
}

$port = 8000
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    $port = 8001
}

# server/.env 由后端启动时自动读取（config.py）；此处启用本地持久化
$env:PERSISTENCE_ENABLED = "true"
$process = Start-Process -FilePath $uvicorn -ArgumentList "app.main:app --host 127.0.0.1 --port $port" -WorkingDirectory $server -PassThru

$url = "http://127.0.0.1:$port/"
$deadline = (Get-Date).AddSeconds(45)
$health = $null
while ((Get-Date) -lt $deadline -and $health.status -ne "ok") {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
        if ($health.status -ne "ok") { Start-Sleep -Milliseconds 500 }
    } catch { Start-Sleep -Milliseconds 500 }
}
if ($health.status -ne "ok") {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "后端启动失败：45 秒内健康检查未通过。"
}

try {
    $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$port/ready" -TimeoutSec 5
    if ($ready.status -ne "ready") { throw "后端尚未就绪。" }
} catch {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "后端尚未就绪：$($_.Exception.Message)"
}

$knowledge = $health.knowledge
if ($knowledge.loaded) {
    Write-Host "知识库已加载：$($knowledge.chunks) 个知识块（构建于 $($knowledge.built_at)）"
} else {
    Write-Host "提示：本地知识库索引尚未构建，答疑将只依赖大模型通用知识。"
    Write-Host "      构建命令：cd server; .\.venv\Scripts\python.exe build_knowledge_index.py"
}
if (-not $health.integrations.llm) {
    Write-Host "提示：未配置大模型（server/.env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），答疑与主观题批改将走规则模式。"
}

Start-Process $url
Write-Host "审计智能学伴已启动：$url"
Write-Host "进程 PID：$($process.Id)"
