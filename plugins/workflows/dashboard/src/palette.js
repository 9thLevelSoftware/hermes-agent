import { NODE_COLORS, NODE_ICONS } from "./canvas-nodes.js";
import { SUPPORTED_NODES, SUPPORTED_TRIGGERS } from "./editor-model.js";

const NODE_CATEGORIES = [
  {
    name: "Triggers",
    color: "trigger",
    nodes: [
      ["manual", "Manual Trigger", "Start the workflow on demand."],
      ["schedule", "Schedule Trigger", "Start the workflow on a schedule."],
      ["webhook", "Webhook", "Start the workflow from an HTTP request."],
    ],
  },
  {
    name: "AI & Logic",
    color: "agent_task",
    nodes: [
      ["agent_task", "AI Agent Task", "Delegate work to a Hermes profile."],
      ["switch", "Switch/Branch", "Route based on a value or condition."],
      ["pass", "Pass/Transform", "Shape or summarize data."],
    ],
  },
  {
    name: "Flow Control",
    color: "parallel",
    nodes: [
      ["parallel", "Parallel Split", "Run independent branches."],
      ["join", "Join", "Wait for branches to complete."],
      ["wait", "Wait/Delay", "Pause before continuing."],
    ],
  },
  {
    name: "Actions",
    color: "send_message",
    nodes: [
      ["send_message", "Send Message", "Send a message to a configured target."],
      ["fail", "Fail/Reject", "Stop the workflow with an error."],
    ],
  },
];

function safeString(value) {
  return value === null || value === undefined ? "" : String(value);
}

function selectionKey(definition) {
  if (!definition) return "";
  return safeString(definition.workflow_id || definition.id) + ":" + safeString(definition.version);
}

function isSupported(type, trigger) {
  const values = trigger ? SUPPORTED_TRIGGERS : SUPPORTED_NODES;
  return values.indexOf(type) !== -1;
}

