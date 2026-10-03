import os, tempfile, unittest
from ember_stt_adapt import Vocabulary


class VocabTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "v.txt")
        self.v = Vocabulary(self.path)

    def test_seeded_and_hotwords(self):
        self.assertIn("Verstappen", self.v.hotwords())
        self.assertTrue(self.v.prompt().startswith("Glossary:"))

    def test_fuzzy_fixes_near_miss_only(self):
        self.assertEqual(self.v.correct("Verstapen won"), "Verstappen won")
        self.assertEqual(self.v.correct("I remember December"), "I remember December")   # look-alikes untouched
        self.assertEqual(self.v.correct("an amber light"), "an amber light")
        self.assertEqual(self.v.correct("version two"), "version two")

    def test_explicit_correction_and_add(self):
        self.assertEqual(self.v.add(term="Verstappen", heard="max will stop"), "added")
        self.assertEqual(self.v.correct("Max will stop is fast"), "Verstappen is fast")
        self.assertEqual(self.v.add(term="Mentalist"), "added")
        self.assertEqual(self.v.add(term="mentalist"), "already there")

    def test_edit_on_disk_applies_without_restart(self):
        import time; time.sleep(0.01)
        with open(self.path, "a") as f:
            f.write("humber => Ember\n")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.v.correct("Humber, stop"), "Ember, stop")

    def test_core_tools_match(self):
        import ember_core
        for phrase, tool in (("when you hear max will stop I mean Verstappen", "teach_speech_correction"),
                             ("add Verstappen to your vocabulary", "teach_speech_term")):
            hit = ember_core._tool_registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit[0].name, tool)


if __name__ == "__main__":
    unittest.main()
