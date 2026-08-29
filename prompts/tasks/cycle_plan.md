You are planning {cycle_length} days of content for {group_name}.

Audience: {audience}

Below are the slots to fill and the topics available in the pool. Assign topics
to slots. You are choosing the *order and pairing*, not writing anything.

RULES:
- Match content_type where you can: a slot asking for a poll wants a topic that
  works as a poll, an image slot wants something visual.
- Respect each day's theme when one is given.
- Space related topics apart. Two topics on the same subject should not land on
  consecutive days, and ideally not in the same week.
- Rotate categories. Avoid the same category twice in one day.
- Use each topic at most once.
- Leave a slot unassigned rather than forcing a bad fit. Fewer, better pairings
  beat filling every slot.

Return ONLY a JSON array mapping slot numbers to topic ids. No commentary.

[
  {{ "slot": 0, "topic_id": "the id of the topic to use" }},
  {{ "slot": 1, "topic_id": "..." }}
]

--- SLOTS ---
{slots}

--- AVAILABLE TOPICS ---
{topics}
