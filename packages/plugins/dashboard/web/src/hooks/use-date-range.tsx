// The dashboard is a single-window product: every page reads the incremental
// store's trailing 7-day window.
export const DASHBOARD_WINDOW_DAYS = 7;

export function useDateRange(): { days: number } {
  return { days: DASHBOARD_WINDOW_DAYS };
}
