import { NODE_COLORS } from "./canvas-nodes.js";
import { SCALAR_INPUT_KINDS, SUPPORTED_NODES, SUPPORTED_TRIGGERS, changeNodeType } from "./editor-model.js";

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

function profileRows(options) {
  return asArray(options && options.profiles).filter(function (profile) {
    return profile && profile.name;
  });
}

function providerRows(options) {
  return asArray(options && options.providers).filter(function (provider) {
    return provider && (provider.slug || provider.provider);
  });
}

function specToEditorText(spec) {
  return JSON.stringify(spec || {}, null, 2);
}

function findSpecNode(spec, nodeId) {
  if (!spec || !nodeId) return null;
  if (Array.isArray(spec.nodes)) {
    return spec.nodes.find(function (node) { return String(node && (node.id || node.name)) === String(nodeId); }) || null;
  }
  var node = spec.nodes && spec.nodes[nodeId];
  return node ? Object.assign({}, node, { id: nodeId }) : null;
}

function renderAdvancedNodeJson(props, spec) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("h3", null, "Advanced JSON"),
    h("textarea", {
      className: "hermes-workflows-node-json",
      value: props.nodeJson,
      onChange: function (event) { props.setNodeJson(event.target.value); },
    }),
    h("div", { className: "hermes-workflows-row" },
      h("button", { type: "button", onClick: props.applyNodeJson }, "Apply node JSON"),
      h("button", { type: "button", onClick: props.useJsonDraft, disabled: !spec }, "Use JSON draft")
    )
  );
}

function renderPromptAssistant(props) {
  var h = props.createElement;
  if (!props.promptAssistantOpen) return null;
  return h("div", { className: "hermes-workflows-assistant" },
    h("h4", null, "Prompt assistant"),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Workflow goal"),
      h("textarea", { value: props.promptAssistantGoal, onChange: function (event) { props.setPromptAssistantGoal(event.target.value); } })
    ),
    props.promptAssistantAdvanced ? h("div", { className: "hermes-workflows-stack" },
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Cell objective"),
        h("textarea", { value: props.promptAssistantObjective, onChange: function (event) { props.setPromptAssistantObjective(event.target.value); } })
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Context placeholders"),
        h("textarea", { value: props.promptAssistantContext, onChange: function (event) { props.setPromptAssistantContext(event.target.value); } })
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Output contract JSON"),
        h("textarea", { value: props.promptAssistantOutput, onChange: function (event) { props.setPromptAssistantOutput(event.target.value); } })
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Constraints"),
        h("textarea", { value: props.promptAssistantConstraints, onChange: function (event) { props.setPromptAssistantConstraints(event.target.value); } })
      ),
      h("button", { type: "button", onClick: function () { props.setPromptAssistantAdvanced(false); }, className: "hermes-workflows-link-btn" }, "Hide advanced fields")
    ) : h("button", { type: "button", onClick: function () { props.setPromptAssistantAdvanced(true); }, className: "hermes-workflows-link-btn" }, "Show advanced fields"),
    h("button", { type: "button", onClick: props.draftPromptWithAssistant, className: "hermes-workflows-primary" }, "Draft prompt")
  );
}

function renderAgentTaskInspector(props) {
  var h = props.createElement;
  var profiles = profileRows(props.agentRoutingOptions);
  var providers = providerRows(props.agentRoutingOptions);
  var selectedProvider = providers.find(function (provider) {
    return String(provider.slug || provider.provider || "") === props.agentProvider;
  });
  var models = asArray(selectedProvider && selectedProvider.models);
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Assigned profile"),
      profiles.length ? h("select", { value: props.agentProfile, onChange: function (event) { props.setAgentProfile(event.target.value); } },
        [h("option", { key: "", value: "" }, "Choose profile")].concat(profiles.map(function (profile) {
          var label = profile.name + (profile.provider || profile.model ? " · " + safeString(profile.provider) + " / " + safeString(profile.model) : "");
          return h("option", { key: profile.name, value: profile.name }, label);
        }))
      ) : h("input", { value: props.agentProfile, onChange: function (event) { props.setAgentProfile(event.target.value); }, placeholder: "reviewer" })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Provider override"),
      h("select", {
        value: props.agentProvider,
        onChange: function (event) {
          props.setAgentProvider(event.target.value);
          props.setAgentModel("");
        },
      }, [h("option", { key: "", value: "" }, "Use profile default provider")].concat(providers.map(function (provider) {
        var slug = String(provider.slug || provider.provider || "");
        return h("option", { key: slug, value: slug }, safeString(provider.label || slug));
      })))
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Model override"),
      models.length ? h("select", {
        value: props.agentModel,
        onChange: function (event) { props.setAgentModel(event.target.value); },
      }, [h("option", { key: "", value: "" }, "Use profile default model")].concat(models.map(function (model) {
        return h("option", { key: String(model), value: String(model) }, safeString(model));
      }))) : h("input", {
        value: props.agentModel,
        onChange: function (event) { props.setAgentModel(event.target.value); },
        placeholder: props.agentProvider ? "Model name for selected provider" : "Use profile default model",
      })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Task title"),
      h("input", { value: props.agentTitle, onChange: function (event) { props.setAgentTitle(event.target.value); }, placeholder: "Review change" })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Agent cell prompt"),
      h("textarea", { className: "hermes-workflows-prompt-editor", value: props.promptText, onChange: function (event) { props.setPromptText(event.target.value); }, placeholder: "Tell the assigned profile exactly what to do. Use ${ input.foo } or ${ node.previous.output.bar } for workflow context." })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Result contract JSON"),
      h("textarea", { className: "hermes-workflows-contract-editor", value: props.resultContractText, onChange: function (event) { props.setResultContractText(event.target.value); } })
    ),
    h("button", { type: "button", onClick: function () { props.setPromptAssistantOpen(!props.promptAssistantOpen); } }, props.promptAssistantOpen ? "Hide Prompt assistant" : "Prompt assistant"),
    renderPromptAssistant(props)
  );
}

