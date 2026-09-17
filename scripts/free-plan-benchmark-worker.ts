// Local-only instrumentation. Never included in the deployed Worker entry point.
import worker from "../cloudflare/control-plane/src/index";
import { Workspace as BaseWorkspace } from "../cloudflare/control-plane/src/workspace";
import { livingRead, livingWrite } from "../cloudflare/control-plane/src/living";

export default worker;
export class Workspace extends BaseWorkspace {
  async invoke(method: string, envelope: string, principal: string): Promise<string> {
    const state = (this as any).state;
    const sql = this.ctx.storage.sql;
    const groups: Record<string, { calls: number; rowsRead: number; rowsWritten: number }> = {};
    const before = sql.exec("SELECT total_changes() AS n").one().n as number;
    state.sql = new Proxy(sql, {
      get(target, name) {
        if (name !== "exec") return Reflect.get(target, name, target);
        return (query: string, ...bindings: any[]) => {
          const cursor = target.exec(query, ...bindings);
          const key = query.trim().match(/^(?:INSERT(?: OR \w+)? INTO|DELETE FROM|UPDATE)\s+(\w+)/i);
          const label = key ? `${query.trim().split(/\s/)[0]} ${key[1]}` : "queries";
          const group = groups[label] ??= { calls: 0, rowsRead: 0, rowsWritten: 0 };
          group.calls++;
          let read = 0, written = 0;
          const capture = () => {
            group.rowsRead += cursor.rowsRead - read;
            group.rowsWritten += cursor.rowsWritten - written;
            read = cursor.rowsRead; written = cursor.rowsWritten;
          };
          capture();
          const wrap = (value: any): any => new Proxy(value, {
            get(object, property) {
              const member = Reflect.get(object, property, object);
              if (typeof member !== "function") return member;
              return (...args: any[]) => {
                try {
                  const result = member.apply(object, args);
                  return property === "raw" || property === Symbol.iterator ? wrap(result) : result;
                } finally { capture(); }
              };
            },
          });
          return wrap(cursor);
        };
      },
    });
    try {
      let response;
      if (method === "benchmark_counter_calibration") {
        sql.exec("CREATE TABLE counter_probe(id TEXT PRIMARY KEY, value TEXT)");
        sql.exec("CREATE INDEX counter_probe_value ON counter_probe(value)");
        const samples = [];
        for (const query of ["INSERT INTO counter_probe VALUES('a','v')",
          "UPDATE counter_probe SET value='w' WHERE id='a'", "DELETE FROM counter_probe WHERE id='a'"]) {
          const before = sql.exec("SELECT total_changes() AS n").one().n as number;
          const cursor = sql.exec(query); cursor.toArray();
          samples.push({ operation: query.split(" ")[0], rowsRead: cursor.rowsRead, rowsWritten: cursor.rowsWritten,
            logical_changes: (sql.exec("SELECT total_changes() AS n").one().n as number) - before });
        }
        let rollbackWrites = 0;
        try { this.ctx.storage.transactionSync(() => {
          rollbackWrites = sql.exec("INSERT INTO counter_probe VALUES('a','v')").rowsWritten;
          throw new Error("intentional local rollback");
        }); } catch { /* Disposable counter probe intentionally rolls back. */ }
        response = { status: 200, body: { samples, rollbackWrites,
          rows_after_rollback: sql.exec("SELECT count(*) AS n FROM counter_probe").one().n } };
      } else if (method === "benchmark_history") {
        const { start, end } = JSON.parse(envelope);
        this.ctx.storage.transactionSync(() => {
          for (let revision = start; revision <= end; revision++) {
            state.sql.exec("UPDATE fact_rows SET valid_to_sequence=? WHERE graph_id='history' AND valid_to_sequence IS NULL", revision - 1);
            for (let id = 0; id < 100; id++) state.sql.exec(
              "INSERT INTO fact_rows VALUES('history','event',?,NULL,?,? ,?, ?,NULL)",
              String(id), id, String(revision), JSON.stringify({ synthetic: true, padding: 'x'.repeat(512) }), revision);
          }
        });
        response = { status: 200, body: { retained: sql.exec("SELECT count(*) AS n FROM fact_rows").one().n,
          current: sql.exec("SELECT count(*) AS n FROM fact_rows WHERE valid_to_sequence IS NULL").one().n } };
      } else if (method === "benchmark_living") {
        const { count, start = 1 } = JSON.parse(envelope);
        this.ctx.storage.transactionSync(() => {
          for (let i = start; i < start + count; i++) livingWrite(state, "ct_collector_heartbeat", {
            agent_id: "00000000-0000-0000-0000-000000000003",
            agent_instance_id: "00000000-0000-0000-0000-000000000004",
            observation_sequence: i, lease_seconds: 60,
          });
        });
        response = { status: 200, body: {} };
      } else if (method === "benchmark_living_read") {
        try {
          const result = livingRead(state, { calls: [{ method: "living.sessions", params: { limit: 1 } }] });
          response = { status: 200, body: { changes: result.results[0].result.changes.length } };
        } catch (error: any) { response = { status: error.status, body: { error: error.code } }; }
      } else response = JSON.parse(await super.invoke(method, envelope, principal));
      const changes = (sql.exec("SELECT total_changes() AS n").one().n as number) - before;
      response.body.__benchmark = { groups, logical_changes: changes, database_bytes: sql.databaseSize };
      return JSON.stringify(response);
    } finally { state.sql = sql; }
  }
}
