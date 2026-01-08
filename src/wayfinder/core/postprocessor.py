"""
Post-processing module for Wayfinder Aura.
Cleans up transcription output using LLM backends (local or cloud).
Supports llama-cpp-python for local inference and Anthropic Claude for cloud.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict, Any


class PostProcessingError(Exception):
    """Raised when post-processing fails."""
    pass


# =============================================================================
# Simplified Style System (5 Presets)
# =============================================================================
#
# The new system has 5 presets that cycle via hotkey:
#   1. Minimal - Universal cleanup, just removes um/uh/ah (no style applied)
#   2. Professional - Clean + business-appropriate tone
#   3. Casual - Clean + relaxed texting style  
#   4. AI Prompt - Clean + formatted for AI assistants
#   5. Personal - Clean + user's learned speech patterns
#
# Each styled preset (2-5) has two intensities:
#   - Standard: Cleans up speech, applies style lightly, KEEPS user's words
#   - Strong: Full transformation, may restructure sentences
#
# "Minimal" is always the same - just filler sound removal.

# =============================================================================
# Tone Guidance (Style-specific instructions for the LLM)
# =============================================================================

TONE_GUIDANCE: Dict[str, Dict[str, str]] = {
    "minimal": {
        # Minimal is the same regardless of intensity - just cleanup
        "standard": "Keep the exact phrasing and sentence structure. Only fix obvious transcription errors.",
        "strong": "Keep the exact phrasing and sentence structure. Only fix obvious transcription errors.",
        "caricature": "Add MORE filler words and verbal tics. Make it sound like a nervous person speaking. Add [clears throat], [sighs], [ummmm], [long pause] annotations.",
    },
    "professional": {
        "standard": "Keep the user's words and sentence structure. Make it sound professional with proper grammar.",
        "strong": "Rewrite for executive clarity. Be concise, formal, and polished. Remove fluff.",
        "caricature": "MAXIMUM CORPORATE BUZZWORDS. Synergize everything. Circle back constantly. Leverage core competencies. Move the needle. Touch base offline. Make it sound like a parody of a LinkedIn influencer or management consultant.",
    },
    "casual": {
        "standard": "Keep the user's words. Make it sound natural and conversational.",
        "strong": "Rewrite as casual texting. Short, relaxed, like messaging a friend.",
        "caricature": "EXTREME internet/Gen-Z speak. Use 'fr fr', 'no cap', 'lowkey', 'highkey', 'bussin', 'ong', 'slay', 'its giving', 'understood the assignment'. Add skull emojis 💀. Make it sound like a parody of chronically online speech.",
    },
    "dev": {
        "standard": "Keep the user's words. This is developer/coding context - recognize git commands (pull, push, commit, merge, branch, main, dev), programming terms, and technical jargon.",
        "strong": "Format as a clear developer request or prompt. Recognize technical terms, file paths, function names, and git terminology.",
        "caricature": "OVER-ENGINEERED PROMPT. Add excessive context like 'You are a world-renowned expert with 47 years of experience'. Include 'Think step by step', 'Take a deep breath', 'I'll tip you $500', 'My career depends on this'. Make it a parody of prompt engineering.",
    },
    "personal": {
        # Personal style uses voice profile - these are fallbacks when no profile exists
        "standard": "Keep the user's characteristic phrases and word choices. Just clean up delivery.",
        "strong": "Polish while preserving the user's unique voice and speaking patterns.",
        "caricature": "EXAGGERATE the user's speech patterns to absurd levels. If they use filler words, triple them. If they have verbal tics, amplify them. Make it a loving parody of how they speak.",
    },
}

# =============================================================================
# Formatting Rules (Punctuation & Structure)
# =============================================================================

FORMATTING_RULES: Dict[str, Dict[str, str]] = {
    "minimal": {
        "standard": "Keep natural punctuation exactly as transcribed.",
        "strong": "Keep natural punctuation exactly as transcribed.",
        "caricature": "Add dramatic punctuation... lots of ellipses... and [annotations] for [awkward pauses] and [nervous laughter].",
    },
    "professional": {
        "standard": "Use proper punctuation and capitalization.",
        "strong": "Use proper punctuation. Structure with clear paragraphs if needed.",
        "caricature": "Use EXCESSIVE capitalization for EMPHASIS. Add bullet points and sub-bullets everywhere. Sign off with corporate platitudes.",
    },
    "casual": {
        "standard": "Relaxed punctuation. Periods optional at end of sentences.",
        "strong": "No periods. Text message style. Lowercase is fine. Only use ? when asking.",
        "caricature": "all lowercase. no punctuation except for excessive question marks??? and exclamation marks!!! add emojis freely 💀😭🔥",
    },
    "dev": {
        "standard": "Use clear punctuation. Preserve technical terms exactly (git commands, file paths, function names).",
        "strong": "Use clear punctuation. Add structure (bullets, code blocks) if it helps clarity. Preserve all technical terminology.",
        "caricature": "Add XML-style tags like <context>, <task>, <constraints>. Number everything. Add a [CRITICAL] and [IMPORTANT] section.",
    },
    "personal": {
        "standard": "Match the user's typical punctuation habits.",
        "strong": "Clean punctuation while keeping the user's style.",
        "caricature": "Exaggerate punctuation quirks. If they use lots of commas, use MORE. Add their verbal tics as written words.",
    },
}

# =============================================================================
# Filler Word Rules (What to remove)
# =============================================================================

FILLER_RULES: Dict[str, str] = {
    "minimal": "Remove ONLY filler sounds: um, uh, ah, er. Keep everything else including 'like', 'you know', 'basically'.",
    "standard": "Remove filler words: um, uh, ah, like, you know. Keep the user's sentence structure.",
    "strong": "Remove all filler words: um, uh, ah, like, you know, basically, actually, I mean, so, well.",
    "caricature": "Keep ALL filler words and ADD MORE for comedic effect. Exaggerate hesitation and verbal tics.",
}


# =============================================================================
# Model Compatibility System
# =============================================================================
# Defines model tiers and known issues to provide better UX

# Model size tiers based on parameter count
MODEL_TIERS = {
    "tiny": {  # < 500M params
        "description": "Very small models, limited instruction following",
        "max_intensity": "light",  # Recommend light intensity max
        "smart_formatting": False,  # Disable smart formatting
        "patterns": ["360m", "135m", "tiny", "0.5b"],
    },
    "small": {  # 500M - 2B params
        "description": "Small models, good for basic cleanup",
        "max_intensity": "standard",
        "smart_formatting": True,  # Works but may have quirks
        "patterns": ["1b", "1.5b", "2b", "small", "mini"],
    },
    "standard": {  # 2B - 7B params
        "description": "Standard models, full capability",
        "max_intensity": "strong",
        "smart_formatting": True,
        "patterns": ["3b", "4b", "7b", "8b", "medium"],
    },
    "large": {  # 7B+ params
        "description": "Large models, best quality",
        "max_intensity": "strong",
        "smart_formatting": True,
        "patterns": ["13b", "14b", "32b", "70b", "large", "gpt-4", "claude"],
    },
}

# Known model-specific issues
MODEL_QUIRKS: Dict[str, Dict[str, Any]] = {
    "llama3.2:1b": {
        "issues": ["safety_filter_email"],
        "workaround": "Disable smart_formatting for professional/dev modes",
        "avoid_words": ["email"],  # These words trigger false-positive safety
    },
    "llama3.2:3b": {
        "issues": ["safety_filter_email"],
        "workaround": "Disable smart_formatting for professional/dev modes",
        "avoid_words": ["email"],
    },
    "smollm2:360m": {
        "issues": ["weak_instruction_following"],
        "workaround": "Use simplified prompts only",
        "tier_override": "tiny",
    },
    "phi3:mini": {
        "issues": [],  # Generally works well
        "tier_override": "small",
    },
    "qwen2.5:1.5b": {
        "issues": [],  # Generally works well
        "tier_override": "small",
    },
}


def detect_model_tier(model_name: str) -> str:
    """
    Detect the capability tier of a model based on its name.
    
    Returns: "tiny", "small", "standard", or "large"
    """
    model_lower = model_name.lower()
    
    # Check for specific model overrides first
    for known_model, quirks in MODEL_QUIRKS.items():
        if known_model in model_lower:
            if "tier_override" in quirks:
                return quirks["tier_override"]
    
    # Check patterns in reverse order (largest first) to match correctly
    for tier in ["large", "standard", "small", "tiny"]:
        tier_info = MODEL_TIERS[tier]
        for pattern in tier_info["patterns"]:
            if pattern in model_lower:
                return tier
    
    # Default to small (conservative but capable)
    return "small"


def get_model_quirks(model_name: str) -> Dict[str, Any]:
    """Get known issues/quirks for a specific model."""
    model_lower = model_name.lower()
    
    for known_model, quirks in MODEL_QUIRKS.items():
        if known_model in model_lower:
            return quirks
    
    return {"issues": [], "workaround": None}


def get_model_compatibility(model_name: str, tone: str, intensity: str, smart_formatting: bool) -> Dict[str, Any]:
    """
    Check model compatibility with current settings and return recommendations.
    
    Returns dict with:
        - compatible: bool - whether settings are fully compatible
        - warnings: list[str] - any warnings to show user
        - recommendations: list[str] - suggested changes
        - auto_adjustments: dict - automatic adjustments to apply
        - upgrade_suggestion: dict - specific model upgrade recommendation
    """
    tier = detect_model_tier(model_name)
    tier_info = MODEL_TIERS[tier]
    quirks = get_model_quirks(model_name)
    
    result = {
        "compatible": True,
        "tier": tier,
        "warnings": [],
        "recommendations": [],
        "auto_adjustments": {},
        "upgrade_suggestion": None,
    }
    
    # Check intensity vs tier capability
    intensity_order = ["light", "standard", "strong"]
    max_intensity = tier_info["max_intensity"]
    if intensity_order.index(intensity) > intensity_order.index(max_intensity):
        result["compatible"] = False
        result["warnings"].append(
            f"⚠️ {model_name} may struggle with '{intensity}' intensity. "
            f"Recommended max: '{max_intensity}'"
        )
        result["recommendations"].append(f"Consider using '{max_intensity}' intensity instead")
        result["auto_adjustments"]["intensity"] = max_intensity
        
        # Add specific upgrade suggestion
        result["upgrade_suggestion"] = get_upgrade_suggestion_for_intensity(intensity)
    
    # Check smart_formatting for tiny models
    if tier == "tiny" and smart_formatting:
        result["warnings"].append(
            f"⚠️ Smart formatting may not work well with small models like {model_name}"
        )
        result["auto_adjustments"]["smart_formatting"] = False
    
    # Check for model-specific quirks
    if "safety_filter_email" in quirks.get("issues", []):
        if smart_formatting and tone in ["professional", "dev"]:
            result["warnings"].append(
                f"⚠️ {model_name} may refuse '{tone}' mode due to safety filters. "
                "Disabling smart formatting automatically."
            )
            result["auto_adjustments"]["smart_formatting"] = False
    
    if "weak_instruction_following" in quirks.get("issues", []):
        result["warnings"].append(
            f"💡 {model_name} has limited capability. Results may vary."
        )
        result["recommendations"].append("Consider upgrading to a 1B+ model for better results")
    
    return result


def get_upgrade_suggestion_for_intensity(intensity: str) -> Dict[str, Any]:
    """
    Get specific model upgrade suggestions based on desired intensity.
    
    Returns dict with:
        - min_params: str - minimum parameter count
        - recommended_models: list - specific model recommendations
        - message: str - user-friendly message
    """
    if intensity == "strong":
        return {
            "min_params": "3B+",
            "recommended_models": [
                {"name": "qwen2.5:3b", "type": "ollama", "description": "Best for strong intensity"},
                {"name": "llama3.2:3b", "type": "ollama", "description": "Good alternative"},
                {"name": "phi3:medium", "type": "ollama", "description": "Fast and capable"},
            ],
            "message": "Strong intensity works best with 3B+ parameter models. "
                       "Try: ollama pull qwen2.5:3b",
        }
    elif intensity == "standard":
        return {
            "min_params": "1B+",
            "recommended_models": [
                {"name": "qwen2.5:1.5b", "type": "ollama", "description": "Great balance"},
                {"name": "llama3.2:1b", "type": "ollama", "description": "Fast option"},
                {"name": "phi3:mini", "type": "ollama", "description": "Reliable"},
            ],
            "message": "Standard intensity works with 1B+ parameter models.",
        }
    else:  # light
        return {
            "min_params": "500M+",
            "recommended_models": [
                {"name": "smollm2:360m", "type": "ollama", "description": "Ultra fast"},
                {"name": "qwen2.5:0.5b", "type": "ollama", "description": "Compact"},
            ],
            "message": "Light intensity works with most models.",
        }


def check_settings_compatibility(config: dict) -> Dict[str, Any]:
    """
    Check if current config settings are compatible with the selected model.
    
    This is the main function for UI to call when settings change.
    Returns a complete compatibility report with actionable feedback.
    
    Args:
        config: Full configuration dictionary
        
    Returns:
        Dict with:
            - is_compatible: bool
            - issues: list of issue descriptions
            - recommendations: list of actionable recommendations  
            - upgrade_message: str or None - specific upgrade instruction
            - severity: "ok" | "warning" | "incompatible"
    """
    # Get model name based on backend
    backend = config.get("post_processing_backend", "ollama")
    if backend == "ollama":
        model_name = config.get("ollama_model", "")
    elif backend == "llama_cpp":
        model_path = config.get("llama_cpp_model_path", "")
        model_name = Path(model_path).stem if model_path else ""
    elif backend == "openai":
        model_name = config.get("openai_model", "gpt-4o-mini")
    elif backend == "anthropic":
        model_name = config.get("anthropic_model", "claude-3-haiku")
    else:
        model_name = ""
    
    # Skip check if post-processing is disabled
    if not config.get("post_processing_enabled", True):
        return {
            "is_compatible": True,
            "issues": [],
            "recommendations": [],
            "upgrade_message": None,
            "severity": "ok",
        }
    
    # Skip check if no model selected
    if not model_name:
        return {
            "is_compatible": False,
            "issues": ["No post-processing model selected"],
            "recommendations": ["Select a model in Post-Processing settings"],
            "upgrade_message": None,
            "severity": "warning",
        }
    
    # Get tone and intensity (new simplified system)
    tone = config.get("output_tone", "professional")
    use_strong = config.get("strong_mode", False)
    intensity = "strong" if use_strong else "standard"
    
    # Minimal style doesn't need compatibility check - it's just filler removal
    if tone == "minimal":
        return {
            "is_compatible": True,
            "issues": [],
            "recommendations": [],
            "upgrade_message": None,
            "severity": "ok",
            "current_model": model_name,
            "current_tier": detect_model_tier(model_name),
            "requested_intensity": "standard",
            "effective_intensity": "standard",
        }
    
    # Run compatibility check
    compat = get_model_compatibility(model_name, tone, intensity, False)
    
    # Build result
    issues = []
    recommendations = []
    upgrade_message = None
    
    if not compat["compatible"]:
        tier = compat["tier"]
        tier_info = MODEL_TIERS.get(tier, {})
        max_intensity = tier_info.get("max_intensity", "standard")
        
        issues.append(
            f"Strong mode requires a larger model"
        )
        
        if compat["upgrade_suggestion"]:
            suggestion = compat["upgrade_suggestion"]
            upgrade_message = suggestion["message"]
            recommendations.append(f"Upgrade to a {suggestion['min_params']} model")
            
            # Add specific model suggestions
            for model in suggestion["recommended_models"][:2]:
                recommendations.append(f"Try: {model['name']} ({model['description']})")
        
        recommendations.append("Or disable Strong mode")
    
    # Add any other warnings (skip duplicates)
    for warning in compat.get("warnings", []):
        cleaned = warning.replace("⚠️ ", "").replace("💡 ", "")
        if "intensity requires" in cleaned.lower() or "strong mode" in cleaned.lower():
            continue
        if cleaned not in issues:
            issues.append(cleaned)
    
    return {
        "is_compatible": compat["compatible"],
        "issues": issues,
        "recommendations": recommendations,
        "upgrade_message": upgrade_message,
        "severity": "incompatible" if not compat["compatible"] else (
            "warning" if issues else "ok"
        ),
        "current_model": model_name,
        "current_tier": compat["tier"],
        "requested_intensity": intensity,
        "effective_intensity": compat["auto_adjustments"].get("intensity", intensity),
    }


def get_tone_guidance(tone: str, intensity: str = "standard") -> str:
    """Get the tone guidance string for a given tone and intensity."""
    tone_dict = TONE_GUIDANCE.get(tone, TONE_GUIDANCE["professional"])
    return tone_dict.get(intensity, tone_dict["standard"])


def get_formatting_rules(tone: str, intensity: str = "standard") -> str:
    """Get the formatting/punctuation rules for a given tone and intensity."""
    tone_dict = FORMATTING_RULES.get(tone, FORMATTING_RULES["professional"])
    return tone_dict.get(intensity, tone_dict["standard"])


def get_filler_rules(tone: str, intensity: str = "standard") -> str:
    """Get the filler word removal rules for a given tone and intensity."""
    # Minimal always uses minimal filler rules regardless of intensity
    if tone == "minimal":
        return FILLER_RULES["minimal"]
    return FILLER_RULES.get(intensity, FILLER_RULES["standard"])

# Refusal detection patterns - phrases that indicate model is refusing to process
REFUSAL_PATTERNS = [
    "i cannot provide",
    "i can't provide",
    "i cannot transcribe",
    "i can't transcribe",
    "i cannot help with",
    "i can't help with",
    "i'm unable to",
    "i am unable to",
    "i cannot assist",
    "i can't assist",
    "as an ai",
    "as a language model",
    "i apologize, but",
    "sorry, but i cannot",
    "i'm not able to",
    "cannot process this",
    "this could be interpreted as",
    "if you need help with",
    "can i help you with something else",
]


def is_refusal_response(response: str) -> bool:
    """
    Detect if the LLM response is a refusal rather than actual processed text.
    
    Args:
        response: The LLM's response text
        
    Returns:
        True if this looks like a refusal/safety filter response
    """
    if not response:
        return False
    
    response_lower = response.lower()
    
    # Check for refusal patterns
    for pattern in REFUSAL_PATTERNS:
        if pattern in response_lower:
            return True
    
    # If response is much longer than expected for cleanup and contains apologies
    if len(response) > 200 and ("apologize" in response_lower or "sorry" in response_lower):
        return True
    
    return False


def is_hallucination(original: str, response: str, threshold: float = 0.3) -> bool:
    """
    Detect if the LLM response is a hallucination (completely different from input).
    
    Uses simple word overlap to detect when the model generates unrelated content
    instead of cleaning up the transcription.
    
    Args:
        original: The original transcription text
        response: The LLM's response text
        threshold: Minimum word overlap ratio (0.0-1.0) to consider valid
        
    Returns:
        True if this looks like a hallucination (low overlap with original)
    """
    if not original or not response:
        return False
    
    # Normalize texts
    def get_words(text: str) -> set:
        # Extract lowercase alphanumeric words
        import re
        return set(re.findall(r'\b[a-z]+\b', text.lower()))
    
    original_words = get_words(original)
    response_words = get_words(response)
    
    if not original_words:
        return False
    
    # Calculate overlap ratio
    common_words = original_words & response_words
    
    # Ignore common filler/stop words that would be removed
    stop_words = {'um', 'uh', 'like', 'you', 'know', 'basically', 'actually', 
                  'i', 'mean', 'so', 'well', 'right', 'the', 'a', 'an', 'and', 
                  'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been', 'to',
                  'of', 'in', 'for', 'on', 'with', 'at', 'by', 'it', 'this', 'that'}
    
    # Meaningful words from original (excluding stop words)
    meaningful_original = original_words - stop_words
    meaningful_common = common_words - stop_words
    
    if not meaningful_original:
        # If original is only stop words, check total overlap
        overlap_ratio = len(common_words) / len(original_words) if original_words else 0
    else:
        # Check meaningful word overlap
        overlap_ratio = len(meaningful_common) / len(meaningful_original)
    
    # If response is much longer and has low overlap, it's likely hallucination
    length_ratio = len(response) / len(original) if original else 1
    
    # Stricter threshold if response is much longer (sign of generation vs cleanup)
    effective_threshold = threshold
    if length_ratio > 3:
        effective_threshold = threshold * 1.5  # Require more overlap for long responses
    
    is_hallucinated = overlap_ratio < effective_threshold
    
    if is_hallucinated:
        print(f"[Post-processing] ⚠ Hallucination detected: {overlap_ratio:.1%} word overlap (threshold: {effective_threshold:.1%})")
    
    return is_hallucinated


# =============================================================================
# Prompt Templates
# =============================================================================

# Minimal cleanup prompt - just removes filler sounds, nothing else
MINIMAL_PROMPT = """Remove filler sounds (um, uh, ah, er) from this transcription. Keep EVERYTHING else exactly as spoken.

