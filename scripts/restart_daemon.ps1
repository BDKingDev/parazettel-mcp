<#
.SYNOPSIS
  Restart the Parazettel daemon cleanly with the correct embedding environment.

.DESCRIPTION
  Stops any running daemon and starts a fresh detached one bound to THIS repo's
  vault. Because the package is installed editable, the new daemon picks up the
  latest code, and new graph columns are added idempotently on open -- so this
  is the right way to apply code changes to a running daemon.

  Embedding settings default to this vault's GPU config, but any PARAZETTEL_* /
  FASTEMBED_* value already present in the environment takes precedence (so a
  shell that already sourced the MCP client's env wins). Edit the defaults below
  if your vault uses a different model/device.

.EXAMPLE
  pwsh scripts/restart_daemon.ps1
#>
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
}
if (-not $py) {
  throw "Python not found: no interpreter at .venv\Scripts\python.exe and no python/python3 on PATH. Create the venv (uv venv --python 3.13) or activate one first."
}

function Set-DefaultEnv([string]$name, [string]$value) {
  if (-not (Test-Path "env:$name")) { Set-Item "env:$name" $value }
}

Set-DefaultEnv "PARAZETTEL_NOTES_DIR"        (Join-Path $repo "data\notes")
Set-DefaultEnv "PARAZETTEL_GRAPH_DB_PATH"    (Join-Path $repo "data\db\graph.kuzu")
Set-DefaultEnv "PARAZETTEL_BACKEND_MODE"     "daemon"
Set-DefaultEnv "PARAZETTEL_DAEMON_HOST"      "127.0.0.1"
Set-DefaultEnv "PARAZETTEL_DAEMON_PORT"      "8766"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_ENABLED"  "true"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_PROVIDER" "fastembed"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_MODEL"    "mixedbread-ai/mxbai-embed-large-v1"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_DIM"      "1024"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_METRIC"   "cosine"
Set-DefaultEnv "PARAZETTEL_EMBEDDING_DEVICE"   "cuda"
Set-DefaultEnv "FASTEMBED_CACHE_PATH"        (Join-Path $repo "data\fastembed_cache")

Write-Host "Stopping any running Parazettel daemon..."
& $py -m parazettel_mcp.main --stop-daemon

Write-Host "Starting a fresh detached daemon (embeddings: $env:PARAZETTEL_EMBEDDING_MODEL on $env:PARAZETTEL_EMBEDDING_DEVICE)..."
# ensure_daemon_running spawns a properly detached background daemon on Windows
# (pythonw, no window) and blocks only until the health endpoint is ready.
& $py -c "import argparse; from parazettel_mcp import main as m; m.ensure_daemon_running(argparse.Namespace(log_level='INFO'))"

Write-Host "--- Daemon status ---"
& $py -m parazettel_mcp.main --daemon-status
