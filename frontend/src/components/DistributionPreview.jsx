import { useEffect, useState, useRef } from "react";
import { api } from "@/lib/api";
import { Users, Cpu, AlertTriangle } from "lucide-react";

/**
 * Live distribution preview for the Create Task form.
 *
 * Calls POST /api/tasks/preview-distribution every time `members` or `mode`
 * change and renders the per-RDP allocation table. The user can override
 * any row inline — overrides are bubbled up via `onAssignmentsChange(map)`
 * so CreateTaskPanel can send them as `pre_assignments` in the task body.
 */
export default function DistributionPreview({
  members,
  mode = "weighted",
  onModeChange,
  onAssignmentsChange,
}) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [overrides, setOverrides] = useState({}); // { worker_id: int }
  const debounceRef = useRef(null);

  // Debounced re-fetch when members or mode changes
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!members || members < 1) {
      setData(null);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      setBusy(true);
      setError("");
      try {
        const { data: resp } = await api.post("/tasks/preview-distribution", {
          members,
          mode,
        });
        setData(resp);
        // Reset overrides when mode changes so user starts from auto-suggestion
        setOverrides({});
      } catch (e) {
        setError(e.response?.data?.detail || "Failed to compute distribution");
        setData(null);
      } finally {
        setBusy(false);
      }
    }, 400);
    return () => clearTimeout(debounceRef.current);
  }, [members, mode]);

  // Bubble overrides up to parent (sanitized to active workers only)
  useEffect(() => {
    if (!data) {
      onAssignmentsChange?.(null);
      return;
    }
    // If user hasn't touched anything → don't pre-assign (let backend
    // do auto-split with live workers in claim loop)
    if (Object.keys(overrides).length === 0) {
      onAssignmentsChange?.(null);
      return;
    }
    // Merge auto + overrides so all rows are sent (locks the plan)
    const merged = {};
    for (const a of data.allocations) {
      const val = overrides[a.worker_id] ?? a.allocated;
      if (val > 0) merged[a.worker_id] = val;
    }
    onAssignmentsChange?.(merged);
  }, [overrides, data, onAssignmentsChange]);

  if (!members || members < 1) return null;

  const setOverride = (wid, val) => {
    const n = parseInt(val, 10);
    if (Number.isNaN(n) || n < 0) {
      const next = { ...overrides };
      delete next[wid];
      setOverrides(next);
    } else {
      setOverrides({ ...overrides, [wid]: n });
    }
  };

  const resetOverrides = () => setOverrides({});

  const totalAllocated = data
    ? data.allocations.reduce(
        (s, a) => s + (overrides[a.worker_id] ?? a.allocated),
        0
      )
    : 0;
  const diff = data ? members - totalAllocated : 0;
  const isLocked = Object.keys(overrides).length > 0;

  return (
    <div className="zs-card-2 p-4 mt-4" data-testid="distribution-preview">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div className="section-icon icon-blue !w-8 !h-8">
            <Users size={14} />
          </div>
          <div>
            <div className="text-white font-bold text-sm">
              Distribution Preview
            </div>
            <div className="text-white/50 text-xs">
              {data
                ? `${data.online_workers} online RDPs · ${data.total_capacity} free cap`
                : busy
                ? "Computing..."
                : ""}
            </div>
          </div>
        </div>
        <select
          value={mode}
          onChange={(e) => onModeChange?.(e.target.value)}
          className="zs-input !py-1 !px-2 !text-xs !w-auto"
          data-testid="distribution-mode-select"
        >
          <option value="weighted">Weighted (by capacity)</option>
          <option value="even">Even split</option>
          <option value="round_robin">Round-robin wave</option>
          <option value="greedy">Greedy (fill RDP-by-RDP)</option>
        </select>
      </div>

      {error && (
        <div className="text-rose-400 text-xs mb-2 flex items-center gap-1">
          <AlertTriangle size={12} /> {error}
        </div>
      )}

      {data && data.allocations.length === 0 && (
        <div className="text-amber-300 text-xs flex items-center gap-1">
          <AlertTriangle size={12} /> No online RDP workers. Start your workers
          first.
        </div>
      )}

      {data && data.unassigned > 0 && (
        <div className="text-amber-300 text-xs mb-2 flex items-center gap-1 zs-card p-2">
          <AlertTriangle size={12} /> Only {data.assigned}/{data.members} bots
          fit in current fleet (free cap = {data.total_capacity}).{" "}
          <span className="font-mono">{data.unassigned} bots cannot be placed</span> —
          increase capacity_max on RDPs or add more workers.
        </div>
      )}

      {data && data.allocations.length > 0 && (
        <>
          <div className="zs-table-wrap !max-h-[260px]">
            <table className="zs-table text-xs">
              <thead>
                <tr>
                  <th>RDP</th>
                  <th>Free</th>
                  <th>Allocated</th>
                  <th>%</th>
                </tr>
              </thead>
              <tbody>
                {data.allocations.map((a) => {
                  const value = overrides[a.worker_id] ?? a.allocated;
                  const pct =
                    a.capacity_max > 0
                      ? Math.round((value * 100) / a.capacity_max)
                      : 0;
                  const overCap = value > a.free_capacity;
                  return (
                    <tr
                      key={a.worker_id}
                      data-testid={`dist-row-${a.worker_id}`}
                    >
                      <td>
                        <div className="font-semibold text-white text-xs">
                          {a.name}
                        </div>
                        <div className="text-white/40 text-[10px] flex items-center gap-1">
                          <Cpu size={10} /> cap {a.capacity_max}
                          {a.ram_free_gb
                            ? ` · ${a.ram_free_gb}GB free`
                            : ""}
                        </div>
                      </td>
                      <td className="font-mono text-xs text-white/70">
                        {a.free_capacity}
                      </td>
                      <td>
                        <input
                          type="number"
                          min="0"
                          max={a.free_capacity}
                          value={value}
                          onChange={(e) =>
                            setOverride(a.worker_id, e.target.value)
                          }
                          className={`zs-input !py-0.5 !px-2 !text-xs !w-20 ${
                            overCap ? "!border-rose-500" : ""
                          }`}
                          data-testid={`dist-input-${a.worker_id}`}
                        />
                      </td>
                      <td className="w-32">
                        <div className="flex items-center gap-1">
                          <div className="flex-1 h-1.5 bg-white/10 rounded-full overflow-hidden">
                            <div
                              className={`h-full ${
                                pct > 90
                                  ? "bg-rose-500"
                                  : pct > 60
                                  ? "bg-amber-400"
                                  : "bg-emerald-500"
                              }`}
                              style={{ width: `${Math.min(100, pct)}%` }}
                            />
                          </div>
                          <span className="text-[10px] font-mono text-white/70 w-8 text-right">
                            {pct}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex items-center justify-between mt-3 text-xs">
            <div className="flex items-center gap-3">
              <span className="text-white/60">
                Total:{" "}
                <span
                  className={`font-mono font-bold ${
                    diff === 0
                      ? "text-emerald-400"
                      : diff > 0
                      ? "text-amber-400"
                      : "text-rose-400"
                  }`}
                  data-testid="dist-total"
                >
                  {totalAllocated} / {members}
                </span>
              </span>
              {diff !== 0 && (
                <span className="text-amber-300 text-[10px]">
                  ({diff > 0 ? `${diff} unassigned` : `${-diff} over`})
                </span>
              )}
              {isLocked && (
                <span className="text-indigo-300 text-[10px] font-semibold">
                  · MANUAL OVERRIDE LOCKED
                </span>
              )}
            </div>
            {isLocked && (
              <button
                type="button"
                onClick={resetOverrides}
                className="text-xs text-indigo-300 hover:text-indigo-200"
                data-testid="dist-reset-overrides"
              >
                Reset to auto
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
