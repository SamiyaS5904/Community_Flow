"""
engine/workflow.py
===================
Orchestrates the platform workflow. No direct API calls here.

Multi-tenant design:
  - PlatformWorkflow requires a GroupConfig (group=) alongside the config_provider.
  - All agent calls pass self.group so prompts are tenant-aware.
  - No "placement_prep" or group-specific strings hardcoded anywhere in this file.
"""
from typing import Optional
import os
import uuid
import time
from datetime import datetime, timezone

from services.openai_service import OpenAIService
from services.search_service import SearchService
from services.storage.post_store import PostStore
from services.storage.models import PostState
from exceptions import GenerationError, SearchError, TelegramError
from services.telegram_service import TelegramService
from services.render_service import RenderService
from agents.definitions import (
    planner_agent,
    research_agent,
    writer_agent,
    qa_agent,
    pdf_writer_agent,
    asset_planner_agent,
    asset_mapper_agent,
)
from engine.group_config import GroupConfig
from engine.prompts import render as render_prompt
import json
import logging
import re

log = logging.getLogger(__name__)


def _parse_schedule(value: str | None):
    """Parse the dashboard's `YYYY-MM-DDTHH:MM` into an aware datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("Unparseable schedule time %r", value)
        return None



_TEMPLATE_PLACEHOLDER_CACHE: dict = {}

class PlatformWorkflow:
    def __init__(self, config_provider, group: GroupConfig):
        """
        Args:
            config_provider: The app config (API keys, paths, sheet name, etc.)
            group: GroupConfig for the active tenant. Drives all agent prompts,
                   word-count limits, brand identity, and blueprint path.
        """
        self.config = config_provider
        self.group = group

        # Build agent dicts once at init — tenant-aware, no rebuilding per call
        self.agents = {
            "planner":       planner_agent(group),
            "research":      research_agent(group),
            "writer":        writer_agent(group),
            "qa":            qa_agent(group),
            "pdf_writer":    pdf_writer_agent(group),
            "asset_planner": asset_planner_agent(group),
            "asset_mapper":  asset_mapper_agent(group),
        }

        self.llm = OpenAIService(api_key=self.config.OPENAI_API_KEY)
        self.search = SearchService(api_key=self.config.SEARCH_API_KEY)
        self.storage = PostStore()
        self.telegram = TelegramService(token=self.config.TELEGRAM_BOT_TOKEN, admin_chat_id=self.config.TELEGRAM_ADMIN_CHAT_ID)
        
        output_dir = os.path.join(self.config.PROJECT_ROOT, "generated")
        templates_dir = os.path.join(self.config.PROJECT_ROOT, "design_templates")
        self.renderer = RenderService(base_output_dir=output_dir, templates_dir=templates_dir)
        self._cycle_planner = None
        self._topic_pool = None

    # ------------------------------------------------------------------
    # Internal LLM call helpers — pass group for tenant-aware prompting
    # ------------------------------------------------------------------

    def _call_agent(self, agent: dict, context: str, use_cache: bool = True) -> str:
        """One agent call. `use_cache=False` is what makes Regenerate regenerate:
        the same topic and the same prompt hash to the same cache key, so the
        cached path handed back the identical draft the operator just rejected."""
        response = self.llm.generate_content(prompt=context, agent=agent,
                                             is_json=False, group=self.group,
                                             use_cache=use_cache)
        if response.startswith("[ERROR]"):
            raise GenerationError(response.removeprefix("[ERROR]").strip())
        return response

    def _call_agent_json(self, agent: dict, context: str, use_cache: bool = True) -> dict | list:
        response_text = self.llm.generate_content(prompt=context, agent=agent,
                                                  is_json=True, group=self.group,
                                                  use_cache=use_cache)
        if response_text.startswith("[ERROR]"):
            raise GenerationError(response_text.removeprefix("[ERROR]").strip())

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as exc:
            # An empty list here used to travel silently into the caller, which
            # read it as "the model chose nothing" rather than "the model
            # returned something unparseable".
            log.error("Agent %s returned unparseable JSON: %s",
                      agent.get("role", "?"), response_text[:300])
            raise GenerationError(
                f"{agent.get('role', 'An agent')} returned invalid JSON: {exc}"
            ) from exc

    def generate_single_content(self, slot: dict, recent_topics: str = "", schedule_time: str = "", save_to_sheets: bool = True, status_callback=None) -> dict:
        """Generates content for a single slot, calling Writer and QA agents."""
        from engine.blueprint_engine import enrich_slot
        slot = enrich_slot(slot)

        start_time = time.time()
        category = slot.get("category", "General")
        topic = slot.get("topic", "General Advice")
        
        # If no topic provided (manual override), generate a single topic string using a simple prompt
        if not topic:
            if status_callback: status_callback(f"Planning topic for {category}...")
            prompt_str = render_prompt(
                "tasks/topic_invention",
                category=category,
                recent_topics=recent_topics or "none recorded",
            )
            topic = self.llm.generate_content(prompt=prompt_str, group=self.group).strip()

        search_required = slot.get("search_required", False)
        pdf_required = slot.get("pdf_required", False)
        image_required = slot.get("image_required", False)
        cta_required = slot.get("cta", False)
        
        # 2. Research
        search_summary = ""
        if search_required:
            if status_callback: status_callback(f"Researching topic: {topic[:20]}...")
            # Serper going down is not a reason to lose the post. Research adds
            # currency to a draft; the Writer works without it, and the post
            # records search_used=False so the gap is visible in the dashboard
            # rather than pretended away.
            try:
                results = self.search.search(topic)
                formatted_results = self.search.format_results(topic, results)
                if formatted_results:
                    search_summary = self._call_agent(
                        self.agents["research"],
                        f"Topic: {topic}\nSearch Results:\n{formatted_results}")
                else:
                    log.warning("Search returned nothing for %r; writing without research.", topic)
            except SearchError as exc:
                log.warning("Research skipped for %r: %s", topic, exc)
                if status_callback:
                    status_callback("Search unavailable — writing without research.")
            
        post_id = str(uuid.uuid4())

        # 3. Write & Pipeline Dependency Inversion
        instruction = slot.get("instruction", "").strip()
        if not instruction:
            instruction = topic

        structured_items_summary = ""
        if pdf_required or image_required:
            if status_callback: status_callback(f"Structuring items for asset: {topic[:20]}...")
            mapper_prompt = render_prompt(
                "tasks/asset_content_brief",
                topic=topic,
                instruction=instruction,
                research=search_summary or "none",
            )
            try:
                placeholders = self._call_agent_json(self.agents["asset_mapper"], mapper_prompt)
                if isinstance(placeholders, list) and len(placeholders) > 0:
                    placeholders = placeholders[0]
            except Exception as e:
                log.warning("Asset Mapper returned no usable JSON: %s", e)
                placeholders = {}

            # Save generated placeholders to disk immediately
            if isinstance(placeholders, dict) and placeholders:
                placeholders_dir = os.path.join(self.config.PROJECT_ROOT, "generated", "placeholders")
                os.makedirs(placeholders_dir, exist_ok=True)
                placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
                try:
                    with open(placeholders_file, "w", encoding="utf-8") as f:
                        json.dump(placeholders, f, indent=4)
                except Exception as e:
                    log.warning("Could not cache placeholders for %s: %s", post_id[:8], e)

                # Build summary context for caption writer
                items_arr = placeholders.get("items", [])
                if isinstance(items_arr, list) and items_arr:
                    summary_titles = [f"- {it.get('title', '')}" for it in items_arr if isinstance(it, dict)]
                    structured_items_summary = "\nVisual Asset Items Summary:\n" + "\n".join(summary_titles)

        if status_callback: status_callback(f"Writing caption for: {topic[:20]}...")
        
        format_hint = ""
        is_poll = category.lower() in ["aptitude / mcq", "poll"]
        if is_poll:
            format_hint = "\n" + render_prompt("tasks/poll_format")
            
        draft_context = render_prompt(
            "tasks/draft_context",
            topic=topic,
            instruction=instruction,
            research=search_summary or "none",
            cta_required=cta_required,
        )
        if structured_items_summary:
            draft_context += "\n" + render_prompt(
                "tasks/asset_caption_note", items_summary=structured_items_summary
            )
        draft_context += format_hint
        
        if is_poll:
            draft = self.llm.generate_content(prompt=draft_context, agent=self.agents["writer"], is_json=True, group=self.group)
        else:
            draft = self._call_agent(self.agents["writer"], draft_context)
        
        # 4. QA
        if status_callback: status_callback(f"Checking quality for: {topic[:20]}...")
        if is_poll:
            final_content = draft
        else:
            final_content = self._call_agent(self.agents["qa"], f"Draft:\n{draft}")
            if final_content.startswith("APPROVED: ") or final_content.startswith("FIXED: "):
                final_content = final_content.split(": ", 1)[-1].strip()

        # Build the post row. The declared-asset flags are stored on the row
        # itself, so every later step — asset generation, the approval gate,
        # the publish reconciler — can tell what this post promised without
        # re-reading the plan it came from.
        generation_time = time.time() - start_time

        fields = {
            "id": post_id,
            "content_type": category,
            "category": category,
            "topic": topic.strip(),
            "title": topic.strip()[:120],
            "content": final_content,
            "search_used": bool(search_summary),
            "state": PostState.RENDERING if (pdf_required or image_required)
                     else PostState.NEEDS_REVIEW,
            "wants_pdf": bool(pdf_required),
            "wants_image": bool(image_required),
            "scheduled_for": _parse_schedule(schedule_time),
            "generation_seconds": round(generation_time, 2),
            # Provenance: which pool topic and which cycle this came from, so
            # publishing can mark the topic used and dedup counts it from then on.
            "topic_id": slot.get("topic_id"),
            "cycle_id": slot.get("cycle_id"),
        }

        if save_to_sheets:
            self.storage.create(self.group.id, **fields)
            if pdf_required or image_required:
                try:
                    self.generate_assets(
                        post_id,
                        topic.strip(),
                        final_content,
                        force_pdf_status="pending" if pdf_required else "N/A",
                        force_img_status="pending" if image_required else "N/A",
                        category=category
                    )
                except Exception as e:
                    # A declared asset that did not render must be visible, not
                    # silently absent — the post stays out of the approval queue
                    # until someone retries it.
                    log.error("Asset generation failed for post %s: %s", post_id, e)
                    self.storage.set_state(
                        post_id, PostState.ASSET_FAILED, error=f"Asset render failed: {e}"
                    )

        return {
            "id": post_id,
            "topic": topic,
            "content": final_content,
            "schedule_time": schedule_time,
            "fields": fields,
            # The declared asset flags, so callers do not have to re-derive them
            # from the slot they passed in. Reading them off the raw slot is what
            # made queue generation produce zero images.
            "wants_pdf": bool(pdf_required),
            "wants_image": bool(image_required),
        }

    def _get_blueprint_content(self) -> str:
        """Load the markdown content blueprint for the active group.

        Checks groups/<group.id>/content_blueprint.md first.
        Falls back to the root content_blueprint.md for backward compatibility.

        Raises:
            FileNotFoundError: if no blueprint file exists for this group,
                               with a clear message listing both paths that were checked.
        """
        group_path = os.path.join(self.config.PROJECT_ROOT, "groups", self.group.id, "content_blueprint.md")
        root_path = os.path.join(self.config.PROJECT_ROOT, "content_blueprint.md")

        if os.path.exists(group_path):
            with open(group_path, 'r', encoding='utf-8') as f:
                return f.read()

        if os.path.exists(root_path):
            with open(root_path, 'r', encoding='utf-8') as f:
                return f.read()

        raise FileNotFoundError(
            f"No content blueprint found for group '{self.group.id}'. "
            f"Checked: (1) {group_path}  (2) {root_path}. "
            "Create a content_blueprint.md inside the group folder before generating content."
        )

    def topic_pool(self):
        """This group's topic pool, gated by its own editorial guardrails.

        Separate from `cycle_planner` because the Topic Pool screen and the
        discovery job both need the pool without needing a planner.
        """
        from engine.planning.strategy import Strategy, StrategyNotFound
        from engine.planning.topic_pool import Guardrails, TopicPool
        from services.embedding_service import EmbeddingService

        if self._topic_pool is not None:
            return self._topic_pool
        try:
            guardrails = Guardrails.from_strategy(
                {"editorial_guardrails": Strategy.load(self.group).guardrails})
        except StrategyNotFound:
            # No strategy file yet: the defaults still dedup, they just do not
            # enforce editorial rules this group has not written down.
            guardrails = Guardrails()

        self._topic_pool = TopicPool(
            self.group,
            EmbeddingService(api_key=self.config.OPENAI_API_KEY),
            guardrails,
        )
        return self._topic_pool

    def cycle_planner(self):
        """The planner for this group's editorial cycle, or None if it has no
        structured strategy (markdown-only groups still use the Planner agent
        to read their plan directly)."""
        from engine.planning.cycle import CyclePlanner
        from engine.planning.strategy import Strategy, StrategyNotFound

        if self._cycle_planner is not None:
            return self._cycle_planner
        try:
            strategy = Strategy.load(self.group)
        except StrategyNotFound:
            return None

        pool = self.topic_pool()
        self._cycle_planner = CyclePlanner(
            self.group, strategy, pool,
            plan_with_model=lambda prompt: self._call_agent(self.agents["planner"], prompt),
        )
        return self._cycle_planner

    def plan_daily_queue(self, date_str: str, recent_topics: str) -> list[dict]:
        """The slots to generate for one date.

        A group with a structured strategy gets them from its cycle plan, which
        assigns pool topics to the rhythm the strategy declares. That is what
        stops the calendar replaying: the strategy no longer holds topics, so
        the same day next cycle draws different ones.
        """
        from engine.blueprint_engine import _load_blueprint, _compute_day_number

        planner = self.cycle_planner()
        if planner is not None:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                return planner.slots_for_date(target_date)
            except Exception as exc:
                log.error("Cycle planning failed for %s: %s", date_str, exc)
                raise

        # Fallback Path: AI-based parsing of Markdown Blueprint
        # _get_blueprint_content() raises FileNotFoundError loudly if missing —
        # the exception will propagate to the caller and the dashboard will
        # show a "no content source for this group" error instead of generating garbage.
        blueprint = self._get_blueprint_content()

        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        from engine.blueprint_engine import _ANCHOR_DATE
        # Derive duration from a temporary _load_blueprint attempt; fall back to 15
        try:
            duration, _ = _load_blueprint(self.group)
        except FileNotFoundError:
            duration = 15
        day_number = _compute_day_number(target_date, duration)

        context = render_prompt(
            "tasks/planner_day_context",
            blueprint=blueprint,
            day_number=day_number,
            date_str=date_str,
            recent_topics=recent_topics or "none recorded",
        )
        queue = self._call_agent_json(self.agents["planner"], context)

        if isinstance(queue, dict):
            for val in queue.values():
                if isinstance(val, list):
                    return val
            return []

        return queue if isinstance(queue, list) else []

    def generate_queue(self, date_str: str, status_callback=None) -> list[dict]:
        """Generate every slot the strategy declares for one date.

        A slot that fails is recorded in self.failed_slots and skipped; the
        rest of the day still generates.
        """
        self.failed_slots: list[dict] = []
        recent_topics = ", ".join(self.storage.recent_topics(self.group.id, limit=30))
        taken_slots = self.storage.scheduled_slots(self.group.id)

        if status_callback:
            status_callback("Planning daily queue...")
        planned_slots = self.plan_daily_queue(date_str, recent_topics)

        results = []
        for i, slot in enumerate(planned_slots):
            sched_time = f"{date_str}T{slot.get('time', '12:00')}"
            if sched_time in taken_slots:
                continue

            if status_callback:
                status_callback(
                    f"Generating post {i + 1}/{len(planned_slots)}: {slot.get('category')}..."
                )
            try:
                res = self.generate_single_content(
                    slot,
                    recent_topics=recent_topics,
                    schedule_time=sched_time,
                    save_to_sheets=False,
                    status_callback=status_callback,
                )
            except Exception as exc:
                # One slot failing must not cost the rest of the day. The
                # failure is recorded and reported, not swallowed.
                log.exception("Slot %s failed to generate", sched_time)
                self.failed_slots.append(
                    {"time": sched_time, "topic": slot.get("topic", ""),
                     "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            results.append(res)
            taken_slots.add(sched_time)
            recent_topics += f", {res['topic']}"

        for r in results:
            self.storage.create(self.group.id, **r["fields"])

        # Then render every asset the slots declared.
        #
        # This loop used to read `pdf_required` / `image_required` back off the
        # raw slot dict. Slots in a strategy file only carry `content_type`;
        # those flags are derived by enrich_slot() inside generate_single_content
        # and never made it back out here, so both were always False and the
        # queue produced no images or PDFs at all — 40 of 155 slots silently
        # empty. The flags now travel on the result.
        for r in results:
            if not (r["wants_pdf"] or r["wants_image"]):
                continue
            if status_callback:
                status_callback(f"Generating graphics for: {r['topic'][:20]}...")
            try:
                self.generate_assets(
                    r["id"],
                    r["topic"],
                    r["content"],
                    force_pdf_status="pending" if r["wants_pdf"] else "N/A",
                    force_img_status="pending" if r["wants_image"] else "N/A",
                    category=r["fields"].get("category"),
                )
            except Exception as e:
                log.error("Queue asset generation failed for %s: %s", r["id"], e)
                self.storage.set_state(
                    r["id"], PostState.ASSET_FAILED, error=f"Asset render failed: {e}"
                )

        return results

    def clean_filename(self, title: str) -> str:
        """Cleans a title string to be a safe, SEO-friendly filename"""
        cleaned = re.sub(r'[^a-zA-Z0-9\s-]', '', title).strip()
        cleaned = re.sub(r'[\s]+', ' ', cleaned)
        # Make it Title Case
        cleaned = cleaned.title()
        return f"{cleaned[:50]} - Document"

    #: Placeholders that are chrome, not content. Empty is fine for these —
    #: they are filled from GroupConfig or are genuinely optional decoration.
    _CHROME_KEYS = frozenset({
        # Brand identity, injected from GroupConfig rather than written by an agent.
        "THEME_CLASS", "BRAND_NAME", "BRAND_SUB", "LOGO", "WEBSITE", "CTA",
        "FOOTER", "PAGE", "SOURCE", "AUTHOR",
        # Optional decoration.
        "PERSON_IMAGE", "COMPANY_LOGO", "ILLUSTRATION", "ICON", "LAPTOP", "BOOK",
        # Built by the renderer or genuinely optional.
        "ITEMS_HTML", "PAGES_HTML", "CHECKLIST", "TIP", "SUBTITLE", "SUBTEXT",
    })

    def _content_keys(self, required: list[str]) -> list[str]:
        """The subset of a template's placeholders that must carry real text."""
        return [key for key in required if key not in self._CHROME_KEYS]

    def _get_template_placeholders(self, template_name: str) -> list[str]:
        if template_name in _TEMPLATE_PLACEHOLDER_CACHE:
            return _TEMPLATE_PLACEHOLDER_CACHE[template_name]
        template_path = os.path.join(self.config.PROJECT_ROOT, "design_templates", template_name)
        if not os.path.exists(template_path):
            return []
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Strip HTML comments to ignore commented-out template library card placeholders
            content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)
            placeholders = re.findall(r'\{\{([A-Z0-9_]+)\}\}', content)
            seen = set()
            res = [x for x in placeholders if not (x in seen or seen.add(x))]
            _TEMPLATE_PLACEHOLDER_CACHE[template_name] = res
            return res
        except Exception as e:
            log.error("Could not read placeholders from %s: %s", template_name, e)
            return []

    def generate_assets(self, post_id: str, title: str, content: str, force_pdf_status: str = None, force_img_status: str = None, category: str = None) -> dict:
        """Generates PDF or PNG using HTML templates via the Asset Planner and Mapper agents."""
        post_data = None
        if not force_pdf_status or not force_img_status or not category:
            post_data = self.storage.get_post_by_id(post_id) or None
            
        pdf_status = force_pdf_status if force_pdf_status else (post_data.get("PDF Path", "N/A") if post_data else "N/A")
        img_status = force_img_status if force_img_status else (post_data.get("Image Path", "N/A") if post_data else "N/A")
        raw_cat = category if category else (post_data.get("Content Type", "") if post_data else "")
        
        pdf_path = None
        img_path = None
        render_error = ""
        template_name = export_type = caption_strategy = ""
        render_start = time.time()

        # Only run if an asset was requested
        if pdf_status.lower() == "pending" or img_status.lower() == "pending":
            
            # 1. Asset Planner selects template and format
            enabled_templates = []
            registry_path = os.path.join(self.config.PROJECT_ROOT, "design_templates", "registry.json")
            if os.path.exists(registry_path):
                try:
                    with open(registry_path, "r", encoding="utf-8") as f:
                        all_tmpl = json.load(f)
                    enabled_templates = [t for t in all_tmpl if t.get("enabled", True)]
                except Exception as e:
                    log.error("Could not load design_templates/registry.json: %s", e)
            
            if not enabled_templates:
                enabled_templates = [{
                    "id": "statement",
                    "name": "Statement",
                    "file": "archetypes/statement.html",
                    "theme": "dark",
                    "version": "1.0",
                    "enabled": True,
                    "priority": 1,
                    "supported_categories": ["motivation"],
                    "supported_asset_types": ["PNG", "PDF"],
                    "supports_pdf": True,
                    "supports_png": True,
                    "max_points": 0,
                    "max_checklist_items": 0,
                    "max_paragraphs": 1
                }]

            # Normalize post category for matching
            cat_norm = raw_cat.strip().lower().replace(" ", "_").replace("-", "_")
            category_mapping = {
                "resume_tip": "resume_tips",
                "hiring": "company_hiring",
                "job_hiring": "company_hiring",
                "company_spotlight": "company_hiring",
                "announcements": "announcement",
                "checklists": "checklist",
                "roadmaps": "roadmap",
                "cheat_sheets": "cheat_sheet",
                "pdf": "cheat_sheet",
                "image": "motivation",
                "message": "motivation",
                "link": "cheat_sheet",
                "poll": "motivation"
            }
            cat_match = category_mapping.get(cat_norm, cat_norm)
            
            # Map standard queue slots to compatible template categories if not mapped above
            if cat_match == "aptitude_mcq":
                cat_match = "motivation"
            elif cat_match == "hr_interview":
                cat_match = "resume_tips"

            # Analyze content details for compatibility checks
            lines = [l.strip() for l in content.split('\n') if l.strip()]
            num_paragraphs = len(lines)
            num_list_items = 0
            for line in content.split('\n'):
                line_s = line.strip()
                if line_s.startswith(('-', '*', '•', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', '10.')) or (line_s and line_s[0] in ['✅', '❌', '⚪', '🟢', '🟡', '⚫', '🔹', '📌', '▪']):
                    num_list_items += 1

            # 1. Ask the Asset Planner Agent to choose the optimal template based on the text context
            templates_description = ""
            for t in enabled_templates:
                # No theme here: an archetype has no theme of its own any more.
                # The surface comes from this group's config as a token swap, so
                # naming one would describe a property the template does not
                # have — and reading it raised KeyError on every image post.
                templates_description += f"- {t['file']} ({t['name']})\n"
                
            planner_context = render_prompt(
                "tasks/asset_planner_context",
                title=title,
                content=content,
                templates_description=templates_description,
            )
            
            asset_plan = {}
            try:
                asset_plan = self._call_agent_json(self.agents["asset_planner"], planner_context)
                if isinstance(asset_plan, list) and len(asset_plan) > 0:
                    asset_plan = asset_plan[0]
            except Exception as e:
                log.warning("Asset Planner failed; falling back to registry matching: %s", e)
                
            planned_template_file = asset_plan.get("template") if isinstance(asset_plan, dict) else None
            selected_template = None
            
            # Try to match the template selected by the Design Director Agent
            if planned_template_file:
                planned_template_file_clean = planned_template_file.strip().lower()
                selected_template = next(
                    (t for t in enabled_templates if t["file"].lower() == planned_template_file_clean or t["id"].lower() == planned_template_file_clean),
                    None
                )
                if selected_template:
                    log.info("Template chosen by the Asset Planner: %s", selected_template["id"])
            
            # Fallback to category-based and constraint-based matching using registry fields (layout_mode, min_items, max_items)
            if not selected_template:
                log.info("Asset Planner named no usable template; matching on registry constraints.")
                # Read saved placeholder state if available to count items
                placeholders_file = os.path.join(self.config.PROJECT_ROOT, "generated", "placeholders", f"{post_id}.json")
                num_items_count = num_list_items
                content_layout_mode = "list" if num_list_items > 0 else "single"
                if os.path.exists(placeholders_file):
                    try:
                        with open(placeholders_file, "r", encoding="utf-8") as f:
                            p_saved = json.load(f)
                        if isinstance(p_saved, dict):
                            items_arr = p_saved.get("items", [])
                            if isinstance(items_arr, list) and len(items_arr) > 0:
                                num_items_count = len(items_arr)
                            content_layout_mode = p_saved.get("layout_mode", "list" if num_items_count > 0 else "single")
                    except Exception:
                        pass

                matching_templates = [
                    t for t in enabled_templates
                    if cat_match in t.get("supported_categories", [])
                    or t.get("layout_mode") == content_layout_mode
                ]
                matching_templates.sort(key=lambda x: x.get("priority", 99))

                for tmpl in matching_templates:
                    min_i = tmpl.get("min_items", 0)
                    max_i = tmpl.get("max_items", 99)
                    tmpl_mode = tmpl.get("layout_mode", "list")
                    
                    if tmpl_mode != content_layout_mode and cat_match not in tmpl.get("supported_categories", []):
                        continue
                    if num_items_count < min_i or num_items_count > max_i:
                        continue
                    
                    selected_template = tmpl
                    break

                if not selected_template and matching_templates:
                    selected_template = matching_templates[0]

                if not selected_template and enabled_templates:
                    selected_template = enabled_templates[0]

            if not selected_template:
                raise Exception(
                    f"Template selection failed for category '{raw_cat}' (normalized: '{cat_match}'). "
                    f"Content stats: {num_paragraphs} paragraphs, {num_list_items} list items. "
                    f"No compatible templates found in registry."
                )

            template_name = selected_template["file"]

            export_type = asset_plan.get("export_type", "PNG").upper() if isinstance(asset_plan, dict) else "PNG"
            caption_strategy = asset_plan.get("caption_strategy", "Image + Caption") if isinstance(asset_plan, dict) else "Image + Caption"
            
            # Explicitly force export_type based on what is actually pending
            if pdf_status.lower() == "pending":
                export_type = "PDF"
            elif img_status.lower() == "pending":
                export_type = "PNG"

            # Enforce template's specific format supports or fallback to sheet needs
            if export_type == "PDF" and not selected_template.get("supports_pdf", True):
                export_type = "PNG"
            elif export_type == "PNG" and not selected_template.get("supports_png", True):
                export_type = "PDF"
                
            render_start = time.time()
            
            # 2. Asset Mapper maps content to placeholders
            required_placeholders = self._get_template_placeholders(template_name)
            mapper_context = render_prompt(
                "tasks/asset_mapper_context",
                title=title,
                content=content,
                template_name=template_name,
                required_placeholders=", ".join(required_placeholders),
            )
            try:
                placeholders = self._call_agent_json(self.agents["asset_mapper"], mapper_context)
                if isinstance(placeholders, list) and len(placeholders) > 0:
                    placeholders = placeholders[0]
            except Exception as e:
                log.error("Asset Mapper failed for %s: %s", post_id[:8], e)
                placeholders = {}
                
            # Brand chrome — name, sub-brand, logo, website, CTA and the theme
            # class — all resolved from this tenant's config in one place. It
            # used to be hardcoded in each template's markup, which is why
            # every group's assets came out branded as the first one.
            brand = self.renderer.brand_placeholders(self.group)
            for key, value in brand.items():
                placeholders.setdefault(key, value)

            # Fill anything the template still asks for so no raw {{KEY}} ships.
            for req in required_placeholders:
                if req not in placeholders:
                    if req == "PAGE":
                        placeholders[req] = "1"
                    elif req in ("PERSON_IMAGE", "COMPANY_LOGO"):
                        # Fallback to the brand logo so <img src=""> is never
                        # rendered — both keys resolve to an existing file in
                        # design_templates/ that Playwright can actually load.
                        placeholders[req] = (
                            "logo_dark.png"
                            if "light" in template_name.lower()
                            else "logo_light.png"
                        )
                    else:
                        placeholders[req] = ""

            # A key filled with "" renders as a blank card, and validate_asset
            # cannot catch it: that only looks for *unsubstituted* {{KEY}}
            # markers, not keys substituted with nothing. The motivation
            # templates need QUOTE/SUBTEXT/TAGLINE, which the mapper schema
            # does not name, so they were routinely rendering empty.
            #
            # Refuse the render instead, so the post shows as asset_failed and
            # somebody can see why.
            missing = [
                key for key in self._content_keys(required_placeholders)
                if not str(placeholders.get(key, "")).strip()
            ]
            if missing:
                raise ValueError(
                    f"Asset Mapper returned nothing for {missing} required by "
                    f"{template_name}. Rendering would produce a blank card."
                )


            # Save placeholders to local JSON file for future editing
            placeholders_dir = os.path.join(self.config.PROJECT_ROOT, "generated", "placeholders")
            os.makedirs(placeholders_dir, exist_ok=True)
            placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
            try:
                with open(placeholders_file, "w", encoding="utf-8") as f:
                    json.dump(placeholders, f, indent=4)
            except Exception as e:
                log.warning("Could not cache placeholders for %s: %s", post_id[:8], e)
                
            # 3. Render
            try:
                rendered_path = self.renderer.render(
                    template_name, placeholders, export_type, group=self.group)
                if export_type == "PDF":
                    pdf_path = rendered_path
                    # Clear pending status for image if planner decided PDF
                    if img_status.lower() == "pending": img_status = "N/A"
                else:
                    img_path = rendered_path
                    # Clear pending status for pdf if planner decided PNG
                    if pdf_status.lower() == "pending": pdf_status = "N/A"
            except Exception as e:
                # A render failure has to be a state, not a string in a path
                # column. It used to write "Failed" into PDF Path / Image Path,
                # which is truthy — so `assets_ready` said yes, the reconciler
                # picked the post up, and the operator learned about it as a
                # Telegram error minutes later instead of seeing it here.
                render_error = str(e).encode('ascii', 'ignore').decode('ascii')
                log.error("Rendering failed for post %s: %s", post_id[:8], render_error)

        updates = {}
        missing = []
        if pdf_path:
            updates["PDF Path"] = pdf_path
        elif pdf_status.lower() == "pending":
            missing.append("PDF")

        if img_path:
            updates["Image Path"] = img_path
        elif img_status.lower() == "pending":
            missing.append("image")

        if pdf_path or img_path:
            updates["Template Used"] = template_name
            updates["Asset Type"] = export_type
            updates["Rendering Time"] = f"{(time.time() - render_start):.2f}s"
            updates["Caption Strategy"] = caption_strategy

        if updates:
            self.storage.update_post(post_id, updates)

        if missing:
            # D1: a slot that declared an asset either yields a file or the post
            # is visibly asset_failed. There is no third outcome.
            reason = (
                f"{' and '.join(missing)} could not be rendered"
                + (f": {render_error}" if render_error else ".")
            )
            self.storage.set_state(post_id, PostState.ASSET_FAILED, error=reason)

        return {"pdf": pdf_path, "image": img_path}
        
    def _chat_id_for(self, group_id: str) -> str:
        """The Telegram chat that owns a post, looked up by its group id."""
        if not group_id:
            return ""
        if group_id == self.group.id:
            return self.group.telegram_chat_id or ""
        try:
            from engine.group_config import load_group_config
            return load_group_config(group_id).telegram_chat_id or ""
        except Exception as exc:
            log.error("Could not load config for group %r: %s", group_id, exc)
            return ""

    def _resolve_asset_for_telegram(self, path: str) -> tuple:
        """The local file to upload, and whether the caller must delete it.

        Assets used to be mirrored to Google Drive and stored as `drive:<id>`,
        so this had to download one back to a temp file before every send. The
        canonical asset is now the asset_document on the post row; the rendered
        file is a cache that is re-rendered if it is gone. The second element
        stays so callers keep their cleanup, and so re-introducing a remote
        store later does not change this signature.
        """
        if path and os.path.exists(path):
            return path, False
        return '', False

    def publish_post(self, post_id: str, content: str, pdf_path: str = None, img_path: str = None) -> bool:
        """Publish a post to its own group's Telegram chat.

        The chat id is resolved from the post row, not from this workflow's
        config. A scheduler thread has no Flask session, so the old code fell
        back to the default tenant and delivered every scheduled post to the
        same community regardless of who wrote it.
        """
        post_data = self.storage.get_post_by_id(post_id)
        if not post_data:
            log.error("publish_post: %s not found", post_id)
            return False

        owner_id = post_data.get("Group") or ""
        chat_id = self._chat_id_for(owner_id)
        if not chat_id:
            # Refusing beats guessing: a post with no resolvable chat must not
            # be delivered to whichever tenant happens to be the default.
            message = (
                f"No Telegram chat configured for group '{owner_id}'. "
                "Set its telegram_chat_id_env var in .env."
            )
            log.error("publish_post: %s", message)
            self.storage.set_state(post_id, PostState.PUBLISH_FAILED, error=message)
            return False

        post_type = post_data.get("Content Type", "")

        try:
            msg_id = self._send_to_telegram(post_id, post_type, content, chat_id,
                                            pdf_path, img_path)
        except TelegramError as exc:
            log.error("Publishing %s failed: %s", post_id[:8], exc)
            self.storage.set_state(post_id, PostState.PUBLISH_FAILED, error=str(exc))
            return False

        if msg_id:
            self.storage.update_post(post_id, {
                "Publish Status": "published", "Telegram Message ID": str(msg_id)})
            return True
        self.storage.set_state(post_id, PostState.PUBLISH_FAILED,
                               error="Telegram accepted nothing to send.")
        return False

    def _send_to_telegram(self, post_id, post_type, content, chat_id,
                          pdf_path, img_path):
        """Route the post to the right Telegram method. Raises TelegramError."""
        msg_id = None
        if post_type.lower() == "poll":
            import re
            import json
            
            question = None
            options = []
            
            # Strategy 0: try parsing as JSON (new format)
            try:
                clean_json = content.strip()
                if clean_json.startswith("```"):
                    clean_json = re.sub(r'^```(?:json)?\n(.*?)\n```$', r'\1', clean_json, flags=re.DOTALL).strip()
                parsed = json.loads(clean_json)
                if isinstance(parsed, dict):
                    question = parsed.get("question", "").strip().replace('*', '')[:299]
                    options = [str(o).strip().replace('*', '')[:99] for o in parsed.get("options", []) if str(o).strip()]
                    options = [o for o in options if o and len(o) > 1]
            except Exception:
                pass
            
            # Strategy 1: look for explicit "Question:" and "Options:" labels (fallback)
            if not question or len(options) < 2:
                q_match = re.search(r'(?i)question:\s*(.*?)(?=\n|$)', content)
                opt_match = re.search(r'(?i)options:\s*(.*?)(?=\n\n|\Z)', content, re.DOTALL)
                
                if q_match and opt_match:
                    question = q_match.group(1).strip().replace('*', '')[:299]
                    raw_options = opt_match.group(1).strip()
                    if ',' in raw_options and '\n' not in raw_options:
                        options = [o.strip().replace('*', '')[:99] for o in raw_options.split(',') if o.strip()]
                    else:
                        options = [o.strip().replace('*', '')[:99] for o in raw_options.split('\n') if o.strip()]
                        options = [re.sub(r'^[\d\-\.\*✅📌]+\s*', '', o).strip()[:99] for o in options]
                    options = [o for o in options if o and len(o) > 1]
            
            # Strategy 2: first non-empty line is question, remaining non-empty lines are options
            if not question or len(options) < 2:
                lines = [l.strip() for l in content.split('\n') if l.strip()]
                # Find the first line that looks like a question (ends with ? or is short)
                for i, line in enumerate(lines):
                    clean_line = re.sub(r'^[\d\-\.\*✅📌🔹]+\s*', '', line).strip()
                    if clean_line.endswith('?') or (len(clean_line) < 150 and i == 0):
                        question = clean_line[:299]
                        # Remaining lines become options, strip bullets/numbers
                        raw_opts = lines[i+1:]
                        options = [re.sub(r'^[\d\-\.\*✅📌🔹]+\s*', '', o).strip()[:99] for o in raw_opts]
                        options = [o for o in options if o and len(o) > 1 and not o.lower().startswith(('let', 'your', 'drop', 'and ', 'join', 'remem', 'share', '#', 'vote'))]
                        if len(options) >= 2:
                            break
            
            if question and len(options) >= 2:
                log.debug("Poll question: %r", question)
                log.debug("Poll options: %s", options[:10])
                
                # Send context message first
                self.telegram.publish_text("👇 Vote below!", chat_id)
                
                try:
                    msg_id = self.telegram.publish_poll(
                        question=question,
                        options=options[:10],
                        chat_id=chat_id,
                        is_anonymous=True,
                        type="regular"
                    )
                except TelegramError as exc:
                    # A poll Telegram will not accept is still worth sending as
                    # text; the fallback below does that.
                    log.warning("Poll rejected for %s (%s) — sending as text.",
                                post_id[:8], exc)
            else:
                log.warning("Could not parse a poll out of the draft (question=%r, %d options).", question, len(options))
            
            # Fallback if poll parsing failed or API failed
            if not msg_id:
                log.info("Sending the poll as a plain text message instead.")
                msg_id = self.telegram.publish_text(content, chat_id)
        else:
            if post_type.lower() == "pdf":
                resolved_pdf, pdf_is_temp = self._resolve_asset_for_telegram(pdf_path)
                if resolved_pdf:
                    try:
                        msg_id = self.telegram.publish_document(resolved_pdf, content[:1000], chat_id)
                    finally:
                        if pdf_is_temp:
                            try: os.unlink(resolved_pdf)
                            except Exception: pass
                else:
                    raise TelegramError(
                        f"This is a PDF post but the file is missing: {pdf_path!r}")
            elif post_type.lower() == "image":
                resolved_img, img_is_temp = self._resolve_asset_for_telegram(img_path)
                if resolved_img:
                    try:
                        msg_id = self.telegram.publish_photo(resolved_img, content[:1000], chat_id)
                    finally:
                        if img_is_temp:
                            try: os.unlink(resolved_img)
                            except Exception: pass
                else:
                    raise TelegramError(
                        f"This is an image post but the file is missing: {img_path!r}")
            else:
                msg_id = self.telegram.publish_text(content, chat_id)
            
        return msg_id
