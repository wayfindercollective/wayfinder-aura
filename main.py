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
import signal
import importlib
from pathlib import Path

# macOS: Suppress SIGTRAP before any imports that touch pynput/CGEventTap.
# When the process is not yet in the Accessibility clients list, macOS sends
# SIGTRAP from CGEventTapCreate. This is a non-fatal warning — hotkeys still
# work once accessibility is granted, and the socket-based trigger always
# works regardless. Ignoring the signal prevents an immediate crash.
if sys.platform == "darwin":
    try:
        signal.signal(signal.SIGTRAP, signal.SIG_IGN)
    except (OSError, ValueError):
        pass  # Already in a signal handler context or SIGTRAP not available

# Windows: an inherited console or pipe uses the legacy code page (cp1252), so
# the app's emoji status prints raise UnicodeEncodeError and kill startup.
# Replace unencodable characters instead of crashing.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


# Ensure the src directory is in the path for package imports
if getattr(sys, 'frozen', False):
    # Running as bundled .app — modules are in the bundle
    _base_dir = os.path.dirname(sys.executable)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.join(_base_dir, "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# macOS has no PR_SET_PDEATHSIG. Long-lived native helpers are launched through
# this lightweight sentinel so they cannot survive a crash of the owning app.
if "--child-supervisor" in sys.argv:
    from wayfinder.utils.child_supervisor import run_from_env

    raise SystemExit(run_from_env())

# Configure certificate trust before any license, catalog, cloud, or model
# download code can create an HTTP client. This is essential for AppImages:
# their bundled OpenSSL otherwise searches the build distro's CA paths, which
# may not exist on Bazzite/Fedora or SteamOS.
from wayfinder.tls import configure_tls_ca_bundle

_TLS_CA_BUNDLE = configure_tls_ca_bundle()


def _configure_frozen_fontconfig() -> Path | None:
    """Expose bundled product fonts when the one-file binary runs directly.

    AppRun already creates an equivalent config for the AppImage and Flatpak
    installs fonts below /app/share/fonts.  A bare PyInstaller executable has
    neither wrapper, so Fedora/Bazzite substitutes Noto Sans even though the
    font files are inside the bundle.  Configure fontconfig before Tk/PyQt can
    initialize it so dogfood and release artifacts render identically.
    """
    if (
        not sys.platform.startswith("linux")
        or not getattr(sys, "frozen", False)
        or os.environ.get("FONTCONFIG_FILE")
    ):
        return None

    bundle_root = Path(getattr(sys, "_MEIPASS", _base_dir))
    fonts_dir = bundle_root / "assets" / "fonts"
    if not fonts_dir.is_dir():
        return None

    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    config_dir = runtime_root / "wayfinder-aura" / "fontconfig"
    config_path = config_dir / "fonts.conf"
    try:
        from xml.sax.saxutils import escape

        config_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "<?xml version=\"1.0\"?>\n"
            "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">\n"
            "<fontconfig>\n"
            "  <include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include>\n"
            f"  <dir>{escape(str(fonts_dir))}</dir>\n"
            "</fontconfig>\n",
            encoding="utf-8",
        )
        os.environ["FONTCONFIG_FILE"] = str(config_path)
        return config_path
    except OSError as exc:
        print(f"[Fonts] Could not expose bundled fonts: {exc}", file=sys.stderr)
        return None


_configure_frozen_fontconfig()

# A font file inside a macOS bundle is not automatically visible to Aqua Tk.
# Register product faces before any renderer self-test or UI root is created.
if sys.platform == "darwin":
    try:
        from wayfinder.utils.macos_fonts import register_bundled_macos_fonts

        _REGISTERED_MACOS_FONTS = register_bundled_macos_fonts()
    except Exception as _font_registration_error:
        _REGISTERED_MACOS_FONTS = []
        print(
            f"[Fonts] Could not register bundled macOS fonts: {_font_registration_error}",
            file=sys.stderr,
        )


