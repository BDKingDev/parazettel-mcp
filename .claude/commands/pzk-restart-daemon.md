---
description: Restart the Parazettel MCP daemon on the current code, with embeddings, and verify health
---

Restart the Parazettel daemon so it picks up the latest code in this checkout
(the package is installed editable; a running daemon keeps its old code until
restarted). The restart MUST preserve the embedding environment or semantic
search comes up disabled.

Do this:

1. From the repository root, run the restart script:

   ```
   pwsh scripts/restart_daemon.ps1     # Windows / PowerShell
   bash scripts/restart_daemon.sh      # POSIX / Git Bash
   ```

   It stops any running daemon, starts a fresh detached one with the embedding
   environment (defaulted in the script, overridable from the shell), and prints
   the status.

2. Confirm the output shows `Parazettel daemon is running.` and
   `Graph writable: True`. If it does not, report the failure -- do not retry
   blindly.

3. Sanity-check that the NEW code is live (not a stale process) by exercising a
   recently added tool, e.g. call `pzk_briefing` (a no-arg tool) or read back a
   note. If the tool is missing from your tool list, the MCP client session
   predates it -- tell the user to reconnect the MCP server in their client (the
   daemon restart alone does not refresh a client's tool list).

Notes:
- Never start a second daemon without stopping the first -- two processes race
  for the daemon port and the loser falls back to read-only.
- If the script can't find Python, create the venv (`uv venv --python 3.13`) or
  activate one; the script also reads `python` from PATH.
- The daemon idle-times-out (default 3600s) and auto-restarts on the next
  request; a manual restart is only needed to apply code changes.
