"""Load role prompts from Markdown files with safe YAML front matter.

Prompt files are deliberately data, not executable code.  This module only
parses metadata and substitutes explicit ``{{ dotted.placeholders }}`` from a
caller-provided context; it never evaluates expressions in a prompt.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PromptError(ValueError):
    """Base class for prompt loading and rendering errors."""


class PromptFrontMatterError(PromptError):
    """The Markdown front matter is malformed or not a mapping."""


class PromptRenderError(PromptError):
    """A prompt contains a placeholder that cannot be rendered safely."""


_TEMPLATE_PATTERN = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:\|\s*(json|text)\s*)?\}\}"
)


def _strip_yaml_comment(value: str) -> str:
    """Strip a YAML comment while preserving # inside a quoted scalar."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _fallback_scalar(value: str) -> Any:
    """Parse the scalar subset used by bundled prompts when PyYAML is absent."""

    value = _strip_yaml_comment(value).strip()
    if not value:
        return None
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith(("'", '"')):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise PromptFrontMatterError(f"Invalid quoted scalar: {value!r}") from exc
    if value.startswith("[") and value.endswith("]"):
        inside = value[1:-1].strip()
        if not inside:
            return []
        # JSON is preferred, while unquoted YAML words are common in prompt
        # metadata.  A compact comma split is sufficient for that fallback.
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [_fallback_scalar(item) for item in inside.split(",")]
        if not isinstance(parsed, list):
            raise PromptFrontMatterError("Inline list must decode to a list")
        return parsed
    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PromptFrontMatterError("Inline mapping must be valid JSON without PyYAML") from exc
        if not isinstance(parsed, dict):
            raise PromptFrontMatterError("Inline mapping must decode to a mapping")
        return parsed
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", value):
            return float(value)
    except ValueError:
        pass
    return value


def _fallback_yaml_mapping(text: str) -> dict[str, Any]:
    """A conservative YAML fallback for simple prompt front matter.

    The normal path uses ``yaml.safe_load`` when PyYAML is available.  This
    fallback supports scalar keys, inline lists, and indented lists/mappings so
    a missing optional dependency does not make the prompt system unusable.
    """

    raw_lines = [line.rstrip("\r\n") for line in text.splitlines()]
    lines = [line for line in raw_lines if line.strip() and not line.lstrip().startswith("#")]

    def indentation(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        is_list = lines[index].lstrip().startswith("- ")
        container: Any = [] if is_list else {}
        while index < len(lines):
            line = lines[index]
            current_indent = indentation(line)
            if current_indent < indent:
                break
            if current_indent > indent:
                raise PromptFrontMatterError(f"Unexpected indentation in front matter: {line!r}")
            stripped = line.strip()
            if is_list:
                if not stripped.startswith("- "):
                    break
                raw_value = stripped[2:].strip()
                index += 1
                if raw_value:
                    container.append(_fallback_scalar(raw_value))
                elif index < len(lines) and indentation(lines[index]) > indent:
                    child, index = parse_block(index, indentation(lines[index]))
                    container.append(child)
                else:
                    container.append(None)
                continue

            if stripped.startswith("- "):
                break
            if ":" not in stripped:
                raise PromptFrontMatterError(f"Expected 'key: value' in front matter: {line!r}")
            key, raw_value = stripped.split(":", 1)
            key = key.strip()
            if not key:
                raise PromptFrontMatterError("Front matter contains an empty key")
            if key in container:
                raise PromptFrontMatterError(f"Duplicate front matter key: {key!r}")
            raw_value = raw_value.strip()
            index += 1
            if raw_value:
                container[key] = _fallback_scalar(raw_value)
            elif index < len(lines) and indentation(lines[index]) > indent:
                container[key], index = parse_block(index, indentation(lines[index]))
            else:
                container[key] = None
        return container, index

    if not lines:
        return {}
    parsed, index = parse_block(0, indentation(lines[0]))
    if index != len(lines) or not isinstance(parsed, dict):
        raise PromptFrontMatterError("Front matter must be a mapping")
    return parsed


def _yaml_mapping(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return _fallback_yaml_mapping(text)

    class UniqueKeySafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise PromptFrontMatterError("Front matter keys must be strings")
            if key in result:
                raise PromptFrontMatterError(f"Duplicate front matter key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        parsed = yaml.load(text, Loader=UniqueKeySafeLoader)
    except PromptFrontMatterError:
        raise
    except yaml.YAMLError as exc:
        raise PromptFrontMatterError(f"Invalid YAML front matter: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise PromptFrontMatterError("Front matter must be a mapping")
    return parsed


def parse_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    """Split Markdown into a front-matter mapping and prompt body.

    A leading UTF-8 BOM is tolerated.  A delimiter only counts as front matter
    when it is the first Markdown line, which avoids treating a later thematic
    break as configuration accidentally.
    """

    if markdown.startswith("\ufeff"):
        markdown = markdown[1:]
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, markdown
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            front_matter = "".join(lines[1:index])
            return _yaml_mapping(front_matter), "".join(lines[index + 1 :])
    raise PromptFrontMatterError("Opening front-matter delimiter has no closing delimiter")


def _resolve_dotted_value(context: Mapping[str, Any], expression: str) -> Any:
    value: Any = context
    for part in expression.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            raise KeyError(expression)
    return value


def _render_value(value: Any, output_format: str | None) -> str:
    if output_format == "json" or isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PromptRenderError("Template value is not JSON serializable") from exc
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_template(body: str, context: Mapping[str, Any] | None = None, *, strict: bool = True) -> str:
    """Render ``{{ key }}`` or ``{{ key | json }}`` placeholders safely."""

    values: Mapping[str, Any] = context or {}

    def replace(match: re.Match[str]) -> str:
        expression, output_format = match.groups()
        try:
            value = _resolve_dotted_value(values, expression)
        except KeyError:
            if strict:
                raise PromptRenderError(f"Missing template value: {expression}") from None
            return match.group(0)
        return _render_value(value, output_format)

    return _TEMPLATE_PATTERN.sub(replace, body)


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    """A role prompt and its declarative metadata."""

    path: Path
    metadata: dict[str, Any]
    body: str
    source_hash: str

    @property
    def prompt_id(self) -> str | None:
        value = self.metadata.get("id")
        return value if isinstance(value, str) else None

    @property
    def rendered_body(self) -> str:
        """The unparameterized body (use :meth:`render` when context is needed)."""

        return self.body

    def render(self, context: Mapping[str, Any] | None = None, *, strict: bool = True) -> str:
        return render_template(self.body, context, strict=strict)


def load_prompt(path: str | Path) -> PromptDefinition:
    """Read one UTF-8 Markdown prompt file without caching it."""

    prompt_path = Path(path)
    try:
        source = prompt_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PromptError(f"Could not read prompt {prompt_path}: {exc}") from exc
    metadata, body = parse_front_matter(source)
    return PromptDefinition(
        path=prompt_path.resolve(),
        metadata=metadata,
        body=body,
        source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


class PromptLoader:
    """Resolve prompt paths below one trusted prompt root."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, reference: str | Path) -> Path:
        candidate = Path(reference)
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".md")
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PromptError(f"Prompt path escapes configured root: {reference}") from exc
        return candidate

    def load(self, reference: str | Path) -> PromptDefinition:
        return load_prompt(self.resolve(reference))

    def render(
        self,
        reference: str | Path,
        context: Mapping[str, Any] | None = None,
        *,
        strict: bool = True,
    ) -> str:
        return self.load(reference).render(context, strict=strict)
