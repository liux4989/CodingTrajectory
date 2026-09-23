const $ = (id) => document.getElementById(id);
const format = (value) => new Intl.NumberFormat("en-US").format(value || 0);
const tokenEstimate = (item) => {
  const attribution = item.token_attribution || {};
  return (
    (attribution.tool_input_tokens || 0) + (attribution.tool_output_tokens || 0)
  );
};
const groups = ["All", "Read", "Write", "Edit", "Bash", "Other"];
const nativeNames = {
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
  Bash: "Bash",
  bash: "Bash",
  exec_command: "Bash",
  shell_command: "Bash",
  run_shell_command: "Bash",
  shell: "Bash",
  write_stdin: "Bash",
};
function group(item) {
  // Native names only. An orchestrating `exec` wrapper cannot be split by its inner commands.
  return nativeNames[item.tool_name] || "Other";
}
function span(className, value) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = value;
  return node;
}

function renderTools(items) {
  let selected = "All";
  const categories = $("categories");
  const sequence = $("sequence");
  function render() {
    categories.replaceChildren();
    for (const name of groups) {
      const subset =
        name === "All" ? items : items.filter((item) => group(item) === name);
      if (name !== "All" && !subset.length) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "category";
      button.setAttribute("aria-pressed", String(selected === name));
      button.append(
        span("category-label", name === "All" ? "All tools" : name),
      );
      button.append(span("category-count", format(subset.length)));
      button.append(
        span(
          "category-meta",
          `${format(subset.reduce((sum, item) => sum + tokenEstimate(item), 0))} visible tokens est.`,
        ),
      );
      button.addEventListener("click", () => {
        selected = name;
        render();
      });
      categories.append(button);
    }
    sequence.replaceChildren();
    const filtered = items
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => selected === "All" || group(item) === selected);
    $("sequence-count").textContent =
      `${format(filtered.length)} of ${format(items.length)} calls`;
    if (!filtered.length) {
      sequence.append(
        span("empty", "No tool calls recorded for this session."),
      );
      return;
    }
    for (const { item, index } of filtered) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "call";
      button.setAttribute("aria-expanded", "false");
      button.append(span("call-index", String(index + 1).padStart(2, "0")));
      const tag = span("tool-tag", group(item));
      tag.dataset.group = group(item);
      button.append(tag);
      button.append(
        span("call-label", item.input_summary || "No input summary retained"),
      );
      button.append(
        span("call-tokens", `${format(tokenEstimate(item))} est. tokens`),
      );
      button.addEventListener("click", () => {
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        button.querySelector(".call-detail")?.remove();
        if (!expanded) {
          const attribution = item.token_attribution || {};
          const detail = span(
            "call-detail",
            `${item.tool_name || "Unknown tool"} · Input ${format(attribution.tool_input_tokens)} · Output ${format(attribution.tool_output_tokens)} estimated visible tokens · ${item.status || "Status unknown"} · Item ${item.item_id}`,
          );
          button.append(detail);
        }
      });
      sequence.append(button);
    }
  }
  render();
}

function renderComposition(stats) {
  const container = $("composition");
  const categories = stats.context_window?.categories || [];
  for (const root of categories) {
    const panel = document.createElement("article");
    panel.className = "composition-panel";
    const heading = document.createElement("h3");
    heading.textContent = root.label;
    panel.append(
      heading,
      span("composition-total", `${format(root.tokens)} estimated tokens`),
    );
    const children = root.children || [];
    for (const child of children) {
      const metric = document.createElement("div");
      metric.className = "metric";
      const label = document.createElement("div");
      label.className = "metric-label";
      label.append(span("", child.label), span("", format(child.tokens)));
      const track = document.createElement("div");
      track.className = "track";
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = `${Math.max(0, Math.min(100, root.tokens ? (child.tokens / root.tokens) * 100 : 0))}%`;
      track.append(fill);
      metric.append(label, track);
      panel.append(metric);
      for (const grandchild of child.children || []) {
        const nested = document.createElement("div");
        nested.className = "metric nested";
        const nestedLabel = document.createElement("div");
        nestedLabel.className = "metric-label";
        nestedLabel.append(
          span("", `↳ ${grandchild.label}`),
          span("", format(grandchild.tokens)),
        );
        nested.append(nestedLabel);
        panel.append(nested);
      }
    }
    container.append(panel);
  }
  if (!categories.length)
    container.append(
      span("empty", "Context composition unavailable for this session."),
    );
}

fetch("/api/breakdown")
  .then(async (response) => {
    const payload = await response.json();
    if (!response.ok)
      throw new Error(payload.error || "Failed to load session");
    const { stats, tools } = payload;
    $("session-id").textContent = stats.root_session_id || "—";
    $("model").textContent = stats.model?.name || "Unknown";
    $("calls").textContent = format(tools.length);
    const seconds = stats.runtime?.execution_seconds;
    $("duration").textContent =
      typeof seconds === "number" ? `${Math.round(seconds)}s` : "—";
    renderTools(tools);
    renderComposition(stats);
    $("status").hidden = true;
    $("report").hidden = false;
  })
  .catch((error) => {
    $("status").classList.add("error");
    $("status").textContent = `Could not load breakdown: ${error.message}`;
  });
