#!/usr/bin/env node
"use strict";

const fs = require("node:fs");

const MAX_SUMMARY_CHARS = 7000;
const FINAL_START = "<<<WAYFINDER_FINAL>>>";
const FINAL_END = "<<<END_WAYFINDER_FINAL>>>";

function fail(message) {
  throw new Error(`LLM summary rejected: ${message}`);
}

function isAllowedOpening(line) {
  if (/^TITLE:\s*\S/i.test(line)) return true;
  if (/^(?:[-*•]\s*)?:[a-z0-9_+-]+:/i.test(line)) return true;
  return /^(?:[-*•]\s*)?\p{Extended_Pictographic}/u.test(line);
}

function selectFinalContent(content) {
  const startCount = content.split(FINAL_START).length - 1;
  const endCount = content.split(FINAL_END).length - 1;

  if (startCount === 0 && endCount === 0) return content;
  if (startCount !== 1 || endCount !== 1) {
    fail("final-answer envelope was incomplete or ambiguous");
  }

  const start = content.indexOf(FINAL_START) + FINAL_START.length;
  const end = content.indexOf(FINAL_END);
  if (end <= start) fail("final-answer envelope was malformed");
  return content.slice(start, end);
}

function extractLlmSummary(rawBody) {
  let parsed;
  try {
    parsed = JSON.parse(rawBody);
  } catch {
    fail("provider returned invalid JSON");
  }

  const content = parsed?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || content.trim().length === 0) {
    fail("provider returned no final content");
  }

  const summary = selectFinalContent(content).replace(/\r\n?/g, "\n").trim();
  if (summary.length === 0) fail("final-answer envelope was empty");
  if (summary.length > MAX_SUMMARY_CHARS) {
    fail(`final content exceeds ${MAX_SUMMARY_CHARS} characters`);
  }

  if (/<\/?think\b|<\/?analysis\b/i.test(summary)) {
    fail("explicit reasoning tags were present");
  }

  const firstLine = summary.split("\n", 1)[0].trim();
  if (!isAllowedOpening(firstLine)) {
    fail("content did not begin with a final Slack bullet or changelog title");
  }

  const reasoningLeak = /(?:^|\n)\s*(?:let me\b|i (?:need|should|will|can|want)\b|i['’]ll\b|hmm\b|now (?:write|craft|compose|structure|refine)\b|draft\s*:|key areas\s*:|rough breakdown\s*:|commit count\s*:|the instructions? (?:say|says|ask|asks)\b)/im;
  if (reasoningLeak.test(summary)) {
    fail("analysis or drafting language was present");
  }

  return summary;
}

function main() {
  try {
    const body = fs.readFileSync(0, "utf8");
    process.stdout.write(extractLlmSummary(body));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}

if (require.main === module) main();

module.exports = {
  FINAL_END,
  FINAL_START,
  MAX_SUMMARY_CHARS,
  extractLlmSummary,
  isAllowedOpening,
  selectFinalContent,
};
