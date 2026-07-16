# How to submit Wayfinder Aura to Flathub

Discover installs from **Flathub**. Packaging for **v1.1.2** is ready; a **human** must open the submission PR (Flathub AI policy).

## Ready pack (this machine)

```text
/var/home/bazzite/Dev/wayfinder-aura/.tmp-flathub-handoff/submission/
  io.wayfindercollective.WayfinderAura.yml
  python-deps.json
  flathub.json
```

Step-by-step commands + policy notes:  
**`.tmp-flathub-handoff/HUMAN-SUBMIT.md`**

Facts to paraphrase into the PR body:  
**`.tmp-flathub-handoff/FACTS-CHECKLIST.md`**

Engineering context / remaining technical follow-ups:  
**`docs/FLATHUB-LAUNCH-HANDOFF-2026-07-16.md`**

Upstream release: https://github.com/wayfindercollective/wayfinder-aura/releases/tag/v1.1.2  

## Short path

1. (Recommended) Request Flathub generative-AI exception (human-written).  
2. Fork `flathub/flathub`, branch from **`new-pr`**.  
3. Copy the three files above into the **root** of that branch.  
4. Open PR against base **`new-pr`**, title: `Add io.wayfindercollective.WayfinderAura`.  
5. Write the description yourself from the facts checklist.  
6. Answer reviewers; `bot, build` when ready.  
7. After merge + publish: `flatpak install flathub io.wayfindercollective.WayfinderAura`.

Official guide: https://docs.flathub.org/docs/for-app-authors/submission  
