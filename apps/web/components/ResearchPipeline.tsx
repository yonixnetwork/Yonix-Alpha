"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Section } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const STAGE_CLASS: Record<string, string> = {
  RESEARCH: "pill pill-off", REVIEW: "pill pill-warn", PAPER: "pill pill-warn", VALIDATION: "pill pill-warn",
  CONTROLLED_RELEASE: "pill pill-ok", REJECTED: "pill pill-danger",
};

function Mover({ item, data, reload }: { item: J; data: J; reload: () => void }) {
  const options: string[] = [...(item.next ? [item.next] : []),
    ...(item.stage === data.rejected ? ["RESEARCH"] : [data.rejected]),
    ...data.stages.filter((s: string) => item.stage !== data.rejected && data.stages.indexOf(s) < data.stages.indexOf(item.stage))];
  const [stage, setStage] = useState<string>(options[0]);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const move = async () => {
    try { await apiPost(`/api/research/${item.id}/move`, { stage, note }); setNote(""); setMsg(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div className="btn-row">
      <select value={stage} onChange={(e) => setStage(e.target.value)} aria-label="Move to">
        {options.map((s) => <option key={s} value={s}>{s.replaceAll("_", " ")}</option>)}
      </select>
      <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={data.requirements[stage] ?? "reason"}
        aria-label="Evidence or reason" style={{ minWidth: 320 }} />
      <button className="btn btn-sm" onClick={move}>Move</button>
      {msg && <span className="small neg">{msg}</span>}
    </div>
  );
}

/** Master §67: research results go RESEARCH -> REVIEW -> PAPER -> VALIDATION ->
 * CONTROLLED RELEASE, moved by the operator with evidence; never a rule change. */
export default function ResearchPipeline() {
  const { data, error, reload } = useApi<J>("/api/research", undefined, { refreshMs: 60000 });
  const [kind, setKind] = useState("launchpad");
  const [title, setTitle] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const create = async (body: J) => {
    try { await apiPost("/api/research", body); setTitle(""); setMsg(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="Research pipeline">
      <p className="muted small">{data.note}</p>
      <p className="muted small">{data.stages.join(" -> ")}; any stage can be REJECTED with the reason.</p>
      {data.items.length === 0 ? <Empty>No research item yet.</Empty> : (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Item</th><th>Kind</th><th>Stage</th><th>Updated</th><th>Move</th></tr></thead>
          <tbody>{data.items.map((i: J) => (
            <tr key={i.id}>
              <td>{i.title}{i.summary ? <div className="muted small">{i.summary}</div> : null}
                <button className="btn btn-ghost btn-sm" onClick={() => setOpen(open === i.id ? null : i.id)}>
                  {open === i.id ? "Hide history" : `History (${i.history.length})`}</button>
                {open === i.id && <ul className="small">{i.history.map((h: J, n: number) => (
                  <li key={n}>{formatDate(h.at)} {h.by}: {h.stage}{h.note ? ` - ${h.note}` : ""}</li>))}</ul>}</td>
              <td className="small">{i.kind.replaceAll("_", " ")}</td>
              <td><span className={STAGE_CLASS[i.stage] ?? "pill pill-off"}>{i.stage.replaceAll("_", " ")}</span></td>
              <td className="small">{formatDate(i.updated_at)}</td>
              <td><Mover key={i.stage} item={i} data={data} reload={reload} /></td>
            </tr>))}</tbody>
        </table></div>
      )}
      {data.suggestions.length > 0 && (
        <>
          <div className="section-title">Not tracked yet</div>
          <ul className="small">{data.suggestions.map((s: J) => (
            <li key={`${s.source}:${s.ref}`}>{s.title}{" "}
              <button className="btn btn-ghost btn-sm" onClick={() => create(s)}>Track</button></li>))}</ul>
        </>
      )}
      <div className="btn-row">
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
          {data.kinds.map((k: string) => <option key={k} value={k}>{k.replaceAll("_", " ")}</option>)}
        </select>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="New research item" aria-label="Title" />
        <button className="btn btn-sm" disabled={title.trim().length < 3} onClick={() => create({ kind, title })}>Add</button>
        {msg && <span className="small neg">{msg}</span>}
      </div>
    </Section>
  );
}
