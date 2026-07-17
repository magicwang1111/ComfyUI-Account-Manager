import os
import sys
import heapq
import copy
import contextvars
import logging
import mimetypes
from datetime import datetime
from aiohttp import web
from typing import Optional

import folder_paths
from server import PromptServer
from execution import PromptQueue, MAXIMUM_HISTORY_SIZE

from .users_db import UsersDB
from .history_assets import persist_temp_assets
from .history_store import HistoryStore


logger = logging.getLogger("ComfyUI-Account-Manager")


class AccessControl:
    def __init__(
        self, users_db: UsersDB, server: PromptServer, history_file: str = None
    ):
        self.users_db = users_db
        self.server = server

        self._current_user = contextvars.ContextVar("user_id", default=None)
        self.__current_user_id = None

        self.__get_output_directory = folder_paths.get_output_directory
        self.__get_temp_directory = folder_paths.get_temp_directory
        self.__get_input_directory = folder_paths.get_input_directory
        self.__get_save_image_path = folder_paths.get_save_image_path

        self.__prompt_queue = self.server.prompt_queue
        self.__history_store = None
        if history_file:
            try:
                self.__history_store = HistoryStore(history_file)
            except Exception:
                logger.exception("Failed to initialize persistent history")
        self.__prompt_queue_put = self.__prompt_queue.put
        self.__prompt_queue_get_flags = self.__prompt_queue.get_flags
        self.__user_manager_get_request_user_id = getattr(
            self.server.user_manager, "get_request_user_id", None
        )

    @property
    def folder_paths(self) -> tuple:
        return (
            self.__get_output_directory(),
            self.__get_temp_directory(),
            self.__get_input_directory(),
        )

    def set_current_user_id(self, user_id: str, set_fallback: bool = False) -> None:
        """Set the current authenticated user."""
        self._current_user.set(user_id)

        if set_fallback:
            self.__current_user_id = user_id

    def get_current_user_id(self) -> str:
        """Retrieve the active user, falling back to the latest prompt submitter."""
        if self._current_user.get():
            return self._current_user.get()

        return self.__current_user_id

    def get_context_user_id(self) -> str:
        """Retrieve only the current request/execution context user."""
        return self._current_user.get()

    def is_admin_user(self, user_id: str) -> bool:
        if not user_id:
            return False
        _, user = self.users_db.get_user(user_id=user_id)
        return bool(user and user.get("admin"))

    def _can_access_owner(self, owner_id: str, current_user_id: str = None) -> bool:
        current_user_id = current_user_id or self.get_current_user_id()
        if self.is_admin_user(current_user_id):
            return True
        return bool(owner_id and owner_id == current_user_id)

    def _get_user_slug(self, user_id: str = None) -> str:
        user_id = user_id or self.get_current_user_id()
        if not user_id:
            return "public"

        _, user = self.users_db.get_user(user_id=user_id)
        return user.get("username") or user_id

    @staticmethod
    def _get_timeline_slug() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _get_user_path_prefix(self, user_id: str = None) -> str:
        """Build the user-visible folder prefix for newly generated files."""
        user_id = user_id or self.get_current_user_id()
        return f"{self._get_timeline_slug()}/{self._get_user_slug(user_id)}"

    def _is_current_user_prefix(self, path_prefix: str, user_id: str = None) -> bool:
        user_id = user_id or self.get_current_user_id()
        if not user_id or not path_prefix:
            return False

        normalized = os.path.normpath(str(path_prefix)).replace("\\", "/").strip("/")
        current_prefix = self._get_user_path_prefix(user_id)
        if normalized == current_prefix or normalized.startswith(f"{current_prefix}/"):
            return True

        # Backward compatibility for files generated before the timeline/username layout.
        return normalized == user_id or normalized.startswith(f"{user_id}/")

    def _can_access_output_subfolder(
        self, subfolder: str, current_user_id: str = None
    ) -> bool:
        current_user_id = current_user_id or self.get_current_user_id()
        if self.is_admin_user(current_user_id):
            return True
        if not current_user_id:
            return False

        normalized = os.path.normpath(str(subfolder or "")).replace("\\", "/").strip("/")
        if not normalized:
            return False

        if normalized == current_user_id or normalized.startswith(f"{current_user_id}/"):
            return True

        username = self._get_user_slug(current_user_id)
        parts = normalized.split("/")
        return len(parts) >= 2 and parts[1] == username

    def _with_user_prefix(self, filename_prefix: str) -> str:
        user_id = self.get_current_user_id()
        if not user_id or not filename_prefix:
            return filename_prefix

        normalized = os.path.normpath(str(filename_prefix)).replace("\\", "/")
        if self._is_current_user_prefix(normalized, user_id):
            return filename_prefix

        return f"{self._get_user_path_prefix(user_id)}/{filename_prefix}"

    def get_user_output_directory(self) -> str:
        """Get the user-specific output directory."""
        return os.path.join(
            self.__get_output_directory(),
            *self._get_user_path_prefix().split("/"),
        )

    def get_user_temp_directory(self) -> str:
        """Get the user-specific temp directory."""
        return os.path.join(
            self.__get_temp_directory(),
            self._get_user_slug(),
        )

    def get_user_input_directory(self) -> str:
        """Get the user-specific input directory."""
        input_directory = os.path.join(
            self.__get_input_directory(),
            self._get_user_slug(),
        )

        os.makedirs(input_directory, exist_ok=True)

        return input_directory

    def add_user_specific_folder_paths(self, json_data) -> None:
        """Add user-specific output prefixes to prompt JSON data."""
        if isinstance(json_data, dict):
            for key, value in json_data.items():
                if key == "filename_prefix" and isinstance(value, str):
                    json_data[key] = self._with_user_prefix(value)
                else:
                    self.add_user_specific_folder_paths(value)
        elif isinstance(json_data, list):
            for item in json_data:
                self.add_user_specific_folder_paths(item)

        return json_data

    def get_user_save_image_path(
        self,
        filename_prefix: str,
        output_dir: str,
        image_width=0,
        image_height=0,
    ) -> tuple[str, str, int, str, str]:
        """Ensure standard ComfyUI save helpers write under the current user."""
        return self.__get_save_image_path(
            self._with_user_prefix(filename_prefix),
            output_dir,
            image_width,
            image_height,
        )

    def patch_folder_paths(self) -> None:
        """Patch folder paths and save helpers with user-specific behavior."""
        folder_paths.get_temp_directory = self.get_user_temp_directory
        folder_paths.get_input_directory = self.get_user_input_directory
        folder_paths.get_save_image_path = self.get_user_save_image_path

        self.server.add_on_prompt_handler(self.add_user_specific_folder_paths)

    def sync_comfy_users(self) -> None:
        """Mirror account-manager users into ComfyUI's in-memory user map."""
        user_manager = getattr(self.server, "user_manager", None)
        if not user_manager or not hasattr(user_manager, "users"):
            return

        for user_id, user in self.users_db.load_users().items():
            username = user.get("username") or user_id
            user_manager.users[user_id] = username

    def patch_comfy_user_manager(self) -> None:
        """Let ComfyUI APIs resolve authenticated account-manager users."""
        user_manager = getattr(self.server, "user_manager", None)
        if not user_manager or self.__user_manager_get_request_user_id is None:
            return

        def get_request_user_id(request):
            user_id = request.get("user_id")
            if user_id and user_id in self.users_db.load_users():
                return user_id

            header_user_id = request.headers.get("comfy-user")
            if header_user_id and header_user_id in self.users_db.load_users():
                return header_user_id

            return self.__user_manager_get_request_user_id(request)

        user_manager.get_request_user_id = get_request_user_id
        self.sync_comfy_users()

    def create_view_access_control_middleware(self) -> web.middleware:
        """Guard /view and /api/view output access by top-level user folder."""

        @web.middleware
        async def view_access_control_middleware(
            request: web.Request, handler
        ) -> web.Response:
            if request.path not in ("/view", "/api/view"):
                return await handler(request)

            view_type = request.rel_url.query.get("type", "output")
            if view_type != "output":
                return await handler(request)

            user_id = request.get("user_id")
            if self.is_admin_user(user_id):
                return await handler(request)

            if self._can_access_output_subfolder(
                request.rel_url.query.get("subfolder", ""), user_id
            ):
                return await handler(request)

            return web.HTTPForbidden(reason="You do not have access to this output.")

        return view_access_control_middleware

    def create_folder_access_control_middleware(
        self, folder_paths: tuple = ()
    ) -> web.middleware:
        """Create middleware for direct folder access control."""

        folder_paths = folder_paths or self.folder_paths

        @web.middleware
        async def folder_access_control_middleware(
            request: web.Request, handler
        ) -> web.Response:
            if not request.path.startswith(folder_paths):
                return await handler(request)

            current_user_id = request.get("user_id")

            try:
                path_parts = request.path.strip("/").split("/")
                folder_user_id = path_parts[1]
            except Exception:
                return web.HTTPNotFound(reason="Folder not found.")

            if not self._can_access_owner(folder_user_id, current_user_id):
                return web.HTTPForbidden(
                    reason="You do not have access to this folder."
                )

            return await handler(request)

        return folder_access_control_middleware

    def _wrap_queue_item(self, item):
        if isinstance(item, tuple) and len(item) >= 7:
            return item
        return item + (self.get_current_user_id(),)

    @staticmethod
    def _unwrap_queue_item(item):
        if isinstance(item, tuple) and len(item) >= 7:
            return item[:6]
        return item

    @staticmethod
    def _queue_item_user_id(item):
        if isinstance(item, tuple) and len(item) >= 7:
            return item[6]
        return None

    def _visible_queue_items(self, items):
        current_user_id = self.get_current_user_id()
        visible = []
        for item in items:
            owner_id = self._queue_item_user_id(item)
            if self._can_access_owner(owner_id, current_user_id):
                visible.append(self._unwrap_queue_item(item))
        return visible

    def user_queue_put(self, item):
        """Put an item in the user-specific queue."""
        wrapped_item = self._wrap_queue_item(item)
        with self.__prompt_queue.mutex:
            heapq.heappush(self.__prompt_queue.queue, wrapped_item)
            self.server.queue_updated()
            self.__prompt_queue.not_empty.notify()

    def user_queue_get(self, timeout=None):
        """Get an item from the user-specific queue."""
        user_queue = self.__prompt_queue.queue
        with self.__prompt_queue.not_empty:
            while len(user_queue) == 0:
                self.__prompt_queue.not_empty.wait(timeout=timeout)
                if timeout is not None and len(user_queue) == 0:
                    return None

            item = heapq.heappop(user_queue)
            user_id = self._queue_item_user_id(item)
            self.set_current_user_id(user_id, True)

            i = self.__prompt_queue.task_counter
            self.__prompt_queue.currently_running[i] = copy.deepcopy(item)
            self.__prompt_queue.task_counter += 1
            self.server.queue_updated()
            return (self._unwrap_queue_item(item), i)

    def user_queue_task_done(
        self,
        item_id,
        history_result,
        status: Optional["PromptQueue.ExecutionStatus"],
        process_item=None,
    ):
        """Mark a user-specific queue task as done."""
        with self.__prompt_queue.mutex:
            wrapped_prompt = self.__prompt_queue.currently_running.pop(item_id)
            prompt = self._unwrap_queue_item(wrapped_prompt)
            user_id = self._queue_item_user_id(wrapped_prompt)

            if len(self.__prompt_queue.history) >= MAXIMUM_HISTORY_SIZE:
                self.__prompt_queue.history.pop(next(iter(self.__prompt_queue.history)))

            status_dict: Optional[dict] = None
            if status is not None:
                status_dict = copy.deepcopy(status._asdict())

            if process_item is not None:
                prompt = process_item(prompt)

            self.__prompt_queue.history[prompt[1]] = {
                "prompt": prompt,
                "outputs": {},
                "status": status_dict,
                "user_id": user_id,
            }
            self.__prompt_queue.history[prompt[1]].update(history_result)
            self._persist_history_assets(prompt[1], user_id)
            self._save_history_item(prompt[1])
            self.server.queue_updated()

    def _persist_history_assets(self, prompt_id: str, user_id: str) -> None:
        if not user_id:
            return
        try:
            destination_subfolder = (
                f"{self._get_user_path_prefix(user_id)}/history_assets/{prompt_id}"
            )
            persist_temp_assets(
                self.__prompt_queue.history[prompt_id],
                self.__get_temp_directory(),
                self.__get_output_directory(),
                self._get_user_slug(user_id),
                destination_subfolder,
            )
        except Exception:
            logger.exception("Failed to persist temporary assets for %s", prompt_id)

    def _save_history_item(self, prompt_id: str) -> None:
        if not self.__history_store:
            return
        try:
            self.__history_store.save(
                prompt_id,
                self.__prompt_queue.history[prompt_id],
                MAXIMUM_HISTORY_SIZE,
            )
        except Exception:
            logger.exception("Failed to persist history item %s", prompt_id)

    def _load_persisted_history(self) -> None:
        if not self.__history_store:
            return
        try:
            persisted = self.__history_store.load(MAXIMUM_HISTORY_SIZE)
            persisted.update(self.__prompt_queue.history)
            self.__prompt_queue.history = persisted
        except Exception:
            logger.exception("Failed to restore persisted history")

    def _delete_persisted_history(
        self, prompt_id: str = None, owner_id: str = None, clear: bool = False
    ) -> None:
        if not self.__history_store:
            return
        try:
            if clear:
                self.__history_store.clear()
            elif owner_id is not None:
                self.__history_store.delete_owner(owner_id)
            elif prompt_id is not None:
                self.__history_store.delete(prompt_id)
        except Exception:
            logger.exception("Failed to update persistent history")

    def user_queue_get_current_queue(self):
        """Get the current user-specific queue."""
        with self.__prompt_queue.mutex:
            running = self._visible_queue_items(self.__prompt_queue.currently_running.values())
            queued = self._visible_queue_items(copy.deepcopy(self.__prompt_queue.queue))
            return (running, queued)

    def user_queue_get_current_queue_volatile(self):
        """Get the current user-specific queue without deep copying items."""
        with self.__prompt_queue.mutex:
            running = self._visible_queue_items(self.__prompt_queue.currently_running.values())
            queued = self._visible_queue_items(copy.copy(self.__prompt_queue.queue))
            return (running, queued)

    def user_queue_get_tasks_remaining(self):
        with self.__prompt_queue.mutex:
            return (
                len(self._visible_queue_items(self.__prompt_queue.queue))
                + len(self._visible_queue_items(self.__prompt_queue.currently_running.values()))
            )

    def user_queue_wipe_queue(self):
        """Wipe the current user's queue, or all queue items for admins."""
        with self.__prompt_queue.mutex:
            current_user_id = self.get_current_user_id()
            if self.is_admin_user(current_user_id):
                self.__prompt_queue.queue = []
            else:
                self.__prompt_queue.queue = [
                    item
                    for item in self.__prompt_queue.queue
                    if self._queue_item_user_id(item) != current_user_id
                ]
            self.server.queue_updated()

    def user_queue_delete_queue_item(self, function):
        """Delete an item from the current user's queue."""
        with self.__prompt_queue.mutex:
            for x in range(len(self.__prompt_queue.queue)):
                item = self.__prompt_queue.queue[x]
                unwrapped_item = self._unwrap_queue_item(item)
                if (
                    function(unwrapped_item)
                    and self._can_access_owner(self._queue_item_user_id(item))
                ):
                    self.__prompt_queue.queue.pop(x)
                    heapq.heapify(self.__prompt_queue.queue)
                    self.server.queue_updated()
                    return True
        return False

    def _visible_history(self):
        current_user_id = self.get_current_user_id()
        if self.is_admin_user(current_user_id):
            return self.__prompt_queue.history
        return {
            k: v
            for k, v in self.__prompt_queue.history.items()
            if v.get("user_id") == current_user_id
        }

    def user_queue_get_history(self, prompt_id=None, max_items=None, offset=-1, map_function=None):
        """Get the current user's queue history."""
        with self.__prompt_queue.mutex:
            user_history = self._visible_history()
            if prompt_id is None:
                out = {}
                i = 0
                if offset < 0 and max_items is not None:
                    offset = len(user_history) - max_items
                for k in user_history:
                    if i >= offset:
                        p = user_history[k]
                        if map_function is None:
                            p = copy.deepcopy(p)
                        else:
                            p = map_function(p)
                        out[k] = p
                        if max_items is not None and len(out) >= max_items:
                            break
                    i += 1
                return out
            if prompt_id in user_history:
                p = user_history[prompt_id]
                if map_function is None:
                    p = copy.deepcopy(p)
                else:
                    p = map_function(p)
                return {prompt_id: p}
            return {}

    def user_queue_wipe_history(self):
        """Wipe the current user's history, or all history for admins."""
        with self.__prompt_queue.mutex:
            current_user_id = self.get_current_user_id()
            if self.is_admin_user(current_user_id):
                self.__prompt_queue.history = {}
                self._delete_persisted_history(clear=True)
            else:
                self.__prompt_queue.history = {
                    k: v
                    for k, v in self.__prompt_queue.history.items()
                    if v.get("user_id") != current_user_id
                }
                self._delete_persisted_history(owner_id=current_user_id)

    def user_queue_delete_history_item(self, id_to_delete):
        with self.__prompt_queue.mutex:
            history_item = self.__prompt_queue.history.get(id_to_delete)
            if history_item and self._can_access_owner(history_item.get("user_id")):
                self.__prompt_queue.history.pop(id_to_delete, None)
                self._delete_persisted_history(prompt_id=id_to_delete)

    def patch_prompt_queue(self):
        """Patch the prompt queue with user-specific methods."""
        self._load_persisted_history()
        self.__prompt_queue.put = self.user_queue_put
        self.__prompt_queue.get = self.user_queue_get
        self.__prompt_queue.task_done = self.user_queue_task_done
        self.__prompt_queue.get_current_queue = self.user_queue_get_current_queue
        self.__prompt_queue.get_current_queue_volatile = self.user_queue_get_current_queue_volatile
        self.__prompt_queue.get_tasks_remaining = self.user_queue_get_tasks_remaining
        self.__prompt_queue.wipe_queue = self.user_queue_wipe_queue
        self.__prompt_queue.delete_queue_item = self.user_queue_delete_queue_item
        self.__prompt_queue.get_history = self.user_queue_get_history
        self.__prompt_queue.wipe_history = self.user_queue_wipe_history
        self.__prompt_queue.delete_history_item = self.user_queue_delete_history_item

    def _default_owner_id(self) -> str:
        return self.get_context_user_id() or ""

    def patch_assets(self) -> None:
        """Patch ComfyUI asset ownership and visibility without editing ComfyUI."""
        try:
            import app.assets.database.queries as queries
            import app.assets.database.queries.common as common_queries
            import app.assets.database.queries.asset_reference as asset_reference_queries
            import app.assets.database.queries.tags as tag_queries
            import app.assets.services as asset_services
            import app.assets.services.ingest as ingest
            import app.assets.services.asset_management as asset_management
            import app.assets.services.tagging as tagging
            from app.assets.database.models import AssetReference
            from app.database.db import create_session
        except Exception:
            return

        def build_visible_owner_clause(owner_id: str):
            owner_id = (owner_id or "").strip()
            if self.is_admin_user(owner_id):
                return asset_reference_queries.sa.true()
            if owner_id == "":
                return AssetReference.owner_id == ""
            return AssetReference.owner_id == owner_id

        def get_reference_with_owner_check(session, reference_id: str, owner_id: str):
            ref = asset_reference_queries.get_reference_by_id(
                session, reference_id=reference_id
            )
            if not ref or ref.deleted_at is not None:
                raise ValueError(f"AssetReference {reference_id} not found")
            if self.is_admin_user(owner_id):
                return ref
            if not ref.owner_id or ref.owner_id != owner_id:
                raise PermissionError("not owner")
            return ref

        common_queries.build_visible_owner_clause = build_visible_owner_clause
        asset_reference_queries.build_visible_owner_clause = build_visible_owner_clause
        tag_queries.build_visible_owner_clause = build_visible_owner_clause
        queries.build_visible_owner_clause = build_visible_owner_clause

        asset_reference_queries.get_reference_with_owner_check = get_reference_with_owner_check
        queries.get_reference_with_owner_check = get_reference_with_owner_check
        asset_management.get_reference_with_owner_check = get_reference_with_owner_check
        tagging.get_reference_with_owner_check = get_reference_with_owner_check

        original_ingest_existing_file = ingest.ingest_existing_file
        original_upload_from_temp_path = ingest.upload_from_temp_path
        original_register_file_in_place = ingest.register_file_in_place
        original_create_from_hash = ingest.create_from_hash

        def ingest_existing_file(*args, owner_id="", **kwargs):
            owner_id = owner_id or self._default_owner_id()
            return original_ingest_existing_file(*args, owner_id=owner_id, **kwargs)

        def upload_from_temp_path(*args, owner_id="", **kwargs):
            owner_id = owner_id or self._default_owner_id()
            return original_upload_from_temp_path(*args, owner_id=owner_id, **kwargs)

        def register_file_in_place(*args, owner_id="", **kwargs):
            owner_id = owner_id or self._default_owner_id()
            return original_register_file_in_place(*args, owner_id=owner_id, **kwargs)

        def create_from_hash(*args, owner_id="", **kwargs):
            owner_id = owner_id or self._default_owner_id()
            return original_create_from_hash(*args, owner_id=owner_id, **kwargs)

        ingest.ingest_existing_file = ingest_existing_file
        ingest.upload_from_temp_path = upload_from_temp_path
        ingest.register_file_in_place = register_file_in_place
        ingest.create_from_hash = create_from_hash

        asset_services.upload_from_temp_path = upload_from_temp_path
        asset_services.register_file_in_place = register_file_in_place
        asset_services.create_from_hash = create_from_hash

        def resolve_hash_to_path(asset_hash: str, owner_id: str = ""):
            with create_session() as session:
                asset = asset_management.queries_get_asset_by_hash(session, asset_hash)
                if not asset:
                    return None
                refs = asset_management.list_references_by_asset_id(
                    session, asset_id=asset.id
                )
                if self.is_admin_user(owner_id):
                    visible = refs
                else:
                    visible = [r for r in refs if r.owner_id == owner_id]
                abs_path = asset_management.select_best_live_path(visible)
                if not abs_path:
                    return None
                display_name = os.path.basename(abs_path)
                for ref in visible:
                    if ref.file_path == abs_path and ref.name:
                        display_name = ref.name
                        break
                content_type = (
                    asset.mime_type
                    or mimetypes.guess_type(display_name)[0]
                    or "application/octet-stream"
                )
            return asset_management.DownloadResolutionResult(
                abs_path=abs_path,
                content_type=content_type,
                download_name=display_name,
            )

        asset_management.resolve_hash_to_path = resolve_hash_to_path
        asset_services.resolve_hash_to_path = resolve_hash_to_path

        route_module = sys.modules.get("app.assets.api.routes")
        if route_module:
            route_module.upload_from_temp_path = upload_from_temp_path
            route_module.create_from_hash = create_from_hash

        for module_name in ("server", "__main__", "main"):
            module = sys.modules.get(module_name)
            if not module:
                continue
            if hasattr(module, "register_file_in_place"):
                module.register_file_in_place = register_file_in_place
            if hasattr(module, "resolve_hash_to_path"):
                module.resolve_hash_to_path = resolve_hash_to_path

    def create_manager_access_control_middleware(
        self, manager_directory: str = "/extensions/comfyui-manager", manager_routes: tuple = ()
    ) -> web.middleware:
        """Create middleware for manager access control."""

        @web.middleware
        async def manager_access_control_middleware(
            request: web.Request, handler
        ) -> web.Response:
            user_id = request.get("user_id")

            if self.users_db.get_admin_user()[0] == user_id or (
                not request.path.startswith(manager_routes)
                and not request.path.lower().startswith(manager_directory)
            ):
                return await handler(request)

            return web.HTTPForbidden(
                reason="You do not have access to comfyui manager."
            )

        return manager_access_control_middleware
