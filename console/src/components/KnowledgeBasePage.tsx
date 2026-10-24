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

// pipeline and kfp reads via proxy backend; numbers come from backend only
const PROXY_PATH = '/api/proxy/plugin/spear-console/backend';

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
  // fresh reports only carry status/note; older chunk fields kept for legacy reports
  abac_tagging?: { status?: string; note?: string; chunks_created?: number; pattern_name?: string };
  sdg_hub_eval_dataset?: { qa_pairs_created: number };
  autorag_trigger?: string;
  error?: string;
  _mlflow_run_id?: string;
  _mlflow_run_url?: string;
}

// kfp runs merged with mlflow report rows per batch_id
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
  // present for /kfp/autorag-runs entries
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
    // all metrics from the winner pattern evaluation
    all_metrics?: { name: string; score: number }[];
  } | null;
}

// sanitization intake path from /ingestion/source-config (no secrets)
interface SourceConfig {
  minio_endpoint: string;
  bucket: string;
  prefix: string;
  manifest_url: string;
  console_url: string;
}

// kpi color tokens; each caller picks status from its own thresholds
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
      {/* min height keeps kpi values aligned */}
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

// static pipeline overview strip, not live data
interface FlowStep {
  label: string;
  detail: string;
}

// component names in pipeline order; tooltips carry detail
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
  const { t } = useTranslation('plugin__spear-console');

  const [runs, setRuns] = useState<IngestionRunSummary[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string>('');
  const [report, setReport] = useState<IngestionReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  // optional source strip; fetch failure just hides it
  const [sourceConfig, setSourceConfig] = useState<SourceConfig | null>(null);

  useEffect(() => {
    consoleFetchJSON(`${PROXY_PATH}/ingestion/source-config`)
      .then((data: SourceConfig) => setSourceConfig(data))
      .catch(() => setSourceConfig(null));
  }, []);

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
  // apply pattern alert sits far from the top trigger buttons
  const [applyPatternMessage, setApplyPatternMessage] = useState<string | null>(null);
  const [applyPatternDashboardUrl, setApplyPatternDashboardUrl] = useState<string | null>(null);

  const refreshRuns = () => {
    consoleFetchJSON(`${PROXY_PATH}/runs`)
      .then((data: { runs: IngestionRunSummary[] }) => setRuns(data.runs ?? []))
      .catch(() => {
        // run picker failure should not block the page
      });
  };

  useEffect(() => {
    refreshRuns();
  }, []);

  // skip /report until log_ingestion_report has written a run
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

  // match autorag run to batch; reset state first so stale ui does not linger
  useEffect(() => {
    setAutoragRunId(null);
    setAutoragRunState(null);
    setAutoragDashboardUrl(null);
    setAutoragResultsUrl(null);
    setLeaderboard(null);
    if (!report?.batch_id) return;
    let cancelled = false;
    const expectedName = `documents-rag-optimization-pipeline-${report.batch_id}`;
    // reused batch_id: ignore autorag runs that predate this sanitization start
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
        // autorag fetch failure; page keeps sanitization level numbers
      });
    return () => {
      cancelled = true;
    };
    // selectedBatchId needed so we reset when report batch_id is still null
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
          // select the batch just started; kfp list updates before mlflow report
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
      <DocumentTitle>{t('Sanitize Knowledge Base')}</DocumentTitle>
      <ListPageHeader title={t('Sanitize Knowledge Base')} />
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
            {/* read-only intake path from backend config */}
            {sourceConfig && (
              <DescriptionList isCompact isHorizontal style={{ marginBottom: '1rem' }}>
                <DescriptionListGroup>
                  <DescriptionListTerm>S3 endpoint</DescriptionListTerm>
                  <DescriptionListDescription>
                    <code>{sourceConfig.minio_endpoint}</code>
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Raw feed</DescriptionListTerm>
                  <DescriptionListDescription>
                    <code>
                      {sourceConfig.bucket}/{sourceConfig.prefix}
                    </code>{' '}
                    <a href={sourceConfig.manifest_url} target="_blank" rel="noreferrer">
                      manifest
                    </a>{' '}
                    ·{' '}
                    <a href={sourceConfig.console_url} target="_blank" rel="noreferrer">
                      open in MinIO console
                    </a>
                  </DescriptionListDescription>
                </DescriptionListGroup>
              </DescriptionList>
            )}
            {/* new run row, then existing run picker */}
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
            title="Could not reach the spear console backend"
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
              {/* risk kpis: zero quarantined or blocked is success */}
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

            {/* autorag outcome after ingestion kpis and audits */}
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
