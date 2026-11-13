/**
 * 時刻表示ユーティリティ（すべて JST 表示 + リモート実行の UTC タイムスタイル補正）。
 */

const MS_PER_SEC = 1000;

function parseEpochMs(value: Date | number | string): number {
  if (value instanceof Date) return value.getTime();
  if (typeof value === "number") return value;
  const raw = String(value).trim();
  if (!raw) return Number.NaN;

  // epoch seconds（backend の一部フィールドは number の ISO/epoch 混在対策として文字列でもあることがある）
  if (/^\d+(?:\.\d+)?$/.test(raw)) {
    const n = Number(raw);
    return n < 1e11 ? n * MS_PER_SEC : n;
  }

  // ISO 8601: timezone が付いてない backend 配信値（"2026-07-28T12:00:00"）を UTC とみなす
  const iso = /^[Tt]/.test(raw) || /^\d{4}-\d{2}-\d{2}[Tt]/.test(raw) ? raw : raw;
  const hasTimezone = /([Zz]|[+-]\d{2}:?\d{2})$/.test(raw);
  const normalized =
    /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(iso)
      ? `${iso}Z`
      : hasTimezone
        ? iso
        : `${iso}Z`;
  const epoch = Date.parse(normalized);
  return Number.isNaN(epoch) ? Number.NaN : epoch;
}

function toDate(value: Date | number | string | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const epochMs = parseEpochMs(value);
  return Number.isNaN(epochMs) ? null : new Date(epochMs);
}

const dateTimeFormatter = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const timeFormatter = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const durationFormatter = new Intl.NumberFormat("ja-JP", {
  style: "decimal",
  maximumFractionDigits: 1,
});

/** JST で "2026-07-28 21:00:00" のような形で表示する（title 用）。 */
export function formatDateTimeJst(value: Date | number | string | null | undefined): string {
  const date = toDate(value);
  return date ? dateTimeFormatter.format(date) : "";
}

/** JST の時刻のみ（モニタFeed の時系列 row 用）。 */
export function formatTimeJst(value: Date | number | string | null | undefined): string {
  const date = toDate(value);
  return date ? timeFormatter.format(date) : "";
}

/**
 * 「12秒前」等の相対時間を生成する。
 * remoteNow が渡されている場合、その時刻を起点にする（リモート実行の UTC と整合させる）。
 */
export function formatRelative(
  value: Date | number | string | null | undefined,
  nowMs?: number,
): string {
  const date = toDate(value);
  if (!date) return "";
  const now = typeof nowMs === "number" ? nowMs : Date.now();
  const deltaSec = Math.max(0, (now - date.getTime()) / MS_PER_SEC);
  if (deltaSec < 5) return "たった今";
  if (deltaSec < 60) return `${Math.floor(deltaSec)}秒前`;
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}分前`;
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}時間前`;
  const days = Math.floor(deltaSec / 86400);
  if (days < 7) return `${days}日前`;
  return formatDateTimeJst(date);
}

/**
 * expires_at から残り秒数（0 未満は 0 に clamp）。
 * 期限なし / 解析不能は null を返す。
 */
export function remainingSeconds(
  expiresAt: Date | number | string | null | undefined,
  nowMs: number,
): number | null {
  const date = toDate(expiresAt);
  if (!date) return null;
  return Math.max(0, Math.floor((date.getTime() - nowMs) / MS_PER_SEC));
}

export function formatDurationSeconds(
  value: number | null | undefined,
): string {
  if (value === null || value === undefined) return "";
  return `${durationFormatter.format(value)}秒`;
}

/** <time title> に入れられる ISO 相当文字列（ローケール非依存）。 */
export function toIsoForTitle(value: Date | number | string | null | undefined): string {
  const date = toDate(value);
  return date ? date.toISOString() : "";
}

/** UTC の "2026-07-28T12:00:00" を JST 配列の UTC として解釈した epoch を返す（表示テスト用）。 */
export function parseNaiveUtcAsEpochMs(value: string): number {
  const epoch = parseEpochMs(value);
  return Number.isNaN(epoch) ? Number.NaN : epoch;
}

/** StatusBadge / MonitorFeed 等で使うバッジの系統色（globals.css の tone class に対応）。 */
export type BadgeTone = "green" | "amber" | "gray" | "blue" | "red";

/** formatRelative の JST 版エイリアス（UI 側 naming 統一用）。 */
export function formatRelativeJst(
  value: Date | number | string | null | undefined,
  nowMs?: number,
): string {
  return formatRelative(value, nowMs);
}
