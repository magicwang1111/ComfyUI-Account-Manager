"""Request-scoped ownership for ComfyUI's record/content asset schema."""
import contextlib
import contextvars
import logging
import os
import re
from datetime import datetime

from aiohttp import web
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, with_loader_criteria


class AssetAccess:
    def __init__(self, users_db, roots, model_roots, preview_root=None):
        self.users_db = users_db
        self.roots = tuple(os.path.realpath(p) for p in roots)
        self.model_roots = tuple(os.path.realpath(p) for p in model_roots)
        self.preview_root = os.path.realpath(preview_root) if preview_root else None
        self.context = contextvars.ContextVar('account_asset_access', default=None)
        self.ready = False

    @contextlib.contextmanager
    def scope(self, user_id, *, write=False):
        users = self.users_db.load_users()
        token = self.context.set((user_id, users, write, {}))
        try:
            yield
        finally:
            self.context.reset(token)

    @staticmethod
    def _relative(path, root):
        try:
            if os.path.commonpath((path, root)) == root:
                return os.path.relpath(path, root).split(os.sep)
        except ValueError:
            pass
        return None

    def owner_for_path(self, path, users=None):
        users = self.users_db.load_users() if users is None else users
        path = os.path.realpath(path)
        for index, root in enumerate(self.roots):
            parts = self._relative(path, root)
            if not parts or len(parts) < 2:
                continue
            # Historical UUID folders and input/temp username folders.
            slug = parts[0]
            if index == 0 and re.fullmatch(r'\d{4}-\d{2}-\d{2}', slug):
                if len(parts) < 3:
                    return None
                slug = parts[1]
            matches = [uid for uid, user in users.items()
                       if slug == uid or slug == user.get('username')]
            return matches[0] if len(matches) == 1 else None
        return None

    def _path_visible(self, path, user_id, users, write):
        if user_id not in users or not path:
            return False
        path = os.path.realpath(path)
        # Private roots take precedence even if a model search path overlaps one.
        if any(self._relative(path, root) is not None for root in self.roots):
            return self.owner_for_path(path, users) == user_id
        return not write and any(self._relative(path, root) is not None for root in self.model_roots)

    def install(self):
        from app.assets.database.models import Asset, AssetContent, AssetTag, Tag
        from app.assets.services import ingest
        self.models = Asset, AssetContent, AssetTag, Tag
        # Fail at installation, rather than silently serving an unsupported schema.
        for model, names in ((Asset, ('content_id', 'preview_id')),
                             (AssetContent, ('path',)), (AssetTag, ('asset_id', 'tag_name'))):
            for name in names:
                getattr(model, name)
        event.listen(Session, 'do_orm_execute', self.filter_query)
        self.original_destination = ingest.resolve_destination_from_tags
        ingest.resolve_destination_from_tags = self.upload_destination
        self.ready = True
        logging.info('Account Manager: record/content asset isolation enabled')

    def upload_destination(self, tags):
        base, subdirs = self.original_destination(tags)
        context = self.context.get()
        if context is None:
            return base, subdirs
        user_id, users, _, _ = context
        user = users.get(user_id)
        if not user:
            raise web.HTTPForbidden(reason='Authenticated asset owner required')
        username = user.get('username') or user_id
        if os.path.basename(username) != username or username in ('.', '..'):
            raise web.HTTPForbidden(reason='Invalid asset owner folder')
        if 'output' in tags:
            base = os.path.join(self.roots[0], datetime.now().strftime('%Y-%m-%d'), username)
        elif 'input' in tags:
            base = os.path.join(self.roots[2], username)
        elif 'models' in tags and not user.get('admin'):
            raise web.HTTPForbidden(reason='Administrator required for shared model uploads')
        return base, subdirs

    def filter_query(self, execution):
        context = self.context.get()
        if context is None:
            return  # Background indexing is not a user-facing catalog read.
        user_id, users, write, cache = context
        if users.get(user_id, {}).get('admin'):
            return
        if not (execution.is_select or execution.is_update or execution.is_delete):
            return
        connection = execution.session.connection()
        if connection.dialect.name != 'sqlite':
            raise RuntimeError('Account Manager asset isolation requires SQLite')
        # Register on the checked-out connection, including pools created before us.
        # No user is stored on the connection: each query receives its own predicate.
        def visible(path, uid, modifying):
            key = (path, uid, modifying)
            if key not in cache:
                if modifying == 2:
                    cache[key] = bool(self.preview_root and path and
                                      self._relative(os.path.realpath(path), self.preview_root) is not None)
                else:
                    cache[key] = self._path_visible(path, uid, users, modifying)
            return int(cache[key])

        connection.connection.dbapi_connection.create_function(
            'am_asset_visible', 3,
            visible,
        )
        Asset, Content, Link, Tag = self.models
        a = Asset.__table__.alias('am_records')
        c = Content.__table__.alias('am_contents')
        link = Link.__table__.alias('am_links')
        direct = select(c.c.id).where(func.am_asset_visible(c.c.path, user_id, int(write)) == 1).correlate(None)
        # Previews are shared physical files; authorize through an accessible parent.
        preview_ids = select(a.c.preview_id).where(a.c.content_id.in_(direct)).correlate(None)
        preview_content = select(a.c.content_id).select_from(a.join(c, a.c.content_id == c.c.id)).where(
            a.c.id.in_(preview_ids), func.am_asset_visible(c.c.path, user_id, 2) == 1,
        ).correlate(None)
        # Shared generated previews are readable, but cannot be edited/deleted by
        # an individual owner while another parent may still reference them.
        content_ids = direct if write else direct.union(preview_content)
        asset_ids = select(a.c.id).where(a.c.content_id.in_(content_ids)).correlate(None)
        tag_names = select(link.c.tag_name).where(link.c.asset_id.in_(asset_ids)).correlate(None)
        criteria = [
            with_loader_criteria(Asset, lambda cls: cls.content_id.in_(content_ids), include_aliases=True),
            with_loader_criteria(Content, lambda cls: cls.id.in_(content_ids), include_aliases=True),
            with_loader_criteria(Link, lambda cls: cls.asset_id.in_(asset_ids), include_aliases=True),
        ]
        # Tags form a global vocabulary. ensure_tag must see existing names to
        # avoid uniqueness conflicts; only public read requests hide unused names.
        if not write:
            criteria.append(with_loader_criteria(Tag, lambda cls: cls.name.in_(tag_names), include_aliases=True))
        execution.statement = execution.statement.options(*criteria)

    def middleware(self):
        import folder_paths

        @web.middleware
        async def asset_middleware(request, handler):
            path = request.path
            asset_path = path == '/api/assets' or path.startswith('/api/assets/') or path == '/api/tags'
            if not self.ready:
                if asset_path or path in ('/view', '/api/view'):
                    return web.json_response({'error': 'Asset permission adapter unavailable'}, status=503)
                return await handler(request)
            user_id = request.get('user_id') or ''
            with self.scope(user_id, write=request.method not in ('GET', 'HEAD', 'OPTIONS')):
                users = self.context.get()[1]
                if path in ('/view', '/api/view') and not users.get(user_id, {}).get('admin'):
                    filename = request.query.get('filename', '')
                    if not filename.startswith('blake3:'):
                        filename, root = folder_paths.annotated_filepath(filename)
                        root = root or folder_paths.get_directory_by_type(request.query.get('type', 'output'))
                        if not root or not filename or '..' in filename or os.path.isabs(filename):
                            return web.Response(status=403)
                        candidate = os.path.join(root, request.query.get('subfolder', ''), os.path.basename(filename))
                        if not self._path_visible(candidate, user_id, users, False):
                            return web.Response(status=403)
                if path.startswith(('/api/assets/seed', '/api/assets/prune')):
                    if not self.context.get()[1].get(user_id, {}).get('admin'):
                        return web.json_response({'error': 'Administrator required'}, status=403)
                return await handler(request)
        return asset_middleware
