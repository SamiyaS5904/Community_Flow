"""
engine/prompt_builder.py
=========================
Centralized prompt engineering layer — multi-tenant aware.

Two tiers of system prompt:
  - CONTENT agents (writer, qa, research, pdf_writer):
      Full brand-voice block — audience psychology, format rules, content strategy.
      Built from group.* so it reflects the active tenant.
  - STRUCTURAL agents (planner, asset_planner, asset_mapper):
      Minimal prompt — one identity line + JSON-output instruction only.
      These agents output JSON schedules / template picks, not reader-facing text.
      Injecting Telegram formatting or audience psychology wastes tokens and
      creates instruction conflicts.

Landlord rules (hardcoded here, never duplicated per-group):
  - Anti-AI-phrase rules
  - Hook → Value → Takeaway → CTA shape
  - "Never write like a corporate robot / coaching institute / ChatGPT" rule
  - QA rejection criteria (robotic wording, prompt leakage, walls of text)

Tenant rules (pulled from group.*):
  - Identity / brand description
  - Audience description
  - Tone
  - Avoid-phrases
  - Word count range
  - Emoji policy
"""
import json
import re
from typing import Optional


# Agents whose system prompt should be the full content-writing voice block
_CONTENT_AGENT_TYPES = {"writer", "qa", "research", "pdf_writer"}

# Of those, the ones writing a Telegram message and so bound by its word count
# and formatting. The PDF Writer shares the voice but not the limits: giving it
# "keep messages between 200-450 words" made it cut the worked example out of
# every section of a multi-page guide to comply.
_TELEGRAM_AGENT_TYPES = {"writer", "qa", "research"}

# Agents that only need a minimal structural prompt
_STRUCTURAL_AGENT_TYPES = {"planner", "asset_planner", "asset_mapper"}


class PromptBuilder:
    """
    Assembles the system and user prompts for every LLM call from the files
    under prompts/system/. This class chooses which blocks apply and in what
    order; it does not author any of their text.

    Call build_system_prompt(group, agent_type) to get the right tier.
    """

    @classmethod
    def build_system_prompt(cls, group=None, agent_type: str = "writer", is_json: bool = False) -> str:
        """
        Build a system prompt appropriate for the given agent type.

        Args:
            group: GroupConfig for the active tenant. Required for content agents.
            agent_type: One of the known agent type strings. Defaults to 'writer'.
            is_json: If True, appends the JSON-only output instruction.

        Returns:
            A complete system prompt string.
        """
        from engine.prompts import render

        agent_type = (agent_type or "writer").lower()

        if agent_type in _STRUCTURAL_AGENT_TYPES:
            # Minimal prompt — no audience/format/tone noise
            base = render(
                "system/structural",
                group_name=group.name if group else "the content engine",
            )
        elif group:
            if agent_type in _TELEGRAM_AGENT_TYPES:
                format_block = render(
                    "system/format_rules",
                    word_count_min=group.word_count_min,
                    word_count_max=group.word_count_max,
                    word_count_hard_max=group.word_count_max + 30,
                    emoji_policy=group.emoji_policy,
                )
            else:
                # A guide is not a chat message. Handing the PDF Writer the
                # Telegram word ceiling made it drop the worked example from
                # every section to stay under it.
                format_block = render("system/document_rules")

            base = "\n".join([
                render("system/content_voice",
                       group_name=group.name,
                       group_description=group.description,
                       tone=group.tone),
                render("system/hard_rules"),
                render("system/audience",
                       audience_description=group.audience_description),
                format_block,
                render("system/content_strategy"),
            ])
        else:
            # No tenant resolved. Should not happen in normal flow — the caller
            # has lost track of which group it is writing for.
            base = "\n".join([
                render("system/content_voice_fallback"),
                render("system/hard_rules"),
                render("system/content_strategy"),
            ])

        if is_json:
            base += "\n\n" + render("system/json_output")
        return base

    @classmethod
    def build_user_prompt(cls, agent: Optional[dict], context: str) -> str:
        if not agent:
            return context

        return (
            f"YOUR ROLE: {agent.get('role', 'Assistant')}\n"
            f"YOUR GOAL: {agent.get('goal', '')}\n"
            f"SPECIFIC INSTRUCTIONS:\n{agent.get('instructions', '')}\n\n"
            f"--- INPUT ---\n"
            f"{context}\n\n"
            f"--- EXPECTED OUTPUT ---\n"
            f"{agent.get('expected_output', 'A high-quality, human response.')}"
        )


