# Wayfinder Voice - Website Copy Brief

## Product Summary

**Wayfinder Voice** is a premium, local-first voice dictation application for Linux that transforms spoken words into typed text anywhere on your screen. Unlike cloud-based alternatives, everything runs locally on your machine using state-of-the-art AI models.

---

## Core Value Propositions

### 1. 🔒 100% Private & Local
- **Zero cloud dependency** - All AI processing happens on your machine
- **No internet required** - Works offline, no data ever leaves your computer
- **No subscription fees** - One-time setup, runs forever
- Your voice data is never uploaded, stored, or analyzed by third parties

### 2. ⚡ Performance Optimized
- **GPU acceleration** - Vulkan, CUDA, and ROCm support for 3-10x faster transcription
- **Monitor-synced animations** - Silky smooth 60Hz/144Hz/240Hz visual feedback
- **Chunked recording** - Unlimited recording duration with real-time transcription
- **Intelligent audio preprocessing** - Gain normalization, high-pass filtering, noise gating

### 3. 🎯 Works Everywhere
- **Universal text injection** - Types directly into any app (browser, IDE, chat, documents)
- **Wayland & X11 native** - Full support for modern Linux desktops
- **Multiple input devices** - Trigger from keyboard, gaming mouse, keypad, or foot pedal
- **System tray integration** - Runs silently in background until needed

---

## Key Features Breakdown

### Voice Recording & Transcription

| Feature | Description | Benefit |
|---------|-------------|---------|
| **Hotkey Triggered** | Press F3 (or any key combo) to start/stop | Instant access from any application |
| **Real-time Audio Levels** | Voice-reactive waveform visualization | Visual confirmation you're being heard |
| **Chunked Recording** | 30-second segments with overlap | Unlimited recording duration, faster feedback |
| **Auto-Preprocessing** | Light/Medium/Heavy audio cleanup | Better accuracy in noisy environments |
| **16kHz Resampling** | Automatic conversion from any mic | Optimal quality for Whisper AI |

### AI Transcription Engine

| Feature | Description | Performance |
|---------|-------------|-------------|
| **whisper.cpp Backend** | C++ optimized Whisper implementation | Low memory, fast CPU inference |
| **Faster-Whisper Backend** | CTranslate2 optimized inference | Best GPU utilization |
| **Model Selection** | Tiny → Large v3 Turbo | 0.5s to 6s per transcription |
| **Beam Search Tuning** | 1-10 beam width | Speed vs accuracy tradeoff |
| **Custom Vocabulary** | Add names, jargon, terms | Better recognition of your words |
| **Context Continuity** | Previous chunk feeds next | Coherent long-form dictation |

### Text Output

| Feature | Description | Options |
|---------|-------------|---------|
| **Instant Paste** | Clipboard injection | 0ms latency |
| **Simulated Typing** | Character-by-character | Fast/Normal/Slow speeds |
| **Auto-Punctuation** | Capitalizes, adds periods | Clean, ready-to-use text |
| **Universal Injection** | ydotool/xdotool/wtype | Works in any Linux app |

---

## Visual Feedback System

### Floating Indicator (During Recording)
- **Pulsing status dot** - Breathing animation shows recording state
- **Voice-reactive waveform** - Multi-layer sine wave responds to your voice
- **Color-coded states** - Red=Recording, Cyan=Processing, Green=Ready
- **Monitor refresh sync** - Buttery smooth at any refresh rate (60-240Hz)

### Main Window Waveform
- **Hero visualization** - Large, dramatic waveform in the dictate tab
- **Idle breathing** - Gentle animation when ready
- **Audio-reactive amplitude** - Responds instantly to voice input
- **Premium easing** - Smooth sinusoidal motion, no jitter

### Smooth Scrolling
- **Momentum physics** - Mac-like scroll inertia
- **Elastic bounce** - Rubber-band effect at scroll boundaries
- **60fps+ rendering** - Matches monitor refresh rate

---

## Performance Specifications

### Transcription Speed (30 seconds of audio)

| Model | GPU Time | CPU Time | Accuracy |
|-------|----------|----------|----------|
| Tiny.en | ~0.5s | ~2s | Good |
| Base.en | ~1s | ~4s | Better |
| Small.en | ~1.5s | ~6s | Great |
| Medium.en | ~3s | ~12s | Excellent |
| **Large v3 Turbo** ⭐ | **~2s** | **~8s** | **Best** |

