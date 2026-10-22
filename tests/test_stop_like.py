"""Stop phrases said over Ember's own voice, and the new acoustic "shut up" keyword."""
import pytest

from ember_voice_text import is_stop_like, is_stop_command
import ember_voice as ev


@pytest.mark.parametrize("text", [
    "shut up", "Stop.", "Ember, stop", "hey ember shut up", "stop it now", "stop talking", "be quiet", "that's enough",
    "and then the race shut up", "the driver is leading stop", "Shut up!", "enough",
])
def test_these_are_stops(text):
    assert is_stop_like(text)


@pytest.mark.parametrize("text", [
    "stop the music", "stop the AQI forecasting system", "don't stop the music please", "I think we should stop at the next shop and get food",
    "what does shut up mean in french", "cancel my reminder for tomorrow morning", "quiet hours start at ten", "",
])
def test_these_are_not(text):
    assert not is_stop_like(text)


def test_strict_check_is_unchanged():
    assert is_stop_command("shut up") and not is_stop_command("and then shut up")


def test_shut_up_keyword_pieces_match_the_models_own_bpe():
    # pieces produced by SentencePiece-encoding "SHUT UP" with the kws-zipformer-gigaspeech bpe.model
    assert ev.KWS_KEYWORDS["shut_up"] == "\u2581S H U T \u2581UP"
    assert "shut_up" in ev.KWS_STOP_NAMES and "shut_up" in ev.KWS_STRONG and "stop" in ev.KWS_STOP_NAMES
