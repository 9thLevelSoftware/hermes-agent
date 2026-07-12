import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";
import { renderPalette, renderWorkflowOnboarding } from "./palette.js";

const require = createRequire(import.meta.url);
const reactDomRequire = createRequire(require.resolve("react-dom/client"));
const React = reactDomRequire("react");
const { act } = React;
const { createRoot } = reactDomRequire("react-dom/client");
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};
const fakeReact = { Fragment: "Fragment", useState: (initial) => [initial, noop] };

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React: fakeReact,
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

  it("keeps the sidebar mounted across the onboarding hook transition", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    function Host() {
      const [hasSpec, setHasSpec] = React.useState(false);
      const startBlankWorkflow = () => setHasSpec(true);
      const sidebarProps = props({
        React,
        createElement: React.createElement,
        activeSpec: hasSpec ? { id: "workflow" } : null,
        startBlankWorkflow,
      });
      const onboardingProps = props({
        React,
        createElement: React.createElement,
        startBlankWorkflow,
      });
      return React.createElement(
        React.Fragment,
        null,
        renderPalette(sidebarProps),
        hasSpec === false ? renderWorkflowOnboarding(onboardingProps) : null,
      );
    }

    try {
      await act(async () => root.render(React.createElement(Host)));
      const startButton = container.querySelector(".hermes-workflows-palette-creation button[aria-label='Start from scratch']");
      expect(startButton).not.toBeNull();
      expect(container.querySelector(".hermes-workflows-onboarding-form")).not.toBeNull();
      let updateError;
      try {
        await act(async () => startButton.click());
      } catch (error) {
        updateError = error;
      }
      expect(updateError).toBeUndefined();
      expect(container.querySelector(".hermes-workflows-sidebar")).not.toBeNull();
      expect(container.querySelector(".hermes-workflows-onboarding-form")).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