DO NOT change words, sentence structure, or add punctuation. Just remove um/uh/ah sounds.

Spoken: {text}

Cleaned:"""

# Standard prompt - cleans up speech while preserving user's words
STANDARD_PROMPT = """Clean up this voice transcription. Keep the user's words and meaning intact.

RULES:
1. {filler_rules}
2. {formatting_rules}
3. {tone_guidance}
4. Do NOT rewrite or restructure sentences. Keep the user's phrasing.

Output ONLY the cleaned text. No commentary.

Spoken: {text}

Cleaned:"""

# Strong prompt - allows restructuring and transformation
STRONG_PROMPT = """Transform this voice transcription into polished {style_name} text.

RULES:
1. {filler_rules}
2. {formatting_rules}
3. {tone_guidance}
4. You may restructure sentences for clarity and flow.
5. If listing items, use bullet points. If it's a message, format appropriately.

Output ONLY the transformed text. No commentary.

Spoken: {text}

Result:"""

# =============================================================================
# 🎭 CARICATURE MODE (Secret Easter Egg!)
# =============================================================================
# This is an intentionally over-the-top, silly mode for fun.
# Unlocked by typing "lol" or "haha" on the Style tab.

CARICATURE_PROMPT = """🎭 CARICATURE MODE ACTIVATED 🎭

