import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { BoardNode, PlanBoard as Board } from "@saathi/api-client";
import { fmtDate, fmtWhen, statusLabel, statusTone } from "../format";
import { fmtMs } from "@saathi/api-client";
import { Badge, Card, Chip, DecisionGlyph } from "../primitives";
import { CheckCircle, Clock, Doc, Pin } from "../icons";

/**
 * One patient's pathway, drawn as the branching thing it is.
 *
 * A navigator holding thirty families needs to see *what is waiting on what*.
 * A list sorted by date hides exactly that: it puts the pending radiology report
 * next to the consult it is holding up and gives no hint that one is the reason
 * for the other. Columns are depth through the dependency graph, so reading
 * left to right is reading the order care actually happens in, and the step
 * everything converges on is the one the family would travel for.
 *
 * The layout comes from the backend. Depth is a property of the care pathway,
 * and a console that computed it would be a second copy of the hospital's model
 * of care, drifting quietly out of step with the first.
 *
 * The edges are drawn, not implied. Columns alone say "these come before
 * those"; they do not say *which* of them is waiting on *which*, and that is
 * the question a navigator holding thirty families is actually asking. The
 * backend has always returned the edges — they were simply thrown away here,
 * so the board looked like a graph and behaved like a list.
 */

const glyphFor = (node: BoardNode) => {
  if (node.satisfied) return <CheckCircle size={14} />;
  if (node.status === "pending_result") return <Clock size={14} />;
  return node.no_journey ? <Doc size={14} /> : <Pin size={14} />;
};

function BoardStep({ node }: { node: BoardNode }) {
  const tone = statusTone(node.status);
  const when = node.scheduled_for ?? node.result_available_on ?? node.completed_on;

  const classes = [
    "board-step",
    node.satisfied ? "done" : "",
    node.is_blocking ? "blocking" : "",
    node.is_target ? "target" : "",
    node.no_journey ? "no-journey" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    // The id is how the edge layer finds this node's position. Measured from
    // the real layout rather than computed, because the grid is responsive
    // and anything we calculated would be wrong at the next breakpoint.
    <div className={classes} data-step-id={node.id}>
      <div className="bs-head">
        <span className={`bs-glyph ${tone}`}>{glyphFor(node)}</span>
        <span className="bs-label">{node.label}</span>
      </div>
      <div className="bs-when">
        {when ? `${fmtDate(when)} · ${fmtWhen(when)}` : "no date on record"}
      </div>
      <div className="bs-tags">
        <Chip tone={tone === "critical" ? "critical" : tone === "go" ? "go" : tone === "wait" ? "wait" : undefined}>
          {statusLabel(node.status)}
        </Chip>
        {node.is_target && <Chip tone="info">the visit being decided</Chip>}
        {node.is_blocking && !node.satisfied && <Chip tone="wait">in the way</Chip>}
        {node.no_journey && <Chip>no journey</Chip>}
        {node.resource_id && <Chip>{node.resource_id}</Chip>}
      </div>
    </div>
  );
}


/** Where one node sits, in the coordinate space of the board. */
interface Box {
  left: number;
  right: number;
  top: number;
  height: number;
}

/**
 * The dependency arrows, drawn over the columns.
 *
 * Positions are *measured*, never computed: the grid is responsive, the step
 * cards are different heights, and anything this file calculated would be
 * right at one width and wrong at the next. So it reads the real boxes after
 * layout and redraws whenever they move.
 *
 * An edge into a step that is in the way is drawn heavier, because that is the
 * chain a navigator is actually tracing — the reason the visit at the end
 * cannot happen yet.
 */
