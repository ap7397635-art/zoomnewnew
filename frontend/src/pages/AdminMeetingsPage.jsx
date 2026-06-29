import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import { useAuth } from "@/auth/AuthContext";
import { toast } from "sonner";
import {
  Video,
  Users,
  Play,
  Square,
  Clock,
  Trash2,
  RefreshCw,
  AlertTriangle,
  CheckCircle2,
  Calendar,
  AlertOctagon,
  Loader2,
} from "lucide-react";

/**
 * AdminMeetingsPage — v8.7
 * Live admin view of every Zoom meeting on the platform.
 *   • Shows running vs scheduled vs stopped
 *   • Member count + already-joined count
 *   • One-click "Remove all members" → cancels all tasks for that meeting
 *     so workers stop spawning bots.
 *   • Auto-refresh every 5s.
 */
export default function AdminMeetingsPage() {
  const { user } = useAuth();
  const [meetings, setMeetings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [removingId, setRemovingId] = useState(null);
  const [confirmRemove, setConfirmRemove] = useState(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetting, setResetting] = useState(false);

  const load = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoading(true);
      const { data } = await api.get("/admin/meetings");
      setMeetings(data.meetings || []);
    } catch (e) {
      if (!silent) toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed to load meetings");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const id = setInterval(() => load(true), 5000);
    return () => clearInterval(id);
  }, [load]);

  const removeAll = async (mid) => {
    setRemovingId(mid);
    try {
      const { data } = await api.post(`/admin/meetings/${mid}/remove-all`);
      toast.success(`Removed all members. ${data.tasks_cancelled} task(s) cancelled.`);
      setConfirmRemove(null);
      load(true);
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setRemovingId(null);
    }
  };

  const manualReset = async () => {
    setResetting(true);
    try {
      const { data } = await api.post("/admin/system/reset-now");
      toast.success(
        `System reset — ${data.tasks_deleted} task(s), ${data.chunks_deleted} chunk(s) wiped. Users + RDPs preserved.`,
      );
      setConfirmReset(false);
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Reset failed");
    } finally {
      setResetting(false);
    }
  };

  const running = meetings.filter((g) => g.status === "running");
  const scheduled = meetings.filter((g) => g.status === "scheduled");
  const stopped = meetings.filter((g) => !["running", "scheduled"].includes(g.status));

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} />

      <div className="px-3 sm:px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <div className="section-icon icon-indigo"><Video size={18} /></div>
            <h1 className="text-2xl font-bold text-white">Live Meetings</h1>
            <span className="text-white/40 text-sm" data-testid="meetings-count">
              ({meetings.length} total · {running.length} running)
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => load()}
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="refresh-meetings"
            >
              <RefreshCw size={14} /> Refresh
            </button>
            <button
              onClick={() => setConfirmReset(true)}
              className="zs-btn zs-btn-danger text-sm"
              data-testid="manual-reset-btn"
              title="Manually trigger the 2 AM IST reset — wipes all tasks/chunks but keeps users + RDPs"
            >
              <AlertOctagon size={14} /> Manual Reset
            </button>
          </div>
        </div>

        {/* Info band */}
        <div className="zs-card-2 p-4 mb-5 border border-amber-500/20 bg-amber-500/5">
          <div className="flex items-start gap-3">
            <Clock size={18} className="text-amber-300 mt-0.5" />
            <div className="text-sm text-amber-100/90">
              <b className="text-amber-200">Auto-Reset:</b> Daily at <b>2:00 AM IST</b> the entire database
              (tasks, chunks, usage counters) auto-resets. <b className="text-amber-200">Users + RDPs preserved.</b>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="zs-card-2 p-10 text-center text-white/50" data-testid="meetings-loading">
            <Loader2 className="mx-auto animate-spin mb-3" />
            Loading meetings…
          </div>
        ) : meetings.length === 0 ? (
          <div className="zs-card-2 p-10 text-center" data-testid="meetings-empty">
            <Video size={36} className="mx-auto text-white/20 mb-3" />
            <div className="text-white/60">No meetings yet</div>
            <div className="text-white/40 text-sm mt-1">
              Meetings appear here as soon as a user creates a task.
            </div>
          </div>
        ) : (
          <>
            {running.length > 0 && (
              <Section
                title="Running"
                count={running.length}
                accent="emerald"
                icon={<Play size={14} />}
              >
                {running.map((m) => (
                  <MeetingRow
                    key={m.meeting_id}
                    m={m}
                    onRemove={() => setConfirmRemove(m)}
                    isRemoving={removingId === m.meeting_id}
                  />
                ))}
              </Section>
            )}

            {scheduled.length > 0 && (
              <Section
                title="Scheduled"
                count={scheduled.length}
                accent="indigo"
                icon={<Calendar size={14} />}
              >
                {scheduled.map((m) => (
                  <MeetingRow
                    key={m.meeting_id}
                    m={m}
                    onRemove={() => setConfirmRemove(m)}
                    isRemoving={removingId === m.meeting_id}
                  />
                ))}
              </Section>
            )}

            {stopped.length > 0 && (
              <Section
                title="Recently Stopped"
                count={stopped.length}
                accent="white"
                icon={<Square size={14} />}
              >
                {stopped.map((m) => (
                  <MeetingRow
                    key={m.meeting_id}
                    m={m}
                    onRemove={() => setConfirmRemove(m)}
                    isRemoving={removingId === m.meeting_id}
                  />
                ))}
              </Section>
            )}
          </>
        )}
      </div>

      {/* Confirm remove modal */}
      {confirmRemove && (
        <Modal
          onClose={() => setConfirmRemove(null)}
          title="Remove all members?"
          testid="confirm-remove-modal"
        >
          <div className="space-y-4">
            <div className="text-white/80 text-sm">
              Yeh meeting <b className="text-amber-300 font-mono">{confirmRemove.meeting_id}</b> ke saare
              active tasks cancel ho jayenge. Workers naye bots spawn karna band kar denge.
              Already-joined bots apne browser close hone par leave karenge.
            </div>
            <div className="zs-card p-3 text-xs space-y-1">
              <div className="flex justify-between"><span className="text-white/50">Total members:</span><b className="text-white">{confirmRemove.total_members}</b></div>
              <div className="flex justify-between"><span className="text-white/50">Already joined:</span><b className="text-emerald-300">{confirmRemove.total_joined}</b></div>
              <div className="flex justify-between"><span className="text-white/50">Active tasks:</span><b className="text-amber-300">{confirmRemove.running_tasks}</b></div>
            </div>
            <div className="flex gap-3">
              <button
                onClick={() => removeAll(confirmRemove.meeting_id)}
                disabled={removingId === confirmRemove.meeting_id}
                className="zs-btn zs-btn-danger flex-1"
                data-testid="confirm-remove-submit"
              >
                {removingId === confirmRemove.meeting_id ? (
                  <span className="zs-spin" />
                ) : (
                  <>
                    <Trash2 size={14} /> Remove All Members
                  </>
                )}
              </button>
              <button
                onClick={() => setConfirmRemove(null)}
                className="zs-btn zs-btn-ghost"
                data-testid="confirm-remove-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        </Modal>
      )}

      {confirmReset && (
        <Modal
          onClose={() => setConfirmReset(false)}
          title="Manual Reset — wipe everything?"
          testid="confirm-reset-modal"
        >
          <div className="space-y-4">
            <div className="text-white/80 text-sm">
              Saari tasks, chunks aur usage counters delete ho jayenge.
              <b className="text-emerald-300"> Users aur RDPs safe rahenge.</b>
            </div>
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200 flex items-start gap-2">
              <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
              <span>This is irreversible. Same as the 2 AM auto-reset, but right now.</span>
            </div>
            <div className="flex gap-3">
              <button
                onClick={manualReset}
                disabled={resetting}
                className="zs-btn zs-btn-danger flex-1"
                data-testid="confirm-reset-submit"
              >
                {resetting ? <span className="zs-spin" /> : "Yes, reset now"}
              </button>
              <button
                onClick={() => setConfirmReset(false)}
                className="zs-btn zs-btn-ghost"
                data-testid="confirm-reset-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Section({ title, count, icon, accent, children }) {
  const accentMap = {
    emerald: "text-emerald-300 bg-emerald-500/10 border-emerald-500/30",
    indigo: "text-indigo-300 bg-indigo-500/10 border-indigo-500/30",
    white: "text-white/70 bg-white/5 border-white/20",
  };
  return (
    <div className="mb-6" data-testid={`section-${title.toLowerCase().replace(/\s+/g, "-")}`}>
      <div className="flex items-center gap-2 mb-3">
        <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-semibold ${accentMap[accent] || accentMap.white}`}>
          {icon}
          {title}
        </span>
        <span className="text-white/40 text-xs">({count})</span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function StatusPill({ status }) {
  const map = {
    running: { tone: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30", label: "RUNNING", Icon: Play, pulse: true },
    scheduled: { tone: "bg-indigo-500/15 text-indigo-300 border-indigo-500/30", label: "SCHEDULED", Icon: Calendar },
    active: { tone: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30", label: "RUNNING", Icon: Play, pulse: true },
    completed: { tone: "bg-white/5 text-white/60 border-white/15", label: "COMPLETED", Icon: CheckCircle2 },
    cancelled: { tone: "bg-amber-500/15 text-amber-300 border-amber-500/30", label: "CANCELLED", Icon: Square },
    failed: { tone: "bg-red-500/15 text-red-300 border-red-500/30", label: "FAILED", Icon: AlertTriangle },
    stopped: { tone: "bg-white/5 text-white/60 border-white/15", label: "STOPPED", Icon: Square },
  };
  const cfg = map[status] || map.stopped;
  const { Icon, tone, label, pulse } = cfg;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] font-semibold tracking-wider ${tone}`} data-testid={`status-pill-${status}`}>
      {pulse && <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />}
      <Icon size={10} />
      {label}
    </span>
  );
}

