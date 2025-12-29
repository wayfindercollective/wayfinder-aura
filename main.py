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
import time

# Ensure the src directory is in the path for package imports
src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)


def wait_for_display(max_wait: int = 10) -> bool:
    """Wait for display to be available (helps with autostart timing)."""
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if display:
        return True
    
    # Wait for display to become available
    for _ in range(max_wait):
        time.sleep(1)
        display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        if display:
            return True
    
    # Set fallback display
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    return True


def fix_tk_scaling():
    """
    Fix Tk9 scaling issues that can occur on autostart.
    
    On some systems (especially during autostart), Tk's scaling detection
    returns NaN, causing a crash. This sets fallback values to prevent that.
    """
    # Set TK_SCALING if not already set - this provides a fallback
    if "TK_SCALING" not in os.environ:
        os.environ["TK_SCALING"] = "1.0"
    
    # For Tcl/Tk 9.0, we may need to patch the scaling after initialization
    # This is done by setting tk scaling explicitly after creating the root window


def main():
    """Run Wayfinder Voice."""
    # Wait for display on autostart
    wait_for_display()
    
    # Apply Tk scaling fix before importing tkinter
    fix_tk_scaling()
    
    try:
        # Import from the legacy module (will be migrated to wayfinder package over time)
        from wayfinder_main import WayfinderApp
        import customtkinter as ctk
        
        # Run the application
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        
        app = WayfinderApp()
        
        # Fix Tk scaling after window creation if needed
        try:
            current_scaling = app.tk.call('tk', 'scaling')
            # Check if scaling is invalid (NaN or negative)
            if not isinstance(current_scaling, (int, float)) or current_scaling <= 0:
                app.tk.call('tk', 'scaling', 1.0)
        except Exception:
            pass  # Scaling is fine, or we can't fix it
        
        app.mainloop()
        
    except Exception as e:
        error_msg = str(e)
        
        # Check for Tk scaling error specifically
        if "NaN" in error_msg or "scaling" in error_msg.lower():
            print(f"Tk scaling error detected: {e}", file=sys.stderr)
            print("Retrying with fixed scaling...", file=sys.stderr)
            
            # Force scaling environment variable and retry
            os.environ["TK_SCALING"] = "1.0"
            
            # Clear any cached Tk state
            import importlib
            if 'tkinter' in sys.modules:
                del sys.modules['tkinter']
            if '_tkinter' in sys.modules:
                del sys.modules['_tkinter']
            
            # Retry import and run
            from wayfinder_main import WayfinderApp
            import customtkinter as ctk
            
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("dark-blue")
            
            app = WayfinderApp()
            app.tk.call('tk', 'scaling', 1.0)  # Force scaling
            app.mainloop()
        else:
            # Re-raise other errors
            raise


if __name__ == "__main__":
    main()
