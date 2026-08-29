You are choosing what {group_name} should post about next.

Audience: {audience}
Content categories: {categories}
Themes to prioritise: {must_cover}
Never propose topics about: {never_cover}

Below is what the web is currently saying in this space. Read it and propose up
to {limit} post topics this audience would genuinely benefit from.

RULES:
- A topic is a specific, useful angle — not a headline restated.
  Bad:  "IIM Ahmedabad releases 2026 cutoffs"
  Good: "What a percentile cutoff actually tells you about your chances"
- Prefer topics that stay useful for weeks over one-day news.
- Do not invent statistics, dates, company names or quotes.
- source_url is REQUIRED: copy the exact "source:" URL of the finding that
  prompted the topic. Never write null, never invent a URL. If no finding
  supports a topic, do not propose that topic.
- Skip anything the audience would already know.
- Fewer good topics beat filling the quota.
- Every topic must be clearly distinct from the others you propose. Two angles
  on the same idea count as one topic — pick the better one.

Return ONLY a JSON array. No markdown fences, no commentary.

[
  {{
    "title": "The topic, as a post title",
    "angle": "One sentence on what makes it worth reading",
    "category": "one of: {categories}",
    "source_url": "the exact source URL of the finding it came from"
  }}
]

--- CURRENT FINDINGS ---
{findings}
