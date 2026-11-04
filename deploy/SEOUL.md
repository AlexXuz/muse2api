# Seoul deployment

- Public admin: https://alexych.asia/muse/
- API base: https://alexych.asia/muse/v1
- One Uvicorn worker and `MUSE2API_CONCURRENCY=1`.
- Code: `/opt/muse2api`; runtime data and profiles: `/var/lib/muse2api`.
- Secret settings: `/var/lib/muse2api/.env` (mode 0600, service user only).
- API and CDP bind to loopback on 18610 and 19210.
- Service user: `muse2api`; writable runtime directory only, no access to user homes.
- systemd: MemoryHigh 800 MiB, MemoryMax 1000 MiB, MemorySwapMax 512 MiB, CPUQuota 100% (one core), bounded restarts.
- Host swap: 2 GiB `/swapfile-muse2api`. This is a pressure buffer, not extra RAM.
- Caddy limits request bodies to 16 MiB. Generation concurrency does not bound all queued media allocations; the service memory cap protects the other applications but may restart Muse under oversized workloads.
- In-panel upstream update/repository operations are disabled at Caddy because they target a different upstream and overwrite the deployed subpath adaptation. Update through the deployment checkout instead.
- A blank account pool is expected on first installation. Health/authentication/browser startup checks do not prove logged-in generation.

Code is based on AlexXuz/muse2api main `58c1e7c01355ca8c10076725143d5a8ca3ceba06`, with path-prefix and loopback CDP changes. Deployment uses `--root-path /muse`; the proxy strips that prefix. Root deployment remains supported.

To disable Muse, stop/disable only `muse2api.service`, remove only the `/muse` redirect and handler from Caddy, validate and reload Caddy. Preserve `/var/lib/muse2api` unless deletion is explicitly wanted. Caddy's predeployment configuration is saved beside the active Caddyfile; do not blindly restore it after unrelated route changes.
