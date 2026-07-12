import { describe, expect, it } from "vitest";
import { renderInspector } from "./inspector.js";

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React: { Fragment: "Fragment" },
    selectedNode: null,
    cellId: "node",
    setCellId: noop,
    cellType: "pass",
    setCellType: noop,
    activeSpec: () => ({ nodes: {} }),
    agentProfile: "",
    setAgentProfile: noop,
    agentProvider: "",
    setAgentProvider: noop,
    agentModel: "",
    setAgentModel: noop,
    agentTitle: "",
    setAgentTitle: noop,
    promptText: "",
    setPromptText: noop,
    resultContractText: "{}",
    setResultContractText: noop,
    agentRoutingOptions: { profiles: [], providers: [] },
    promptAssistantOpen: false,
    setPromptAssistantOpen: noop,
    promptAssistantGoal: "",
    setPromptAssistantGoal: noop,
    promptAssistantObjective: "",
    setPromptAssistantObjective: noop,
    promptAssistantContext: "",
    setPromptAssistantContext: noop,
    promptAssistantOutput: "{}",
    setPromptAssistantOutput: noop,
    promptAssistantConstraints: "",
    setPromptAssistantConstraints: noop,
    promptAssistantAdvanced: false,
    setPromptAssistantAdvanced: noop,
    draftPromptWithAssistant: noop,
    triggerInputRows: [],
    setTriggerInputRows: noop,
    triggerInputName: "",
    setTriggerInputName: noop,
    triggerInputKind: "text",
    setTriggerInputKind: noop,
    triggerInputRequired: true,
    setTriggerInputRequired: noop,
    triggerInputDefault: "",
    setTriggerInputDefault: noop,
    triggerInputMinLength: "",
    setTriggerInputMinLength: noop,
    triggerInputMaxLength: "",
    setTriggerInputMaxLength: noop,
    triggerIntakeMode: "single",
    setTriggerIntakeMode: noop,
    triggerDedupeKey: "",
    setTriggerDedupeKey: noop,
    triggerReadyPath: "",
    setTriggerReadyPath: noop,
    triggerSchedule: "",
    setTriggerSchedule: noop,
    addTriggerInputFieldFromUi: noop,
    removeTriggerInputField: noop,
    switchDefault: "",
    setSwitchDefault: noop,
    switchCases: [],
    setSwitchCases: noop,
    switchCaseName: "",
    setSwitchCaseName: noop,
    switchCasePath: "$.input.status",
    setSwitchCasePath: noop,
    switchCaseEquals: "",
    setSwitchCaseEquals: noop,
    addSwitchCaseFromUi: noop,
    cellSeconds: "60",
    setCellSeconds: noop,
    cellOutputText: "",
    setCellOutputText: noop,
    sendMessagePlatform: "auto",
    setSendMessagePlatform: noop,
    sendMessageText: "",
    setSendMessageText: noop,
    sendMessageTarget: "",
    setSendMessageTarget: noop,
    subworkflowRef: "",
    setSubworkflowRef: noop,
    subworkflowInputMappingText: "{}",
    setSubworkflowInputMappingText: noop,
    applyAgentCellForm: noop,
    applyBasicCellForm: noop,
    deleteSelectedCell: noop,
    advancedJsonOpen: false,
    setAdvancedJsonOpen: noop,
    nodeJson: "{}",
    setNodeJson: noop,
    applyNodeJson: noop,
    useJsonDraft: noop,
    nodeMessage: "",
    setNodeMessage: noop,
    setDraftSpec: noop,
    setSelectedDefinition: noop,
  }, overrides);
}

describe("renderInspector", () => {
  it("renders an empty state without a selected node", () => {
    const output = renderInspector(props());
    expect(output.props.className).toBe("hermes-workflows-inspector");
    expect(JSON.stringify(output)).toContain("Choose a node");
  });

  it("dispatches agent_task nodes to the agent renderer", () => {
    const output = renderInspector(props({ selectedNode: { id: "agent", type: "agent_task", specKind: "node" } }));
    expect(JSON.stringify(output)).toContain("Assigned profile");
  });

  it("dispatches trigger nodes to the trigger renderer", () => {
    const output = renderInspector(props({ selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" } }));
    expect(JSON.stringify(output)).toContain("Input schema");
  });
});
