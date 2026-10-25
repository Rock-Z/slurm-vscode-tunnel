import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paseo_plugin_deploy import snapshot, publish, extract_release

class DeployTest(unittest.TestCase):
    def test_snapshot_includes_worktree_edits_but_not_ignored_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / '.gitignore').write_text('node_modules/\nsecret\n')
            (source / 'index.server.ts').write_text('old')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            (source / 'index.server.ts').write_text('edited')
            (source / 'new.ts').write_text('new')
            (source / 'secret').write_text('private')
            dest = root / 'snapshot'
            snapshot(source, dest)
            self.assertEqual((dest / 'index.server.ts').read_text(), 'edited')
            self.assertTrue((dest / 'new.ts').exists())
            self.assertFalse((dest / '.git').exists())
            self.assertFalse((dest / 'secret').exists())
            (dest / 'index.server.ts').write_text('deployed')
            self.assertEqual((source / 'index.server.ts').read_text(), 'edited')

    def test_snapshot_rejects_links_outside_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (root / 'private').write_text('private')
            (source / 'link').symlink_to('../private')
            with self.assertRaises(ValueError): snapshot(source, root / 'snapshot')

    def test_archive_restores_after_node_cache_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package = root / 'package'; package.mkdir()
            (package / 'index.server.ts').write_text('old')
            archive = publish(package, root / 'store', 'plugin')
            self.assertEqual(publish(package, root / 'store', 'plugin'), archive)
            first = extract_release(archive, root / 'node-one')
            (package / 'index.server.ts').write_text('new')
            second = extract_release(archive, root / 'node-two')
            self.assertEqual((first / 'index.server.ts').read_text(), 'old')
            self.assertEqual((second / 'index.server.ts').read_text(), 'old')
            self.assertEqual(list((root / 'store/plugin').iterdir()), [archive])

    def test_corrupt_archive_is_not_published_to_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / (('0' * 64) + '.tar.gz')
            archive.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                extract_release(archive, root / 'cache')
            self.assertFalse((root / 'cache').exists())

if __name__ == '__main__': unittest.main()
