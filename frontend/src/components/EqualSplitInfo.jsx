import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Layers, Server, CheckCircle2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";

/**
 * EqualSplitInfo — v8.7
 * Replaces the old DistributionPreview picker. Distribution mode is now
 * FORCED to "even" backend-side, so this is a read-only info card that
 * just shows the live split:
 *   "10 members ÷ 4 online RDPs = ~3 per RDP (4 get +1)"
 *
 * v9.8: RDP/distribution details ADMIN-ONLY. Normal user ko poora card hide.
 */
export default function EqualSplitInfo({ members }) {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [workers, setWorkers] = useState([]);

  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    const fetch = async () => {
      try {
        const { data } = await api.get("/workers");
        if (alive) setWorkers(data || []);
      } catch {
        /* ignore — admin-only endpoint, normal users won't see */
      }
    };
    fetch();
    const id = setInterval(fetch, 10000);
    return () => { alive = false; clearInterval(id); };
  }, [isAdmin]);

  if (!isAdmin) return null;

  const onlineWorkers = workers.filter((w) => w.status === "online");
  const n = onlineWorkers.length;
  const m = parseInt(members, 10) || 0;
  const base = n > 0 ? Math.floor(m / n) : 0;
  const remainder = n > 0 ? m - base * n : 0;

  return (
    <div className="mt-5 zs-card-2 p-4 border border-emerald-500/20" data-testid="equal-split-info">
      <div className="flex items-center gap-2 mb-2">
        <Layers size={16} className="text-emerald-300" />
        <div className="text-emerald-300 font-semibold text-sm">
          Equal Distribution
        </div>
        <span className="text-[10px] text-white/40 uppercase tracking-wider ml-1">auto</span>
      </div>
      <div className="text-white/70 text-xs leading-relaxed">
        Members ko <b className="text-emerald-300">{n}</b> online RDP{n === 1 ? "" : "s"} pe
        <b className="text-emerald-300"> equally</b> baant diya jayega — koi distribution chunav
        nahi karna padega.
      </div>
      {m > 0 && n > 0 && (
        <div className="mt-3 rounded-lg bg-emerald-500/5 border border-emerald-500/20 p-3 font-mono text-xs flex items-center gap-3 flex-wrap" data-testid="equal-split-math">
          <span className="text-white/60">{m}</span>
          <span className="text-white/40">÷</span>
          <span className="text-white/60">{n} RDP</span>
          <span className="text-white/40">=</span>
          <span className="text-emerald-300 font-semibold">~{base} per RDP</span>
          {remainder > 0 && (
            <span className="text-amber-300">
              (first {remainder} RDP{remainder === 1 ? "" : "s"} get +1)
            </span>
          )}
        </div>
      )}
      {n === 0 && m > 0 && (
        <div className="mt-3 rounded-lg bg-amber-500/10 border border-amber-500/30 p-3 text-xs text-amber-200 flex items-center gap-2" data-testid="equal-split-no-rdp">
          <Server size={14} /> No online RDPs — task will queue until an RDP comes online.
        </div>
      )}
      {n > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5" data-testid="equal-split-rdps">
          {onlineWorkers.slice(0, 12).map((w) => (
            <span
              key={w.id}
              className="inline-flex items-center gap-1 text-[11px] bg-white/5 border border-white/10 rounded-full px-2 py-0.5 text-white/70"
              data-testid={`equal-split-rdp-${w.id}`}
              title={`Capacity: ${w.capacity_max}`}
            >
              <CheckCircle2 size={10} className="text-emerald-400" />
              {w.name}
            </span>
          ))}
          {onlineWorkers.length > 12 && (
            <span className="text-[11px] text-white/40">+{onlineWorkers.length - 12} more</span>
          )}
        </div>
      )}
    </div>
  );
}
