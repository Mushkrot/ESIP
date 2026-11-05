"""Provider-neutral requests, responses, and model bindings for generative AI."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol
from threading import Lock


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return mutable provider-input primitives from frozen ESIP values."""
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if is_dataclass(value):
        return {field.name: thaw(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_value(value: Any) -> Any:
    """Convert ESIP transport values to deterministic JSON-compatible data."""
    if isinstance(value, bytes):
        return {
            "bytes_sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    if isinstance(value, Mapping):
        return {str(key): canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: canonical_value(getattr(value, field.name))
            for field in fields(value)
            if not (isinstance(value, GenerationRequest) and field.name == "location" and value.location is None)
        }
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class GenerationConfig:
    """Frozen provider-neutral generation configuration."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GenerationConfig":
        return cls(payload=payload)

    def to_dict(self) -> dict[str, Any]:
        return thaw(self.payload)


@dataclass(frozen=True)
class FunctionDeclaration:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze(self.parameters))


@dataclass(frozen=True)
class Tool:
    function_declarations: tuple[FunctionDeclaration, ...]

    def __init__(self, *, function_declarations: Sequence[FunctionDeclaration]) -> None:
        object.__setattr__(self, "function_declarations", tuple(function_declarations))


class FunctionCallingMode(str, Enum):
    AUTO = "AUTO"
    NONE = "NONE"


@dataclass(frozen=True)
class FunctionCallingConfig:
    mode: FunctionCallingMode

    Mode = FunctionCallingMode


@dataclass(frozen=True)
class ToolConfig:
    function_calling_config: FunctionCallingConfig

    FunctionCallingConfig = FunctionCallingConfig


@dataclass(frozen=True)
class InlineDataPart:
    data: bytes
    mime_type: str


class Part:
    """Construct an inline image or document part."""

    @staticmethod
    def from_data(*, data: bytes, mime_type: str) -> InlineDataPart:
        return InlineDataPart(data=bytes(data), mime_type=mime_type)


@dataclass(frozen=True)
class GenerationRequest:
    surface: str
    model: str
    contents: Any
    generation_config: GenerationConfig
    tools: tuple[Tool, ...] = ()
    tool_config: ToolConfig | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        if not self.surface.strip():
            raise ValueError("generation surface must be explicit")
        if not self.model.strip():
            raise ValueError("generation model must be explicit")
        payload = self.generation_config.to_dict()
        if "max_output_tokens" not in payload:
            raise ValueError("max_output_tokens must be explicit")
        thinking = payload.get("thinking_config")
        if not isinstance(thinking, Mapping):
            raise ValueError("thinking_config must be explicit")
        present = {name for name in ("thinking_budget", "thinking_level") if name in thinking}
        if len(present) != 1:
            raise ValueError("exactly one thinking control must be explicit")
        object.__setattr__(self, "contents", _freeze(self.contents))
        object.__setattr__(self, "tools", tuple(self.tools))

    def canonical_payload(self) -> dict[str, Any]:
        payload = canonical_value(self)
        if self.location is None:
            payload.pop("location", None)
        return payload


@dataclass(frozen=True)
class UsageMetadata:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0
    cached_content_token_count: int = 0


@dataclass(frozen=True)
class SafetyRating:
    category: str | None = None
    probability: str | None = None
    blocked: bool | None = None


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: Mapping[str, Any]
    id: str | None = None
    partial_args: tuple[Mapping[str, Any], ...] = ()
    will_continue: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze(self.args))
        object.__setattr__(self, "partial_args", tuple(_freeze(item) for item in self.partial_args))


@dataclass(frozen=True)
class FunctionResponse:
    name: str
    response: Mapping[str, Any]
    id: str | None = None
    will_continue: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", _freeze(self.response))


@dataclass(frozen=True)
class ResponsePart:
    text: str | None = None
    function_call: FunctionCall | None = None
    thought: bool | None = None
    thought_signature: bytes | None = None
    function_response: FunctionResponse | None = None

    def __post_init__(self) -> None:
        signature = self.thought_signature
        if signature is not None:
            if not isinstance(signature, (bytes, bytearray)):
                raise TypeError("thought_signature must be opaque bytes")
            object.__setattr__(self, "thought_signature", bytes(signature))


@dataclass(frozen=True)
class ResponseContent:
    parts: tuple[ResponsePart, ...] = ()
    role: str | None = None


@dataclass(frozen=True)
class Candidate:
    content: ResponseContent | None = None
    finish_reason: int | str | None = None
    finish_message: str | None = None
    safety_ratings: tuple[SafetyRating, ...] = ()


@dataclass(frozen=True)
class GenerationResult:
    text: str
    candidates: tuple[Candidate, ...] = ()
    usage_metadata: UsageMetadata | None = None
    prompt_block_reason: int | str | None = None
    model_version: str | None = None


@dataclass(frozen=True)
class GenerationChunk:
    text: str
    candidates: tuple[Candidate, ...] = ()
    usage_metadata: UsageMetadata | None = None
    prompt_block_reason: int | str | None = None
    model_version: str | None = None


@dataclass(frozen=True)
class EmbeddingRequest:
    surface: str
    model: str
    texts: tuple[str, ...]
    task_type: str
    output_dimensionality: int | None = None

    def __init__(
        self,
        *,
        surface: str,
        model: str,
        texts: Sequence[str],
        task_type: str,
        output_dimensionality: int | None = None,
    ) -> None:
        if not surface.strip() or not model.strip() or not task_type.strip():
            raise ValueError("embedding surface, model, and task type must be explicit")
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "texts", tuple(texts))
        object.__setattr__(self, "task_type", task_type)
        object.__setattr__(self, "output_dimensionality", output_dimensionality)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]

    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        object.__setattr__(
            self,
            "vectors",
            tuple(tuple(float(value) for value in vector) for vector in vectors),
        )


class GenAITransport(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...

    def stream(self, request: GenerationRequest) -> Iterator[GenerationChunk]: ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


class GenAITransportErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class GenAITransportError(RuntimeError):
    """Sanitized project-owned provider failure used by existing fallbacks."""

    def __init__(self, kind: GenAITransportErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


_transport: GenAITransport | None = None
_transport_lock = Lock()


def get_transport() -> GenAITransport:
    global _transport
    with _transport_lock:
        if _transport is None:
            from app.services.genai_google import GoogleGenAIAdapter

            _transport = GoogleGenAIAdapter()
    return _transport


def close_transport() -> None:
    """Close and clear the reusable provider client during process shutdown."""
    global _transport
    with _transport_lock:
        current = _transport
        _transport = None
    if current is None:
        return
    close = getattr(current, "close", None)
    if callable(close):
        close()


class BoundModel:
    """Model-specific facade that immediately delegates through GenAITransport."""

    def __init__(self, model: str, *, surface: str = "generation", location: str | None = None) -> None:
        self.model = model
        self.surface = surface
        self.location = location

    def generate_content(
        self,
        contents: Any,
        *,
        generation_config: GenerationConfig | Mapping[str, Any],
        tools: Sequence[Tool] | None = None,
        tool_config: ToolConfig | None = None,
        stream: bool = False,
        surface: str | None = None,
    ) -> GenerationResult | Iterator[GenerationChunk]:
        config = (
            generation_config
            if isinstance(generation_config, GenerationConfig)
            else GenerationConfig.from_dict(generation_config)
        )
        request = GenerationRequest(
            surface=surface or self.surface,
            model=self.model,
            contents=contents,
            generation_config=config,
            tools=tuple(tools or ()),
            tool_config=tool_config,
            location=self.location,
        )
        transport = get_transport()
        if stream:
            return transport.stream(request)
        return transport.generate(request)
