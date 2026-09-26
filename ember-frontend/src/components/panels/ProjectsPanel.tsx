import { PanelHost, PlaceholderNotice } from "./PanelHost";

export function ProjectsPanel({ onClose }: { onClose: () => void }) {
  return (
    <PanelHost title="Projects" onClose={onClose}>
      <PlaceholderNotice>
        Projects as real, persistent workspaces (scoped conversations, files,
        and memory) haven't been built yet — Ember's memory store has a loose
        "project" tag today, but nothing wires it up to a proper Projects
        view. Coming in a later pass.
      </PlaceholderNotice>
    </PanelHost>
  );
}
