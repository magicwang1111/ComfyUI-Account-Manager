"""Integration tests against the installed ComfyUI asset models and queries."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database.models import Base
from app.assets.database.models import Asset, AssetContent
from app.assets.database.queries.records import (
    RecordPageSpec, create_content, create_record, list_records_page,
)
from app.assets.database.queries.tags import list_tags_with_usage


class AssetAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('am_asset_access', ROOT / 'utils/asset_access.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.users = {'a': {'username': 'alice'}, 'b': {'username': 'bob'}, 'admin': {'username': 'boss', 'admin': True}}
        self.access = self.module.AssetAccess(
            SimpleNamespace(load_users=lambda: self.users),
            tuple(str(self.root / p) for p in ('output', 'temp', 'input')),
            [str(self.root / 'models')],
            str(self.root / 'previews'),
        )
        self.access.install()
        from app.assets.services import ingest
        self.addCleanup(setattr, ingest, 'resolve_destination_from_tags', self.access.original_destination)
        from sqlalchemy import event
        self.addCleanup(event.remove, Session, 'do_orm_execute', self.access.filter_query)
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.ids = {}
        with Session(self.engine) as s:
            for name, path in {
                'alice': 'output/2026-10-20/alice/a.png',
                'bob': 'output/2026-10-22/bob/b.png',
                'legacy': 'output/a/old.png',
                'unknown': 'output/loose.png',
                'model': 'models/shared.safetensors',
                'preview': 'previews/alice.png',
            }.items():
                file = self.root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'image')
                c = create_content(s, str(file), size_bytes=5)
                a = create_record(s, c.id, name, tags=[name + '-tag'])
                self.ids[name] = a.id
            s.get(Asset, self.ids['alice']).preview_id = self.ids['preview']
            s.commit()

    def scope(self, user, write=False):
        return self.access.scope(user, write=write)

    def test_list_filters_before_pagination_and_count(self):
        with self.scope('a'), Session(self.engine) as s:
            rows, _, total = list_records_page(s, RecordPageSpec(limit=1, sort='name', order='asc'))
            self.assertEqual(total, 4)
            self.assertEqual([r.name for r in rows], ['alice'])
            rows, _, total = list_records_page(s, RecordPageSpec(limit=1, offset=1, sort='name', order='asc'))
            self.assertEqual([r.name for r in rows], ['legacy'])

    def test_direct_ids_hash_content_and_tags_are_private(self):
        with self.scope('a'), Session(self.engine) as s:
            self.assertIsNone(s.get(Asset, self.ids['bob']))
            paths = list(s.scalars(select(AssetContent.path)))
            self.assertFalse(any('bob' in p for p in paths))
            rows, total = list_tags_with_usage(s, include_zero=True)
            self.assertEqual({name for name, _ in rows}, {'alice-tag','legacy-tag','model-tag','preview-tag'})
            self.assertEqual(total, 4)

    def test_admin_and_context_reset(self):
        with self.scope('admin'), Session(self.engine) as s:
            self.assertEqual(len(list(s.scalars(select(Asset)))), 6)
        with self.scope('b'), Session(self.engine) as s:
            self.assertIsNone(s.get(Asset, self.ids['alice']))
            self.assertIsNotNone(s.get(Asset, self.ids['bob']))
        with Session(self.engine) as s:
            self.assertEqual(len(list(s.scalars(select(Asset)))), 6)

    def test_shared_model_cannot_be_modified_by_regular_user(self):
        with self.scope('a', write=True), Session(self.engine) as s:
            self.assertIsNone(s.get(Asset, self.ids['model']))
            self.assertIsNotNone(s.get(Asset, self.ids['alice']))

    def test_unknown_user_cannot_read_assets(self):
        with self.scope('missing'), Session(self.engine) as s:
            self.assertEqual(list(s.scalars(select(Asset))), [])

    def test_real_path_cannot_escape_owner_directory(self):
        own = self.root / 'output/2026-10-20/alice'
        self.assertEqual(self.access.owner_for_path(str(own / 'a.png')), 'a')
        self.assertIsNone(self.access.owner_for_path(str(own / '../../../loose.png')))
        self.assertIsNone(self.access.owner_for_path(str(self.root / 'output/2026-10-20/alice-other/a.png')))

    def test_preview_link_cannot_grant_access_to_another_users_output(self):
        with Session(self.engine) as s:
            s.get(Asset, self.ids['alice']).preview_id = self.ids['bob']
            s.commit()
        with self.scope('a'), Session(self.engine) as s:
            self.assertIsNone(s.get(Asset, self.ids['bob']))

    def test_symlink_to_another_owner_is_not_visible(self):
        link = self.root / 'output/2026-10-20/alice/link.png'
        try:
            link.symlink_to(self.root / 'output/2026-10-22/bob/b.png')
        except OSError as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')
        self.assertEqual(self.access.owner_for_path(str(link)), 'b')
        with Session(self.engine) as s:
            content = create_content(s, str(link))
            record = create_record(s, content.id, 'linked')
            record_id = record.id
            s.commit()
        with self.scope('a'), Session(self.engine) as s:
            self.assertIsNone(s.get(Asset, record_id))


if __name__ == '__main__':
    unittest.main()