Transform this transcription into an ABSURDLY EXAGGERATED {style_name} parody.
This is meant to be FUNNY and OVER-THE-TOP. Go absolutely wild!

STYLE GUIDANCE:
{tone_guidance}

FORMATTING:
{formatting_rules}

FILLER WORDS:
{filler_rules}

BE RIDICULOUS. BE SILLY. MAKE IT A PARODY.
The goal is comedy, not accuracy. Lean into stereotypes of this style.

Original: {text}

🎭 Caricature version:"""

# Simplified prompt for tiny models (<500M params)
SIMPLE_CLEANUP_PROMPT = """Clean this text. Remove "um", "uh". Be {tone_simple}.

Text: {text}

Clean:"""

# Simple tone descriptions for tiny models
SIMPLE_TONES = {
    "minimal": "exact, change nothing except removing um/uh",
    "professional": "formal and polished",
    "casual": "friendly and relaxed",
    "dev": "clear developer context, recognize git and coding terms",
    "personal": "natural, keeping the user's own speaking style",
}


def build_prompt(text: str, config: dict, apply_compatibility: bool = True) -> tuple[str, Dict[str, Any]]:
    """
    Build the appropriate prompt based on config settings.
    
    New simplified system:
    - Minimal: Just removes um/uh/ah, no style processing
    - Other styles: Standard (preserve words) or Strong (transform)
    
    Args:
        text: The transcription text to process
        config: Configuration dictionary with style settings
        apply_compatibility: Whether to check and apply model compatibility adjustments
        
    Returns:
        Tuple of (formatted_prompt, compatibility_info)
    """
    # Get tone and intensity
    tone = config.get("output_tone", "professional")
    
    # 🎭 Caricature mode (secret easter egg!) takes priority
    use_caricature = config.get("caricature_mode", False)
    use_strong = config.get("strong_mode", False)
    
    if use_caricature:
        intensity = "caricature"
    elif use_strong:
        intensity = "strong"
    else:
        intensity = "standard"
    
    # Minimal style ignores intensity - always does minimal cleanup (unless caricature!)
    if tone == "minimal" and not use_caricature:
        intensity = "standard"  # Doesn't matter for minimal, but keep consistent
    
    # Get voice profile context when "personal" style is selected
    voice_profile_context = ""
    is_personal_style = tone == "personal"
    if is_personal_style:
        try:
            from .voice_profile import get_voice_profile
            voice_profile = get_voice_profile(
                history_limit=config.get("voice_learning_history_limit", 100),
                regen_interval=config.get("voice_learning_regen_interval", 20),
            )
            voice_profile_context = voice_profile.get_prompt_context()
        except Exception as e:
            print(f"[Post-processing] ⚠ Could not load voice profile: {e}")
    
    # Get model name for compatibility check
    backend = config.get("post_processing_backend", "ollama")
    if backend == "ollama":
        model_name = config.get("ollama_model", "")
    elif backend == "llama_cpp":
        model_path = config.get("llama_cpp_model_path", "")
        model_name = Path(model_path).stem if model_path else ""
    else:
        model_name = config.get(f"{backend}_model", "")
    
    # Check compatibility and get auto-adjustments
    compatibility = {"warnings": [], "auto_adjustments": {}, "tier": "standard"}
    if apply_compatibility and model_name:
        compatibility = get_model_compatibility(model_name, tone, intensity, False)  # smart_formatting removed
        
        # Apply auto-adjustments for intensity
        if "intensity" in compatibility["auto_adjustments"]:
            intensity = compatibility["auto_adjustments"]["intensity"]
    
    # Detect if we should use the simple prompt for tiny models
    tier = compatibility.get("tier", detect_model_tier(model_name) if model_name else "standard")
    
    if tier == "tiny":
        # Use simplified prompt for tiny models
        tone_simple = SIMPLE_TONES.get(tone, "clear")
        prompt = SIMPLE_CLEANUP_PROMPT.format(tone_simple=tone_simple, text=text)
        return prompt, compatibility
    
    # === MINIMAL STYLE: Special case - just remove filler sounds ===
    if tone == "minimal":
        prompt = MINIMAL_PROMPT.format(text=text)
        return prompt, compatibility
    
    # === STYLED PROCESSING ===
    
    # Get all the dynamic rules based on tone + intensity
    formatting_rules = get_formatting_rules(tone, intensity)
    filler_rules = get_filler_rules(tone, intensity)
    
    # For "personal" style, use voice profile as THE tone guidance
    if is_personal_style and voice_profile_context:
        tone_guidance = f"Match this user's natural speaking style: {voice_profile_context}"
    else:
        tone_guidance = get_tone_guidance(tone, intensity)
    
    # Style names for prompts
    style_names = {
        "minimal": "minimal/raw",
        "professional": "professional/business",
        "casual": "casual/texting",
        "dev": "developer/coding",
        "personal": "personal",
    }
    
    # Choose prompt template based on intensity
    if intensity == "caricature":
        # 🎭 CARICATURE MODE - Go absolutely wild!
        prompt = CARICATURE_PROMPT.format(
            style_name=style_names.get(tone, tone),
            tone_guidance=tone_guidance,
            formatting_rules=formatting_rules,
            filler_rules=filler_rules,
            text=text
        )
    elif intensity == "strong":
        # Strong mode allows restructuring
        prompt = STRONG_PROMPT.format(
            style_name=style_names.get(tone, tone),
            tone_guidance=tone_guidance,
            formatting_rules=formatting_rules,
            filler_rules=filler_rules,
            text=text
        )
    else:
        # Standard mode preserves user's words
        prompt = STANDARD_PROMPT.format(
            tone_guidance=tone_guidance,
            formatting_rules=formatting_rules,
            filler_rules=filler_rules,
            text=text
        )
    
    return prompt, compatibility


# Legacy compatibility - keeping for any external code that might use these
PROMPT_TEMPLATES: Dict[str, str] = {
    "clean": STANDARD_PROMPT,
}


def get_prompt_template(template_name: str, custom_prompt: str = "") -> str:
    """Legacy function - now uses build_prompt internally."""
    return CLEAN_ONLY_PROMPT


def format_prompt(template: str, text: str) -> str:
    """Format a prompt template with the transcription text."""
    return template.replace("{text}", text)


# =============================================================================
# Abstract Backend
# =============================================================================

class PostProcessorBackend(ABC):
    """Abstract base class for post-processing backends."""
    
    @abstractmethod
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process transcribed text to clean it up.
        
        Args:
            text: The raw transcription text
            prompt_template: The formatted prompt to use
            
        Returns:
            Cleaned/formatted text
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available/installed."""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the display name of this backend."""
        pass


# =============================================================================
# llama-cpp-python Backend (Local)
# =============================================================================

class LlamaCppBackend(PostProcessorBackend):
    """
    Local LLM backend using llama-cpp-python.
    Recommended models: Phi-3-mini, Qwen2.5-1.5B-Instruct, SmolLM2-1.7B
    """
    
    # Class-level model cache to avoid reloading
    _model_cache: Dict[str, Any] = {}
    
    def __init__(
        self,
        model_path: str = "",
        n_ctx: int = 2048,
        n_threads: int = 4,
        n_gpu_layers: int = -1,  # -1 = auto (use all available)
        max_tokens: int = 1024,
        temperature: float = 0.1,
        use_gpu: bool = True,
    ):
        self.model_path = os.path.expanduser(model_path) if model_path else ""
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers if use_gpu else 0
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._model = None
    
    def get_name(self) -> str:
        return "llama.cpp (Local)"
    
    def is_available(self) -> bool:
        """Check if llama-cpp-python is installed and model exists."""
        try:
            import llama_cpp
            if self.model_path:
                return Path(self.model_path).exists()
            return True  # Library is available, model path may be set later
        except ImportError:
            return False
    
    def _get_model(self):
        """Get or create the LLM model (cached)."""
        if self._model is not None:
            return self._model
        
        if not self.model_path or not Path(self.model_path).exists():
            raise PostProcessingError(
                f"Model file not found: {self.model_path}. "
                "Please download a GGUF model (e.g., Phi-3-mini, Qwen2.5-1.5B-Instruct)."
            )
        
        cache_key = (self.model_path, self.n_ctx, self.n_gpu_layers)
        if cache_key in LlamaCppBackend._model_cache:
            self._model = LlamaCppBackend._model_cache[cache_key]
            return self._model
        
        try:
            from llama_cpp import Llama
            
            self._model = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                n_gpu_layers=self.n_gpu_layers,
                verbose=False,
            )
            
            LlamaCppBackend._model_cache[cache_key] = self._model
            return self._model
            
        except Exception as e:
            raise PostProcessingError(f"Failed to load llama.cpp model: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using local LLM.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            raise PostProcessingError(
                "llama-cpp-python is not installed. "
                "Install with: pip install llama-cpp-python"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            model = self._get_model()
            
            # Format the full prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Generate response
            response = model(
                full_prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stop=["Transcription:", "Spoken text:", "\n\n\n"],  # Stop tokens
                echo=False,
            )
            
            result = response["choices"][0]["text"].strip()
            
            # Clean up any artifacts and check for refusals
            result = self._clean_response(result, original_text=text)
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"llama.cpp processing failed: {e}")
    
    def _clean_response(self, text: str, original_text: str = "") -> str:
        """
        Clean up LLM response artifacts and detect refusals/hallucinations.
        
        Args:
            text: The LLM response
            original_text: The original input (returned if refusal/hallucination detected)
            
        Returns:
            Cleaned text, or original_text if refusal/hallucination detected
        """
        # Check for refusal first
        if is_refusal_response(text):
            print("[Post-processing] ⚠ Model refused to process - using original text")
            return original_text if original_text else text
        
        # Remove common artifacts
        lines = text.split("\n")
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            # Skip empty lines at start
            if not cleaned_lines and not line:
                continue
            # Skip lines that look like prompts/instructions
            if line.lower().startswith(("transcription:", "cleaned text:", "result:", "formatted", "spoken text:", "cleaned version:")):
                continue
            cleaned_lines.append(line)
        
        cleaned = "\n".join(cleaned_lines).strip()
        
        # Check for hallucination (model generated unrelated content)
        if original_text and is_hallucination(original_text, cleaned):
            print("[Post-processing] ⚠ Model hallucinated - using original text")
            return original_text
        
        return cleaned


# =============================================================================
# Anthropic Claude Backend (Cloud)
# =============================================================================

class AnthropicBackend(PostProcessorBackend):
    """
    Cloud LLM backend using Anthropic Claude API.
    Uses Claude Haiku for fast, cheap, high-quality results.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-3-haiku-20240307",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "Anthropic Claude (Cloud)"
    
    def is_available(self) -> bool:
        """Check if anthropic is installed and API key is set."""
        try:
            import anthropic
            return bool(self.api_key)
        except ImportError:
            return False
    
    def _get_client(self):
        """Get or create the Anthropic client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise PostProcessingError(
                "Anthropic API key not set. "
                "Set ANTHROPIC_API_KEY environment variable or configure in settings."
            )
        
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
            return self._client
            
        except Exception as e:
            raise PostProcessingError(f"Failed to initialize Anthropic client: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using Anthropic Claude API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            if not self.api_key:
                raise PostProcessingError("Anthropic API key not configured.")
            raise PostProcessingError(
                "anthropic package is not installed. "
                "Install with: pip install anthropic"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            client = self._get_client()
            
            # Format the prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Call Claude API
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": full_prompt,
                    }
                ],
            )
            
            result = message.content[0].text.strip()
            
            # Check for refusal (rare with Claude but possible)
            if is_refusal_response(result):
                print("[Post-processing] ⚠ Model refused to process - using original text")
                return text
            
            # Check for hallucination
            if is_hallucination(text, result):
                print("[Post-processing] ⚠ Model hallucinated - using original text")
                return text
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"Anthropic API call failed: {e}")


# =============================================================================
# OpenAI Backend (Cloud)
# =============================================================================

class OpenAIBackend(PostProcessorBackend):
    """
    Cloud LLM backend using OpenAI API.
    Uses GPT-4o-mini for fast, cheap, high-quality results.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "OpenAI (Cloud)"
    
    def is_available(self) -> bool:
        """Check if openai is installed and API key is set."""
        try:
            import openai
            return bool(self.api_key)
        except ImportError:
            return False
    
    def _get_client(self):
        """Get or create the OpenAI client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise PostProcessingError(
                "OpenAI API key not set. "
                "Set OPENAI_API_KEY environment variable or configure in settings."
            )
        
        try:
            import openai
            self._client = openai.OpenAI(api_key=self.api_key)
            return self._client
            
        except Exception as e:
            raise PostProcessingError(f"Failed to initialize OpenAI client: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using OpenAI API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            if not self.api_key:
                raise PostProcessingError("OpenAI API key not configured.")
            raise PostProcessingError(
                "openai package is not installed. "
                "Install with: pip install openai"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            client = self._get_client()
            
            # Format the prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Call OpenAI API
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": full_prompt,
                    }
                ],
            )
            
            result = response.choices[0].message.content.strip()
            
            # Check for refusal (rare with OpenAI but possible)
            if is_refusal_response(result):
                print("[Post-processing] ⚠ Model refused to process - using original text")
                return text
            
            # Check for hallucination
            if is_hallucination(text, result):
                print("[Post-processing] ⚠ Model hallucinated - using original text")
                return text
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"OpenAI API call failed: {e}")


# =============================================================================
# Ollama Backend (Local)
# =============================================================================

class OllamaBackend(PostProcessorBackend):
    """
    Local LLM backend using Ollama API.
    Requires Ollama to be installed and running locally.
    Recommended models: phi3:mini, qwen2.5:1.5b, llama3.2:1b
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "phi3:mini",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "Ollama (Local)"
    
    def is_available(self) -> bool:
        """Check if Ollama is installed and running."""
        try:
            import requests
            response = requests.get(f"{self.base_url}/api/tags", timeout=2)
            return response.status_code == 200
        except:
            return False
    
    def _get_client(self):
        """Get or create the requests session."""
        if self._client is not None:
            return self._client
        
        try:
            import requests
            self._client = requests.Session()
            return self._client
        except ImportError:
            raise PostProcessingError(
                "requests package is not installed. "
                "Install with: pip install requests"
            )
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using Ollama API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            raise PostProcessingError(
                "Ollama is not running. "
                "Please start Ollama service: ollama serve"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            import requests
            
            # The prompt_template is already fully formatted (text embedded by build_prompt)
            # No need to call format_prompt again
            full_prompt = prompt_template
            
            # Debug: Log what we're sending (truncated for readability)
            prompt_preview = full_prompt[:200] + "..." if len(full_prompt) > 200 else full_prompt
            print(f"[Post-processing] Sending to {self.model}...")
            
            # Call Ollama API using chat endpoint for better instruction following
            # Use longer timeout (60s) for first-time model loading
            try:
                response = requests.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a transcription cleanup assistant. You ONLY clean up spoken text - never generate new content. Output ONLY the cleaned text, nothing else."
                            },
                            {
                                "role": "user", 
                                "content": full_prompt
                            }
                        ],
                        "stream": False,
                        "options": {
                            "num_predict": self.max_tokens,
                            "temperature": self.temperature,
                        },
                    },
                    timeout=60,  # Longer timeout for model loading
                )
            except requests.exceptions.Timeout:
                print(f"[Post-processing] ⚠ Ollama timeout after 60s - model may still be loading")
                raise PostProcessingError("Ollama timeout - model may be loading, try again")
            
            if response.status_code != 200:
                raise PostProcessingError(f"Ollama API error: {response.status_code}")
            
            # Chat API returns message.content instead of response
            response_json = response.json()
            if "message" in response_json:
                result = response_json.get("message", {}).get("content", "").strip()
            else:
                # Fallback for generate API format
                result = response_json.get("response", "").strip()
            
            # Debug: Log response preview
            result_preview = result[:150] + "..." if len(result) > 150 else result
            print(f"[Post-processing] Response: {result_preview}")
            
            # Clean up any artifacts and check for refusals
            result = self._clean_response(result, original_text=text)
            
            return result if result else text
            
        except requests.exceptions.RequestException as e:
            raise PostProcessingError(f"Ollama API call failed: {e}")
        except Exception as e:
            raise PostProcessingError(f"Ollama processing failed: {e}")
    
    def _clean_response(self, text: str, original_text: str = "") -> str:
        """
        Clean up LLM response artifacts and detect refusals/hallucinations.
        
        Args:
            text: The LLM response
            original_text: The original input (returned if refusal/hallucination detected)
            
        Returns:
            Cleaned text, or original_text if refusal/hallucination detected
        """
        # Check for refusal first
        if is_refusal_response(text):
            print("[Post-processing] ⚠ Model refused to process - using original text")
            return original_text if original_text else text
        
        # Remove common artifacts
        lines = text.split("\n")
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            # Skip empty lines at start
            if not cleaned_lines and not line:
                continue
            # Skip lines that look like prompts/instructions
            if line.lower().startswith(("transcription:", "cleaned text:", "result:", "formatted", "spoken text:", "cleaned version:")):
                continue
            cleaned_lines.append(line)
        
        cleaned = "\n".join(cleaned_lines).strip()
        
        # Check for hallucination (model generated unrelated content)
        if original_text and is_hallucination(original_text, cleaned):
            print("[Post-processing] ⚠ Model hallucinated - using original text")
            return original_text
        
        return cleaned


