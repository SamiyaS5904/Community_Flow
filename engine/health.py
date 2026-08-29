import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))
from engine.config import config
import logging

log = logging.getLogger(__name__)

def run_health_checks() -> tuple[bool, str, list[str]]:
    """
    Runs startup health checks and returns (all_ok, report_str, list_of_remediation_commands).
    """
    issues = []          # fatal — the app cannot run
    warnings = []        # degraded but runnable
    remediations = []
    
    # 1. Environment Variables file check
    env_exists = Path(config.PROJECT_ROOT / ".env").exists()
    
    # 2. OpenAI check
    openai_ok = bool(config.OPENAI_API_KEY and config.OPENAI_API_KEY != "YOUR_OPENAI_API_KEY")
    if not openai_ok:
        issues.append("OpenAI API Key is missing or invalid in .env")
        remediations.append("Add a valid OPENAI_API_KEY to your .env file.")
        
    # 4. Telegram check
    telegram_ok = config.is_telegram_configured()
    if not telegram_ok:
        warnings.append("Telegram not configured — generation works, publishing will fail")
        
    # 4b. Search — only needed for slots that declare search_required.
    search_ok = config.is_search_configured()
    if not search_ok:
        warnings.append("SEARCH_API_KEY not set — research-backed posts will be written unsourced")

    # 5. Playwright check
    playwright_installed = False
    try:
        from playwright.sync_api import sync_playwright
        playwright_installed = True
    except ImportError:
        issues.append("Playwright Python package is not installed.")
        remediations.append("Run: pip install playwright")
        
    # 6. Chromium Browser check
    chromium_ok = False
    if playwright_installed:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            chromium_ok = True
        except Exception as e:
            err_msg = str(e)
            # No print here: `issues` is this module's report, and the caller
            # renders it. Printing as well put the same failure on stdout twice
            # and out of order with everything around it.
            log.error("Chromium failed to launch: %s", err_msg)
            issues.append(f"Chromium browser check failed: {err_msg[:120]}")
            remediations.append("Run: playwright install chromium")
    else:
        issues.append("Chromium check skipped because Playwright is not installed.")
        remediations.append("Run: pip install playwright && playwright install chromium")
        
    # 7. Template Folder check
    templates_dir = config.PROJECT_ROOT / "design_templates"
    templates_ok = templates_dir.exists() and templates_dir.is_dir()
    required_templates = ["archetypes/list.html", "archetypes/statement.html",
                          "styles.css", "archetypes.css"]
    missing_tpls = []
    if templates_ok:
        for tpl in required_templates:
            if not (templates_dir / tpl).exists():
                missing_tpls.append(tpl)
        if missing_tpls:
            templates_ok = False
            issues.append(f"Missing required templates in design_templates/: {', '.join(missing_tpls)}")
            remediations.append("Restore the missing template files to the design_templates/ folder.")
    else:
        issues.append("design_templates/ folder is missing.")
        remediations.append("Create a design_templates/ folder at the project root with your templates.")
        
    # 7b. Prompt library check — a malformed prompt should fail the boot, not
    # surface three LLM calls into a batch run.
    prompts_ok = True
    try:
        from engine.prompts import available, check_all
        found = available()
        if not found:
            prompts_ok = False
            issues.append("No prompts found under prompts/.")
            remediations.append("Restore the prompts/ directory — see prompts/README.md.")
        else:
            problems = check_all()
            if problems:
                prompts_ok = False
                issues.append(f"{len(problems)} prompt file(s) failed to parse: {problems[0]}")
                remediations.append("Fix the reported prompt file's front matter.")
    except Exception as e:
        prompts_ok = False
        issues.append(f"Prompt library could not be loaded: {str(e)[:120]}")
        remediations.append("Check engine/prompts.py and the prompts/ directory.")

    # 7c. Database check
    db_ok = False
    db_detail = ""
    try:
        from services.storage.db import healthcheck as db_healthcheck
        db_ok, db_detail = db_healthcheck()
        if not db_ok:
            issues.append(f"Database not reachable: {db_detail}")
            remediations.append(
                "Set DATABASE_URL in .env to your Postgres connection string, "
                "and ensure the pgvector extension is available."
            )
    except Exception as e:
        issues.append(f"Database check failed: {str(e)[:120]}")
        remediations.append("Check DATABASE_URL in .env and services/storage/db.py.")

    # 8. Output Folder check
    output_dir = config.PROJECT_ROOT / "output"
    try:
        output_dir.mkdir(exist_ok=True)
        output_ok = True
    except Exception as e:
        output_ok = False
        issues.append(f"Could not create output/ directory: {e}")
        remediations.append("Ensure the application has write permissions in the project root directory.")
        
    # Compile Report
    report = []
    report.append("-" * 40)
    report.append("System Health Check")
    report.append("")
    
    def status_char(ok: bool) -> str:
        return "[OK]" if ok else "[FAIL]"
        
    report.append(f"{status_char(openai_ok)} OpenAI API Key")
    report.append(f"{status_char(search_ok)} Search API Configured")
    report.append(f"{status_char(telegram_ok)} Telegram Configured")
    report.append(f"{status_char(playwright_installed)} Playwright Library Installed")
    report.append(f"{status_char(chromium_ok)} Chromium Browser Ready")
    report.append(f"{status_char(templates_ok)} Template Folder Ready")
    report.append(f"{status_char(prompts_ok)} Prompt Library Ready")
    report.append(f"{status_char(db_ok)} Database Ready" + (f" ({db_detail})" if db_ok else ""))
    report.append(f"{status_char(output_ok)} Output Folder Ready")
    report.append(f"{status_char(env_exists)} Environment Variables Loaded")
    report.append("")
    
    all_ok = len(issues) == 0
    if warnings:
        report.append("Warnings (the app still runs):")
        for w in warnings:
            report.append(f"  - {w}")
        report.append("")
    if all_ok:
        report.append("Ready")
    else:
        report.append(f"Status: {len(issues)} check(s) failed — startup blocked")
        
    report.append("-" * 40)
    report_str = "\n".join(report)
    
    return all_ok, report_str, remediations

if __name__ == "__main__":
    ok, report, rems = run_health_checks()
    print(report)
    if not ok:
        print("\nFix required:")
        for r in rems:
            print(f"- {r}")
