# ComfyUI Account Manager

ComfyUI Account Manager adds login, admin-managed user registration, API token generation, IP filtering, and per-account asset isolation to ComfyUI through a custom node extension.

## Features

- Admin-first setup with managed user registration.
- JWT cookie or bearer-token authentication for ComfyUI routes.
- Per-account input, temp, output, queue, and history isolation.
- Per-account image/video asset visibility for generated outputs and uploaded assets.
- Admin accounts can inspect all account assets, including legacy public assets.
- Optional IP allow/deny lists, login timeout protection, HTTPS enforcement, and ComfyUI Manager admin-only access.

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/magicwang1111/ComfyUI-Account-Manager
cd ComfyUI-Account-Manager
pip install -r requirements.txt
```

Restart ComfyUI, open the Web UI, and register the first account. The first registered account becomes the administrator.

## Configuration

Edit `config.json` before starting ComfyUI.

```json
{
  "secret_key_env": "SECRET_KEY",
  "secret_key_file": "secret_key.txt",
  "users_db": "users_db.json",
  "history_db": "history.sqlite3",
  "distributed_queue_enabled": true,
  "scheduler_db": "scheduler.sqlite3",
  "max_concurrent_jobs_per_user": 6,
  "admin_concurrency_limit": 0,
  "worker_heartbeat_seconds": 5,
  "worker_stale_seconds": 60,
  "scheduler_secret_file": "scheduler_secret.txt",
  "access_token_expiration_hours": 8760,
  "max_access_token_expiration_hours": 8760,
  "log": "account_manager.log",
  "log_levels": ["INFO"],
  "whitelist": "whitelist.txt",
  "blacklist": "blacklist.txt",
  "blacklist_after_attempts": 0,
  "free_memory_on_logout": false,
  "force_https": false,
  "separate_users": true,
  "manager_admin_only": false
}
```

`separate_users` enables account-specific inputs, outputs, queue history, and asset visibility. The older misspelled `seperate_users` key is still accepted for compatibility, but new installs should use `separate_users`.

`distributed_queue_enabled` lets multiple ComfyUI processes share one validated
prompt queue. Every process must use the same Account Manager directory and must
set `ACCOUNT_MANAGER_INSTANCE_PORT` to its actual listen port. Ordinary accounts
can run up to `max_concurrent_jobs_per_user` prompts across the whole worker
pool; additional prompts stay queued. `admin_concurrency_limit: 0` makes the
administrator exempt from this execution limit.

The included `manage_comfyui.sh` starts 30 workers on ports 8180-8209 by
default, keeps `--enable-assets`, and gives every worker its own ComfyUI asset
database. Account Manager keeps the queue, history, ownership metadata, and
cross-worker event routing in shared plugin-managed storage.

Completed generation history is persisted in `history.sqlite3` and restored when ComfyUI restarts. The history database keeps the same 10,000-item limit as ComfyUI's in-memory history.

Temporary preview images, videos, and audio referenced by completed history are linked into the user's persistent output folder and registered in ComfyUI's asset database before the history entry is saved. This keeps Job Queue previews and Media Assets available after refreshes and restarts.

When another custom node uses an external temp directory, Account Manager resolves the exact source path from the authenticated asset reference instead of assuming ComfyUI's default temp root.

ComfyUI's optional Assets panel is separate from queue history and requires starting ComfyUI with `--enable-assets`.

## API Access

Authenticated API calls can include either:

- `Authorization: Bearer <jwt>`
- a cookie named `jwt_token`

### Register

`POST /register`

```json
{
  "new_user_username": "your_username",
  "new_user_password": "your_password",
  "username": "admin_username",
  "password": "admin_password"
}
```

### Login

`POST /login`

```json
{
  "username": "your_username",
  "password": "your_password"
}
```

The login response includes `user_settings_id`, which is used by the frontend to bind ComfyUI requests to the authenticated account.

## Notes

- Ordinary users only see assets generated or uploaded under their own account.
- Administrators can view all users' assets and legacy public assets.
- Existing public assets are not migrated automatically.
- A task running on a worker that stops heartbeating is marked
  `worker_lost`; it is never submitted again automatically.
- This extension improves access isolation for shared ComfyUI installs, but it is not a substitute for a full security review of your deployment.

## Bulk Registration

`admin/bulk_register_sentinel.py` preserves the existing form-encoded
`POST /register` contract and reads credentials from environment variables:

```bash
export COMFYUI_ADMIN_USER='admin-user'
export COMFYUI_ADMIN_PASS='admin-password'
python admin/bulk_register_sentinel.py
```

Optional variables include `COMFYUI_BASE_URL`, `COMFYUI_USERS_CSV`,
`COMFYUI_REGISTER_RESULTS`, `COMFYUI_REGISTER_TIMEOUT`, and
`COMFYUI_VERIFY_SSL`.
