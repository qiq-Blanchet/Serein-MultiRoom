import { roomLocalStorage } from "../storage/roomScopedStorage.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Check,
  DownloadSimple,
  Eye,
  Plus,
  Question,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { useRecallObservationFeed } from '../hooks/useRecallObservationFeed.js';
import './recall-observation-live.css';
import { semanticRouteSnapshot } from "../data/basement.js";
import {
  appendSemanticRouteDraftExample,
  inspectServerSemanticRouteDraft,
  readRecallObservationReviews,
  readSemanticRouteDraft,
  readServerSemanticRouteDraft,
  saveRecallObservationReviews,
  saveSemanticRouteDraft,
  saveServerSemanticRouteDraft,
} from "../storage/basementStore.js";
import { buildRecallObservationTrainingExport } from "../storage/recallObservationExport.js";
import {
  mergeObservationRows,
  recallObservationPageLimits,
} from "../storage/recallObservationPagination.js";
import { readRecallSimulationTrainingLabels } from "../storage/recallSimulationTraining.js";
import { initializePersonal, loadPersonalScope } from "../storage/personalStore.js";
import { resolveBridgeObservationOutcome, resolveGatewayObservationOutcome, gatewayRequestLabel } from "../recallObservationOutcome.js";

const snapshotRouteLabels = Object.fromEntries(
  semanticRouteSnapshot.routes.map((route) => [route.name, route.label || route.name]),
);
const snapshotRouteActions = Object.fromEntries(
  semanticRouteSnapshot.routes.map((route) => [route.name, route.action || ""]),
);

const outcomeLabels = {
  injected: "已注入",
  no_match: "未命中",
  skip: "已跳过",
  failed: "失败／中断",
  pending: "未完成",
};

const sourceLabels = {
  gateway: "Gateway 当前请求",
  hook: "Hook 注入",
};

const verdicts = [
  { key: "correct", label: "正确", icon: Check },
  { key: "false_positive", label: "误召", icon: X },
  { key: "missed", label: "漏召", icon: Plus },
  { key: "uncertain", label: "不确定", icon: Question },
];

const routeVerdicts = [
  { key: "correct", label: "正确", icon: Check },
  { key: "incorrect", label: "错误", icon: X },
  { key: "uncertain", label: "不确定", icon: Question },
];

const candidateRelevances = [
  { key: "core", label: "核心相关" },
  { key: "weak", label: "弱相关" },
  { key: "irrelevant", label: "无关" },
];

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function normalizeScore(item) {
  const raw = item?.score?.final ?? item?.score?.semantic ?? item?.score?.keyword ?? item?.score;
  if (raw === null || raw === undefined || raw === "") return "";
  const score = Number(raw);
  if (!Number.isFinite(score)) return "";
  return score <= 1 ? `${(score * 100).toFixed(1)}%` : score.toFixed(2);
}

function normalizeScoreValue(item) {
  const raw = item?.score?.final ?? item?.score?.semantic ?? item?.score?.keyword ?? item?.score;
  if (raw === null || raw === undefined || raw === "") return null;
  const score = Number(raw);
  return Number.isFinite(score) ? score : null;
}

