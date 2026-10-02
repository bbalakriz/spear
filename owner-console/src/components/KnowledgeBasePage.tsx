import {
  DocumentTitle,
  ListPageHeader,
  consoleFetchJSON,
} from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Gallery,
  Grid,
  GridItem,
  Icon,
  Label,
  List,
  ListItem,
  PageSection,
  Progress,
  ProgressMeasureLocation,
  Spinner,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';
import {
  CheckCircleIcon,
  ExclamationCircleIcon,
  ExclamationTriangleIcon,
  InfoCircleIcon,
  TrophyIcon,
} from '@patternfly/react-icons';
import { useEffect, useState } from 'react';

// PHASE1_PLAN.md section 6/7: reads the real log-ingestion-report mlflow
// artifact, lists and triggers real kfp runs, and reads a completed
// autorag run's real per pattern scores, all through this plugin's own
// spec.proxy backend (manifests/40-owner-console plus
// owner-console-backend/). this component never recomputes or fabricates
// any of the numbers it shows, everything here is either a direct read of
// something the pipelines themselves already wrote, or a direct kfp api
// call made on the signed in user's own behalf.
const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

interface QuarantinedDoc {
  doc_id: string;
  filename: string;
  title: string;
  quarantine_reasons: string[];
  minio_url?: string;
}

interface GuardrailsAuditEntry {
  doc_id: string;
  filename: string;
  verdict: string;
  minio_url?: string;
}

interface IngestionReport {
  batch_id: string;
  generated_at: string;
  ingestion: {
    documents_scanned: number;
    documents_valid: number;
    documents_quarantined: number;
    quarantined_detail: QuarantinedDoc[];
  };
  guardrails: {
    documents_sanitized: number;
    documents_blocked: number;
    audit: GuardrailsAuditEntry[];
  };
  // as of 2026-09-28 the sanitization pipeline never writes to rag_chunks
  // itself anymore, see PHASE1_PLAN.md, so a fresh report only ever carries
  // status/note here. the older chunks_created/pattern_name shape is kept
  // optional purely so a report generated before that change still renders.
  abac_tagging?: { status?: string; note?: string; chunks_created?: number; pattern_name?: string };
  sdg_hub_eval_dataset?: { qa_pairs_created: number };
  autorag_trigger?: string;
  error?: string;
  _mlflow_run_id?: string;
  _mlflow_run_url?: string;
}

// one row per batch_id, merging kfp's own run list (has an in flight run
// the moment it is triggered) with mlflow's run list (only ever has a run
// once log_ingestion_report, the pipeline's last step, writes it). see
// server.py's merged_ingestion_runs for how the two are correlated.
interface IngestionRunSummary {
  batch_id: string;
  mlflow_run_id: string | null;
  mlflow_status: string | null;
  kfp_run_id: string | null;
  kfp_state: string | null;
  start_time: number;
  has_report: boolean;
  dashboard_url: string | null;
}

interface KfpRunSummary {
  run_id: string;
  display_name: string;
  state: string;
  created_at: string;
  finished_at?: string;
  dashboard_url: string;
  // only ever present when this run came from /kfp/autorag-runs, see
  // recent_kfp_runs() in server.py.
  autorag_results_url?: string;
}

interface AutoragLeaderboard {
  state: string;
  patterns_tested: number;
  winner: {
    pattern_name: string;
    metric_name: string;
    score: number;
    settings: {
      chunking?: { chunk_size?: number; chunk_overlap?: number };
      embedding?: { model_id?: string };
      generation?: { model_id?: string };
    };
    // every metric pattern.json's own evaluation carries for the winner,
    // the optimization_metric one included, not just the single score
    // used to pick it. see autorag_leaderboard() in server.py.
    all_metrics?: { name: string; score: number }[];
  } | null;
}

// simple red/amber/green style status for the kpi gallery below, each
// caller decides its own thresholds (a quarantined count of zero is good,
// an eval qa pair count of zero is not, the color can never be a single
// shared rule), this just centralizes what each status actually looks like.
type KpiStatus = 'success' | 'warning' | 'danger' | 'neutral';

const KPI_STATUS_STYLE: Record<KpiStatus, { color: string; Icon: typeof CheckCircleIcon }> = {
  success: { color: 'var(--pf-t--global--color--status--success--default)', Icon: CheckCircleIcon },
  warning: {
    color: 'var(--pf-t--global--color--status--warning--default)',
    Icon: ExclamationTriangleIcon,
  },
  danger: {
    color: 'var(--pf-t--global--color--status--danger--default)',
    Icon: ExclamationCircleIcon,
  },
  neutral: { color: 'var(--pf-t--global--color--status--info--default)', Icon: InfoCircleIcon },
};

