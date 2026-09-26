const $ = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat("en-US").format(value);
const compact = (value) =>
  value >= 1000 ? `${(value / 1000).toFixed(1)}K` : number(value);
const duration = (seconds) =>
  seconds >= 60
    ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
    : `${Math.round(seconds)}s`;
const categories = ["Bash", "Read", "Write", "Edit", "Other"];
const toolTypes = {
  Bash: "Bash",
  bash: "Bash",
  shell_command: "Bash",
  exec_command: "Bash",
  run_shell_command: "Bash",
  shell: "Bash",
  write_stdin: "Bash",
  Read: "Read",
  read: "Read",
  View: "Read",
  read_file: "Read",
  read_many_files: "Read",
  Write: "Write",
  write: "Write",
  write_file: "Write",
  create_file: "Write",
  Edit: "Edit",
  edit: "Edit",
  MultiEdit: "Edit",
  replace: "Edit",
  apply_patch: "Edit",
  edit_file: "Edit",
};
const group = (item) => toolTypes[item.tool_name] || "Other";
const visibleTokens = (item) =>
  (item.token_attribution?.tool_input_tokens || 0) +
  (item.token_attribution?.tool_output_tokens || 0);
const shortId = (id) => id?.slice(0, 8) || "—";
const node = (tag, className, value) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = String(value);
  return element;
};
const empty = (text) => node("p", "empty", text);
const sessionId = (row) => row.root_session_id;
const nameOf = (row) => row.title || `Session ${shortId(sessionId(row))}`;
const sessions = [];
const cache = new Map();
let total = 0;
let cursor = null;
let selected = [];
let activeCategory = "All";
let loadVersion = 0;

async function read(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Local query failed");
  return data;
}

function showStatus(text, error = false) {
  $("status").hidden = false;
  $("status").classList.toggle("error", error);
  $("status").textContent = text;
}

