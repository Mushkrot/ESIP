"""AI transport contracts exercised through the Google SDK with local HTTP responses."""

from contextlib import contextmanager
import json
from pathlib import Path
import traceback
from unittest import mock

import httpx
import pytest
from google import genai
from google.auth.credentials import AnonymousCredentials
from google.genai import types

from app.config import settings
from app.services import genai_google as google_adapter
from app.services import genai_transport as transport


def request(**overrides):
    values = dict(
        surface="test",
        model="gemini-2.5-flash",
        contents="Summarize the supplied ticket.",
        generation_config=transport.GenerationConfig.from_dict({
            "temperature": 0.2, "max_output_tokens": 1024,
            "thinking_config": {"thinking_budget": 0},
        }),
    )
    values.update(overrides)
    return transport.GenerationRequest(**values)


def response(text="Answer", *, finish_reason="STOP", parts=None):
    return {
        "candidates": [{"content": {"role": "model", "parts": parts or [{"text": text}]},
                        "finishReason": finish_reason}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 4,
                          "thoughtsTokenCount": 2, "cachedContentTokenCount": 3},
        "modelVersion": "gemini-2.5-flash",
    }


@contextmanager
def sdk_adapter(handler):
    """Run real SDK serialization against an in-process HTTP endpoint."""
    credentials = AnonymousCredentials()
    credentials.token = "offline-test"
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [google_adapter._capture_stream_response]},
    ) as http_client:
        with genai.Client(
            enterprise=True, project="esip-test", location="us-central1",
            credentials=credentials,
            http_options=types.HttpOptions(
                api_version="v1", timeout=60_000,
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_client=http_client,
            ),
        ) as client:
            yield google_adapter.GoogleGenAIAdapter(client=client)


def test_requests_copy_nested_inputs_and_preserve_binary_parts():
    payload = {"max_output_tokens": 1024, "thinking_config": {"thinking_budget": 0}}
    contents = [{"role": "user", "parts": [{"text": "Original"}]}]
    item = request(contents=contents, generation_config=transport.GenerationConfig.from_dict(payload))
    contents[0]["parts"][0]["text"] = "Changed"
    payload["thinking_config"]["thinking_budget"] = 500
    assert item.contents[0]["parts"][0]["text"] == "Original"
    assert item.generation_config.to_dict()["thinking_config"] == {"thinking_budget": 0}
    with pytest.raises(TypeError):
        item.contents[0]["parts"][0]["text"] = "Changed"
    data = bytearray(b"image")
    part = transport.Part.from_data(data=data, mime_type="image/png")
    data[0] = 0
    assert part.data == b"image"


@pytest.mark.parametrize("payload", [
    {}, {"max_output_tokens": 10},
    {"max_output_tokens": 10, "thinking_config": {}},
    {"max_output_tokens": 10, "thinking_config": {"thinking_budget": 0, "thinking_level": "LOW"}},
])
def test_missing_or_ambiguous_limits_fail_before_transport(payload):
    with pytest.raises(ValueError):
        request(generation_config=transport.GenerationConfig.from_dict(payload))


@pytest.mark.parametrize("model,payload", [
    ("gemini-2.5-flash", {"max_output_tokens": 0, "thinking_config": {"thinking_budget": 0}}),
    ("gemini-2.5-flash", {"max_output_tokens": True, "thinking_config": {"thinking_budget": 0}}),
    ("gemini-2.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_budget": -1}}),
    ("gemini-2.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_level": "LOW"}}),
    ("gemini-3.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_budget": 0}}),
    ("gemini-3.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_level": "none"}}),
    ("gemini-3.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_level": "LOW"}, "temperature": 0}),
    ("unknown-model", {"max_output_tokens": 10, "thinking_config": {"thinking_budget": 0}}),
    ("gemini-2.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_budget": 0}, "http_options": {}}),
    ("gemini-2.5-flash", {"max_output_tokens": 10, "thinking_config": {"thinking_budget": 0}, "automatic_function_calling": {"disable": False}}),
])
def test_invalid_profiles_never_load_credentials(model, payload):
    credentials = mock.Mock(side_effect=AssertionError("credentials must not load"))
    adapter = google_adapter.GoogleGenAIAdapter(credentials_factory=credentials)
    with pytest.raises(transport.GenAITransportError) as caught:
        adapter.generate(request(model=model, generation_config=transport.GenerationConfig.from_dict(payload)))
    assert caught.value.kind is transport.GenAITransportErrorKind.INVALID_REQUEST
    credentials.assert_not_called()


