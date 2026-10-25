"""Offline privacy regressions against real temporary Git indexes and ZIPs."""
from contextlib import redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_share
import check_share


class ShareToolsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="share-tools-test-")
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.root = self.directory / "package"
        self.root.mkdir()
        self.write("README.md", b"Reviewed source.\n")
        self.write_manifest({"README.md": b"Reviewed source.\n"})

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_manifest(self, files):
        data = build_share.manifest_bytes({name: check_share.SharedFile(data) for name, data in files.items()}, date(2026, 10, 25))
        self.write(check_share.MANIFEST, data)

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.root), *arguments], capture_output=True)
        self.assertEqual(result.returncode, 0, "Temporary Git operation failed")
        return result.stdout

    def init_git(self):
        if not shutil.which("git"):
            self.skipTest("Git is required")
        self.git("init", "--quiet")
        self.git("add", "--", "README.md", check_share.MANIFEST)

    def check_repo(self):
        return check_share.check_files(check_share.read_index(self.root))[1]

    def test_index_secret_cannot_be_hidden_by_clean_working_tree(self):
        self.init_git()
        secret = ("sk-" + "a" * 32).encode()
        self.write("README.md", secret)
        self.write_manifest({"README.md": secret})
        self.git("add", "--", "README.md", check_share.MANIFEST)
        self.write("README.md", b"Clean working tree, unsafe staged version.\n")
        output = io.StringIO()
        with redirect_stdout(output):
            status = check_share.main(["--repo", "--root", str(self.root)])
        self.assertEqual(status, 1)
        self.assertIn("疑似服务密钥", output.getvalue())
        self.assertNotIn(secret.decode(), output.getvalue())

    def test_repo_ignores_local_private_data_but_distribution_stays_strict(self):
        self.init_git()
        self.write(".local/cookies.json", b"Private browser state")
        self.write("02VideoWorkSpace/my-task/private.txt", b"Private task")
        self.assertEqual(self.check_repo(), [])
        _, problems = check_share.check_distribution(self.root)
        self.assertTrue(any("额外" in issue for issue in problems))

    def test_manifest_itself_is_scanned_from_the_index(self):
        self.init_git()
        manifest = json.loads((self.root / check_share.MANIFEST).read_bytes())
        manifest["description"] = "ghp_" + "b" * 35
        self.write(check_share.MANIFEST, json.dumps(manifest).encode())
        self.git("add", "--", check_share.MANIFEST)
        self.write_manifest({"README.md": b"Reviewed source.\n"})
        with self.assertRaisesRegex(check_share.ShareError, "疑似服务密钥"):
            self.check_repo()

    def test_index_hashes_must_match_the_staged_manifest(self):
        self.init_git()
        self.write("README.md", b"Changed staged content.\n")
        self.git("add", "--", "README.md")
        self.assertTrue(any("清单不同" in issue for issue in self.check_repo()))

    def test_private_tracked_files_and_symlinks_are_rejected(self):
        self.init_git()
        self.write(".local/state.json", b"{}")
        self.git("add", "-f", "--", ".local/state.json")
        with self.assertRaisesRegex(check_share.ShareError, "私人目录"):
            check_share.read_index(self.root)
        self.git("rm", "--cached", "--", ".local/state.json")
        link = self.root / "link.md"
        link.symlink_to("README.md")
        self.git("add", "--", "link.md")
        with self.assertRaisesRegex(check_share.ShareError, "符号链接"):
            check_share.read_index(self.root)

    def test_path_traversal_case_collisions_and_duplicate_json_are_rejected(self):
        for name in ("../outside.py", "/outside.py", "a/../b.py", "a//b.py", "a\\b.py", "C:/outside.py", "CON.txt", "line\nbreak.py"):
            with self.subTest(name=name), self.assertRaises(check_share.ShareError):
                check_share.safe_relative(name)
        for data in (b'{"schema_version": 1, "files": [], "files": []}',
                     build_share.manifest_bytes({"README.md": check_share.SharedFile(b"one"), "readme.md": check_share.SharedFile(b"two")}, date(2026, 10, 25))):
            with self.assertRaises(check_share.ShareError):
                check_share.parse_manifest(data)

    def test_forced_private_account_state_and_preferences_names_are_rejected(self):
        for name in ("cookies.txt", "Preferences.json", "preferences.md", "storage_state-backup.json",
                     "storage-state-2026.json", "token.json", "session.json", "credentials.json"):
            with self.subTest(name=name):
                self.assertIn("包含私有配置或浏览器文件", check_share.inspect_bytes(name, b"{}"))

    def test_bootstrap_build_only_includes_manifest_and_explicit_files(self):
        self.write("tools/new.py", b"print('reviewed')\n")
        self.write("scratch.py", b"Unreviewed local file")
        self.write(".env", b"PRIVATE_VALUE=local")
        target = self.directory / "export.zip"
        count = build_share.build(self.root, includes=["tools/new.py"], prepared_date=date(2026, 10, 25), output=target)
        self.assertEqual(count, 2)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {"AIVideoAgents/README.md", "AIVideoAgents/tools/new.py", "AIVideoAgents/MANIFEST.json"})
            manifest = json.loads(archive.read("AIVideoAgents/MANIFEST.json"))
            self.assertEqual(manifest["prepared_date"], "2026-10-25")
            destination = self.directory / "extracted"
            archive.extractall(destination)
        self.assertEqual(check_share.check_distribution(destination / "AIVideoAgents")[1], [])

    def test_repo_build_and_zip_use_index_bytes_not_working_tree(self):
        self.init_git()
        self.write("README.md", ("sk-" + "c" * 32).encode())
        self.write("untracked.py", b"Not staged")
        target = self.directory / "repo-export.zip"
        build_share.build(self.root, output=target, prepared_date=date(2026, 10, 25))
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read("AIVideoAgents/README.md"), b"Reviewed source.\n")
            self.assertNotIn("AIVideoAgents/untracked.py", archive.namelist())
        self.git("add", "--", check_share.MANIFEST)
        self.assertEqual(self.check_repo(), [])

    def test_manifest_only_requires_staging_its_updated_hashes(self):
        self.init_git()
        self.write("README.md", b"New reviewed content.\n")
        self.git("add", "--", "README.md")
        build_share.build(self.root, prepared_date=date(2026, 10, 25))
        self.assertTrue(self.check_repo())
        self.git("add", "--", check_share.MANIFEST)
        self.assertEqual(self.check_repo(), [])

    def test_builder_refuses_secrets_unsafe_includes_and_inside_zip(self):
        original = (self.root / check_share.MANIFEST).read_bytes()
        self.write("secret.py", ("sk-" + "d" * 32).encode())
        for include in ("secret.py", "../outside.py"):
            with self.subTest(include=include), self.assertRaises(check_share.ShareError):
                build_share.build(self.root, includes=[include])
        with self.assertRaisesRegex(check_share.ShareError, "之外"):
            build_share.build(self.root, output=self.root / "unsafe.zip")
        self.assertEqual((self.root / check_share.MANIFEST).read_bytes(), original)

    def test_symlink_parent_and_workspace_data_cannot_enter_bootstrap(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "source.py").write_bytes(b"print('outside')")
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(check_share.ShareError, "符号链接"):
            build_share.build(self.root, includes=["linked/source.py"])
        self.write("02VideoWorkSpace/private.txt", b"Private task")
        with self.assertRaisesRegex(check_share.ShareError, "任务数据"):
            build_share.build(self.root, includes=["02VideoWorkSpace/private.txt"])


if __name__ == "__main__":
    unittest.main()
