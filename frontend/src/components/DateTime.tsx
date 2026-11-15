import { formatDateTimeJst, formatRelativeJst, toIsoForTitle } from "@/lib/format";

/** JST で絶対時刻を表示。title には生の ISO 値（または epoch ms）を保持 §9.6。 */
export function DateTime({
  value,
  className,
}: {
  value?: string | number | null;
  className?: string;
}) {
  if (value === null || value === undefined || value === "") {
    return <span className="datetime">—</span>;
  }
  let text: string;
  try {
    text = formatDateTimeJst(value);
  } catch {
    text = String(value);
  }
  return (
    <time className={className ?? "datetime"} title={String(value)} dateTime={typeof value === "string" ? value : undefined}>
      {text}
    </time>
  );
}

/** "〜分前" 系の相対表示（ジャストインタイム表示用）。 */
export function RelativeTime({ value }: { value?: string | number | null }) {
  if (value === null || value === undefined || value === "") {
    return <span className="datetime">—</span>;
  }
  let text: string;
  try {
    text = formatRelativeJst(value);
  } catch {
    text = String(value);
  }
  return <span className="datetime datetime-relative">{text}</span>;
}
