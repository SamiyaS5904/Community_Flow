You are writing ONE section of a guide for {group_name}.
Audience: {audience}

Guide: {guide_title}
This section: {heading}
What it should cover: {intent}

Write the body of this section and nothing else.

- THREE paragraphs. 45 to 70 words each. Separate them with a blank line.
- Paragraph 1: what to do, concretely.
- Paragraph 2: why it works, or what goes wrong without it.
- Paragraph 3: how the reader can tell they have done it properly.
- Plain sentences. No markdown, no headings, no bullet characters, no emoji.
- Never invent statistics, dates, company names, salary figures or URLs.
- Do not repeat the heading back.

Other sections in this guide cover: {siblings}
Do not duplicate their ground.

Return ONLY a JSON object:
{{
  "body": "Paragraph one.\n\nParagraph two.\n\nParagraph three.",
  "example": "One specific situation the reader would recognise, 30-50 words."
}}
