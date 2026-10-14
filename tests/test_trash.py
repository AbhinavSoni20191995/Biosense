"""The trash: what a person deleted, restoring it, and removing it for good.

Deleting a run or a project moves it to a trash folder rather than unlinking
it, so a mistaken click is recoverable. Nothing ever emptied that folder, so on
a hosted deployment deleted runs kept their storage for ever. The trash can
now be seen, restored from and emptied — by the item's owner only, through a
name that can never reach outside the trash, and only with an explicit
confirmation, because removing for good cannot be undone.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import workspace as WS
from biosense.production import project_builder as PB
from biosense.production import run_store as RS
from tests import test_accounts_admin as AA


def _run_dir(root, name, owner, run_id='r1', title='A run'):
    d = Path(root) / name
    d.mkdir(parents=True)
    K.write_json_atomic(d / RS.STATE_NAME, {'run_id': run_id, 'owner': owner, 'title': title,
                                            'status': 'complete', 'project_id': 'p1'})
    (d / 'protocol_summary.json').write_text('{"x": "' + 'y' * 5000 + '"}')
    return d


class RunTrashTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_deleted_run_is_listed_with_its_size_for_its_owner_only(self):
        RS.trash(self.root, _run_dir(self.root, 'ai-20261014-aaaaaa', 'me'))
        mine = RS.trashed(self.root, owner='me')
        self.assertEqual(1, len(mine))
        self.assertEqual('A run', mine[0]['title'])
        self.assertGreater(mine[0]['bytes'], 5000)
        self.assertIsNotNone(mine[0]['deleted_at'])
        self.assertEqual([], RS.trashed(self.root, owner='someone else'))

    def test_restore_puts_it_back_under_its_own_name(self):
        RS.trash(self.root, _run_dir(self.root, 'ai-20261014-aaaaaa', 'me'))
        tid = RS.trashed(self.root, owner='me')[0]['trash_id']
        back = RS.restore_trashed(self.root, tid, owner='me')
        self.assertEqual('ai-20261014-aaaaaa', back.name)
        self.assertTrue((self.root / 'ai-20261014-aaaaaa' / RS.STATE_NAME).is_file())
        self.assertEqual([], RS.trashed(self.root, owner='me'))

    def test_restore_never_overwrites_a_run_of_the_same_name(self):
        RS.trash(self.root, _run_dir(self.root, 'ai-20261014-aaaaaa', 'me'))
        _run_dir(self.root, 'ai-20261014-aaaaaa', 'me', run_id='r2')
        tid = RS.trashed(self.root, owner='me')[0]['trash_id']
        with self.assertRaises(ValueError):
            RS.restore_trashed(self.root, tid, owner='me')

    def test_purge_removes_the_files_and_reports_the_space(self):
        RS.trash(self.root, _run_dir(self.root, 'ai-20261014-aaaaaa', 'me'))
        tid = RS.trashed(self.root, owner='me')[0]['trash_id']
        self.assertIsNone(RS.purge_trashed(self.root, tid, owner='intruder'))
        freed = RS.purge_trashed(self.root, tid, owner='me')
        self.assertGreater(freed, 5000)
        self.assertFalse((self.root / RS.TRASH / tid).exists())

    def test_an_unowned_run_is_an_operators_alone(self):
        RS.trash(self.root, _run_dir(self.root, 'ai-20261014-bbbbbb', None))
        self.assertEqual([], RS.trashed(self.root, owner='me'))
        self.assertEqual(1, len(RS.trashed(self.root, owner='me', operator=True)))

    def test_a_trash_id_can_never_reach_outside_the_trash(self):
        live = _run_dir(self.root, 'ai-20261014-cccccc', 'me')
        outside = self.root.parent / f'{self.root.name}-victim-20261014-120000'
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        for bad in ('../ai-20261014-cccccc', 'ai-20261014-cccccc', '..',
                    f'../../{outside.name}', '/etc', ''):
            self.assertIsNone(RS.purge_trashed(self.root, bad, owner='me'), bad)
        # A symlink planted in the trash is not followed.
        (self.root / RS.TRASH).mkdir(exist_ok=True)
        link = self.root / RS.TRASH / 'ai-20261014-dddddd-20261014-120000'
        link.symlink_to(live, target_is_directory=True)
        self.assertIsNone(RS.purge_trashed(self.root, link.name, owner='me'))
        self.assertTrue(live.is_dir(), 'the live run behind the link is untouched')
        self.assertTrue(outside.is_dir())


class ProjectTrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(self.tmp)
        self.addCleanup(os.environ.pop, 'BIOSENSE_PRIVATE_DATA', None)
        self.me = WS.WorkspaceIdentity('me@lab.example', authenticated=True,
                                       server='https://id.example')
        self.other = WS.WorkspaceIdentity('other@lab.example', authenticated=True,
                                          server='https://id.example')

    def test_a_deleted_project_can_be_restored_or_removed_for_good(self):
        PB.save(self.me, PB.quick_build(project_id='mine_one', name='Mine One'))
        PB.delete(self.me, 'mine_one')
        rows = PB.trashed_projects(self.me)
        self.assertEqual(['mine_one'], [r['project_id'] for r in rows])
        self.assertEqual([], PB.trashed_projects(self.other))
        self.assertIsNone(PB.purge_project(self.other, rows[0]['trash_id']))
        self.assertEqual('mine_one', PB.restore_project(self.me, rows[0]['trash_id']))
        self.assertIn('mine_one', [p.project_id for p in PB.workspace_projects(self.me)])
        PB.delete(self.me, 'mine_one')
        tid = PB.trashed_projects(self.me)[0]['trash_id']
        self.assertGreater(PB.purge_project(self.me, tid), 0)
        self.assertEqual([], PB.trashed_projects(self.me))


class TrashHttpTests(unittest.TestCase):
    """The endpoints, on the same live server and accounts as the role tests."""
    setUpClass = classmethod(AA.HttpRoleTests.setUpClass.__func__)
    tearDownClass = classmethod(AA.HttpRoleTests.tearDownClass.__func__)
    _req = AA.HttpRoleTests._req
    _run = AA.HttpRoleTests._run

    def _trash(self, sid):
        code, d, _ = self._req('GET', '/api/trash', sid=sid)
        self.assertEqual(200, code, d)
        return d

    def test_delete_restore_and_remove_for_good(self):
        rid = self._run(self.user_sid)
        self.assertEqual(200, self._req('POST', f'/api/discovery/{rid}/delete',
                                        sid=self.user_sid)[0])
        mine = self._trash(self.user_sid)
        item = next(r for r in mine['runs'] if r['run_id'] == rid)
        self.assertGreater(item['bytes'], 0)
        self.assertGreaterEqual(mine['total_bytes'], item['bytes'])
        self.assertNotIn(rid, [r['run_id'] for r in self._trash(self.other_sid)['runs']],
                         "another account never sees what I deleted")
        tid = item['trash_id']

        # Another account cannot restore or remove it; nobody removes without confirming.
        for action in ('restore', 'purge'):
            self.assertEqual(404, self._req('POST', f'/api/trash/run/{tid}/{action}',
                                            {'confirm': True}, sid=self.other_sid)[0])
        self.assertEqual(400, self._req('POST', f'/api/trash/run/{tid}/purge', {},
                                        sid=self.user_sid)[0])

        # Restore: back in the list.
        self.assertEqual(200, self._req('POST', f'/api/trash/run/{tid}/restore', {},
                                        sid=self.user_sid)[0])
        listed = self._req('GET', '/api/discovery', sid=self.user_sid)[1]['runs']
        self.assertIn(rid, [r['run_id'] for r in listed])

        # Delete again, then remove for good: the files are gone.
        self._req('POST', f'/api/discovery/{rid}/delete', sid=self.user_sid)
        tid = next(r for r in self._trash(self.user_sid)['runs'] if r['run_id'] == rid)['trash_id']
        code, d, _ = self._req('POST', f'/api/trash/run/{tid}/purge', {'confirm': True},
                               sid=self.user_sid)
        self.assertEqual(200, code, d)
        self.assertGreater(d['bytes_freed'], 0)
        self.assertFalse((self.tmp / 'runs' / RS.TRASH / tid).exists())
        self.assertNotIn(rid, [r['run_id'] for r in self._trash(self.user_sid)['runs']])

    def test_empty_trash_removes_only_my_items_and_needs_confirming(self):
        mine, theirs = self._run(self.user_sid), self._run(self.other_sid)
        self._req('POST', f'/api/discovery/{mine}/delete', sid=self.user_sid)
        self._req('POST', f'/api/discovery/{theirs}/delete', sid=self.other_sid)
        self.assertEqual(400, self._req('POST', '/api/trash/empty', {}, sid=self.user_sid)[0])
        code, d, _ = self._req('POST', '/api/trash/empty', {'confirm': True}, sid=self.user_sid)
        self.assertEqual(200, code, d)
        self.assertEqual([], self._trash(self.user_sid)['runs'])
        self.assertIn(theirs, [r['run_id'] for r in self._trash(self.other_sid)['runs']],
                      "emptying my trash leaves another account's alone")


if __name__ == '__main__':
    unittest.main()