class ResponseCleaner:
    """Cleans and validates LLM outputs before they reach the application."""

    # Phrases that indicate the LLM is sounding like a generic AI
    AI_PHRASES = [
        "as an ai", "i am an ai", "delve into", "in conclusion",
        "here is a", "i hope this helps", "certainly!", "of course!",
        "absolutely!", "great question", "i'd be happy to",
    ]

    @staticmethod
    def clean(text: str) -> str:
        if not text:
            return ""

        # Strip markdown code fences
        text = re.sub(r"^```(?:json|markdown|python)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        # Normalize whitespace
        text = text.strip()
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Ensure bullet points have a space after the marker
        text = re.sub(r"^([*\-])(?=[a-zA-Z0-9])", r"\1 ", text, flags=re.MULTILINE)

        # Remove empty headings
        text = re.sub(r"^#+\s*$\n", "", text, flags=re.MULTILINE)

        return text

    #: A guide needs enough sections to be worth downloading.
    MIN_SECTIONS = 3

    @staticmethod
    def validate_pdf_document(text: str) -> tuple:
        """Check a PDF guide OUTLINE is complete enough to expand.

        Bodies are written by a later per-section call, because asking one call
        for a whole guide reliably produced 30-word sections however the prompt
        was worded — the model will not write long prose inside a JSON field.
        What this checks is that the outline has the parts that pass needs.
        """
        try:
            doc = json.loads(text)
        except Exception as exc:
            return False, f"Invalid JSON: {exc}"

        sections = doc.get("SECTIONS")
        if not isinstance(sections, list) or len(sections) < ResponseCleaner.MIN_SECTIONS:
            return False, (
                f"SECTIONS must be a list of at least {ResponseCleaner.MIN_SECTIONS} "
                f"sections; got {len(sections) if isinstance(sections, list) else 'none'}."
            )

        problems = []
        for i, section in enumerate(sections, 1):
            if not isinstance(section, dict):
                problems.append(f"section {i} is not an object")
                continue
            if not str(section.get("heading", "")).strip():
                problems.append(f"section {i} has no heading")
            if len(str(section.get("intent", "")).split()) < 5:
                problems.append(f"section {i} needs a one-sentence intent")

        if len(str(doc.get("INTRO", "")).split()) < 40:
            problems.append("INTRO should be two paragraphs of 50-80 words")
        if len(doc.get("CHECKLIST") or []) < 4:
            problems.append("CHECKLIST needs at least 4 items")
        if not str(doc.get("TITLE", "")).strip():
            problems.append("TITLE is empty")

        if problems:
            return False, "Outline incomplete. Fix all of: " + "; ".join(problems[:6])
        return True, ""

    @staticmethod
    def validate(text: str, is_json: bool = False, word_count_max: int = 300,
                 agent_type: str = "") -> tuple:
        """
        Validate LLM output.

        Args:
            text: The cleaned LLM response.
            is_json: Whether the response should be valid JSON.
            word_count_max: Maximum allowed word count for non-JSON responses.
                            Comes from group.word_count_max so it's tenant-aware.

        Returns:
            (is_valid: bool, error_message: str)
        """
        if not text:
            return False, "Response is empty."

        if is_json:
            if agent_type == "pdf_writer":
                return ResponseCleaner.validate_pdf_document(text)
            try:
                json.loads(text)
                return True, ""
            except Exception as e:
                return False, f"Invalid JSON: {e}"

        # Check for AI-sounding phrases
        lower = text.lower()
        for phrase in ResponseCleaner.AI_PHRASES:
            if phrase in lower:
                return False, f"AI-sounding phrase detected: '{phrase}'"

        # Reject if JSON leaked into text response
        if not is_json and re.search(r'\{["\'"].*?["\'"]:', text, re.DOTALL):
            return False, "Raw JSON detected in text response."

        # Word limit check — uses tenant-specific max, not a hardcoded constant
        word_count = len(text.split())
        if word_count > word_count_max:
            return False, f"Response too long: {word_count} words (max {word_count_max})."

        return True, ""
