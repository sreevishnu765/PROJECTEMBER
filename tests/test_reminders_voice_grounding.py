import asyncio, os, tempfile, unittest
from types import SimpleNamespace

import ember_attachments as ea
import ember_core
from ember_reminders import reminder_speech, make_timer_message, display_message


class SpeechTests(unittest.TestCase):
    def test_reminder_wording(self):
        self.assertEqual(reminder_speech("submit Alpha"), "Reminder to submit Alpha, sir.")
        self.assertEqual(reminder_speech("to call mum."), "Reminder to call mum, sir.")
        self.assertEqual(reminder_speech("this"), "Reminder, sir.")

    def test_timer_wording(self):
        self.assertEqual(reminder_speech(make_timer_message("10 minute")), "Your 10 minute timer is up, sir.")
        self.assertEqual(reminder_speech(make_timer_message("5 minute", "pasta")), "Your 5 minute timer for pasta is up, sir.")
        self.assertEqual(display_message(make_timer_message("5 minute", "pasta")), "5 minute timer (pasta)")


class TimerToolTests(unittest.TestCase):
    def setUp(self):
        self._saved = ember_core._reminder_store._reminders[:]
        ember_core._reminder_store._reminders = []
        ember_core._reminder_store._save_locked = lambda: None

    def tearDown(self):
        ember_core._reminder_store._reminders = self._saved

    def run_phrase(self, phrase):
        hit = ember_core._tool_registry.match(phrase)
        self.assertIsNotNone(hit, phrase)
        self.assertEqual(hit[0].name, "set_timer", phrase)
        return hit[0].handler(hit[1])

    def test_phrasings(self):
        for phrase, secs in (("set a timer for 10 minutes", 600), ("start a 5 minute timer for the pasta", 300),
                             ("set a 30 second timer", 30), ("set a timer for an hour", 3600), ("set a timer for half an hour", 1800)):
            ember_core._reminder_store._reminders = []
            self.assertIn("Timer set", self.run_phrase(phrase))
            r = ember_core._reminder_store.list_active()[0]
            delta = (r.fire_at - r.created_at).total_seconds()
            self.assertAlmostEqual(delta, secs, delta=3, msg=phrase)

    def test_cancel_by_label_and_by_kind(self):
        self.run_phrase("start a 5 minute timer for the pasta")
        self.assertIn("Stopped", ember_core._fuzzy_cancel_reminder("pasta timer"))
        self.run_phrase("set a timer for 10 minutes")
        self.assertIn("Stopped", ember_core._fuzzy_cancel_reminder("timer"))


class GroundingTests(unittest.TestCase):
    def test_first_person_questions_trigger_recall(self):
        for q in ("where do i reside?", "who am i", "did i tell you my city", "when was i born", "what did i say about the trip"):
            self.assertTrue(ember_core.needs_recall(q), q)
        self.assertFalse(ember_core.needs_recall("what is the capital of france"))

    def test_empty_recall_tells_model_not_to_guess(self):
        p = ember_core.build_system_prompt(memory_context="", memory_checked=True)
        self.assertIn("nothing relevant was stored", p)
        self.assertNotIn("nothing relevant was stored", ember_core.build_system_prompt(memory_context="- x", memory_checked=True))
        self.assertIn("contact card", ember_core.BASE_PERSONA)        # the rule names the exact invention seen


class AttachmentFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.conv = SimpleNamespace()
        self.calls = []

        def vision(raw, mime, q):
            self.calls.append(q)
            return f"description#{len(self.calls)}"
        self.vision = vision

    def attach(self, name):
        saved, err = ea.save_attachments([{"name": name, "mime": "image/jpeg",
                                           "data": __import__("base64").b64encode(b"\xff\xd8fake").decode()}], self.d)
        self.assertIsNone(err)
        self.conv.attachments = saved

    def test_dispute_re_examines_image_with_the_users_claim(self):
        self.attach("car.jpg")
        ea.augment_prompt("what car is that?", self.conv, self.vision)
        n = len(self.calls)
        out = ea.augment_prompt("i think its a 365", self.conv, self.vision)
        self.assertEqual(len(self.calls), n + 1)
        self.assertIn("365", self.calls[-1])
        self.assertIn("RE-EXAMINATION", out)
        self.assertIn("not your own eyes", out)

    def test_plain_follow_up_does_not_spend_a_vision_call(self):
        self.attach("car.jpg")
        ea.augment_prompt("what car is that?", self.conv, self.vision)
        n = len(self.calls)
        ea.augment_prompt("what colour is it", self.conv, self.vision)
        self.assertEqual(len(self.calls), n)

    def test_earlier_images_survive_a_new_attachment(self):
        self.attach("one.jpg")
        ea.augment_prompt("what car?", self.conv, self.vision)
        self.attach("two.jpg")
        out = ea.augment_prompt("and this?", self.conv, self.vision)
        self.assertIn("description#1", out)
        self.assertIn("description#2", out)
        later = ea.augment_prompt("what was in those images?", self.conv, self.vision)
        self.assertIn("description#1", later)
        self.assertIn("description#2", later)


class TransportReminderTests(unittest.TestCase):
    def test_missed_reminders_are_kept_only_while_nobody_is_connected(self):
        import ember_transport as et
        et._MISSED_REMINDERS.clear(); et._REMINDER_CLIENTS.clear()
        et._remember_missed_reminder("reminder.due", {"id": "a", "message": "x"})
        et._REMINDER_CLIENTS.add("ws-1")
        et._remember_missed_reminder("reminder.due", {"id": "b", "message": "y"})
        got = et._drain_missed_reminders()
        self.assertEqual([g["id"] for g in got], ["a"])
        self.assertEqual(et._drain_missed_reminders(), [])
        et._REMINDER_CLIENTS.clear()


if __name__ == "__main__":
    unittest.main()