# Packaged network release probe. It validates the same urllib + CA path used
# by speech-model downloads without downloading a model payload.
if "--tls-self-test" in sys.argv:
    try:
        from wayfinder.tls import probe_tls

        _tls_status, _tls_bundle = probe_tls()
        print(
            "TLS_SELF_TEST_OK "
            f"status={_tls_status} ca_bundle={str(_tls_bundle)!r}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _tls_error:
        print(
            "TLS_SELF_TEST_FAILED "
            f"{_tls_error.__class__.__name__}: {_tls_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Frozen overlay dispatch: PyInstaller builds can't spawn "python overlay.py"
# (sys.executable IS the app binary — that spawn would relaunch the full app,
# a fork bomb). Instead the binary doubles as the overlay when launched with
# this sentinel. Must run before ANY app-side work: this process becomes only
# the overlay. Remaining argv (--mode/--style/--scale/…) is parsed by overlay.
if "--overlay-subprocess" in sys.argv:
    sys.argv.remove("--overlay-subprocess")
    from wayfinder.ui.overlay import run_overlay
    run_overlay()
    sys.exit(0)


# Packaged audio release probe.  It runs before Tk, config, the single-instance
# lock, or GPU setup so CI can validate AppImage/Flatpak playback headlessly.
# The helper is also the mic-test UI's real playback implementation.
if "--audio-output-self-test" in sys.argv:
    try:
        from wayfinder.utils.audio_output import output_self_test

        _audio_result = output_self_test()
        print(
            "AUDIO_OUTPUT_SELF_TEST_OK "
            f"device={_audio_result.output_name!r} "
            f"requested_rate={_audio_result.requested_rate} "
            f"used_rate={_audio_result.used_rate} "
            f"resampled={_audio_result.resampled}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _audio_error:
        print(
            "AUDIO_OUTPUT_SELF_TEST_FAILED "
            f"{_audio_error.__class__.__name__}: {_audio_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Packaged microphone probe. Unlike an import check, this opens the finished
# AppImage/Flatpak capture boundary and requires real frames from its audio
# server (CI provides a null source). It catches silent/wedged package routes.
if "--audio-input-self-test" in sys.argv:
    try:
        from wayfinder.utils.audio_input import input_self_test

        _audio_input_result = input_self_test()
        print(
            "AUDIO_INPUT_SELF_TEST_OK "
            f"backend={_audio_input_result.backend!r} "
            f"frames={_audio_input_result.frames} "
            f"source={_audio_input_result.source_name!r}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _audio_input_error:
        print(
            "AUDIO_INPUT_SELF_TEST_FAILED "
            f"{_audio_input_error.__class__.__name__}: {_audio_input_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Packaged preprocessing probe. Medium silently degrades to Light when SciPy's
# dynamically imported signal module is omitted from a bundle, while unbounded
# normalization can turn stationary mic noise into hallucinated speech. Exercise
# those exact package boundaries without opening an audio device or Tk.
if "--audio-processing-self-test" in sys.argv:
    try:
        import numpy as np
        from wayfinder.core import recorder as _recorder

        if _recorder._get_scipy_signal_functions() is None:
            raise RuntimeError("scipy.signal unavailable; Medium rumble filter is disabled")

        _rate = 16000
        _rng = np.random.default_rng(20260731)
        _noise = _rng.normal(0.0, 0.002, _rate * 2).astype(np.float32)
        _light = _recorder.preprocess_audio(_noise, _rate, "light")
        _medium = _recorder.preprocess_audio(_noise, _rate, "medium")
        _heavy = _recorder.preprocess_audio(_noise, _rate, "heavy")

        if float(np.max(np.abs(_light))) > float(np.max(np.abs(_noise))) * 8.01:
            raise RuntimeError("Light normalization exceeded its +18dB safety cap")
        if _recorder.audio_has_speech_activity(_light, _rate):
            raise RuntimeError("stationary noise was classified as speech")
        _medium_rms = float(np.sqrt(np.mean(_medium * _medium)))
        _heavy_rms = float(np.sqrt(np.mean(_heavy * _heavy)))
        if _heavy_rms >= _medium_rms * 0.4:
            raise RuntimeError("Heavy soft gate did not reduce stationary noise")

        print(
            "AUDIO_PROCESSING_SELF_TEST_OK "
            f"light_peak={float(np.max(np.abs(_light))):.6f} "
            f"medium_rms={_medium_rms:.6f} heavy_rms={_heavy_rms:.6f}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _processing_error:
        print(
            "AUDIO_PROCESSING_SELF_TEST_FAILED "
            f"{_processing_error.__class__.__name__}: {_processing_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Packaged visual release probe. A no-Xft Tk build boots successfully but
# silently renders every family/size as the same legacy bitmap font, so a
# normal GUI liveness test cannot catch the resulting flat/jagged interface.
if "--ui-renderer-self-test" in sys.argv:
    try:
        from wayfinder.utils.tk_renderer import require_compatible_tk_renderer

        _renderer = require_compatible_tk_renderer()
        print(
            "UI_RENDERER_SELF_TEST_OK "
            f"tk={_renderer.patchlevel!r} "
            f"windowing={_renderer.windowing_system!r} "
            f"fontsystem={_renderer.font_system!r} "
            f"family={_renderer.actual_family!r} "
            f"families={_renderer.family_count}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _renderer_error:
        print(
            "UI_RENDERER_SELF_TEST_FAILED "
            f"{_renderer_error.__class__.__name__}: {_renderer_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Probe the bundled local-cleanup engine before Tk or single-instance locking.
# Manual acceptance can also exercise inference with an already-downloaded GGUF.
if "--llm-engine-self-test" in sys.argv:
    try:
        import llama_cpp

        llama_cpp.llama_print_system_info()
        _llm_test_model = os.environ.get("WAYFINDER_LLM_SELFTEST_MODEL")
        if _llm_test_model:
            # llama_cpp.Llama is not a context manager in the pinned 0.3.x wheel.
            _llm = llama_cpp.Llama(model_path=_llm_test_model, n_ctx=512, verbose=False)
            try:
                _llm_result = _llm.create_completion(prompt="The capital of France is", max_tokens=8)
            finally:
                _llm.close()
            if not _llm_result["choices"][0]["text"].strip():
                raise RuntimeError("LLM engine returned empty completion text")
        print("LLM_ENGINE_SELF_TEST_OK", flush=True)
        sys.exit(0)
    except Exception as _llm_error:
        print(
            "LLM_ENGINE_SELF_TEST_FAILED "
            f"{_llm_error.__class__.__name__}: {_llm_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Import the complete application module from the frozen archive. This catches
# missing hidden imports/native extensions without pretending a hardware-less
# CI runner is a real Linux desktop. Renderer, playback, and processing have
# dedicated packaged probes; the built AppImage is launched on Bazzite before
# tagging a release.
if "--app-import-self-test" in sys.argv:
    try:
        from wayfinder_main import WayfinderApp as _imported_app

        if not callable(_imported_app):
            raise RuntimeError("WayfinderApp did not import as a callable class")
        print("APP_IMPORT_SELF_TEST_OK", flush=True)
        sys.exit(0)
    except Exception as _import_error:
        print(
            "APP_IMPORT_SELF_TEST_FAILED "
            f"{_import_error.__class__.__name__}: {_import_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Confirm that native executables shipped beside the frozen application are
# discoverable through the same resolver used by transcription and cleanup.
if "--runtime-assets-self-test" in sys.argv:
    try:
        from wayfinder.utils.runtime_assets import find_llama_binary, find_whisper_binary

        _whisper_binary = find_whisper_binary()
        _llama_binary = find_llama_binary()
        if not _whisper_binary:
            raise RuntimeError("bundled whisper-cli was not resolved")
        if not _llama_binary:
            raise RuntimeError("bundled llama.cpp CLI was not resolved")
        print(
            "RUNTIME_ASSETS_SELF_TEST_OK "
            f"whisper={_whisper_binary!r} llama={_llama_binary!r}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _runtime_assets_error:
        print(
            "RUNTIME_ASSETS_SELF_TEST_FAILED "
            f"{_runtime_assets_error.__class__.__name__}: {_runtime_assets_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


if "--macos-native-renderers-self-test" in sys.argv:
    try:
        import ctypes
        from wayfinder.ui import macos_hero_metal as _hero_metal
        from wayfinder.ui import macos_overlay_metal as _overlay_metal

        _native_path = next(
            path for path in _hero_metal._native_library_candidates() if path.is_file()
        )
        _native_lib = ctypes.CDLL(str(_native_path))
        for _symbol in (
            "wf_hero_create",
            "wf_hero_destroy",
            "wf_overlay_create",
            "wf_overlay_destroy",
        ):
            getattr(_native_lib, _symbol)
        _device = _hero_metal._default_device()
        if _device is None:
            raise RuntimeError("Metal device unavailable")
        for _source, _function in (
            (_hero_metal._SHADER, "hero_wave"),
            (_overlay_metal._SHADER, "overlay_wave"),
        ):
            _result = _device.newLibraryWithSource_options_error_(_source, None, None)
            _library, _error = _result if isinstance(_result, tuple) else (_result, None)
            if _library is None or _library.newFunctionWithName_(_function) is None:
                raise RuntimeError(f"Metal shader {_function} failed: {_error}")
        print(
            f"MACOS_NATIVE_RENDERERS_SELF_TEST_OK library={str(_native_path)!r}",
            flush=True,
        )
        sys.exit(0)
    except Exception as _native_renderer_error:
        print(
            "MACOS_NATIVE_RENDERERS_SELF_TEST_FAILED "
            f"{_native_renderer_error.__class__.__name__}: {_native_renderer_error}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# Scaling cache follows the platform directory contract. In particular, a
# macOS build must not create a Linux-style ~/.config directory.
from wayfinder.utils.platform import get_cache_dir

# macOS: ~/Library/Caches. Linux/Windows keep main's location.
SCALING_CACHE_FILE = (
    get_cache_dir() / "display_scaling.json"
    if sys.platform == "darwin"
    else Path.home() / ".config" / "wayfinder-aura" / "display_scaling.json"
)


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


# Single-instance: fcntl flock on a regular file (survives crashes without
# leaving a half-open Unix socket path that blocks later launches). SHOW is
# delivered via the control socket the tray already uses.
_INSTANCE_LOCK_FD = None  # keep open for process lifetime


def _instance_lock_path() -> Path:
    if sys.platform in {"win32", "darwin"}:
        # Windows/macOS have no XDG_RUNTIME_DIR; keep the lock in the
        # platform-native per-user cache directory.
        from wayfinder.utils.platform import get_cache_dir
        return get_cache_dir() / "instance.lock"
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return Path(runtime) / "wayfinder-aura" / "instance.lock"


def _control_socket_path() -> str:
    try:
        from wayfinder.config import SOCKET_PATH
        return str(SOCKET_PATH)
    except Exception:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        return str(Path(runtime) / "wayfinder-aura" / "wayfinder-aura.sock")


def _try_control_command(verb: bytes, *, expect_reply: bool = False) -> bool:
    """Send a control-socket verb to a live instance. Returns True if connected."""
    if sys.platform == "win32":
        # AF_UNIX control IPC is Linux/macOS-only. A Windows control channel
        # (named pipe or localhost socket) is a planned adapter; until then a
        # second launch simply exits instead of raising the first window.
        return False
    import socket

    path = _control_socket_path()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(path)
        sock.sendall(verb)
        if expect_reply:
            try:
                sock.recv(16)
            except socket.timeout:
                pass
        sock.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def _try_control_show() -> bool:
    """Ask a live instance to raise its window via the control socket."""
    return _try_control_command(b"show", expect_reply=True)


def _dispatch_cli_control_verb() -> int | None:
    """Handle flatpak/desktop CLI hooks: --toggle / --cycle-style / --hide.

    Returns an exit code if this process should exit, or None to continue
    normal GUI launch.
    """
    if len(sys.argv) < 2:
        return None
    flag = sys.argv[1]
    mapping = {
        "--toggle": (b"toggle", False),
        "--cycle-style": (b"style", False),
        "--hide": (b"hide", True),
        "--show": (b"show", True),
    }
    if flag not in mapping:
        return None
    verb, expect = mapping[flag]
    if _try_control_command(verb, expect_reply=expect):
        return 0
    print(f"[CLI] No live instance for {flag} (socket unreachable)", file=sys.stderr)
    return 1


def _acquire_instance_lock_windows() -> bool:
    """Windows single-instance lock via msvcrt byte-range lock (the fcntl.flock analog).

    Uses a non-blocking exclusive lock on byte 0 of a lock file in the cache dir.
    The lock is held for the process lifetime (fd kept open) and released by the
    OS on exit. Opened append-mode so a second instance never truncates the byte
    the primary holds locked (which would surface as a sharing violation).
    """
    import msvcrt

    global _INSTANCE_LOCK_FD
    path = _instance_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "a+", encoding="utf-8")
    except OSError as e:
        print(f"[Instance] Warning: could not open lock file ({e}); continuing")
        return True  # fail open — better a second instance than no launch
    try:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            fd.close()
        except Exception:
            pass
        return False  # another live instance holds the lock
    try:
        fd.write(str(os.getpid()))  # append-mode write; informational only
        fd.flush()
    except OSError:
        pass  # PID annotation is best-effort; the lock is what matters
    _INSTANCE_LOCK_FD = fd  # keep open so the lock is held
    return True


def _acquire_instance_lock() -> bool:
    """Exclusive flock. True = we are the primary instance. False = another holds it."""
    if sys.platform == "win32":
        return _acquire_instance_lock_windows()

    import fcntl

    global _INSTANCE_LOCK_FD
    path = _instance_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "w", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        _INSTANCE_LOCK_FD = fd  # keep open so the lock is held
        return True
    except BlockingIOError:
        try:
            fd.close()
        except Exception:
            pass
        return False
    except OSError as e:
        print(f"[Instance] Warning: could not acquire lock ({e}); continuing")
        return True  # fail open — better a second instance than no launch


def _signal_existing_instance() -> bool:
    """If another instance holds the lock, ask it to show and return True (caller exits)."""
    if _acquire_instance_lock():
        return False  # we own the lock — continue startup

    if _try_control_show():
        print("[Instance] Signaled existing instance to show window")
        return True

    print("[Instance] Another instance holds the lock but is not responding")
    return True  # still exit — don't stack a second half-dead UI


VENV_SMOKE_IMPORTS = ("customtkinter", "PIL", "numpy")


def _missing_venv_smoke_imports(modules: tuple[str, ...] = VENV_SMOKE_IMPORTS) -> list[str]:
    missing: list[str] = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing.append(f"{module} ({exc})")
    return missing


def _has_tkinter_failure(missing_imports: list[str]) -> bool:
    return any("tkinter" in item for item in missing_imports)


def _check_venv_health(venv_dir: Path | None = None, smoke_imports: tuple[str, ...] = VENV_SMOKE_IMPORTS):
    """Check that the virtual environment matches the running Python version.

    System updates (e.g. Fedora/Bazzite) can change the system Python version,
    leaving the venv pointing at a version that no longer exists. This causes
    cryptic ModuleNotFoundError crashes on launch. pyvenv.cfg can also be stale
    even when the interpreter and imports are usable, so version metadata alone
    is only a warning after smoke imports pass.
    """
    from wayfinder.config import IS_APPIMAGE
    if IS_APPIMAGE:
        return  # AppImage bundles its own Python — no venv to check

    venv_dir = venv_dir or Path(__file__).parent / "venv-gpu"
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
            missing_imports = _missing_venv_smoke_imports(smoke_imports)
            if not missing_imports:
                print(
                    f"[Launcher] Warning: pyvenv.cfg says Python {venv_version}, "
                    f"but running Python {running_version}; smoke imports passed, continuing."
                )
                return

            print(f"\n{'='*60}")
            print(f"  VENV MISMATCH: venv was built with Python {venv_version}")
            print(f"  but the system is now running Python {running_version}.")
            print(f"  This usually happens after a system update + reboot.")
            print("")
            print("  Failed smoke imports:")
            for item in missing_imports:
                print(f"    - {item}")
            print(f"")
            print(f"  Fix: rebuild the venv:")
            print(f"    rm -rf venv-gpu")
            print(f"    python3 -m venv venv-gpu")
            print(f"    source venv-gpu/bin/activate")
            print(f"    pip install -r requirements.txt")
            if _has_tkinter_failure(missing_imports):
                print("")
                print("  If tkinter is missing, install the OS Tk package first:")
                print("    Fedora/Bazzite: sudo dnf install python3-tkinter")
                print("    Debian/Ubuntu: sudo apt install python3-tk")
            print(f"{'='*60}\n")
            sys.exit(1)
    except Exception:
        pass  # Don't block launch if we can't read the config


def main():
    """Run Wayfinder Aura."""
    # Desktop actions / Flatpak CLI: send control-socket verbs and exit
    # (do not take the single-instance lock or start a second UI).
    _cli_exit = _dispatch_cli_control_verb()
    if _cli_exit is not None:
        sys.exit(_cli_exit)

    # === Venv health check ===
    _check_venv_health()

    # === Single-instance check ===
    # flock-based: if another instance holds the lock, signal show and exit
    if _signal_existing_instance():
        sys.exit(0)

    # Delete audio remnants left by a prior crash before any recorder can
    # create files for this process. The legacy wayfinder_main.py entry point
    # did this, but the real packaged entry point is this module.
    try:
        from wayfinder.utils.fs_security import (
            cleanup_app_temp_dir,
            migrate_legacy_macos_cache,
        )

        migrate_legacy_macos_cache()
        cleanup_app_temp_dir()
    except Exception:
        pass
    
    # === GPU SETUP (do this FIRST, before any imports that might use GPU) ===
    # This sets GGML_VK_VISIBLE_DEVICES once, and all subprocesses inherit it
    try:
        from wayfinder.utils.gpu_simple import setup_gpu_environment
        from wayfinder.config import enforce_license_config, load_config, save_config
        from wayfinder.license import get_feature_gate
        config = load_config()
        if sys.platform == "darwin":
            try:
                from wayfinder.utils.macos_permissions import (
                    MacOSInputPermissionStatus,
                    macos_install_location_ready,
                    request_startup_input_permissions,
                )

                # Request the grants in sequence. macOS may require Aura to be
                # added manually to Input Monitoring; the in-app banner names
                # that fallback explicitly.
                if not macos_install_location_ready():
                    print(
                        "[Permissions] Move Wayfinder Aura to /Applications before "
                        "granting macOS privacy permissions.",
                        flush=True,
                    )
                    _permission_status = MacOSInputPermissionStatus(None, None, False)
                else:
                    _permission_status = request_startup_input_permissions(config)
                _accessibility_trusted = _permission_status.accessibility
                _input_monitoring_trusted = _permission_status.input_monitoring
                if _permission_status.config_changed:
                    save_config(config)
                if _accessibility_trusted is False or _input_monitoring_trusted is False:
                    print(
                        "[Permissions] Allow Wayfinder Aura in System Settings > "
                        "Privacy & Security > Accessibility and Input Monitoring, "
                        "then restart the app.",
                        flush=True,
                    )
                elif _accessibility_trusted is None or _input_monitoring_trusted is None:
                    print(
                        "[Permissions] Could not query macOS input permissions; "
                        "global hotkeys and paste may require manual approval.",
                        flush=True,
                    )
            except Exception as _permission_error:
                print(
                    f"[Permissions] macOS permission check failed: {_permission_error}",
                    flush=True,
                )
        # macOS startup is offline-first: a stale-token refresh happens after
        # first paint so a disconnected Mac never bounces for ten seconds before
        # UI. Until that refresh runs, don't persist repairs (they reset Ultra
        # settings) from a token that merely needed refreshing. Linux/Windows
        # keep refreshing here, before the repair, as on main.
        _startup_gate = get_feature_gate(refresh_online=sys.platform != "darwin")
        _entitlement_repairs = (
            []
            if _startup_gate.refresh_pending
            else enforce_license_config(config, _startup_gate)
        )
        if _entitlement_repairs:
            save_config(config)
            print(
                "[license] Repaired unavailable settings before runtime setup: "
                + ", ".join(_entitlement_repairs)
            )
        setup_gpu_environment(config)
    except Exception as e:
        print(f"[GPU] Warning: Could not setup GPU environment: {e}")

    # Pre-warm clipboard daemon on macOS (best-effort, non-blocking)
    try:
        from wayfinder.core.injector import warmup_clipboard
        warmup_clipboard()
    except Exception:
        pass

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

        if sys.platform == "darwin":
            def _graceful_macos_signal(_signum, _frame):
                app.quit_app()

            signal.signal(signal.SIGTERM, _graceful_macos_signal)
            signal.signal(signal.SIGINT, _graceful_macos_signal)
        
        # ─── First-run flow: dependency setup (inline pane) → welcome tour ───
        # The dependency setup is now an IN-WINDOW pane (src/wayfinder/ui/setup_pane.py)
        # placed over the tab content — no more modal SetupWizard CTkToplevel /
        # grab_set (CLAUDE.md rule 2). All app subsystems (hotkey listener, warm
        # mic, overlay) already start in WayfinderApp.__init__ regardless of the
        # wizard, so nothing here gates them — the old wizard only blocked the
        # mainloop start via wait_window(), which the inline pane doesn't need.
        #
        # Frozen (.app) builds bundle every dependency AND crash on the Linux
        # package-manager probes, so setup is force-completed there; the welcome
        # tour (pure UI, safe on every platform) still runs.
        try:
            from wayfinder.ui.setup_pane import first_run_plan, should_chain_welcome

            frozen = getattr(sys, 'frozen', False)
            frozen_runtime_ready = True
            if frozen and sys.platform == "darwin":
                # Native dependencies are bundled, but speech-model weights are
                # intentionally downloaded after install. Do not send a clean
                # Mac into the live-dictation Welcome step until one exists.
                frozen_runtime_ready = app._has_usable_whisper_model()
                # Keep a genuinely clean install marked incomplete until the
                # required model exists. Existing packaged users who already
                # completed setup retain their marker if a model is later moved.
                if frozen_runtime_ready:
                    app.config["setup_completed"] = True
                save_config(app.config)
            elif frozen:
                # Linux/Windows frozen builds (as on main): keep the flag in
                # WayfinderApp's live config so later save_config() calls persist
                # it (frozen builds never run the setup pane).
                app.config["setup_completed"] = True

            plan = first_run_plan(
                setup_completed=app.config.get("setup_completed", False),
                welcome_completed=app.config.get("welcome_completed", False),
                frozen=frozen,
            )
            if frozen and not frozen_runtime_ready:
                # The packaged onboarding owns the required free Base-model
                # download as its first step. A user who previously skipped the
                # tour still gets the standalone model panel on later launches.
                plan = {
                    "show_setup": False,
                    "show_welcome": not app.config.get("welcome_completed", False),
                }

            def _after_setup(result: bool) -> None:
                # Preserves the old wizard.result logging, then hands off to the
                # welcome tour (delayed so the setup pane has fully torn down).
                print("[Setup] Setup completed successfully" if result
                      else "[Setup] Setup skipped")
                if should_chain_welcome(app.config.get("welcome_completed", False)):
                    app.after(400, app.show_welcome_pane)

            if (
                frozen
                and not frozen_runtime_ready
                and app.config.get("welcome_completed", False)
            ):
                app.after(500, app.show_first_run_model_setup)
            elif plan["show_setup"]:
                # Delayed so the window is mapped before the pane is placed over
                # the tab content (mirrors the welcome-pane trigger).
                app.after(300, lambda: app.show_setup_pane(on_done=_after_setup))
            elif plan["show_welcome"]:
                app.after(800, app.show_welcome_pane)
        except Exception as e:
            print(f"[Setup] Warning: Could not start first-run flow: {e}")
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
        # Aqua's application menu can end the Tcl loop without delivering a
        # window-close protocol on older Tk builds. Keep cleanup authoritative.
        if sys.platform == "darwin":
            app.quit_app()
        
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


# flock is released automatically when _INSTANCE_LOCK_FD is closed on process exit.


if __name__ == "__main__":
    main()
