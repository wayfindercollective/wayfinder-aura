# Chunk processing evaluation — 2026-09-30

Should Chunk Processing be on for Ultra, and does it keep every word? Run on
the Mac Studio (M3 Ultra, macOS 27) through the app's own path:
`transcribe_with_config()` on the resident whisper-server (bundled
whisper.cpp), the recorder's exact slicing, the previous chunk's text as the
prompt and the production join (`wayfinder.core.chunking`). Reproduce with
`scripts/eval_chunking.py --server` (see its docstring).

Corpus: the 22 tone-corpus samples read as one continuous dictation (506
words) by five macOS voices (US, UK, Indian, Australian and Irish English),
2 min 9 s to 2 min 27 s each, plus shorter cuts of the same text (~45 s and
~70 s, three voices). WER as in docs/EVAL-2026-09-24.md (fillers removed from
both sides). Models from the shipped catalog, on the GPU.

"Auto" is the Ultra default: one request up to 30 s, then 15 s pieces with
2 s of overlap. "One pass" is Chunk Processing Off.

## Result

WER, mean of five voices (2+ minute dictations), after the fixes below:

| Speech model | One pass | Auto | Words kept (of 506), Auto |
|---|---|---|---|
| **Large v3 Turbo Q5** (Ultra pick) | 37.7% | **5.8%** | 492–504 |
| Small (EN) | 7.4% | 9.3% | 503–523 |
| Base (EN) | 13.0% | 13.9% | 497–517 |

- **One pass drops text on Turbo Q5.** Four of five voices lost 22–47% of the
  words (270–395 of 506 kept): long-form decoding skips or loops. Auto kept every
  voice at 4.5–7.3%.
- Shorter dictations: under 30 s Auto is one request, so nothing changes.
  At ~45 s Auto and one pass were identical (Turbo Q5 2.8–3.3%, Base
  4.4–6.1%). At ~70 s Turbo Q5 improved (10.4–13.4% → 5.9–9.3%) and Base was
  even (15.6–18.2% → 16.0–16.7%).
- On Small and Base, Auto costs 1–2 points on 2+ minute dictations. The
  remaining errors are at the cuts, where a word is split between chunks.

So Chunk Processing Auto is part of the one-time Ultra setup
(core/ultra_defaults.py): it is what makes the recommended Turbo Q5 safe for
long dictations, and it costs little on the smaller models.

## What was wrong at the chunk boundaries, and the fixes

Before the fixes, Auto averaged 8.2% (Turbo Q5), 19.6% (Base) and 13.4%
(Small) on the first three voices. Three problems:

1. **The prompt ended on a cut word.** The recorder cuts mid-word, and Whisper
   finishes a cut word with an invented one ("today" → "to", "the budget" →
   "the day."). With that text as its prompt, the next chunk continued from the
   invented words and lost or garbled real ones. Fix: the prompt drops the
   previous chunk's last 5 words (about the 2 s overlap), so the next chunk
   hears the overlap afresh (`chunk_prompt_context`).
2. **The join only removed exact repeats.** When the first chunk's last words
   were invented, the overlap was never matched and appeared twice. Fix: an
   exact run of 4+ words at the two cut edges anchors the splice; only the
   words beyond it (at most 4 at the end of the first chunk, 3 at the start of
   the next) are dropped, and both chunks cover that audio
   (`find_anchored_overlap`). Without an anchor both texts are kept whole, as
   before.
3. **A prompted chunk sometimes collapsed.** With a prompt, Base returned "a"
   for 17 s of speech (one voice lost 77 words). Fix: a prompted chunk with
   fewer than 0.5 words per second is heard again without the prompt, and the
   retry is kept only if it has at least twice the words plus three
   (`chunk_text_looks_truncated`, `prefer_fuller_chunk_text`).

| Auto, mean of 3 voices | Old | Prompt trimmed | + anchored join |
|---|---|---|---|
| Large v3 Turbo Q5 | 8.2% | 7.8% | 5.7% |
| Base (EN) | 19.6% | 16.5% | 14.4% |
| Small (EN) | 13.4% | 10.6% | 9.2% |

The retry then fixed the one collapsed chunk (Base, US voice: 29.4% → 16.6%).
A prompt matters: with no prompt at all, Auto was 9–60%.

## Caveats

- Synthetic voices and one machine. Real rooms, real microphones and pauses
  mid-sentence are not covered; recheck with recorded dictation.
- The server keeps whisper.cpp's temperature fallback, so repeated one-pass
  runs of the same file can differ (one voice: 31.0% and 25.7%).
- The recorder's own audio processing (Light) runs per chunk; the eval files
  were already level.
