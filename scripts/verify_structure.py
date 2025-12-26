#!/usr/bin/env python3
"""
Verify the Wayfinder Voice project structure is correct.

Run from project root:
    python scripts/verify_structure.py
    
Or with PYTHONPATH for package imports:
    PYTHONPATH=src python scripts/verify_structure.py
"""

import sys
import os

# Add src to path for package imports
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_dir = os.path.join(project_root, "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def test_package_imports():
    """Test that all package modules import correctly."""
    print("Testing package imports...")
    
    tests = [
        ("wayfinder", "__version__"),
        ("wayfinder.config", "load_config"),
        ("wayfinder.config", "DEFAULT_CONFIG"),
        ("wayfinder.state", "AppState"),
        ("wayfinder.ui.theme", "COLORS"),
        ("wayfinder.ui.components", "ToolTip"),
        ("wayfinder.hotkeys", "EventType"),
        ("wayfinder.hotkeys.evdev", "hotkey_listener"),
        ("wayfinder.hotkeys.socket", "socket_listener"),
        ("wayfinder.hotkeys.dbus", "wayland_hotkey_listener"),
        ("wayfinder.utils.gpu", "get_gpu_info"),
        ("wayfinder.utils.platform", "is_wayland"),
        ("wayfinder.core", "AudioRecorder"),
        ("wayfinder.core", "transcribe_with_config"),
        ("wayfinder.core", "inject_text"),
        ("wayfinder.app", "load_config"),
    ]
    
    failed = []
    for module, attr in tests:
        try:
            mod = __import__(module, fromlist=[attr])
            if not hasattr(mod, attr):
                failed.append(f"{module}.{attr} - attribute not found")
            else:
                print(f"  ✓ {module}.{attr}")
        except ImportError as e:
            failed.append(f"{module}.{attr} - {e}")
    
    return failed


def test_root_imports():
    """Test that legacy root imports still work (for wayfinder_main.py)."""
    print("\nTesting root imports (legacy)...")
    
    tests = [
        "recorder",
        "transcriber", 
        "injector",
        "postprocessor",
        "ollama_manager",
        "license",
    ]
    
    failed = []
    for module in tests:
        try:
            __import__(module)
            print(f"  ✓ {module}")
        except ImportError as e:
            failed.append(f"{module} - {e}")
    
    return failed


def test_structure():
    """Verify expected directories and files exist."""
    print("\nVerifying project structure...")
    
    expected = [
        "src/wayfinder/__init__.py",
        "src/wayfinder/__main__.py",
        "src/wayfinder/app.py",
        "src/wayfinder/config.py",
        "src/wayfinder/state.py",
        "src/wayfinder/core/__init__.py",
        "src/wayfinder/core/recorder.py",
        "src/wayfinder/core/transcriber.py",
        "src/wayfinder/core/injector.py",
        "src/wayfinder/ui/__init__.py",
        "src/wayfinder/ui/theme.py",
        "src/wayfinder/ui/components.py",
        "src/wayfinder/ui/overlay.py",
        "src/wayfinder/hotkeys/__init__.py",
        "src/wayfinder/hotkeys/evdev.py",
        "src/wayfinder/hotkeys/socket.py",
        "src/wayfinder/hotkeys/dbus.py",
        "src/wayfinder/utils/__init__.py",
        "src/wayfinder/utils/gpu.py",
        "src/wayfinder/utils/platform.py",
        "main.py",
        "wayfinder_main.py",
        "pyproject.toml",
        "requirements.txt",
    ]
    
    missing = []
    for path in expected:
        full_path = os.path.join(project_root, path)
        if os.path.exists(full_path):
            print(f"  ✓ {path}")
        else:
            missing.append(path)
    
    return missing


def main():
    print("=" * 60)
    print("WAYFINDER VOICE - STRUCTURE VERIFICATION")
    print("=" * 60)
    print()
    
    all_errors = []
    
    # Test package imports
    pkg_errors = test_package_imports()
    all_errors.extend(pkg_errors)
    
    # Test root imports
    root_errors = test_root_imports()
    all_errors.extend(root_errors)
    
    # Test structure
    struct_errors = test_structure()
    all_errors.extend([f"Missing: {p}" for p in struct_errors])
    
    print()
    print("=" * 60)
    
    if all_errors:
        print("VERIFICATION FAILED ❌")
        print()
        print("Errors:")
        for err in all_errors:
            print(f"  • {err}")
        return 1
    else:
        print("ALL CHECKS PASSED ✅")
        print()
        print("The project structure is ready for development.")
        return 0


if __name__ == "__main__":
    sys.exit(main())


