"""Laptop shutdown: the real tool classifier + confirmation gate, with the Windows call stubbed out."""
import os
import tempfile
from types import SimpleNamespace

import pytest

import ember_core as c
import ember_reminders as er
from ember_tools import power
from ember_voice_text import confirmation_prompt


class Gate:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def request_confirmation(self, tool_name, args, conversation=None, **kw):
        self.asked.append((tool_name, args))
        return self.answer


@pytest.fixture()
def rig(monkeypatch):
    calls = []
    codes = {"code": 0}

    def fake_run(args, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=codes["code"], stdout="", stderr="")

    d = tempfile.mkdtemp()
    monkeypatch.setattr(power, "_run", fake_run)
    monkeypatch.setattr(power, "_is_windows", lambda: True)
    monkeypatch.setattr(c, "_reminder_store", er.ReminderStore(path=os.path.join(d, "r.json"), notify_fn=lambda *a, **k: None))
    gate = Gate(True)
    monkeypatch.setattr(c, "_confirm_gate", gate)
    return SimpleNamespace(calls=calls, codes=codes, gate=gate, core=c)


def run(msg):
    a = c._classify_action(msg)
    return (a[0].name, c._dispatch_action(a, None)) if a else (None, None)


@pytest.mark.parametrize("msg", [
    "shut down the laptop", "Shut down my computer", "turn off the pc", "shut the laptop down",
    "power off the computer, ember", "please shut down this laptop", "switch off the machine",
])
def test_shutdown_phrasings_are_recognised_and_confirmed(rig, msg):
    name, reply = run(msg)
    assert name == "shutdown_laptop"
    assert rig.gate.asked and rig.gate.asked[0][0] == "shutdown_laptop"
    assert rig.calls == [["shutdown", "/s", "/t", "30", "/c", rig.calls[0][5]]]
    assert "/f" not in rig.calls[0]                      # never force-close apps
    assert "30 seconds" in reply and "cancel the shutdown" in reply


def test_delay_is_honoured_and_floored(rig):
    run("shut down the laptop in 10 minutes")
    assert rig.calls[-1][:4] == ["shutdown", "/s", "/t", "600"]
    run("shut down the computer in 3 seconds")
    assert rig.calls[-1][3] == "10"                      # never shorter than the cancel window
    name, reply = run("turn off the laptop in 48 hours")
    assert name == "shutdown_laptop" and "24 hours" in reply and len(rig.calls) == 2


def test_denied_confirmation_runs_nothing(rig):
    rig.gate.answer = False
    name, reply = run("shut down the laptop")
    assert name == "shutdown_laptop" and rig.calls == [] and "not confirmed" in reply


@pytest.mark.parametrize("msg", [
    "shut down ember", "shut up", "turn off the computer screen", "turn off the laptop's backlight",
    "how do I shut down my laptop", "what happens when I shut down the laptop",
    "turn off the pc speakers", "why does my laptop shut down by itself",
])
def test_not_a_shutdown_command(rig, msg):
    name, _ = run(msg)
    assert name not in ("shutdown_laptop", "shutdown_later_unsupported")
    assert rig.calls == [] and rig.gate.asked == []


def test_a_reminder_about_shutting_down_stays_a_reminder(rig):
    name, _ = run("remind me to shut down the laptop at 10pm")
    assert name not in ("shutdown_laptop", "shutdown_later_unsupported")
    assert rig.calls == []


@pytest.mark.parametrize("msg", [
    "shut down the laptop at 11pm", "shut down the computer tonight", "turn off the pc after the race",
    "shut down the laptop tomorrow",
])
def test_clock_time_or_condition_is_declined_honestly(rig, msg):
    name, reply = run(msg)
    assert name == "shutdown_later_unsupported" and "Nothing was scheduled" in reply
    assert rig.calls == [] and rig.gate.asked == []      # no prompt, no OS call


def test_cancel(rig):
    name, reply = run("cancel the shutdown")
    assert name == "cancel_shutdown" and reply == "Shutdown cancelled, sir." and rig.calls[-1] == ["shutdown", "/a"]
    assert run("stop the laptop shutdown")[0] == "cancel_shutdown"
    assert run("don't shut down")[0] == "cancel_shutdown"
    rig.codes["code"] = 1116
    assert "No shutdown is pending" in run("abort the shutdown")[1]
    assert rig.gate.asked == []                          # cancelling never needs a confirmation


def test_already_scheduled_and_refused(rig):
    rig.codes["code"] = 1190
    assert "already counting down" in run("shut down the laptop")[1]
    rig.codes["code"] = 5
    assert "refused" in run("shut down the laptop")[1]


