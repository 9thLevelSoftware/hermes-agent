from hermes_cli.workflows_expr import eval_condition, resolve_path


def test_resolve_path_nested_dict_and_list():
    data = {"node": {"review": {"output": {"items": [{"score": 0.9}]}}}}
    assert resolve_path(data, "$.node.review.output.items[0].score") == 0.9


def test_eval_condition_boolean_tree():
    data = {"review": {"verdict": "approved", "confidence": 0.92}}
    cond = {
        "op": "and",
        "args": [
            {"op": "eq", "left": {"path": "$.review.verdict"}, "right": "approved"},
            {"op": "gte", "left": {"path": "$.review.confidence"}, "right": 0.8},
        ],
    }
    assert eval_condition(cond, data) is True


def test_eval_condition_missing_path_is_false_for_comparison():
    assert eval_condition({"op": "eq", "left": {"path": "$.missing"}, "right": 1}, {}) is False


def test_eval_condition_rejects_unknown_op():
    try:
        eval_condition({"op": "exec", "code": "print('nope')"}, {})
    except ValueError as exc:
        assert "unsupported condition op" in str(exc)
    else:
        raise AssertionError("expected ValueError")
