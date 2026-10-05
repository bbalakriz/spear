import { DocumentTitle, ListPageHeader, consoleFetchJSON } from '@openshift-console/dynamic-plugin-sdk';
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
  ArrowRightIcon,
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
}

// every real component name the backend trace can ever name on either
// side of a hop, mapped to an icon, so the flow diagram reads as actual
// named infrastructure rather than generic numbered steps
const NODE_ICON: Record<string, ComponentType> = {
  'owner-console-backend': OutlinedUserIcon,
  'coordinator-agent (runc sandbox)': ShieldAltIcon,
  'guardrails + glm-53-flash': BrainIcon,
  'retrieval-agent (kata sandbox)': LockIcon,
  'mcp-gateway -> spear-openproject-mcp': TopologyIcon,
  'rag-query-relay': DatabaseIcon,
};

const FlowNode = ({ name, danger }: { name: string; danger?: boolean }) => {
  const NodeIcon = NODE_ICON[name] ?? CubesIcon;
  return (
    <Flex
      direction={{ default: 'column' }}
      alignItems={{ default: 'alignItemsCenter' }}
      spaceItems={{ default: 'spaceItemsXs' }}
      style={{ width: '9rem', textAlign: 'center' }}
    >
      <FlexItem>
        <Icon size="lg" status={danger ? 'danger' : undefined}>
          <NodeIcon />
        </Icon>
      </FlexItem>
      <FlexItem>
        <Content
          component="small"
          style={{
            fontWeight: 'bold',
            lineHeight: 1.2,
            color: danger ? 'var(--pf-t--global--text--color--status--danger--default)' : undefined,
          }}
        >
          {name}
        </Content>
      </FlexItem>
    </Flex>
  );
};

// one real hop, rendered as two named boxes with the actual protocol and
// wall clock duration on the arrow between them, a request flow diagram
// built straight from the backend's own live trace, not a static drawing.
// a hop the backend marked ok: false was a genuine refusal or failure,
// rendered red end to end, the line, the arrow and the destination box,
// so the exact point a blocked request actually stopped is obvious at a
// glance rather than only readable in the prose detail line below it
const FlowHop = ({ hop }: { hop: TraceStep }) => {
  const isBlocked = hop.ok === false;
  const lineColor = isBlocked
    ? 'var(--pf-t--global--border--color--status--danger--default)'
    : 'var(--pf-t--global--border--color--default)';
  return (
    <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }} style={{ marginBottom: '0.75rem' }}>
      <FlexItem>
        <FlowNode name={hop.from} />
      </FlexItem>
      <FlexItem grow={{ default: 'grow' }}>
        <div style={{ textAlign: 'center', padding: '0 0.5rem' }}>
          <Content component="small">{hop.protocol}</Content>
          <Flex
            alignItems={{ default: 'alignItemsCenter' }}
            justifyContent={{ default: 'justifyContentCenter' }}
            style={{ margin: '0.15rem 0' }}
          >
            <div style={{ flexGrow: 1, borderTop: `2px solid ${lineColor}` }} />
            <Icon size="sm" status={isBlocked ? 'danger' : undefined} style={{ margin: '0 -0.2rem' }}>
              <ArrowRightIcon />
            </Icon>
          </Flex>
          <Content
            component="small"
            style={{
              fontWeight: 'bold',
              color: isBlocked ? 'var(--pf-t--global--text--color--status--danger--default)' : undefined,
            }}
          >
            {isBlocked ? `blocked: ${hop.step}` : hop.step}
          </Content>
          <Content component="small">{hop.duration_ms} ms</Content>
        </div>
      </FlexItem>
      <FlexItem>
        <FlowNode name={hop.to} danger={isBlocked} />
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
  // to open the diagram just to find out something was refused at all
  const blockedHop = trace.find((h) => h.ok === false);
  const [isExpanded, setIsExpanded] = useState(Boolean(blockedHop));
  const toggleText = blockedHop
    ? `request flow (${trace.length} hops, blocked at "${blockedHop.step}")`
    : `request flow (${trace.length} hops, real a2a/mcp calls)`;
  return (
    <ExpandableSection
      toggleText={toggleText}
      isExpanded={isExpanded}
      onToggle={(_e, expanded) => setIsExpanded(expanded)}
      style={{ marginTop: '0.5rem' }}
    >
      <Card isCompact style={{ marginTop: '0.5rem', overflowX: 'auto' }}>
        <CardBody>
          {trace.map((hop, i) => (
            <div key={i}>
              <FlowHop hop={hop} />
              <Content
                component="small"
                style={{
                  display: 'block',
                  margin: '-0.5rem 0 0.75rem 0.5rem',
                  color:
                    hop.ok === false
                      ? 'var(--pf-t--global--text--color--status--danger--default)'
                      : 'var(--pf-t--global--text--color--subtle)',
                }}
              >
                {hop.detail}
              </Content>
            </div>
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
          SPEAR Shield is this environment{'\u2019'}s governed entry point into the organization{'\u2019'}s
          knowledge base and work tracker. Every answer below is generated as you, the signed in
          console user, through the same identity, isolation and guardrails controls the rest of
          this platform enforces for every other agent to agent and agent to tool call, nothing on
          this page is a separate or relaxed path.
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
                          {turn.trace && turn.trace.length > 0 && <RequestTrace trace={turn.trace} />}
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
            <Button variant="primary" isLoading={busy} isDisabled={busy || !draft.trim()} onClick={ask}>
              Ask
            </Button>
          </FlexItem>
        </Flex>
      </PageSection>
    </>
  );
}