function renderTriggerInspector(props) {
  var h = props.createElement;
  var rows = asArray(props.triggerInputRows);
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Trigger type"),
      h("input", { value: props.cellType, onChange: function (event) { props.setCellType(event.target.value); }, placeholder: "manual", list: "workflow-trigger-type-options-inspector" }),
      h("datalist", { id: "workflow-trigger-type-options-inspector" }, SUPPORTED_TRIGGERS.map(function (type) {
        return h("option", { key: type, value: type });
      }))
    ),
    props.cellType === "schedule" ? h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Schedule / cron"),
      h("input", { value: props.triggerSchedule, onChange: function (event) { props.setTriggerSchedule(event.target.value); }, placeholder: "0 9 * * *" })
    ) : null,
    h("div", { className: "hermes-workflows-trigger-editor", "aria-label": "Input schema" },
      h("div", { className: "hermes-workflows-item-title" },
        h("strong", null, "Input schema"),
        h("span", { className: "hermes-workflows-meta" }, "Advanced JSON remains available")
      ),
      h("p", { className: "hermes-workflows-muted" }, "Phase 1 supports scalar manual and continuous input items. Batch splitting and document uploads are not supported in this release."),
      rows.length ? h("div", { className: "hermes-workflows-input-field-list" }, rows.map(function (row) {
        return h("div", { key: row.name, className: "hermes-workflows-input-field-row" },
          h("span", null, safeString(row.name)),
          h("span", { className: "hermes-workflows-badge" }, safeString(row.kind)),
          h("span", { className: "hermes-workflows-meta" }, row.required ? "required" : "optional"),
          h("button", { type: "button", onClick: function () { props.removeTriggerInputField(row.name); } }, "Remove")
        );
      })) : h("p", { className: "hermes-workflows-muted" }, "No input fields yet. Add fields below, then Apply."),
      h("div", { className: "hermes-workflows-input-field-editor" },
        h("label", null,
          h("span", { className: "hermes-workflows-muted" }, "Input field name"),
          h("input", { value: props.triggerInputName, onChange: function (event) { props.setTriggerInputName(event.target.value); }, placeholder: "repo_path" })
        ),
        h("label", null,
          h("span", { className: "hermes-workflows-muted" }, "Input field kind"),
          h("select", { value: props.triggerInputKind, onChange: function (event) { props.setTriggerInputKind(event.target.value); } }, SCALAR_INPUT_KINDS.map(function (kind) {
            return h("option", { key: kind, value: kind }, kind);
          }))
        ),
        h("label", { className: "hermes-workflows-run-advanced-toggle" },
          h("input", { type: "checkbox", checked: props.triggerInputRequired, onChange: function (event) { props.setTriggerInputRequired(event.target.checked); } }),
          h("span", null, "Required input")
        ),
        h("label", null,
          h("span", { className: "hermes-workflows-muted" }, "Default value"),
          h("input", { value: props.triggerInputDefault, onChange: function (event) { props.setTriggerInputDefault(event.target.value); }, placeholder: "optional" })
        ),
        h("label", null,
          h("span", { className: "hermes-workflows-muted" }, "Min length"),
          h("input", { type: "number", min: "0", value: props.triggerInputMinLength, onChange: function (event) { props.setTriggerInputMinLength(event.target.value); }, placeholder: "0" })
        ),
        h("label", null,
          h("span", { className: "hermes-workflows-muted" }, "Max length"),
          h("input", { type: "number", min: "0", value: props.triggerInputMaxLength, onChange: function (event) { props.setTriggerInputMaxLength(event.target.value); }, placeholder: "optional" })
        ),
        h("button", { type: "button", onClick: props.addTriggerInputFieldFromUi }, "Add input field")
      )
    ),
    h("div", { className: "hermes-workflows-trigger-editor", "aria-label": "Intake mode" },
      h("strong", null, "Intake mode"),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Mode"),
        h("select", { value: props.triggerIntakeMode, onChange: function (event) { props.setTriggerIntakeMode(event.target.value); } },
          h("option", { value: "single" }, "single"),
          h("option", { value: "continuous" }, "continuous")
        )
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Dedupe key"),
        h("input", { value: props.triggerDedupeKey, onChange: function (event) { props.setTriggerDedupeKey(event.target.value); }, placeholder: "$.input.repo_path" })
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "Ready when field path"),
        h("input", { value: props.triggerReadyPath, onChange: function (event) { props.setTriggerReadyPath(event.target.value); }, placeholder: "$.input.repo_path" })
      )
    )
  );
}

