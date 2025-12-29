#!/usr/bin/env python3
"""
Wayfinder Voice - Local voice dictation for Linux

This is the main entry point that maintains backwards compatibility.
Run with: python main.py

The application is being migrated to a proper package structure at src/wayfinder/
For package-style execution, use: python -m wayfinder (from project root with PYTHONPATH set)
"""

import sys
import os
import json
from pathlib import Path

# Ensure the src directory is in the path for package imports
src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)


# Scaling cache file location
SCALING_CACHE_FILE = Path.home() / ".config" / "wayfinder-voice" / "display_scaling.json"


def load_cached_scaling() -> float:
    """Load the last known good scaling value from cache."""
    try:
        if SCALING_CACHE_FILE.exists():
            with open(SCALING_CACHE_FILE, "r") as f:
                data = json.load(f)
                scaling = data.get("scaling", 1.0)
                # Validate it's a reasonable value
                if isinstance(scaling, (int, float)) and 0.5 <= scaling <= 4.0:
                    return float(scaling)
    except Exception:
        pass
    return 1.0  # Safe default


def save_cached_scaling(scaling: float) -> None:
    """Save the current scaling value to cache for future startups."""
    try:
        SCALING_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SCALING_CACHE_FILE, "w") as f:
            json.dump({"scaling": scaling}, f)
    except Exception:
        pass  # Non-critical, ignore errors


def apply_scaling_fix():
    """
    Apply Tk scaling fix using cached value.
    
    This uses the last known good scaling value so the app can start
    immediately without waiting for display detection. The scaling
    will be updated once the display is actually available.
    """
    cached_scaling = load_cached_scaling()
    os.environ["TK_SCALING"] = str(cached_scaling)
    return cached_scaling


def detect_and_update_scaling(app) -> None:
    """
    Detect current display scaling and update the cache.
    Called after the app is running to capture the real scaling.
    """
    try:
        # Get current Tk scaling
        current_scaling = app.tk.call('tk', 'scaling')
        
        # Validate it's a good value (not NaN, not negative, reasonable range)
        if isinstance(current_scaling, (int, float)) and 0.5 <= current_scaling <= 4.0:
            # Only update cache if it's different from what we have
            cached = load_cached_scaling()
            if abs(current_scaling - cached) > 0.01:  # Meaningful difference
                save_cached_scaling(float(current_scaling))
                print(f"[Scaling] Updated cached scaling: {cached} -> {current_scaling}")
    except Exception:
        pass  # Display might not be ready yet, that's okay


def schedule_scaling_detection(app, delay_ms: int = 5000) -> None:
    """
    Schedule periodic scaling detection.
    This runs after the app starts to detect when display becomes available.
    """
    def check_scaling():
        detect_and_update_scaling(app)
        # Check again in 30 seconds (in case display was turned on later)
        app.after(30000, check_scaling)
    
    # First check after initial delay (give display time to initialize)
    app.after(delay_ms, check_scaling)


def main():
    """Run Wayfinder Voice."""
    # Apply scaling fix immediately using cached value (no waiting!)
    cached_scaling = apply_scaling_fix()
    print(f"[Scaling] Using cached scaling: {cached_scaling}")
    
    try:
        # Import from the legacy module
        from wayfinder_main import WayfinderApp
        import customtkinter as ctk
        
        # Run the application
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        
        app = WayfinderApp()
        
        # Try to apply scaling directly if the cached value seems wrong
        try:
            current = app.tk.call('tk', 'scaling')
            # Check if current scaling is invalid
            if not isinstance(current, (int, float)) or current <= 0 or current != current:  # NaN check
                app.tk.call('tk', 'scaling', cached_scaling)
        except Exception:
            # If we can't even check, force our cached scaling
            try:
                app.tk.call('tk', 'scaling', cached_scaling)
            except Exception:
                pass
        
        # Schedule background scaling detection to update cache when display is ready
        schedule_scaling_detection(app)
        
        app.mainloop()
        
    except Exception as e:
        error_msg = str(e)
        
        # Check for Tk scaling error specifically
        if "NaN" in error_msg or "scaling" in error_msg.lower() or "tk.tcl" in error_msg.lower():
            print(f"[Scaling] Tk error detected: {e}", file=sys.stderr)
            print(f"[Scaling] Retrying with safe scaling (1.0)...", file=sys.stderr)
            
            # Force safe scaling and retry
            os.environ["TK_SCALING"] = "1.0"
            save_cached_scaling(1.0)  # Update cache with safe value
            
            # Clear any cached Tk state
            for mod in list(sys.modules.keys()):
                if 'tk' in mod.lower():
                    del sys.modules[mod]
            
            # Retry
            from wayfinder_main import WayfinderApp
            import customtkinter as ctk
            
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("dark-blue")
            
            app = WayfinderApp()
            try:
                app.tk.call('tk', 'scaling', 1.0)
            except Exception:
                pass
            
            schedule_scaling_detection(app)
            app.mainloop()
        else:
            raise


if __name__ == "__main__":
    main()
