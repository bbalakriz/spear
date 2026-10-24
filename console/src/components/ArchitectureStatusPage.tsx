import { DocumentTitle, ListPageHeader, consoleFetchJSON } from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  Flex,
  FlexItem,
  Gallery,
  Icon,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import { CheckCircleIcon, ExclamationCircleIcon } from '@patternfly/react-icons';
import { useEffect, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/spear-console/backend';
// poll cluster status on this interval
const POLL_MS = 15000;

interface Component {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

interface Status {
  components: Component[];
  same_node: boolean;
}

// request path left to right; order pinned here, not backend list order
const DISPLAY_ORDER = ['keycloak', 'a2a-gateway', 'coordinator', 'retrieval', 'mcp-gateway', 'guardrails'];

const StatusCard = ({ component }: { component: Component }) => (
  <Card
    style={{
      borderLeft: `4px solid ${
        component.ok
          ? 'var(--pf-t--global--color--status--success--default)'
          : 'var(--pf-t--global--color--status--danger--default)'
      }`,
    }}
  >
    <CardTitle style={{ minHeight: '3rem', display: 'flex', alignItems: 'center' }}>
      {component.label}
    </CardTitle>
    <CardBody>
      <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }}>
        <FlexItem>
          <Icon status={component.ok ? 'success' : 'danger'}>
            {component.ok ? <CheckCircleIcon /> : <ExclamationCircleIcon />}
          </Icon>
        </FlexItem>
        <FlexItem>
          <Content component="small">{component.ok ? 'reachable' : 'unreachable'}</Content>
        </FlexItem>
      </Flex>
      <Content component="small" style={{ display: 'block', marginTop: '0.5rem', wordBreak: 'break-all' }}>
        {component.detail}
      </Content>
    </CardBody>
  </Card>
);

export default function ArchitectureStatusPage() {
  const { t } = useTranslation('plugin__spear-console');
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const refresh = () => {
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/status`)
      .then((data: Status) => {
        setStatus(data);
        setError(null);
        setLastChecked(new Date());
      })
      .catch((err: Error) => setError(err.message));
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, []);

  const byId = new Map((status?.components ?? []).map((c) => [c.id, c]));
  const ordered = DISPLAY_ORDER.map((id) => byId.get(id)).filter(Boolean) as Component[];

  return (
    <>
      <DocumentTitle>{t('Architecture Status')}</DocumentTitle>
      <ListPageHeader title={t('Architecture Status')} />
      <PageSection>
        <Content component="p">
          A live read of the stack this environment runs on: OpenShell sandboxes, one on plain
          runc and one isolated under Kata, a SPIRE derived workload identity on the agent to agent
          hop, Keycloak backed caller identity, and a gateway that enforces per tool RBAC in front of
          the work tracker. Every badge below is checked against the live cluster on a {' '}
          {POLL_MS / 1000} second interval.
        </Content>

        <Flex
          justifyContent={{ default: 'justifyContentSpaceBetween' }}
          alignItems={{ default: 'alignItemsCenter' }}
          style={{ marginTop: '1rem' }}
        >
          <FlexItem>
            {lastChecked && (
              <Content component="small">last checked {lastChecked.toLocaleTimeString()}</Content>
            )}
          </FlexItem>
          <FlexItem>
            <Button variant="secondary" onClick={refresh}>
              Refresh now
            </Button>
          </FlexItem>
        </Flex>

        {error && (
          <Alert
            variant="danger"
            title="Could not reach the spear console backend"
            isInline
            style={{ marginTop: '1rem' }}
          >
            {error}
          </Alert>
        )}

        {!status && !error && <Spinner size="lg" style={{ marginTop: '1rem' }} />}

        {status && (
          <>
            {status.same_node && (
              <Alert
                variant="info"
                isInline
                title="the coordinator (runc) and retrieval (kata) sandboxes are scheduled on the same physical node right now"
                style={{ marginTop: '1rem' }}
              />
            )}
            <Gallery hasGutter minWidths={{ default: '240px' }} style={{ marginTop: '1rem' }}>
              {ordered.map((component) => (
                <StatusCard key={component.id} component={component} />
              ))}
            </Gallery>
          </>
        )}
      </PageSection>
    </>
  );
}