function renderSwitchInspector(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Default target cell"),
      h("input", { value: props.switchDefault, onChange: function (event) { props.setSwitchDefault(event.target.value); }, placeholder: "Optional target id; or connect from switch.default" })
    ),
    h("div", { className: "hermes-workflows-meta" }, "Switch cases: " + (props.switchCases.length ? props.switchCases.map(function (item) { return item && item.name; }).filter(Boolean).join(", ") : "none yet")),
    h("div", { className: "hermes-workflows-row" },
      h("input", { value: props.switchCaseName, onChange: function (event) { props.setSwitchCaseName(event.target.value); }, placeholder: "Case name, e.g. approved" }),
      h("input", { value: props.switchCasePath, onChange: function (event) { props.setSwitchCasePath(event.target.value); }, placeholder: "$.input.status" }),
      h("input", { value: props.switchCaseEquals, onChange: function (event) { props.setSwitchCaseEquals(event.target.value); }, placeholder: "Equals value" }),
      h("button", { type: "button", onClick: props.addSwitchCaseFromUi }, "Add case")
    )
  );
}

function renderWaitInspector(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Wait seconds"),
      h("input", { value: props.cellSeconds, onChange: function (event) { props.setCellSeconds(event.target.value); }, placeholder: "60" })
    )
  );
}

function renderPassFailInspector(props, kind) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, kind === "fail" ? "Failure message" : "Output text"),
      h("textarea", { className: "hermes-workflows-prompt-editor", value: props.cellOutputText, onChange: function (event) { props.setCellOutputText(event.target.value); }, placeholder: kind === "fail" ? "Why this workflow should fail." : "Optional output text for this cell." })
    )
  );
}

function renderSendMessageInspector(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Platform"),
      h("select", { value: props.sendMessagePlatform || "auto", onChange: function (event) { props.setSendMessagePlatform(event.target.value); } },
        ["auto", "discord", "telegram", "slack"].map(function (platform) {
          return h("option", { key: platform, value: platform }, platform);
        })
      )
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Target (channel/chat ID)"),
      h("input", { value: props.sendMessageTarget || "", onChange: function (event) { props.setSendMessageTarget(event.target.value); }, placeholder: "channel or chat ID" })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Message text"),
      h("textarea", { className: "hermes-workflows-prompt-editor", value: props.sendMessageText || "", onChange: function (event) { props.setSendMessageText(event.target.value); }, placeholder: "Message to send" })
    )
  );
}

function renderSubworkflowInspector(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Workflow reference ID"),
      h("input", { value: props.subworkflowRef || "", onChange: function (event) { props.setSubworkflowRef(event.target.value); }, placeholder: "workflow_id" })
    ),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Input mapping JSON"),
      h("textarea", { className: "hermes-workflows-contract-editor", value: props.subworkflowInputMappingText || "{}", onChange: function (event) { props.setSubworkflowInputMappingText(event.target.value); } })
    )
  );
}

function renderMinimalInspector(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("p", { className: "hermes-workflows-muted" }, "Connect incoming and outgoing edges on the canvas."),
    h("label", null,
      h("span", { className: "hermes-workflows-muted" }, "Notes"),
      h("textarea", { className: "hermes-workflows-prompt-editor", value: props.promptText, onChange: function (event) { props.setPromptText(event.target.value); }, placeholder: "Optional notes for this cell." })
    )
  );
}

