import { makeColors } from "./make-colors.js"

// The logo palette: orange #ff6905, amber #ff7e07 and gold #fbbc1d on navy #17212a. One mode, since the scene is a night sky.
// Surfaces step up gently from the navy and the status and git colors are lifted, so every pair passes the contrast check.
export const { theme, problems } = makeColors({
  label: "Omega",
  roles: {
    accent: "#ff6905",
    danger: { seed: "#ff6883", fillShade: 400 },
    success: { seed: "#7bd88f", stops: { 400: "#3fbf68" } },
    warning: "#ff7e07",
    pro: "#fbbc1d",
  },
  only: "dark",
  dark: {
    gray: {
      anchors: {
        "surface-0": "#17212a",
        "surface-1": "#1b252e",
        "surface-2": "#1f2932",
        "surface-3": "#232d36",
        "text-primary": "#f4f6f8",
        "text-secondary": "#c5c8cb",
        "text-muted": "#a2a6aa",
      },
    },
    git: {
      added: "#8fe3a1",
      removed: "#ff9aab",
      modified: "#fbbc1d",
      merged: "#c7a8ff",
      closed: "#ff9aab",
      conflicting: "#ffa45c",
      draft: "#b4bac0",
    },
  },
})