function normalizeObservation(row) {
  const payload = row?.payload && typeof row.payload === "object" ? row.payload : {};
  const semantic = payload.semantic_recall_debug && typeof payload.semantic_recall_debug === "object"
    ? payload.semantic_recall_debug
    : {};
  const why = payload.recall_why_summary && typeof payload.recall_why_summary === "object"
    ? payload.recall_why_summary
    : {};
  const injectedDetails = asArray(why.injected);
  const injectedIds = asArray(payload.injected_bucket_ids);
  const detailsById = new Map();
  for (const item of [...asArray(payload.prepared_items), ...injectedDetails]) {
    const id = item.bucket_id || item.id;
    detailsById.set(id, { ...detailsById.get(id), ...item });
  }
  // Current requests use confirmed IDs as the authority. Prepared candidates
  // must not appear as delivered merely because details were recorded.
  const deliveredItems = payload.observation_version || injectedIds.length
    ? injectedIds.map(id => ({ ...detailsById.get(id), id })) : injectedDetails;
  const injected = deliveredItems.map((item) => ({
      id: item.bucket_id || item.id || "",
      title: item.bucket_name || item.title || item.bucket_id || item.id || "未命名记忆",
      score: normalizeScore(asArray(item.evidence)[0] || item),
      scoreValue: normalizeScoreValue(asArray(item.evidence)[0] || item),
      sourceKind: String(item.source_kind || "").trim(),
    }));
  const action = String(semantic.applied_action || semantic.action || "").trim();
  const query = String(payload.query || payload.query_preview || payload.original_query || payload.user_query || "").trim();
  const outcome = resolveGatewayObservationOutcome(payload, action === "skip" ? "skip" : injected.length ? "injected" : "no_match");
  const confidence = Number(semantic.confidence);
  return {
    id: String(row?.id ?? `${row?.session_id || "session"}-${row?.round_id || "round"}`),
    createdAt: row?.created_at || "",
    query: query || (payload.request_kind === 'tool_continuation' ? '工具续轮（没有新的用户原句）' : "旧记录未保留原句"),
    queryAvailable: Boolean(query),
    route: String(semantic.route || "").trim(),
    action,
    observedAction: action,
    sessionId: row?.session_id ?? row?.sessionId ?? payload.session_id ?? "",
    reviewBatchId: row?.review_batch_id ?? row?.reviewBatchId ?? payload.review_batch_id ?? payload.reviewBatchId ?? "",
    confidence: Number.isFinite(confidence) ? `${(confidence * 100).toFixed(1)}%` : "",
    outcome,
    injected,
    source: "gateway",
    requestLabel: gatewayRequestLabel(payload),
    requestKind: payload.request_kind,
    memoryEnabled: payload.memory_enabled,
    prepared: (asArray(payload.prepared_items).length ? asArray(payload.prepared_items)
      : asArray(payload.prepared_ids).map(id => ({id})))
      .map(item => ({id:item.id, title:item.title || item.id, sourceKind:item.source_kind, score:normalizeScore(item)})),
    trigger: String(semantic.reason || "").trim(),
    hookOutcome: "",
  };
}

function normalizeBridgeObservation(row, routeActions = snapshotRouteActions) {
  const injectedIds = asArray(row?.gateway_memory_injected_ids).map((item) => String(item || "").trim()).filter(Boolean);
  const injectedDetails = asArray(row?.gateway_memory_items);
  const detailsById = new Map(injectedDetails.map((item) => [String(item?.id || "").trim(), item]));
  const injected = injectedIds.map((id) => {
    const item = detailsById.get(id) || {};
    return {
      id,
      title: String(item.title || id),
      score: normalizeScore(item),
      scoreValue: normalizeScoreValue(item),
      sourceKind: String(item.source_kind || "").trim(),
    };
  });
  const hookOutcome = String(row?.hook_memory_outcome || "").trim();
  const trigger = String(row?.gateway_memory_trigger || "").trim();
  const route = String(row?.gateway_memory_route || "").trim();
  const routeAction = routeActions[route] || "";
  const query = String(row?.query || "").trim();
  const outcome = resolveBridgeObservationOutcome({ injected, hookOutcome, trigger, routeAction });
  return {
    id: `hook-${row?.id}`,
    createdAt: row?.created_at || "",
    query: query || "原句未记录",
    queryAvailable: Boolean(query),
    route,
    action: routeAction,
    observedAction: routeAction,
    sessionId: row?.session_id ?? row?.sessionId ?? "",
    reviewBatchId: row?.review_batch_id ?? row?.reviewBatchId ?? "",
    confidence: "",
    outcome,
    injected,
    source: "hook",
    trigger,
    hookOutcome,
    messageId: row?.id,
  };
}

function injectedMemoryKind(memory) {
  const kind = String(memory?.sourceKind || "").trim().toLowerCase();
  if (kind === "event" || String(memory?.id || "").startsWith("event:")) return "Event";
  if (kind === "scene" || String(memory?.id || "").startsWith("scene:")) return "Scene";
  return "";
}

