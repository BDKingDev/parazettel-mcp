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

.EXAMPLE
  pwsh scripts/restart_daemon.ps1 -OrphanMaxAgeHours 12   # only reap very old strays
#>
param(
  # Before restarting, reap stray (non-daemon) parazettel facade processes older
  # than this many hours. Orphaned MCP facades never get cleaned up on session
  # end and keep pinning VRAM (a loaded reranker/CUDA context) and cache file
  # locks. Active sessions are far younger than this default, so they are never
  # touched. Set very high to disable reaping.
  [double]$OrphanMaxAgeHours = 6
)
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

function Remove-OrphanFacades([double]$maxAgeHours) {
  # Kill stray facade processes (python -m parazettel_mcp.main WITHOUT
  # --run-daemon) older than $maxAgeHours. These are sessions that ended without
  # cleaning up; each still pins its CUDA context / model and OS handles. The
  # live daemon and any active session (younger than the threshold) are spared.
  $now = Get-Date
  $orphans = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
      Where-Object { $_.CommandLine -match 'parazettel_mcp\.main' -and $_.CommandLine -notmatch '--run-daemon' } |
      ForEach-Object {
        $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        if ($p -and (($now - $p.StartTime).TotalHours -gt $maxAgeHours)) { $p }
      }
  )
  if (-not $orphans) { Write-Host "No orphan facades older than ${maxAgeHours}h."; return }
  foreach ($p in $orphans) {
    $age = [math]::Round(($now - $p.StartTime).TotalHours, 1)
    Write-Host "Reaping orphan facade PID $($p.Id) (age ${age}h)"
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
    catch { Write-Host "  (could not stop $($p.Id): $($_.Exception.Message))" }
  }
}

function Clear-StaleLocks([string]$repo) {
  # Best-effort removal of leftover lock files from processes that died mid-load.
  # An actively-held lock cannot be deleted on Windows (the open handle blocks
  # it), so this can only ever clear genuinely stale ones — safe to run blindly.
  $targets = @()
  $cache = Join-Path $repo "data\fastembed_cache"
  if (Test-Path $cache) {
    $targets += Get-ChildItem -Path $cache -Recurse -Filter '*.lock' -File -ErrorAction SilentlyContinue
  }
  $runtimeDir = if (Test-Path "env:PARAZETTEL_DAEMON_RUNTIME_DIR") { $env:PARAZETTEL_DAEMON_RUNTIME_DIR } else { Join-Path $env:TEMP "parazettel-daemon" }
  $startLock = Join-Path $runtimeDir "daemon-start.lock"
  if (Test-Path $startLock) { $targets += Get-Item $startLock }
  foreach ($f in $targets) {
    try { Remove-Item $f.FullName -Force -ErrorAction Stop; Write-Host "Cleared stale lock: $($f.FullName)" }
    catch { }  # held by a live process -> leave it
  }
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
# Keep the per-facade dedup reranker on CPU so concurrent/orphaned sessions never
# stack cross-encoders onto the GPU (the embedding-flap cause); the embedder above
# keeps the card to itself. Override only for a single always-on session.
Set-DefaultEnv "PARAZETTEL_DEDUP_RERANK_DEVICE" "cpu"
Set-DefaultEnv "FASTEMBED_CACHE_PATH"        (Join-Path $repo "data\fastembed_cache")

Write-Host "Stopping any running Parazettel daemon..."
& $py -m parazettel_mcp.main --stop-daemon

Write-Host "Reaping orphan facade processes (older than ${OrphanMaxAgeHours}h)..."
Remove-OrphanFacades $OrphanMaxAgeHours
Write-Host "Clearing stale model-cache / daemon-start locks..."
Clear-StaleLocks $repo

Write-Host "Starting a fresh detached daemon (embeddings: $env:PARAZETTEL_EMBEDDING_MODEL on $env:PARAZETTEL_EMBEDDING_DEVICE)..."
# ensure_daemon_running spawns a properly detached background daemon on Windows
# (pythonw, no window) and blocks only until the health endpoint is ready.
& $py -c "import argparse; from parazettel_mcp import main as m; m.ensure_daemon_running(argparse.Namespace(log_level='INFO'))"

Write-Host "--- Daemon status ---"
& $py -m parazettel_mcp.main --daemon-status
