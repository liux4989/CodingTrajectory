import { Json, requireThat, State } from "./shared";

const TABLES = ["fact_rows", "fact_schema", "staged_fact_rows", "staged_fact_items",
  "staged_fact_generations", "validated_fact_graphs"] as const;

/** Read-only inventory; names are fixed, never supplied by a caller. */
export function legacyFactTables(state: State): Json {
  const existing = new Set(state.sql.exec<{ name: string }>(
    "SELECT name FROM sqlite_master WHERE type='table'",
  ).toArray().map(row => row.name));
  const tables: Record<string, number> = {};
  for (const table of TABLES) {
    if (existing.has(table)) tables[table] = state.sql.exec<{ count: number }>(
      `SELECT COUNT(*) AS count FROM ${table}`,
    ).one().count;
  }
  const publications = state.sql.exec<{ count: number }>(
    "SELECT COUNT(*) AS count FROM records WHERE kind IN ('graph_publication','publisher')",
  ).one().count;
  return { tables, legacy_publication_records: publications };
}

/** Deployment-gated schema migration. Caller must run inside transactionSync. */
export function dropEmptyLegacyFactTables(state: State): void {
  const inventory = legacyFactTables(state);
  requireThat(inventory.legacy_publication_records === 0, "legacy_cleanup_data_present", 409);
  for (const [table, count] of Object.entries(inventory.tables)) {
    if (table === "fact_schema") {
      // The sole expected non-data row is the retired schema version marker.
      const rows = state.sql.exec<{ id: number; version: number }>(
        "SELECT id,version FROM fact_schema",
      ).toArray();
      requireThat(rows.length === 0 || (rows.length === 1 && rows[0].id === 1
        && rows[0].version === 1), "legacy_cleanup_schema_unexpected", 409);
    } else requireThat(count === 0, "legacy_cleanup_data_present", 409);
  }
  // Validate the complete inventory before any drop. Associated indexes are
  // dropped by SQLite; shared records, sequences and artifact tables are kept.
  for (const table of TABLES) state.sql.exec(`DROP TABLE IF EXISTS ${table}`);
}
