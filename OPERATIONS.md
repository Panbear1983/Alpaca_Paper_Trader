# Operations

- Launchd schedulers own reporting/trading execution. Do not manually start duplicate jobs.
- Logs: `.logs/`.
- The event watcher has `.event_watcher.lock`; verify lock behavior before changing its lifecycle.
- Existing scheduler exit `78` incidents are separate from Hermes profile work. Diagnose the service/config error first; do not restart blindly.
- Telegram output is outbound-only unless a named migration explicitly changes ownership.
