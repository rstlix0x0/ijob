from ijob_ai.tracing import attributes


def test_attribute_names() -> None:
    assert attributes.PROVIDER_NAME == "gen_ai.provider.name"
    assert attributes.OPERATION_NAME == "gen_ai.operation.name"
    assert attributes.REQUEST_MODEL == "gen_ai.request.model"
    assert attributes.RESPONSE_MODEL == "gen_ai.response.model"
    assert attributes.INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert attributes.OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
    assert attributes.COST == "gen_ai.usage.cost"
    assert attributes.TASK == "ijob.task"
    assert attributes.TASK_VERSION == "ijob.task_version"
    assert attributes.POSTING_ID == "ijob.posting_id"
    assert attributes.OUTCOME == "ijob.outcome"
    assert attributes.PROMPT == "ijob.prompt"
    assert attributes.COMPLETION == "ijob.completion"


def test_provider_names() -> None:
    assert dict(attributes.PROVIDER_NAMES) == {
        "claude-sdk": "anthropic",
        "openrouter": "openrouter",
    }


def test_outcome_values() -> None:
    assert [outcome.value for outcome in attributes.Outcome] == [
        "ok",
        "task_output_error",
        "provider_failed",
        "provider_unavailable",
        "cost_unknown",
        "timeout",
    ]
