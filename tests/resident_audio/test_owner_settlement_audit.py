from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


PACKAGE = (
    Path(__file__).parents[2]
    / "app"
    / "service"
    / "analysis"
    / "resident_audio"
)


@dataclass(frozen=True)
class CallSite:
    path: str
    method: str | None
    receiver: str
    line: int
    parent_kind: str


def _trees(source_by_name: dict[str, str] | None = None):
    if source_by_name is not None:
        for name, source in source_by_name.items():
            yield name, ast.parse(source)
        return
    for path in sorted(PACKAGE.glob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _enclosing_method(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> str | None:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        parent = parents.get(parent)
    return None


def _calls(
    attribute: str, source_by_name: dict[str, str] | None = None
) -> list[CallSite]:
    found = []
    for name, tree in _trees(source_by_name):
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attribute
            ):
                continue
            parent = parents.get(node)
            found.append(
                CallSite(
                    path=name,
                    method=_enclosing_method(node, parents),
                    receiver=ast.unparse(node.func.value),
                    line=node.lineno,
                    parent_kind=type(parent).__name__,
                )
            )
    return found


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        (
            "remove_owner",
            [
                (
                    "owner_settlement.py",
                    "_advance_finality_admission",
                    "self._finality",
                )
            ],
        ),
        (
            "acknowledge_finality",
            [
                (
                    "owner_settlement.py",
                    "_advance_acknowledgement",
                    "self._finality",
                )
            ],
        ),
        (
            "prune_finality",
            [
                (
                    "owner_settlement.py",
                    "_advance_prune",
                    "self._finality",
                )
            ],
        ),
    ],
    ids=["remove", "acknowledge", "prune"],
)
def test_stream_finality_calls_have_exact_owner_receiver_and_method(
    attribute, expected
):
    actual = [
        (site.path, site.method, site.receiver)
        for site in _calls(attribute)
    ]
    assert actual == expected


@pytest.mark.parametrize(
    ("attribute", "method"),
    [
        ("_acknowledge_entry", "_dispatch_acknowledge_finality"),
        ("_prune_entry", "_dispatch_prune_finality"),
    ],
)
def test_private_ledger_dispatch_has_one_exact_stream_identity_receiver(
    attribute, method
):
    actual = [
        (site.path, site.method, site.receiver)
        for site in _calls(attribute)
    ]
    assert actual == [("stream_identity.py", method, "self._owner_finality")]


def _exact_removal_flow(source: str) -> bool:
    tree = ast.parse(source)
    parents = _parents(tree)
    removal_calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "remove_owner"
        )
    ]
    if len(removal_calls) != 1:
        return False
    removal = removal_calls[0]
    assignment = parents.get(removal)
    if not (
        isinstance(assignment, ast.Assign)
        and len(assignment.targets) == 1
        and isinstance(assignment.targets[0], ast.Name)
    ):
        return False
    bound_name = assignment.targets[0].id
    method = _enclosing_method(removal, parents)
    validations = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "exact_settlement_removal"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == bound_name
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == "claim"
            and _enclosing_method(node, parents) == method
        )
    ]
    return len(validations) == 1


def test_removal_receipt_is_bound_and_flows_through_exact_claim_validation():
    source = (PACKAGE / "owner_settlement.py").read_text(encoding="utf-8")
    assert _exact_removal_flow(source)


@pytest.mark.parametrize(
    "source",
    [
        """
def _advance_finality_admission(self, claim):
    self._finality.remove_owner(claim)
""",
        """
def _advance_finality_admission(self, claim):
    receipt = self._finality.remove_owner(claim)
    return bool(receipt)
""",
        """
def _advance_finality_admission(self, claim):
    receipt = self._finality.remove_owner(claim)
    return exact_settlement_removal(receipt, other_claim)
""",
    ],
)
def test_removal_flow_audit_rejects_ignored_or_nonexact_receipts(source):
    assert not _exact_removal_flow(source)


def test_only_coordinator_adapter_and_ownerless_helper_call_terminal_primitive():
    sites = [
        (site.path, site.method, site.receiver)
        for site in _calls("_terminalize_locked")
    ]
    assert sites == [
        (
            "pool_settlement.py",
            "_ownerless_reserved_terminal_locked",
            "self",
        ),
    ]


def test_post_owner_settled_true_assignments_have_exact_approved_enclosures():
    actual = []
    for path, tree in _trees():
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            ):
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if any(
                isinstance(target, ast.Attribute)
                and target.attr == "settled"
                for target in targets
            ):
                actual.append((path, _enclosing_method(node, parents)))
    assert actual == [
        ("pool.py", "_terminalize_locked"),
        ("pool_settlement.py", "terminal_cas"),
    ]


