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
  Content,
  ExpandableSection,
  Flex,
  FlexItem,
  Icon,
  PageSection,
  TextArea,
} from '@patternfly/react-core';
import {
  BrainIcon,
  CubesIcon,
  DatabaseIcon,
  LockIcon,
  OutlinedUserIcon,
  ShieldAltIcon,
  TopologyIcon,
} from '@patternfly/react-icons';
import { ComponentType, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './spear-shield-chat.css';

// no persona picker; backend resolves identity from the forwarded token
const PROXY_PATH = '/api/proxy/plugin/spear-console/backend';

interface TraceStep {
  step: string;
  detail: string;
  duration_ms: number;
  from: string;
  to: string;
  protocol: string;
  // false when a hop was refused or failed; missing on older traces means ok
  ok?: boolean;
  // epoch ms when the hop finished; missing on older traces
  timestamp_ms?: number;
}

// local time with ms for hop labels
function formatHopTime(ms?: number): string | null {
  if (!ms) return null;
  const d = new Date(ms);
  const time = d.toLocaleTimeString(undefined, { hour12: false });
  const millis = String(d.getMilliseconds()).padStart(3, '0');
  return `${time}.${millis}`;
}

// trace node names to icons
const NODE_ICON: Record<string, ComponentType> = {
  'owner-console-backend': OutlinedUserIcon,
  'coordinator-agent (runc sandbox)': ShieldAltIcon,
  'guardrails + glm-53-flash': BrainIcon,
  'retrieval-agent (kata sandbox)': LockIcon,
  'mcp-gateway -> spear-openproject-mcp': TopologyIcon,
  'rag-query-relay': DatabaseIcon,
};

// one hop on a vertical timeline; ok false styles as blocked
const TimelineHop = ({ hop, isLast }: { hop: TraceStep; isLast: boolean }) => {
  const isBlocked = hop.ok === false;
  const ToIcon = NODE_ICON[hop.to] ?? CubesIcon;
  const dangerColor = 'var(--pf-t--global--text--color--status--danger--default)';
  return (
    <Flex alignItems={{ default: 'alignItemsFlexStart' }} gap={{ default: 'gapSm' }}>
      <FlexItem style={{ alignSelf: 'stretch' }}>
        <Flex
          direction={{ default: 'column' }}
          alignItems={{ default: 'alignItemsCenter' }}
          style={{ width: '1.5rem', height: '100%' }}
        >
          <FlexItem>
            <Icon size="sm" status={isBlocked ? 'danger' : 'success'}>
              <ToIcon />
            </Icon>
          </FlexItem>
          {!isLast && (
            <FlexItem grow={{ default: 'grow' }}>
              <div
                style={{
                  width: 2,
                  height: '100%',
                  minHeight: '1.25rem',
                  margin: '0.2rem auto 0',
                  background: 'var(--pf-t--global--border--color--default)',
                }}
              />
            </FlexItem>
          )}
        </Flex>
      </FlexItem>
      <FlexItem grow={{ default: 'grow' }} style={{ paddingBottom: isLast ? 0 : '1rem' }}>
        <Flex
          justifyContent={{ default: 'justifyContentSpaceBetween' }}
          alignItems={{ default: 'alignItemsCenter' }}
        >
          <FlexItem>
            <Content
              component="small"
              style={{ fontWeight: 'bold', color: isBlocked ? dangerColor : undefined }}
            >
              {isBlocked ? `blocked: ${hop.step}` : hop.step}
            </Content>
          </FlexItem>
          {formatHopTime(hop.timestamp_ms) && (
            <FlexItem>
              <Content
                component="small"
                style={{ color: 'var(--pf-t--global--text--color--subtle)' }}
              >
                {formatHopTime(hop.timestamp_ms)}
              </Content>
            </FlexItem>
          )}
        </Flex>
        <Content
          component="small"
          style={{ display: 'block', color: 'var(--pf-t--global--text--color--subtle)' }}
        >
          {hop.from} {'\u2192'} {hop.to} {'\u00b7'} {hop.protocol} {'\u00b7'} {hop.duration_ms} ms
        </Content>
        <Content
          component="small"
          style={{
            display: 'block',
            color: isBlocked ? dangerColor : undefined,
            // pre-wrap for multi line mcp evidence in detail
            whiteSpace: 'pre-wrap',
            fontFamily: 'var(--pf-t--global--font--family--mono, monospace)',
          }}
        >
          {hop.detail}
        </Content>
      </FlexItem>
    </Flex>
  );
};

interface ChatTurn {
  question: string;
  answer?: string;
  error?: string;
  trace?: TraceStep[];
}

// typing indicator while ask is in flight
const TypingIndicator = () => {
  const [dots, setDots] = useState(1);
  useEffect(() => {
    const id = setInterval(() => setDots((d) => (d % 3) + 1), 450);
    return () => clearInterval(id);
  }, []);
  return (
    <Content component="small" style={{ fontStyle: 'italic' }}>
      SPEAR Shield is thinking{'.'.repeat(dots)}
    </Content>
  );
};

const AgentAvatar = () => (
  <Icon size="lg" status="info">
    <ShieldAltIcon />
  </Icon>
);

const UserAvatar = () => (
  <Icon size="lg">
    <OutlinedUserIcon />
  </Icon>
);

const RequestTrace = ({ trace }: { trace: TraceStep[] }) => {
  // expand when a hop was blocked
  const blockedHop = trace.find((h) => h.ok === false);
  const [isExpanded, setIsExpanded] = useState(Boolean(blockedHop));
  const toggleText = isExpanded
    ? 'Hide agent activity'
    : blockedHop
      ? `Show agent activity (blocked at "${blockedHop.step}")`
      : `Show agent activity (${trace.length} hops, a2a/mcp calls)`;
  return (
    <ExpandableSection
      toggleText={toggleText}
      isExpanded={isExpanded}
      onToggle={(_e, expanded) => setIsExpanded(expanded)}
      style={{ marginTop: '0.5rem' }}
    >
      <Card isCompact style={{ marginTop: '0.5rem' }}>
        <CardBody>
          {trace.map((hop, i) => (
            <TimelineHop key={i} hop={hop} isLast={i === trace.length - 1} />
          ))}
        </CardBody>
      </Card>
    </ExpandableSection>
  );
};

export default function SpearShieldChatPage() {
  const { t } = useTranslation('plugin__spear-console');
  const [draft, setDraft] = useState('');
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [busy, setBusy] = useState(false);
  // scroll new turns into view
  const bottomRef = useRef<HTMLDivElement>(null);

  const ask = () => {
    const question = draft.trim();
    if (!question || busy) return;
    setDraft('');
    setBusy(true);
    const turnIndex = turns.length;
    setTurns((prev) => [...prev, { question }]);
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/ask`, 'POST', {
      body: JSON.stringify({ message: question }),
    })
      .then((data: { answer: string; trace?: TraceStep[] }) => {
        setTurns((prev) =>
          prev.map((turn, i) =>
            i === turnIndex ? { ...turn, answer: data.answer, trace: data.trace } : turn,
          ),
        );
      })
      .catch((err: Error) => {
        setTurns((prev) =>
          prev.map((turn, i) => (i === turnIndex ? { ...turn, error: err.message } : turn)),
        );
      })
      .finally(() => {
        setBusy(false);
        setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
      });
  };

  return (
    <>
      <DocumentTitle>{t('Ask SPEAR Shield')}</DocumentTitle>
      <ListPageHeader title={t('Ask SPEAR Shield')} />
      <PageSection>
        <Content component="p">
          SPEAR Shield is the governed gateway to the organization{'\u2019'}s knowledge base and OpenProject work tracker. All responses execute as the signed in user, subject to the same identity, isolation and security controls enforced across all agent to agent and agent to tool interactions. No exceptions or alternate trust paths.
        </Content>

        <Card style={{ marginTop: '1rem' }}>
          <CardBody>
            {turns.length === 0 && (
              <Content component="small">No questions asked yet in this session</Content>
            )}
            <Flex direction={{ default: 'column' }} gap={{ default: 'gapLg' }}>
              {turns.map((turn, i) => (
                <FlexItem key={i}>
                  <Flex alignItems={{ default: 'alignItemsFlexStart' }} gap={{ default: 'gapSm' }}>
                    <FlexItem>
                      <UserAvatar />
                    </FlexItem>
                    <FlexItem grow={{ default: 'grow' }}>
                      <Content component="p" style={{ fontWeight: 'bold', margin: 0 }}>
                        {turn.question}
                      </Content>
                    </FlexItem>
                  </Flex>

                  <Flex
                    alignItems={{ default: 'alignItemsFlexStart' }}
                    gap={{ default: 'gapSm' }}
                    style={{ marginTop: '0.5rem' }}
                  >
                    <FlexItem>
                      <AgentAvatar />
                    </FlexItem>
                    <FlexItem grow={{ default: 'grow' }}>
                      {turn.error && (
                        <Alert variant="danger" title="spear shield could not answer this" isInline>
                          {turn.error}
                        </Alert>
                      )}
                      {!turn.error && turn.answer === undefined && <TypingIndicator />}
                      {!turn.error && turn.answer !== undefined && (
                        <>
                          {/* coordinator answers are markdown */}
                          <div className="coordinator-answer-md">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.answer}</ReactMarkdown>
                          </div>
                          {turn.trace && turn.trace.length > 0 && (
                            <RequestTrace trace={turn.trace} />
                          )}
                        </>
                      )}
                    </FlexItem>
                  </Flex>
                </FlexItem>
              ))}
              <div ref={bottomRef} />
            </Flex>
          </CardBody>
        </Card>

        <Flex
          alignItems={{ default: 'alignItemsFlexEnd' }}
          gap={{ default: 'gapMd' }}
          style={{ marginTop: '1rem' }}
        >
          <FlexItem grow={{ default: 'grow' }}>
            <TextArea
              aria-label="question for spear shield"
              value={draft}
              onChange={(_e, value) => setDraft(value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  ask();
                }
              }}
              rows={2}
              placeholder="Ask about work packages, policy documents, anything this project's knowledge base covers"
            />
          </FlexItem>
          <FlexItem>
            <Button
              variant="primary"
              isLoading={busy}
              isDisabled={busy || !draft.trim()}
              onClick={ask}
            >
              Ask
            </Button>
          </FlexItem>
        </Flex>
      </PageSection>
    </>
  );
}
