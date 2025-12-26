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

# Ensure the src directory is in the path for package imports
src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)


def main():
    """Run Wayfinder Voice."""
    # Import from the legacy module (will be migrated to wayfinder package over time)
    from wayfinder_main import WayfinderApp
    import customtkinter as ctk
    
    # Run the application
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    
    app = WayfinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