def _function(path: str, name: str) -> ast.FunctionDef | None:
    tree = ast.parse((PACKAGE / path).read_text(encoding="utf-8"))
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def test_ownerless_helper_asserts_every_forbidden_resource_absent():
    helper = _function(
        "pool_settlement.py", "_ownerless_reserved_terminal_locked"
    ) if (PACKAGE / "pool_settlement.py").exists() else None
    assert helper is not None
    asserted = {
        child.left.attr
        for node in helper.body
        if isinstance(node, ast.Assert)
        for child in ast.walk(node.test)
        if (
            isinstance(child, ast.Compare)
            and isinstance(child.left, ast.Attribute)
            and len(child.ops) == 1
            and isinstance(child.ops[0], ast.Is)
            and len(child.comparators) == 1
            and isinstance(child.comparators[0], ast.Constant)
            and child.comparators[0].value is None
        )
    }
    assert asserted == {
        "owner_instance_id",
        "owner",
        "writer",
        "stream",
        "artifact",
        "settlement_claim",
    }


ENTRY_INTENT_ROUTES = [
    (
        "pool_commit.py",
        "commit",
        "_rollback_commit",
        "PREPARE_ROLLBACK",
        "keyword",
    ),
    (
        "pool_commit.py",
        "cancel",
        "_cleanup_committed",
        "CLIENT_CANCEL",
        "keyword",
    ),
    (
        "pool_commit.py",
        "_execute_owner_terminal",
        "_submit_settlement",
        "OWNER_SUCCESS",
        "local",
    ),
    (
        "pool_commit.py",
        "_execute_owner_terminal",
        "_submit_settlement",
        "OWNER_FAILURE",
        "local",
    ),
    (
        "pool_reconcile.py",
        "_stream_failed",
        "_claim_stream_failure_settlements",
        "STREAM_FAILURE",
        "positional",
    ),
    (
        "pool.py",
        "_run_shutdown",
        "_cleanup_committed",
        "POOL_SHUTDOWN",
        "keyword",
    ),
]


def _is_intent(node: ast.AST, intent: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == intent
        and isinstance(node.value, ast.Name)
        and node.value.id == "SettlementIntent"
    )


def _intent_route_exists(
    source: str,
    *,
    method: str,
    call_attribute: str,
    intent: str,
    binding: str,
) -> bool:
    tree = ast.parse(source)
    parents = _parents(tree)
    calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == call_attribute
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and _enclosing_method(node, parents) == method
        )
    ]
    if len(calls) != 1:
        return False
    call = calls[0]
    if binding == "keyword":
        return any(
            keyword.arg == "intent" and _is_intent(keyword.value, intent)
            for keyword in call.keywords
        )
    if binding == "positional":
        return any(_is_intent(argument, intent) for argument in call.args)
    if binding != "local":
        return False
    method_node = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == method
        ),
        None,
    )
    return (
        method_node is not None
        and any(_is_intent(node, intent) for node in ast.walk(method_node))
        and any(
            isinstance(argument, ast.Name) and argument.id == "intent"
            for argument in call.args
        )
    )


@pytest.mark.parametrize(
    ("path", "method", "call_attribute", "intent", "binding"),
    ENTRY_INTENT_ROUTES,
    ids=[route[3] for route in ENTRY_INTENT_ROUTES],
)
def test_every_real_entry_uses_exact_receiver_enclosure_and_intent(
    path, method, call_attribute, intent, binding
):
    source = (PACKAGE / path).read_text(encoding="utf-8")
    assert _intent_route_exists(
        source,
        method=method,
        call_attribute=call_attribute,
        intent=intent,
        binding=binding,
    )


def test_intent_route_audit_rejects_enum_presence_without_exact_submission():
    source = """
def unrelated(self):
    marker = SettlementIntent.PREPARE_ROLLBACK
    other._rollback_commit(record, intent=marker)
"""
    assert not _intent_route_exists(
        source,
        method="commit",
        call_attribute="_rollback_commit",
        intent="PREPARE_ROLLBACK",
        binding="keyword",
    )


AGGREGATE_NAMES = {"owner_count", "owners", "registrations"}


def _aggregate_finality_violations(source: str) -> list[int]:
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp, ast.Assert)):
            continue
        if any(
            isinstance(child, ast.Attribute)
            and child.attr in AGGREGATE_NAMES
            for child in ast.walk(node.test)
        ):
            violations.append(node.lineno)
    return sorted(set(violations))


def test_aggregate_finality_audit_rejects_negative_source_snippet():
    source = """
def settle(stream):
    if stream.snapshot().owner_count == 0:
        terminalize()
"""
    assert _aggregate_finality_violations(source) == [3]


def test_no_settlement_reachable_code_uses_aggregate_owner_proof():
    checked = (
        "owner_settlement.py",
        "pool_settlement.py",
        "pool_commit.py",
        "pool_cleanup.py",
        "pool_control.py",
        "pool_reconcile.py",
        "pool.py",
    )
    violations = {}
    for name in checked:
        path = PACKAGE / name
        if not path.exists():
            continue
        lines = _aggregate_finality_violations(
            path.read_text(encoding="utf-8")
        )
        if lines:
            violations[name] = lines
    assert violations == {}
