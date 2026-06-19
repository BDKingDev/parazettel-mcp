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

  Before starting the fresh daemon it also reaps ORPHANED facade processes —
  MCP facades whose Claude session has exited but which never cleaned up and
  keep holding memory/handles (and, before the reranker moved to CPU, GPU VRAM).
  Orphans are detected by parent liveness (a facade with no running session
  ancestor), so they are reaped at any age while active sessions are never
  touched. Stale model-cache / daemon-start locks are cleared too. Use -DryRun
  to preview the reap without killing anything or restarting.

.EXAMPLE
  pwsh scripts/restart_daemon.ps1

.EXAMPLE
  pwsh scripts/restart_daemon.ps1 -DryRun   # preview which facades would be reaped; nothing killed
#>
param(
  # Preview the orphan reap without killing anything or restarting the daemon.
  [switch]$DryRun
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

function Test-FacadeOrphaned {
  # A facade is ORPHANED when no live Claude session spawned it. We decide by
  # walking up the parent chain: an ACTIVE facade reaches a live, older,
  # NON-python ancestor (claude.exe / the shell that launched the session)
  # through its python launcher parents (a venv stub re-execs the real python,
  # so there can be a python->python link). A parent that is GONE — or whose
  # creation time is NEWER than its child, meaning that PID was recycled and the
  # real parent is dead — proves the session is gone. This catches an orphan the
  # instant its session dies, at ANY age, and never flags a running session.
  param($facade, $byId)
  $cur = $facade
  for ($depth = 0; $depth -lt 8; $depth++) {
    $parent = $byId[[int]$cur.ParentProcessId]
    if (-not $parent) { return $true }                               # ancestor gone
    if ($parent.CreationDate -gt $cur.CreationDate) { return $true }  # PID recycled -> real parent dead
    # Keep this "name starts with python" launcher rule in sync with main.py's
    # _resolve_session_host (the in-process watchdog walks the same chain).
    if ($parent.Name -notmatch '^python') { return $false }          # live, older session host -> active
    $cur = $parent                                                   # climb through python launchers
  }
  return $true  # implausibly deep python chain with no host -> treat as orphan
}

function Remove-OrphanFacades([switch]$DryRun) {
  # Reap facade processes (python -m parazettel_mcp.main, NOT --run-daemon) whose
  # spawning Claude session is gone. Independent of age, so a freshly-orphaned
  # facade is reaped immediately while active sessions are never touched, however
  # long they have been open.
  # Map ALL processes by PID (not just python) so the walk can resolve the
  # non-python session host (claude.exe / shell) at the top of the chain.
  $all = @(Get-CimInstance Win32_Process)
  $byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
  $facades = $all | Where-Object {
    ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
    $_.CommandLine -match 'parazettel_mcp\.main' -and $_.CommandLine -notmatch '--run-daemon'
  }
  $reaped = 0
  foreach ($f in $facades) {
    if (-not (Test-FacadeOrphaned -facade $f -byId $byId)) { continue }
    $reaped++
    $created = $f.CreationDate.ToString('yyyy-MM-dd HH:mm:ss')
    if ($DryRun) {
      Write-Host "[dry-run] would reap orphan facade PID $($f.ProcessId) (created $created; parent PID $($f.ParentProcessId) is gone/recycled)"
      continue
    }
    Write-Host "Reaping orphan facade PID $($f.ProcessId) (created $created; no live session ancestor)"
    try { Stop-Process -Id $f.ProcessId -Force -ErrorAction Stop }
    catch { Write-Host "  (could not stop $($f.ProcessId): $($_.Exception.Message))" }
  }
  if ($reaped -eq 0) {
    Write-Host "No orphan facades found (every live facade is attached to a running session)."
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

if ($DryRun) {
  Write-Host "[dry-run] previewing orphan reap only -- nothing is killed and the daemon is left running."
  Remove-OrphanFacades -DryRun
  return
}

Write-Host "Stopping any running Parazettel daemon..."
& $py -m parazettel_mcp.main --stop-daemon

Write-Host "Reaping orphan facades (sessions that are no longer running)..."
Remove-OrphanFacades
Write-Host "Clearing stale model-cache / daemon-start locks..."
Clear-StaleLocks $repo

Write-Host "Starting a fresh detached daemon (embeddings: $env:PARAZETTEL_EMBEDDING_MODEL on $env:PARAZETTEL_EMBEDDING_DEVICE)..."
# ensure_daemon_running spawns a properly detached background daemon on Windows
# (pythonw, no window) and blocks only until the health endpoint is ready.
& $py -c "import argparse; from parazettel_mcp import main as m; m.ensure_daemon_running(argparse.Namespace(log_level='INFO'))"

Write-Host "--- Daemon status ---"
& $py -m parazettel_mcp.main --daemon-status
