"""Pins every claim in the Settings > Guide page (ember-frontend/.../GuidePanel.tsx) to what the code
really does. If a guide example stops working, or a command changes, this fails -- so the guide can't
quietly drift into promising things that don't happen. Phrases here mirror the guide's examples."""
import unittest

import ember_core as c
import ember_voice_text as vt


def route(msg):
    intent, _topic, payload = c.classify_intent(msg, [])
    if intent == "action":
        return payload[0].name
    if intent == "command":
        return payload.intent
    return intent


class TextCommandClaims(unittest.TestCase):
    def setUp(self):
        # the guide's calendar examples describe Google Calendar being connected; without it they are kept locally
        from unittest import mock
        p = mock.patch.object(c, "_google_calendar_configured", lambda: True)
        p.start()
        self.addCleanup(p.stop)

    CASES = {
        "remind me to submit the report tomorrow at 6 PM": "create_reminder",
        "I have a trip in 5 days": "note_life_event",
        "my physics practical is on 3rd November": "note_life_event",
        "sem exams from the 12th to the 24th of December": "note_life_event",
        "add scooty service on the 31st": "note_life_event",
        "schedule a part change for the 31st of January 2027": "note_life_event",
        "what's coming up?": "list_life_events",
        "what are my plans": "list_life_events",
        "upcoming exams": "list_life_events",
        "add to my profile: I'm in 5th semester, mechanical": "profile_add",
        "show my profile": "profile_show",
        "remind me in 2 hours to call home": "create_reminder",
        "remind me every Sunday to review the week": "create_reminder",
        "set a timer for 10 minutes": "set_timer",
        "start a 5 minute timer for the pasta": "set_timer",
        "set a 30 second timer": "set_timer",
        "show active reminders": "list_reminders",
        "what are my reminders?": "list_reminders",
        "do i have any reminders": "list_reminders",
        "reminders": "list_reminders",
        "cancel the 30 minute reminder": "cancel_reminder",
        "cancel the 5pm one reminder": "cancel_reminder",
        "cancel reminder abc12345": "cancel_reminder",
        "move the budget file to Documents": "move_file",
        "rename the budget file to final budget": "rename_file",
        "delete the old export file": "delete_file",
        "undo that": "undo_file_op",
        "open notes.txt": "open_file",
        "open Spotify": "launch_app",
        "show budget.xlsx in explorer": "reveal_file",
        "list files in C:\\Users": "list_directory",
        "read file at C:\\x.txt": "read_file",
        "allow access to C:\\Users": "allow_path",
        "run script at C:\\x.py": "run_script",
        "what do you remember about my car?": "INSPECT_MEMORY",
        "search memory for where I live": "INSPECT_MEMORY",
        "update my city to Pune": "UPDATE_MEMORY",
        "forget that I'm watching a show": "forget_memory",
        "clear my memory": "clear_memory",
        "search for the latest news": "search",
        "find out when the next race starts": "search",
        "what time is the race this evening?": "search",
        "are you sure?": "verify",
        "recheck that": "verify",
        "what's on my calendar today?": "check_calendar",
        "schedule dentist for tomorrow at 4pm": "add_calendar_event",
        "reschedule the team meeting to Wednesday": "reschedule_calendar_event",
        "cancel the dentist meeting": "cancel_calendar_event",
        "play Hotel California on Spotify": "play_spotify",
        "what's on my screen?": "analyze_screen",
        "export that as a PDF": "generate_pdf",
        "what time is it?": "GET_TIME",
        "what's the date?": "GET_DATE",
        "what day is it?": "GET_DAY",
        "ember, status": "STATUS",
        "diagnose yourself": "DIAGNOSE",
        "clear context": "CLEAR_CONTEXT",
        "new chat": "NEW_CONVERSATION",
        "let's start fresh": "NEW_CONVERSATION",
        "add Mentalist to your vocabulary": "teach_speech_term",
        "when you hear max will stop, I mean Verstappen": "teach_speech_correction",
    }

    def test_every_guide_example_routes_where_the_guide_says(self):
        wrong = {m: (want, route(m)) for m, want in self.CASES.items() if route(m) != want}
        self.assertEqual(wrong, {}, "guide example does not do what the guide says")

    def test_race_question_is_not_deflected_and_search_query_is_clean(self):
        self.assertEqual(c.clean_search_query("search for what time is the race this evening."),
                         "what time is the race this evening")
        self.assertEqual(c.clean_search_query("Ember, look up the weather in Pune"), "the weather in Pune")
        self.assertEqual(c.clean_search_query("what time is the race"), "what time is the race")
        self.assertNotEqual(route("what time is my meeting this evening"), "search")   # "my" = calendar, not the web


class VoicePhraseClaims(unittest.TestCase):
    def test_voice_mode_phrases(self):
        for p in ("voice mode", "let's talk", "start voice mode"):
            self.assertEqual(vt.parse_voice_mode_command(p), "on", p)
        for p in ("stop listening", "go to sleep", "exit voice mode", "voice mode off", "we're done"):
            self.assertEqual(vt.parse_voice_mode_command(p), "off", p)

    def test_stop_and_close_and_presence(self):
        for p in ("stop", "quiet", "shut up", "enough", "never mind", "hold on", "wait"):
            self.assertTrue(vt.is_stop_command(p), p)
        self.assertFalse(vt.is_stop_command("stop the music"))
        for p in ("thank you", "that's all", "thanks"):
            self.assertTrue(vt.is_closing_phrase(p), p)
        for p in ("ember, you there?", "are you there", "can you hear me"):
            self.assertTrue(vt.is_presence_check(p), p)


class VoiceFlowClaims(unittest.TestCase):
    """The real session logic, with fake audio engines (see tests/test_voice_session.py's Rig)."""

    def setUp(self):
        from tests.test_voice_session import Rig
        self.r = Rig()

    def tearDown(self):
        self.r.close()

    def test_voice_mode_needs_the_name_first_then_needs_no_wake_word(self):
        r = self.r
        r.say("voice mode")                                   # no wake word, not awake: ignored
        self.assertFalse(r.v._voice_mode)
        r.say("Hey Ember, voice mode")
        self.assertTrue(r.v._voice_mode)
        r.advance(120)                                        # no timeout in voice mode
        r.say("what is the capital of France")
        self.assertEqual(r.submitted[-1], "what is the capital of France")
        r.say("stop listening")
        self.assertFalse(r.v._voice_mode)

    def test_wake_then_command_in_one_breath(self):
        self.r.say("Ember, what's the capital of France?")
        self.assertEqual(len(self.r.submitted), 1)

    def test_follow_up_window_then_closes(self):
        r = self.r
        r.say("Hey Ember, what's the capital of France")
        r.reply("Paris, sir.", final="Paris, sir.")
        r.wait(lambda: r.of("audio_chunk"))
        r.advance(5.0)                                         # her reply has finished playing
        r.say("and Spain")                                     # inside the window: no wake word needed
        self.assertEqual(len(r.submitted), 2)
        r.reply("Madrid, sir.", final="Madrid, sir.")
        r.wait(lambda: len(r.of("audio_chunk")) >= 2)
        r.advance(60)
        r.say("and Italy")                                     # window over
        self.assertEqual(len(r.submitted), 2)


if __name__ == "__main__":
    unittest.main()
