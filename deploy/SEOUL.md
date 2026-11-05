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

## Native login and workspace status

The admin import page supports the existing Muse account OTP flow from the public login client (observed 2026-11-05). Its authenticated `/admin/login/*` routes keep the temporary HTTP session and CSRF state in memory, expire after ten minutes, limit attempts/resends, and store the final Muse session only after `/api/session` confirms it. Password, account creation, terms, and additional checkpoint steps require the official website. They are not completed automatically. Login initialization was verified on Seoul without requesting an OTP; full login requires the user's own account and verification code.

Login validity and workspace availability are separate. An imported session with `vm_state=DISABLED`, or a page showing Muse's explicit disconnected notice, must not be shown as usable or reported as a missing input box. This state describes that imported session, and does not prove that every session of the user's account is disabled. Existing account records, enabled settings and quota caches are preserved.

Code is based on AlexXuz/muse2api main `58c1e7c01355ca8c10076725143d5a8ca3ceba06`, with path-prefix and loopback CDP changes. Deployment uses `--root-path /muse`; the proxy strips that prefix. Root deployment remains supported.

To disable Muse, stop/disable only `muse2api.service`, remove only the `/muse` redirect and handler from Caddy, validate and reload Caddy. Preserve `/var/lib/muse2api` unless deletion is explicitly wanted. Caddy's predeployment configuration is saved beside the active Caddyfile; do not blindly restore it after unrelated route changes.