def test_active_reminders_are_mentioned(rig):
    from datetime import datetime, timedelta
    c._reminder_store.add("call mum", datetime.now() + timedelta(hours=2))
    _, reply = run("shut down the laptop")
    assert "1 active reminder;" in reply and "won't fire" in reply


def test_not_windows_is_honest_and_does_nothing(rig, monkeypatch):
    monkeypatch.setattr(power, "_is_windows", lambda: False)
    _, reply = run("shut down the laptop")
    assert "only set up for Windows" in reply and rig.calls == []


def test_missing_binary_and_timeout_do_not_raise(rig, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(power, "_run", boom)
    assert "couldn't find" in run("shut down the laptop")[1]


def test_spoken_prompt():
    assert confirmation_prompt("shutdown_laptop", {}).startswith("Shut the laptop down")


def test_spoken_duration():
    assert power.spoken_duration(30) == "30 seconds" and power.spoken_duration(600) == "10 minutes"
    assert power.spoken_duration(5400) == "1 hour 30 minutes" and power.spoken_duration(1) == "1 second"


# ---- "terminate": no confirmation, whole-utterance only -------------------------------------------------------
@pytest.mark.parametrize("msg", ["terminate", "Terminate.", "hey ember, terminate", "Hey Ember, terminate.", "ember terminate now",
                                 "terminate, sir", "please terminate"])
def test_terminate_shuts_down_without_asking(rig, msg):
    name, reply = run(msg)
    assert name == "terminate_laptop" and rig.gate.asked == []
    assert rig.calls[0][:4] == ["shutdown", "/s", "/t", "30"] and "/f" not in rig.calls[0] and "30 seconds" in reply


def test_terminate_with_a_delay(rig):
    name, reply = run("hey ember, terminate in 10 minutes")
    assert name == "terminate_laptop" and rig.calls[0][3] == "600" and "10 minutes" in reply and rig.gate.asked == []
    run("terminate in an hour and a half")
    assert rig.calls[1][3] == "5400"


@pytest.mark.parametrize("msg", ["terminate the script", "terminate that process", "how do I terminate a process",
                                 "terminate the contract in 10 minutes", "do not terminate", "why did you terminate"])
def test_terminate_only_as_the_whole_command(rig, msg):
    name, _ = run(msg)
    assert name != "terminate_laptop" and rig.calls == []


def test_cancel_after_terminate(rig):
    run("terminate")
    assert run("cancel the shutdown")[1] == "Shutdown cancelled, sir." and rig.calls[-1] == ["shutdown", "/a"]


# ---- "lights out": sleep, not shutdown -----------------------------------------------------------------------
@pytest.fixture()
def sleeper(rig, monkeypatch):
    launched = []
    monkeypatch.setattr(power, "_popen", lambda args, **kw: launched.append(list(args)))
    return launched


@pytest.mark.parametrize("msg", ["lights out", "Lights out.", "hey ember, lights out", "Ember lights out please", "light out, sir"])
def test_lights_out_sleeps_without_asking_and_never_shuts_down(rig, sleeper, msg):
    name, reply = run(msg)
    assert name == "lights_out" and reply.startswith("Lights out, sir.")
    assert rig.gate.asked == [] and rig.calls == []                 # no confirmation, and shutdown.exe never called
    assert len(sleeper) == 1 and sleeper[0][0] == "powershell" and "Suspend" in sleeper[0][-1]
    assert "Hibernate" not in sleeper[0][-1] and "rundll32" not in " ".join(sleeper[0])


@pytest.mark.parametrize("msg", ["turn the lights out in the hall", "lights out in the living room", "the lights out there are broken",
                                 "why are the lights out"])
def test_lights_out_only_as_the_whole_command(rig, sleeper, msg):
    assert run(msg)[0] != "lights_out" and sleeper == []


def test_lights_out_mentions_reminders_and_handles_failures(rig, sleeper, monkeypatch):
    from datetime import datetime, timedelta
    c._reminder_store.add("call mum", datetime.now() + timedelta(hours=2))
    assert "1 active reminder;" in run("lights out")[1]
    monkeypatch.setattr(power, "_is_windows", lambda: False)
    assert "only set up for Windows" in run("lights out")[1] and len(sleeper) == 1
    monkeypatch.setattr(power, "_is_windows", lambda: True)
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(power, "_popen", boom)
    assert "couldn't find PowerShell" in run("lights out")[1]
