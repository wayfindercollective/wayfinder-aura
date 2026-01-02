#!/usr/bin/env python3
"""
Wayfinder Aura - Entry point for `python -m wayfinder`

This module allows running Wayfinder Aura as a package:
    python -m wayfinder
    
Or via the console script after pip install:
    wayfinder-aura
"""

import sys
import os


def main():
    """
    Main entry point for Wayfinder Aura.
    
    Currently delegates to wayfinder_main.py during the migration period.
    As more functionality is extracted to the wayfinder package, this will
    be updated to use the new modular structure directly.
    """
    # Add the project root to path so we can import the legacy main module
    # This allows gradual migration to the new package structure
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    
    # Import and run the legacy main module
    try:
        from wayfinder_main import main as run_app
        run_app()
    except ImportError:
        # Fallback: try importing from the installed location
        print("Error: Could not find wayfinder_main.py")
        print("Make sure you're running from the project root directory.")
        sys.exit(1)


if __name__ == "__main__":
    main()

