import os
import sys
import warnings
import uuid
import json
from typing import Dict, Any

EXT_PATH = os.path.join(os.path.dirname(__file__), "..")
CONFIG_FILE = os.path.join(EXT_PATH, "config.json")


def load_config(file_path: str) -> Dict[str, Any]:
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


config = load_config(CONFIG_FILE)

SECRET_KEY = os.getenv(config.get("secret_key_env", "SECRET_KEY"))
SECRET_KEY_FILE = os.path.join(EXT_PATH, config.get("secret_key_file", "secret_key.txt"))

if not SECRET_KEY:
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            SECRET_KEY = f.read().strip()
    else:
        warnings.warn(
            "The SECRET_KEY environment variable is not set. A persistent key will be created in secret_key.txt."
        )
        SECRET_KEY = "".join([str(uuid.uuid4().hex) for _ in range(128)])
        with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(SECRET_KEY)

MATCH_HEADERS = {"X-Forwarded-Proto": "https"}

TOKEN_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * config.get("access_token_expiration_hours", 8760)
MAX_TOKEN_EXPIRE_MINUTES = 60 * config.get("max_access_token_expiration_hours", 8760)

USERS_FILE = os.path.join(EXT_PATH, config.get("users_db", "users_db.json"))
HISTORY_FILE = os.path.join(EXT_PATH, config.get("history_db", "history.sqlite3"))
SCHEDULER_FILE = os.path.join(
    EXT_PATH, config.get("scheduler_db", "scheduler.sqlite3")
)
LOG_FILE = os.path.join(EXT_PATH, config.get("log", "account_manager.log"))
LOG_LEVELS = config.get("log_levels", ["INFO"])

WHITELIST = os.path.join(EXT_PATH, config.get("whitelist", "whitelist.txt"))
BLACKLIST = os.path.join(EXT_PATH, config.get("blacklist", "blacklist.txt"))

BLACKLIST_AFTER_ATTEMPTS = config.get("blacklist_after_attempts")

FREE_MEMORY_ON_LOGOUT = config.get("free_memory_on_logout", False)
FORCE_HTTPS = config.get("force_https", False)

SEPARATE_USERS = config.get("separate_users", config.get("seperate_users", False))
SEPERATE_USERS = SEPARATE_USERS

MANAGER_ADMIN_ONLY = config.get("manager_admin_only", False)

DISTRIBUTED_QUEUE_ENABLED = config.get("distributed_queue_enabled", False)
MAX_CONCURRENT_JOBS_PER_USER = max(
    1, int(config.get("max_concurrent_jobs_per_user", 6))
)
ADMIN_CONCURRENCY_LIMIT = max(0, int(config.get("admin_concurrency_limit", 0)))
RESOURCE_CONCURRENCY_LIMITS = config.get("resource_concurrency_limits", {})
GPU_NODE_TYPES = config.get("gpu_node_types", [])
WORKER_HEARTBEAT_SECONDS = max(1, int(config.get("worker_heartbeat_seconds", 5)))
WORKER_STALE_SECONDS = max(
    WORKER_HEARTBEAT_SECONDS * 2,
    int(config.get("worker_stale_seconds", 60)),
)
SCHEDULER_SECRET_FILE = os.path.join(
    EXT_PATH, config.get("scheduler_secret_file", "scheduler_secret.txt")
)


def _runtime_port() -> int:
    configured = os.getenv("ACCOUNT_MANAGER_INSTANCE_PORT", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            warnings.warn("ACCOUNT_MANAGER_INSTANCE_PORT must be an integer")
    try:
        index = sys.argv.index("--port")
        return int(sys.argv[index + 1])
    except (ValueError, IndexError):
        return 8188


INSTANCE_PORT = _runtime_port()
WORKER_RESOURCE_CLASS = (
    os.getenv("ACCOUNT_MANAGER_WORKER_CLASS", "default").strip().lower() or "default"
)
INSTANCE_LOG_FILE = os.getenv("ACCOUNT_MANAGER_INSTANCE_LOG", "").strip()

CLOUD_ARCHIVE_ENABLED = bool(config.get("cloud_archive_enabled", False))
CLOUD_SERVER_ID = str(config.get("cloud_server_id", "")).strip()
CLOUD_OSS_REGION = str(config.get("cloud_oss_region", "cn-hangzhou")).strip()
CLOUD_OSS_ENDPOINT = str(config.get("cloud_oss_endpoint", "")).strip()
CLOUD_OSS_BUCKET = str(config.get("cloud_oss_bucket", "")).strip()
CLOUD_OSS_PREFIX = str(config.get("cloud_oss_prefix", "")).strip().strip("/")
CLOUD_PUBLIC_BASE_URL = str(config.get("cloud_public_base_url", "")).strip().rstrip("/")
CLOUD_MAX_ATTEMPTS = max(1, int(config.get("cloud_max_attempts", 3)))
CLOUD_UPLOAD_CONCURRENCY = min(
    2, max(1, int(config.get("cloud_upload_concurrency", 2)))
)
CLOUD_REMOTE_MAX_BYTES = max(
    1, int(config.get("cloud_remote_max_bytes", 20 * 1024 * 1024 * 1024))
)
CLOUD_MANIFEST_MAX_BYTES = max(
    1, int(config.get("cloud_manifest_max_bytes", 2 * 1024 * 1024 * 1024))
)
CLOUD_STAGING_DIR = os.path.join(
    EXT_PATH, config.get("cloud_staging_dir", "cloud_staging")
)
CLOUD_CHECKPOINT_DIR = os.path.join(
    EXT_PATH, config.get("cloud_checkpoint_dir", "cloud_upload_checkpoints")
)

WEB_DIR = os.path.join(EXT_PATH, "account-manager-web")
HTML_DIR = WEB_DIR
CSS_DIR = os.path.join(WEB_DIR, "css")
JS_DIR = os.path.join(WEB_DIR, "js")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
