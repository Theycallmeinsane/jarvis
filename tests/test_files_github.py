"""File access and GitHub tools. File tests work only inside a temporary folder; GitHub calls are mocked."""
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from jarvis.config import Section, load_config
from jarvis.tools import ToolBox
from jarvis.tools import files_ext as fx
from jarvis.tools import github_tools as gt
from jarvis.tools.builtin import ToolFailed


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.context = {}


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "Downloads").mkdir()
        (self.root / "Documents" / "Invoices").mkdir(parents=True)
        old = time.time() - 3600
        for name, data in [("Downloads/Resume_Muhammad.pdf", b"%PDF-1.4 x"), ("Downloads/holiday.JPG", b"img"),
                           ("Downloads/song.mp3", b"abc" * 100), ("Downloads/copy of song.mp3", b"abc" * 100),
                           ("Downloads/setup.exe", b"MZ"), ("Downloads/half.crdownload", b"..."),
                           ("Documents/notes.txt", "Buy milk\nCall Ali\n".encode()),
                           ("Documents/Invoices/march.csv", b"a,b\n1,2\n")]:
            p = self.root / name
            p.write_bytes(data)
            os.utime(p, (old, old))
        self.cfg = Section(load_config())
        self.cfg["files"] = {"allowed_roots": [str(self.root)], "protected": []}
        self.ctx = Ctx(self.cfg)
        # on Windows this temp folder is under protected AppData; allowing it explicitly must still work

    def tearDown(self):
        self.tmp.cleanup()

    def r(self, spoken):
        return fx.resolve(spoken, self.cfg)

    def test_resolve_spoken_names(self):
        self.assertEqual(self.r(str(self.root / "Downloads")), self.root / "Downloads")
        self.assertEqual(self.r(f"resume pdf in {self.root / 'Downloads'}"), self.root / "Downloads/Resume_Muhammad.pdf")
        self.assertEqual(self.r(f"invoices in {self.root / 'Documents'}"), self.root / "Documents/Invoices")
        self.assertEqual(self.r(str(self.root / "Documents" / "note.txt")), self.root / "Documents/notes.txt")
        with self.assertRaises(ToolFailed):
            self.r(f"zebra in {self.root / 'Documents'}")
        self.assertEqual(fx._drive("the d drive"), Path("D:\\") if os.name == "nt" else Path("/"))

    def test_list_read_usage_duplicates(self):
        out = fx.list_folder(str(self.root / "Downloads"), sort="largest", ctx=self.ctx)
        self.assertIn("6 files and 0 folders", out)
        self.assertTrue(out.splitlines()[1].startswith("- song.mp3") or "copy of song" in out.splitlines()[1])
        self.assertIn("Buy milk", fx.read_file(f"notes in {self.root / 'Documents'}", ctx=self.ctx))
        self.assertIn("Downloads", fx.disk_usage(str(self.root), ctx=self.ctx))
        dup = fx.find_duplicates(str(self.root), ctx=self.ctx)
        self.assertIn("1 sets of duplicates", dup)
        self.assertIn("song.mp3", dup)
        with self.assertRaises(ToolFailed):
            fx.read_file(str(self.root / "Downloads/setup.exe"), ctx=self.ctx)

    def test_create_write_copy_never_overwrite(self):
        fx.create_folder(f"Reports in {self.root / 'Documents'}", ctx=self.ctx)
        self.assertTrue((self.root / "Documents/Reports").is_dir())
        fx.write_text_file(str(self.root / "Documents/notes"), "new", ctx=self.ctx)
        self.assertEqual((self.root / "Documents/notes.txt").read_text().splitlines()[0], "Buy milk")
        self.assertEqual((self.root / "Documents/notes (2).txt").read_text(), "new")
        fx.write_text_file(str(self.root / "Documents/notes.txt"), "Pay rent", append=True, ctx=self.ctx)
        self.assertIn("Pay rent", (self.root / "Documents/notes.txt").read_text())
        fx.copy_item(str(self.root / "Documents/notes.txt"), str(self.root / "Documents"), ctx=self.ctx)
        self.assertTrue((self.root / "Documents/notes (3).txt").exists())

    def test_rename_move_delete_are_confirmed_and_safe(self):
        box = ToolBox(self.cfg)
        for name in ("rename_item", "move_item", "delete_item", "organize_folder"):
            self.assertTrue(box.get(name).dangerous, name)
        for name in ("list_folder", "read_file", "copy_item", "create_folder"):
            self.assertFalse(box.get(name).dangerous, name)
        q = box.get("delete_item").confirmation({"path": str(self.root / "Downloads/setup.exe")})
        self.assertIn("setup.exe", q)
        self.assertIn("Recycle Bin", q)
        fx.rename_item(str(self.root / "Downloads/holiday.JPG"), "beach", ctx=self.ctx)
        self.assertTrue((self.root / "Downloads/beach.JPG").exists())
        fx.move_item(str(self.root / "Downloads/beach.JPG"), str(self.root / "Documents/Photos"), ctx=self.ctx)
        self.assertTrue((self.root / "Documents/Photos/beach.JPG").exists())
        with mock.patch("send2trash.send2trash") as trash:
            fx.delete_item(str(self.root / "Downloads/setup.exe"), ctx=self.ctx)
            trash.assert_called_once_with(str(self.root / "Downloads/setup.exe"))

    def test_protected_and_outside_roots_are_refused(self):
        with self.assertRaises(ToolFailed):
            fx.check_writable(self.root, self.cfg)            # the allowed root itself
        self.cfg["files"]["protected"] = [str(self.root / "Documents")]
        with self.assertRaises(ToolFailed):
            fx.delete_item(str(self.root / "Documents/notes.txt"), ctx=self.ctx)
        with self.assertRaises(ToolFailed):
            fx.check_writable(Path(tempfile.gettempdir()).resolve().parent / "x", self.cfg)
        with self.assertRaises(ToolFailed):
            fx.check_writable(fx.ROOT / "config.yaml", Section({"files": {"allowed_roots": [str(fx.ROOT.parent)]}}))

    def test_an_allowed_folder_inside_a_protected_one_wins(self):
        target = self.root / "Documents" / "new"
        with mock.patch.object(fx, "_soft_protected", return_value=[self.root.parent]):
            fx.check_writable(target, self.cfg)                              # explicit root inside it: allowed
            with self.assertRaises(ToolFailed):
                fx.check_writable(target, Section({"files": {"allowed_roots": "auto"}}))
        with self.assertRaises(ToolFailed):                                  # Windows and Jarvis itself: never
            fx.check_writable(fx.ROOT / "scratch" / "x",
                              Section({"files": {"allowed_roots": [str(fx.ROOT / "scratch")]}}))

    def test_organize_counts_and_skips_partial_downloads(self):
        q = fx._organize_question({"path": str(self.root / "Downloads")})
        self.assertIn("5 files", q)
        fx.organize_folder(str(self.root / "Downloads"), ctx=self.ctx)
        d = self.root / "Downloads"
        self.assertTrue((d / "Documents/Resume_Muhammad.pdf").exists())
        self.assertTrue((d / "Images/holiday.JPG").exists())
        self.assertTrue((d / "Installers/setup.exe").exists())
        self.assertTrue((d / "half.crdownload").exists())

    def test_zip_and_unzip_refuses_path_tricks(self):
        fx.zip_item(str(self.root / "Documents/Invoices"), ctx=self.ctx)
        z = self.root / "Documents/Invoices.zip"
        self.assertTrue(z.exists())
        fx.unzip_item(str(z), ctx=self.ctx)
        self.assertTrue((self.root / "Documents/Invoices (2)/Invoices/march.csv").exists())
        bad = self.root / "Downloads/bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../../escape.txt", "x")
        with self.assertRaises(ToolFailed):
            fx.unzip_item(str(bad), ctx=self.ctx)


