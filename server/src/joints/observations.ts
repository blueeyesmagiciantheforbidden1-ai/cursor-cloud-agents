const DROPPED_KEYS = new Set([
  "command",
  "argv",
  "print",
  "shell",
  "cmd",
  "script",
  "stdout",
  "stderr",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function reportObservation(input: unknown): unknown {
  if (Array.isArray(input)) return input.map((item) => reportObservation(item));
  if (!isRecord(input)) return input;

  const sanitized: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(input)) {
    if (DROPPED_KEYS.has(key.toLowerCase())) continue;
    sanitized[key] = reportObservation(value);
  }
  return sanitized;
}
