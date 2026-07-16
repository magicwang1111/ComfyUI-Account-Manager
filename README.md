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
- This extension improves access isolation for shared ComfyUI installs, but it is not a substitute for a full security review of your deployment.
