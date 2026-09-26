import { ShieldAlert } from "lucide-react";
import type { PendingConfirmation } from "../types";

interface ConfirmationDialogProps {
  confirmation: PendingConfirmation;
  onRespond: (approved: boolean) => void;
}

export function ConfirmationDialog({ confirmation, onRespond }: ConfirmationDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-surface p-5 animate-riseIn">
        <div className="mb-3 flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-statusLocal/15 text-statusLocal">
            <ShieldAlert size={16} />
          </div>
          <h2 className="text-[14.5px] font-medium text-textPrimary">Confirmation needed</h2>
        </div>
        <p className="text-[13.5px] text-textSecondary">
          Ember wants to run <span className="font-mono text-textPrimary">{confirmation.toolName}</span>. This action
          is destructive or irreversible — approve it?
        </p>
        {confirmation.args && Object.keys(confirmation.args).length > 0 && (
          <pre className="mt-3 max-h-28 overflow-auto rounded-lg border border-border bg-base px-3 py-2 font-mono text-[11.5px] text-textMuted">
            {JSON.stringify(confirmation.args, null, 2)}
          </pre>
        )}
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => onRespond(false)}
            className="flex-1 rounded-lg border border-border py-2 text-[13px] text-textPrimary hover:bg-surfaceRaised"
          >
            Deny
          </button>
          <button
            onClick={() => onRespond(true)}
            className="flex-1 rounded-lg bg-ember py-2 text-[13px] font-medium text-base"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
