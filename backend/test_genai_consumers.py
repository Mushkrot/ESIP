"""Application flows through the AI adapter without cloud credentials or traffic."""

from contextlib import ExitStack, contextmanager
import json
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import embeddings, gemini, genai_transport, reference_runtime, universal_chat
from app.services.cost_tracker import CostGateError
from test_genai_adapter import EventStream, response, sdk_adapter


@contextmanager
def consumer_adapter(module, handler):
    with ExitStack() as stack:
        adapter = stack.enter_context(sdk_adapter(handler))
        stack.enter_context(mock.patch.object(genai_transport, "_transport", adapter))
        gate = stack.enter_context(mock.patch.object(module, "check_cost_gate"))
        stack.enter_context(mock.patch.object(module, "record_estimate"))
        usage = stack.enter_context(mock.patch.object(module, "record_usage", return_value=0.0))
        if hasattr(module, "check_divergence"):
            stack.enter_context(mock.patch.object(module, "check_divergence"))
        yield gate, usage


def test_ticket_summary_uses_structured_json_and_records_usage():
    expected = {"overview": "Payment terminal is offline.", "current_status": "Pending investigation.",
                "key_actions_taken": ["Checked connectivity."], "kb_suggestions": []}
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=response(json.dumps(expected)))
    with consumer_adapter(gemini, handler) as (gate, usage):
        result = gemini.generate_summary(reference_runtime.ticket_detail(950001), "brief")
    assert result == expected
    gate.assert_called_once()
    assert gate.call_args.kwargs["fail_closed"] is True
    assert usage.call_args.args[1:4] == (20, 4, 2)
    config = requests[0]["generationConfig"]
    assert config["maxOutputTokens"] == 4096
    assert config["responseMimeType"] == "application/json"
    assert config["thinkingConfig"] == {"thinking_budget": 0}
    assert config["responseSchema"]["required"] == list(expected)


@pytest.mark.parametrize("finish_reason,expected", [("STOP", [{"name": "search_knowledge_base", "args": {"query": "terminal offline"}}]), ("MALFORMED_FUNCTION_CALL", [])])
def test_manual_tool_selection_preserves_arguments_and_rejects_malformed_calls(finish_reason, expected):
    def handler(_):
        return httpx.Response(200, json=response(finish_reason=finish_reason, parts=[{
            "functionCall": {"name": "search_knowledge_base", "args": {"query": "terminal offline"}},
        }]))
    with consumer_adapter(gemini, handler):
        calls = gemini.decide_tool_calls(reference_runtime.ticket_detail(950001), [{"role": "user", "content": "Find a guide."}])
    assert calls == expected


def test_ocr_image_uses_inline_bytes_and_existing_token_budget():
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=response("[IMAGE DESCRIPTION: Error dialog]\n\nNetwork unavailable"))
    with consumer_adapter(gemini, handler):
        text, error = gemini.ocr_image(b"fixture-image", "image/png")
    assert text.endswith("Network unavailable")
    assert error == ""
    assert requests[0]["contents"][0]["parts"][0]["inlineData"]["mime_type"] == "image/png"
    assert requests[0]["generationConfig"]["maxOutputTokens"] == 2048


def test_ocr_timeout_does_not_enter_application_retry_loop():
    def handler(_):
        raise httpx.ReadTimeout("test timeout")
    with consumer_adapter(gemini, handler), mock.patch.object(gemini.time, "sleep") as sleep:
        text, error = gemini.ocr_image(b"fixture-image", "image/png")
    assert text is None
    assert "timed out" in error
    sleep.assert_not_called()


def test_universal_answer_stream_preserves_text_and_usage():
    source = EventStream()
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
    with consumer_adapter(universal_chat, handler) as (gate, usage):
        text = "".join(universal_chat.chat_stream([{"role": "user", "content": "Summarize the evidence."}]))
    assert text == "First second"
    assert source.closed
    gate.assert_called_once()
    assert usage.call_args.args[1:4] == (20, 4, 2)
    assert requests[0]["generationConfig"]["thinkingConfig"] == {"thinking_budget": 1024}
    assert requests[0]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"


def test_embedding_batches_preserve_order_dimensions_and_cost_gates():
    requests = []
    def handler(req):
        wire = json.loads(req.content)
        requests.append(wire)
        predictions = [{"embeddings": {"values": [float(item["content"])] * 3072}} for item in wire["instances"]]
        return httpx.Response(200, json={"predictions": predictions})
    with consumer_adapter(embeddings, handler) as (gate, usage):
        vectors = embeddings.embed_texts([str(i) for i in range(7)])
    assert [len(v) for v in vectors] == [3072] * 7
    assert [v[0] for v in vectors] == list(range(7))
    assert [len(r["instances"]) for r in requests] == [5, 2]
    assert all(item["task_type"] == "RETRIEVAL_DOCUMENT" for r in requests for item in r["instances"])
    assert gate.call_count == 3
    assert usage.call_count == 2


def test_embedding_gate_denial_stops_before_provider_access():
    with (
        mock.patch.object(embeddings, "check_cost_gate", side_effect=CostGateError("test denial")),
        mock.patch.object(embeddings, "get_transport") as provider,
    ):
        with pytest.raises(CostGateError):
            embeddings.embed_query("sample query")
    provider.assert_not_called()


def test_reference_http_flows_and_shutdown_do_not_initialize_provider():
    with (
        mock.patch.object(genai_transport, "_transport", None),
        mock.patch("app.services.genai_google.genai.Client", side_effect=AssertionError("provider call")),
        mock.patch("google.auth.default", side_effect=AssertionError("credential lookup")),
        TestClient(app) as client,
    ):
        headers = {"Authorization": f"Bearer {reference_runtime.REFERENCE_SESSION_TOKEN}"}
        ticket = client.get("/api/tickets/950001", headers=headers)
        summary = client.post("/api/ai/summary", headers=headers, json={"ticket_id": 950001, "mode": "brief", "language": "en"})
        assert ticket.status_code == 200
        assert summary.status_code == 200
        assert summary.json()["summary"]["overview"]
    assert genai_transport._transport is None


def test_cancelling_application_stream_closes_provider_response():
    source = EventStream()
    def handler(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
    with consumer_adapter(universal_chat, handler):
        stream = universal_chat.chat_stream([{"role": "user", "content": "Summarize the evidence."}])
        assert next(stream) == "First "
        stream.close()
        assert source.closed


def test_application_shutdown_closes_existing_transport():
    adapter = mock.Mock()
    with mock.patch.object(genai_transport, "_transport", adapter):
        with TestClient(app):
            adapter.close.assert_not_called()
        adapter.close.assert_called_once()
        assert genai_transport._transport is None
