(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG || typeof REG.register !== "function" || !SDK.React || !SDK.fetchJSON) return;

  const React = SDK.React;
  const h = React.createElement;
  const Card = SDK.components && SDK.components.Card ? SDK.components.Card : "section";
  const API = "/api/plugins/workflows";
  const DEFINITIONS_API = "/api/plugins/workflows/definitions";
  const EXAMPLE_DEFINITION = [
    "id: dashboard_demo",
    "name: Dashboard Demo",
    "version: 1",
    "enabled: true",
    "triggers:",
    "  - id: manual",
    "    type: manual",
    "nodes:",
    "  start:",
    "    type: pass",
    "    output:",
    "      ok: true",
    "edges: []",
  ].join("\n");

  function api(path, options) {
    return SDK.fetchJSON(API + path, options);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function safeString(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value);
  }

  function jsonBlock(value) {
    try {
      return JSON.stringify(value || {}, null, 2);
    } catch (_) {
      return String(value);
    }
  }

  function nodeList(spec) {
    const nodes = spec && spec.nodes ? spec.nodes : {};
    if (Array.isArray(nodes)) return nodes;
    return Object.keys(nodes).map(function (id) {
      return Object.assign({ id: id }, nodes[id] || {});
    });
  }

  function edgeList(spec) {
    return asArray(spec && spec.edges).map(function (edge, index) {
      return {
        id: edge.id || String(index + 1),
        from: edge.from || edge.source || edge.start || "?",
        to: edge.to || edge.target || edge.end || "?",
      };
    });
  }

  function statusClass(status) {
    return "hermes-workflows-badge " + (status === false ? "is-off" : "is-on");
  }

  function WorkflowsPage() {
    const useState = React.useState;
    const useEffect = React.useEffect;
    const stateDefinitions = useState([]);
    const definitions = stateDefinitions[0];
    const setDefinitions = stateDefinitions[1];
    const stateExecutions = useState([]);
    const executions = stateExecutions[0];
    const setExecutions = stateExecutions[1];
    const stateSelectedDefinition = useState(null);
    const selectedDefinition = stateSelectedDefinition[0];
    const setSelectedDefinition = stateSelectedDefinition[1];
    const stateSelectedExecution = useState(null);
    const selectedExecution = stateSelectedExecution[0];
    const setSelectedExecution = stateSelectedExecution[1];
    const stateEditorText = useState(EXAMPLE_DEFINITION);
    const editorText = stateEditorText[0];
    const setEditorText = stateEditorText[1];
    const stateRunWorkflowId = useState("");
    const runWorkflowId = stateRunWorkflowId[0];
    const setRunWorkflowId = stateRunWorkflowId[1];
    const stateRunInputText = useState("{}");
    const runInputText = stateRunInputText[0];
    const setRunInputText = stateRunInputText[1];
    const stateEvents = useState([]);
    const events = stateEvents[0];
    const setEvents = stateEvents[1];
    const stateStatus = useState("");
    const status = stateStatus[0];
    const setStatus = stateStatus[1];
    const stateError = useState("");
    const error = stateError[0];
    const setError = stateError[1];
    const stateLoading = useState(false);
    const loading = stateLoading[0];
    const setLoading = stateLoading[1];
    const stateValidating = useState(false);
    const validating = stateValidating[0];
    const setValidating = stateValidating[1];
    const stateDeploying = useState(false);
    const deploying = stateDeploying[0];
    const setDeploying = stateDeploying[1];
    const stateRunning = useState(false);
    const running = stateRunning[0];
    const setRunning = stateRunning[1];

    function fail(err) {
      setError(err && err.message ? err.message : String(err));
    }

    function loadDefinition(workflowId) {
      if (!workflowId) {
        setSelectedDefinition(null);
        return Promise.resolve(null);
      }
      return api("/definitions/" + encodeURIComponent(workflowId)).then(function (res) {
        const definition = res.definition || null;
        setSelectedDefinition(definition);
        if (definition) setRunWorkflowId(definition.workflow_id || definition.id || workflowId);
        return definition;
      });
    }

    function loadEvents(executionId) {
      if (!executionId) {
        setEvents([]);
        return Promise.resolve([]);
      }
      return api("/executions/" + encodeURIComponent(executionId) + "/events").then(function (res) {
        const rows = asArray(res.events);
        setEvents(rows);
        return rows;
      });
    }

    function loadExecution(executionId) {
      if (!executionId) {
        setSelectedExecution(null);
        setEvents([]);
        return Promise.resolve(null);
      }
      return api("/executions/" + encodeURIComponent(executionId)).then(function (res) {
        const execution = res.execution || null;
        setSelectedExecution(execution);
        return loadEvents(executionId).then(function () { return execution; });
      });
    }

    function loadDefinitions(preferId) {
      return SDK.fetchJSON(DEFINITIONS_API).then(function (res) {
        const rows = asArray(res.definitions);
        const currentId = selectedDefinition && (selectedDefinition.workflow_id || selectedDefinition.id);
        const nextId = preferId || currentId || runWorkflowId || (rows[0] && (rows[0].workflow_id || rows[0].id)) || "";
        setDefinitions(rows);
        if (nextId) return loadDefinition(nextId);
        setRunWorkflowId("");
        setSelectedDefinition(null);
        return null;
      });
    }

    function loadExecutions(preferId) {
      return api("/executions").then(function (res) {
        const rows = asArray(res.executions);
        const currentId = selectedExecution && selectedExecution.execution_id;
        const nextId = preferId || currentId || (rows[0] && rows[0].execution_id) || "";
        setExecutions(rows);
        if (nextId) return loadExecution(nextId);
        setSelectedExecution(null);
        setEvents([]);
        return null;
      });
    }

    function refresh(preferExecutionId) {
      setLoading(true);
      setError("");
      return Promise.all([loadDefinitions(), loadExecutions(preferExecutionId)])
        .catch(fail)
        .finally(function () { setLoading(false); });
    }

    useEffect(function () {
      refresh();
    }, []);

    function validateDefinition() {
      setValidating(true);
      setError("");
      api("/definitions/validate", {
        method: "POST",
        headers: { "Content-Type": "text/plain" },
        body: editorText,
      }).then(function (res) {
        const definition = res.definition || {};
        setStatus("Validated " + safeString(definition.workflow_id || definition.id));
        if (definition.spec) setSelectedDefinition(definition);
      }).catch(fail).finally(function () { setValidating(false); });
    }

    function deployDefinition() {
      setDeploying(true);
      setError("");
      api("/definitions/deploy", {
        method: "POST",
        headers: { "Content-Type": "text/plain" },
        body: editorText,
      }).then(function (res) {
        const definition = res.definition || {};
        const id = definition.workflow_id || definition.id || "";
        setStatus("Deployed " + safeString(id));
        return loadDefinitions(id);
      }).catch(fail).finally(function () { setDeploying(false); });
    }

    function runWorkflow(event) {
      event.preventDefault();
      const workflowId = (runWorkflowId || "").trim();
      if (!workflowId) {
        setError("Choose a workflow before running it.");
        return;
      }
      setRunning(true);
      setError("");
      api("/definitions/" + encodeURIComponent(workflowId) + "/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input_json: runInputText }),
      }).then(function (res) {
        const execution = res.execution || {};
        const executionId = execution.execution_id;
        setStatus("Started execution " + safeString(executionId));
        return loadExecutions(executionId);
      }).catch(fail).finally(function () { setRunning(false); });
    }

    function renderDefinitionList() {
      return h("div", { className: "hermes-workflows-list" },
        definitions.length ? definitions.map(function (definition) {
          const id = definition.workflow_id || definition.id;
          const selectedId = selectedDefinition && (selectedDefinition.workflow_id || selectedDefinition.id);
          return h("button", {
            key: id + ":" + safeString(definition.version),
            type: "button",
            className: "hermes-workflows-item" + (id === selectedId ? " is-selected" : ""),
            onClick: function () {
              setError("");
              loadDefinition(id).catch(fail);
            },
          },
            h("div", { className: "hermes-workflows-item-title" },
              h("span", null, safeString(definition.name || id)),
              h("span", { className: statusClass(definition.enabled) }, definition.enabled ? "enabled" : "disabled")
            ),
            h("div", { className: "hermes-workflows-meta" }, safeString(id) + " · v" + safeString(definition.version))
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No workflow definitions deployed yet.")
      );
    }

    function renderExecutions() {
      return h("div", { className: "hermes-workflows-executions" },
        executions.length ? executions.map(function (execution) {
          const id = execution.execution_id;
          return h("button", {
            key: id,
            type: "button",
            className: "hermes-workflows-item" + (selectedExecution && selectedExecution.execution_id === id ? " is-selected" : ""),
            onClick: function () {
              setError("");
              loadExecution(id).catch(fail);
            },
          },
            h("div", { className: "hermes-workflows-item-title" },
              h("span", null, safeString(id)),
              h("span", { className: "hermes-workflows-badge" }, safeString(execution.status))
            ),
            h("div", { className: "hermes-workflows-meta" },
              safeString(execution.workflow_id) + " · " + safeString(execution.updated_at || execution.created_at)
            )
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")
      );
    }

    function renderTimeline() {
      return h("div", { className: "hermes-workflows-timeline" },
        selectedExecution ? h("div", { className: "hermes-workflows-event" },
          h("div", { className: "hermes-workflows-item-title" },
            h("strong", null, safeString(selectedExecution.execution_id)),
            h("span", { className: "hermes-workflows-badge" }, safeString(selectedExecution.status))
          ),
          h("div", { className: "hermes-workflows-meta" },
            safeString(selectedExecution.workflow_id) + " · created " + safeString(selectedExecution.created_at)
          ),
          h("pre", { className: "hermes-workflows-pre" }, jsonBlock(selectedExecution.input))
        ) : h("p", { className: "hermes-workflows-muted" }, "Select an execution to inspect it."),
        events.length ? events.map(function (row) {
          return h("div", { key: row.id, className: "hermes-workflows-event" },
            h("div", { className: "hermes-workflows-item-title" },
              h("span", { className: "hermes-workflows-event-kind" }, safeString(row.kind)),
              h("span", { className: "hermes-workflows-meta" }, safeString(row.created_at))
            ),
            row.node_run_id ? h("div", { className: "hermes-workflows-meta" }, "node run " + row.node_run_id) : null,
            h("pre", { className: "hermes-workflows-pre" }, jsonBlock(row.payload))
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No events recorded for this execution.")
      );
    }

    function renderGraph() {
      const spec = selectedDefinition && selectedDefinition.spec ? selectedDefinition.spec : null;
      const nodes = nodeList(spec);
      const edges = edgeList(spec);
      return h("div", { className: "hermes-workflows-graph" },
        h("div", null,
          h("h2", null, "Readonly graph"),
          h("p", { className: "hermes-workflows-muted" }, spec ? safeString(spec.name || selectedDefinition.name || selectedDefinition.workflow_id) : "Select or validate a workflow to render its nodes and edges.")
        ),
        nodes.length ? h("div", { className: "hermes-workflows-node-grid" }, nodes.map(function (node) {
          const id = node.id || node.name || "node";
          return h("div", { key: id, className: "hermes-workflows-node-card" },
            h("h3", null, safeString(id)),
            h("div", { className: "hermes-workflows-node-type" }, safeString(node.type)),
            h("pre", { className: "hermes-workflows-pre" }, jsonBlock(node))
          );
        })) : h("p", { className: "hermes-workflows-muted" }, "No nodes to render."),
        h("div", { className: "hermes-workflows-stack" },
          h("h3", null, "Edges"),
          edges.length ? edges.map(function (edge) {
            return h("div", { key: edge.id, className: "hermes-workflows-edge-card" }, safeString(edge.from) + " → " + safeString(edge.to));
          }) : h("p", { className: "hermes-workflows-muted" }, "No edges defined.")
        )
      );
    }

    return h("div", { className: "hermes-workflows" },
      h(Card, { className: "hermes-workflows-header" },
        h("div", null,
          h("h1", null, "Workflows"),
          h("p", { className: "hermes-workflows-muted" }, "Practical workflow screen for definitions, manual runs, executions, and graph previews.")
        ),
        h("button", { type: "button", disabled: loading, onClick: function () { refresh(); } }, loading ? "Refreshing…" : "Refresh")
      ),
      error ? h("div", { className: "hermes-workflows-banner is-error" }, error) : null,
      status ? h("div", { className: "hermes-workflows-banner" }, status) : null,
      h("div", { className: "hermes-workflows-grid" },
        h("div", { className: "hermes-workflows-stack" },
          h(Card, { className: "hermes-workflows-panel" },
            h("h2", null, "Workflow list"),
            renderDefinitionList()
          ),
          h(Card, { className: "hermes-workflows-panel hermes-workflows-run-form" },
            h("h2", null, "Manual run form"),
            h("form", { className: "hermes-workflows-stack", onSubmit: runWorkflow },
              h("label", null,
                h("span", { className: "hermes-workflows-muted" }, "Workflow id"),
                definitions.length ? h("select", {
                  value: runWorkflowId,
                  onChange: function (event) {
                    setRunWorkflowId(event.target.value);
                    loadDefinition(event.target.value).catch(fail);
                  },
                }, definitions.map(function (definition) {
                  const id = definition.workflow_id || definition.id;
                  return h("option", { key: id, value: id }, id);
                })) : h("input", {
                  value: runWorkflowId,
                  onChange: function (event) { setRunWorkflowId(event.target.value); },
                  placeholder: "workflow_id",
                })
              ),
              h("label", null,
                h("span", { className: "hermes-workflows-muted" }, "Input JSON"),
                h("textarea", {
                  className: "hermes-workflows-run-input",
                  value: runInputText,
                  onChange: function (event) { setRunInputText(event.target.value); },
                })
              ),
              h("button", { type: "submit", disabled: running, className: "hermes-workflows-primary" }, running ? "Running…" : "Run workflow")
            )
          ),
          h(Card, { className: "hermes-workflows-panel" },
            h("h2", null, "Execution list"),
            renderExecutions()
          )
        ),
        h("div", { className: "hermes-workflows-stack" },
          h(Card, { className: "hermes-workflows-panel" },
            h("h2", null, "Validate / deploy definition"),
            h("textarea", {
              className: "hermes-workflows-editor",
              value: editorText,
              onChange: function (event) { setEditorText(event.target.value); },
            }),
            h("div", { className: "hermes-workflows-row" },
              h("button", { type: "button", disabled: validating, onClick: validateDefinition }, validating ? "Validating…" : "Validate"),
              h("button", { type: "button", disabled: deploying, onClick: deployDefinition, className: "hermes-workflows-primary" }, deploying ? "Deploying…" : "Deploy")
            )
          ),
          h(Card, { className: "hermes-workflows-panel" }, renderGraph()),
          h(Card, { className: "hermes-workflows-panel" },
            h("h2", null, "Execution detail timeline"),
            renderTimeline()
          )
        )
      )
    );
  }

  REG.register("workflows", WorkflowsPage);
})();
