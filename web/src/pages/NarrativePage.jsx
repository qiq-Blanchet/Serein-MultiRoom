import { roomSessionStorage } from "../storage/roomScopedStorage.js";
import { useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  ArrowLeft,
  ArrowRight,
  CaretDown,
  CircleNotch,
  PencilSimple,
  Trash,
  X,
} from "@phosphor-icons/react";
import {
  loadNarrativeRolls,
  previewNarrativeRoll,
  projectLiveRoll,
  deleteNarrativeRoll,
  renameNarrativeRoll,
  saveNarrativeRollBody,
  uploadNarrativeMaterial,
} from "../storage/narrativeStore.js";
import { bookAppearance, chronologicalSources } from "../storage/narrativeAppearance.js";
import { NarrativeCreateDialog } from "../components/NarrativeCreateDialog.jsx";
import { LinkCandidatePicker } from "../components/ResumeMemoryPicker.jsx";
import { narrativeRequest } from '../storage/narrativeErrors.js';

const transitionTo = (update) => {
  if (!document.startViewTransition) {
    update();
    return;
  }

  document.startViewTransition(() => {
    flushSync(update);
  });
};

const formatRange = (roll) => `${roll.timeStart} — ${roll.timeEnd}`;

export function NarrativePage() {
  const [createOpen, setCreateOpen] = useState(false);
  const libraryRead = useRef(null);
  const [narrativeRolls, setNarrativeRolls] = useState([]);
  const [libraryError, setLibraryError] = useState('');
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [titleEditing, setTitleEditing] = useState(false);
  const [titleInput, setTitleInput] = useState('');
  const [titleError, setTitleError] = useState('');
  const [selectedRollId, setSelectedRollId] = useState(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [previewBody, setPreviewBody] = useState("");
  const [previewDiff, setPreviewDiff] = useState("");
  const [previewMode, setPreviewMode] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [writingNew, setWritingNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [materialIds, setMaterialIds] = useState(null);
  const [materialType, setMaterialType] = useState("event_ids");
  const [materialIdInput, setMaterialIdInput] = useState("");
  const [materialLabels, setMaterialLabels] = useState({});
  const [previewSeal, setPreviewSeal] = useState(null);
  const selectedRoll = useMemo(
    () => narrativeRolls.find((roll) => roll.id === selectedRollId) ?? null,
    [narrativeRolls, selectedRollId],
  );

  useEffect(() => {
    let cancelled = false;
    const refreshRolls = () => {
      libraryRead.current?.abort();
      const controller = new AbortController();
      libraryRead.current = controller;
      return loadNarrativeRolls({ signal: controller.signal }).then((rolls) => {
        if (!cancelled && !controller.signal.aborted) { setNarrativeRolls(rolls); setLibraryError(''); }
      }).catch(error => { if (!cancelled && !controller.signal.aborted) setLibraryError(error.message); })
        .finally(() => { if (!cancelled && !controller.signal.aborted) setLoading(false); });
    };
    const openFromInbox = () => {
      if (window.location.hash === "#narrative" && roomSessionStorage().getItem("serein:narrative-open-intent")) refreshRolls();
    };
    refreshRolls();
    window.addEventListener("hashchange", openFromInbox);
    return () => {
      cancelled = true;
      libraryRead.current?.abort();
      window.removeEventListener("hashchange", openFromInbox);
    };
  }, []);

  useEffect(() => {
    if (!selectedRoll) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === "Escape" && !previewing && !saving && !deleting && !writingNew) {
        if (titleEditing) {
          setTitleEditing(false);
        } else if (editorOpen) {
          setEditorOpen(false);
        } else {
          transitionTo(() => setSelectedRollId(null));
        }
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [editorOpen, selectedRoll, previewing, saving, deleting, writingNew, titleEditing]);

  const openRoll = (rollId) => {
    setTitleEditing(false); setTitleError('');
    setDeleteError('');
    setMaterialIds(null);
    transitionTo(() => setSelectedRollId(rollId));
  };

  const closeRoll = () => {
    setTitleEditing(false); setTitleError('');
    setDeleteError('');
    setEditorOpen(false);
    setPreviewBody("");
    setPreviewDiff("");
    setPreviewMode("");
    setPreviewError("");
    setSaveMessage("");
    setMaterialIds(null);
    setPreviewSeal(null);
    transitionTo(() => setSelectedRollId(null));
  };

  const deleteRoll = async () => {
    if (!selectedRoll || deleting || previewing || saving || writingNew || uploading) return;
    if (!window.confirm(`删除《${selectedRoll.title}》？\n这本书的正文和材料关联会从书架移除，原始 Event、日记、Scene 和上传文件保留。`)) return;
    setDeleting(true); setDeleteError('');
    try {
      await deleteNarrativeRoll(selectedRoll);
      libraryRead.current?.abort(); setLoading(false);
      setNarrativeRolls(rolls => rolls.filter(roll => roll.id !== selectedRoll.id));
      closeRoll();
    } catch (error) { setDeleteError(error.message); }
    finally { setDeleting(false); }
  };

  const saveTitle = async (event) => {
    event.preventDefault();
    if (!selectedRoll || saving) return;
    setSaving(true); setTitleError('');
    try {
      const result = await renameNarrativeRoll(selectedRoll, titleInput.trim());
      libraryRead.current?.abort(); setLoading(false);
      setNarrativeRolls(rolls => rolls.map((roll, index) => roll.id === selectedRoll.id
        ? projectLiveRoll(result, index, roll) : roll));
      setPreviewSeal(null); setTitleEditing(false);
    } catch (error) { setTitleError(error.message); }
    finally { setSaving(false); }
  };

  const openEditor = () => {
    setTitleEditing(false); setTitleError('');
    setMaterialIdInput("");
    setMaterialLabels({});
    setPreviewBody(selectedRoll?.body || selectedRoll?.paragraphs?.join("\n\n") || "");
    setPreviewDiff("");
    setPreviewMode("");
    setPreviewError("");
    setSaveMessage("");
    setMaterialIds(selectedRoll?.materialIds || null);
    setPreviewSeal(null);
    setEditorOpen(true);
  };

  const closeEditor = () => {
    setEditorOpen(false);
    setPreviewBody("");
    setPreviewDiff("");
    setPreviewMode("");
    setPreviewError("");
    setSaveMessage("");
    setMaterialIds(null);
    setPreviewSeal(null);
  };

  const generatePreview = async (mode, targetRoll = selectedRoll, proposedBody = "") => {
    if (!targetRoll || previewing) return;
    setTitleEditing(false); setTitleError('');
    const targetMaterialIds = (targetRoll.id === selectedRoll?.id ? materialIds : null) || targetRoll.materialIds;
    setMaterialIds(targetMaterialIds);
    setEditorOpen(true);
    setPreviewBody(targetRoll.body || targetRoll.paragraphs.join("\n\n"));
    setPreviewDiff("");
    setPreviewMode(mode);
    setPreviewing(true);
    setPreviewError("");
    setSaveMessage("");
    setPreviewSeal(null);
    try {
      const result = await previewNarrativeRoll(targetRoll, mode, targetMaterialIds, proposedBody);
      if (result.status === "insufficient") {
        setPreviewBody(targetRoll.body || targetRoll.paragraphs.join("\n\n"));
        setPreviewDiff("");
        setPreviewMode(mode);
        setPreviewError('书写未完成：' + (result.issues?.join("；") || "当前绑定材料不足以生成可信正文。")
          + (targetRoll.isUnwritten ? '\n叙事线和材料已保存，正文仍然留白。' : '\n原正文未改变。'));
        return null;
      }
      setPreviewBody(result.body);
      setPreviewDiff(result.diff || "");
      setPreviewMode(mode);
      setMaterialIds(result.proposed_material_ids);
      setPreviewSeal(result);
      return result;
    } catch (error) {
      setPreviewError((error.message || "没有生成这次预览。")
        + (targetRoll.isUnwritten ? '\n叙事线和材料已保存，正文仍然留白。' : '\n原正文未改变。'));
      return null;
    } finally {
      setPreviewing(false);
    }
  };

  const writeNewRoll = async (targetRoll = selectedRoll) => {
    if (!targetRoll || writingNew || previewing || saving) return;
    setWritingNew(true);
    try {
      const preview = await generatePreview("rewrite", targetRoll);
      if (!preview?.preview_fingerprint) return;
      setSaving(true);
      const result = await saveNarrativeRollBody(targetRoll, preview.body, preview);
      setNarrativeRolls(rolls => rolls.map((roll, index) => roll.id === targetRoll.id
        ? projectLiveRoll(result, index, roll) : roll));
      setEditorOpen(false);
      setPreviewSeal(null);
    } catch (error) {
      setPreviewError((error.message || "正文保存失败。") + '\n叙事线和材料已保存，这份正文仍留在编辑区。');
    } finally {
      setSaving(false);
      setWritingNew(false);
    }
  };

  const saveLineMaterials = async () => {
    setSaving(true);
    setPreviewError("");
    try {
      const { payload: result } = await narrativeRequest("/__serein/memory/save-narrative-materials", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ narrative_id: selectedRoll.id, expected_revision: selectedRoll.revision,
          material_ids: materialIds || selectedRoll.materialIds }),
      }, '保存材料', ['updated']);
      setNarrativeRolls(rolls => rolls.map((roll, index) => roll.id === selectedRoll.id
        ? projectLiveRoll(result, index, { ...roll, sources: editableMaterials.map(source => ({
          ...source, type: source.key.replace(/_ids$/, ''),
        })) }) : roll));
      setPreviewSeal(null);
      setSaveMessage("材料已保存，正文仍然留白。");
    } catch (error) {
      setPreviewError(error.message);
    } finally { setSaving(false); }
  };

  useEffect(() => {
    if (window.location.hash !== "#narrative") return;
    const raw = roomSessionStorage().getItem("serein:narrative-open-intent");
    if (!raw) return;
    const intent = JSON.parse(raw);
    const roll = narrativeRolls.find(item => item.id === intent.narrativeId);
    if (!roll) return;
    roomSessionStorage().removeItem("serein:narrative-open-intent");
    setSelectedRollId(roll.id);
    if (intent.action === "write") writeNewRoll(roll);
  }, [narrativeRolls]);

  useEffect(() => {
    const openRewriteIntent = () => {
      if (window.location.hash !== "#narrative") return;
      const narrativeId = roomSessionStorage().getItem("serein:narrative-rewrite-intent") || "";
      const targetRoll = narrativeRolls.find((roll) => roll.id === narrativeId);
      if (!targetRoll) return;
      roomSessionStorage().removeItem("serein:narrative-rewrite-intent");
      setSelectedRollId(targetRoll.id);
      generatePreview("rewrite", targetRoll);
    };
    openRewriteIntent();
    window.addEventListener("hashchange", openRewriteIntent);
    return () => window.removeEventListener("hashchange", openRewriteIntent);
  }, [narrativeRolls]);

  const saveBody = async () => {
    if (!selectedRoll || saving || previewing || !previewBody.trim()) return;
    if (!previewSeal) {
      const validation = await generatePreview("edit", selectedRoll, previewBody);
      if (validation?.preview_fingerprint) {
        setSaveMessage("正文与材料已经校验；请查看增删项，再确认保存。");
      }
      return;
    }
    if (!window.confirm("确认把这份正文与材料增删一起保存为新的 revision？")) return;
    setSaving(true);
    setPreviewError("");
    setSaveMessage("");
    try {
      const result = await saveNarrativeRollBody(selectedRoll, previewBody, previewSeal);
      setNarrativeRolls((rolls) => rolls.map((roll, index) => (
        roll.id === selectedRoll.id
          ? projectLiveRoll(result, index, { ...roll, sources: editableMaterials.map(source => ({
              ...source, type: source.key.replace(/_ids$/, ''),
            })) })
          : roll
      )));
      setPreviewDiff("");
      setPreviewMode("");
      setPreviewSeal(null);
      setSaveMessage(`已保存为 revision ${result.revision}`);
    } catch (error) {
      setPreviewError(error.message || "这次正文没有保存。");
    } finally {
      setSaving(false);
    }
  };

  const removeMaterial = (key, id) => {
    setMaterialIds((current) => ({
      ...(current || selectedRoll.materialIds),
      [key]: (current?.[key] || selectedRoll.materialIds?.[key] || []).filter((value) => String(value) !== String(id)),
    }));
    setPreviewSeal(null);
    setSaveMessage("");
  };

  const addMaterial = (candidate = null) => {
    if (!selectedRoll || saving || previewing || writingNew || uploading) return;
    const kind = materialType.replace(/_ids$/, "");
    if (candidate && candidate.kind !== kind) return;
    const raw = candidate ? String(candidate.id).trim() : materialIdInput.trim();
    if (!raw) return;
    const value = ["diary_ids", "darkroom_ids"].includes(materialType) ? Number(raw) : raw;
    if (["diary_ids", "darkroom_ids"].includes(materialType) && (!Number.isSafeInteger(value) || value <= 0)) {
      setPreviewError("Diary / Darkroom ID 必须是正整数。");
      return;
    }
    if (candidate) setMaterialLabels(current => ({ ...current,
      [JSON.stringify([selectedRoll.id, kind, String(value)])]: candidate }));
    setMaterialIds((current) => {
      const base = current || selectedRoll.materialIds;
      const values = base?.[materialType] || [];
      return { ...base, [materialType]: values.some((item) => String(item) === String(value)) ? values : [...values, value] };
    });
    setMaterialIdInput("");
    setPreviewError("");
    setPreviewSeal(null);
    setSaveMessage("");
  };

  const uploadLocalMaterial = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || uploading) return;
    setUploading(true);
    setPreviewError("");
    try {
      const uploaded = await uploadNarrativeMaterial(file);
      setMaterialIds((current) => {
        const base = current || selectedRoll.materialIds;
        const values = base?.upload_ids || [];
        return {
          ...base,
          upload_ids: values.includes(uploaded.upload_id) ? values : [...values, uploaded.upload_id],
        };
      });
      setNarrativeRolls((rolls) => rolls.map((roll) => (
        roll.id !== selectedRoll.id
          ? roll
          : {
              ...roll,
              sources: [
                ...(roll.sources || []).filter((source) => !(source.type === "upload" && source.id === uploaded.upload_id)),
                {
                  type: "upload",
                  typeLabel: "本地文件",
                  id: uploaded.upload_id,
                  title: uploaded.filename,
                  date: String(uploaded.created_at || "").slice(0, 10),
                  status: uploaded.extraction_status,
                },
              ],
            }
      )));
      setPreviewSeal(null);
      setSaveMessage(`${uploaded.filename} 已上传并加入拟绑定材料。`);
    } catch (error) {
      setPreviewError(error.message || "这个文件没有上传成功。");
    } finally {
      setUploading(false);
    }
  };

  const editableMaterials = useMemo(() => {
    if (!selectedRoll || !materialIds) return [];
    const keys = { event: "event_ids", scene: "scene_ids", diary: "diary_ids", darkroom: "darkroom_ids", upload: "upload_ids" };
    const labels = { event_ids: "Event", scene_ids: "Scene", diary_ids: "日记", darkroom_ids: "暗房", upload_ids: "本地文件" };
    const sourceByKey = new Map((selectedRoll.sources || []).map((source) => [`${source.type}:${source.id}`, source]));
    return chronologicalSources(Object.entries(labels).flatMap(([key, typeLabel]) => (
      (materialIds[key] || []).map((id) => {
        const type = Object.keys(keys).find((candidate) => keys[candidate] === key);
        const source = materialLabels[JSON.stringify([selectedRoll.id, type, String(id)])] || sourceByKey.get(`${type}:${id}`);
        return { key, id, typeLabel, date: source?.date || "", title: source?.title || String(id) };
      })
    )));
  }, [materialIds, selectedRoll, materialLabels]);

  return (
    <div className={`narrative-experience${selectedRoll ? " is-reading" : ""}`}>
      {createOpen && <NarrativeCreateDialog onClose={() => setCreateOpen(false)} onCreated={async (id, write, saved) => {
        const roll = saved ? projectLiveRoll(saved, narrativeRolls.length) : (await loadNarrativeRolls()).find(item => item.id === id);
        if (!roll) throw new Error("叙事线已保存，但书架尚未刷新。请关闭窗口后刷新书架。");
        libraryRead.current?.abort(); setLoading(false);
        setNarrativeRolls(rolls => [...rolls.filter(item => item.id !== id), roll]);
        setCreateOpen(false); setSelectedRollId(id); setMaterialIds(roll.materialIds);
        if (write) await writeNewRoll(roll);
      }} />}
      {!selectedRoll ? (
        <section className="narrative-library" aria-labelledby="narrative-title">
          <header className="narrative-library__header">
            <div>
              <span className="narrative-kicker">NARRATIVE ROLLS</span>
              <h1 id="narrative-title">叙事卷</h1>
              <p>把散落的记忆放回时间里，看它们怎样慢慢长成一条路。</p>
            </div>
            <div className="narrative-library__count" aria-label={`目前共有 ${narrativeRolls.length} 卷`}>
              <strong>{String(narrativeRolls.length).padStart(2, "0")}</strong>
              <span>卷，仍在生长</span>
            </div>
          </header>

          {loading && <p role="status">正在读取书架…</p>}
          {libraryError && <div role="alert" style={{ whiteSpace: 'pre-line' }}>{libraryError}
            <button type="button" disabled={loading} onClick={async () => {
              libraryRead.current?.abort();
              const controller = new AbortController(); libraryRead.current = controller;
              setLoading(true);
              try {
                const rolls = await loadNarrativeRolls({ signal: controller.signal });
                if (!controller.signal.aborted) { setNarrativeRolls(rolls); setLibraryError(''); }
              } catch (error) { if (!controller.signal.aborted) setLibraryError(error.message); }
              finally { if (!controller.signal.aborted) setLoading(false); }
            }}>重新读取书架</button></div>}

          <div className="narrative-shelf-shell">
            <div
              className="narrative-shelf"
              role="list"
              aria-label="叙事卷书架"
            >
              {narrativeRolls.map((roll, index) => (
                <div className="narrative-book-slot" role="presentation" key={roll.id}>
                <button
                  className="narrative-book"
                  type="button"
                  role="listitem"
                  style={{
                    "--roll-index": index % 5,
                    ...bookAppearance(roll.sourceCount ?? roll.sources?.length ?? 0, index),
                    viewTransitionName: `roll-${roll.id}`,
                  }}
                  onClick={() => openRoll(roll.id)}
                  aria-label={`打开第 ${roll.volume} 卷：${roll.title}，${roll.subtitle}`}
                >
                  <span className="narrative-book__page narrative-book__page--one" aria-hidden="true" />
                  <span className="narrative-book__page narrative-book__page--two" aria-hidden="true" />
                  <span className="narrative-book__page narrative-book__page--three" aria-hidden="true" />
                  <span className="narrative-book__cover">
                    <span className="narrative-book__volume">VOL. {roll.volume}</span>
                    <span
                      className={`narrative-book__title${/[A-Za-z]/u.test(roll.spineTitle) ? " narrative-book__title--latin" : ""}`}
                    >
                      {roll.spineTitle}
                    </span>
                  <span className="narrative-book__subtitle">{roll.isUnwritten ? "叙事线 · 攒材料中" : roll.spineSubtitle}</span>
                    <span className="narrative-book__footer">
                      <span>{roll.timeStart}</span>
                      <ArrowRight size={15} weight="light" aria-hidden="true" />
                    </span>
                  </span>
                </button>
                </div>
              ))}

              <div className="narrative-book-slot" role="presentation">
              <button type="button" className="narrative-book narrative-book--future" role="listitem"
                aria-label="增加叙事卷" onClick={() => setCreateOpen(true)}>
                <CircleNotch size={17} weight="light" aria-hidden="true" />
                <span>尚未命名</span>
                <small>从一个主题开始</small>
              </button>
              </div>
            </div>
            <p className="narrative-shelf__hint">每一本，都还在生长</p>
          </div>
        </section>
      ) : (
        <article className="narrative-reader" aria-labelledby="narrative-reader-title">
          <nav className="narrative-reader__topbar" aria-label="叙事卷阅读导航">
            <button className="narrative-reader__back" type="button" disabled={previewing || saving || deleting || writingNew} onClick={closeRoll}>
              <ArrowLeft size={18} weight="light" aria-hidden="true" />
              回到书架
            </button>
            <div className="narrative-reader__tools">
              <span>
                VOL. {selectedRoll.volume}
                <i aria-hidden="true">·</i>
                {selectedRoll.status}
              </span>
              <div className="narrative-reader__actions" role="group" aria-label="叙事卷操作">
                <button
                  className={editorOpen && !previewMode ? "is-active" : ""}
                  type="button"
                  disabled={previewing || saving || deleting}
                  onClick={openEditor}
                >
                  <PencilSimple size={15} weight="light" aria-hidden="true" />
                  编辑
                </button>
                <button
                  className={previewMode === "update" ? "is-active" : ""}
                  type="button"
                  disabled={previewing || saving || deleting || selectedRoll.isUnwritten}
                  onClick={() => generatePreview("update")}
                >
                  {previewing && previewMode === "update" ? <CircleNotch className="is-spinning" size={15} aria-hidden="true" /> : null}
                  更新
                </button>
                <button
                  className={previewMode === "rewrite" ? "is-active" : ""}
                  type="button"
                  disabled={previewing || saving || deleting}
                  onClick={() => selectedRoll.isUnwritten ? writeNewRoll() : generatePreview("rewrite")}
                >
                  {previewing && previewMode === "rewrite" ? <CircleNotch className="is-spinning" size={15} aria-hidden="true" /> : null}
                  {selectedRoll.isUnwritten ? "书写" : "重写"}
                </button>
                <button type="button" disabled={previewing || saving || writingNew || uploading || deleting} onClick={deleteRoll}>
                  <Trash size={15} weight="light" aria-hidden="true" />{deleting ? '正在删除…' : '删除'}
                </button>
                {editorOpen ? (
                  <button className="is-close" type="button" disabled={previewing || saving || deleting || writingNew} aria-label="关闭编辑区" onClick={closeEditor}>
                    <X size={15} weight="light" aria-hidden="true" />
                  </button>
                ) : null}
              </div>
            </div>
          </nav>

          <section
            className="narrative-reader__article"
            style={{ viewTransitionName: `roll-${selectedRoll.id}` }}
          >
            {deleteError && <p className="narrative-editor__error" role="alert" style={{ whiteSpace: 'pre-line' }}>{deleteError}</p>}
            <header className="narrative-reader__article-header">
              <span>
                NARRATIVE PROJECTION · {selectedRoll.scope?.toUpperCase() ?? "ARC"} · VOL. {selectedRoll.volume}
              </span>
              <h1 id="narrative-reader-title">{selectedRoll.title}</h1>
              {titleEditing ? <form className="narrative-title-edit" onSubmit={saveTitle}>
                <label htmlFor="narrative-book-title">书名</label>
                <input id="narrative-book-title" autoFocus value={titleInput} disabled={saving}
                  onChange={event => setTitleInput(Array.from(event.target.value).slice(0, 16).join(''))} />
                <small>{Array.from(titleInput).length}/16</small>
                <button type="submit" disabled={saving || !titleInput.trim() || Array.from(titleInput.trim()).length > 16}>{saving ? '正在保存…' : '保存书名'}</button>
                <button type="button" disabled={saving} onClick={() => { setTitleEditing(false); setTitleError(''); }}>取消</button>
              </form> : <button type="button" className="narrative-reader__rename"
                disabled={editorOpen || previewing || saving || deleting || writingNew || uploading}
                title={editorOpen ? '关闭正文编辑区后可修改书名' : undefined}
                onClick={() => { setTitleInput(selectedRoll.title); setTitleEditing(true); setTitleError(''); }}>改书名</button>}
              {titleError && <p role="alert" className="narrative-editor__error" style={{ whiteSpace: 'pre-line' }}>{titleError}</p>}
              <p>{selectedRoll.subtitle}</p>
              <small>{selectedRoll.description}</small>
              <dl className="narrative-reader__facts">
                <div>
                  <dt>状态</dt>
                  <dd>{selectedRoll.isUnwritten ? "叙事线 · 尚未书写" : selectedRoll.projectionStatus ?? selectedRoll.status}</dd>
                </div>
                <div>
                  <dt>证据范围</dt>
                  <dd>{formatRange(selectedRoll)}</dd>
                </div>
                <div>
                  <dt>来源</dt>
                  <dd>{selectedRoll.sourceCount ?? selectedRoll.sceneCount} 条</dd>
                </div>
              </dl>
            </header>

            {editorOpen ? (
              <section className="narrative-editor" aria-label="叙事卷编辑预览">
                <header>
                  <div>
                    <span>{previewMode ? "WRITER PREVIEW" : "BODY EDITOR"}</span>
                    <h2>{selectedRoll.isUnwritten && previewMode === 'rewrite' ? '书写正文' : previewMode ? (previewMode === "update" ? "更新预览" : previewMode === "rewrite" ? "重写预览" : "保存前校验") : "手改正文"}</h2>
                  </div>
                  <small>{writingNew ? "正在书写，完成后保存成卷；材料不足时会保留叙事线。" : selectedRoll.isUnwritten ? '材料已保存；书写成功后自动保存正文。' : previewMode ? "预览不会自动发布，确认后再保存" : "保存只改正文，不调用 Writer"}</small>
                </header>
                {previewError ? <p className="narrative-editor__error" role="alert" style={{ whiteSpace: 'pre-line' }}>{previewError}</p> : null}
                <div className="narrative-editor__workspace">
                  <div className="narrative-editor__paper">
                    <textarea
                      aria-label="叙事卷正文预览"
                      value={previewBody}
                      disabled={previewing || saving || writingNew || deleting}
                      onChange={(event) => {
                        setPreviewBody(event.target.value);
                        setSaveMessage("");
                        setPreviewSeal(null);
                      }}
                      spellCheck="false"
                    />
                    {saveMessage ? <p className="narrative-editor__saved" role="status">{saveMessage}</p> : null}
                  </div>
                  <aside className="narrative-editor__materials" aria-label="当前绑定材料">
                    <span>拟绑定材料</span>
                    <strong>{editableMaterials.length} 条</strong>
                    <ol>
                      {editableMaterials.map((source) => (
                        <li key={`editor:${source.key}:${source.id}`}>
                          <em>{source.typeLabel}</em>
                          <span>{source.title}<small style={{display:"block",overflowWrap:"anywhere"}}>{source.date ? `${source.date} · ` : ""}ID：{source.id}</small></span>
                          <button type="button" disabled={saving || previewing || writingNew || uploading} onClick={() => removeMaterial(source.key, source.id)}>移除</button>
                        </li>
                      ))}
                    </ol>
                    <div className="narrative-editor__material-add">
                      <select value={materialType} disabled={saving || previewing || writingNew || uploading} onChange={(event) => { setMaterialType(event.target.value); setMaterialIdInput(""); }} aria-label="材料类型">
                        <option value="event_ids">Event</option>
                        <option value="scene_ids">Scene</option>
                        <option value="diary_ids">日记</option>
                        <option value="darkroom_ids">暗房</option>
                        <option value="upload_ids">已上传文件</option>
                      </select>
                      <LinkCandidatePicker key={`${selectedRoll.id}:${materialType}`} kind={materialType.replace(/_ids$/, "")}
                        label="查找叙事卷材料" disabled={saving || previewing || writingNew || uploading}
                        blockedIds={(materialIds || selectedRoll.materialIds)?.[materialType] || []} onSelect={addMaterial} />
                      <input value={materialIdInput} disabled={saving || previewing || writingNew || uploading} onChange={(event) => setMaterialIdInput(event.target.value)} placeholder="也可输入精确 ID" aria-label="新增材料 ID" />
                      <button type="button" disabled={saving || previewing || writingNew || uploading || !materialIdInput.trim()} onClick={() => addMaterial()}>按 ID 加入拟绑定</button>
                    </div>
                    <label className="narrative-editor__material-upload">
                      <input type="file" onChange={uploadLocalMaterial} disabled={uploading || saving || previewing || writingNew} />
                      <span>{uploading ? "正在上传…" : "从本地上传材料"}</span>
                      <small>保留原文件；文字类文件会同时提供给 Writer 阅读，单个不超过 10 MB。</small>
                    </label>
                    {previewSeal?.material_delta ? (
                      <div className="narrative-editor__material-delta" role="status">
                        <strong>本次材料变更</strong>
                        {Object.entries(previewSeal.material_delta.added || {}).flatMap(([key, ids]) => ids.map((id) => <span className="is-added" key={`added:${key}:${id}`}>＋ {id}</span>))}
                        {Object.entries(previewSeal.material_delta.removed || {}).flatMap(([key, ids]) => ids.map((id) => <span className="is-removed" key={`removed:${key}:${id}`}>－ {id}</span>))}
                        {!Object.values(previewSeal.material_delta.added || {}).some((ids) => ids.length) && !Object.values(previewSeal.material_delta.removed || {}).some((ids) => ids.length) ? <span>材料不变</span> : null}
                      </div>
                    ) : null}
                  </aside>
                </div>
                {previewDiff ? (
                  <details className="narrative-editor__diff">
                    <summary>查看与当前正文的差异</summary>
                    <pre>{previewDiff}</pre>
                  </details>
                ) : null}
                <footer>
                  {selectedRoll.isUnwritten && <button type="button"
                    disabled={saving || previewing} onClick={saveLineMaterials}>只保存材料</button>}
                  <button className="is-primary" type="button" disabled={saving || previewing || !previewBody.trim()} onClick={saveBody}>
                    {saving ? <CircleNotch className="is-spinning" size={16} aria-hidden="true" /> : null}
                    {previewSeal ? "确认保存" : "校验变更"}
                  </button>
                </footer>
              </section>
            ) : (
              <section className="narrative-manuscript__body" aria-label="叙事正文">
                {selectedRoll.isUnwritten && <p>材料已留在这里，正文还没有写。可以继续攒材料，准备好时再书写。</p>}
                {selectedRoll.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
              </section>
            )}

            {!editorOpen && selectedRoll.sources ? (
              <details className="narrative-ledger">
                <summary>
                  <span>
                    <strong>来源账</strong>
                    <small>{selectedRoll.sources.length} 条绑定材料</small>
                  </span>
                  <CaretDown size={17} weight="light" aria-hidden="true" />
                </summary>
                <ol>
                  {selectedRoll.sources.map((source) => (
                    <li key={`${source.type || "scene"}:${source.id}`}>
                      <time>{source.date || "—"}</time>
                      <span>
                        <span className="narrative-ledger__source-title">
                          <em>{source.typeLabel || "Scene"}</em>
                          <strong>{source.title}</strong>
                        </span>
                        <small>{source.purpose || source.id}</small>
                      </span>
                    </li>
                  ))}
                </ol>
              </details>
            ) : !editorOpen ? (
              <section className="narrative-scenes" aria-labelledby="narrative-scenes-title">
                <div>
                  <h2 id="narrative-scenes-title">卷中的 Scene</h2>
                  <span>{selectedRoll.sourceCount ?? selectedRoll.sceneCount} 条来源</span>
                </div>
                <ul>
                  {selectedRoll.sceneNames.map((scene) => <li key={scene}>{scene}</li>)}
                </ul>
              </section>
            ) : null}
          </section>
        </article>
      )}
    </div>
  );
}
