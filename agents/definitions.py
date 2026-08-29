"""
agents/definitions.py
======================
One function per pipeline agent. Each takes a GroupConfig and returns the agent
dict the workflow and OpenAIService expect.

These functions are deliberately thin. They derive tenant values from the group
config and hand them to prompts/agents/<name>.md — they do not author prompt
text. Changing how the Writer sounds means editing prompts/agents/writer.md,
not this file.

Landlord vs tenant:
  - Landlord (prompts/agents/ and prompts/system/): structural formatting rules,
    anti-AI-phrase rules, Hook->Value->Takeaway->CTA shape, JSON-output rules,
    QA rejection criteria.
  - Tenant (from group.*): identity, audience, tone, avoid-phrases, word-count
    range, emoji policy, category IDs, active CTA text.

Adding a new group = create its config.yaml. Zero changes needed here.
"""
from __future__ import annotations

from engine.group_config import GroupConfig
from engine.prompts import load_agent


def _website(group: GroupConfig) -> str:
    """The public-facing site, taken from the footer's 'name | site' form."""
    return group.footer.split("|")[-1].strip() if "|" in group.footer else group.name


def planner_agent(group: GroupConfig) -> dict:
    """Decides what topics to post about today and returns a strict JSON schedule."""
    return load_agent(
        "planner",
        group_name=group.name,
        category_ids=", ".join(group.all_category_ids),
    )


def research_agent(group: GroupConfig) -> dict:
    """Synthesizes search results into actionable insights for the group's audience."""
    return load_agent(
        "research",
        group_name=group.name,
        audience_description=group.audience_description,
        tone=group.tone,
    )


def writer_agent(group: GroupConfig) -> dict:
    """Writes an engaging, on-brand Telegram post for the group's audience."""
    avoid_list = (
        "\n".join(f"- {p}" for p in group.avoid_phrases)
        if group.avoid_phrases else "- None specified"
    )
    active_ctas = group.get_active_ctas()
    cta_examples = (
        "\n".join(f"- {c.text.strip()}" for c in active_ctas)
        if active_ctas else "- (no active CTAs)"
    )
    return load_agent(
        "writer",
        group_name=group.name,
        group_description=group.description,
        audience_description=group.audience_description,
        tone=group.tone,
        avoid_list=avoid_list,
        word_count_min=group.word_count_min,
        word_count_max=group.word_count_max,
        word_count_hard_max=group.word_count_max + 30,
        emoji_policy=group.emoji_policy,
        cta_examples=cta_examples,
    )


def qa_agent(group: GroupConfig) -> dict:
    """Reviews and polishes the draft to ensure it meets editorial standards."""
    return load_agent(
        "qa",
        avoid_phrases=", ".join(group.avoid_phrases) if group.avoid_phrases else "none specified",
        word_count_max=group.word_count_max,
    )


def pdf_writer_agent(group: GroupConfig) -> dict:
    """Generates long-form, structured educational content as JSON for PDF export."""
    return load_agent(
        "pdf_writer",
        group_name=group.name,
        audience_description=group.audience_description,
    )


def asset_planner_agent(group: GroupConfig) -> dict:
    """Selects the design template, export format, and caption strategy."""
    return load_agent("asset_planner", group_name=group.name)


def asset_mapper_agent(group: GroupConfig) -> dict:
    """Maps final content onto the canonical asset document templates consume."""
    return load_agent(
        "asset_mapper",
        logo_light="logo_light.png",
        website=_website(group),
        cta_default=f"Join {group.name}",
    )


AGENT_FACTORIES = {
    "planner": planner_agent,
    "research": research_agent,
    "writer": writer_agent,
    "qa": qa_agent,
    "pdf_writer": pdf_writer_agent,
    "asset_planner": asset_planner_agent,
    "asset_mapper": asset_mapper_agent,
}


# ---------------------------------------------------------------------------
# Backward-compatibility shims.
#
# These resolve against the DEFAULT group, not the active one, so any call site
# still using them silently produces another tenant's prompt. They exist only so
# nothing breaks mid-migration; each remaining use is a tracked bug (P1-8).
# Do not add new ones.
# ---------------------------------------------------------------------------
def _default_group() -> GroupConfig:
    from engine.group_config import load_group_config
    return load_group_config("placement_prep")


class _LazyAgent(dict):
    """A dict that builds itself from the default group on first access."""

    def __init__(self, factory):
        self._factory = factory
        super().__init__()

    def _get_dict(self) -> dict:
        return self._factory(_default_group())

    def __getitem__(self, key):
        return self._get_dict()[key]

    def get(self, key, default=None):
        return self._get_dict().get(key, default)

    def __contains__(self, key):
        return key in self._get_dict()

    def __iter__(self):
        return iter(self._get_dict())

    def __len__(self):
        return len(self._get_dict())

    def items(self):
        return self._get_dict().items()

    def keys(self):
        return self._get_dict().keys()

    def values(self):
        return self._get_dict().values()

    def __repr__(self):
        return repr(self._get_dict())


PLANNER_AGENT = _LazyAgent(planner_agent)
RESEARCH_AGENT = _LazyAgent(research_agent)
WRITER_AGENT = _LazyAgent(writer_agent)
QA_AGENT = _LazyAgent(qa_agent)
PDF_WRITER_AGENT = _LazyAgent(pdf_writer_agent)
ASSET_PLANNER_AGENT = _LazyAgent(asset_planner_agent)
ASSET_MAPPER_AGENT = _LazyAgent(asset_mapper_agent)