function fmt(iso) {
  if (!iso) return null;
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function MeetingRow({ m, onRemove, isRemoving }) {
  const pct = m.total_members > 0 ? Math.round((m.total_joined / m.total_members) * 100) : 0;
  const canRemove = m.status === "running" || m.status === "scheduled";
  return (
    <div className="zs-card-2 p-4" data-testid={`meeting-row-${m.meeting_id}`}>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex-1 min-w-[260px]">
          <div className="flex items-center gap-3 flex-wrap">
            <code className="text-white font-mono text-lg font-bold" data-testid={`meeting-id-${m.meeting_id}`}>
              {m.meeting_id}
            </code>
            <StatusPill status={m.status} />
            {m.meeting_password && (
              <span className="text-[10px] text-white/40 font-mono">pwd: {m.meeting_password}</span>
            )}
          </div>
          <div className="text-white/50 text-xs mt-1 flex flex-wrap gap-x-3 gap-y-1">
            {m.owner_name && (
              <span>
                Owner: <b className="text-white/80">{m.owner_name}</b>
              </span>
            )}
            {m.last_activity && <span>Updated: {fmt(m.last_activity)}</span>}
            <span>{m.tasks.length} task{m.tasks.length === 1 ? "" : "s"}</span>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="flex items-center gap-2 text-xs text-white/50">
              <Users size={12} /> Members
            </div>
            <div className="text-2xl font-bold text-white font-mono" data-testid={`meeting-members-${m.meeting_id}`}>
              <span className="text-emerald-300">{m.total_joined}</span>
              <span className="text-white/40 text-base"> / {m.total_members}</span>
            </div>
            <div className="w-32 h-1.5 bg-white/10 rounded-full overflow-hidden mt-1">
              <div
                className="h-full bg-emerald-500 transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>

          {canRemove && (
            <button
              onClick={onRemove}
              disabled={isRemoving}
              className="zs-btn zs-btn-danger text-sm"
              data-testid={`remove-all-${m.meeting_id}`}
              title="One-click remove ALL members from this meeting"
            >
              {isRemoving ? <span className="zs-spin" /> : (
                <>
                  <Trash2 size={14} /> Remove All
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Per-task break-up */}
      {m.tasks.length > 1 && (
        <div className="mt-3 pt-3 border-t border-white/5 grid grid-cols-2 md:grid-cols-3 gap-2">
          {m.tasks.slice(0, 6).map((t) => (
            <div key={t.id} className="rounded bg-white/5 border border-white/5 p-2 text-[11px]">
              <div className="flex items-center justify-between">
                <StatusPill status={t.status} />
                <span className="text-white/40 font-mono">
                  {t.joined_count}/{t.members}
                </span>
              </div>
              {t.worker_name && (
                <div className="text-white/50 mt-1 truncate">→ {t.worker_name}</div>
              )}
            </div>
          ))}
          {m.tasks.length > 6 && (
            <div className="rounded bg-white/5 border border-white/5 p-2 text-[11px] text-white/40 flex items-center justify-center">
              +{m.tasks.length - 6} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Modal({ children, onClose, title, testid }) {
  return (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
      onClick={onClose}
      data-testid={testid}
    >
      <div className="zs-card-2 p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-white font-bold text-lg mb-4">{title}</h3>
        {children}
      </div>
    </div>
  );
}
