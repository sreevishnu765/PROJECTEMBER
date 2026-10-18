import { useState } from "react";
import { Check } from "lucide-react";
import { PanelHost } from "./PanelHost";
import { GuidePanel } from "./GuidePanel";

const VOICES = ["Heart", "Isabella", "Daniel"] as const;
type VoiceName = (typeof VOICES)[number];
const VOICE_STORAGE_KEY = "ember.voicePreference";
const SPEED_STORAGE_KEY = "ember.voiceSpeed";
const SPEED_MIN = 0.7;
const SPEED_MAX = 1.5;
const SPEED_STEP = 0.05;

function loadSpeed(): number {
  const v = parseFloat(localStorage.getItem(SPEED_STORAGE_KEY) ?? "");
  return Number.isFinite(v) ? Math.min(SPEED_MAX, Math.max(SPEED_MIN, v)) : 1;
}

interface SettingsPanelProps {
  onClose: () => void;
  onSelectVoice: (voice: string) => void;
  onSelectSpeed: (speed: number) => void;
}

export function SettingsPanel({ onClose, onSelectVoice, onSelectSpeed }: SettingsPanelProps) {
  const [tab, setTab] = useState<"general" | "guide">("general");
  const [selectedVoice, setSelectedVoice] = useState<VoiceName>(
    () => (localStorage.getItem(VOICE_STORAGE_KEY) as VoiceName) || "Heart",
  );

  const [speed, setSpeed] = useState<number>(loadSpeed);

  const changeSpeed = (value: number) => {
    const v = Math.round(value * 100) / 100;
    setSpeed(v);
    localStorage.setItem(SPEED_STORAGE_KEY, String(v));
    onSelectSpeed(v);
  };

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
        </div>
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Speaking speed</h3>
            <div className="flex items-center gap-2">
              <span className="text-[12.5px] tabular-nums text-textPrimary">{speed.toFixed(2)}×</span>
              {speed !== 1 && (
                <button
                  onClick={() => changeSpeed(1)}
                  className="text-[11.5px] text-textMuted transition-colors hover:text-textPrimary"
                >
                  Reset
                </button>
              )}
            </div>
          </div>
          <input
            type="range"
            min={SPEED_MIN}
            max={SPEED_MAX}
            step={SPEED_STEP}
            value={speed}
            onChange={(e) => changeSpeed(parseFloat(e.target.value))}
            aria-label="Speaking speed"
            className="ember-range w-full cursor-pointer"
            style={{ ["--fill" as string]: `${((speed - SPEED_MIN) / (SPEED_MAX - SPEED_MIN)) * 100}%` }}
          />
          <div className="mt-1.5 flex justify-between text-[11px] text-textMuted">
            <span>Slower</span>
            <span>Normal</span>
            <span>Faster</span>
          </div>
          <p className="mt-2 text-[12px] text-textMuted">Applies to the next thing Ember says.</p>
        </div>
      </div>
      )}
    </PanelHost>
  );
}
