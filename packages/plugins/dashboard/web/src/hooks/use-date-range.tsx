import * as React from "react";

const STORAGE_KEY = "ct-date-range";
const DEFAULT_DAYS = 7;
export const DATE_RANGE_PRESETS = [1, 7, 30, 90] as const;
export type DateRange = (typeof DATE_RANGE_PRESETS)[number];

function isValidRange(value: unknown): value is DateRange {
  return typeof value === "number" && (DATE_RANGE_PRESETS as readonly number[]).includes(value);
}

type Ctx = { days: DateRange; setDays: (next: DateRange) => void };

const DateRangeContext = React.createContext<Ctx | null>(null);

export function DateRangeProvider({ children }: { children: React.ReactNode }) {
  const [days, setDaysState] = React.useState<DateRange>(() => {
    if (typeof window === "undefined") return DEFAULT_DAYS;
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw == null ? NaN : Number(raw);
    return isValidRange(parsed) ? parsed : DEFAULT_DAYS;
  });

  const setDays = React.useCallback((next: DateRange) => {
    setDaysState(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, String(next));
    }
  }, []);

  const value = React.useMemo(() => ({ days, setDays }), [days, setDays]);
  return <DateRangeContext.Provider value={value}>{children}</DateRangeContext.Provider>;
}

export function useDateRange(): Ctx {
  const ctx = React.useContext(DateRangeContext);
  if (!ctx) throw new Error("useDateRange must be used within DateRangeProvider");
  return ctx;
}
