import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.config import config
from engine.workflow import PlatformWorkflow
from engine.group_config import load_group_config

def test_integration():
    print("Initializing Platform Workflow...")
    # TODO: make CLI-selectable
    group = load_group_config("placement_prep")
    workflow = PlatformWorkflow(config, group)
    
    print("Testing Generation...")
    slot = {"category": "motivation", "topic": "Consistency in placement preparation", "time": "10:00"}
    
    def status_cb(msg):
        print(f"[Status] {msg}")
        
    res = workflow.generate_single_content(slot, save_to_sheets=True, status_callback=status_cb)
    
    post_id = res['id']
    title = res['topic']
    content = res['content']
    
    print(f"Generated Post ID: {post_id}")
    print(f"Generated Title: {title}")
    
    print("Testing Asset Generation (Planner -> Mapper -> Renderer)...")
    # Force generating PNG
    asset_res = workflow.generate_assets(post_id, title, content, force_pdf_status="N/A", force_img_status="pending")
    
    print("Integration Test Complete.")
    
if __name__ == "__main__":
    test_integration()
