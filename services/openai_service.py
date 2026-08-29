import openai
import logging
from collections import OrderedDict
import time
import json
import re
from engine.prompt_builder import PromptBuilder, ResponseCleaner

log = logging.getLogger(__name__)

def _sanitize_text(text: str) -> str:
    """
    Remove surrogate characters and other non-UTF-8 safe characters
    that would cause OpenAI API encoding failures.
    """
    if not isinstance(text, str):
        text = str(text)
    # Encode to utf-8 ignoring surrogates, decode back to clean string
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

#: Output budget per agent. A single global ceiling was squeezing every agent
#: to the size of a Telegram post: the PDF Writer silently dropped the worked
#: example from every section of a guide to fit inside it, and a cycle planner
#: assigning seventy-odd slots would have been cut off mid-array.
MAX_TOKENS_BY_AGENT = {
    "pdf_writer":    3200,   # a multi-page guide: cover, intro, 4-6 sections, close
    "planner":       2600,   # assigns every slot in a 15-day cycle
    "asset_mapper":  1400,   # 3-4 items, each with a description and an example
    "writer":         900,   # one Telegram post
    "qa":             900,   # rewrites that post
    "research":       900,
    "asset_planner":  300,   # picks a template and a format
}
DEFAULT_MAX_TOKENS = 900

#: Model per agent. The steps differ enough that one model for all of them is
#: the wrong trade either way: a JSON template pick does not need a strong
#: model, and prose does not want a cheap one. Override any of these from .env,
#: e.g. OPENAI_MODEL_WRITER=gpt-4.1.
#:
#: Measured on this project: asked in one call for four guide sections of three
#: paragraphs each, gpt-4o-mini returned ~30 words per section every time.
#: Splitting the work into one call per section fixed that without a bigger
#: model — so the cheap model is fine where the task is small and well shaped.
MODEL_BY_AGENT = {
    "writer":        "OPENAI_MODEL_WRITER",
    "qa":            "OPENAI_MODEL_QA",
    "pdf_writer":    "OPENAI_MODEL_PDF",
    "research":      "OPENAI_MODEL_RESEARCH",
    "planner":       "OPENAI_MODEL_PLANNER",
    "asset_mapper":  "OPENAI_MODEL_MAPPER",
    "asset_planner": "OPENAI_MODEL_ASSET_PLANNER",
}

TEMPERATURE_BY_AGENT = {
    "pdf_writer":    0.4,
    "planner":       0.3,
    "asset_mapper":  0.4,
    "asset_planner": 0.2,
}
DEFAULT_TEMPERATURE = 0.75


