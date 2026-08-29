"""
platform/group_config.py
========================
Loads and exposes a group's configuration from its config.yaml file.

Usage:
    from engine.group_config import load_group_config
    group = load_group_config("placement_prep")

    print(group.name)               # "Campus Placement Prep"
    print(group.telegram_chat_id)   # reads from .env via chat_id_env key
    
Design:
    Every group lives in  groups/<group_id>/config.yaml
    Adding a new group = create that folder and file.
    No code changes needed anywhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml

from engine.config import config


@dataclass
class ContentCategory:
    """Represents one content category (e.g., 'resume_tip', 'motivation')."""
    id: str
    name: str
    search_required: bool
    frequency_weight: int
    hashtags: list[str]


@dataclass
class CTAOption:
    """A Call-to-Action that can optionally be appended to posts."""
    id: str
    text: str
    active: bool


@dataclass
class GroupConfig:
    """
    Complete, fully-loaded configuration for one Telegram group.

    This is what every agent and service receives.
    All runtime values (env vars, file paths) are resolved here.
    """
    # Identity
    id: str
    name: str
    tagline: str
    description: str
    telegram_chat_id: str           # Resolved from .env
    telegram_admin_chat_id: str     # Resolved from .env

    # Brand
    primary_color: str
    secondary_color: str
    accent_color: str
    footer: str
    always_hashtags: list[str]
    #: Short label for the CTA pill on a rendered asset. The CTAs under
    #: `cta.available_ctas` are sentences written for the end of a Telegram
    #: post; a pill on a 1080px canvas needs two or three words.
    cta_label: str
    #: Which surface this group's assets use: "dark", "light" or "editorial".
    #: Theme is a token swap, not a separate template.
    theme: str
    #: Optional per-group type pairing: {"heading": ..., "body": ...}, naming
    #: one of the four self-hosted families by key (display / editorial / body /
    #: mono). Only self-hosted families are allowed — a name the renderer
    #: cannot load falls back silently, and renders must not touch the network.
    fonts: dict

    # Audience
    audience_description: str
    tone: str
    avoid_phrases: list[str]

    # Content
    categories: list[ContentCategory]
    posts_per_day: int
    max_posts_per_day: int
    approval_mode: bool
    #: IANA name, e.g. "Asia/Kolkata". Slot times in the strategy and schedule
    #: times typed into the dashboard are read in this zone. It was hardcoded
    #: to IST for every tenant, which is wrong the moment a group is elsewhere.
    timezone: str

    # CTA rules
    min_educational_before_cta: int
    max_cta_per_day: int
    available_ctas: list[CTAOption]

    # Post format
    word_count_min: int
    word_count_max: int
    emoji_policy: str

    def get_category(self, category_id: str) -> Optional[ContentCategory]:
        """Look up a category by its id string."""
        for cat in self.categories:
            if cat.id == category_id:
                return cat
        return None

    def get_active_ctas(self) -> list[CTAOption]:
        """Returns only CTAs that are currently enabled."""
        return [cta for cta in self.available_ctas if cta.active]

    def needs_search(self, category_id: str) -> bool:
        """Returns True if this content type should use Serper search."""
        cat = self.get_category(category_id)
        return cat.search_required if cat else False

    @property
    def tz(self):
        """This group's timezone as a tzinfo, falling back to UTC if the name
        in config is not one Python knows."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            import logging
            logging.getLogger(__name__).warning(
                "Group %s has an unknown timezone %r; using UTC.", self.id, self.timezone)
            from datetime import timezone as _tz
            return _tz.utc

    @property
    def all_category_ids(self) -> list[str]:
        return [cat.id for cat in self.categories]


def load_group_config(group_id: str) -> GroupConfig:
    """
    Load and return a fully resolved GroupConfig from the group's YAML file.

    Args:
        group_id: The folder name under groups/  (e.g. "placement_prep")

    Raises:
        FileNotFoundError: If the group's config.yaml doesn't exist.
        ValueError: If required fields are missing in the YAML.
    """
    config_path = config.GROUPS_DIR / group_id / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Group config not found: {config_path}\n"
            f"Create the file to add the '{group_id}' group."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    g = raw["group"]
    brand = raw["brand"]
    audience = raw["audience"]
    posting = raw["posting"]
    cta_conf = raw["cta"]
    fmt = raw["post_format"]

    # Resolve Telegram chat IDs from .env (not stored in YAML for security)
    chat_id = os.getenv(g.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID"), "")
    admin_chat_id = os.getenv(g.get("telegram_admin_chat_id_env", "TELEGRAM_ADMIN_CHAT_ID"), "")

    categories = [
        ContentCategory(
            id=cat["id"],
            name=cat["name"],
            search_required=cat.get("search_required", False),
            frequency_weight=cat.get("frequency_weight", 1),
            hashtags=cat.get("hashtags", []),
        )
        for cat in raw.get("content_categories", [])
    ]

    available_ctas = [
        CTAOption(
            id=c["id"],
            text=c["text"].strip(),
            active=c.get("active", False),
        )
        for c in cta_conf.get("available_ctas", [])
    ]

    return GroupConfig(
        id=g["id"],
        name=g["name"],
        tagline=g.get("tagline", ""),
        description=g.get("description", ""),
        telegram_chat_id=chat_id,
        telegram_admin_chat_id=admin_chat_id,

        primary_color=brand.get("primary_color", "#FF6B35"),
        secondary_color=brand.get("secondary_color", "#2D2D2D"),
        accent_color=brand.get("accent_color", "#FFD700"),
        footer=brand.get("footer", "Powered by Carrot Owl Education"),
        always_hashtags=brand.get("hashtags_always", []),
        cta_label=brand.get("cta_label", "").strip() or f"Join {g['name']}",
        theme=(brand.get("theme", "dark") or "dark").strip().lower(),
        fonts={k: v for k, v in (brand.get("fonts") or {}).items() if v},

        audience_description=audience.get("description", ""),
        tone=audience.get("tone", "practical and friendly"),
        avoid_phrases=audience.get("avoid", []),

        categories=categories,
        posts_per_day=posting.get("posts_per_day", 4),
        max_posts_per_day=posting.get("max_posts_per_day", 5),
        approval_mode=posting.get("approval_mode", True),
        timezone=(posting.get("timezone") or "Asia/Kolkata").strip(),

        min_educational_before_cta=cta_conf.get("min_educational_before_cta", 3),
        max_cta_per_day=cta_conf.get("max_cta_per_day", 1),
        available_ctas=available_ctas,

        word_count_min=fmt["word_count"].get("min", 200),
        word_count_max=fmt["word_count"].get("max", 450),
        emoji_policy=fmt.get("emoji_policy", "Use 3-5 emojis naturally"),
    )


def list_available_groups() -> list[str]:
    """Returns a list of all configured group IDs."""
    if not config.GROUPS_DIR.exists():
        return []
    return [
        d.name
        for d in config.GROUPS_DIR.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    ]

