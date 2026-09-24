import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools' / 'dev' / 'materialize-runtime'
FILES = (
    'VERSION',
    'client.py',
    'forum.py',
    'forum_cache.py',
    'forum_ledger.py',
    'forum_liveness.py',
    'forum_state.py',
    'http_transport.py',
    'mcp.json',
)


class RuntimeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.repo = root / 'repo'
        self.target = root / 'runtime'
        self.repo.mkdir()
        self.target.mkdir()
        self._git('init')
        self._git('config', 'user.email', 'test@example.invalid')
        self._git('config', 'user.name', 'projection-test')
        for name in FILES:
            path = self.repo / name
            path.write_text(f'committed:{name}\n')
        (self.repo / 'forum.py').chmod(0o755)
        self._git('add', *FILES)
        self._git('commit', '-m', 'fixture')
        self.head = self._git('rev-parse', 'HEAD').stdout.strip()
        (self.target / 'citizen.json').write_text('credential-sentinel\n')
        (self.target / '.forum-state.json').write_text('state-sentinel\n')

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ['git', '-C', str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--repo', str(self.repo), '--target', str(self.target), *extra],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_dry_run_names_exact_source_and_preserves_runtime_state(self):
        proc = self._run('--source-ref', self.head, '--dry-run')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt['status'], 'DRY_RUN')
        self.assertEqual(receipt['source_revision'], self.head)
        self.assertTrue(all(row['action'] == 'create' for row in receipt['files']))
        self.assertFalse((self.target / 'forum.py').exists())
        self.assertEqual((self.target / 'citizen.json').read_text(), 'credential-sentinel\n')
        self.assertEqual((self.target / '.forum-state.json').read_text(), 'state-sentinel\n')

    def test_apply_projects_declared_surface_and_keeps_runtime_only_files(self):
        proc = self._run('--source-ref', self.head)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt['status'], 'VERIFIED')
        self.assertEqual(set(receipt['changed_files']), set(FILES))
        for name in FILES:
            self.assertEqual((self.target / name).read_text(), f'committed:{name}\n')
        self.assertEqual((self.target / 'citizen.json').read_text(), 'credential-sentinel\n')
        self.assertEqual((self.target / '.forum-state.json').read_text(), 'state-sentinel\n')

        second = self._run('--source-ref', self.head)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_receipt = json.loads(second.stdout)
        self.assertEqual(second_receipt['changed_files'], [])
        self.assertTrue(all(row['action'] == 'unchanged' for row in second_receipt['files']))

    def test_dirty_worktree_requires_explicit_ref_and_exact_ref_ignores_dirty_bytes(self):
        (self.repo / 'client.py').write_text('dirty-client\n')
        blocked = self._run('--dry-run')
        self.assertEqual(blocked.returncode, 2)
        error = json.loads(blocked.stderr)
        self.assertEqual(error['code'], 'SOURCE_DIRTY')

        explicit = self._run('--source-ref', self.head)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual((self.target / 'client.py').read_text(), 'committed:client.py\n')


if __name__ == '__main__':
    unittest.main()
