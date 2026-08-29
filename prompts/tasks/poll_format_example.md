OUTPUT FORMAT IS CRITICAL. You MUST generate a structured JSON object representing a Telegram Poll.
Output ONLY a valid JSON object with the following keys:
- "question": The poll question (string, maximum 300 characters, no markdown).
- "options": An array of 2 to 4 short answer options (strings, maximum 100 characters each).

Example:
{{
  "question": "What's the #1 thing making you anxious about placements right now?",
  "options": ["Aptitude Round", "DSA Round", "HR Interview", "Resume Prep"]
}}

Do not include any prose, commentary, or markdown formatting outside of the JSON block.