async function loadSessions(next = false) {
  $("more").disabled = true;
  $("inventory-note").textContent = "Loading local inventory…";
  try {
    const page = await read(
      `/api/sessions${next && cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    if (!next) sessions.length = 0;
    sessions.push(...page.items);
    total = page.total;
    cursor = page.next_cursor;
    renderInventory();
  } catch (error) {
    $("inventory-note").textContent = `Inventory unavailable: ${error.message}`;
    if (!selected.length)
      showStatus(`Could not discover sessions: ${error.message}`, true);
  } finally {
    $("more").disabled = false;
  }
}

function renderInventory() {
  const list = $("session-list");
  list.replaceChildren();
  const query = $("search").value.trim().toLowerCase();
  let matched = 0;
  for (const row of sessions) {
    const ids = [
      sessionId(row),
      ...(row.session_ids || []).filter((id) => id !== sessionId(row)),
    ];
    for (const id of ids) {
      const label =
        id === sessionId(row) ? nameOf(row) : `Member ${shortId(id)}`;
      if (
        ![label, row.project, row.vendors?.join(" "), id]
          .join(" ")
          .toLowerCase()
          .includes(query)
      )
        continue;
      matched++;
      const item = node("div", "session-row");
      if (selected.includes(id)) item.classList.add("selected");
      const open = node("button", "session-open");
      open.type = "button";
      open.append(
        node("strong", "", label),
        node(
          "span",
          "",
          `${row.project || "No project"} · ${(row.vendors || []).join(", ") || "Unknown vendor"}`,
        ),
      );
      open.title = id;
      open.addEventListener("click", () => select([id]));
      const compare = node(
        "button",
        "compare",
        selected.includes(id) ? "−" : "+",
      );
      compare.type = "button";
      compare.title = selected.includes(id)
        ? "Remove from comparison"
        : "Add to comparison";
      compare.setAttribute("aria-label", `${compare.title}: ${label}`);
      compare.disabled = !selected.includes(id) && selected.length >= 3;
      compare.addEventListener("click", () =>
        select(
          selected.includes(id)
            ? selected.filter((value) => value !== id)
            : [...selected, id],
        ),
      );
      item.append(open, compare);
      list.append(item);
    }
  }
  if (!matched)
    list.append(
      empty(query ? "No matches on loaded pages." : "No local sessions found."),
    );
  $("inventory-count").textContent = `${sessions.length} / ${total}`;
  $("more").hidden = !cursor;
  $("inventory-note").textContent = cursor
    ? `Showing ${sessions.length} of ${total} session graphs. Load more to search older sessions.`
    : `${total} session graph${total === 1 ? "" : "s"} discovered locally.`;
}

function select(ids) {
  selected = ids;
  activeCategory = "All";
  location.hash = ids.length ? `sessions=${ids.join(",")}` : "";
  renderInventory();
  loadReport();
}

async function loadReport() {
  const version = ++loadVersion;
  if (!selected.length) {
    $("report").hidden = true;
    showStatus(
      total
        ? "Select a session from the sidebar to inspect its evidence."
        : "No sessions available on this host.",
    );
    return;
  }
  showStatus(
    `Loading ${selected.length} session${selected.length === 1 ? "" : "s"}…`,
  );
  $("report").hidden = true;
  try {
    const data = await Promise.all(
      selected.map(async (id) => {
        if (!cache.has(id))
          cache.set(
            id,
            read(`/api/breakdown?session_id=${encodeURIComponent(id)}`),
          );
        try {
          return await cache.get(id);
        } catch (error) {
          cache.delete(id);
          throw error;
        }
      }),
    );
    if (version !== loadVersion) return;
    $("status").hidden = true;
    $("report").hidden = false;
    renderReport(data);
  } catch (error) {
    if (version === loadVersion)
      showStatus(`Could not load breakdown: ${error.message}`, true);
  }
}

function nameAt(index) {
  const row = sessions.find((item) =>
    [sessionId(item), ...(item.session_ids || [])].includes(selected[index]),
  );
  return row && selected[index] === sessionId(row)
    ? nameOf(row)
    : `Session ${shortId(selected[index])}`;
}

function runLabel(index, data, fullName = false) {
  const wrapper = node("div", `run-label run-${index}`);
  wrapper.append(
    node("i", "swatch"),
    node("strong", "", fullName ? nameAt(index) : shortId(selected[index])),
  );
  wrapper.title = nameAt(index);
  const vendor = data.sessions?.find(
    (session) => session.session_id === selected[index],
  );
  wrapper.append(
    node(
      "small",
      "",
      vendor?.model ||
        data.stats.model?.name ||
        vendor?.vendor ||
        "Model unavailable",
    ),
  );
  return wrapper;
}

function renderMetrics(data) {
  const definitions = [
    [
      "TOTAL COST ($)",
      (d) =>
        typeof d.usage.estimated_cost?.value_usd === "number"
          ? d.usage.estimated_cost.value_usd
          : null,
      (v) => `$${v.toFixed(3)}`,
    ],
    [
      "EXECUTION TIME",
      (d) => d.usage.runtime?.execution_seconds ?? null,
      duration,
    ],
    [
      "BILLED INPUT TOKENS",
      (d) =>
        d.usage.total_usage?.availability === "unavailable"
          ? null
          : (d.usage.total_usage?.input_tokens ?? null),
      compact,
    ],
    [
      "PROVIDER REQUESTS",
      (d) =>
        d.usage.total_usage?.availability === "unavailable" ? null : d.requests,
      number,
    ],
  ];
  $("metrics").replaceChildren();
  for (const [title, getValue, display] of definitions) {
    const card = node("article", "metric-card");
    card.append(node("h2", "", title));
    const values = data.map(getValue);
    const max = Math.max(
      1,
      ...values.filter((value) => typeof value === "number"),
    );
    data.forEach((run, index) => {
      const row = node("div", "metric-row");
      row.append(runLabel(index, run));
      const bar = node("div", "bar");
      if (values[index] !== null) {
        const fill = node("div", `bar-fill run-${index}`);
        fill.style.width = `${Math.max(2, (values[index] / max) * 100)}%`;
        bar.append(fill);
      }
      row.append(
        bar,
        node(
          "b",
          "metric-value",
          values[index] === null ? "—" : display(values[index]),
        ),
      );
      card.append(row);
    });
    if (values.every((value) => value === null))
      card.append(
        node(
          "p",
          "metric-note",
          "Not available in this session's provider observations.",
        ),
      );
    $("metrics").append(card);
  }
}

function renderTimeline(data) {
  $("timeline").replaceChildren();
  const maxDuration = Math.max(
    1,
    ...data.map((run) =>
      (run.usage.turns || []).reduce(
        (sum, turn) => sum + (turn.runtime?.execution_seconds || 0),
        0,
      ),
    ),
  );
  data.forEach((run, index) => {
    const turns = run.usage.turns || [];
    const panel = node("div", "strip-row");
    panel.append(runLabel(index, run));
    const track = node("div", "turn-track");
    const measured = turns.filter(
      (turn) => typeof turn.runtime?.execution_seconds === "number",
    );
    const sum = measured.reduce(
      (value, turn) => value + turn.runtime.execution_seconds,
      0,
    );
    if (sum)
      measured.forEach((turn, turnIndex) => {
        const segment = node("span", `turn-segment run-${index}`);
        segment.style.width = `${(turn.runtime.execution_seconds / maxDuration) * 100}%`;
        segment.title = `Turn ${turnIndex + 1}: ${duration(turn.runtime.execution_seconds)}`;
        track.append(segment);
      });
    else
      track.append(node("span", "empty-inline", "Turn durations unavailable"));
    panel.append(
      track,
      node("span", "strip-total", sum ? `${measured.length} turns` : "—"),
    );
    $("timeline").append(panel);
  });
}

function renderSequence(data) {
  $("sequence").replaceChildren();
  const counts = new Map(categories.map((category) => [category, 0]));
  data.forEach((run, index) => {
    const row = node("div", "strip-row");
    row.append(runLabel(index, run));
    const strip = node("div", "call-strip");
    for (const [position, item] of run.tools.entries()) {
      const type = group(item);
      counts.set(type, counts.get(type) + 1);
      const segment = node("span", `call-segment type-${type}`);
      segment.title = `${position + 1}. ${item.tool_name || "Unknown"} · ${number(visibleTokens(item))} estimated visible tokens`;
      strip.append(segment);
    }
    if (!run.tools.length)
      strip.append(node("span", "empty-inline", "No recorded tool calls"));
    row.append(
      strip,
      node("span", "strip-total", `${number(run.tools.length)} calls`),
    );
    $("sequence").append(row);
  });
  $("legend").replaceChildren();
  for (const category of categories) {
    if (!counts.get(category)) continue;
    const entry = node("span", "legend-item");
    entry.append(
      node("i", `type-${category}`),
      node("span", "", `${category} ${counts.get(category)}`),
    );
    $("legend").append(entry);
  }
}

function renderComposition(data) {
  $("composition").replaceChildren();
  $("composition").style.setProperty("--runs", data.length);
  data.forEach((run, index) => {
    const card = node("article", `composition-card run-${index}`);
    const header = node("div", "composition-head");
    header.append(runLabel(index, run, true));
    const roots = run.stats.context_window?.categories || [];
    const total = roots.reduce((value, root) => value + (root.tokens || 0), 0);
    header.append(node("span", "", `${compact(total)} est. visible`));
    card.append(header);
    for (const root of roots) {
      const rootRow = node("div", "composition-group");
      rootRow.append(
        node("span", "", root.label),
        node("span", "", compact(root.tokens)),
      );
      card.append(rootRow);
      for (const child of root.children || []) {
        const line = node("div", "composition-line");
        const label = node("div", "composition-label");
        label.append(
          node("span", "", child.label),
          node("span", "", compact(child.tokens)),
        );
        const track = node("div", "composition-track");
        const fill = node("span", "composition-fill");
        fill.style.width = `${Math.min(100, total ? (child.tokens / total) * 100 : 0)}%`;
        track.append(fill);
        line.append(label, track);
        card.append(line);
        for (const sub of child.children || []) {
          const nested = node("div", "composition-nested");
          nested.append(
            node("span", "", sub.label),
            node("span", "", compact(sub.tokens)),
          );
          card.append(nested);
        }
      }
    }
    $("composition").append(card);
  });
}

function summaryMap(run) {
  const map = new Map();
  for (const turn of run.turns || [])
    for (const activity of turn.activities || []) {
      const text =
        activity.cmd ||
        activity.path ||
        activity.query ||
        activity.target ||
        activity.task ||
        activity.url ||
        activity.tool;
      for (const id of activity.item_ids) map.set(id, text);
    }
  return map;
}

function renderDetails(data) {
  $("filters").replaceChildren();
  const allTools = data.flatMap((run) => run.tools);
  for (const category of ["All", ...categories]) {
    const count =
      category === "All"
        ? allTools.length
        : allTools.filter((item) => group(item) === category).length;
    if (!count) continue;
    const button = node("button", "filter", `${category} ${count}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(activeCategory === category));
    button.addEventListener("click", () => {
      activeCategory = category;
      renderDetails(data);
    });
    $("filters").append(button);
  }
  $("details").replaceChildren();
  $("details").style.setProperty("--runs", data.length);
  data.forEach((run, index) => {
    const column = node("div", "detail-column");
    column.append(runLabel(index, run, true));
    const summaries = summaryMap(run);
    const filtered = run.tools
      .map((tool, order) => ({ tool, order }))
      .filter(
        ({ tool }) =>
          activeCategory === "All" || group(tool) === activeCategory,
      );
    for (const { tool, order } of filtered.slice(0, 80)) {
      const line = node("div", "detail-row");
      line.append(
        node("span", "detail-index", String(order + 1).padStart(2, "0")),
      );
      const body = node("div", "detail-body");
      const title = node("div", "detail-title");
      title.append(
        node("b", `type-name type-text-${group(tool)}`, group(tool)),
        node(
          "span",
          "",
          run.item_details?.[tool.item_id]?.target ||
            summaries.get(tool.item_id) ||
            tool.input_summary ||
            "Input summary not retained",
        ),
      );
      body.append(
        title,
        node(
          "small",
          "",
          `${tool.tool_name || "Unknown tool"} · ${number(visibleTokens(tool))} est. visible tokens · ${typeof run.item_details?.[tool.item_id]?.duration_ms === "number" ? `${(run.item_details[tool.item_id].duration_ms / 1000).toFixed(1)}s · ` : ""}${tool.status || "status unavailable"}`,
        ),
      );
      line.append(body);
      column.append(line);
    }
    if (filtered.length > 80)
      column.append(
        empty(`Showing first 80 of ${filtered.length} calls in this category.`),
      );
    if (!filtered.length) column.append(empty("No calls in this category."));
    $("details").append(column);
  });
}

