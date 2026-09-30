"""Tests for ember_attachments.py (chat file uploads) and its wiring."""
import base64
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ember_attachments as ea


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _att(name, data: bytes, mime="application/octet-stream"):
    return {"name": name, "mime": mime, "data": _b64(data)}


class SaveAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "uploads"

    def tearDown(self):
        self.tmp.cleanup()

    def test_saves_text_file_with_safe_unique_name(self):
        a, err = ea.save_attachments([_att("notes.txt", b"hello")], self.dir)
        self.assertIsNone(err)
        b, _ = ea.save_attachments([_att("notes.txt", b"hello")], self.dir)
        self.assertEqual(a[0]["kind"], "text")
        self.assertNotEqual(a[0]["path"], b[0]["path"])  # never overwrites
        self.assertEqual(Path(a[0]["path"]).read_bytes(), b"hello")
        self.assertEqual(a[0]["name"], "notes.txt")

    def test_traversal_and_weird_names_are_neutralised(self):
        saved, err = ea.save_attachments([_att("../../etc/pass wd?.txt", b"x")], self.dir)
        self.assertIsNone(err)
        p = Path(saved[0]["path"])
        self.assertEqual(p.parent, self.dir)
        self.assertNotIn("..", p.name)

    def test_rejects_blocked_extension_and_saves_nothing(self):
        saved, err = ea.save_attachments([_att("ok.txt", b"x"), _att("bad.EXE", b"x")], self.dir)
        self.assertEqual(saved, [])
        self.assertIn("isn't allowed", err)
        self.assertEqual(list(self.dir.glob("*")) if self.dir.exists() else [], [])

    def test_rejects_bad_base64_empty_and_oversize(self):
        _, err = ea.save_attachments([{"name": "a.txt", "mime": "", "data": "!!!notb64"}], self.dir)
        self.assertIn("corrupted", err)
        _, err = ea.save_attachments([_att("a.txt", b"")], self.dir)
        self.assertIn("empty", err)
        big = b"x" * (ea.MAX_FILE_BYTES + 1)
        _, err = ea.save_attachments([_att("big.txt", big)], self.dir)
        self.assertIn("over", err)

    def test_rejects_too_many_and_too_large_total(self):
        many = [_att(f"f{i}.txt", b"x") for i in range(ea.MAX_FILES_PER_MESSAGE + 1)]
        _, err = ea.save_attachments(many, self.dir)
        self.assertIn("Too many", err)
        chunk = b"x" * (ea.MAX_FILE_BYTES - 10)
        _, err = ea.save_attachments([_att("a.txt", chunk), _att("b.txt", chunk)], self.dir)
        self.assertIn("total", err)

    def test_binary_masquerading_as_text_is_treated_as_other(self):
        saved, _ = ea.save_attachments([_att("data.txt", b"ab\x00cd")], self.dir)
        self.assertEqual(saved[0]["kind"], "other")

    def test_malformed_items(self):
        _, err = ea.save_attachments(["nope"], self.dir)
        self.assertEqual(err, "Malformed attachment.")


class BlockAndAugmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, name, data):
        saved, err = ea.save_attachments([_att(name, data)], self.dir)
        self.assertIsNone(err)
        return saved

    def test_text_is_injected_inside_untrusted_markers_and_truncated(self):
        saved = self._save("big.txt", b"A" * (ea.MAX_TEXT_CHARS + 500))
        block = ea.build_block(saved, "summarise")
        self.assertIn("untrusted", block)
        self.assertIn("--- file: big.txt", block)
        self.assertIn("truncated", block)

    def test_image_uses_vision_with_users_question(self):
        seen = {}

        def vision(raw, mime, q):
            seen.update(raw=raw, mime=mime, q=q)
            return "a red square"

        saved = self._save("pic.PNG", b"\x89PNG fake")
        block = ea.build_block(saved, "what colour is it?", vision)
        self.assertIn("a red square", block)
        self.assertEqual(seen["mime"], "image/png")
        self.assertEqual(seen["q"], "what colour is it?")

    def test_image_without_vision_is_honest(self):
        saved = self._save("pic.jpg", b"fake")
        self.assertIn("unavailable", ea.build_block(saved, "q", None))

    def test_vision_failure_is_reported_not_raised(self):
        def boom(*a):
            raise RuntimeError("quota")

        saved = self._save("pic.jpg", b"fake")
        self.assertIn("vision analysis failed", ea.build_block(saved, "q", boom))

    def test_pdf_text_extracted(self):
        from pypdf import PdfWriter
        import io
        # a blank page has no text -> exercises the honest "no text" path
        w = PdfWriter()
        w.add_blank_page(200, 200)
        buf = io.BytesIO()
        w.write(buf)
        saved = self._save("blank.pdf", buf.getvalue())
        self.assertEqual(saved[0]["kind"], "pdf")
        self.assertIn("no text could be extracted", ea.build_block(saved, "q"))

    def test_corrupt_pdf_does_not_raise(self):
        saved = self._save("bad.pdf", b"not really a pdf")
        self.assertIn("no text could be extracted", ea.build_block(saved, "q"))

    def test_augment_injects_once_then_sticky_for_follow_ups_then_stops(self):
        conv = SimpleNamespace(attachments=self._save("n.txt", b"secret plan"))
        statuses, recorded = [], []
        out = ea.augment_prompt("what is in it?", conv, status_fn=statuses.append, on_saved=recorded.append)
        self.assertIn("secret plan", out)
        self.assertIsNone(conv.attachments)              # consumed
        self.assertEqual(len(statuses), 1)
        self.assertEqual(recorded[0]["name"], "n.txt")   # registry hook fired

        for _ in range(ea.STICKY_TURNS):                 # follow-ups keep the context
            self.assertIn("secret plan", ea.augment_prompt("and then?", conv))
        # ...and eventually it lapses, so old files don't haunt the conversation
        self.assertNotIn("secret plan", ea.augment_prompt("unrelated", conv))
        self.assertEqual(ea.augment_prompt("unrelated", conv), "unrelated")

    def test_no_attachments_leaves_prompt_untouched(self):
        self.assertEqual(ea.augment_prompt("hi", SimpleNamespace()), "hi")


class WiringTests(unittest.TestCase):
    """Cheap static guards that the hooks stay wired where they must be."""

    def test_transport_raises_websocket_size_limit(self):
        src = Path("ember_transport.py").read_text(encoding="utf-8")
        self.assertIn("max_size=MAX_MESSAGE_BYTES", src)
        self.assertGreater(32 * 1024 * 1024, (ea.MAX_TOTAL_BYTES * 4) // 3)

    def test_core_bypasses_tools_when_files_attached_and_augments_prompt(self):
        src = Path("ember_core.py").read_text(encoding="utf-8")
        self.assertIn('if getattr(conversation, "attachments", None):', src)
        self.assertIn("effective_prompt = _augment_with_attachments(effective_prompt", src)


if __name__ == "__main__":
    unittest.main()
