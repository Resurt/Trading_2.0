import type { JsonPayload } from "../api/types";

export function formatMoney(value: string | number | null | undefined, currency = "RUB"): string {
  if (value === null || value === undefined || value === "") {
    return "Нет данных";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "Нет данных";
  }
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(numeric);
}

export function formatDecimal(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") {
    return "Нет данных";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "Нет данных";
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(numeric);
}

export function formatPercentRatio(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Нет данных";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "Нет данных";
  }
  return new Intl.NumberFormat("ru-RU", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(numeric);
}

export function compactDateTime(value: string | null | undefined): string {
  if (!value) {
    return "Нет данных";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function jsonValue(payload: JsonPayload, key: string): string | number | boolean | null {
  const value = payload[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  return null;
}

export function nestedRecord(payload: JsonPayload, key: string): Record<string, unknown> {
  const value = payload[key];
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

export function objectEntries(payload: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(payload).map(([key, value]) => [key, stringifyValue(value)]);
}

export function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "Нет данных";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

export function countdownFromMicroSession(microSessionId: string | null | undefined): string {
  if (!microSessionId) {
    return "Нет активного окна сбора";
  }
  const now = new Date();
  const minutes = 60 - now.getMinutes() - 1;
  const seconds = 60 - now.getSeconds();
  const safeMinutes = Math.max(0, minutes);
  const safeSeconds = seconds === 60 ? 0 : seconds;
  return `${String(safeMinutes).padStart(2, "0")}:${String(safeSeconds).padStart(2, "0")}`;
}