export function renderPalette(props) {
  const h = props.createElement;
  const React = props.React || {};
  const useState = React.useState || function (initial) { return [initial, function () {}]; };
  const tabState = useState("workflows");
  const activeTab = tabState[0];
  const setActiveTab = tabState[1];
  const searchState = useState("");
  const search = searchState[0];
  const setSearch = searchState[1];
  const definitions = Array.isArray(props.definitions) ? props.definitions : [];
  const executions = Array.isArray(props.executions) ? props.executions : [];

  function call(handler) {
    if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  function dragProps(type) {
    return {
      draggable: true,
      onDragStart: function (event) {
        if (event.dataTransfer && event.dataTransfer.setData) event.dataTransfer.setData("text/plain", type);
        if (typeof window !== "undefined") window.__HERMES_DRAG_NODE_TYPE = type;
      },
    };
  }

  function renderWorkflowForm() {
    return h("section", { className: "hermes-workflows-palette-section hermes-workflows-palette-creation" },
      h("h3", null, props.activeSpec ? "New workflow / prompt" : "New workflow"),
      h("p", { className: "hermes-workflows-muted" }, "Describe it or start from blank."),
      h("form", { className: "hermes-workflows-stack", "aria-label": "Create workflow", onSubmit: props.draftFromGoal },
        h("input", {
          value: props.newWorkflowName || "",
          onChange: function (event) { call(props.setNewWorkflowName, event.target.value); },
          placeholder: "Workflow name",
        }),
        h("textarea", {
          "aria-label": "Describe workflow goal",
          value: props.goalText || "",
          onChange: function (event) { call(props.setGoalText, event.target.value); },
          placeholder: "Example: review code changes, run tests, then deploy if approved.",
        }),
        h("div", { className: "hermes-workflows-row" },
          h("button", { type: "submit", disabled: props.drafting, className: "hermes-workflows-primary" }, props.drafting ? "Generating…" : "Generate From Prompt"),
          h("button", { type: "button", "aria-label": "Start from scratch", onClick: props.startBlankWorkflow }, "Start From Scratch")
        )
      ),
      props.activeSpec ? h("form", { className: "hermes-workflows-stack", onSubmit: props.refineWorkflow },
        h("textarea", {
          value: props.refineText || "",
          onChange: function (event) { call(props.setRefineText, event.target.value); },
          placeholder: "Refine: add a step, change routing, etc.",
          "aria-label": "Refine workflow",
        }),
        h("button", { type: "submit", disabled: props.refining }, props.refining ? "Refining…" : "Refine")
      ) : null,
      props.draftResult ? h("div", { className: "hermes-workflows-palette-draft hermes-workflows-stack" },
        props.draftResult.summary ? h("p", { className: "hermes-workflows-muted" }, props.draftResult.summary) : null,
        (props.draftResult.assumptions || []).length ? h("p", null, h("strong", null, "Assumptions: "), props.draftResult.assumptions.join("; ")) : null,
        (props.draftResult.warnings || []).length ? h("p", { className: "hermes-workflows-muted" }, h("strong", null, "Warnings: "), props.draftResult.warnings.join("; ")) : null,
        h("div", { className: "hermes-workflows-row" },
          h("button", { type: "button", onClick: props.acceptDraftCandidate, className: "hermes-workflows-primary" }, props.candidateSource === "generate" ? "Accept Draft" : "Accept Changes"),
          h("button", { type: "button", onClick: props.rejectDraftCandidate }, "Reject")
        )
      ) : null
    );
  }

  function renderDefinitions() {
    return h("section", { className: "hermes-workflows-palette-section" },
      h("h3", null, "Workflows"),
      h("div", { className: "hermes-workflows-sidebar-list" },
        definitions.length ? definitions.map(function (definition) {
          const id = definition.workflow_id || definition.id;
          const key = selectionKey(definition);
          return h("button", {
            key: key,
            type: "button",
            className: "hermes-workflows-sidebar-item" + (key === selectionKey(props.selectedDefinition) ? " is-selected" : ""),
            onClick: function () { call(props.loadDefinition, id, definition.version); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, safeString(definition.name || id)),
            h("span", { className: "hermes-workflows-sidebar-badge" + (definition.enabled ? " is-enabled" : "") }, definition.enabled ? "on" : "off")
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No workflows deployed.")
      )
    );
  }

  function renderExecutions() {
    return h("section", { className: "hermes-workflows-palette-section" },
      h("h3", null, "Executions"),
      h("div", { className: "hermes-workflows-sidebar-list" },
        executions.length ? executions.slice(0, 20).map(function (execution) {
          const id = safeString(execution.execution_id || execution.id);
          const status = safeString(execution.status);
          const statusClass = status === "succeeded" ? " is-succeeded" : status === "failed" ? " is-failed" : "";
          return h("button", {
            key: id,
            type: "button",
            className: "hermes-workflows-sidebar-item",
            onClick: function () { call(props.loadExecution, id); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, id.slice(0, 16)),
            h("span", { className: "hermes-workflows-sidebar-badge" + statusClass }, status)
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")
      )
    );
  }

  function renderNodeCard(node) {
    const type = node[0];
    const trigger = NODE_CATEGORIES[0].nodes.some(function (item) { return item[0] === type; });
    if (!isSupported(type, trigger)) return null;
    return h("button", Object.assign({
      key: type,
      type: "button",
      className: "hermes-workflows-palette-card",
      "aria-label": "Add " + node[1],
      onClick: function () {
        trigger ? call(props.addTriggerOfType, type) : call(props.addWorkflowCellOfType, type);
      },
    }, dragProps(type)),
      h("span", { className: "hermes-workflows-palette-icon", "aria-hidden": "true" }, NODE_ICONS[type] || "•"),
      h("div", { className: "hermes-workflows-palette-text" },
        h("span", { className: "hermes-workflows-palette-title" }, node[1]),
        h("span", { className: "hermes-workflows-palette-desc" }, node[2])
      )
    );
  }

  function renderNodes() {
    const Fragment = React.Fragment || "div";
    const needle = String(search || "").trim().toLowerCase();
    const visibleCategories = needle
      ? NODE_CATEGORIES.map(function (category) {
          return {
            name: category.name,
            color: category.color,
            nodes: category.nodes.filter(function (node) {
              const type = String(node[0] || "").toLowerCase();
              const label = String(node[1] || "").toLowerCase();
              return type.includes(needle) || label.includes(needle);
            }),
          };
        }).filter(function (category) { return category.nodes.length > 0; })
      : NODE_CATEGORIES;
    return h("div", { className: "hermes-workflows-palette-panel hermes-workflows-node-library", role: "tabpanel", "aria-label": "Nodes" },
      h("p", { className: "hermes-workflows-muted" }, "Drag a node onto the canvas, or click to add it."),
      h("input", {
        type: "search",
        className: "hermes-workflows-palette-search-input",
        placeholder: "Search nodes...",
        "aria-label": "Search nodes",
        value: search,
        onChange: function (event) { setSearch(event.target.value); },
      }),
      visibleCategories.length
        ? visibleCategories.map(function (category) {
            return h(Fragment, { key: category.name },
              h("h3", { className: "hermes-workflows-palette-category", style: { color: NODE_COLORS[category.color] || "inherit" } }, category.name),
              h("div", { className: "hermes-workflows-node-palette" }, category.nodes.map(renderNodeCard))
            );
          })
        : h("p", { className: "hermes-workflows-muted" }, "No nodes match \u201c" + String(search) + "\u201d.")
    );
  }

  if (props.variant === "onboarding") {
    return h("div", { className: "hermes-workflows-onboarding-form" }, renderWorkflowForm());
  }

  return h("aside", { className: "hermes-workflows-sidebar hermes-workflows-palette" },
    h("div", { className: "hermes-workflows-palette-tabs", role: "tablist", "aria-label": "Workflow palette" },
      h("button", {
        type: "button",
        role: "tab",
        className: "hermes-workflows-palette-tab" + (activeTab === "workflows" ? " is-active" : ""),
        "aria-selected": activeTab === "workflows" ? "true" : "false",
        onClick: function () { setActiveTab("workflows"); },
      }, "Workflows"),
      h("button", {
        type: "button",
        role: "tab",
        className: "hermes-workflows-palette-tab" + (activeTab === "nodes" ? " is-active" : ""),
        "aria-selected": activeTab === "nodes" ? "true" : "false",
        onClick: function () { setActiveTab("nodes"); },
      }, "Nodes")
    ),
    activeTab === "nodes" ? renderNodes() : h("div", { className: "hermes-workflows-palette-panel hermes-workflows-workflow-library", role: "tabpanel", "aria-label": "Workflows" },
      renderWorkflowForm(),
      renderDefinitions(),
      renderExecutions()
    )
  );
}