# =============================================================================
# Factory Functions
# =============================================================================

def get_backend(config: dict) -> PostProcessorBackend:
    """
    Factory function to create the appropriate post-processing backend.
    
    Args:
        config: Configuration dictionary with post-processing settings
        
    Returns:
        A PostProcessorBackend instance
    """
    backend_type = config.get("post_processing_backend", "ollama")
    
    if backend_type == "ollama":
        return OllamaBackend(
            base_url=config.get("ollama_base_url", "http://localhost:11434"),
            model=config.get("ollama_model", "phi3:mini"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    elif backend_type == "anthropic":
        # API key is read from environment variable only (secure)
        return AnthropicBackend(
            api_key="",  # Will be read from ANTHROPIC_API_KEY env var
            model=config.get("anthropic_model", "claude-3-haiku-20240307"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    elif backend_type == "openai":
        # API key is read from environment variable only (secure)
        return OpenAIBackend(
            api_key="",  # Will be read from OPENAI_API_KEY env var
            model=config.get("openai_model", "gpt-4o-mini"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    else:
        # Default to llama.cpp
        return LlamaCppBackend(
            model_path=config.get("llama_cpp_model_path", ""),
            n_ctx=config.get("llama_cpp_n_ctx", 2048),
            n_threads=config.get("llama_cpp_n_threads", 4),
            n_gpu_layers=config.get("llama_cpp_n_gpu_layers", -1),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
            use_gpu=config.get("use_gpu", True),
        )


def process_with_config(text: str, config: dict) -> str:
    """
    Post-process transcription using settings from config dictionary.
    This is the main entry point for post-processing.
    
    Uses the new tone-based system with optional smart formatting:
    - output_tone: minimal | professional | casual | dev | personal
    - smart_formatting: True (auto-detect content type) | False (clean only)
    
    Includes model compatibility checking and auto-adjustments.
    
    Args:
        text: The raw transcription text
        config: Configuration dictionary with post-processing settings
        
    Returns:
        Cleaned/formatted text, or original text if post-processing is disabled/fails
    """
    # Check if post-processing is enabled
    if not config.get("post_processing_enabled", True):
        return text
    
    if not text or not text.strip():
        return text
    
    try:
        # Build the prompt using new tone-based system with compatibility checks
        prompt, compatibility = build_prompt(text, config)
        
        # Log any compatibility warnings
        for warning in compatibility.get("warnings", []):
            print(f"[Post-processing] {warning}")
        
        # Get backend and process
        backend = get_backend(config)
        
        if not backend.is_available():
            backend_type = config.get("post_processing_backend", "ollama")
            if backend_type == "openai":
                print(f"[Post-processing] ⚠ OpenAI not available - check OPENAI_API_KEY env var")
            elif backend_type == "anthropic":
                print(f"[Post-processing] ⚠ Anthropic not available - check ANTHROPIC_API_KEY env var")
            else:
                print(f"[Post-processing] ⚠ llama.cpp not available - no model selected")
            return text
        
        # Process with the built prompt (prompt already includes the text)
        result = backend.process(text, prompt)
        return result
        
    except PostProcessingError as e:
        # If post-processing fails, return original text
        print(f"[Post-processing] ✗ Error: {e}")
        return text
    except Exception as e:
        # Catch-all for unexpected errors
        print(f"[Post-processing] ✗ Unexpected error: {e}")
        return text


def get_available_backends() -> list:
    """
    Get a list of available post-processing backends.
    
    Returns:
        List of dicts with backend info
    """
    backends = []
    
    # Check llama.cpp
    llama_backend = LlamaCppBackend()
    try:
        import llama_cpp
        llama_available = True
    except ImportError:
        llama_available = False
    
    backends.append({
        "id": "llama_cpp",
        "name": "llama.cpp (Local)",
        "available": llama_available,
        "requires_model": True,
        "description": "Fast local inference using GGUF models. Recommended: Phi-3-mini, Qwen2.5-1.5B",
    })
    
    # Check Anthropic
    try:
        import anthropic
        anthropic_available = True
    except ImportError:
        anthropic_available = False
    
    backends.append({
        "id": "anthropic",
        "name": "Anthropic Claude (Cloud)",
        "available": anthropic_available,
        "requires_api_key": True,
        "description": "High-quality cloud processing using Claude Haiku. Fast and affordable.",
    })
    
    # Check OpenAI
    try:
        import openai
        openai_available = True
    except ImportError:
        openai_available = False
    
    backends.append({
        "id": "openai",
        "name": "OpenAI (Cloud)",
        "available": openai_available,
        "requires_api_key": True,
        "description": "Cloud processing using GPT-4o-mini. Fast and reliable.",
    })
    
    # Check Ollama
    ollama_backend = OllamaBackend()
    ollama_available = ollama_backend.is_available()
    
    backends.append({
        "id": "ollama",
        "name": "Ollama (Local)",
        "available": ollama_available,
        "requires_model": True,
        "description": "Local inference using Ollama. Recommended: phi3:mini, qwen2.5:1.5b",
    })
    
    return backends


def get_template_names() -> list:
    """Legacy function - returns empty list as templates are replaced by tone system."""
    return []


def get_template_descriptions() -> Dict[str, str]:
    """Legacy function - returns empty dict as templates are replaced by tone system."""
    return {}


def get_tone_options() -> list:
    """Get list of available output tones (5 presets)."""
    return [
        {"id": "minimal", "name": "Minimal", "icon": "🎤", "description": "Just removes um/uh. Your exact words."},
        {"id": "professional", "name": "Professional", "icon": "💼", "description": "Clean, business-appropriate tone"},
        {"id": "casual", "name": "Casual", "icon": "💬", "description": "Relaxed, texting style"},
        {"id": "dev", "name": "Dev", "icon": "🖥️", "description": "Developer mode - recognizes git, code, and tech terms"},
        {"id": "personal", "name": "Personal", "icon": "✨", "description": "Your learned speech patterns"},
    ]


def get_model_tier_info(model_name: str) -> Dict[str, Any]:
    """
    Get detailed tier information for a model.
    Useful for UI to display recommendations.
    
    Returns dict with:
        - tier: str (tiny, small, standard, large)
        - description: str
        - max_intensity: str
        - smart_formatting_ok: bool
        - quirks: list of known issues
        - recommendations: list of suggestions
    """
    tier = detect_model_tier(model_name)
    tier_info = MODEL_TIERS[tier]
    quirks = get_model_quirks(model_name)
    
    return {
        "tier": tier,
        "description": tier_info["description"],
        "max_intensity": tier_info["max_intensity"],
        "smart_formatting_ok": tier_info["smart_formatting"],
        "quirks": quirks.get("issues", []),
        "recommendations": [
            f"Best with intensity: {tier_info['max_intensity']} or lower",
        ] + ([f"Note: {quirks['workaround']}"] if quirks.get("workaround") else []),
    }


def get_recommended_models() -> list:
    """
    Get list of recommended models for post-processing.
    
    Returns list of dicts with model recommendations by tier.
    """
    return [
        {
            "tier": "recommended",
            "models": [
                {"name": "qwen2.5:1.5b", "description": "Best balance of speed and quality"},
                {"name": "phi3:mini", "description": "Fast, good instruction following"},
                {"name": "llama3.2:3b", "description": "Higher quality, slightly slower"},
            ],
        },
        {
            "tier": "budget",
            "models": [
                {"name": "llama3.2:1b", "description": "Fast but has quirks with some modes"},
                {"name": "smollm2:360m", "description": "Very fast, basic cleanup only"},
            ],
        },
        {
            "tier": "premium",
            "models": [
                {"name": "qwen2.5:7b", "description": "Excellent quality, slower"},
                {"name": "gpt-4o-mini", "description": "Cloud API, very high quality"},
                {"name": "claude-3-haiku", "description": "Cloud API, excellent quality"},
            ],
        },
    ]

