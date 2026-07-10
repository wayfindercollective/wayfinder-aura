/**
 * Worker authorization unit tests (no Cloudflare runtime required).
 * Run: node --experimental-strip-types --test src/auth.test.ts
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  isEffectivelyPublic,
  looksUltraOnly,
  requiredFeatureForObject,
  tokenAllowsObject,
} from "./auth.ts";

describe("looksUltraOnly / PUBLIC_OBJECTS safety", () => {
  it("flags medium/large/phi/qwen3-4b prefixes", () => {
    assert.equal(looksUltraOnly("whisper/ggml-medium.en.bin"), true);
    assert.equal(looksUltraOnly("whisper/ggml-large-v3-turbo-q5_0.bin"), true);
    assert.equal(looksUltraOnly("llm/Phi-3-mini-4k-instruct-q4.gguf"), true);
    assert.equal(looksUltraOnly("llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"), true);
    assert.equal(looksUltraOnly("whisper/ggml-tiny.en.bin"), false);
  });

  it("never treats Ultra keys as public even if listed", () => {
    const pub = new Set(["whisper/ggml-tiny.en.bin", "whisper/ggml-medium.en.bin"]);
    assert.equal(isEffectivelyPublic("whisper/ggml-tiny.en.bin", pub), true);
    assert.equal(isEffectivelyPublic("whisper/ggml-medium.en.bin", pub), false);
  });
});

describe("requiredFeatureForObject", () => {
  it("maps whisper large/medium to large_models", () => {
    assert.equal(
      requiredFeatureForObject("whisper/ggml-large-v3-turbo-q5_0.bin"),
      "large_models",
    );
    assert.equal(
      requiredFeatureForObject("whisper/ggml-medium.bin"),
      "large_models",
    );
  });

  it("maps Phi and Qwen3-4B to large_cleanup_models", () => {
    assert.equal(
      requiredFeatureForObject("llm/Phi-3-mini-4k-instruct-q4.gguf"),
      "large_cleanup_models",
    );
    assert.equal(
      requiredFeatureForObject("llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
      "large_cleanup_models",
    );
  });
});

describe("tokenAllowsObject", () => {
  const large = "whisper/ggml-large-v3-turbo-q5_0.bin";
  const cleanup = "llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf";

  it("denies legacy v1 / featureless tokens even with paid plan", () => {
    assert.equal(tokenAllowsObject({ plan: "pro", v: 1 }, large), false);
    assert.equal(tokenAllowsObject({ plan: "ultra" }, large), false);
    assert.equal(
      tokenAllowsObject({ plan: "pro", v: 2, features: [] }, large),
      false,
    );
  });

  it("denies wrong feature subset", () => {
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["gpu_acceleration"], plan: "pro" },
        large,
      ),
      false,
    );
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["large_models"], plan: "pro" },
        cleanup,
      ),
      false,
    );
  });

  it("allows matching v2 features", () => {
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["large_models"], plan: "pro" },
        large,
      ),
      true,
    );
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["large_cleanup_models"], plan: "pro" },
        cleanup,
      ),
      true,
    );
  });

  it("allows unknown private keys with any Ultra download feature", () => {
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["large_models"], plan: "pro" },
        "llm/future-model.gguf",
      ),
      true,
    );
    assert.equal(
      tokenAllowsObject(
        { v: 2, features: ["tone_system"], plan: "pro" },
        "llm/future-model.gguf",
      ),
      false,
    );
  });
});