function renderTypeSelector(props, kind) {
  var h = props.createElement;
  if (props.selectedNode.specKind === "trigger") return null;
  return h("label", null,
    h("span", { className: "hermes-workflows-muted" }, "Cell type"),
    h("select", {
      value: props.cellType,
      "aria-label": "Change selected cell type",
      onChange: function (event) {
        var nextType = event.target.value;
        var spec = typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
        if (!spec || !props.selectedNode) {
          props.setCellType(nextType);
          return;
        }
        var preview = changeNodeType(spec, props.selectedNode.id, nextType);
        if (preview.removedFields.length > 0 && typeof window !== "undefined" && typeof window.confirm === "function") {
          var message = "Changing to " + nextType + " will remove: " + preview.removedFields.join(", ") + ". Continue?";
          if (!window.confirm(message)) return;
        }
        props.setCellType(nextType);
        if (props.setDraftSpec) props.setDraftSpec(preview.spec);
        if (props.updateEditorText) props.updateEditorText(specToEditorText(preview.spec));
        if (props.setSelectedDefinition) props.setSelectedDefinition(Object.assign({}, props.selectedDefinition || {}, { spec: preview.spec }));
        var nextNode = findSpecNode(preview.spec, props.selectedNode.id);
        if (nextNode && props.setSelectedNode) {
          props.setSelectedNode(Object.assign({}, nextNode, { id: props.selectedNode.id, specKind: props.selectedNode.specKind }));
          if (props.setNodeJson) props.setNodeJson(jsonBlock(Object.assign({}, nextNode, { id: props.selectedNode.id })));
        }
        if (props.setNodeMessage) props.setNodeMessage(preview.removedFields.length ? "Changed type to " + nextType + "; removed " + preview.removedFields.join(", ") + "." : "Changed type to " + nextType + ".");
      },
    }, SUPPORTED_NODES.map(function (type) {
      return h("option", { key: type, value: type }, type);
    }))
  );
}

export function renderInspector(props) {
  var h = props.createElement;
  if (!props.selectedNode) {
    return h("aside", { className: "hermes-workflows-inspector" },
      h("h3", null, "Properties"),
      h("p", { className: "hermes-workflows-muted" }, "Choose a node from the palette, select it on the canvas, then configure it in Properties.")
    );
  }

  var spec = typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
  var kind = props.selectedNode.specKind === "trigger" ? "trigger" : (props.selectedNode.type || props.cellType || "pass");
  var body;
  if (kind === "agent_task") body = renderAgentTaskInspector(props);
  else if (kind === "trigger") body = renderTriggerInspector(props);
  else if (kind === "switch") body = renderSwitchInspector(props);
  else if (kind === "wait") body = renderWaitInspector(props);
  else if (kind === "pass" || kind === "fail") body = renderPassFailInspector(props, kind);
  else if (kind === "send_message") body = renderSendMessageInspector(props);
  else if (kind === "subworkflow") body = renderSubworkflowInspector(props);
  else body = renderMinimalInspector(props);

  var applyHandler = kind === "agent_task" ? props.applyAgentCellForm : props.applyBasicCellForm;
  return h("aside", { className: "hermes-workflows-inspector" },
    h("h3", null, "Properties"),
    h("div", { className: "hermes-workflows-stack" },
      h("div", { className: "hermes-workflows-inspector-header" },
        h("strong", null, safeString(props.selectedNode.id)),
        h("span", { className: "hermes-workflows-type-badge", style: { backgroundColor: NODE_COLORS[kind] || "#64748b" } }, kind)
      ),
      h("label", null,
        h("span", { className: "hermes-workflows-muted" }, "ID"),
        h("input", { value: props.cellId, onChange: function (event) { props.setCellId(event.target.value); }, placeholder: "cell-id" })
      ),
      renderTypeSelector(props, kind),
      body,
      h("div", { className: "hermes-workflows-row" },
        h("button", { type: "button", onClick: applyHandler, className: "hermes-workflows-primary" }, "Apply"),
        h("button", { type: "button", onClick: props.deleteSelectedCell }, "Delete"),
        h("button", { type: "button", onClick: function () { props.setAdvancedJsonOpen(!props.advancedJsonOpen); } }, props.advancedJsonOpen ? "Hide JSON" : "Advanced JSON")
      ),
      props.advancedJsonOpen ? renderAdvancedNodeJson(props, spec) : null,
      props.nodeMessage ? h("p", { className: "hermes-workflows-muted" }, props.nodeMessage) : null
    )
  );
}
