import type { HourlyDensity, ProjectTrend, ProjectSlice } from "@/api";

export function generateSampleHourlyDensity(
  projects: ProjectSlice[],
): HourlyDensity[] {
  const hours = Array.from({ length: 24 }, (_, i) => i);
  const weights = [
    0.01, 0.005, 0.003, 0.002, 0.002, 0.01, 0.03, 0.08, 0.12, 0.18, 0.22,
    0.15, 0.1, 0.14, 0.2, 0.25, 0.22, 0.16, 0.12, 0.14, 0.18, 0.15, 0.06,
    0.02,
  ];
  const totalSeconds = projects.reduce((s, p) => s + p.execution_seconds, 0);

  return hours.map((hour) => {
    const base = weights[hour] * totalSeconds;
    const jitter = 0.8 + Math.random() * 0.4;
    const density = Math.round(base * jitter);
    const by_project: Record<string, number> = {};
    for (const p of projects) {
      const share = totalSeconds > 0 ? p.execution_seconds / totalSeconds : 0;
      const pJitter = 0.7 + Math.random() * 0.6;
      by_project[p.project_name] = Math.round(density * share * pJitter);
    }
    return { hour, density, by_project };
  });
}

export function generateSampleProjectTrend(
  projects: ProjectSlice[],
): ProjectTrend[] {
  const now = new Date();
  const days = 90;

  return projects.map((p) => {
    const trend: { date: string; seconds: number }[] = [];
    for (let i = days; i >= 0; i--) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      const dateStr = d.toISOString().slice(0, 10);
      const hasActivity = Math.random() > 0.4;
      const seconds = hasActivity
        ? Math.round((p.execution_seconds / days) * (0.3 + Math.random() * 2.5))
        : 0;
      trend.push({ date: dateStr, seconds });
    }
    return { project_name: p.project_name, days: trend };
  });
}
