/**
 * Pure authorization helpers for Models CDN (unit-tested without Workers runtime).
 */

/** Private object → required feature id. Prefer exact prefix matches. */
export const OBJECT_FEATURE_RULES: Array<{ prefix: string; feature: string }> = [
  { prefix: "whisper/ggml-medium", feature: "large_models" },
  { prefix: "whisper/ggml-large", feature: "large_models" },
  { prefix: "llm/Qwen_Qwen3-4B", feature: "large_cleanup_models" },
  { prefix: "llm/Phi-3", feature: "large_cleanup_models" },
];

/** Never allow these prefixes via PUBLIC_OBJECTS even if misconfigured. */
export const ULTRA_ONLY_PREFIXES = [
  "whisper/ggml-large",
  "whisper/ggml-medium",
  "llm/Qwen_Qwen3-4B",
  "llm/Phi-3",
];

export function looksUltraOnly(key: string): boolean {
  return ULTRA_ONLY_PREFIXES.some((p) => key.startsWith(p));
}

export function requiredFeatureForObject(key: string): string | null {
  for (const rule of OBJECT_FEATURE_RULES) {
    if (key.startsWith(rule.prefix)) return rule.feature;
  }
  return null;
}

/**
 * Whether a verified license payload may download a private object.
 * Phase A: require v2+ with a non-empty features[] — no legacy plan fallback.
 */
export function tokenAllowsObject(
  payload: Record<string, unknown>,
  objectKey: string,
): boolean {
  const ver = Number(payload.v ?? 0);
  if (!Number.isFinite(ver) || ver < 2) return false;

  const features = payload.features;
  if (!Array.isArray(features) || features.length === 0) return false;

  const need = requiredFeatureForObject(objectKey);
  if (need) return (features as unknown[]).includes(need);

  // Unknown private key: any Ultra download entitlement is enough.
  const anyUltra = ["large_models", "large_cleanup_models", "gpu_acceleration"];
  return anyUltra.some((f) => (features as unknown[]).includes(f));
}

/** Effective public: listed in PUBLIC_OBJECTS and not an Ultra-only key. */
export function isEffectivelyPublic(
  objectKey: string,
  publicSet: Set<string>,
): boolean {
  return publicSet.has(objectKey) && !looksUltraOnly(objectKey);
}