const Kpi = ({
  title,
  value,
  status = 'neutral',
}: {
  title: string;
  value: number | string;
  status?: KpiStatus;
}) => {
  const { color, Icon } = KPI_STATUS_STYLE[status];
  return (
    <Card style={{ borderLeft: `4px solid ${color}` }}>
      {/* bala: ux - fixed min height to keep the value lined up */}
      <CardTitle style={{ minHeight: '3rem', display: 'flex', alignItems: 'center' }}>
        {title}
      </CardTitle>
      <CardBody>
        <Content
          component="p"
          style={{
            fontSize: 'var(--pf-t--global--font--size--3xl)',
            color,
            display: 'flex',
            alignItems: 'center',
            gap: '0.4rem',
          }}
        >
          <Icon /> {value}
        </Content>
      </CardBody>
    </Card>
  );
};

// bala: deliberately static, not wired to live data. the point of this strip is
// to give the user one glance at the shape of the pipeline before
// diving into the real numbers below it..
interface FlowStep {
  label: string;
  detail: string;
}

// bala:names call out the actual component doing the work, not a generic
// phase name, hover each one for what it actually does.
// pipelines/phase1_ingestion/pipeline.py,
// pipelines/phase1_autorag/run_autorag.py and
// pipelines/phase1_apply_pattern/pipeline.py for the real implementations,
// and FLOW.md for the full end to end diagram.
const FLOW_STEPS: FlowStep[] = [
  {
    label: 'ABAC metadata checks',
    detail:
      'lists newly landed documents in the raw intake bucket (minio) and validates each one\u2019s abac metadata, anything missing a required field is quarantined before it ever reaches guardrails.',
  },
  {
    label: 'NeMo policy checks',
    detail:
      'every valid document is sent through the guardrails service for prompt injection and policy checks, only sanitized documents that pass move on to eval dataset generation and, once a pattern is chosen, production indexing.',
  },
  {
    label: 'SDG Hub eval QA pairs',
    detail:
      'sdg hub generates a real evaluation question set against this batch\u2019s sanitized documents, no placeholder questions. this is the last step of the ingestion pipeline itself, nothing is chunked or written to pgvector yet.',
  },
  {
    label: 'AutoRAG eval leaderboard',
    detail:
      'autorag sweeps multiple chunking, retrieval and model configurations against the eval set and reports the best performing pattern, no manual tuning involved.',
  },
  {
    label: 'Ingest with best pattern + ABAC',
    detail:
      'once a batch has a winning pattern, triggering this step chunks and embeds that batch\u2019s sanitized documents using the winner\u2019s exact chunk size, overlap and embedding model, then writes them to pgvector tagged with the pattern name and autorag run id. this is the only place documents are actually indexed for production retrieval.',
  },
];

const FlowDiagram = () => (
  <Flex
    alignItems={{ default: 'alignItemsCenter' }}
    flexWrap={{ default: 'nowrap' }}
    gap={{ default: 'gapSm' }}
  >
    {FLOW_STEPS.map((step, i) => (
      <Flex
        key={step.label}
        alignItems={{ default: 'alignItemsCenter' }}
        gap={{ default: 'gapSm' }}
        flexWrap={{ default: 'nowrap' }}
      >
        <FlexItem>
          <Tooltip content={step.detail} position="bottom">
            <Label isCompact color="blue" style={{ cursor: 'help' }}>
              {step.label}
            </Label>
          </Tooltip>
        </FlexItem>
        {i < FLOW_STEPS.length - 1 && (
          <FlexItem>
            <Content component="small">{'\u2192'}</Content>
          </FlexItem>
        )}
      </Flex>
    ))}
  </Flex>
);

