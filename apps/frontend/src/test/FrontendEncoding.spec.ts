import { describe, expect, it } from "vitest";

const BAD_PATTERNS = [
  sequence(0x0420, 0x045c),
  sequence(0x0420, 0x045f),
  sequence(0x0420, 0x045b),
  sequence(0x0420, 0x201d),
  sequence(0x0420, 0x203a),
  sequence(0x0420, 0x00b5),
  sequence(0x0420, 0x0405),
  sequence(0x0420, 0x0451),
  sequence(0x0420, 0x00b0),
  sequence(0x0420, 0x0454),
  sequence(0x0421, 0x0403),
  sequence(0x0421, 0x201a),
  sequence(0x0421, 0x040a),
  sequence(0x0421, 0x2039),
  sequence(0x0421, 0x2021),
  sequence(0x0421, 0x2030),
  String.fromCharCode(0x00d0),
  String.fromCharCode(0x00d1),
];

const SOURCE_MODULES = import.meta.glob("../**/*.{vue,ts}", {
  eager: true,
  import: "default",
  query: "?raw",
}) as Record<string, string>;

describe("frontend text encoding", () => {
  it("does not contain common mojibake patterns", () => {
    const findings = Object.entries(SOURCE_MODULES).flatMap(([file, text]) =>
      text
        .split(/\r?\n/)
        .map((line: string, index: number) => ({ line, lineNumber: index + 1 }))
        .filter(({ line }) => BAD_PATTERNS.some((pattern) => line.includes(pattern)))
        .map(({ lineNumber }) => `${file}:${lineNumber}`),
    );

    expect(findings).toEqual([]);
  });
});

function sequence(...codepoints: number[]): string {
  return String.fromCodePoint(...codepoints);
}