def test_client_is_lazy_reused_and_closed_with_explicit_options():
    credentials = mock.Mock(return_value=AnonymousCredentials())
    client = mock.Mock()
    client.models.generate_content.return_value = types.GenerateContentResponse.model_validate(response())
    with mock.patch.object(google_adapter.genai, "Client", return_value=client) as factory:
        adapter = google_adapter.GoogleGenAIAdapter(credentials_factory=credentials)
        factory.assert_not_called()
        adapter.generate(request())
        adapter.generate(request())
        factory.assert_called_once()
        args = factory.call_args.kwargs
        assert args["enterprise"] is True
        assert args["project"] == settings.google_cloud_project_id
        assert args["location"] == settings.gemini_location
        assert args["http_options"].api_version == "v1"
        assert args["http_options"].timeout == 60_000
        assert args["http_options"].retry_options.attempts == 1
        credentials.assert_called_once()
        adapter.close()
        adapter.close()
        client.close.assert_called_once()


def test_explicit_credentials_failure_never_falls_back_to_another_identity():
    with (
        mock.patch.object(settings, "google_application_credentials", "/missing/esip-test.json"),
        mock.patch.object(google_adapter.google.auth, "default") as default,
    ):
        with pytest.raises(transport.GenAITransportError) as caught:
            google_adapter._explicit_credentials()
    default.assert_not_called()
    assert caught.value.kind is transport.GenAITransportErrorKind.AUTHENTICATION
    assert "/missing/" not in "".join(traceback.format_exception(caught.value))


