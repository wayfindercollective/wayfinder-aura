# Wayfinder Aura - Ubuntu Installation Guide (NVIDIA GPU)

Quick setup guide for running Wayfinder Aura on Ubuntu with an NVIDIA GPU.

> GPU acceleration and every speech model other than Base/Base.en are Ultra
> features. Free runs Base/Base.en on CPU.

---

## 1. Run the AppImage

```bash
# Make it executable and run (the file name carries the version)
chmod +x Wayfinder_Aura-*-x86_64.AppImage
./Wayfinder_Aura-*-x86_64.AppImage
```

The app will launch, but you need the dependencies below for full functionality.

---

## 2. Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Audio (required for recording)
sudo apt install -y portaudio19-dev python3-dev

# Add yourself to the input group (for the hotkey listener, not for typing)
sudo usermod -aG input $USER
# NOTE: Log out and back in for group change to take effect
```

### Typing text at the cursor

- **Wayland session (Ubuntu's default GNOME, or KDE Plasma):** nothing to
  install. Aura types through the desktop's RemoteDesktop portal after you
  approve its prompt once (verified on KDE Plasma; GNOME 45+, which Ubuntu
  24.04 ships, is expected to work the same way but is not yet verified).
- **X11 session:** install xdotool. The AppImage bundles only `wtype`, not
  xdotool.
- **ydotool is not an option on Ubuntu 22.04/24.04:** their `ydotool` package
  is version 0.1.8, which Aura cannot use (it needs 1.x, packaged from Ubuntu
  26.04). Use the portal or xdotool instead.

```bash
# X11 sessions
sudo apt install -y xdotool
```

---

## 3. NVIDIA GPU Setup

### Install NVIDIA Drivers

Ubuntu makes this easy:

```bash
# Option A: Ubuntu driver manager (recommended)
sudo ubuntu-drivers install

# Option B: Specific driver version
sudo apt install -y nvidia-driver-550
```

Reboot after installing drivers:
```bash
sudo reboot
```

Verify drivers are working:
```bash
nvidia-smi
# Should show your RTX 3090 with driver version and CUDA version
```

### Install CUDA Toolkit

whisper.cpp needs CUDA to use your GPU:

```bash
# Install CUDA toolkit (compiler + libraries)
sudo apt install -y nvidia-cuda-toolkit

# Verify
nvcc --version
```

---

## 4. Build whisper.cpp with CUDA

This is the speech-to-text engine. Building from source with CUDA gives you GPU acceleration.

```bash
# Clone whisper.cpp
cd ~
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp

# Build with CUDA support (for NVIDIA GPUs)
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j$(nproc)

# Verify it built correctly
./build/bin/whisper-cli --help
# Should mention GPU-related options
```

### Download a Whisper Model

```bash
# Download the recommended model (large-v3-turbo: best speed/accuracy balance)
cd ~/whisper.cpp/models
bash download-ggml-model.sh large-v3-turbo

# The model file will be at:
# ~/whisper.cpp/models/ggml-large-v3-turbo.bin (~1.6 GB)
```

**Model options** (pick based on preference):

| Model | Size | Speed | Accuracy | Best For |
|-------|------|-------|----------|----------|
| `tiny.en` | 75 MB | Fastest | Basic | Quick testing |
| `base.en` | 142 MB | Fast | Good | Light use |
| `small.en` | 466 MB | Medium | Great | Daily use |
| `large-v3-turbo` | 1.6 GB | Fast (GPU) | Excellent | **Recommended with 3090** |
| `large-v3` | 3.1 GB | Slower | Best | Maximum accuracy |

With a 3090 (24GB VRAM), you can easily run `large-v3-turbo` or even `large-v3`.
Free installs are limited to Base/Base.en on CPU: GPU acceleration and every
other speech model above are Ultra.

---

## 5. (Optional) Set Up LLM Post-Processing

Wayfinder can clean up transcriptions using a local LLM (fixes filler words, punctuation, formatting). This is optional but recommended.

### Option A: Local LLM with llama.cpp (Private, Free)

```bash
# llama-cpp-python is bundled with the app, but for GPU acceleration:
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124

# Download a small, fast model (Gemma 3 1B is the recommended default)
# Visit: https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF
# Download the Q4_K_M variant (~806 MB)
# Roomier alternative: https://huggingface.co/unsloth/Qwen3.5-2B-GGUF (1.3 GB)
mkdir -p ~/.local/share/wayfinder-aura/llm-models
# Place the .gguf file in the directory above
```

### Option B: Cloud API (Easier Setup)

If you have API keys, you can use cloud LLMs instead:
- **OpenAI**: Set `OPENAI_API_KEY` environment variable
- **Anthropic**: Set `ANTHROPIC_API_KEY` environment variable

Configure the backend in the app's Settings tab.

---

## 6. Configure Wayfinder Aura

Aura finds whisper.cpp and your models on its own: by default
`~/whisper.cpp/build/bin/whisper-cli` (unless the AppImage bundles its own) and
the models in `~/whisper.cpp/models/`. Settings has no fields for these paths;
if yours live elsewhere, set `whisper_binary` and `model_path` in
`~/.config/wayfinder-aura/config.json`.

Launch the app and go to Settings:

1. **Whisper Model** (Settings → Processing Mode): open it to pick the model you
   downloaded. Models above Base are Ultra.
2. **GPU Acceleration**: Ultra. With a license, turn it on (should auto-detect
   your 3090).
3. **Hotkey**: Default is Ctrl+Alt+Space to start/stop recording.

---

## 7. Test It

1. Open any text editor or browser
2. Press Ctrl+Alt+Space to start recording
3. Speak naturally
4. Press Ctrl+Alt+Space again to stop
5. Your transcribed text appears at the cursor

---

## Troubleshooting

### "ydotool: Permission denied" (only if you use the ydotool fallback)
```bash
# Make sure ydotoold is running
sudo systemctl status ydotoold

# Make sure you're in the input group
groups $USER
# Should include "input" - if not, re-login after usermod
```

### "whisper-cli: GPU not detected"
```bash
# Verify CUDA is working
nvidia-smi

# Verify whisper.cpp was built with CUDA
~/whisper.cpp/build/bin/whisper-cli --help 2>&1 | grep -i gpu
# Should show --no-gpu flag (meaning GPU is ON by default)

# If not, rebuild:
cd ~/whisper.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j$(nproc)
```

### "No audio device found"
```bash
# List audio devices
arecord -l

# Install PulseAudio/PipeWire utilities
sudo apt install -y pavucontrol
# Open pavucontrol and make sure your mic is set as default input
```

### App won't start (library errors)
```bash
# Install common missing libraries
sudo apt install -y libxcb-xinerama0 libxcb-cursor0 libgl1-mesa-glx
sudo apt install -y libfuse2  # Required for AppImage on Ubuntu 22.04+
```

---

## Quick Reference

| What | Where |
|------|-------|
| App config | `~/.config/wayfinder-aura/config.json` |
| whisper.cpp | `~/whisper.cpp/build/bin/whisper-cli` |
| Models | `~/whisper.cpp/models/` |
| ydotool socket (fallback only) | `/run/ydotool/ydotool.sock` |
| Hotkey | Ctrl+Alt+Space (toggle recording) |
| Style toggle | Ctrl+Alt+Enter (cycle output styles) |
