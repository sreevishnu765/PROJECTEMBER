"""
File operations by name (ember_tools/file_ops.py) and their wiring in ember_core.
Everything runs inside a temp directory: nothing real is ever moved.

Run from the project root:  python -m unittest tests.test_file_ops
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

from ember_tools import computer, file_ops  # noqa: E402


class FileOpsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.project = t / "project"
        self.home = t / "home"
        self.outside = t / "elsewhere"
        for d in (self.project / "data" / "uploads", self.project / "data" / "trash", self.project / "auth",
                  self.home / "Documents", self.home / "Downloads", self.home / "Archive", self.outside):
            d.mkdir(parents=True)
        (self.project / "ember_core.py").write_text("# core")
        (self.project / ".env").write_text("SECRET=1")
        (self.project / "auth" / "token.json").write_text("{}")
        allow = t / "allowed.json"
        allow.write_text(json.dumps([str(self.project), str(self.home)]))
        self.patches = [
            mock.patch.object(computer, "_ALLOWLIST_PATH", allow),
            mock.patch.object(file_ops, "_PROJECT_ROOT", self.project),
            mock.patch.object(file_ops, "TRASH_DIR", self.project / "data" / "trash"),
            mock.patch.object(file_ops, "JOURNAL_PATH", self.project / "data" / "file_ops.json"),
            mock.patch.object(Path, "home", classmethod(lambda cls: self.home)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def make(self, path: Path, text="x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class ResolveTests(FileOpsBase):
    def test_typo_and_type_word_resolve(self):
        f = self.make(self.project / "data" / "uploads" / "1_drivetrain_comparision.pdf")
        for q in ("drivetrain comparison pdf", "the drivetrain pdf", "drivetrain_comparision.pdf", "drivetrain comparision"):
            r = file_ops.resolve_file(q)
            self.assertEqual((r.status, r.path), ("ok", f.resolve()), q)

    def test_two_plausible_files_means_ask_not_guess(self):
        a = self.make(self.home / "Documents" / "report.pdf")
        b = self.make(self.home / "Downloads" / "report.pdf")
        r = file_ops.resolve_file("report.pdf")
        self.assertEqual(r.status, "ambiguous")
        self.assertEqual({p for p in r.candidates}, {a.resolve(), b.resolve()})
        self.assertIn("Documents", r.message)
        self.assertIn("Downloads", r.message)

    def test_nothing_found_is_honest(self):
        r = file_ops.resolve_file("quarterly budget")
        self.assertEqual(r.status, "none")

    def test_known_files_from_the_registry_win_over_lookalikes(self):
        known = self.make(self.project / "data" / "uploads" / "notes_final.txt")
        self.make(self.home / "Documents" / "notes_final_v2.txt")
        r = file_ops.resolve_file("notes final", known_paths=[str(known)])
        self.assertEqual((r.status, r.path), ("ok", known.resolve()))

    def test_same_words_beat_a_longer_lookalike(self):
        exact = self.make(self.home / "Documents" / "notes_final.txt")
        self.make(self.home / "Documents" / "notes_final_v2.txt")
        self.assertEqual(file_ops.resolve_file("notes final").path, exact.resolve())

    def test_pronoun_uses_last_file_then_registry(self):
        a = self.make(self.home / "Documents" / "a.txt")
        b = self.make(self.home / "Documents" / "b.txt")
        self.assertEqual(file_ops.resolve_file("that", last_file=str(a)).path, a.resolve())
        self.assertEqual(file_ops.resolve_file("it", known_paths=[str(b)]).path, b.resolve())
        self.assertEqual(file_ops.resolve_file("that").status, "none")

    def test_project_files_are_invisible_and_protected(self):
        self.assertEqual(file_ops.resolve_file("ember_core.py").status, "none")
        r = file_ops.resolve_file(str(self.project / "ember_core.py"))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(file_ops.resolve_file(str(self.project / "auth" / "token.json")).status, "blocked")
        self.assertEqual(file_ops.resolve_file(str(self.project / ".env")).status, "blocked")

    def test_outside_the_allowlist_is_blocked_with_a_hint(self):
        f = self.make(self.outside / "secret.txt")
        r = file_ops.resolve_file(str(f))
        self.assertEqual(r.status, "blocked")
        self.assertIn("allow access", r.message)

    def test_folder_resolution(self):
        self.assertEqual(file_ops.resolve_folder("documents").path, (self.home / "Documents").resolve())
        self.assertEqual(file_ops.resolve_folder("the Archive folder").path, (self.home / "Archive").resolve())
        self.assertEqual(file_ops.resolve_folder("uploads").path, (self.project / "data" / "uploads").resolve())
        self.assertEqual(file_ops.resolve_folder(str(self.project)).status, "blocked")   # can't drop files in the project root
        self.assertEqual(file_ops.resolve_folder(str(self.outside)).status, "blocked")
        self.assertEqual(file_ops.resolve_folder("nonexistent stuff").status, "none")


class OperationTests(FileOpsBase):
    def test_move_by_names(self):
        f = self.make(self.home / "Downloads" / "invoice.pdf", "data")
        r = file_ops.move_file("invoice.pdf to Documents")
        self.assertTrue(r.ok, r.message)
        self.assertFalse(f.exists())
        self.assertEqual((self.home / "Documents" / "invoice.pdf").read_text(), "data")
        self.assertEqual((r.src, r.dst), (str(f.resolve()), str((self.home / "Documents" / "invoice.pdf").resolve())))

    def test_move_with_the_word_to_inside_the_filename(self):
        self.make(self.home / "Downloads" / "how to guide.pdf")
        r = file_ops.move_file("how to guide.pdf to Archive")
        self.assertTrue(r.ok, r.message)
        self.assertTrue((self.home / "Archive" / "how to guide.pdf").exists())

    def test_move_never_overwrites(self):
        src = self.make(self.home / "Downloads" / "a.txt", "new")
        self.make(self.home / "Documents" / "a.txt", "old")
        r = file_ops.move_file(f"{src} to Documents")
        self.assertFalse(r.ok)
        self.assertIn("won't overwrite", r.message)
        self.assertEqual((self.home / "Documents" / "a.txt").read_text(), "old")
        self.assertTrue((self.home / "Downloads" / "a.txt").exists())

    def test_move_needs_a_destination_and_a_findable_file(self):
        self.make(self.home / "Downloads" / "a.txt")
        self.assertIn("where", file_ops.move_file("a.txt").message.lower())
        self.assertFalse(file_ops.move_file("ghost.txt to Documents").ok)
        self.assertFalse(file_ops.move_file("a.txt to nowhere land").ok)

    def test_move_into_project_root_is_refused(self):
        f = self.make(self.home / "Downloads" / "a.txt")
        r = file_ops.move_file(f"a.txt to {self.project}")
        self.assertFalse(r.ok)
        self.assertTrue(f.exists())

    def test_rename_keeps_extension_and_validates(self):
        f = self.make(self.home / "Documents" / "draft.pdf")
        r = file_ops.rename_file("draft.pdf to final")
        self.assertTrue(r.ok, r.message)
        self.assertTrue((self.home / "Documents" / "final.pdf").exists())
        self.assertFalse(f.exists())
        bad = file_ops.rename_file("final.pdf to a/b")
        self.assertFalse(bad.ok)
        self.assertIn("can't contain", bad.message)
        self.assertFalse(file_ops.rename_file("final.pdf to CON").ok)

    def test_rename_never_overwrites(self):
        self.make(self.home / "Documents" / "a.txt")
        self.make(self.home / "Documents" / "b.txt", "keep")
        r = file_ops.rename_file("a.txt to b.txt")
        self.assertFalse(r.ok)
        self.assertEqual((self.home / "Documents" / "b.txt").read_text(), "keep")

    def test_rename_of_protected_file_is_refused(self):
        r = file_ops.rename_file(f"{self.project / 'ember_core.py'} to hacked.py")
        self.assertFalse(r.ok)
        self.assertTrue((self.project / "ember_core.py").exists())

    def test_delete_needs_real_confirmation_and_is_recoverable(self):
        f = self.make(self.home / "Documents" / "old.txt", "bye")
        asked = []
        no = file_ops.trash_file("old.txt", lambda p: asked.append(p) or False)
        self.assertFalse(no.ok)
        self.assertTrue(f.exists())
        self.assertEqual(asked, [str(f.resolve())])       # confirmation showed the EXACT resolved path

        yes = file_ops.trash_file("old.txt", lambda p: True)
        self.assertTrue(yes.ok, yes.message)
        self.assertFalse(f.exists())
        trashed = list((self.project / "data" / "trash").glob("*old.txt"))
        self.assertEqual(len(trashed), 1)

        undo = file_ops.undo_last()
        self.assertTrue(undo.ok, undo.message)
        self.assertEqual(f.read_text(), "bye")
        self.assertEqual(list((self.project / "data" / "trash").glob("*old.txt")), [])

    def test_delete_fails_closed_when_confirmation_errors(self):
        f = self.make(self.home / "Documents" / "keep.txt")

        def boom(_):
            raise RuntimeError("no gate")

        self.assertFalse(file_ops.trash_file("keep.txt", boom).ok)
        self.assertTrue(f.exists())

    def test_undo_move_and_rename_in_reverse_order(self):
        f = self.make(self.home / "Downloads" / "x.txt")
        file_ops.move_file("x.txt to Documents")
        file_ops.rename_file("x.txt to y.txt")
        self.assertTrue((self.home / "Documents" / "y.txt").exists())
        self.assertTrue(file_ops.undo_last().ok)                          # undoes the rename
        self.assertTrue((self.home / "Documents" / "x.txt").exists())
        self.assertTrue(file_ops.undo_last().ok)                          # then the move
        self.assertTrue(f.exists())
        self.assertIn("nothing to undo", file_ops.undo_last().message)

    def test_undo_refuses_when_original_spot_is_taken(self):
        f = self.make(self.home / "Downloads" / "x.txt")
        file_ops.move_file("x.txt to Documents")
        self.make(f, "someone else's file")
        r = file_ops.undo_last()
        self.assertFalse(r.ok)
        self.assertEqual(f.read_text(), "someone else's file")


class GuardTests(unittest.TestCase):
    def test_real_file_requests_pass(self):
        for s in ("move the drivetrain pdf to documents", "move notes.txt to archive", "rename that file to final",
                  "move it to Downloads", r"move C:\a\b.txt to D:\c", "delete the file old.txt"):
            self.assertTrue(file_ops.looks_like_file_request(s), s)

    def test_everyday_sentences_do_not(self):
        for s in ("move the meeting to friday", "move this conversation to my phone", "rename the project later",
                  "delete that", "move on to the next topic"):
            self.assertFalse(file_ops.looks_like_file_request(s), s)


class CoreBase(FileOpsBase):
    def setUp(self):
        super().setUp()
        import ember_core
        from ember_conversation import EmberConversation
        from ember_files import FileRegistry
        self.core = ember_core
        self.registry = FileRegistry(str(self.project / "data" / "files.json"))
        self.conv = EmberConversation("wiring")
        self.asked = []
        gate = mock.MagicMock()
        gate.request_confirmation.side_effect = lambda name, args, conv=None: self.asked.append((name, args)) or True
        self.conv.confirm_gate = gate
        p = mock.patch.object(ember_core, "file_registry", self.registry)
        p.start()
        self.addCleanup(p.stop)
        ember_core._dispatch_context["last_file"] = None

    def act(self, message):
        payload = self.core._classify_action(message)
        self.assertIsNotNone(payload, message)
        return payload[0].name, self.core._dispatch_action(payload, None, conversation=self.conv)


class CoreWiringTests(CoreBase):
    def test_routing_and_precedence(self):
        route = lambda m: (self.core._classify_action(m) or (None,))[0]  # noqa: E731
        name = lambda m: getattr(route(m), "name", None)  # noqa: E731
        self.assertEqual(name("move the drivetrain pdf to documents"), "move_file")
        self.assertEqual(name("rename notes.txt to todo"), "rename_file")
        self.assertEqual(name("delete the file old.txt"), "delete_file")
        self.assertEqual(name("undo that"), "undo_file_op")
        self.assertEqual(name("remind me to move the file to documents tomorrow"), "create_reminder")
        self.assertIsNone(name("move the meeting to friday"))
        self.assertIsNone(name("move this conversation to my phone"))
        self.assertIsNone(name("delete that"))
        self.assertIsNone(name("move that to friday"))          # no recent file -> not a file request
        self.core._dispatch_context["last_file"] = str(self.make(self.home / "Documents" / "z.txt"))
        self.assertEqual(name("move that to archive"), "move_file")
        self.assertEqual(name("rename it to final"), "rename_file")

    def test_move_by_name_updates_the_files_panel_and_that_context(self):
        up = self.make(self.project / "data" / "uploads" / "111_drivetrain_comparision.pdf")
        self.registry.record("upload", "attached", str(up), label="drivetrain_comparision.pdf")
        tool, reply = self.act("move the drivetrain pdf to documents")
        self.assertEqual(tool, "move_file")
        moved = self.home / "Documents" / "111_drivetrain_comparision.pdf"
        self.assertTrue(moved.exists(), reply)
        entry = self.registry.list_all()[0]
        self.assertEqual(Path(entry["path"]).resolve(), moved.resolve())     # panel follows the file
        # "that file" now means the moved file
        _, reply2 = self.act("rename that file to final ember")
        self.assertTrue((self.home / "Documents" / "final.pdf").exists() or (self.home / "Documents" / "final ember.pdf").exists(), reply2)

    def test_delete_confirmation_shows_the_resolved_file_and_undo_restores(self):
        f = self.make(self.home / "Documents" / "old.txt")
        self.registry.record("generated", "pdf_export", str(f))
        tool, reply = self.act("delete the file old.txt")
        self.assertFalse(f.exists(), reply)
        name, args = self.asked[-1]
        self.assertEqual(name, "delete_file")
        self.assertEqual(Path(args["file"]).resolve(), f.resolve())
        self.assertEqual(self.registry.list_all(), [])                       # gone from the panel too
        _, reply2 = self.act("undo that")
        self.assertTrue(f.exists(), reply2)

    def test_folder_delete_confirmation_carries_the_contents_summary(self):
        d = self.home / "Documents" / "Old stuff"
        self.make(d / "a.txt")
        self.make(d / "b.txt")
        _, reply = self.act("delete the old stuff folder")
        self.assertFalse(d.exists(), reply)                       # approved, so it really went to the trash
        name, args = self.asked[-1]
        self.assertEqual(Path(args["file"]).resolve(), d.resolve())
        self.assertIn("2 file(s)", args["contents"])

    def test_upload_sets_last_file_for_pronouns(self):
        up = self.make(self.project / "data" / "uploads" / "5_notes.txt")
        self.core._on_attachment_saved({"path": str(up), "name": "notes.txt"})
        _, reply = self.act("move it to archive")
        self.assertTrue((self.home / "Archive" / "5_notes.txt").exists(), reply)


class FolderTests(FileOpsBase):
    def folder(self, path: Path, *files: str) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        for f in files:
            self.make(path / f, f)
        return path

    def test_move_folder_by_name_keeps_contents(self):
        src = self.folder(self.home / "Downloads" / "Reports", "a.txt", "sub/b.txt")
        r = file_ops.move_file("the reports folder to Archive")
        self.assertTrue(r.ok, r.message)
        self.assertTrue(r.is_dir)
        self.assertFalse(src.exists())
        self.assertEqual((self.home / "Archive" / "Reports" / "sub" / "b.txt").read_text(), "sub/b.txt")

    def test_a_file_and_a_folder_with_the_same_name_means_ask(self):
        self.folder(self.home / "Documents" / "Reports")
        self.make(self.home / "Downloads" / "reports.txt")
        r = file_ops.resolve_item("reports")
        self.assertEqual(r.status, "ambiguous")
        self.assertIn("(folder)", r.message)
        self.assertEqual(file_ops.resolve_item("reports folder").status, "ok")      # the word folder disambiguates
        self.assertEqual(file_ops.resolve_item("reports.txt").status, "ok")          # so does an extension

    def test_cannot_move_a_folder_into_itself_or_its_own_subfolder(self):
        self.folder(self.home / "Reports" / "Sub")
        r = file_ops.move_file("Reports to Sub")
        self.assertFalse(r.ok)
        self.assertIn("into itself", r.message)
        self.assertTrue((self.home / "Reports" / "Sub").exists())

    def test_move_folder_never_overwrites_an_existing_folder(self):
        src = self.folder(self.home / "Downloads" / "Reports", "new.txt")
        self.folder(self.home / "Archive" / "Reports", "old.txt")
        r = file_ops.move_file(f"{src} to Archive")
        self.assertFalse(r.ok)
        self.assertTrue((src / "new.txt").exists())
        self.assertTrue((self.home / "Archive" / "Reports" / "old.txt").exists())

    def test_rename_folder_adds_no_extension_and_allows_dots(self):
        self.folder(self.home / "Archive" / "Reports", "a.txt")
        r = file_ops.rename_file("Reports folder to v2.1")
        self.assertTrue(r.ok, r.message)
        self.assertTrue((self.home / "Archive" / "v2.1" / "a.txt").exists())
        self.assertTrue(file_ops.undo_last().ok)
        self.assertTrue((self.home / "Archive" / "Reports" / "a.txt").exists())

    def test_delete_folder_confirms_with_item_count_then_trash_and_undo(self):
        src = self.folder(self.home / "Documents" / "Old stuff", "a.txt", "b/c.txt")
        seen = []
        no = file_ops.trash_file("old stuff folder", lambda p, detail="": seen.append((p, detail)) or False)
        self.assertFalse(no.ok)
        self.assertTrue(src.exists())
        self.assertEqual(seen[0][0], str(src.resolve()))
        self.assertIn("2 file(s)", seen[0][1])
        yes = file_ops.trash_file("old stuff folder", lambda p, detail="": True)
        self.assertTrue(yes.ok, yes.message)
        self.assertFalse(src.exists())
        self.assertTrue(file_ops.undo_last().ok)
        self.assertEqual((src / "b" / "c.txt").read_text(), "b/c.txt")

    def test_huge_folder_is_refused_before_any_prompt(self):
        src = self.folder(self.home / "Documents" / "Big", "1", "2", "3")
        asked = []
        with mock.patch.object(file_ops, "TRASH_MAX_ITEMS", 2):
            r = file_ops.trash_file("Big folder", lambda *a: asked.append(a) or True)
        self.assertFalse(r.ok)
        self.assertIn("too big", r.message)
        self.assertEqual(asked, [])
        self.assertTrue(src.exists())

    def test_dangerous_folders_are_blocked(self):
        cases = {
            str(self.home / "Documents"): "standard Windows folders",
            str(self.home): "top-level",
            str(self.project): "top-level",
            str(self.project / "data" / "uploads"): "Ember needs",
            str(self.project / "auth"): "project files",
            str(self.project.parent): "allowed",     # ancestor of the allowlisted roots (and outside them)
        }
        self.folder(self.home / "Archive")
        for path, phrase in cases.items():
            for op in (file_ops.move_file, file_ops.rename_file):
                tail = f"{path} to Archive" if op is file_ops.move_file else f"{path} to zzz"
                r = op(tail)
                self.assertFalse(r.ok, (op.__name__, path))
            r = file_ops.trash_file(path, lambda *a: True)
            self.assertFalse(r.ok, path)
            self.assertTrue(Path(path).exists(), path)

    def test_pronoun_can_mean_a_folder(self):
        d = self.folder(self.home / "Downloads" / "Pics", "x.png")
        r = file_ops.move_file("that folder to Archive", last_file=str(d))
        self.assertTrue(r.ok, r.message)
        self.assertTrue((self.home / "Archive" / "Pics" / "x.png").exists())
        self.assertEqual(file_ops.resolve_item("that folder", last_file=str(self.make(self.home / "Documents" / "f.txt"))).status, "none")


class OpenTests(CoreBase):
    def setUp(self):
        super().setUp()
        self.opened, self.revealed = [], []
        for p in (mock.patch.object(file_ops, "_native_open", lambda path: self.opened.append(path)),
                  mock.patch.object(file_ops, "_native_reveal", lambda path: self.revealed.append(path))):
            p.start()
            self.addCleanup(p.stop)

    def test_open_by_name_type_word_and_folder(self):
        pdf = self.make(self.project / "data" / "uploads" / "9_drivetrain_comparision.pdf")
        self.assertTrue(file_ops.open_item("the drivetrain pdf").ok)
        self.assertEqual(self.opened[-1], str(pdf.resolve()))
        self.assertTrue(file_ops.open_item("the Documents folder").ok)
        self.assertEqual(self.opened[-1], str((self.home / "Documents").resolve()))
        miss = file_ops.open_item("quarterly budget")
        self.assertFalse(miss.ok)
        self.assertEqual(len(self.opened), 2)

    def test_scripts_and_installers_are_shown_not_launched(self):
        for name in ("setup.exe", "run.bat", "tool.py", "x.ps1", "evil.lnk"):
            f = self.make(self.home / "Downloads" / name)
            r = file_ops.open_item(str(f))
            self.assertTrue(r.ok)
            self.assertIn("File Explorer", r.message)
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.revealed), 5)

    def test_reveal_and_missing_file(self):
        f = self.make(self.home / "Documents" / "a.txt")
        self.assertTrue(file_ops.open_path(str(f), reveal=True).ok)
        self.assertEqual(self.revealed, [str(f)])
        f.unlink()
        gone = file_ops.open_path(str(f))
        self.assertFalse(gone.ok)
        self.assertIn("any more", gone.message)

    def test_routing_open_file_vs_launch_app(self):
        name = lambda m: (self.core._classify_action(m) or (None,))[0]  # noqa: E731
        name = lambda m, _n=name: getattr(_n(m), "name", None)  # noqa: E731
        self.assertEqual(name("open the drivetrain pdf"), "open_file")
        self.assertEqual(name("open notes.txt"), "open_file")
        self.assertEqual(name("open the downloads folder"), "open_file")
        self.assertEqual(name("open that file"), "open_file")
        self.assertEqual(name("open file explorer"), "launch_app")      # the Windows app, not a file
        self.assertEqual(name("open explorer"), "launch_app")
        self.assertEqual(name("open files"), "launch_app")
        self.assertEqual(name("open notepad"), "launch_app")
        self.assertEqual(name("open chrome"), "launch_app")
        self.assertEqual(name("launch spotify"), "launch_app")
        self.assertEqual(name("open it"), "launch_app")                 # nothing recent for "it" to mean
        self.core._dispatch_context["last_file"] = str(self.make(self.home / "Documents" / "z.txt"))
        self.assertEqual(name("open it"), "open_file")
        self.assertEqual(name("show that file in explorer"), "reveal_file")

    def test_open_that_file_after_a_move_actually_opens_it(self):
        up = self.make(self.project / "data" / "uploads" / "1_notes.txt")
        self.core._on_attachment_saved({"path": str(up), "name": "notes.txt"})
        self.act("move that to documents")
        tool, reply = self.act("open that file")
        self.assertEqual(tool, "open_file")
        self.assertEqual(self.opened[-1], str((self.home / "Documents" / "1_notes.txt").resolve()), reply)
        _, reply2 = self.act("show it in explorer")
        self.assertEqual(self.revealed[-1], str((self.home / "Documents" / "1_notes.txt").resolve()), reply2)

    def test_folder_move_keeps_the_files_panel_in_sync(self):
        d = self.home / "Downloads" / "Pics"
        f = self.make(d / "x.png")
        self.registry.record("upload", "attached", str(f), label="x.png")
        self.act("move the pics folder to archive")
        entry = self.registry.list_all()[0]
        self.assertEqual(Path(entry["path"]).resolve(), (self.home / "Archive" / "Pics" / "x.png").resolve())
        self.act("delete the pics folder")
        self.assertEqual(self.registry.list_all(), [])

    def test_files_panel_query_opens_by_entry_id_only(self):
        from ember_query_registry import get_query_registry
        q = get_query_registry()
        f = self.make(self.project / "data" / "uploads" / "p.txt")
        entry = self.registry.record("upload", "attached", str(f), label="p.txt")
        ok = q.dispatch("file_open", {"id": entry["id"]})
        self.assertTrue(ok["ok"] and ok["data"]["opened"], ok)
        self.assertEqual(self.opened[-1], str(f))
        self.assertTrue(q.dispatch("file_open", {"id": entry["id"], "reveal": True})["data"]["opened"])
        self.assertEqual(self.revealed[-1], str(f))
        self.assertFalse(q.dispatch("file_open", {"id": "nope"})["data"]["opened"])
        self.assertFalse(q.dispatch("file_open", {"path": str(f)})["data"]["opened"])      # a raw path is never accepted
        nopath = self.registry.record("generated", "drive_download", None, label="drive thing")
        self.assertFalse(q.dispatch("file_open", {"id": nopath["id"]})["data"]["opened"])
        f.unlink()
        gone = q.dispatch("file_open", {"id": entry["id"]})["data"]
        self.assertFalse(gone["opened"])
        self.assertIn("any more", gone["message"])


class RegistryPrefixTests(unittest.TestCase):
    def test_prefix_update_and_remove(self):
        from ember_files import FileRegistry
        with tempfile.TemporaryDirectory() as t:
            reg = FileRegistry(os.path.join(t, "f.json"))
            a = reg.record("upload", "attached", os.path.join(t, "old", "a.txt"))
            b = reg.record("upload", "attached", os.path.join(t, "old", "sub", "b.txt"))
            c = reg.record("upload", "attached", os.path.join(t, "older", "c.txt"))   # shares a string prefix, NOT a path prefix
            self.assertEqual(reg.update_prefix(os.path.join(t, "old"), os.path.join(t, "new")), 2)
            paths = {e["id"]: e["path"] for e in reg.list_all()}
            self.assertEqual(paths[a["id"]], os.path.join(t, "new", "a.txt"))
            self.assertEqual(paths[b["id"]], os.path.join(t, "new", "sub", "b.txt"))
            self.assertEqual(paths[c["id"]], os.path.join(t, "older", "c.txt"))
            self.assertEqual(reg.remove_prefix(os.path.join(t, "new")), 2)
            self.assertEqual([e["id"] for e in reg.list_all()], [c["id"]])
            self.assertEqual(reg.get(c["id"])["path"], os.path.join(t, "older", "c.txt"))
            self.assertIsNone(reg.get("missing"))


if __name__ == "__main__":
    unittest.main()
