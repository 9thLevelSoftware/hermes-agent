(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG || typeof REG.register !== "function") return;

  const React = SDK.React;
  const h = React.createElement;
  const Card = SDK.components && SDK.components.Card ? SDK.components.Card : "section";

  function WorkflowsPage() {
    return h(Card, { className: "p-6 space-y-3" },
      h("h2", { className: "text-xl font-semibold" }, "Workflows"),
      h("p", { className: "text-sm text-muted-foreground" },
        "Workflow backend API is available; UI coming next task."
      ),
      h("ul", { className: "list-disc pl-5 text-sm text-muted-foreground" },
        h("li", null, "Definitions: /api/plugins/workflows/definitions"),
        h("li", null, "Executions: /api/plugins/workflows/executions")
      )
    );
  }

  // ponytail: placeholder only; Task 14 owns the real MVP UI.
  REG.register("workflows", WorkflowsPage);
})();
