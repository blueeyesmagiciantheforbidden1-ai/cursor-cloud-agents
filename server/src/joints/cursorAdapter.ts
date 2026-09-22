import { Buffer } from "node:buffer";
import { AdapterError } from "./errors.js";

export { AdapterError };

const PROMPT_MAX_BYTES = 8000;

function assertNoUnpairedSurrogates(text: string): void {
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) {
        throw new AdapterError("unpaired surrogate");
      }
      i += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      throw new AdapterError("unpaired surrogate");
    }
  }
}

function utf8Bytes(codePoint: number): number {
  if (codePoint <= 0x7f) return 1;
  if (codePoint <= 0x7ff) return 2;
  if (codePoint <= 0xffff) return 3;
  return 4;
}

export function boundedText(text: string, maxBytes: number): string {
  if (maxBytes < 0) throw new AdapterError("maxBytes must be non-negative");
  assertNoUnpairedSurrogates(text);

  let bytes = 0;
  let index = 0;
  while (index < text.length) {
    const code = text.charCodeAt(index);
    let units = 1;
    let codePoint = code;
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      codePoint = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00);
      units = 2;
    }
    const size = utf8Bytes(codePoint);
    if (bytes + size > maxBytes) break;
    bytes += size;
    index += units;
  }
  return text.slice(0, index);
}

function isAbsoluteExecutable(executable: string): boolean {
  return executable.startsWith("/") || /^[A-Za-z]:[\\/]/.test(executable);
}

function resolveExecutable(executable: string | undefined): string {
  if (executable === undefined) {
    throw new AdapterError("Cursor requires an explicit local executable");
  }
  if (/users[\\/]9/i.test(executable)) {
    throw new AdapterError("executable path contains Users\\9");
  }
  if (executable.trim() === "" || !isAbsoluteExecutable(executable)) {
    throw new AdapterError("Cursor requires an absolute local executable");
  }
  return executable;
}

function assertPrompt(prompt: string): void {
  if (prompt.trim() === "") throw new AdapterError("prompt is required");
  if (prompt.includes("\0")) throw new AdapterError("prompt contains NUL");
  assertNoUnpairedSurrogates(prompt);
  if (Buffer.byteLength(prompt, "utf8") > PROMPT_MAX_BYTES) {
    throw new AdapterError("prompt exceeds 8000 UTF-8 bytes");
  }
}

export function buildCursorCommand(input: {
  prompt: string;
  executable: string | undefined;
  executionMode: string;
}): string[] {
  const executable = resolveExecutable(input.executable);
  if (input.executionMode === "project_work") {
    throw new AdapterError("Project work is currently supported only by the verified Claude worker");
  }
  if (input.executionMode !== "review" && input.executionMode !== "ask") {
    throw new AdapterError("unsupported execution mode");
  }
  assertPrompt(input.prompt);
  return [executable, input.prompt];
}
