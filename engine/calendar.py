"""
engine/calendar.py
===================
Editorial Calendar configuration for the Parent Group (Placement Prep).
"""

DEFAULT_SCHEDULE = [
    {"time": "08:00", "category": "motivation", "name": "Morning Motivation"},
    {"time": "10:30", "category": "resume_tip", "name": "Resume Tips / Career Advice"},
    {"time": "13:00", "category": "aptitude_mcq", "name": "Technical MCQ / Aptitude"},
    {"time": "16:00", "category": "hr_interview", "name": "HR Interview / GD Topic"},
    {"time": "20:00", "category": "company_spotlight", "name": "Daily Challenge / Company Insight / Reflection"}
]

def get_schedule_for_date(date_str: str) -> list[dict]:
    """Returns the default editorial schedule for a specific date."""
    # In V2 this could vary by day of week. For V1, it's consistent.
    schedule = []
    for item in DEFAULT_SCHEDULE:
        schedule.append({
            "datetime": f"{date_str}T{item['time']}",
            "time": item["time"],
            "category": item["category"],
            "name": item["name"]
        })
    return schedule
