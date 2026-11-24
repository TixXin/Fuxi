import { BookOpen, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { Switch, Tag } from "../components/controls";
import { Button, Card, Dot, ErrorNote, Group, PageHead, Row, Skeleton } from "../components/ui";
import { api, type FaqItem, type KnowledgeSearch } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { ago, clock } from "../lib/format";
import { useApi } from "../lib/useApi";
import "./Knowledge.css";

export function Knowledge({ onMenu, onNavigate }: { onMenu: () => void; onNavigate: (id: string) => void }) {
  const info = useApi(api.knowledge);
  const [syncMsg, setSyncMsg] = useState("");
  useBridgeEvent(["knowledge"], (e) => {
    const msg = String(e.result ?? "");
    if (msg) setSyncMsg(msg);
    void info.reload();
  });

  async function sync() {
    setSyncMsg("");
    try {
      await api.knowledgeSync();
      await info.reload();
    } catch (e) {
      setSyncMsg(e instanceof Error ? e.message : String(e));
    }
  }

  const d = info.data;
  return (
    <div className="page">
      <PageHead title="资料库" onMenu={onMenu} />
      {!d && info.error && (
        <Card>
          <ErrorNote message={info.error} onRetry={info.reload} />
        </Card>
      )}
      {!d && !info.error && <Skeleton height={160} />}
      {d && (
        <Group
          title="同步"
          meta={d.repo}
          footer="产品助手群里有人提问时，从这里找相关段落给模型参考；被 @ 但找不到时会转人工。每 6 小时自动同步一次，只下载变了的文件。"
        >
          <Card>
            <Row
              icon={<Dot level={d.error && (d.failed_at ?? 0) > (d.synced_at ?? 0) ? "bad" : d.synced_at ? "ok" : "idle"} />}
              title={d.syncing ? "正在同步…" : `${d.files} 个文件，${d.chunks} 段`}
              desc={
                <>
                  上次同步 {ago(d.synced_at)}
                  {d.synced_at ? `（${clock(d.synced_at)}）` : ""}
                  {Object.entries(d.sources).map(([k, n]) => ` · ${k} ${n} 段`)}
                </>
              }
            >
              <Button icon={<RefreshCw className={d.syncing ? "spin" : undefined} />} busy={d.syncing} onClick={sync}>
                立即同步
              </Button>
            </Row>
            {d.error && (d.failed_at ?? 0) > (d.synced_at ?? 0) && <ErrorNote message={`上次失败（${ago(d.failed_at)}）：${d.error}`} />}
            {syncMsg && <div className="knowledge__msg">{syncMsg}</div>}
            <Row
              icon={<BookOpen size={16} />}
              title="产品助手群"
              desc={d.enabled ? d.groups.join("、") : "没有设置，产品助手不工作，也不会自动同步"}
            >
              <Button onClick={() => onNavigate("settings")}>去设置</Button>
            </Row>
          </Card>
        </Group>
      )}
      <FaqSection />
      <TrySearch />
    </div>
  );
}

function FaqSection() {
  const list = useApi(api.knowledgeFaqs);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [q, setQ] = useState("");
  const [a, setA] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useBridgeEvent(["knowledge"], () => void list.reload());

  function openNew() {
    setEditing("new");
    setQ("");
    setA("");
    setError("");
  }

  function openEdit(it: FaqItem) {
    setEditing(it.id);
    setQ(it.q);
    setA(it.a);
    setError("");
  }

  async function save() {
    if (!q.trim() || !a.trim()) {
      setError("问题和回答都要填");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.saveFaq(q.trim(), a.trim(), editing === "new" || editing === null ? undefined : editing);
      setEditing(null);
      setQ("");
      setA("");
      await list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: number) {
    setBusy(true);
    setError("");
    try {
      await api.deleteFaq(id);
      if (editing === id) setEditing(null);
      await list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const items = list.data?.items ?? [];
  return (
    <Group
      title="自定义常见问答"
      meta={`${items.length} 条 · 优先于仓库文档`}
      footer="群里反复问到、但仓库文档没写或问法口语化的问题，直接在这里补一条，保存立即参与检索。"
    >
      <Card>
        <div className="faq__bar">
          <span className="faq__hint">用来补足口语问法、临时已知问题或群内高频答疑</span>
          {editing === null && (
            <Button icon={<Plus size={14} />} onClick={openNew}>
              新增问答
            </Button>
          )}
        </div>
        {error && <ErrorNote message={error} />}
        {editing !== null && (
          <div className="faq__form">
            <input
              className="field faq__q"
              placeholder="群友常怎么问？比如：Mac 提示已损坏打不开怎么办"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <textarea
              className="field faq__a"
              rows={3}
              placeholder="简短准确的回答（含具体步骤或命令）"
              value={a}
              onChange={(e) => setA(e.target.value)}
            />
            <div className="faq__actions">
              <Button variant="primary" busy={busy} onClick={save}>
                {editing === "new" ? "添加" : "保存修改"}
              </Button>
              <Button onClick={() => setEditing(null)}>取消</Button>
            </div>
          </div>
        )}
        {items.length === 0 && editing === null && (
          <div className="faq__empty">还没有自定义问答；遇到答不准的高频问题随时补一条。</div>
        )}
        {items.map((it) => (
          <div className="faq__item" key={it.id}>
            <div className="faq__item-head">
              <b>{it.q}</b>
              <div className="faq__item-ops">
                <Button icon={<Pencil size={13} />} onClick={() => openEdit(it)}>
                  改
                </Button>
                <Button icon={<Trash2 size={13} />} busy={busy} onClick={() => remove(it.id)}>
                  删
                </Button>
              </div>
            </div>
            <div className="faq__item-a">{it.a}</div>
          </div>
        ))}
      </Card>
    </Group>
  );
}

function TrySearch() {
  const [q, setQ] = useState("");
  const [judge, setJudge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<KnowledgeSearch | null>(null);
  const [error, setError] = useState("");

  async function run() {
    if (!q.trim()) return;
    setBusy(true);
    setError("");
    try {
      setRes(await api.knowledgeSearch(q, judge));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const verdict = res && (res.judged ?? res.picked);
  return (
    <Group title="试搜" meta="看群里问这句时会给模型哪些资料">
      <Card>
        <div className="trysearch">
          <label className="toolbar__search trysearch__input">
            <Search size={14} />
            <input
              className="field"
              placeholder="比如：截图怎么贴到屏幕上"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
            />
          </label>
          <label className="trysearch__judge">
            <Switch label="让模型再筛一遍" checked={judge} onChange={setJudge} />
            让模型再筛一遍
          </label>
          <Button variant="primary" busy={busy} onClick={run}>
            搜
          </Button>
        </div>
        {error && <ErrorNote message={error} />}
        {res && (
          <div className="trysearch__verdict">
            {verdict && verdict.length ? (
              <>
                会附给模型 <b>{verdict.length}</b> 段{res.judged ? "（模型筛过）" : ""}
              </>
            ) : (
              <>
                没有可用的资料{res.question ? "，群里这么问会转人工" : "（这句不像提问，不会转人工）"}
              </>
            )}
            <span className="trysearch__rule">
              门槛：分数 ≥ {res.floor}，覆盖 ≥ {Math.round(res.cover * 100)}%
            </span>
          </div>
        )}
        {res?.candidates.map((c) => {
          const used = (res.judged ?? res.picked).includes(c.title);
          return (
            <div className={`cand${used ? " is-used" : ""}`} key={c.source + c.title}>
              <div className="cand__head">
                <b>{c.title}</b>
                {used && <Tag tone="mint">会用</Tag>}
                {!used && res.picked.includes(c.title) && <Tag tone="warn">模型筛掉</Tag>}
                <span className="cand__score">
                  分数 {c.score} · 覆盖 {Math.round(c.coverage * 100)}%
                </span>
              </div>
              <div className="cand__source mono">{c.source}</div>
              <div className="cand__text">{c.text}</div>
            </div>
          );
        })}
      </Card>
    </Group>
  );
}