### Latency Breakdown

| Component | Impact | Notes |
|-----------|--------|-------|
| Audio preprocessing | +2-10ms | Configurable levels |
| Chunked mode overhead | +50-100ms | Per segment boundary |
| Beam search (5) | Baseline | Good balance |
| Beam search (1) | -50% time | Fast mode |
| GPU acceleration | -70-90% time | Massive speedup |

---

## Technical Differentiators

### Why whisper.cpp?
- Written in C/C++ for maximum performance
- Vulkan GPU support works on AMD, NVIDIA, Intel
- Quantized models (Q5) for smaller downloads
- Active development, latest Whisper features

### Why Local Processing?
- **Privacy**: Medical, legal, personal content stays private
- **Reliability**: No internet? No problem
- **Speed**: No upload/download latency
- **Cost**: No per-minute charges

### Why Linux Native?
- First-class Wayland support (including KDE Plasma)
- Direct evdev hotkey detection
- Native system tray integration
- Works with PipeWire, PulseAudio, ALSA

---

## User Experience Highlights

### Onboarding
- One-click model downloads from Hugging Face
- Auto-detection of GPU capabilities
- Smart defaults that "just work"
- Comprehensive tooltips with latency indicators

### Daily Use
- Start minimized to tray
- Global hotkey works from any app
- Visual feedback confirms recording
- Text appears where your cursor is

### Customization
- UI scaling (70% - 250%) for any display
- Adjustable typing speed
- Custom prompts and vocabulary
- Multiple transcription backends

---

## Target Audiences

### Power Users
- Developers dictating code comments/documentation
- Writers doing first drafts
- Researchers transcribing interviews

### Privacy-Conscious Users
- Medical professionals (HIPAA considerations)
- Legal professionals (confidential notes)
- Anyone who values data sovereignty

### Linux Enthusiasts
- KDE Plasma users wanting native feel
- Wayland early adopters
- Users tired of cloud-dependent tools

---

## Competitive Advantages

| vs Cloud Services | vs Other Local Tools |
|-------------------|---------------------|
| ✅ No subscription | ✅ Modern UI (CustomTkinter) |
| ✅ Works offline | ✅ Wayland native support |
| ✅ Zero latency upload | ✅ GPU acceleration |
| ✅ Complete privacy | ✅ Monitor-synced animations |
| ✅ No usage limits | ✅ Chunked unlimited recording |
| ✅ One-time setup | ✅ Premium visual feedback |

---

## Technical Stack

- **UI Framework**: CustomTkinter (modern dark theme)
- **Audio**: sounddevice + scipy (real-time capture & processing)
- **AI**: whisper.cpp / Faster-Whisper (state-of-the-art speech recognition)
- **Text Injection**: ydotool/xdotool/wtype (universal compatibility)
- **System**: pystray (tray), evdev (hotkeys), dbus (Wayland portals)

---

## Messaging Themes

### Hero Statement Options
1. "Your voice, your machine, your privacy."
2. "Local AI dictation that feels instant."
3. "Type with your voice. No cloud required."
4. "Premium voice dictation for Linux power users."

### Key Phrases
- "Buttery smooth animations"
- "Monitor-synced refresh rates"
- "Zero-latency local processing"
- "Privacy by design"
- "Works everywhere you type"
- "Unlimited recording duration"
- "GPU-accelerated transcription"

### Pain Points Addressed
- "Tired of cloud subscriptions eating your budget?"
- "Worried about sensitive voice data leaving your machine?"
- "Frustrated by dictation tools that don't work on Linux?"
- "Want visual feedback that doesn't feel like 1999?"

---

## Call-to-Action Ideas

- "Download and dictate in minutes"
- "Get started with one command"
- "Try the Large v3 Turbo model - best accuracy, great speed"
- "Join the privacy-first voice revolution"

---

## Assets Needed for Website

1. **Screenshots**
   - Main dictate view with waveform
   - Settings panel
   - Model download dialog
   - Floating indicator during recording

2. **Animations/GIFs**
   - Voice-reactive waveform demo
   - Smooth scrolling showcase
   - Recording → Processing → Typing flow

3. **Comparison Charts**
   - Model speed/accuracy table
   - vs cloud services feature matrix

4. **Code Snippets**
   - Installation commands
   - Configuration examples

