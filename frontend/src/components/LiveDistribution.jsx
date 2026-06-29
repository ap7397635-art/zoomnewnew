import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ChevronDown, ChevronUp, CheckCircle2, Loader2, Circle } from "lucide-react";

/**
 * Inline per-RDP live progress strip for an active task row.
 *
 * Polls GET /api/tasks/{id}/distribution every 5s. Collapsible — closed by
 * default to keep the table compact. When open, shows a row per RDP with
 * planned (if pre-assigned), claimed, joined, and a coloured progress bar.
 */
export default function LiveDistribution({ taskId, totalMembers }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!open) return;
    let stop = false;
    const tick = async () => {
      try {
        const { data: resp } = await api.get(`/tasks/${taskId}/distribution`);
        if (!stop) setData(resp);
      } catch {
        /* ignore */
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [taskId, open]);

  const rows = data?.workers || [];

  return (
    <div className="mt-1" data-testid={`live-dist-${taskId}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-xs text-indigo-300 hover:text-indigo-200"
        data-testid={`live-dist-toggle-${taskId}`}
      >
        {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        {open ? "Hide" : "Per-RDP"} ({rows.length || "..."})
      </button>

      {open && (
        <div className="mt-2 zs-card p-2 space-y-1.5 max-w-[520px]">
          {rows.length === 0 ? (
            <div className="text-white/40 text-[10px]">
              No claims yet — waiting for workers...
            </div>
          ) : (
            rows.map((r) => {
              const denom = r.planned && r.planned > 0 ? r.planned : Math.max(r.claimed, 1);
              const joinedPct = Math.min(100, Math.round((r.joined * 100) / denom));
              const claimedPct = Math.min(100, Math.round((r.claimed * 100) / denom));
              const StatusIcon =
                r.status === "complete"
                  ? CheckCircle2
                  : r.status === "active"
                  ? Loader2
                  : Circle;
              const statusColor =
                r.status === "complete"
                  ? "text-emerald-400"
                  : r.status === "active"
                  ? "text-sky-400 animate-spin"
                  : "text-white/30";
              return (
                <div
                  key={r.worker_id}
                  className="grid grid-cols-[100px_1fr_90px] gap-2 items-center text-[11px]"
                  data-testid={`live-dist-row-${r.worker_id}`}
                >
                  <div className="flex items-center gap-1 truncate">
                    <StatusIcon size={11} className={statusColor} />
                    <span className="text-white/90 truncate" title={r.name}>
                      {r.name}
                    </span>
                  </div>
                  <div className="relative h-2 bg-white/10 rounded-full overflow-hidden">
                    {/* Claimed (light bar = reserved/announced) */}
                    <div
                      className="absolute inset-y-0 left-0 bg-indigo-500/40"
                      style={{ width: `${claimedPct}%` }}
                    />
                    {/* Joined (solid bar = actually in meeting) */}
                    <div
                      className="absolute inset-y-0 left-0 bg-emerald-500"
                      style={{ width: `${joinedPct}%` }}
                    />
                  </div>
                  <div className="font-mono text-white/80 text-right tabular-nums">
                    {r.joined}/{r.planned ?? r.claimed}
                    {r.planned && r.claimed < r.planned ? (
                      <span className="text-white/40"> ({r.claimed} claimed)</span>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}

          {data && (
            <div className="flex items-center justify-between pt-1.5 mt-1 border-t border-white/5 text-[10px] text-white/50">
              <span>
                Mode:{" "}
                <span className="text-indigo-300 font-semibold">
                  {data.distribution_mode}
                </span>
                {data.pre_assigned && (
                  <span className="ml-1 text-amber-300">· pre-assigned</span>
                )}
              </span>
              <span className="font-mono">
                {data.members_joined}/{totalMembers || data.members} joined ·{" "}
                {data.joined_pct}%
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