def runs(*rows):
    return [dict({"databaseId": 1, "displayTitle": "x", "workflowName": "tests", "headBranch": "main",
                  "status": "completed", "conclusion": "success", "createdAt": "2026-10-17T10:00:00Z",
                  "event": "push", "url": ""}, **r) for r in rows]


class GitHubToolTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Section(load_config())
        self.cfg["github"] = {"enabled": True, "default_repo": None, "watch": ["me/jarvis"], "alerts": True}
        self.ctx = Ctx(self.cfg)
        p = mock.patch.object(gt, "gh_path", return_value="gh")
        p.start()
        self.addCleanup(p.stop)

    def test_tools_appear_only_with_gh(self):
        self.assertIsNotNone(ToolBox(self.cfg).get("github_builds"))
        with mock.patch.object(gt, "gh_path", return_value=None):
            self.assertIsNone(ToolBox(self.cfg).get("github_builds"))
        box = ToolBox(self.cfg)
        for name in ("github_create_issue", "github_comment", "github_rerun_build"):
            self.assertTrue(box.get(name).dangerous)
        self.assertFalse(any(n for n in box.tools if "merge" in n or "delete_repo" in n))

    def test_resolve_repo(self):
        with mock.patch.object(gt, "_my_repos", return_value=["me/jarvis", "me/resume-screener"]):
            self.assertEqual(gt.resolve_repo(self.cfg, ""), "me/jarvis")
            self.assertEqual(gt.resolve_repo(self.cfg, "my resume screener repo"), "me/resume-screener")
            self.assertEqual(gt.resolve_repo(self.cfg, "Jarvis"), "me/jarvis")
            self.assertEqual(gt.resolve_repo(self.cfg, "other/thing"), "other/thing")

    def test_repos_of_a_second_signed_in_account_are_found(self):
        def fake_json(*args, **kw):
            return {"user/repos?per_page=100&sort=pushed": ["work/api"],
                    "users/owner/repos?per_page=100&sort=pushed": ["owner/jarvis"]}[args[1]]
        self.cfg["github"]["watch"] = []
        gt._repo_cache = (0.0, [])
        self.addCleanup(setattr, gt, "_repo_cache", (0.0, []))
        with mock.patch.object(gt, "_accounts", return_value=["work", "owner"]), \
                mock.patch.object(gt, "gh_json", side_effect=fake_json):
            self.assertEqual(gt.resolve_repo(self.cfg, "jarvis"), "owner/jarvis")

    def test_builds_and_failure_excerpt(self):
        data = runs({"databaseId": 7, "conclusion": "failure", "displayTitle": "Add files"},
                    {"databaseId": 6})
        log_text = ("unit\tRun tests\t2026-10-17T10:00:01.1Z collecting...\n"
                    "unit\tRun tests\t2026-10-17T10:00:02.1Z test_volume ... ok\n"
                    "unit\tRun tests\t2026-10-17T10:00:03.1Z FAIL: test_organize (tests.test_files)\n"
                    "unit\tRun tests\t2026-10-17T10:00:03.2Z AssertionError: 4 != 5\n"
                    "unit\tRun tests\t2026-10-17T10:00:04.0Z ##[error]Process completed with exit code 1.\n")
        with mock.patch.object(gt, "gh_json", return_value=data):
            out = gt.github_builds(ctx=self.ctx)
            self.assertIn("failed", out)
            self.assertIn("passed", out)
            with mock.patch.object(gt, "gh", return_value=log_text) as g:
                out = gt.github_build_failure(ctx=self.ctx)
                self.assertIn("--log-failed", g.call_args.args)
        self.assertIn("Run 7", out)
        self.assertIn("AssertionError: 4 != 5", out)
        self.assertIn("[step: Run tests]", out)
        self.assertNotIn("2026-10-17T10", out)

    def test_nothing_failed(self):
        with mock.patch.object(gt, "gh_json", return_value=runs({})):
            self.assertIn("None of the recent builds", gt.github_build_failure(ctx=self.ctx))

    def test_gh_errors_are_sayable(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="To get started with GitHub CLI, please run:  gh auth login")
        with mock.patch.object(gt.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(gt.GitHubError, "gh auth login"):
                gt.gh("api", "user")

    def test_watcher_announces_only_new_failures_once(self):
        said = []
        w = gt.BuildWatcher(self.cfg, said.append)
        with mock.patch.object(gt, "gh_json", return_value=runs({"databaseId": 1, "conclusion": "failure"})):
            self.assertEqual(w.check(), [])          # old failure at startup: not announced
        new = runs({"databaseId": 2, "conclusion": "failure", "displayTitle": "Fix it"},
                   {"databaseId": 1, "conclusion": "failure"})
        with mock.patch.object(gt, "gh_json", return_value=new):
            self.assertEqual(len(w.check()), 1)
            self.assertEqual(w.check(), [])          # same failure: announced once
        self.assertIn("Fix it", said[0])
        self.assertIn("jarvis", said[0])

    def test_create_issue_says_number(self):
        with mock.patch.object(gt, "gh", return_value="https://github.com/me/jarvis/issues/12\n") as g:
            out = gt.github_create_issue("Wake word misses me", ctx=self.ctx)
        self.assertEqual(out, "Created issue 12 on me/jarvis.")
        self.assertIn("--title", g.call_args.args)


class GoogleSetupTests(unittest.TestCase):
    def test_finds_and_installs_the_downloaded_client(self):
        from jarvis import google_setup as gs

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            start = time.time()
            (d / "other.json").write_text(json.dumps({"web": {"client_id": "x"}}))
            good = d / "client_secret_123.apps.googleusercontent.com.json"
            good.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))
            self.assertEqual(gs.find_client_file(d, start), good)
            cfg = Section(load_config())
            cfg["google"] = {"credentials_file": str(d / "secrets/google_credentials.json"),
                             "token_file": str(d / "secrets/google_token.json")}
            dest = gs.install_client_file(good, cfg)
            self.assertTrue(gs.is_client_file(dest))
            self.assertFalse(good.exists())
            self.assertEqual(len(gs.STEPS), 5)
            self.assertTrue(all(url.startswith("https://console.cloud.google.com/") for _, url, _ in gs.STEPS))


if __name__ == "__main__":
    unittest.main()
