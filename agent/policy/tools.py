"""The tool policy: whether an identity may call one of the agent's tools on a resource.

`authorize(principal, action, resource, context)` is the seam a caller asks before a tool call.
Behind it Cedar, through the `cedarpy` bindings in this process, evaluates the policies of
`tools.cedar` against the schema of `tools.cedarschema`, both beside this module; the policies are
validated against the schema when they load, and each request is checked against the schema before
any policy is evaluated.

A `Decision` is allowed only when all of these hold: the engine answers `Allow`, its diagnostics
carry no error, and it names at least one policy as its reason. Every other outcome is a denial
carrying its reasons, including:
- the engine's `Deny`: no permit matches, or a forbid does;
- a request the schema refuses (the engine's `NoDecision`): an action or entity type it does not
  declare, an entity type the action does not apply to, a context of another shape;
- an argument of the wrong Python type;
- policies or a schema that do not parse, do not validate or cannot be read;
- an `Exception` the engine raises, or an answer of a shape this module does not know.

Limits: an exception outside `Exception` (`KeyboardInterrupt`, `SystemExit`) propagates; entities
go to the engine with no attributes and no parents, so no policy can read either; which identities
and resources exist is not checked here, only their types.
"""

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import cedarpy

POLICIES = Path(__file__).with_name("tools.cedar")
SCHEMA = Path(__file__).with_name("tools.cedarschema")


@dataclass(frozen=True)
class Entity:
    """A Cedar entity by its type and id: `Entity("Persona", "auditor")`."""

    type: str
    id: str


@dataclass(frozen=True)
class Decision:
    """The answer to one request: allowed or not, the policies that decided it, and why.

    `policies` names each determining policy by its `@id` annotation where it has one; `reasons`
    holds the engine's errors or this module's explanation of a denial. Its truth value is
    `allowed`, so `if decision:` reads as "if allowed".
    """

    allowed: bool
    policies: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.allowed


class PolicyInvalid(ValueError):
    """Policies that do not validate against the schema."""


def _deny(*reasons: str, policies: tuple[str, ...] = ()) -> Decision:
    return Decision(allowed=False, policies=policies, reasons=reasons)


def _entity(value: object, role: str) -> dict[str, str]:
    if not (
        isinstance(value, Entity) and isinstance(value.type, str) and isinstance(value.id, str)
    ):
        raise TypeError(f"the {role} must be an Entity of two strings, not {value!r}")
    return {"type": value.type, "id": value.id}


class ToolPolicy:
    """A loaded policy set and its schema; raises on policies that do not load or validate."""

    def __init__(self, policies: str, schema: str) -> None:
        validation = cedarpy.validate_policies(policies, schema)
        if not validation.validation_passed:
            errors = "; ".join(str(error) for error in validation.errors) or "no error given"
            raise PolicyInvalid(f"the policies do not validate against the schema: {errors}")
        self._policies = cedarpy.PolicySet.from_str(policies)
        self._schema = cedarpy.Schema.from_str(schema)

    @classmethod
    def from_files(cls, policies: Path | None = None, schema: Path | None = None) -> "ToolPolicy":
        """The policies and schema read from files, by default `tools.cedar` and its schema."""
        policies_text = (policies or POLICIES).read_text(encoding="utf-8")
        schema_text = (schema or SCHEMA).read_text(encoding="utf-8")
        return cls(policies_text, schema_text)

    def authorize(
        self, principal: Entity, action: str, resource: Entity, context: Mapping[str, object]
    ) -> Decision:
        """Whether `principal` may call the tool `action` on `resource`; any failure is a denial."""
        try:
            return self._decide(principal, action, resource, context)
        except Exception as error:
            return _deny(f"the engine raised {type(error).__name__}: {error}")

    def _decide(
        self, principal: object, action: object, resource: object, context: object
    ) -> Decision:
        if not isinstance(action, str):
            raise TypeError(f"the action must be a string, not {action!r}")
        if not isinstance(context, Mapping):
            raise TypeError(f"the context must be a mapping, not {context!r}")
        request = {
            "principal": _entity(principal, "principal"),
            "action": {"type": "Action", "id": action},
            "resource": _entity(resource, "resource"),
            "context": dict(context),
        }
        result = cedarpy.is_authorized(request, self._policies, [], schema=self._schema)
        diagnostics = result.diagnostics
        errors = tuple(str(error) for error in diagnostics.errors)
        reasons = tuple(str(reason) for reason in diagnostics.reasons)
        named = tuple(str(diagnostics.id_annotations_by_reason.get(r, r)) for r in reasons)
        decision = result.decision
        if decision is not cedarpy.Decision.Allow:
            if errors:
                return _deny(*errors, policies=named)
            if decision is cedarpy.Decision.Deny and named:
                return _deny("a forbid matched", policies=named)
            return _deny(f"no policy permits {action} ({decision.value})", policies=named)
        if errors:
            return _deny("the engine allowed with errors", *errors, policies=named)
        if not named:
            return _deny("the engine allowed with no policy as its reason")
        return Decision(allowed=True, policies=named)


@functools.cache
def default_policy() -> ToolPolicy:
    """The policy of `tools.cedar`, loaded once; a load that raises is tried again next call."""
    return ToolPolicy.from_files()


def authorize(
    principal: Entity, action: str, resource: Entity, context: Mapping[str, object]
) -> Decision:
    """Whether `principal` may call the tool `action` on `resource`, under `tools.cedar`.

    Deny by default: see the module's docstring for what comes back as allowed.
    """
    try:
        policy = default_policy()
    except Exception as error:
        return _deny(f"the tool policy did not load: {type(error).__name__}: {error}")
    return policy.authorize(principal, action, resource, context)
