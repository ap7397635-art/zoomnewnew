import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import { Network, Save, RotateCcw, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/auth/AuthContext";
import { useNavigate } from "react-router-dom";

export default function AdminProxiesPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [text, setText] = useState("");
  const [perBot, setPerBot] = useState(50);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (user && user.role !== "admin") {
      toast.error("Admin access required");
      nav("/dashboard", { replace: true });
    }
  }, [user, nav]);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/proxies");
      setText(data.proxies_text || "");
      setPerBot(data.proxies_per_bot || 50);
      setCount(data.count || 0);
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed to load proxies");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      const { data } = await api.put("/admin/proxies", {
        proxies_text: text,
        proxies_per_bot: Number(perBot) || 1,
      });
      setText(data.proxies_text || "");
      setCount(data.count || 0);
      toast.success(`Saved — ${data.count} proxies. Workers will auto-pull on next restart.`);
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const liveCount = text.split(/[\n,]+/).map((s) => s.trim()).filter((s) => s && !s.startsWith("#")).length;
  const capacity = liveCount * Number(perBot || 0);

  return (
    <div className="min-h-screen bg-[#0b0d12] text-white">
      <TopBar switchTo="classic" />
      <div className="max-w-4xl mx-auto px-6 pb-16">
        <div className="flex items-center gap-2 mb-2">
          <Network className="text-sky-400" size={22} />
          <h1 className="text-2xl font-bold">Proxy / IP Rotation Pool</h1>
        </div>
        <p className="text-white/50 text-sm mb-6">
          Paste your proxies here ONCE. Every RDP worker pulls this list automatically on start —
          no need to create a file on each machine. Bypasses Zoom's per-IP rate limit so one box can run more bots.
        </p>

        <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 space-y-4" data-testid="proxy-panel">
          <div>
            <label className="text-xs uppercase tracking-wider text-white/40">
              Proxies (one per line) — format: <span className="text-sky-300 font-mono">scheme://[user:pass@]host:port</span>
            </label>
            <textarea
              data-testid="proxy-textarea"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={12}
              spellCheck={false}
              placeholder={"socks5://user:pass@1.2.3.4:1080\nhttp://5.6.7.8:8080\n# lines starting with # are ignored"}
              className="mt-2 w-full rounded-xl bg-black/40 border border-white/10 px-4 py-3 font-mono text-sm text-white/90 focus:outline-none focus:ring-2 focus:ring-sky-500/60"
            />
          </div>

          <div className="flex flex-wrap items-end gap-6">
            <div>
              <label className="text-xs uppercase tracking-wider text-white/40">Bots per IP</label>
              <input
                data-testid="proxy-perbot"
                type="number"
                min={1}
                value={perBot}
                onChange={(e) => setPerBot(e.target.value)}
                className="mt-2 w-32 rounded-xl bg-black/40 border border-white/10 px-4 py-2.5 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-sky-500/60"
              />
            </div>
            <div className="text-sm text-white/60">
              <div><span className="text-white font-semibold">{liveCount}</span> proxies entered</div>
              <div className="text-emerald-300 flex items-center gap-1">
                <ShieldCheck size={14} /> ~{capacity} bots clean capacity (no lobby)
              </div>
            </div>
          </div>

          <div className="flex items-center gap-3 pt-2">
            <button
              data-testid="proxy-save-btn"
              onClick={save}
              disabled={saving || loading}
              className="zs-pill text-sky-200 border-sky-500/40 bg-sky-500/15 hover:bg-sky-500/25 transition px-5 py-2.5 disabled:opacity-50"
            >
              <Save size={15} /> {saving ? "Saving..." : "Save Proxy Pool"}
            </button>
            <button
              onClick={load}
              className="zs-pill text-white/70 border-white/15 bg-white/5 hover:bg-white/10 transition px-4 py-2.5"
            >
              <RotateCcw size={15} /> Reload
            </button>
            <span className="text-xs text-white/40">{count} saved on server</span>
          </div>
        </div>

        <div className="mt-6 rounded-2xl border border-amber-500/20 bg-amber-500/5 p-4 text-sm text-amber-100/80">
          <b>Tip:</b> Free proxies don't work for Zoom (blacklisted/slow). Use <b>residential</b> (IPRoyal,
          Smartproxy, Bright Data) or cheap <b>dedicated datacenter</b> IPs. Leave empty for single-IP mode
          (one 64GB RDP still runs ~80 bots fine without proxies).
        </div>
      </div>
    </div>
  );
}