class OpenAIService:
    MAX_RETRIES = 3
    RETRY_DELAY = 8  # Reduced from 10 for faster recovery

    #: Response cache, bounded. It used to be an unbounded class dict that
    #: nothing evicted and nothing could bypass, so it grew for the life of the
    #: process and "Regenerate" handed back the identical post every time.
    _cache: "OrderedDict[str, str]" = OrderedDict()
    CACHE_CAPACITY = 256

    # Lightweight telemetry for API usage dashboard
    _stats: dict = {
        "total_calls": 0,
        "total_tokens": 0,
        "total_latency": 0.0,
        "failures": 0,
        "cache_hits": 0,
        "total_retries": 0,
    }

    #: Seconds before a single request is abandoned. The SDK default is 600,
    #: which is longer than Gunicorn's own timeout — a hung call would pin a
    #: background thread for ten minutes and take the worker with it.
    REQUEST_TIMEOUT = 90.0

    def __init__(self, api_key: str, default_model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.default_model = default_model
        self.client = openai.OpenAI(
            api_key=self.api_key,
            timeout=self.REQUEST_TIMEOUT,
            max_retries=0,      # this class already retries, with feedback
        )

    @classmethod
    def _remember(cls, key: str, value: str) -> None:
        cls._cache[key] = value
        cls._cache.move_to_end(key)
        while len(cls._cache) > cls.CACHE_CAPACITY:
            cls._cache.popitem(last=False)

    @classmethod
    def get_stats(cls) -> dict:
        """Return a copy of the current API usage stats."""
        calls = cls._stats["total_calls"] or 1
        # average cost of gpt-4o-mini is ~$0.38 per 1M tokens (input/output blend)
        estimated_cost = round((cls._stats["total_tokens"] / 1000000.0) * 0.38, 5)
        return {
            **cls._stats,
            "avg_latency": round(cls._stats["total_latency"] / calls, 2),
            "estimated_cost": estimated_cost,
            "success_rate": round(
                (calls - cls._stats["failures"]) / calls * 100, 1
            ),
        }

    def generate_content(
        self,
        prompt: str,
        model: str = None,
        agent: dict = None,
        is_json: bool = False,
        group=None,  # GroupConfig — used for tenant-aware system prompt & word-count validation
        use_cache: bool = True,
    ) -> str:
        """Generates content using OpenAI API with caching, validation, and prompt building.

        Args:
            prompt: The user-facing context/input for this call.
            model: Override the default model.
            agent: Agent definition dict (role, goal, instructions, expected_output, agent_type).
            is_json: If True, enforces JSON-only output and uses json_object response format.
            group: GroupConfig for the active tenant. Drives system prompt tone and word-count limit.
        """
        if not self.api_key or self.api_key == "YOUR_OPENAI_API_KEY":
            return "Error: OpenAI API Key is missing or invalid."

        # An explicit argument wins; then a per-agent override from .env;
        # then the service default.
        import os as _os
        _agent_type_early = agent.get("agent_type", "writer") if agent else "writer"
        _env_var = MODEL_BY_AGENT.get(_agent_type_early)
        use_model = (
            model
            or (_os.getenv(_env_var, "").strip() if _env_var else "")
            or _os.getenv("OPENAI_MODEL", "").strip()
            or self.default_model
        )

        # Sanitize incoming prompt to remove surrogate/bad characters
        prompt = _sanitize_text(prompt)

        # Determine agent type for prompt-tier selection
        agent_type = agent.get("agent_type", "writer") if agent else "writer"

        # Build the structured prompt (tenant-aware)
        system_prompt = PromptBuilder.build_system_prompt(group=group, agent_type=agent_type, is_json=is_json)
        user_prompt = PromptBuilder.build_user_prompt(agent, context=prompt)

        # Sanitize built prompts too
        system_prompt = _sanitize_text(system_prompt)
        user_prompt = _sanitize_text(user_prompt)

        cache_key = f"{use_model}_{hash(system_prompt)}_{hash(user_prompt)}"
        if use_cache and cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            agent_name = agent.get("role", "None") if agent else "None"
            log.info(f"LLM Cache Hit | Agent: {agent_name}")
            OpenAIService._stats["cache_hits"] += 1
            return self._cache[cache_key]

        # Resolve word-count ceiling from the active group (tenant-aware)
        word_count_max = group.word_count_max if group else 300

        last_error = None
        feedback = ""
        best_attempt = None      # kept so retries improve rather than gamble

        for attempt in range(1, self.MAX_RETRIES + 1):
            if attempt > 1:
                OpenAIService._stats["total_retries"] += 1
            try:
                start_time = time.time()

                final_user_prompt = user_prompt
                if feedback:
                    final_user_prompt += (
                        f"\n\nFEEDBACK ON PREVIOUS ATTEMPT (DO NOT REPEAT MISTAKE):\n{feedback}"
                    )

                kwargs = {
                    "model": use_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": final_user_prompt},
                    ],
                    # Creative temperature helps a Telegram post read like a
                    # person wrote it. For a structured document or a JSON
                    # schedule it only adds variance — the same prompt returned
                    # a full guide once and 25-word stubs the next time.
                    "temperature": TEMPERATURE_BY_AGENT.get(agent_type, DEFAULT_TEMPERATURE),
                    "max_tokens": MAX_TOKENS_BY_AGENT.get(agent_type, DEFAULT_MAX_TOKENS),
                }

                if is_json:
                    kwargs["response_format"] = {"type": "json_object"}

                response = self.client.chat.completions.create(**kwargs)
                raw_text = response.choices[0].message.content.strip()

                latency = round(time.time() - start_time, 2)
                tokens = response.usage.total_tokens if response.usage else 0

                # Update telemetry
                OpenAIService._stats["total_calls"] += 1
                OpenAIService._stats["total_tokens"] += tokens
                OpenAIService._stats["total_latency"] += latency

                # Clean and Validate
                cleaned_text = ResponseCleaner.clean(raw_text)
                is_valid, validation_msg = ResponseCleaner.validate(
                    cleaned_text, is_json=is_json, word_count_max=word_count_max,
                    agent_type=agent_type,
                )

                agent_name = agent.get("role", "None") if agent else "None"

                if is_valid:
                    log.info(
                        f"LLM OK | Agent: {agent_name} | {latency}s | {tokens} tokens"
                    )
                    self._remember(cache_key, cleaned_text)
                    return cleaned_text
                else:
                    # Keep the fullest attempt: retries vary, and the last one
                    # is not necessarily the best.
                    if best_attempt is None or len(cleaned_text) > len(best_attempt):
                        best_attempt = cleaned_text
                    feedback = validation_msg
                    log.warning(
                        f"LLM Validation Failed | Agent: {agent_name} | {feedback}. Retrying ({attempt}/{self.MAX_RETRIES})..."
                    )
                    time.sleep(self.RETRY_DELAY / 2)
                    continue

            except openai.RateLimitError as e:
                last_error = f"Rate limit: {str(e)}"
                log.warning(f"Rate limit hit on attempt {attempt}. Waiting {self.RETRY_DELAY}s...")
                time.sleep(self.RETRY_DELAY)
            except UnicodeEncodeError as e:
                # Catch encoding errors explicitly so we can sanitize and retry
                last_error = f"Unicode encode error: {str(e)}"
                log.error(f"Unicode error on attempt {attempt} — sanitizing and retrying...")
                user_prompt = _sanitize_text(user_prompt)
                system_prompt = _sanitize_text(system_prompt)
                time.sleep(1)
            except Exception as e:
                last_error = f"OpenAI API error: {str(e)}"
                log.error(f"Attempt {attempt} failed: {last_error}")
                time.sleep(self.RETRY_DELAY / 2)

        # A response that came back but fell short of the quality bar is still
        # usable; only a call that never succeeded is an error. Failing the
        # whole post because one section was two words short helps nobody.
        if best_attempt and not last_error:
            log.warning(
                "Agent %s never met the quality bar after %d attempts; using the "
                "fullest response. Last issue: %s",
                agent_name, self.MAX_RETRIES, feedback,
            )
            self._remember(cache_key, best_attempt)
            return best_attempt

        OpenAIService._stats["failures"] += 1
        error_msg = f"Failed after {self.MAX_RETRIES} attempts. Last error: {last_error or feedback}"
        log.error(error_msg)
        return f"[ERROR] {error_msg}"
