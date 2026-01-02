# Migration Status

This document tracks the migration from the monolithic `wayfinder_main.py` to the modular package structure.

## Current Status

**Phase 1 Complete**: The package structure is fully in place. Root-level Python files are now thin
re-export shims that import from the package. This allows `wayfinder_main.py` to continue working
with its existing imports while the canonical source of truth is in `src/wayfinder/`.

## Root Files (Shims)

These files now re-export from the package for backward compatibility with `wayfinder_main.py`:

| File | Type | Package Location |
|------|------|------------------|
| `recorder.py` | Shim | `src/wayfinder/core/recorder.py` |
| `transcriber.py` | Shim | `src/wayfinder/core/transcriber.py` |
| `injector.py` | Shim | `src/wayfinder/core/injector.py` |
| `postprocessor.py` | Shim | `src/wayfinder/core/postprocessor.py` |
| `ollama_manager.py` | Shim | `src/wayfinder/core/ollama_manager.py` |
| `license.py` | Shim | `src/wayfinder/license.py` |
| `status_overlay.py` | Original | `src/wayfinder/ui/overlay.py` |
| `wayfinder_main.py` | Legacy main | To be broken into modules |

## Remaining Migration Tasks

### Phase 2: Extract Dialogs from wayfinder_main.py

The following can be extracted to `src/wayfinder/ui/dialogs/`:
- Settings dialog → `settings.py`
- Hotkey dialog → `hotkey.py`
- Model download dialog → `model_download.py`
- Ollama setup wizard → `ollama_setup.py`
- Benchmark dialog → `benchmark.py`
- License dialog → `license.py`

### Phase 3: Extract Main Window and Tray

- Main window layout → `src/wayfinder/ui/main_window.py`
- System tray → `src/wayfinder/ui/tray.py`

### Phase 4: Slim Down wayfinder_main.py

After all extractions, `wayfinder_main.py` should contain only:
- WayfinderApp class (coordinating all components)
- Event loop and state management

## For AI Agents

When making changes:
1. **Prefer the package modules** in `src/wayfinder/` for new code
2. **Keep root files in sync** if you modify package modules (for now)
3. **Test both import paths** until migration is complete

```bash
# Test package imports
PYTHONPATH=src python -c "from wayfinder.core import AudioRecorder; print('OK')"

# Test root imports (used by wayfinder_main.py)
python -c "from recorder import AudioRecorder; print('OK')"
```