def test_default_credentials_are_used_when_no_file_is_configured():
    credentials = AnonymousCredentials()
    with (
        mock.patch.object(settings, "google_application_credentials", ""),
        mock.patch.object(google_adapter.google.auth, "default", return_value=(credentials, "esip-test")) as default,
    ):
        assert google_adapter._explicit_credentials() is credentials
    default.assert_called_once_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def test_sdk_wire_preserves_schema_tools_image_and_response_metadata():
    captured = []
    def handler(http_request):
        captured.append(json.loads(http_request.content))
        assert http_request.url.path.endswith("/gemini-2.5-flash:generateContent")
        return httpx.Response(200, json=response(parts=[
            {"text": "Private reasoning", "thought": True},
            {"text": "Answer"},
            {"functionCall": {"name": "search_tickets", "args": {"query": "network"}},
             "thoughtSignature": "c2lnbmF0dXJl"},
        ]))
    config = transport.GenerationConfig.from_dict({
        "max_output_tokens": 1024, "thinking_config": {"thinking_budget": 0},
        "response_mime_type": "application/json",
        "response_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    })
    tool = transport.Tool(function_declarations=[transport.FunctionDeclaration(
        name="search_tickets", description="Search ticket evidence.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )])
    item = request(
        contents=["Read this image.", transport.Part.from_data(data=b"image", mime_type="image/png")],
        generation_config=config, tools=(tool,),
        tool_config=transport.ToolConfig(transport.FunctionCallingConfig(transport.FunctionCallingMode.AUTO)),
    )
    with sdk_adapter(handler) as adapter:
        result = adapter.generate(item)
        snapshot = adapter.request_snapshot(item)
    wire = captured[0]
    assert len(captured) == 1
    assert wire["generationConfig"]["maxOutputTokens"] == 1024
    assert wire["generationConfig"]["thinkingConfig"] == {"thinking_budget": 0}
    assert wire["generationConfig"]["responseSchema"]["property_ordering"] == ["answer"]
    assert wire["contents"][0]["parts"][1]["inlineData"] == {"data": "aW1hZ2U=", "mime_type": "image/png"}
    assert wire["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert len(wire["safetySettings"]) == 4
    assert all(s["threshold"] == "BLOCK_MEDIUM_AND_ABOVE" for s in wire["safetySettings"])
    assert snapshot["config"]["automatic_function_calling"]["disable"] is True
    assert result.text == "Answer"
    assert result.usage_metadata == transport.UsageMetadata(20, 4, 2, 3)
    assert result.candidates[0].finish_reason == "STOP"
    assert result.candidates[0].content.parts[2].function_call.args == {"query": "network"}
    assert result.candidates[0].content.parts[2].thought_signature == b"signature"


def test_blocked_response_has_no_invented_text():
    with sdk_adapter(lambda _: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})) as adapter:
        result = adapter.generate(request())
    assert result.text == ""
    assert result.candidates == ()
    assert result.prompt_block_reason == "SAFETY"
    assert result.usage_metadata is None


class EventStream(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False

    def __iter__(self):
        for text in ("First ", "second"):
            yield f"data: {json.dumps(response(text))}\n\n".encode()

    def close(self):
        self.closed = True


@pytest.mark.parametrize("cancel", [False, True])
def test_sdk_stream_preserves_chunks_and_closes_http_response(cancel):
    source = EventStream()
    with sdk_adapter(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)) as adapter:
        chunks = adapter.stream(request())
        first = next(chunks)
        assert first.text == "First "
        assert first.usage_metadata.thoughts_token_count == 2
        if cancel:
            chunks.close()
        else:
            assert [chunk.text for chunk in chunks] == ["second"]
    assert source.closed


@pytest.mark.parametrize("status,kind", [(400, "invalid_request"), (401, "authentication"),
    (403, "authentication"), (408, "timeout"), (429, "quota"), (500, "unavailable"), (503, "unavailable"), (504, "timeout")])
@pytest.mark.parametrize("operation", ["generate", "stream", "embed"])
def test_provider_errors_are_sanitized_without_hidden_retries(status, kind, operation):
    calls = []
    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(status, json={"error": {"code": status, "message": "private upstream detail"}})
    with sdk_adapter(handler) as adapter:
        with pytest.raises(transport.GenAITransportError) as caught:
            if operation == "stream":
                list(adapter.stream(request()))
            elif operation == "embed":
                adapter.embed(transport.EmbeddingRequest(surface="test", model="gemini-embedding-001", texts=["sample"], task_type="RETRIEVAL_QUERY"))
            else:
                adapter.generate(request())
    assert len(calls) == 1
    assert caught.value.kind.value == kind
    assert "private upstream detail" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error,kind", [(httpx.ReadTimeout("private"), "timeout"), (httpx.ConnectError("private"), "unavailable")])
def test_transport_errors_are_classified(error, kind):
    def handler(_):
        raise error
    with sdk_adapter(handler) as adapter:
        with pytest.raises(transport.GenAITransportError) as caught:
            adapter.generate(request())
    assert caught.value.kind.value == kind


@pytest.mark.parametrize("vectors", [[], [[1, 2]], [[1, 2, 3], [4, 5, 6]]])
def test_embedding_count_and_dimensions_are_checked(vectors):
    with sdk_adapter(lambda _: httpx.Response(200, json={"predictions": [{"embeddings": {"values": v}} for v in vectors]})) as adapter:
        with pytest.raises(transport.GenAITransportError):
            adapter.embed(transport.EmbeddingRequest(surface="test", model="gemini-embedding-001", texts=["sample"], task_type="RETRIEVAL_QUERY", output_dimensionality=3))


def test_application_imports_do_not_depend_on_provider_sdk():
    services = Path(__file__).parent / "app" / "services"
    for path in (Path(__file__).parent / "app").rglob("*.py"):
        source = path.read_text()
        assert "import vertexai" not in source
        assert "from vertexai" not in source
        if path != services / "genai_google.py":
            assert "from google import genai" not in source
            assert "from google.genai" not in source


def test_response_parts_round_trip_through_the_sdk():
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=response(parts=[{
            "functionCall": {"id": "call-1", "name": "search", "args": {"query": "network"}},
            "thoughtSignature": "c2lnbmF0dXJl",
        }]))
    with sdk_adapter(handler) as adapter:
        result = adapter.generate(request())
        adapter.generate(request(contents=[
            result.candidates[0].content,
            transport.ResponseContent(role="user", parts=(transport.ResponsePart(
                function_response=transport.FunctionResponse(name="search", id="call-1", response={"count": 1}),
            ),)),
        ]))
    parts = requests[1]["contents"][0]["parts"]
    assert parts[0]["thoughtSignature"] == "c2lnbmF0dXJl"
    assert parts[0]["functionCall"]["args"] == {"query": "network"}
    assert requests[1]["contents"][1]["parts"][0]["functionResponse"]["response"] == {"count": 1}


@pytest.mark.parametrize("last", [
    {"role": "model", "parts": [{"text": "Previous answer"}]},
    transport.ResponseContent(role="model", parts=(transport.ResponsePart(text="Previous answer"),)),
])
def test_flash_lite_rejects_final_model_turn_for_both_content_forms(last):
    credentials = mock.Mock(side_effect=AssertionError("credentials must not load"))
    adapter = google_adapter.GoogleGenAIAdapter(credentials_factory=credentials)
    config = transport.GenerationConfig.from_dict({"max_output_tokens": 1024, "thinking_config": {"thinking_level": "MINIMAL"}})
    with pytest.raises(transport.GenAITransportError) as caught:
        adapter.generate(request(model="gemini-3.5-flash-lite", contents=[last], generation_config=config))
    assert caught.value.kind is transport.GenAITransportErrorKind.INVALID_REQUEST
    credentials.assert_not_called()


def test_cancelled_stream_does_not_close_another_active_response():
    sources = [EventStream(), EventStream()]
    available = iter(sources)
    with sdk_adapter(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=next(available))) as adapter:
        first = adapter.stream(request())
        second = adapter.stream(request())
        assert next(first).text == "First "
        assert next(second).text == "First "
        first.close()
        assert sources[0].closed
        assert not sources[1].closed
        assert [chunk.text for chunk in second] == ["second"]
    assert sources[1].closed
