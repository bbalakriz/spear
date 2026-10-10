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

// PHASE2_PLAN.md section 8: no persona picker here, this page always asks
// as whoever is actually signed into the console right now. the backend
// resolves that real identity from the forwarded token itself, this page
// never sends a username anywhere, see owner-console-backend/server.py's
// ask_spear_shield.
const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

interface TraceStep {
  step: string;
  detail: string;
  duration_ms: number;
  from: string;
  to: string;
  protocol: string;
  // false only for a hop that was genuinely refused or failed, missing
  // entirely on older cached turns, treated as ok in that case so this
  // stays backward compatible rather than flagging everything red
  ok?: boolean;
  // real wall clock moment this hop finished, epoch ms, missing on
  // older cached turns from before the backend started sending it
  timestamp_ms?: number;
}

// hh:mm:ss.mmm in the browser's own local time, short enough to sit
// next to the duration without crowding the step name above it
function formatHopTime(ms?: number): string | null {
  if (!ms) return null;
  const d = new Date(ms);
  const time = d.toLocaleTimeString(undefined, { hour12: false });
  const millis = String(d.getMilliseconds()).padStart(3, '0');
  return `${time}.${millis}`;
}

// every real component name the backend trace can ever name on either
// side of a hop, mapped to an icon, so the timeline reads as actual
// named infrastructure rather than generic numbered steps
const NODE_ICON: Record<string, ComponentType> = {
  'owner-console-backend': OutlinedUserIcon,
  'coordinator-agent (runc sandbox)': ShieldAltIcon,
  'guardrails + glm-53-flash': BrainIcon,
  'retrieval-agent (kata sandbox)': LockIcon,
  'mcp-gateway -> spear-openproject-mcp': TopologyIcon,
  'rag-query-relay': DatabaseIcon,
};

// one real hop in the live trace, rendered as a single row on a running
// vertical rail rather than a pair of boxes with an arrow between them,
// a continuous timeline of what the request actually did in order. a
// hop the backend marked ok: false was a genuine refusal or failure,
// the dot, the step name and the detail line all turn red for it, so
// the exact point a blocked request stopped is obvious at a glance
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
          style={{ display: 'block', color: isBlocked ? dangerColor : undefined }}
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

// a small typing indicator, three dots cycling through, rendered while a
// question is in flight, same spirit as any modern chat product's own
// "assistant is typing" affordance rather than a plain generic spinner
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
  // a blocked turn starts expanded, not collapsed behind an extra click,
  // and names the one real reason inline rather than leaving the viewer
  // to open the timeline just to find out something was refused at all
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
  const { t } = useTranslation('plugin__phase1-ingestion-console');
  const [draft, setDraft] = useState('');
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [busy, setBusy] = useState(false);
  // scrolls the latest turn into view once it lands, a chat page reads
  // oddest when a new answer appears below the fold
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
          SPEAR Shield is this environment{'\u2019'}s governed entry point into the organization
          {'\u2019'}s knowledge base and work tracker. Every answer below is generated as you, the
          signed in console user, through the same identity, isolation and guardrails controls the
          rest of this platform enforces for every other agent to agent and agent to tool call,
          nothing on this page is a separate or relaxed path.
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
                          {/* the coordinator's own system prompt pins the answer to
                              markdown, so it renders here through a real markdown
                              renderer, not a pre-wrap text blob that shows raw ##,
                              *, | syntax, this holds regardless of which model sits
                              behind the prompt since react-markdown handles the full
                              commonmark plus gfm surface, tables included */}
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
