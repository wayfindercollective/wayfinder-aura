#!/usr/bin/env python3
"""
Wayfinder Aura - Local voice dictation for Linux

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
SCALING_CACHE_FILE = Path.home() / ".config" / "wayfinder-aura" / "display_scaling.json"


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


LOCK_SOCKET_PATH = "/tmp/wayfinder-aura.lock"


def _signal_existing_instance() -> bool:
    """Try to signal an already-running instance to show its window.
    
    Uses a Unix socket to communicate with the existing instance.
    Returns True if we successfully signaled another instance (so we should exit).
    """
    import socket
    
    try:
        # Try to connect to existing instance's lock socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(LOCK_SOCKET_PATH)
        sock.sendall(b"SHOW\n")
        sock.close()
        print("[Instance] Signaled existing instance to show window")
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        # No existing instance running
        return False


def _start_instance_listener(app):
    """Start a background listener that receives signals from new launch attempts."""
    import socket
    import threading
    
    # Clean up stale socket
    try:
        os.unlink(LOCK_SOCKET_PATH)
    except OSError:
        pass
    
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(LOCK_SOCKET_PATH)
    server.listen(1)
    server.settimeout(1)
    
    def listener():
        while True:
            try:
                conn, _ = server.accept()
                data = conn.recv(64).decode().strip()
                conn.close()
                if data == "SHOW":
                    # Show the window from the main thread
                    app.after(0, app.show_from_tray)
            except socket.timeout:
                continue
            except Exception:
                break
    
    t = threading.Thread(target=listener, daemon=True, name="InstanceListener")
    t.start()
    return server


def _check_venv_health():
    """Check that the virtual environment matches the running Python version.

    System updates (e.g. Fedora/Bazzite) can change the system Python version,
    leaving the venv pointing at a version that no longer exists. This causes
    cryptic ModuleNotFoundError crashes on launch.
    """
    venv_dir = Path(__file__).parent / "venv-gpu"
    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if not pyvenv_cfg.exists():
        return  # No venv to check

    try:
        cfg = {}
        for line in pyvenv_cfg.read_text().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                cfg[key.strip()] = val.strip()

        venv_version = cfg.get("version", "")
        running_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # Compare major.minor — micro mismatches are usually fine
        venv_major_minor = ".".join(venv_version.split(".")[:2])
        running_major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"

        if venv_major_minor and venv_major_minor != running_major_minor:
            print(f"\n{'='*60}")
            print(f"  VENV MISMATCH: venv was built with Python {venv_version}")
            print(f"  but the system is now running Python {running_version}.")
            print(f"  This usually happens after a system update + reboot.")
            print(f"")
            print(f"  Fix: rebuild the venv:")
            print(f"    rm -rf venv-gpu")
            print(f"    python3 -m venv venv-gpu")
            print(f"    source venv-gpu/bin/activate")
            print(f"    pip install -r requirements.txt")
            print(f"{'='*60}\n")
            sys.exit(1)
    except Exception:
        pass  # Don't block launch if we can't read the config


def main():
    """Run Wayfinder Aura."""
    # === Venv health check ===
    _check_venv_health()

    # === Single-instance check ===
    # If another instance is already running, signal it to show and exit
    if _signal_existing_instance():
        sys.exit(0)
    
    # === GPU SETUP (do this FIRST, before any imports that might use GPU) ===
    # This sets GGML_VK_VISIBLE_DEVICES once, and all subprocesses inherit it
    try:
        from wayfinder.utils.gpu_simple import setup_gpu_environment
        from wayfinder.config import load_config
        config = load_config()
        setup_gpu_environment(config)
    except Exception as e:
        print(f"[GPU] Warning: Could not setup GPU environment: {e}")
    
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
        
        # Start single-instance listener so future launches signal us
        _instance_server = _start_instance_listener(app)
        
        # ─── First-run setup wizard ───
        # Use the app's config (which is the most up-to-date after constructor)
        print(f"[Setup] setup_completed={app.config.get('setup_completed')} (type={type(app.config.get('setup_completed')).__name__})", flush=True)
        try:
            if not app.config.get("setup_completed", False):
                print(f"[Setup] Showing wizard!", flush=True)
                from wayfinder.ui.dialogs.setup_wizard import SetupWizard
                
                # Pause animations during wizard to keep UI responsive
                # (animations consume CPU in the shared Tk event loop)
                app._stop_idle_breath()
                
                # Don't withdraw the main window - on KDE Wayland, transient
                # children of withdrawn windows don't appear. The wizard's
                # grab_set() prevents interaction with the main window anyway.
                wizard = SetupWizard(app, app.config)
                app.wait_window(wizard)
                
                # Resume animations and ensure main window is visible + focused
                app._start_idle_breath()
                app.deiconify()
                app.lift()
                app.focus_force()
                
                # Sync the flag into WayfinderApp's config so its future
                # save_config() calls don't overwrite and erase it.
                app.config["setup_completed"] = True
                
                if wizard.result:
                    print("[Setup] Setup wizard completed successfully")
                else:
                    print("[Setup] Setup wizard skipped")
        except Exception as e:
            print(f"[Setup] Warning: Could not show setup wizard: {e}")
            import traceback
            traceback.print_exc()
        
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
        
        # Clean up lock socket on normal exit
        try:
            os.unlink(LOCK_SOCKET_PATH)
        except OSError:
            pass
        
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


import atexit

def _cleanup_lock_socket():
    """Remove the lock socket on exit."""
    try:
        os.unlink(LOCK_SOCKET_PATH)
    except OSError:
        pass

atexit.register(_cleanup_lock_socket)


if __name__ == "__main__":
    main()
