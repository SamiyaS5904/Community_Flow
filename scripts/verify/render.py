import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.config import config
from services.render_service import RenderService

def test_render():
    print("Starting rendering verification...")
    
    # 1. Check directories
    templates_dir = os.path.join(config.PROJECT_ROOT, "design_templates")
    output_dir = os.path.join(config.PROJECT_ROOT, "output")
    
    # 2. Instantiate RenderService
    renderer = RenderService(output_dir, templates_dir)
    
    # 3. Choose template and dummy data
    template_name = "motivation-theme.html"
    placeholders = {
        "TAGLINE": "CONSISTENCY",
        "QUOTE": "Small daily improvements over time lead to stunning results.",
        "SUBTEXT": "Keep grinding every day.",
        "WEBSITE": "carrotowleducation.com",
        "CTA": "Join Now"
    }
    
    try:
        # 4. Generate PNG
        print("Rendering sample template...")
        generated_path = renderer.render(template_name, placeholders, "PNG")
        
        # 5. Verify file exists and is not empty
        if os.path.exists(generated_path) and os.path.getsize(generated_path) > 0:
            print(f"Generated asset successfully at: {generated_path}")
            
            # 6. Delete temporary file
            os.remove(generated_path)
            print("Temporary file cleaned up.")
            print("Rendering Engine Verified")
            return True
        else:
            print("Error: Generated file does not exist or is empty.")
            return False
            
    except Exception as e:
        print(f"Verification Failed: {e}")
        print("\nPlease ensure Playwright Chromium browser is installed by running:")
        print("  playwright install chromium")
        return False

if __name__ == "__main__":
    success = test_render()
    sys.exit(0 if success else 1)
