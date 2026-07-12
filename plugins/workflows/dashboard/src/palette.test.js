import { describe, expect, it } from "vitest";
import { renderPalette, renderWorkflowOnboarding } from "./palette.js";

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};
const React = { Fragment: "Fragment", useState: (initial) => [initial, noop] };

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React,
    activeSpec: null,
    goalText: "",
    setGoalText: noop,
    newWorkflowName: "",
    setNewWorkflowName: noop,
    draftFromGoal: noop,
    drafting: false,
    startBlankWorkflow: noop,
    refineWorkflow: noop,
    refining: false,
    refineText: "",
    setRefineText: noop,
    draftResult: null,
    candidateSource: null,
    acceptDraftCandidate: noop,
    rejectDraftCandidate: noop,
    definitions: [],
    selectedDefinition: null,
    loadDefinition: () => Promise.resolve(),
    executions: [],
    loadExecution: () => Promise.resolve(),
    addTriggerOfType: noop,
    addWorkflowCellOfType: noop,
  }, overrides);
}

function contains(node, predicate) {
  if (Array.isArray(node)) return node.some((child) => contains(child, predicate));
  if (!node || typeof node !== "object") return predicate(node);
  return predicate(node) || contains(node.children, predicate);
}

describe("renderPalette", () => {
  it("returns an element with a palette class", () => {
    const node = renderPalette(props());
    expect(node.props.className).toContain("palette");
  });

  it("shows the workflow creation form when no workflow is active", () => {
    const node = renderPalette(props({ activeSpec: null }));
    expect(contains(node, (value) => value === "Generate From Prompt")).toBe(true);
    expect(contains(node, (value) => value === "Start From Scratch")).toBe(true);
  });

  it("renders hook-free onboarding with visible accessible field labels", () => {
    const node = renderWorkflowOnboarding(props({ activeSpec: null }));
    expect(node.props.className).toContain("hermes-workflows-onboarding-form");
    expect(contains(node, (value) => value === "Workflow name")).toBe(true);
    expect(contains(node, (value) => value === "Describe workflow goal")).toBe(true);
    expect(contains(node, (value) => value === "Generate From Prompt")).toBe(true);
    expect(contains(node, (value) => value === "Start From Scratch")).toBe(true);
  });

  it("hides only the sidebar creation form when requested", () => {
    const node = renderPalette(props({
      hideWorkflowForm: true,
      definitions: [{ id: "existing", name: "Existing workflow", enabled: true }],
      executions: [{ id: "execution-1", status: "succeeded" }],
    }));
    const json = JSON.stringify(node);
    expect(json).not.toContain("Generate From Prompt");
    expect(json).not.toContain("Describe workflow goal");
    expect(json).toContain("Existing workflow");
    expect(json).toContain("Executions");
  });
});
