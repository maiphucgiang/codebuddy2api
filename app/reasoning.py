"""Normalize readable reasoning and request controls for Chat upstreams."""


class ReasoningInputError(ValueError):
    """Identify an unsupported reasoning field without exposing its contents."""

    def __init__(self, message, field=""):
        self.field = field
        super().__init__(f"{field}: {message}" if field else message)


def _text_parts(parts, kinds, field):
    if parts is None:
        return []
    if not isinstance(parts, list):
        raise ReasoningInputError("must be an array", field)
    texts = []
    for index, part in enumerate(parts):
        location = f"{field}[{index}]"
        if not isinstance(part, dict) or part.get("type") not in kinds:
            raise ReasoningInputError("unsupported reasoning text block", location)
        if not isinstance(part.get("text"), str):
            raise ReasoningInputError("must be a string", location + ".text")
        texts.append(part["text"])
    return texts


def extract_reasoning_text(block):
    """Return readable thinking without interpreting signatures or encrypted state."""
    kind = block.get("type")
    if kind == "redacted_thinking":
        raise ReasoningInputError("encrypted reasoning cannot be converted to Chat", "data")
    if kind == "thinking":
        text = block.get("thinking")
        if not isinstance(text, str):
            raise ReasoningInputError("must be a string", "thinking")
        if not text and block.get("signature") not in (None, ""):
            raise ReasoningInputError("encrypted-only thinking cannot be converted to Chat", "signature")
        return text
    if kind == "reasoning":
        if block.get("encrypted_content") not in (None, ""):
            raise ReasoningInputError("encrypted reasoning cannot be converted to Chat", "encrypted_content")
        content = _text_parts(block.get("content"), ("reasoning_text", "text"), "content")
        summary = _text_parts(block.get("summary"), ("summary_text",), "summary")
        return "".join(content if content else summary)
    return None


def thinking_mode(body):
    """Read the already-validated Messages thinking mode for account selection."""
    thinking = body.get("thinking")
    return thinking.get("type") if isinstance(thinking, dict) else None


def _object(body, field):
    value = body.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReasoningInputError("must be an object", field)
    return value


def _effort(value, field, allowed=None):
    if value is not None and (not isinstance(value, str) or not value.strip()
                              or (allowed is not None and value not in allowed)):
        raise ReasoningInputError("unsupported reasoning effort", field)
    return value


def map_reasoning_controls(body, chat, *, protocol):
    """Map explicit controls, leaving implicit activation to the selected account."""
    explicit = "reasoning_effort" in body
    effort = _effort(body.get("reasoning_effort"), "reasoning_effort")
    if protocol == "responses":
        reasoning = _object(body, "reasoning")
        if not explicit and "effort" in reasoning:
            effort = _effort(reasoning["effort"], "reasoning.effort")
            explicit = True
    elif protocol == "messages":
        thinking = _object(body, "thinking")
        mode = thinking_mode(body)
        if body.get("thinking") is not None and mode not in ("enabled", "adaptive", "disabled"):
            raise ReasoningInputError("must be enabled, adaptive or disabled", "thinking.type")
        if mode == "enabled":
            budget = thinking.get("budget_tokens")
            if type(budget) is not int or budget < 1024:
                raise ReasoningInputError("must be an integer >= 1024", "thinking.budget_tokens")
        elif "budget_tokens" in thinking:
            raise ReasoningInputError("requires thinking.type=enabled", "thinking.budget_tokens")
        if thinking.get("display") not in (None, "summarized"):
            raise ReasoningInputError("only summarized display is supported by Chat upstreams", "thinking.display")
        output = _object(body, "output_config")
        if not explicit and "effort" in output:
            effort = _effort(output["effort"], "output_config.effort", ("low", "medium", "high", "xhigh", "max"))
            explicit = True
        if mode == "disabled":
            effort, explicit = "none", True
    else:
        raise ValueError("unsupported reasoning protocol")
    if explicit:
        chat["reasoning_effort"] = effort


def resolve_reasoning_effort(effort, mode, model):
    """Use the same account-owned default for capability checks and upstream requests."""
    if mode == "disabled":
        return "none"
    if effort is not None or mode not in ("enabled", "adaptive"):
        return effort
    model = model or {}
    reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
    supported = reasoning.get("supportedEfforts")
    supported = supported if isinstance(supported, list) and supported and all(isinstance(v, str) for v in supported) else None
    candidates = [reasoning.get("defaultEffort"), reasoning.get("effort"),
                  "high", "medium", "low", "xhigh", "max", "minimal", *(supported or [])]
    for candidate in candidates:
        if (isinstance(candidate, str) and candidate.strip() and candidate != "none"
                and (supported is None or candidate in supported)):
            return candidate
    return "high"
