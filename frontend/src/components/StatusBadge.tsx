import type { BadgeTone } from "@/lib/format";
import { badgeToneForStatus, statusLabelJa } from "@/lib/status";

export function StatusBadge({
  status,
  label,
  titleText,
}: {
  status?: string | null;
  label?: string;
  titleText?: string;
}) {
  const tone: BadgeTone = badgeToneForStatus(status ?? null);
  const shown = label ?? statusLabelJa(status ?? null);
  return (
    <span
      className={`status-badge status-badge--${tone}`}
      title={titleText ?? (status ?? "")}
    >
      {shown}
    </span>
  );
}
