import { useState } from "react";
import { Check } from "lucide-react";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

const VOICES = ["Heart", "Isabella", "Daniel"] as const;
type VoiceName = (typeof VOICES)[number];
const VOICE_STORAGE_KEY = "ember.voicePreference";

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [selectedVoice, setSelectedVoice] = useState<VoiceName>(
    () => (localStorage.getItem(VOICE_STORAGE_KEY) as VoiceName) || "Heart",
  );

  const selectVoice = (voice: VoiceName) => {
    setSelectedVoice(voice);
    localStorage.setItem(VOICE_STORAGE_KEY, voice);
  };

  return (
    <PanelHost title="Settings" onClose={onClose}>
      <div className="space-y-4">
        <div>
          <h3 className="mb-2 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Voice</h3>
          <div className="flex gap-2">
            {VOICES.map((voice) => (
              <button
                key={voice}
                onClick={() => selectVoice(voice)}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-[13px] transition-colors ${
                  selectedVoice === voice
                    ? "border-ember bg-ember/10 text-textPrimary"
                    : "border-border text-textSecondary hover:border-borderLight"
                }`}
              >
                {selectedVoice === voice && <Check size={13} className="text-ember" />}
                {voice}
              </button>
            ))}
          </div>
          <p className="mt-2 text-[12px] text-textMuted">
            Speed is preset per voice in code — no speed control by design.
          </p>
        </div>

        <PlaceholderNotice>
          This selection isn't connected to anything yet — Ember doesn't
          speak until the voice pass is built. Your choice is just
          remembered here for when it does.
        </PlaceholderNotice>
      </div>
    </PanelHost>
  );
}
