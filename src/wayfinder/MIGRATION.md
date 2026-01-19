# Migration Status

This document tracks the migration from the monolithic `wayfinder_main.py` to the modular package structure.

> **For AI Agents**: See [../../AGENTS.md](../../AGENTS.md) for comprehensive technical documentation.

## Current Status

**Phase 1.5 Complete**: Root-level shim files have been removed. All imports in `wayfinder_main.py`
now use the `wayfinder.*` package paths directly. The canonical source of truth is `src/wayfinder/`.

## Root Files Status

| File | Status | Notes |
|------|--------|-------|
| `recorder.py` | **DELETED** | Import from `wayfinder.core.recorder` |
| `transcriber.py` | **DELETED** | Import from `wayfinder.core.transcriber` |
| `injector.py` | **DELETED** | Import from `wayfinder.core.injector` |
| `postprocessor.py` | **DELETED** | Import from `wayfinder.core.postprocessor` |
| `ollama_manager.py` | **DELETED** | Import from `wayfinder.core.ollama_manager` |
| `license.py` | **DELETED** | Import from `wayfinder.license` |
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
1. **Always edit** files in `src/wayfinder/` for core functionality
2. **Use package imports** like `from wayfinder.core import AudioRecorder`
3. **UI changes** still go in `wayfinder_main.py` until dialogs are extracted

```bash
# Test package imports
PYTHONPATH=src python -c "from wayfinder.core import AudioRecorder; print('OK')"

# Run tests
pytest tests/
```




