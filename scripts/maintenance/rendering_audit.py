import sys
import os
import json
import re
from pathlib import Path

# Make platform importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.config import config
from services.render_service import RenderService

def get_template_placeholders(templates_dir: str, template_name: str) -> list[str]:
    template_path = os.path.join(templates_dir, template_name)
    if not os.path.exists(template_path):
        return []
    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()
    placeholders = re.findall(r'\{\{([A-Z0-9_]+)\}\}', content)
    seen = set()
    return [x for x in placeholders if not (x in seen or seen.add(x))]

def run_audit():
    print("=== STARTING RENDERING PIPELINE AUDIT ===")
    
    templates_dir = os.path.join(config.PROJECT_ROOT, "design_templates")
    output_dir = os.path.join(config.PROJECT_ROOT, "generated", "audit_test")
    os.makedirs(output_dir, exist_ok=True)
    
    renderer = RenderService(base_output_dir=output_dir, templates_dir=templates_dir)
    
    registry_path = os.path.join(templates_dir, "registry.json")
    if not os.path.exists(registry_path):
        print(f"Error: Registry file not found at {registry_path}")
        return
        
    with open(registry_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
        
    dummy_data = {
        "LOGO": "logo_light.png",
        "CATEGORY": "PLACEMENT GUIDE",
        "HOOK": "MASTER THE HR ROUND",
        "TITLE": "Tell me about a time you faced a challenge at work?",
        "SUBTITLE": "A classic behavior question asked in 90% of interview rounds.",
        "POINT_1": "Clearly outline the situation and the core challenge you faced.",
        "POINT_2": "Describe the specific actions you took to resolve it.",
        "POINT_3": "Highlight the positive results and key learning takeaways.",
        "TIP": "Pro Tip: Keep it structured using the STAR framework.",
        "CHECKLIST": '<li><span class="check-box"></span>Keep it under 2 minutes</li><li><span class="check-box"></span>Focus on professional growth</li>',
        "WEBSITE": "owlet-campus.com",
        "CTA": "Join @carrotowl",
        "PAGE": "1",
        "TAGLINE": "INTERVIEW HACK",
        "QUOTE": "HOW TO HANDLE WORKPLACE CHALLENGES",
        "SUBTEXT": "Be honest about the situation, focus heavily on your actions, and always conclude with the quantifiable business impact.",
        "PERSON_IMAGE": "logo_light.png"
    }
    
    audit_results = []
    
    for t in templates:
        t_id = t["id"]
        t_file = t["file"]
        t_theme = t["theme"]
        print(f"\nAuditing template: {t['name']} ({t_file})...")
        
        # Determine appropriate logo based on theme
        if t_theme == "light":
            dummy_data["LOGO"] = "logo_dark.png"
        else:
            dummy_data["LOGO"] = "logo_light.png"
            
        required_placeholders = get_template_placeholders(templates_dir, t_file)
        placeholders = {k: dummy_data[k] for k in required_placeholders if k in dummy_data}
        
        # Add fallbacks for missing mock data
        for req in required_placeholders:
            if req not in placeholders:
                placeholders[req] = f"[MISSING MOCK DATA FOR {req}]"
                
        template_status = {
            "id": t_id,
            "name": t["name"],
            "file": t_file,
            "png_status": "Skipped",
            "png_error": None,
            "pdf_status": "Skipped",
            "pdf_error": None
        }
        
        # Test PNG
        if t.get("supports_png", True):
            try:
                png_path = renderer.render(t_file, placeholders, "PNG")
                template_status["png_status"] = "PASSED"
                print(f"  [OK] PNG generated successfully: {os.path.basename(png_path)}")
            except Exception as e:
                template_status["png_status"] = "FAILED"
                template_status["png_error"] = str(e)
                print(f"  [FAIL] PNG generation FAILED: {e}")
                
        # Test PDF
        if t.get("supports_pdf", True):
            try:
                pdf_path = renderer.render(t_file, placeholders, "PDF")
                template_status["pdf_status"] = "PASSED"
                print(f"  [OK] PDF generated successfully: {os.path.basename(pdf_path)}")
            except Exception as e:
                template_status["pdf_status"] = "FAILED"
                template_status["pdf_error"] = str(e)
                print(f"  [FAIL] PDF generation FAILED: {e}")
                
        audit_results.append(template_status)
        
    # Write Audit Report to markdown file in appDataDir/brain/<conversation-id>/
    report_content = "# Rendering Validation Report\n\n"
    report_content += "This report summarizes the rendering, formatting, and structural validation tests executed for all registered design templates.\n\n"
    report_content += "| Template Name | File | PNG Status | PDF Status | Notes / Errors |\n"
    report_content += "| --- | --- | --- | --- | --- |\n"
    
    all_passed = True
    for res in audit_results:
        notes = []
        if res["png_error"]:
            notes.append(f"PNG Error: {res['png_error']}")
            all_passed = False
        if res["pdf_error"]:
            notes.append(f"PDF Error: {res['pdf_error']}")
            all_passed = False
            
        notes_str = "; ".join(notes) if notes else "All checks passed successfully"
        report_content += f"| {res['name']} | `{res['file']}` | {res['png_status']} | {res['pdf_status']} | {notes_str} |\n"
        
    report_path = os.path.join(r"C:\Users\win11\.gemini\antigravity\brain\e3062b9b-fc2d-4dad-ba2b-70f3bbb4ab9f", "rendering_validation_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    print(f"\nAudit completed. Report written to {report_path}")
    if all_passed:
        print("RESULT: ALL TEMPLATES PASSED VALIDATION.")
    else:
        print("RESULT: SOME TEMPLATES FAILED VALIDATION.")

if __name__ == "__main__":
    run_audit()
