from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


class UnsupportedPromptFormat(ValueError):
    """Raised when model metadata names a format this client cannot render safely."""


@dataclass(frozen=True, slots=True)
class FormattedPrompt:
    prompt: str
    stop_sequences: tuple[str, ...]
    boundary_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptTurn:
    role: Literal["user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("Prompt turn role must be 'user' or 'assistant'")


_CHATML_TOKENS = ("<|im_start|>", "<|im_end|>")
_LLAMA3_TOKENS = (
    "<|begin_of_text|>",
    "<|start_header_id|>",
    "<|end_header_id|>",
    "<|eot_id|>",
    "<|end_of_text|>",
)
_MISTRAL_TOKENS = ("<s>", "</s>", "[INST]", "[/INST]")
_GEMMA_TOKENS = ("<bos>", "<start_of_turn>", "<end_of_turn>")
_ALPACA_TOKENS = (
    "### System:",
    "### Instruction:",
    "### Response Instructions:",
    "### Response:",
)
_ALL_BOUNDARY_TOKENS = tuple(
    sorted(
        {
            *_CHATML_TOKENS,
            *_LLAMA3_TOKENS,
            *_MISTRAL_TOKENS,
            *_GEMMA_TOKENS,
            *_ALPACA_TOKENS,
        },
        key=len,
        reverse=True,
    )
)


def _neutralize_boundaries(text: str) -> str:
    safe = text.replace("\x00", "")
    for token in _ALL_BOUNDARY_TOKENS:
        safe = safe.replace(token, token[0] + "\u200b" + token[1:])
    return safe


def _alternating_turns(turns: tuple[PromptTurn, ...]) -> tuple[PromptTurn, ...]:
    """Adapt multi-user Discord history to strictly alternating chat templates."""
    normalized: list[PromptTurn] = []
    for turn in turns:
        if not normalized and turn.role == "assistant":
            continue
        if normalized and normalized[-1].role == turn.role:
            normalized[-1] = PromptTurn(
                turn.role,
                f"{normalized[-1].content}\n{turn.content}",
            )
        else:
            normalized.append(turn)
    return tuple(normalized)


def _chatml(
    system: str,
    user: str,
    history: tuple[PromptTurn, ...],
    post_history: str,
) -> FormattedPrompt:
    conversation = "".join(
        f"<|im_start|>{turn.role}\n{turn.content}<|im_end|>\n" for turn in history
    )
    final_instruction = (
        f"<|im_start|>system\n{post_history}<|im_end|>\n" if post_history else ""
    )
    return FormattedPrompt(
        prompt=(
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"{conversation}"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"{final_instruction}"
            "<|im_start|>assistant\n"
        ),
        stop_sequences=("<|im_end|>", "<|im_start|>"),
        boundary_tokens=_CHATML_TOKENS,
    )


def _llama3(
    system: str,
    user: str,
    history: tuple[PromptTurn, ...],
    post_history: str,
) -> FormattedPrompt:
    conversation = "".join(
        f"<|start_header_id|>{turn.role}<|end_header_id|>\n\n"
        f"{turn.content}<|eot_id|>"
        for turn in history
    )
    final_instruction = (
        "<|start_header_id|>system<|end_header_id|>\n\n"
        f"{post_history}<|eot_id|>"
        if post_history
        else ""
    )
    return FormattedPrompt(
        prompt=(
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n"
            f"{system}<|eot_id|>{conversation}"
            "<|start_header_id|>user<|end_header_id|>\n\n"
            f"{user}<|eot_id|>{final_instruction}"
            "<|start_header_id|>assistant<|end_header_id|>\n\n"
        ),
        stop_sequences=("<|eot_id|>", "<|end_of_text|>"),
        boundary_tokens=_LLAMA3_TOKENS,
    )


def _mistral(
    system: str,
    user: str,
    history: tuple[PromptTurn, ...],
    post_history: str,
) -> FormattedPrompt:
    final_user = f"{user}\n\n{post_history}" if post_history else user
    turns = _alternating_turns((*history, PromptTurn("user", final_user)))
    rendered: list[str] = ["<s>"]
    first_user = True
    for turn in turns:
        if turn.role == "user":
            content = f"{system}\n\n{turn.content}" if first_user else turn.content
            rendered.append(f"[INST] {content} [/INST]")
            first_user = False
        else:
            rendered.append(f"{turn.content}</s>")
    return FormattedPrompt(
        prompt="".join(rendered),
        stop_sequences=("</s>", "[INST]"),
        boundary_tokens=_MISTRAL_TOKENS,
    )


def _gemma(
    system: str,
    user: str,
    history: tuple[PromptTurn, ...],
    post_history: str,
) -> FormattedPrompt:
    final_user = f"{user}\n\n{post_history}" if post_history else user
    turns = _alternating_turns((*history, PromptTurn("user", final_user)))
    rendered: list[str] = ["<bos>"]
    first_user = True
    for turn in turns:
        role = "model" if turn.role == "assistant" else "user"
        content = (
            f"{system}\n\n{turn.content}"
            if turn.role == "user" and first_user
            else turn.content
        )
        rendered.append(f"<start_of_turn>{role}\n{content}<end_of_turn>\n")
        if turn.role == "user":
            first_user = False
    rendered.append("<start_of_turn>model\n")
    return FormattedPrompt(
        prompt="".join(rendered),
        stop_sequences=("<end_of_turn>", "<start_of_turn>"),
        boundary_tokens=_GEMMA_TOKENS,
    )


def _alpaca(
    system: str,
    user: str,
    history: tuple[PromptTurn, ...],
    post_history: str,
) -> FormattedPrompt:
    rendered = [f"### System:\n{system}\n\n"]
    for turn in history:
        heading = "### Instruction:" if turn.role == "user" else "### Response:"
        rendered.append(f"{heading}\n{turn.content}\n\n")
    rendered.append(f"### Instruction:\n{user}\n\n")
    if post_history:
        rendered.append(f"### Response Instructions:\n{post_history}\n\n")
    rendered.append("### Response:\n")
    return FormattedPrompt(
        prompt="".join(rendered),
        stop_sequences=("### Instruction:", "### System:"),
        boundary_tokens=_ALPACA_TOKENS,
    )


_FORMATTERS: dict[
    str,
    Callable[[str, str, tuple[PromptTurn, ...], str], FormattedPrompt],
] = {}


def _register(
    names: tuple[str, ...],
    formatter: Callable[[str, str, tuple[PromptTurn, ...], str], FormattedPrompt],
) -> None:
    for name in names:
        _FORMATTERS[name.casefold()] = formatter


_register(("ChatML",), _chatml)
_register(("Llama 3", "Llama3", "Llama 3 Instruct", "Llama 3 Chat"), _llama3)
_register(
    ("Mistral", "Mistral V3", "Mistral V3 Tekken", "Mistral V7 Tekken"),
    _mistral,
)
_register(("Gemma", "Gemma 2", "Gemma 4"), _gemma)
_register(("Alpaca",), _alpaca)


def supported_instruction_formats() -> tuple[str, ...]:
    return (
        "Alpaca",
        "ChatML",
        "Gemma",
        "Gemma 2",
        "Gemma 4",
        "Llama 3",
        "Llama 3 Chat",
        "Llama 3 Instruct",
        "Llama3",
        "Mistral",
        "Mistral V3",
        "Mistral V3 Tekken",
        "Mistral V7 Tekken",
    )


def is_supported_instruction_format(name: str) -> bool:
    return name.strip().casefold() in _FORMATTERS


def format_prompt(
    instruction_format: str,
    system_prompt: str,
    user_prompt: str,
    history: tuple[PromptTurn, ...] = (),
    post_history: str = "",
) -> FormattedPrompt:
    formatter = _FORMATTERS.get(instruction_format.strip().casefold())
    if formatter is None:
        raise UnsupportedPromptFormat(
            f"Unsupported AI Horde instruction format: {instruction_format!r}"
        )
    safe_history = tuple(
        PromptTurn(turn.role, _neutralize_boundaries(turn.content)) for turn in history
    )
    return formatter(
        _neutralize_boundaries(system_prompt),
        _neutralize_boundaries(user_prompt),
        safe_history,
        _neutralize_boundaries(post_history).strip(),
    )
