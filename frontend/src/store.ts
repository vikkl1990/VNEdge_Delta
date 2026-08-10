// Zustand for UI chrome only (per the roadmap) — server state lives in
// TanStack Query, never here. This holds just the command-palette open flag.
import { create } from "zustand";

interface UiState {
  paletteOpen: boolean;
  setPalette: (open: boolean) => void;
  togglePalette: () => void;
}

export const useUi = create<UiState>((set) => ({
  paletteOpen: false,
  setPalette: (open) => set({ paletteOpen: open }),
  togglePalette: () => set((s) => ({ paletteOpen: !s.paletteOpen })),
}));
