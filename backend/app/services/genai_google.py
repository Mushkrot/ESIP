"""Google Gen AI client, wire conversion, and sanitized provider errors."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any, Callable

import google.auth
import httpx
import requests
from google import genai
from google.auth.credentials import Credentials
from google.genai import errors, types
from google.oauth2 import service_account

from app.config import settings
from app.services.genai_transport import (
    Candidate,
    EmbeddingRequest,
    EmbeddingResult,
    FunctionCall,
    FunctionResponse,
    FunctionCallingMode,
    GenAITransportError,
    GenAITransportErrorKind,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    InlineDataPart,
    ResponseContent,
    ResponsePart,
    SafetyRating,
    Tool,
    ToolConfig,
    UsageMetadata,
    canonical_value,
    thaw,
)


API_VERSION = "v1"
REQUEST_TIMEOUT_MS = 60_000
REQUEST_ATTEMPTS = 1
AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
CURRENT_EMBEDDING_MODEL = "gemini-embedding-001"
AUTOMATIC_FUNCTION_CALLING_DISABLED = True
MODEL_THINKING_LEVELS = {
    "gemini-3.5-flash": ("MINIMAL", "LOW", "MEDIUM", "HIGH"),
    "gemini-3.5-flash-lite": ("MINIMAL", "LOW", "MEDIUM", "HIGH"),
}
GEMINI_3_FORBIDDEN_FIELDS = frozenset(
    {
        "candidate_count",
        "frequency_penalty",
        "presence_penalty",
        "temperature",
        "top_k",
        "top_p",
    }
)
ADAPTER_OWNED_CONFIG_FIELDS = frozenset(
    {
        "automatic_function_calling",
        "cached_content",
        "http_options",
        "safety_settings",
        "tool_config",
        "tools",
    }
)

_stream_responses: ContextVar[list[httpx.Response] | None] = ContextVar("genai_stream_responses", default=None)


def _capture_stream_response(response: httpx.Response) -> None:
    responses = _stream_responses.get()
    if responses is not None:
        responses.append(response)


def _explicit_credentials() -> Credentials:
    credential_path = str(settings.google_application_credentials or "").strip()
    try:
        if credential_path:
            return service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=list(AUTH_SCOPES),
            )
        credentials, _ = google.auth.default(scopes=list(AUTH_SCOPES))
        return credentials
    except Exception:
        raise GenAITransportError(
            GenAITransportErrorKind.AUTHENTICATION,
            "Gen AI credentials could not be loaded.",
        ) from None


def _safety_settings() -> list[types.SafetySetting]:
    values = (
        (types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
        (types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
        (types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
        (types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    )
    return [types.SafetySetting(category=category, threshold=threshold) for category, threshold in values]


class GoogleGenAIAdapter:
    """Convert application requests to Google Gen AI calls."""

    def __init__(
        self,
        *,
        client: genai.Client | None = None,
        credentials_factory: Callable[[], Credentials] = _explicit_credentials,
    ) -> None:
        self._client = client
        self._location_clients: dict[str, genai.Client] = {}
        self._credentials_factory = credentials_factory
        self._client_lock = threading.Lock()

    def _get_client(self, location: str | None = None) -> genai.Client:
        client = self._client if location is None else self._location_clients.get(location)
        if client is not None:
            return client
        with self._client_lock:
            client = self._client if location is None else self._location_clients.get(location)
            if client is None:
                client = genai.Client(
                    enterprise=True,
                    project=settings.google_cloud_project_id,
                    location=location or settings.gemini_location,
                    credentials=self._credentials_factory(),
                    http_options=types.HttpOptions(
                        api_version=API_VERSION,
                        timeout=REQUEST_TIMEOUT_MS,
                        retry_options=types.HttpRetryOptions(attempts=REQUEST_ATTEMPTS),
                        client_args={"event_hooks": {"response": [_capture_stream_response]}},
                    ),
                )
                if location is None:
                    self._client = client
                else:
                    self._location_clients[location] = client
            return client

    def close(self) -> None:
        with self._client_lock:
            clients = list(self._location_clients.values())
            if self._client is not None:
                clients.append(self._client)
            self._client = None
            self._location_clients.clear()
        for client in clients:
            client.close()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        try:
            contents = self._contents(request.contents)
            config = self._generation_config(request)
            client = self._get_client(request.location)
            response = client.models.generate_content(
                model=request.model,
                contents=contents,
                config=config,
            )
            return self._result(response)
        except GenAITransportError:
            raise
        except Exception as exc:
            raise self._mapped_error(exc) from None

    def stream(self, request: GenerationRequest) -> Iterator[GenerationChunk]:
        try:
            contents = self._contents(request.contents)
            config = self._generation_config(request)
            client = self._get_client(request.location)
            responses = client.models.generate_content_stream(
                model=request.model,
                contents=contents,
                config=config,
            )
            http_responses: list[httpx.Response] = []
            try:
                while True:
                    token = _stream_responses.set(http_responses)
                    try:
                        response = next(responses)
                    except StopIteration:
                        break
                    finally:
                        _stream_responses.reset(token)
                    normalized = self._result(response)
                    yield GenerationChunk(
                        text=normalized.text,
                        candidates=normalized.candidates,
                        usage_metadata=normalized.usage_metadata,
                        prompt_block_reason=normalized.prompt_block_reason,
                        model_version=normalized.model_version,
                    )
            finally:
                try:
                    close = getattr(responses, "close", None)
                    if callable(close):
                        close()
                finally:
                    # SDK 2.16 does not close the HTTP response on generator cancellation.
                    for response in http_responses:
                        response.close()
        except GenAITransportError:
            raise
        except Exception as exc:
            raise self._mapped_error(exc) from None

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        if request.model != CURRENT_EMBEDDING_MODEL:
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                "Gen AI embedding model is not approved for this runtime.",
            )
        if not request.texts:
            return EmbeddingResult([])
        try:
            response = self._get_client().models.embed_content(
                model=request.model,
                contents=list(request.texts),
                config=types.EmbedContentConfig(
                    task_type=request.task_type,
                    output_dimensionality=request.output_dimensionality,
                ),
            )
            vectors = [tuple(embedding.values or ()) for embedding in (response.embeddings or ())]
            if len(vectors) != len(request.texts):
                raise GenAITransportError(
                    GenAITransportErrorKind.INTERNAL,
                    "Gen AI embedding response count did not match the request.",
                )
            if request.output_dimensionality is not None and any(
                len(vector) != request.output_dimensionality for vector in vectors
            ):
                raise GenAITransportError(
                    GenAITransportErrorKind.INTERNAL,
                    "Gen AI embedding response dimensions did not match the request.",
                )
            return EmbeddingResult(vectors)
        except GenAITransportError:
            raise
        except Exception as exc:
            raise self._mapped_error(exc) from None

    def request_snapshot(self, request: GenerationRequest, *, stream: bool = False) -> dict[str, Any]:
        """Return deterministic provider inputs without opening a client or transport."""
        config = self._generation_config(request)
        return {
            "config": canonical_value(config.model_dump(mode="json", exclude_none=True)),
            "contents": canonical_value(self._provider_value(self._contents(request.contents))),
            "model": request.model,
            "stream": stream,
            "surface": request.surface,
            **({"location": request.location} if request.location is not None else {}),
        }

    @staticmethod
    def embedding_snapshot(request: EmbeddingRequest) -> dict[str, Any]:
        config = types.EmbedContentConfig(
            task_type=request.task_type,
            output_dimensionality=request.output_dimensionality,
        )
        return {
            "config": canonical_value(config.model_dump(mode="json", exclude_none=True)),
            "contents": list(request.texts),
            "model": request.model,
            "surface": request.surface,
        }

    @classmethod
    def _generation_config(cls, request: GenerationRequest) -> types.GenerateContentConfig:
        payload = request.generation_config.to_dict()
        cls._validate_generation_payload(request.model, payload)
        final_role = None
        if isinstance(request.contents, (list, tuple)) and request.contents:
            last = request.contents[-1]
            final_role = last.get("role") if isinstance(last, Mapping) else getattr(last, "role", None)
        if request.model == "gemini-3.5-flash-lite" and str(final_role).lower() == "model":
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                "Gemini 3.5 Flash-Lite does not support a final model-role input turn.",
            )
        conflicting = ADAPTER_OWNED_CONFIG_FIELDS.intersection(payload)
        if conflicting:
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                f"Gen AI request attempted to override adapter-owned fields: {sorted(conflicting)}",
            )
        if isinstance(payload.get("response_schema"), Mapping):
            payload["response_schema"] = cls._schema(payload["response_schema"])
        return types.GenerateContentConfig(
            **payload,
            safety_settings=_safety_settings(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=AUTOMATIC_FUNCTION_CALLING_DISABLED,
                maximum_remote_calls=None,
            ),
            tools=[cls._tool(tool) for tool in request.tools],
            tool_config=cls._tool_config(request.tool_config) if request.tool_config else None,
        )

    @staticmethod
    def _validate_generation_payload(model: str, payload: Mapping[str, Any]) -> None:
        max_output_tokens = payload.get("max_output_tokens")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                "Gen AI max_output_tokens must be a positive explicit integer.",
            )
        thinking = payload.get("thinking_config")
        if not isinstance(thinking, Mapping):
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                "Gen AI thinking configuration must be explicit.",
            )
        present = {key for key in ("thinking_budget", "thinking_level") if key in thinking}
        if len(present) != 1:
            raise GenAITransportError(
                GenAITransportErrorKind.INVALID_REQUEST,
                "Gen AI request must contain exactly one family-compatible thinking control.",
            )
        normalized_model = model.strip().lower()
        if normalized_model.startswith("gemini-2.5-"):
            if present != {"thinking_budget"}:
                raise GenAITransportError(
                    GenAITransportErrorKind.INVALID_REQUEST,
                    "Gemini 2.5 requires thinking_budget.",
                )
            budget = thinking["thinking_budget"]
            if type(budget) is not int or budget < 0:
                raise GenAITransportError(
                    GenAITransportErrorKind.INVALID_REQUEST,
                    "Thinking budget must be a nonnegative integer.",
                )
            return
        if normalized_model.startswith("gemini-3."):
            level = str(thinking.get("thinking_level") or "").upper()
            if present != {"thinking_level"} or level not in MODEL_THINKING_LEVELS.get(normalized_model, ()):
                raise GenAITransportError(
                    GenAITransportErrorKind.INVALID_REQUEST,
                    "Gemini 3 requires an approved explicit thinking_level.",
                )
            forbidden = GEMINI_3_FORBIDDEN_FIELDS.intersection(payload)
            if forbidden:
                raise GenAITransportError(
                    GenAITransportErrorKind.INVALID_REQUEST,
                    f"Gemini 3 request contains forbidden generation fields: {sorted(forbidden)}",
                )
            return
        raise GenAITransportError(
            GenAITransportErrorKind.INVALID_REQUEST,
            "Gen AI model family is not approved for this runtime.",
        )

    @classmethod
    def _contents(cls, value: Any) -> Any:
        if isinstance(value, InlineDataPart):
            return types.Part.from_bytes(data=value.data, mime_type=value.mime_type)
        if isinstance(value, ResponseContent):
            return types.Content(
                role=value.role,
                parts=[cls._contents(part) for part in value.parts],
            )
        if isinstance(value, ResponsePart):
            kwargs: dict[str, Any] = {}
            if value.text is not None:
                kwargs["text"] = value.text
            if value.thought is not None:
                kwargs["thought"] = value.thought
            if value.thought_signature is not None:
                kwargs["thought_signature"] = bytes(value.thought_signature)
            if value.function_call is not None:
                function_call_kwargs: dict[str, Any] = {
                    "id": value.function_call.id,
                    "name": value.function_call.name,
                    "args": thaw(value.function_call.args),
                }
                if value.function_call.partial_args:
                    function_call_kwargs["partial_args"] = [
                        types.PartialArg(**thaw(item)) for item in value.function_call.partial_args
                    ]
                if value.function_call.will_continue is not None:
                    function_call_kwargs["will_continue"] = value.function_call.will_continue
                kwargs["function_call"] = types.FunctionCall(**function_call_kwargs)
            if value.function_response is not None:
                kwargs["function_response"] = types.FunctionResponse(
                    id=value.function_response.id,
                    name=value.function_response.name,
                    response=thaw(value.function_response.response),
                    will_continue=value.function_response.will_continue,
                )
            return types.Part(**kwargs)
        if isinstance(value, tuple):
            return [cls._contents(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): cls._contents(item) for key, item in value.items()}
        return value

    @classmethod
    def _provider_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._provider_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._provider_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._provider_value(item) for item in value]
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        return value

    @staticmethod
    def _schema(value: Mapping[str, Any]) -> types.Schema:
        """Keep declared property order in structured responses and tool arguments."""
        payload = thaw(value)
        properties = payload.get("properties")
        if isinstance(properties, Mapping):
            payload["properties"] = {
                str(name): GoogleGenAIAdapter._schema(schema)
                for name, schema in properties.items()
            }
            payload.setdefault("property_ordering", [str(name) for name in properties])
        items = payload.get("items")
        if isinstance(items, Mapping):
            payload["items"] = GoogleGenAIAdapter._schema(items)
        any_of = payload.get("any_of")
        if isinstance(any_of, list):
            payload["any_of"] = [
                GoogleGenAIAdapter._schema(item) if isinstance(item, Mapping) else item
                for item in any_of
            ]
        definitions = payload.get("defs")
        if isinstance(definitions, Mapping):
            payload["defs"] = {
                str(name): GoogleGenAIAdapter._schema(schema)
                for name, schema in definitions.items()
            }
        additional = payload.get("additional_properties")
        if isinstance(additional, Mapping):
            payload["additional_properties"] = GoogleGenAIAdapter._schema(additional)
        return types.Schema(**payload)

    @staticmethod
    def _tool(tool: Tool) -> types.Tool:
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=declaration.name,
                    description=declaration.description,
                    parameters=GoogleGenAIAdapter._schema(declaration.parameters),
                )
                for declaration in tool.function_declarations
            ]
        )

    @staticmethod
    def _tool_config(tool_config: ToolConfig) -> types.ToolConfig:
        mode = tool_config.function_calling_config.mode
        provider_mode = (
            types.FunctionCallingConfigMode.AUTO
            if mode is FunctionCallingMode.AUTO
            else types.FunctionCallingConfigMode.NONE
        )
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=provider_mode)
        )

    @classmethod
    def _result(cls, response: Any) -> GenerationResult:
        candidates = tuple(cls._candidate(candidate) for candidate in (response.candidates or ()))
        content = candidates[0].content if candidates else None
        text = "".join(part.text for part in (content.parts if content else ()) if part.text and not part.thought)
        prompt_feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(prompt_feedback, "block_reason", None)
        block_reason = getattr(block_reason, "value", block_reason)
        return GenerationResult(
            text=text,
            candidates=candidates,
            usage_metadata=cls._usage(getattr(response, "usage_metadata", None)),
            prompt_block_reason=block_reason,
            model_version=(
                getattr(response, "model_version", None)
                or getattr(response, "modelVersion", None)
            ),
        )

    @classmethod
    def _candidate(cls, candidate: Any) -> Candidate:
        raw_content = getattr(candidate, "content", None)
        content = None
        if raw_content is not None:
            content = ResponseContent(
                role=getattr(raw_content, "role", None),
                parts=tuple(
                    cls._response_part(part)
                    for part in (getattr(raw_content, "parts", None) or ())
                ),
            )
        finish_reason = getattr(candidate, "finish_reason", None)
        finish_reason = getattr(finish_reason, "value", finish_reason)
        return Candidate(
            content=content,
            finish_reason=finish_reason,
            finish_message=getattr(candidate, "finish_message", None),
            safety_ratings=tuple(
                cls._safety_rating(rating)
                for rating in (getattr(candidate, "safety_ratings", None) or ())
            ),
        )

    @staticmethod
    def _response_part(part: Any) -> ResponsePart:
        raw_call = getattr(part, "function_call", None)
        function_call = None
        if raw_call is not None and getattr(raw_call, "name", None):
            args = getattr(raw_call, "args", None) or {}
            if hasattr(args, "model_dump"):
                args = args.model_dump(mode="python", exclude_none=True)
            function_call = FunctionCall(
                name=raw_call.name,
                args=dict(args),
                id=getattr(raw_call, "id", None),
                partial_args=tuple(
                    item.model_dump(mode="python", exclude_none=True)
                    if hasattr(item, "model_dump") else dict(item)
                    for item in (getattr(raw_call, "partial_args", None) or ())
                ),
                will_continue=getattr(raw_call, "will_continue", None),
            )
        raw_response = getattr(part, "function_response", None)
        function_response = None
        if raw_response is not None and getattr(raw_response, "name", None):
            response = getattr(raw_response, "response", None) or {}
            if hasattr(response, "model_dump"):
                response = response.model_dump(mode="python", exclude_none=True)
            function_response = FunctionResponse(
                name=raw_response.name,
                response=dict(response),
                id=getattr(raw_response, "id", None),
                will_continue=getattr(raw_response, "will_continue", None),
            )
        try:
            text = getattr(part, "text", None)
        except Exception:
            text = None
        try:
            thought = getattr(part, "thought", None)
            thought_signature = getattr(part, "thought_signature", None)
        except Exception:
            thought = None
            thought_signature = None
        return ResponsePart(
            text=text,
            function_call=function_call,
            thought=thought,
            thought_signature=thought_signature,
            function_response=function_response,
        )

    @staticmethod
    def _safety_rating(rating: Any) -> SafetyRating:
        category = getattr(rating, "category", None)
        probability = getattr(rating, "probability", None)
        return SafetyRating(
            category=getattr(category, "value", category),
            probability=getattr(probability, "value", probability),
            blocked=getattr(rating, "blocked", None),
        )

    @staticmethod
    def _usage(usage: Any) -> UsageMetadata | None:
        if usage is None:
            return None
        return UsageMetadata(
            prompt_token_count=getattr(usage, "prompt_token_count", 0) or 0,
            candidates_token_count=getattr(usage, "candidates_token_count", 0) or 0,
            thoughts_token_count=getattr(usage, "thoughts_token_count", 0) or 0,
            cached_content_token_count=getattr(usage, "cached_content_token_count", 0) or 0,
        )

    @staticmethod
    def _mapped_error(exc: Exception) -> GenAITransportError:
        if isinstance(exc, (TimeoutError, httpx.TimeoutException, requests.Timeout)):
            return GenAITransportError(
                GenAITransportErrorKind.TIMEOUT,
                "Gen AI provider request timed out.",
            )
        if isinstance(exc, google.auth.exceptions.GoogleAuthError):
            return GenAITransportError(
                GenAITransportErrorKind.AUTHENTICATION,
                "Gen AI credentials could not be loaded.",
            )
        if isinstance(exc, (httpx.TransportError, requests.ConnectionError)):
            return GenAITransportError(
                GenAITransportErrorKind.UNAVAILABLE,
                "Gen AI provider is temporarily unavailable.",
            )
        if isinstance(exc, errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            if code in {401, 403}:
                kind = GenAITransportErrorKind.AUTHENTICATION
                message = "Gen AI provider authentication failed."
            elif code in {408, 504}:
                kind = GenAITransportErrorKind.TIMEOUT
                message = "Gen AI provider request timed out."
            elif code == 429:
                kind = GenAITransportErrorKind.QUOTA
                message = "Gen AI provider quota is temporarily unavailable."
            elif 400 <= code < 500:
                kind = GenAITransportErrorKind.INVALID_REQUEST
                message = "Gen AI provider rejected the request."
            else:
                kind = GenAITransportErrorKind.UNAVAILABLE
                message = "Gen AI provider is temporarily unavailable."
            return GenAITransportError(kind, message)
        return GenAITransportError(
            GenAITransportErrorKind.INTERNAL,
            "Gen AI provider request failed.",
        )