function renderReport(data) {
  $("title").textContent =
    selected.length === 1 ? nameAt(0) : `${selected.length} sessions compared`;
  $("breadcrumb-current").textContent =
    selected.length === 1 ? "BREAKDOWN" : "COMPARE";
  $("subtitle").textContent =
    `${selected.map((id) => shortId(id)).join(" vs ")} · Select a session or add up to three for side-by-side comparison.`;
  renderMetrics(data);
  renderTimeline(data);
  renderSequence(data);
  renderComposition(data);
  renderDetails(data);
}

$("search").addEventListener("input", renderInventory);
$("more").addEventListener("click", () => loadSessions(true));
$("refresh").addEventListener("click", async () => {
  cache.clear();
  await loadSessions();
  loadReport();
});
Promise.all([read("/api/config"), loadSessions()])
  .then(([config]) => {
    const hash = new URLSearchParams(location.hash.slice(1)).get("sessions");
    const ids = hash ? hash.split(",").slice(0, 3) : [];
    const valid = ids.filter((id) => /^[0-9a-f-]{36}$/i.test(id));
    if (valid.length) select(valid);
    else if (config.initial_session_id) select([config.initial_session_id]);
    else if (sessions.length) select([sessionId(sessions[0])]);
    else if (!$("status").classList.contains("error"))
      showStatus("No sessions available on this host.");
  })
  .catch((error) =>
    showStatus(`Could not start browser: ${error.message}`, true),
  );