function BoardEdges({
  edges,
  blocking,
  container,
}: {
  edges: { from: string; to: string }[];
  blocking: Set<string>;
  container: React.RefObject<HTMLDivElement | null>;
}) {
  const [boxes, setBoxes] = useState<Record<string, Box>>({});
  const [size, setSize] = useState({ width: 0, height: 0 });

  const measure = useCallback(() => {
    const root = container.current;
    if (!root) return;
    const origin = root.getBoundingClientRect();
    const found: Record<string, Box> = {};
    for (const el of root.querySelectorAll<HTMLElement>("[data-step-id]")) {
      const id = el.dataset.stepId;
      if (!id) continue;
      const r = el.getBoundingClientRect();
      found[id] = {
        left: r.left - origin.left,
        right: r.right - origin.left,
        top: r.top - origin.top,
        height: r.height,
      };
    }
    setBoxes(found);
    setSize({ width: root.scrollWidth, height: root.scrollHeight });
  }, [container]);

  useLayoutEffect(measure, [measure, edges]);

  useEffect(() => {
    const root = container.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    // Re-measure on resize rather than on an interval: a board that redrew
    // every second would repaint behind a reader for no reason.
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    for (const el of root.querySelectorAll("[data-step-id]")) observer.observe(el);
    return () => observer.disconnect();
  }, [container, measure, edges]);

  const drawn = edges
    .map((edge) => ({ edge, from: boxes[edge.from], to: boxes[edge.to] }))
    .filter((e) => e.from && e.to);

  if (drawn.length === 0 || size.width === 0) return null;

  return (
    <svg
      className="board-edges"
      width={size.width}
      height={size.height}
      aria-hidden="true"
      focusable="false"
    >
      {drawn.map(({ edge, from, to }) => {
        const x1 = from.right;
        const y1 = from.top + from.height / 2;
        const x2 = to.left;
        const y2 = to.top + to.height / 2;
        // A flat curve rather than a straight line: with several edges
        // converging on one step, straight lines overlap into a single
        // unreadable wedge.
        const bend = Math.max(18, (x2 - x1) / 2);
        const path = `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
        const holdingUp = blocking.has(edge.from);
        return (
          <path
            key={`${edge.from}-${edge.to}`}
            d={path}
            className={holdingUp ? "board-edge blocking" : "board-edge"}
            markerEnd={holdingUp ? "url(#edge-head-blocking)" : "url(#edge-head)"}
          />
        );
      })}
      <defs>
        {["edge-head", "edge-head-blocking"].map((id) => (
          <marker
            key={id}
            id={id}
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="7"
            markerHeight="7"
            orient="auto"
          >
            <path d="M 0 1 L 7 4 L 0 7 z" className={id.endsWith("blocking") ? "head blocking" : "head"} />
          </marker>
        ))}
      </defs>
    </svg>
  );
}

export function PlanBoard({ board, compact }: { board: Board; compact?: boolean }) {
  const columns = useMemo(() => {
    const grouped: BoardNode[][] = Array.from({ length: board.columns }, () => []);
    for (const node of board.nodes) grouped[node.column]?.push(node);
    return grouped;
  }, [board]);

  const decision = board.decision;
  const surface = useRef<HTMLDivElement | null>(null);
  // Which steps are holding something up — the edges leaving these are the
  // chain worth following, so they are drawn heavier.
  const blocking = useMemo(
    () => new Set(board.nodes.filter((n) => n.is_blocking && !n.satisfied).map((n) => n.id)),
    [board.nodes],
  );
  // `edges` names step *kinds* ("consult" -> "biopsy") while nodes carry step
  // *ids* ("EKY-KAMLA-consult"). The kind is the pathway's vocabulary and the
  // id is this patient's, which is correct on both sides and means nothing
  // lines up until one is translated into the other.
  const wired = useMemo(() => {
    const byKind = new Map(board.nodes.map((n) => [n.kind, n.id]));
    return board.edges
      .map((e) => ({ from: byKind.get(e.from), to: byKind.get(e.to) }))
      .filter((e): e is { from: string; to: string } => Boolean(e.from && e.to));
  }, [board.edges, board.nodes]);

  return (
    <Card
      title={`${board.patient.id} · ${board.patient.district}`}
      hint={`${board.patient.distance_km} km away`}
      aside={
        decision ? (
          <Badge
            tone={decision === "go" ? "go" : decision === "wait" || decision === "replan" ? "wait" : "uncertain"}
            icon={<DecisionGlyph decision={decision} size={13} />}
          >
            {decision === "go" && board.travel_on
              ? `Travel ${fmtDate(board.travel_on)}`
              : decision.toUpperCase()}
          </Badge>
        ) : undefined
      }
      flush
      note={
        compact
          ? undefined
          : "Columns are depth through the care pathway, so left to right is the order things actually happen in. A step is in the way when something the family would travel for is waiting on it."
      }
    >
      <div className="board-scroll">
        <div className="board-surface" ref={surface}>
        {/* A floor, not a fixed width. Five columns at 168px overflowed the
            pane by 29px inside the stage's laptop frame, which clipped the
            rightmost step — the one the family would actually travel for. */}
        <div
          className="board-columns"
          style={{ gridTemplateColumns: `repeat(${board.columns}, minmax(148px, 1fr))` }}
        >
          {columns.map((group, index) => (
            <div className="board-column" key={index}>
              {group.map((node) => (
                <BoardStep node={node} key={node.id} />
              ))}
            </div>
          ))}
        </div>
        <BoardEdges edges={wired} blocking={blocking} container={surface} />
        </div>
      </div>

      {board.waiting_on.length > 0 && (
        <div className="board-waiting">
          <span className="bw-label">Waiting on</span>
          {board.waiting_on.map((wait) => (
            <span className="bw-item" key={wait.step_id}>
              <strong>{wait.label}</strong>
              {wait.earliest && wait.latest ? (
                wait.from_booking ? (
                  <> · booked for {fmtDate(wait.earliest)}</>
                ) : (
                  <>
                    {" "}
                    · expected {fmtDate(wait.earliest)} – {fmtDate(wait.latest)}
                  </>
                )
              ) : (
                <> · no date Saathi can stand behind</>
              )}
            </span>
          ))}
        </div>
      )}

      {!compact && board.computed_at && (
        <div className="board-stamp">
          {board.rationale}
          {board.plan_ms != null && <span className="mono"> · planner {fmtMs(board.plan_ms)}</span>}
        </div>
      )}
    </Card>
  );
}