class ReminderManagementTests(unittest.TestCase):
    """The exact conversation that failed: the model said 'Done, cancelled' and nothing was cancelled."""

    def setUp(self):
        self.store = ember_core._reminder_store
        self._saved = self.store._reminders[:]
        self.store._reminders = []
        self.store._save_locked = lambda: None
        ember_core._dispatch_context.clear()

    def tearDown(self):
        self.store._reminders = self._saved
        ember_core._dispatch_context.clear()

    def say(self, phrase):
        """Run a phrase the way process_turn does; returns (tool name or None, reply)."""
        intent, _t, payload = ember_core.classify_intent(phrase, [])
        if intent == "action":
            tool, match = payload
            return tool.name, tool.handler(match)
        unrouted = ember_core._unrouted_reminder_reply(phrase) if intent == "chat" else None
        return (intent if not unrouted else "unrouted"), unrouted

    def n(self):
        return len(self.store.list_active())

    def test_compound_duration_and_clean_text(self):
        name, reply = self.say("remind me in 3 hours and 40 minutes to wake up")
        self.assertEqual(name, "create_reminder")
        r = self.store.list_active()[0]
        self.assertEqual(r.message, "wake up")
        self.assertAlmostEqual((r.fire_at - r.created_at).total_seconds(), 13200, delta=5)
        self.say("remind me to take out the trash tonight")
        self.assertIn("take out the trash", [x.message for x in self.store.list_active()])   # was "ake out the trash"

    def test_repeating_the_same_request_does_not_pile_up_duplicates(self):
        for _ in range(3):
            self.say("remind me in 3 hours and 40 minutes to wake up")
        self.assertEqual(self.n(), 1)

    def test_list_phrasings_all_reach_the_tool(self):
        self.say("remind me in 2 hours to call home")
        for p in ("show active reminders", "active reminders", "reminders", "what are my reminders", "do i have any reminders",
                  "list my timers", "what reminders do i have", "show me my active reminders"):
            self.assertEqual(self.say(p)[0], "list_reminders", p)

    def test_cancel_by_words_id_duration_time_and_pronoun(self):
        self.say("remind me to check the stream in 30 minutes")
        self.say("remind me to wake up in 5 hours")
        self.assertEqual(self.say("cancel the 30 minute reminder")[0], "cancel_reminder")
        self.assertEqual([r.message for r in self.store.list_active()], ["wake up"])
        self.say("remind me to check the stream in 30 minutes")
        self.assertIn("Cancelled", self.say("cancel the check stream reminder")[1])
        self.assertIn("Cancelled", self.say("cancel wake up")[1])
        self.assertEqual(self.n(), 0)
        self.say("remind me to call mum in 2 hours")
        rid = self.store.list_active()[0].id
        self.assertIn("Cancelled", self.say(f"cancel {rid}")[1])
        self.say("remind me to call mum in 2 hours")
        self.say("show active reminders")
        self.assertIn("Cancelled", self.say("cancel it")[1])           # NOT "I don't have a specific event in mind"
        self.assertEqual(self.n(), 0)

    def test_duplicates_cancelled_together_and_ambiguity_is_resolvable(self):
        for msg in ("wake up", "call home"):
            self.store.add(msg, __import__("datetime").datetime.now() + __import__("datetime").timedelta(hours=2))
        intent, _t, payload = ember_core.classify_intent("cancel reminder", [])      # singular + two exist: ask, don't wipe
        self.assertEqual(payload[0].name, "cancel_reminder")
        reply = payload[0].handler(payload[1])
        self.assertIn("match", reply)
        self.assertEqual(self.n(), 2)                                    # asking must not cancel anything
        name, reply = self.say("the second one")
        self.assertEqual(name, "resolve_reminder_choice")
        self.assertEqual(self.n(), 1)
        self.store.add("call home", __import__("datetime").datetime.now() + __import__("datetime").timedelta(hours=3))
        name, reply = self.say("cancel it")
        self.assertIn("Which one", reply)
        self.assertEqual(self.n(), 2)
        name, reply = self.say("all of them")
        self.assertEqual(name, "resolve_reminder_choice")
        self.assertEqual(self.n(), 0)

    def test_cancel_all_needs_confirmation_and_only_when_something_exists(self):
        self.say("remind me in 2 hours to call home")
        intent, _t, payload = ember_core.classify_intent("delete active reminders", [])
        self.assertEqual(payload[0].name, "cancel_all_reminders")
        self.assertTrue(payload[0].destructive)
        self.store._reminders = []
        self.assertNotEqual(ember_core.classify_intent("delete active reminders", [])[2][0].name if ember_core.classify_intent("delete active reminders", [])[0] == "action" else "", "cancel_all_reminders")

    def test_a_cancel_that_matches_nothing_says_so_instead_of_pretending(self):
        self.say("remind me in 2 hours to call home")
        name, reply = self.say("cancel the 30 minute reminder")
        self.assertIn("nothing was cancelled", reply)
        self.assertEqual(self.n(), 1)

    def test_meeting_and_music_commands_are_not_stolen(self):
        self.store.add("dentist", __import__("datetime").datetime.now() + __import__("datetime").timedelta(hours=2))
        self.assertEqual(self.say("cancel the dentist meeting")[0], "cancel_calendar_event")
        self.assertNotEqual(self.say("stop the music")[0], "cancel_reminder")
        self.assertEqual(self.n(), 1)

    def test_commands_no_tool_handles_never_reach_the_model(self):
        self.assertIn("nothing was set", self.say("set an alarm for 7am")[1])
        self.assertIn("can't edit", self.say("change my wake up reminder to 8am")[1])
        self.assertIsNone(ember_core._unrouted_reminder_reply("how do reminders work?"))
        self.assertIsNone(ember_core._unrouted_reminder_reply("what's the best way to cancel a reminder?"))