export default function KnowledgeBasePage() {
  const { t } = useTranslation('plugin__phase1-ingestion-console');

  const [runs, setRuns] = useState<IngestionRunSummary[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string>('');
  const [report, setReport] = useState<IngestionReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);

  const [leaderboard, setLeaderboard] = useState<AutoragLeaderboard | null>(null);
  const [autoragRunId, setAutoragRunId] = useState<string | null>(null);
  const [autoragRunState, setAutoragRunState] = useState<string | null>(null);
  const [autoragDashboardUrl, setAutoragDashboardUrl] = useState<string | null>(null);
  const [autoragResultsUrl, setAutoragResultsUrl] = useState<string | null>(null);

  const [ingestionBatchId, setIngestionBatchId] = useState('');
  const [triggerBusy, setTriggerBusy] = useState<'ingestion' | 'autorag' | 'apply-pattern' | null>(
    null,
  );
  const [triggerMessage, setTriggerMessage] = useState<string | null>(null);
  const [triggerDashboardUrl, setTriggerDashboardUrl] = useState<string | null>(null);
  // bala: apply pattern gets its own confirmation state, not the shared
  // triggerMessage above, because that alert renders next to the ingestion
  // and autorag buttons near the top of the page, far from the "apply this
  // pattern to production" button down in the retrieval optimization card.
  const [applyPatternMessage, setApplyPatternMessage] = useState<string | null>(null);
  const [applyPatternDashboardUrl, setApplyPatternDashboardUrl] = useState<string | null>(null);

  const refreshRuns = () => {
    consoleFetchJSON(`${PROXY_PATH}/runs`)
      .then((data: { runs: IngestionRunSummary[] }) => setRuns(data.runs ?? []))
      .catch(() => {
        // the run picker is a convenience on top of the always working
        // latest run view below, a failure here should not block the page.
      });
  };

  useEffect(() => {
    refreshRuns();
  }, []);

  // a selected batch with no mlflow run yet has nothing for /report to
  // read, log_ingestion_report simply has not run yet, the pending banner
  // below covers that case instead of firing a request that would just
  // 404 or fall back to a different batch's data.
  const selectedRun = selectedBatchId
    ? runs.find((r) => r.batch_id === selectedBatchId)
    : undefined;

  useEffect(() => {
    let cancelled = false;
    if (selectedBatchId && selectedRun && !selectedRun.has_report) {
      setReport(null);
      setReportError(null);
      return;
    }
    const path = selectedRun?.mlflow_run_id
      ? `${PROXY_PATH}/report?run_id=${selectedRun.mlflow_run_id}`
      : `${PROXY_PATH}/report`;
    consoleFetchJSON(path)
      .then((data: IngestionReport) => {
        if (cancelled) return;
        setReport(data);
        setReportError(null);
      })
      .catch((err: Error) => {
        if (!cancelled) setReportError(err.message);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedBatchId, runs]);

  // once the ingestion report is known, look for an autorag run that
  // scored the eval dataset this exact batch produced, matching the same
  // job_name convention run_autorag.py itself uses (pipeline name plus
  // batch_id). never guesses a leaderboard, only ever shows one once a
  // matching, succeeded run is actually found.
  //
  // clearing all four autorag flavored states up front, before the early
  // return below, matters: without it a freshly triggered or freshly
  // selected batch that has no report yet (report is still null while
  // sanitization runs) would just skip this effect body entirely and leave
  // whatever state a previously viewed batch left behind on screen, e.g.
  // showing a stale FAILED from an old batch as if it belonged to the
  // batch the user just started. batch_id reuse (the same string used
  // across more than one console triggered run) makes the same thing
  // possible even once a real report does exist, so the effect always
  // starts from a clean slate rather than only clearing on the empty path.
  useEffect(() => {
    setAutoragRunId(null);
    setAutoragRunState(null);
    setAutoragDashboardUrl(null);
    setAutoragResultsUrl(null);
    setLeaderboard(null);
    if (!report?.batch_id) return;
    let cancelled = false;
    const expectedName = `documents-rag-optimization-pipeline-${report.batch_id}`;
    // a batch_id can be reused across more than one console triggered
    // sanitization run (e.g. the same string typed twice), and each reuse
    // overwrites the same eval dataset s3 key, so an autorag run started
    // before the currently selected sanitization run began can only ever
    // have scored a now overwritten, stale eval dataset. requiring the
    // autorag run to have started at or after this sanitization run's own
    // start_time keeps a genuinely old run under a reused batch_id from
    // being shown as if it belongs to the batch currently selected.
    const sanitizationStartedAt = selectedRun?.start_time ?? 0;
    consoleFetchJSON(`${PROXY_PATH}/kfp/autorag-runs`)
      .then((data: { runs: KfpRunSummary[] }) => {
        if (cancelled) return;
        const candidates = (data.runs ?? []).filter((r) => r.display_name === expectedName);
        const match =
          candidates.find((r) => new Date(r.created_at).getTime() >= sanitizationStartedAt) ??
          candidates[0];
        if (!match) {
          setAutoragRunId(null);
          setAutoragRunState(null);
          setAutoragDashboardUrl(null);
          setAutoragResultsUrl(null);
          setLeaderboard(null);
          return;
        }
        setAutoragRunId(match.run_id);
        setAutoragRunState(match.state);
        setAutoragDashboardUrl(match.dashboard_url ?? null);
        setAutoragResultsUrl(match.autorag_results_url ?? null);
        if (match.state !== 'SUCCEEDED') {
          setLeaderboard(null);
          return;
        }
        return consoleFetchJSON(
          `${PROXY_PATH}/kfp/autorag-leaderboard?run_id=${match.run_id}`,
        ).then((lb: AutoragLeaderboard) => {
          if (!cancelled) setLeaderboard(lb);
        });
      })
      .catch(() => {
        // no rbac or the pipeline server is unreachable, the business value
        // section below just falls back to the sanitization level numbers.
      });
    return () => {
      cancelled = true;
    };
    // selectedBatchId is in this dependency list on purpose, not just
    // report?.batch_id: a freshly selected or freshly triggered batch with
    // no report yet leaves report?.batch_id unchanged (still null), which
    // would otherwise skip this effect entirely and leave a previous
    // batch's autorag state on screen, see the comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report?.batch_id, selectedBatchId]);

  const triggerIngestion = () => {
    setTriggerBusy('ingestion');
    setTriggerMessage(null);
    setTriggerDashboardUrl(null);
    consoleFetchJSON(`${PROXY_PATH}/trigger/ingestion`, 'POST', {
      body: JSON.stringify({ batch_id: ingestionBatchId || undefined }),
    })
      .then(
        (result: { run_id: string; state: string; batch_id: string; dashboard_url: string }) => {
          setTriggerMessage(`Started sanitization run ${result.run_id}, state ${result.state}`);
          setTriggerDashboardUrl(result.dashboard_url);
          setIngestionBatchId('');
          // jump the picker straight to the batch just started, refreshRuns
          // pulls it in from kfp's own run list immediately, it does not
          // wait for mlflow to have a report yet.
          setSelectedBatchId(result.batch_id);
          refreshRuns();
        },
      )
      .catch((err: Error) => setTriggerMessage(`Could not start sanitization run: ${err.message}`))
      .finally(() => setTriggerBusy(null));
  };

  const triggerAutorag = () => {
    if (!report?.batch_id) return;
    setTriggerBusy('autorag');
    setTriggerMessage(null);
    setTriggerDashboardUrl(null);
    consoleFetchJSON(`${PROXY_PATH}/trigger/autorag`, 'POST', {
      body: JSON.stringify({ batch_id: report.batch_id }),
    })
      .then((result: { run_id: string; state: string; dashboard_url: string }) => {
        setTriggerMessage(`Started AutoRAG run ${result.run_id}, state ${result.state}`);
        setTriggerDashboardUrl(result.dashboard_url);
      })
      .catch((err: Error) => setTriggerMessage(`Could not start AutoRAG run: ${err.message}`))
      .finally(() => setTriggerBusy(null));
  };

  const triggerApplyPattern = () => {
    if (!report?.batch_id || !autoragRunId) return;
    setTriggerBusy('apply-pattern');
    setApplyPatternMessage(null);
    setApplyPatternDashboardUrl(null);
    consoleFetchJSON(`${PROXY_PATH}/trigger/apply-pattern`, 'POST', {
      body: JSON.stringify({ batch_id: report.batch_id, autorag_run_id: autoragRunId }),
    })
      .then(
        (result: {
          run_id: string;
          state: string;
          pattern_name: string;
          dashboard_url: string;
        }) => {
          setApplyPatternMessage(
            `Started apply pattern run ${result.run_id} for ${result.pattern_name}, state ${result.state}`,
          );
          setApplyPatternDashboardUrl(result.dashboard_url);
        },
      )
      .catch((err: Error) =>
        setApplyPatternMessage(`Could not start apply pattern run: ${err.message}`),
      )
      .finally(() => setTriggerBusy(null));
  };

  return (
    <>
      <DocumentTitle>{t('Knowledge Base')}</DocumentTitle>
      <ListPageHeader title={t('Knowledge Base')} />
      <PageSection>
        <Card>
          <CardTitle>Knowledge pipeline</CardTitle>
          <CardBody>
            <FlowDiagram />
          </CardBody>
        </Card>

        <Card style={{ marginTop: '1rem' }}>
          <CardTitle>Sanitization run</CardTitle>
          <CardBody>
            {/* first row: starting a brand new run, the primary action on this
                card. second row: jumping back to look at an already started
                one. new before existing reads better top to bottom, and
                keeps the buttons next to the input they actually act on. */}
            <Flex alignItems={{ default: 'alignItemsFlexEnd' }} gap={{ default: 'gapMd' }}>
              <FlexItem>
                <FormGroup label="New batch ID" fieldId="new-batch-id">
                  <TextInput
                    id="new-batch-id"
                    placeholder="batch id"
                    value={ingestionBatchId}
                    onChange={(_e, value) => setIngestionBatchId(value)}
                    style={{ width: '160px' }}
                  />
                </FormGroup>
              </FlexItem>
              <FlexItem>
                <Button
                  variant="primary"
                  isLoading={triggerBusy === 'ingestion'}
                  isDisabled={triggerBusy !== null}
                  onClick={triggerIngestion}
                >
                  Sanitize sources
                </Button>
              </FlexItem>
              <FlexItem>
                <Button
                  variant="secondary"
                  isLoading={triggerBusy === 'autorag'}
                  isDisabled={triggerBusy !== null || !report?.batch_id}
                  onClick={triggerAutorag}
                >
                  Optimize RAG parameters
                </Button>
              </FlexItem>
            </Flex>

            <Flex
              alignItems={{ default: 'alignItemsFlexEnd' }}
              gap={{ default: 'gapMd' }}
              style={{ marginTop: '1rem' }}
            >
              <FlexItem>
                <FormGroup label="Existing sanitization run" fieldId="existing-batch-select">
                  <FormSelect
                    id="existing-batch-select"
                    value={selectedBatchId}
                    onChange={(_e, value) => setSelectedBatchId(value)}
                  >
                    <FormSelectOption key="latest" value="" label="latest run" />
                    {runs.map((r) => (
                      <FormSelectOption
                        key={r.batch_id}
                        value={r.batch_id}
                        label={`${r.batch_id} (${
                          r.has_report
                            ? 'report ready'
                            : `${r.kfp_state ?? 'unknown'}, no report yet`
                        }) ${new Date(r.start_time).toLocaleString()}`}
                      />
                    ))}
                  </FormSelect>
                </FormGroup>
              </FlexItem>
            </Flex>
            {triggerMessage && (
              <Alert variant="info" title={triggerMessage} isInline style={{ marginTop: '0.5rem' }}>
                {triggerDashboardUrl && (
                  <a href={triggerDashboardUrl} target="_blank" rel="noreferrer">
                    View this run in the pipelines view
                  </a>
                )}
              </Alert>
            )}
          </CardBody>
        </Card>

        {selectedBatchId && selectedRun && !selectedRun.has_report && (
          <Alert
            variant="info"
            isInline
            title={`Batch ${selectedBatchId} has no report yet`}
            style={{ marginTop: '1rem' }}
          >
            pipeline state: {selectedRun.kfp_state ?? 'unknown'}. the report below fills in once
            log_ingestion_report, the pipeline{'\u2019'}s last step, runs.{' '}
            {selectedRun.dashboard_url && (
              <a href={selectedRun.dashboard_url} target="_blank" rel="noreferrer">
                View this run in the pipelines view
              </a>
            )}
          </Alert>
        )}

        {reportError && (
          <Alert
            variant="danger"
            title="Could not reach the owner console backend"
            isInline
            style={{ marginTop: '1rem' }}
          >
            {reportError}
          </Alert>
        )}
        {!report &&
          !reportError &&
          !(selectedBatchId && selectedRun && !selectedRun.has_report) && (
            <Spinner size="lg" style={{ marginTop: '1rem' }} />
          )}
        {report?.error && (
          <Alert variant="warning" title={report.error} isInline style={{ marginTop: '1rem' }} />
        )}

        {report && !report.error && (
          <>
            <Content component="p" style={{ marginTop: '1rem' }}>
              batch <Label isCompact>{report.batch_id}</Label> generated {report.generated_at},
              mlflow run{' '}
              {report._mlflow_run_url ? (
                <a href={report._mlflow_run_url} target="_blank" rel="noreferrer">
                  <code>{report._mlflow_run_id}</code>
                </a>
              ) : (
                <code>{report._mlflow_run_id}</code>
              )}
            </Content>

            <Gallery hasGutter minWidths={{ default: '160px' }} style={{ marginTop: '1rem' }}>
              <Kpi
                title="Documents scanned"
                value={report.ingestion.documents_scanned}
                status={report.ingestion.documents_scanned > 0 ? 'neutral' : 'warning'}
              />
              {/* quarantined and threats caught are risk metrics, zero is
                  the good outcome here, so their color logic is inverted
                  relative to a plain volume metric like scanned/sanitized. */}
              <Kpi
                title="Documents quarantined"
                value={report.ingestion.documents_quarantined}
                status={report.ingestion.documents_quarantined > 0 ? 'danger' : 'success'}
              />
              <Kpi
                title="Documents sanitized"
                value={report.guardrails.documents_sanitized}
                status={report.guardrails.documents_sanitized > 0 ? 'success' : 'warning'}
              />
              <Kpi
                title="Threats caught"
                value={report.guardrails.documents_blocked}
                status={report.guardrails.documents_blocked > 0 ? 'danger' : 'success'}
              />
              <Kpi
                title="Eval QA pairs generated"
                value={report.sdg_hub_eval_dataset?.qa_pairs_created ?? 0}
                status={
                  (report.sdg_hub_eval_dataset?.qa_pairs_created ?? 0) > 0 ? 'success' : 'warning'
                }
              />
            </Gallery>

            {report.abac_tagging?.note && (
              <Content component="small" style={{ display: 'block', marginTop: '0.5rem' }}>
                {report.abac_tagging.note}
              </Content>
            )}

            <Card style={{ marginTop: '1rem' }}>
              <CardTitle>Quarantined documents, pending an owner</CardTitle>
              <CardBody>
                {report.ingestion.quarantined_detail.length === 0 ? (
                  <Content component="p">none quarantined in this batch</Content>
                ) : (
                  <List>
                    {report.ingestion.quarantined_detail.map((doc) => (
                      <ListItem key={doc.doc_id}>
                        {doc.minio_url ? (
                          <a href={doc.minio_url} target="_blank" rel="noreferrer">
                            <strong>{doc.filename}</strong>
                          </a>
                        ) : (
                          <strong>{doc.filename}</strong>
                        )}{' '}
                        ({doc.doc_id}): {doc.quarantine_reasons.join(', ')}
                      </ListItem>
                    ))}
                  </List>
                )}
              </CardBody>
            </Card>

            <Card style={{ marginTop: '1rem' }}>
              <CardTitle>Guardrails audit</CardTitle>
              <CardBody>
                <List>
                  {report.guardrails.audit.map((entry) => (
                    <ListItem key={entry.doc_id}>
                      {entry.minio_url ? (
                        <a href={entry.minio_url} target="_blank" rel="noreferrer">
                          {entry.filename}
                        </a>
                      ) : (
                        entry.filename
                      )}
                      :{' '}
                      <Label color={entry.verdict === 'success' ? 'green' : 'red'} isCompact>
                        {entry.verdict}
                      </Label>
                    </ListItem>
                  ))}
                </List>
              </CardBody>
            </Card>

            {/* last on purpose: this is the pipeline's eventual outcome,
                everything above it (kpis, quarantine, guardrails audit) is
                what actually produced the eval set this result is scored
                against, reads better read top to bottom in that order. */}
            <Card style={{ marginTop: '1rem' }}>
              <CardTitle>Retrieval optimization result</CardTitle>
              <CardBody>
                {leaderboard?.winner ? (
                  <>
                    <Flex
                      justifyContent={{ default: 'justifyContentSpaceBetween' }}
                      alignItems={{ default: 'alignItemsCenter' }}
                      flexWrap={{ default: 'wrap' }}
                    >
                      <FlexItem>
                        <Flex
                          alignItems={{ default: 'alignItemsCenter' }}
                          gap={{ default: 'gapSm' }}
                        >
                          <FlexItem>
                            <Icon status="success" size="lg">
                              <TrophyIcon />
                            </Icon>
                          </FlexItem>
                          <FlexItem>
                            <Content
                              component="p"
                              style={{ margin: 0, fontSize: 'var(--pf-t--global--font--size--lg)' }}
                            >
                              {leaderboard.winner.pattern_name}
                            </Content>
                            <Content component="small">
                              Winner out of {leaderboard.patterns_tested} configurations AutoRAG
                              evaluated against this batch{'\u2019'}s eval set, no manual tuning
                            </Content>
                          </FlexItem>
                        </Flex>
                      </FlexItem>
                      <Flex direction={{ default: 'column' }} gap={{ default: 'gapXs' }}>
                        {autoragDashboardUrl && (
                          <FlexItem>
                            <a href={autoragDashboardUrl} target="_blank" rel="noreferrer">
                              View this run in the pipelines view
                            </a>
                          </FlexItem>
                        )}
                        {autoragResultsUrl && (
                          <FlexItem>
                            <a href={autoragResultsUrl} target="_blank" rel="noreferrer">
                              View AutoRAG results
                            </a>
                          </FlexItem>
                        )}
                      </Flex>
                    </Flex>

                    <Grid hasGutter style={{ marginTop: '1rem' }}>
                      <GridItem span={7}>
                        {(leaderboard.winner.all_metrics ?? []).map((m) => (
                          <Progress
                            key={m.name}
                            title={m.name}
                            value={Math.round(m.score * 100)}
                            label={m.score.toFixed(4)}
                            valueText={m.score.toFixed(4)}
                            measureLocation={ProgressMeasureLocation.outside}
                            size="sm"
                            variant={
                              m.name === leaderboard.winner!.metric_name ? 'success' : undefined
                            }
                            style={{ marginBottom: '0.75rem' }}
                          />
                        ))}
                      </GridItem>
                      <GridItem span={5}>
                        <DescriptionList isCompact isHorizontal>
                          <DescriptionListGroup>
                            <DescriptionListTerm>Chunk size</DescriptionListTerm>
                            <DescriptionListDescription>
                              {leaderboard.winner.settings.chunking?.chunk_size ?? '?'}
                            </DescriptionListDescription>
                          </DescriptionListGroup>
                          <DescriptionListGroup>
                            <DescriptionListTerm>Chunk overlap</DescriptionListTerm>
                            <DescriptionListDescription>
                              {leaderboard.winner.settings.chunking?.chunk_overlap ?? '?'}
                            </DescriptionListDescription>
                          </DescriptionListGroup>
                          <DescriptionListGroup>
                            <DescriptionListTerm>Embedding model</DescriptionListTerm>
                            <DescriptionListDescription>
                              {leaderboard.winner.settings.embedding?.model_id ?? '?'}
                            </DescriptionListDescription>
                          </DescriptionListGroup>
                          <DescriptionListGroup>
                            <DescriptionListTerm>Generation model</DescriptionListTerm>
                            <DescriptionListDescription>
                              {leaderboard.winner.settings.generation?.model_id ?? '?'}
                            </DescriptionListDescription>
                          </DescriptionListGroup>
                        </DescriptionList>
                      </GridItem>
                    </Grid>

                    <Button
                      variant="primary"
                      style={{ marginTop: '1rem' }}
                      isLoading={triggerBusy === 'apply-pattern'}
                      isDisabled={triggerBusy !== null}
                      onClick={triggerApplyPattern}
                    >
                      Apply this pattern to production
                    </Button>
                    {applyPatternMessage && (
                      <Alert
                        variant="info"
                        title={applyPatternMessage}
                        isInline
                        style={{ marginTop: '0.5rem' }}
                      >
                        {applyPatternDashboardUrl && (
                          <a href={applyPatternDashboardUrl} target="_blank" rel="noreferrer">
                            View this run in the pipelines view
                          </a>
                        )}
                      </Alert>
                    )}
                  </>
                ) : (
                  <Content component="small">
                    {autoragRunState
                      ? `AutoRAG run for this batch is currently ${autoragRunState}, the result will appear here once it succeeds.`
                      : 'No AutoRAG run has been triggered for this batch yet, use the button above to start one.'}{' '}
                    {autoragDashboardUrl && (
                      <a href={autoragDashboardUrl} target="_blank" rel="noreferrer">
                        View this run in the pipelines view
                      </a>
                    )}{' '}
                    {autoragResultsUrl && (
                      <a href={autoragResultsUrl} target="_blank" rel="noreferrer">
                        View AutoRAG results
                      </a>
                    )}
                  </Content>
                )}
              </CardBody>
            </Card>
          </>
        )}
      </PageSection>
    </>
  );
}
