"""Contract tests use SDK model serialization plus a fake service; no Azure account."""
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition, VectorStoreStatus, RunStatus, MessageRole
from claims_triage.agents import AGENTS
from claims_triage.backends.azure import AzureBackend
from claims_triage.config import Settings
from claims_triage.storage import Store

@pytest.fixture
def azure(tmp_path):
    settings = Settings(backend="azure", state_dir=tmp_path, project_endpoint="https://example.services.ai.azure.com/api/projects/demo", model_deployment="test-model")
    store = Store(tmp_path)
    client = Mock()
    client.files.upload_and_poll.return_value = NS(id="file-1")
    client.vector_stores.create_and_poll.return_value = NS(id="vector-1", status=VectorStoreStatus.COMPLETED)
    client.create_agent.side_effect = [NS(id=f"agent-{r}") for r in AGENTS]
    client.update_agent.side_effect = lambda **kw: NS(id=kw["agent_id"])
    client.threads.create.return_value = NS(id="thread-1")
    return AzureBackend(settings, store, client=client), client, store


def test_real_sdk_tool_schemas_serialize():
    for agent in AGENTS.values():
        for definition in agent.tools:
            sdk = FunctionToolDefinition(function=FunctionDefinition(**definition["function"]))
            assert sdk.as_dict() == definition


def test_programmatic_creation_and_reuse(azure):
    backend, client, store = azure
    result = backend.provision()
    assert len(result["agents"]) == 4
    assert client.create_agent.call_count == 4
    anomaly_args = client.create_agent.call_args_list[2].kwargs
    assert any(t.as_dict()["type"] == "file_search" for t in anomaly_args["tools"])
    assert anomaly_args["tool_resources"].file_search.vector_store_ids == ["vector-1"]
    backend.provision()
    assert client.create_agent.call_count == 4
    assert client.update_agent.call_count == 4
    assert client.files.upload_and_poll.call_count == 1


def test_tool_execution_and_run_scoped_messages(azure):
    backend, client, store = azure
    backend.provision()
    session = store.session()
    call = NS(id="call-1", type="function", function=NS(name="validate_claims", arguments='{"batch_id":"b"}'))
    client.runs.create.return_value = NS(id="run-1", status=RunStatus.REQUIRES_ACTION, required_action=NS(submit_tool_outputs=NS(tool_calls=[call])))
    client.runs.get.return_value = NS(id="run-1", status=RunStatus.COMPLETED)
    client.messages.list.return_value = [NS(role=MessageRole.AGENT, text_messages=[NS(text=NS(value="Validated",annotations=[]))])]
    registry = Mock()
    registry.execute.return_value = {"claims":[]}
    result = backend.execute("intake", {"batch_id":"b"}, session, registry)
    registry.execute.assert_called_once_with("intake", "validate_claims", {"batch_id":"b"})
    assert result["tool_result"] == {"claims":[]}
    assert client.messages.list.call_args.kwargs["run_id"] == "run-1"
    assert client.runs.submit_tool_outputs.call_args.kwargs["tool_outputs"][0].tool_call_id == "call-1"
    backend.thread(session, "intake")
    assert client.threads.create.call_count == 1


def test_timeout_cancels_remote_run(azure):
    backend, client, store = azure
    backend.provision()
    backend.settings = replace(backend.settings, timeout=-1)
    client.runs.create.return_value = NS(id="run-1", status=RunStatus.IN_PROGRESS)
    with pytest.raises(TimeoutError):
        backend.run("supervisor", {}, store.session())
    client.runs.cancel.assert_called_once_with(thread_id="thread-1", run_id="run-1")


def test_agent_cannot_skip_required_tool(azure):
    backend, client, store = azure
    backend.provision()
    client.runs.create.return_value = NS(id="run-1", status=RunStatus.COMPLETED)
    with pytest.raises(RuntimeError, match="required custom tool"):
        backend.run("intake", {}, store.session(), Mock())


def test_failed_remote_run_not_silently_mocked(azure):
    backend, client, store = azure
    backend.provision()
    client.runs.create.return_value = NS(id="run-1", status="failed", last_error="quota")
    with pytest.raises(RuntimeError, match="quota"):
        backend.run("supervisor", {}, store.session())


def test_changed_agent_instructions_require_reprovision(azure):
    backend, client, store = azure
    backend.provision()
    backend.settings = replace(backend.settings, model_deployment="another-model")
    with pytest.raises(ValueError, match="configuration changed"):
        backend.run("supervisor", {}, store.session())


def test_missing_configuration_fails_before_cloud(tmp_path):
    with pytest.raises(ValueError, match="requires"):
        AzureBackend(Settings(backend="azure", state_dir=tmp_path), Store(tmp_path))
