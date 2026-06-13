import { defineStore } from "pinia";

type RuntimeMode = "historical_replay" | "sandbox" | "shadow" | "production";
type SessionType = "weekday_morning" | "weekday_main" | "weekday_evening" | "weekend";
type SessionPhase =
  | "opening_auction"
  | "continuous_trading"
  | "closing_auction"
  | "break"
  | "dealer_mode"
  | "closed";

export const useShellStore = defineStore("shell", {
  state: () => ({
    runtimeMode: "historical_replay" as RuntimeMode,
    sessionType: "weekday_morning" as SessionType,
    sessionPhase: "closed" as SessionPhase,
  }),
});
