import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const sourceDir = path.dirname(fileURLToPath(import.meta.url));
const appSource = fs.readFileSync(path.join(sourceDir, "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(sourceDir, "style.css"), "utf8");

describe("workflow canvas workspace layout", () => {
  it("gives the Build panel and canvas a full-height grid-zone chain", () => {
    expect(cssSource).toContain(".hermes-workflows-canvas-zone > .hermes-workflows-build-mode");
    expect(cssSource).toContain("grid-row: 1;");
    expect(cssSource).toContain("minmax(0, 1fr)");
  });

  it("renders an intentional square grid through CSS rather than a competing flow background", () => {
    expect(cssSource).toContain("linear-gradient(to right");
    expect(cssSource).toContain("linear-gradient(to bottom");
  });

  it("fits only after the rendered flow-node membership is available", () => {
    expect(appSource).toContain("const key = flowNodes.map");
    expect(appSource).toContain("requestAnimationFrame");
    expect(appSource).toContain("membershipKeyRef.current = key;");
  });

  it("converts the palette drop point into flow coordinates before creating a node", () => {
    expect(appSource).toContain("screenToFlowPosition");
    expect(appSource).toContain("addWorkflowCellAtPosition(type, dropPosition)");
    expect(appSource).toContain("function addWorkflowCellAtPosition(type, position)");
  });
});