function formatObservedAt(value) {
  if (!value) return "时间未记录";
  const raw = String(value).trim();
  const sqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw);
  const date = new Date(sqliteUtc ? `${raw.replace(" ", "T")}Z` : raw);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function BasementRecallObservation() {
  const [draftRoutes, setDraftRoutes] = useState(readSemanticRouteDraft);
  const [publishedRoutes, setPublishedRoutes] = useState(semanticRouteSnapshot.routes);
  const [draftDatasetVersion, setDraftDatasetVersion] = useState(semanticRouteSnapshot.datasetVersion);
  const [draftConflict, setDraftConflict] = useState(null);
  const draftRevisionRef = useRef(0);
  const rootRef = useRef(null);
  const bottomRef = useRef(null);
  const [reviewsReady, setReviewsReady] = useState(false);
  const [source, setSource] = useState("gateway");
  const [filter, setFilter] = useState("all");
  const [reviews, setReviews] = useState(readRecallObservationReviews);
  const [draftForms, setDraftForms] = useState({});
  const [draftNotices, setDraftNotices] = useState({});
  const [exportNotice, setExportNotice] = useState("");
  const [manualSimulations, setManualSimulations] = useState(readRecallSimulationTrainingLabels);

  const { feeds, refresh: load, loadEarlier, reveal } = useRecallObservationFeed({
    source, reviews, ready: reviewsReady, rootRef, bottomRef,
  });
  const datasets = useMemo(() => ({ hook: feeds.hook.rows, gateway: feeds.gateway.rows }), [feeds]);
  const errors = useMemo(() => Object.fromEntries(Object.entries(feeds).filter(([, feed]) => feed.error).map(([key, feed]) => [key, feed.error])), [feeds]);
  const pagination = useMemo(() => Object.fromEntries(Object.entries(feeds).map(([key, feed]) => [key, {
    hasMore: feed.hasMore, nextBeforeId: feed.nextBeforeId, recoveredCount: feed.reviewed.length,
  }])), [feeds]);
  const pageLoading = { hook: feeds.hook.loadingEarlier, gateway: feeds.gateway.loadingEarlier };
  const refreshing = feeds[source].loading;
  const status = !reviewsReady || (!feeds.hook.loaded && !feeds.gateway.loaded && !Object.keys(errors).length)
    ? 'loading' : feeds.hook.loaded || feeds.gateway.loaded ? 'done' : 'error';

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await initializePersonal();
        const rows = await loadPersonalScope('recall_review');
        if (cancelled) return;
        roomLocalStorage().setItem('serein.basement.recall-observation-review.v1', JSON.stringify(Object.fromEntries(rows.map(row => [row.key, row.value]))));
        setManualSimulations(readRecallSimulationTrainingLabels());
      } catch (error) { if (!cancelled) setExportNotice(error.message); }
      if (!cancelled) { setReviews(readRecallObservationReviews()); setReviewsReady(true); }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const refreshManualSimulations = () => setManualSimulations(readRecallSimulationTrainingLabels());
    window.addEventListener("serein:recall-simulation-training-updated", refreshManualSimulations);
    return () => window.removeEventListener("serein:recall-simulation-training-updated", refreshManualSimulations);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const hydrateDraftRoutes = async () => {
      try {
        const [publishedResponse, serverState] = await Promise.all([
          fetch("/__serein/gateway/semantic-routes"),
          readServerSemanticRouteDraft(),
        ]);
        const published = await publishedResponse.json();
        if (!publishedResponse.ok || !Array.isArray(published.routes)) throw new Error("route_dataset_unavailable");
        const datasetVersion = Number(published.dataset_version);
        const publishedRoutes = published.routes.map((route) => ({
          ...route,
          label: route.label || route.name,
          enabled: route.enabled !== false,
          utterances: Array.isArray(route.utterances) ? route.utterances : [],
        }));
        if (!cancelled) setPublishedRoutes(publishedRoutes);
        const serverDraftState = inspectServerSemanticRouteDraft(serverState, datasetVersion);
        let nextRoutes;
        if (serverDraftState.status === "current") {
          nextRoutes = serverDraftState.draft.routes;
          draftRevisionRef.current = serverDraftState.draft.revision || 0;
          if (!cancelled) setDraftConflict(null);
        } else if (serverDraftState.status === "conflict") {
          nextRoutes = publishedRoutes;
          draftRevisionRef.current = serverDraftState.draft.revision || 0;
          if (!cancelled) setDraftConflict({
            baseDatasetVersion: serverDraftState.baseDatasetVersion,
            datasetVersion,
          });
        } else {
          if (!cancelled) setDraftConflict(null);
          const snapshot = { ...semanticRouteSnapshot, datasetVersion, routes: publishedRoutes };
          nextRoutes = readSemanticRouteDraft(snapshot);
          if (JSON.stringify(nextRoutes) !== JSON.stringify(publishedRoutes)) {
            const migrated = await saveServerSemanticRouteDraft(nextRoutes, datasetVersion, 0);
            draftRevisionRef.current = migrated.draft.revision;
          } else {
            draftRevisionRef.current = 0;
          }
        }
        if (cancelled) return;
        saveSemanticRouteDraft(nextRoutes, datasetVersion);
        setDraftDatasetVersion(datasetVersion);
        setDraftRoutes(nextRoutes);
      } catch {
        // Keep the local draft available when the private server bridge is temporarily unreachable.
      }
    };
    hydrateDraftRoutes();
    return () => { cancelled = true; };
  }, []);

  const publishedRouteActions = useMemo(() => ({
    ...snapshotRouteActions,
    ...Object.fromEntries(publishedRoutes.map((route) => [route.name, route.action || ""])),
  }), [publishedRoutes]);
  const publishedRouteLabels = useMemo(() => ({
    ...snapshotRouteLabels,
    ...Object.fromEntries(publishedRoutes.map((route) => [route.name, route.label || route.name])),
  }), [publishedRoutes]);
  const normalizedDatasets = useMemo(() => ({
    hook: asArray(datasets.hook).map((row) => normalizeBridgeObservation(row, publishedRouteActions)),
    gateway: asArray(datasets.gateway).map(normalizeObservation),
  }), [datasets, publishedRouteActions]);
  const items = normalizedDatasets[source];
  const exportItems = useMemo(
    () => [
      ...mergeObservationRows(feeds.hook.reviewed, datasets.hook).map(row => normalizeBridgeObservation(row, publishedRouteActions)),
      ...mergeObservationRows(feeds.gateway.reviewed, datasets.gateway).map(normalizeObservation),
    ].filter(item => !['pending', 'failed'].includes(item.outcome)),
    [feeds, datasets, publishedRouteActions],
  );
  const exportPayload = useMemo(
    () => buildRecallObservationTrainingExport(exportItems, reviews, new Date().toISOString(), manualSimulations),
    [exportItems, reviews, manualSimulations],
  );
  const exportSummary = exportPayload.summary;
  const filteredItems = useMemo(
    () => filter === "all" ? items : items.filter((item) => item.outcome === filter),
    [filter, items],
  );

  const persistReviews = async (nextReviews) => {
    try { setReviews(await saveRecallObservationReviews(nextReviews)); setExportNotice(""); }
    catch (error) { setExportNotice(error.message); }
  };

  const setVerdict = async (item, verdict) => {
    const nextReviews = {
      ...reviews,
      [item.id]: {
        ...(reviews[item.id] || {}),
        verdict,
        observedAt: item.createdAt,
        query: item.query,
        updatedAt: new Date().toISOString(),
      },
    };
    await persistReviews(nextReviews);
    if (["false_positive", "missed"].includes(verdict)) {
      setDraftForms((current) => ({
        ...current,
        [item.id]: current[item.id] || { routeName: "", role: "typical" },
      }));
    }
  };

  const setCandidateRelevance = async (item, memoryId, relevance) => {
    if (!memoryId) return;
    const currentReview = reviews[item.id] || {};
    const candidateReviews = { ...(currentReview.candidateReviews || {}) };
    if (candidateReviews[memoryId] === relevance) delete candidateReviews[memoryId];
    else candidateReviews[memoryId] = relevance;
    const nextReviews = {
      ...reviews,
      [item.id]: {
        ...currentReview,
        candidateReviews,
        observedAt: item.createdAt,
        query: item.query,
        updatedAt: new Date().toISOString(),
      },
    };
    await persistReviews(nextReviews);
  };

  const setRouteVerdict = async (item, routeVerdict) => {
    const currentReview = reviews[item.id] || {};
    const nextReview = {
      ...currentReview,
      routeVerdict,
      observedAt: item.createdAt,
      query: item.query,
      updatedAt: new Date().toISOString(),
    };
    if (routeVerdict !== "incorrect") delete nextReview.expectedRoute;
    const nextReviews = { ...reviews, [item.id]: nextReview };
    await persistReviews(nextReviews);
  };

  const setExpectedRoute = async (item, expectedRoute) => {
    const currentReview = reviews[item.id] || {};
    const nextReviews = {
      ...reviews,
      [item.id]: {
        ...currentReview,
        routeVerdict: "incorrect",
        expectedRoute,
        observedAt: item.createdAt,
        query: item.query,
        updatedAt: new Date().toISOString(),
      },
    };
    await persistReviews(nextReviews);
    setDraftForms((current) => ({
      ...current,
      [item.id]: { ...(current[item.id] || { role: "typical" }), routeName: expectedRoute },
    }));
  };

  const addDraft = async (item) => {
    if (draftConflict) {
      setDraftNotices((current) => ({
        ...current,
        [item.id]: `服务器仍保留 v${draftConflict.baseDatasetVersion} 草稿；先在例句维护处理与 v${draftConflict.datasetVersion} 的冲突。`,
      }));
      return;
    }
    const form = draftForms[item.id] || {};
    if (!form.routeName) {
      setDraftNotices((current) => ({ ...current, [item.id]: "先选这句话应该属于哪条路线。" }));
      return;
    }
    const verdict = reviews[item.id]?.verdict;
    const result = appendSemanticRouteDraftExample({
      routeName: form.routeName,
      text: item.query,
      role: form.role,
      origin: verdict === "missed" ? "online_false_negative" : "online_false_positive",
      routes: draftRoutes,
      baseDatasetVersion: draftDatasetVersion,
    });
    setDraftRoutes(result.routes);
    let status = result.status;
    if (status === "added") {
      try {
        const saved = await saveServerSemanticRouteDraft(
          result.routes,
          draftDatasetVersion,
          draftRevisionRef.current,
        );
        draftRevisionRef.current = saved.draft.revision;
      } catch (error) {
        status = error.message === "route_draft_revision_conflict" ? "conflict" : "save_failed";
      }
    }
    const notice = status === "added"
      ? "已进入服务器例句草稿；发布前不会改变线上 Router。"
      : status === "duplicate" ? "这句已经在例句草稿或生产快照里。"
        : status === "conflict" ? "服务器草稿刚在别处变化，重新打开召回观察后再转入。"
          : status === "save_failed" ? "只保住了当前浏览器草稿，服务器暂时没有保存成功。"
            : "这条记录暂时不能转成草稿。";
    setDraftNotices((current) => ({ ...current, [item.id]: notice }));
  };

  const counts = useMemo(() => ({
    injected: items.filter((item) => item.outcome === "injected").length,
    no_match: items.filter((item) => item.outcome === "no_match").length,
    skip: items.filter((item) => item.outcome === "skip").length,
    pending: items.filter(item => item.outcome === "pending").length,
    failed: items.filter(item => item.outcome === "failed").length,
  }), [items]);

  const downloadTrainingExport = useCallback(() => {
    if (!exportSummary.total_cases) {
      setExportNotice("当前没有已加载的观察记录，暂时没有可导出的内容。");
      return;
    }
    const blob = new Blob([JSON.stringify(exportPayload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    anchor.href = url;
    anchor.download = `serein-recall-training-${stamp}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    setExportNotice(
      `已导出当前加载窗口：Hook ${exportSummary.loaded_hook_observations}、Gateway ${exportSummary.loaded_gateway_observations}、模拟 ${exportSummary.total_manual_simulations}。`,
    );
  }, [exportPayload, exportSummary]);

  return (
    <section ref={rootRef} className="basement-workbench" aria-labelledby="recall-observation-title">
      <header className="basement-workbench__header">
        <div>
          <span className="basement-kicker">真实运行，只读观察</span>
          <h2 id="recall-observation-title">召回观察</h2>
          <p>记录用户轮的准备、完成与中断状态；准备的记忆不等于成功交付。工具续轮不重复记账。</p>
        </div>
        <div className="observation-header-actions">
          <button className="observation-refresh" type="button" onClick={load} disabled={refreshing || pageLoading[source]}>
            <ArrowClockwise size={16} className={refreshing ? "is-spinning" : ""} aria-hidden="true" />
            刷新
          </button>
          <button
            className="observation-export-button"
            type="button"
            title="只导出当前已加载的观察记录与人工模拟，不含完整 prompt、Scene 正文、cue、证据原文或上下文。"
            onClick={downloadTrainingExport}
            disabled={!exportSummary.total_cases || status === "loading" || pageLoading.hook || pageLoading.gateway}
          >
            <DownloadSimple size={16} aria-hidden="true" />
            导出训练标注
          </button>
        </div>
      </header>

      <div className="observation-toolbar" role="group" aria-label="召回结果筛选">
        <div className="observation-source-switch" role="group" aria-label="观测来源">
          {Object.entries(sourceLabels).map(([key, label]) => (
            <button type="button" className={source === key ? "is-active" : ""} key={key} onClick={() => setSource(key)}>
              {label}<span>{datasets[key].length}</span>
            </button>
          ))}
        </div>
        <i aria-hidden="true" />
        {[
          ["injected", "已注入", counts.injected],
          ["no_match", "未命中", counts.no_match],
          ["skip", "已跳过", counts.skip],
          ["pending", "进行中／未完成", counts.pending],
          ["failed", "失败／中断", counts.failed],
          ["all", "全部", items.length],
        ].map(([key, label, count]) => (
          <button type="button" className={filter === key ? "is-active" : ""} key={key} onClick={() => setFilter(key)}>
            {label}<span>{count}</span>
          </button>
        ))}
        <p>{source === "hook" ? "这里显示 Hook 的完成记录；按实际结果区分已注入、未命中与跳过，不代表客户端已显示回复。" : "当前来源每次加载 20 条，页面可见时自动更新；失败和中断不计成功交付，迁入的旧记录标为历史记录。"}</p>
      </div>

      {feeds[source].buffered.length > 0 && (
        <div className="observation-live-notice" role="status">
          <button type="button" onClick={reveal}>有 {feeds[source].buffered.length} 条新记录，点击查看</button>
        </div>
      )}

      {exportNotice && <p className="observation-export-notice" role="status">{exportNotice}</p>}

      {status === "loading" && (
        <div className="observation-loading" aria-live="polite">
          {[0, 1, 2].map((item) => <i key={item} />)}
        </div>
      )}

      {status === "error" && (
        <div className="basement-error" role="alert">
          <WarningCircle size={19} aria-hidden="true" />
          <div><strong>两个观测入口都没有读到</strong><p>{Object.values(errors).join(" / ")}</p></div>
        </div>
      )}

      {status === "done" && errors[source] && (
        <div className="basement-error" role="alert">
          <WarningCircle size={19} aria-hidden="true" />
          <div><strong>{sourceLabels[source]}暂时不可用</strong><p>{errors[source]}</p></div>
        </div>
      )}

      {status === "done" && !filteredItems.length && (
        <div className="basement-empty-state">
          <Eye size={23} weight="light" aria-hidden="true" />
          <span>{source === "hook" && !items.length ? "还没有 hook 注入记录" : "这个筛选里还没有记录"}</span>
          <p>{source === "hook" && !items.length
            ? "启用自动召回后，成功的聊天请求会记录交付结果；关闭召回的请求请看 Gateway 当前请求。"
            : "可以换一个结果类型，或等下一轮真实聊天经过对应入口。"}</p>
        </div>
      )}

      {status === "done" && filteredItems.length > 0 && (
        <div className="observation-list">
          {filteredItems.map((item) => {
            const review = reviews[item.id] || {};
            const preparationOnly = item.source === 'gateway' && ['failed','pending'].includes(item.outcome);
            const memories = preparationOnly ? item.prepared : item.injected;
            const showDraft = ["false_positive", "missed"].includes(review.verdict);
            const form = draftForms[item.id] || { routeName: "", role: "typical" };
            return (
              <article className="observation-card" data-observation-id={item.id} key={item.id}>
                <header>
                  <div>
                    <time>{formatObservedAt(item.createdAt)} · {sourceLabels[item.source]}</time>
                    <h3>{item.query}</h3>
                    {item.requestLabel && <p className="observation-request-status">{item.requestLabel}</p>}
                  </div>
                  <span className={`observation-outcome observation-outcome--${item.outcome}`}>{outcomeLabels[item.outcome]}</span>
                </header>

                <dl className="observation-route-facts">
                  <div><dt>Router 路线</dt><dd>{item.route ? publishedRouteLabels[item.route] || item.route : "未记录／本次未执行"}</dd></div>
                  <div><dt>{item.source === "hook" ? "Gateway trigger" : "动作"}</dt><dd>{item.source === "hook" ? item.trigger || "未记录" : item.action || "未执行"}</dd></div>
                  <div><dt>{item.source === "hook" ? "Hook outcome" : "置信度"}</dt><dd>{item.source === "hook" ? item.hookOutcome || "未记录" : item.confidence || "未记录"}</dd></div>
                </dl>

                <div className="observation-injections">
                  <span>{preparationOnly ? '准备的记忆（尚未确认上游成功）' : '真正送入模型'}</span>
                  {memories.length ? memories.map((memory) => (
                    <div className="observation-memory-row" key={`${item.id}-${memory.id}`}>
                      <strong>
                        {injectedMemoryKind(memory) && <span className="observation-memory-kind">{injectedMemoryKind(memory)}</span>}
                        {memory.title}
                      </strong>
                      <code>{memory.id || "ID 未记录"}</code>
                      <em>{memory.score || "score 未记录"}</em>
                      {item.outcome === "injected" && memory.id && (
                        <div className="observation-memory-review" role="group" aria-label={`记忆相关度：${memory.title}`}>
                          <span>单卡相关度</span>
                          {candidateRelevances.map(({ key, label }) => (
                            <button
                              type="button"
                              className={review.candidateReviews?.[memory.id] === key ? "is-active" : ""}
                              key={key}
                              onClick={() => setCandidateRelevance(item, memory.id, key)}
                            >
                              {label}
                            </button>
                          ))}
                          <button
                            type="button"
                            className="observation-memory-review__replay"
                            disabled
                            title="Gateway 当前没有按 observation_id + candidate_id 强制重放的 simulation-only 接口"
                          >
                            重跑 shadow 校准
                          </button>
                          <small>telemetry unavailable · 不会换成其他候选重跑</small>
                        </div>
                      )}
                    </div>
                  )) : <p>{preparationOnly ? '没有已记录的准备材料。' : '本次没有自动召回的记忆。'}</p>}
                </div>

                <div className="observation-review">
                  <span>召回动作</span>
                  <div role="group" aria-label={`判断：${item.query}`}>
                    {verdicts.map(({ key, label, icon: Icon }) => (
                      <button type="button" className={review.verdict === key ? "is-active" : ""} key={key} disabled={preparationOnly} onClick={() => setVerdict(item, key)}>
                        <Icon size={14} aria-hidden="true" />{label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="observation-review observation-route-review">
                  <span>路线判断</span>
                  <div role="group" aria-label={`路线判断：${item.query}`}>
                    {routeVerdicts.map(({ key, label, icon: Icon }) => (
                      <button type="button" className={review.routeVerdict === key ? "is-active" : ""} key={key} onClick={() => setRouteVerdict(item, key)}>
                        <Icon size={14} aria-hidden="true" />{label}
                      </button>
                    ))}
                  </div>
                  {review.routeVerdict === "incorrect" && (
                    <label>
                      应属路线
                      <select value={review.expectedRoute || ""} onChange={(event) => setExpectedRoute(item, event.target.value)}>
                        <option value="">选择路线</option>
                        {draftRoutes.map((route) => <option value={route.name} key={route.name}>{route.label || route.name}</option>)}
                      </select>
                    </label>
                  )}
                </div>

                {showDraft && (
                  <div className="observation-draft">
                    <div>
                      <label>
                        应属路线
                        <select value={form.routeName} onChange={(event) => setDraftForms((current) => ({
                          ...current,
                          [item.id]: { ...form, routeName: event.target.value },
                        }))}>
                          <option value="">选择路线</option>
                          {draftRoutes.map((route) => <option value={route.name} key={route.name}>{route.label || route.name}</option>)}
                        </select>
                      </label>
                      <label>
                        样本角色
                        <select value={form.role} onChange={(event) => setDraftForms((current) => ({
                          ...current,
                          [item.id]: { ...form, role: event.target.value },
                        }))}>
                          <option value="typical">典型例句</option>
                          <option value="boundary">边界例句</option>
                        </select>
                      </label>
                    </div>
                    <button type="button" onClick={() => addDraft(item)}>转为例句草稿</button>
                    {draftNotices[item.id] && <p>{draftNotices[item.id]}</p>}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {status === "done" && (datasets[source].length > 0 || !errors[source]) && (
        <div ref={bottomRef} className="observation-pagination" aria-live="polite">
          <span>
            已加载 {sourceLabels[source]} {datasets[source].length} 条 · 首屏窗口 {recallObservationPageLimits[source]} 条
            {pagination[source].recoveredCount > 0 ? ` · 回查旧判断 ${pagination[source].recoveredCount} 条` : ""}
          </span>
          <button
            type="button"
            onClick={() => loadEarlier(source)}
            disabled={(!pagination[source].hasMore && !errors[source]) || pageLoading[source]}
          >
            {pageLoading[source]
              ? "正在加载"
              : errors[source] ? "重试加载更早" : pagination[source].hasMore ? "加载更早 20 条" : "已到当前最早"}
          </button>
        </div>
      )}
    </section>
  );
}
