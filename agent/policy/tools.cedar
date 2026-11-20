// The agent's tool policy, deny by default: Cedar allows a call only when a permit below matches
// it and no forbid does, so a tool no permit names is denied.
// Each permit names one read-only tool of tools.cedarschema.

// Running a governed metric's query is permitted to the personas of access/personas.yaml that hold
// a read or aggregate grant on a data product holding figures (payments, ledger, group totals);
// the metric definitions and the audit log hold none, so a grant on them alone does not count.
// Which rows a query returns is the warehouse's grants' business, not this policy's.
// tests/agent/test_tool_policy.py holds this list equal to the one the catalogue gives.
@id("run-metric-query")
permit (
  principal is Persona,
  action == Action::"run_metric_query",
  resource is Metric
)
when
{
  principal in
    [Persona::"accountant-chicago",
     Persona::"accountant-uk-transport",
     Persona::"accountant-nl-government",
     Persona::"group-controller",
     Persona::"payables-clerk",
     Persona::"approver",
     Persona::"auditor",
     Persona::"metric-owner"]
};
