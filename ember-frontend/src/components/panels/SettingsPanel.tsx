import { useState } from "react";
import { Check } from "lucide-react";
import { PanelHost } from "./PanelHost";
import { GuidePanel } from "./GuidePanel";

const VOICES = ["Heart", "Isabella", "Daniel"] as const;
type VoiceName = (typeof VOICES)[number];
const VOICE_STORAGE_KEY = "ember.voicePreference";

interface SettingsPanelProps {
  onClose: () => void;
  onSelectVoice: (voice: string) => void;
}

export function SettingsPanel({ onClose, onSelectVoice }: SettingsPanelProps) {
  const [tab, setTab] = useState<"general" | "guide">("general");
  const [selectedVoice, setSelectedVoice] = useState<VoiceName>(
    () => (localStorage.getItem(VOICE_STORAGE_KEY) as VoiceName) || "Heart",
  );

  const selectVoice = (voice: VoiceName) => {
    setSelectedVoice(voice);
    localStorage.setItem(VOICE_STORAGE_KEY, voice);
    onSelectVoice(voice);
  };

  return (
    <PanelHost title="Settings" onClose={onClose}>
      <div className="mb-4 flex gap-1 rounded-lg border border-border p-0.5">
        {(["general", "guide"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 rounded-md px-3 py-1.5 text-[12.5px] capitalize transition-colors ${
              tab === t ? "bg-surfaceRaised text-textPrimary" : "text-textSecondary hover:text-textPrimary"
            }`}
          >
            {t === "guide" ? "Guide" : "General"}
          </button>
        ))}
      </div>
      {tab === "guide" ? (
        <GuidePanel />
      ) : (
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
      </div>
      )}
    </PanelHost>
  );
}
