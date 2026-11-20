// The agent's tool policy, deny by default: Cedar allows a call only when a permit below matches
// it and no forbid does, so a tool no permit names is denied.
// Each permit names one read-only tool of tools.cedarschema.

// Any persona may run a governed metric's query; which rows it returns is the warehouse's grants'
// business, not this policy's.
@id("run-metric-query")
permit (
  principal is Persona,
  action == Action::"run_metric_query",
  resource is Metric
);
