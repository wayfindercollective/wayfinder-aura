# Wayfinder Voice - Flatpak & Monetization Strategy

## Part 1: Flatpak Distribution

### Option A: Flathub (Free/Open)
- **Pros**: Massive reach, automatic updates, trusted by users
- **Cons**: Must be open source, can't easily monetize
- **Best for**: Free tier, building user base

### Option B: Self-Hosted Flatpak Repo
- **Pros**: Full control, can include license checks, premium features
- **Cons**: Users must add your repo manually, less discovery
- **Best for**: Premium version

### Option C: Direct Download (.flatpak bundle)
- **Pros**: Simple, works anywhere, no repo needed
- **Cons**: No auto-updates, manual install
- **Best for**: One-time purchase model

### Recommended Approach: Hybrid
1. **Flathub**: Free version with basic features (builds your brand)
2. **Your website**: Premium `.flatpak` bundle for $20 (one-time)
3. **License key**: Unlocks premium features in both versions

---

## Part 2: Free vs Premium Features

### 🆓 FREE TIER (Flathub)
| Feature | Details |
|---------|---------|
| Basic Transcription | whisper.cpp CPU only |
| Models | Tiny.en, Base.en, Small.en |
| Recording | Standard (non-chunked) |
| Audio Processing | Light only |
| Beam Search | 1-3 (fast mode) |
| Typing Speed | Instant only |
| UI | Full UI, all settings visible |

### 💎 PREMIUM TIER ($20 one-time)
| Feature | Value Proposition |
|---------|-------------------|
| **GPU Acceleration** | 3-10x faster transcription |
| **Faster-Whisper Backend** | Best GPU utilization |
| **Large Models** | Medium.en, Large v3 Turbo |
| **Chunked Recording** | Unlimited duration |
| **Advanced Audio** | Medium & Heavy preprocessing |
| **High Beam Search** | 4-10 (accuracy mode) |
| **All Typing Speeds** | Fast, Normal, Slow, Very Slow |
| **Custom Vocabulary** | Add your own terms |
| **Priority Support** | Email support for 1 year |

### Why This Split Works
- Free tier is **genuinely useful** (not crippled)
- Premium features are **professional/power-user** focused
- GPU acceleration alone justifies $20 (saves hours of waiting)
- One-time purchase matches your "no subscription" brand

---

## Part 3: License Key Implementation

### Architecture
```
┌─────────────────────────────────────────────────────────┐
│                    License System                        │
├─────────────────────────────────────────────────────────┤
│  1. User purchases on website (Gumroad/LemonSqueezy)    │
│  2. Receives license key: WV-XXXX-XXXX-XXXX-XXXX        │
│  3. Enters key in app → stored in config                │
│  4. App validates key cryptographically (offline!)      │
│  5. Premium features unlocked                           │
└─────────────────────────────────────────────────────────┘
```

### Key Features
- **Offline validation** - No phone home (matches privacy brand)
- **Machine binding** - Optional, prevents casual sharing
- **Cryptographic** - Can't easily generate fake keys
- **Graceful degradation** - Invalid key = free tier, not broken app

### Payment Platforms for One-Time Purchases
1. **Gumroad** - Simple, handles taxes, 10% fee
2. **LemonSqueezy** - Modern, EU-friendly, 5% + $0.50
3. **Paddle** - Enterprise, handles everything, 5-10%
4. **Ko-fi Shop** - Creator-friendly, 0% on paid plans

---

## Part 4: Implementation Checklist

### Phase 1: Flatpak Working (This Week)
- [ ] Fix python-deps to use offline sources
- [ ] Test local Flatpak build
- [ ] Create proper App ID (io.github.YOURUSERNAME.WayfinderVoice)
- [ ] Take screenshots for listing
- [ ] Submit free version to Flathub

### Phase 2: License System (Next Week)
- [ ] Create license.py module
- [ ] Add license key entry in Settings
- [ ] Gate premium features behind license check
- [ ] Create premium build with all features
- [ ] Test license validation

### Phase 3: Sales Infrastructure
- [ ] Set up payment platform (Gumroad recommended to start)
- [ ] Create landing page with purchase button
- [ ] Set up license key delivery (automatic email)
- [ ] Create premium Flatpak bundle

### Phase 4: Launch
- [ ] Announce free version on Flathub
- [ ] Blog post / Reddit / Hacker News
- [ ] Offer launch discount ($15 first week)
- [ ] Collect testimonials

---

## Part 5: Pricing Psychology

### Why $20 is the Sweet Spot
- **Below impulse threshold** - Many devs will pay without much thought
- **Above "throwaway"** - Signals quality, not abandonware
- **One hour of work** - Easy ROI justification for professionals
- **Matches market** - Similar to other premium Linux tools

### Alternative Pricing Models
| Model | Price | Pros | Cons |
|-------|-------|------|------|
| One-time | $20 | Simple, matches brand | No recurring revenue |
| Yearly | $12/yr | Recurring revenue | Contradicts "no subscription" |
| Pay-what-you-want | $5-50 | Good PR | Lower average |
| Donations only | $0+ | Maximum reach | Minimal income |

**Recommendation**: Stick with $20 one-time. It matches your "no subscription" messaging perfectly.

---

## Part 6: Marketing Angles

### For Premium Pitch
> "The free version is great for quick notes. But if you're dictating all day—blog posts, documentation, emails—GPU acceleration pays for itself in the first hour."

### Trust Builders
- "100% offline license validation"
- "No DRM, no phone home"
- "Works forever, even if we disappear"
- "30-day money-back guarantee"

### Comparison Hook
> "Otter.ai: $100/year. Google Cloud Speech: $0.006/15 seconds. Wayfinder Voice Premium: $20 once, forever, private."






