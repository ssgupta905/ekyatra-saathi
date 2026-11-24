import { useEffect, useState } from "react";
import {
  hospital as hospitalApi,
  type HospitalBoard,
  type HospitalChange,
  type HospitalNow as HospitalNowData,
  type HospitalResource,
} from "@saathi/api-client";
import { Card } from "@saathi/ui-web";
import { EmptyState, Panel, Pill, SkeletonRows, StatTile } from "../ui/kit";
import { useWorld } from "../world";

/**
 * Every hospital right now: what each hospital's system has changed, and what
 * is out of service.
 *
 * The combined stream is first on the screen on purpose. A family treated in
 * Mumbai may have her bloods done in Gaya, and the Mumbai system cannot see
 * the Gaya result blocking her Mumbai appointment. Those two rows appearing
 * on one board is the thing no hospital system can do, so the screen leads
 * with it and shows the per-hospital boards underneath.
 *
 * Two rules decide the order, and both are stated on screen so a desk can say
 * why a row is where it is. Newest first; and anything that lands on a family
 * we are already carrying is lifted above that, because a cancelled slot for
 * somebody on a train is not the same news as one for somebody who has not
 * left home. There is no score and nowhere to put one.
 *
 * The live/sandbox tag on each hospital is not decoration. A pane that does
 * not say whether it is reading a real system is making a claim it has not
 * earned, so it says "desks" where nothing is connected rather than quietly
 * showing an empty board.
 */

const KIND_WORDS: Record<HospitalChange["kind"], string> = {
  slot_lost: "slot lost",
  slot_moved: "moved",
  slot_booked: "booked",
};

/** How often to re-read. The connectors poll the systems; this polls them. */
const REFRESH_MS = 4000;

function ago(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function clock(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function Source({ connector }: { connector: HospitalBoard["connector"] }) {
  const live = connector.live;
  return (
    <div className="hn-source">
      <Pill tone={live ? "go" : "neutral"}>{live ? "Live" : "Sandbox"}</Pill>
      <strong>{connector.hospital_name}</strong>
      <span className="plain">
        {live ? (
          <>
            reading <strong>{connector.system}</strong> · last heard{" "}
            {ago(connector.last_read_at)} · {connector.appointments} appointments in a{" "}
            {connector.window_days ?? 0}-day window
          </>
        ) : (
          <>
            no hospital system connected. The desks are the feed: status reaches this
            board when staff enter it, with their name on it.
          </>
        )}
      </span>
    </div>
  );
}

function ResourceRow({ resource }: { resource: HospitalResource }) {
  return (
    <tr className={resource.up ? undefined : "hn-down"}>
      <td>
        <strong>{resource.display_name}</strong>
        <div className="plain hn-small">
          {resource.department} · {resource.kind}
        </div>
      </td>
      <td>
        <Pill tone={resource.up ? "go" : "critical"}>
          {resource.up ? "Working" : "Out of service"}
        </Pill>
      </td>
      <td className="plain">
        {resource.up
          ? "—"
          : [
              resource.down_until ? `until ${resource.down_until}` : "no end given",
              resource.note,
              resource.reported_by ? `per ${resource.reported_by}` : "",
            ]
              .filter(Boolean)
              .join(" · ")}
      </td>
    </tr>
  );
}

function FeedRow({
  change,
  onOpen,
  showHospital,
  nameOf,
}: {
  change: HospitalChange;
  onOpen: (id: string) => void;
  showHospital?: boolean;
  nameOf: (id: string) => string;
}) {
  const urgent = change.hits_booked_patients;
  return (
    <li className={urgent ? "hn-row urgent" : "hn-row"}>
      <span className="hn-when mono">{clock(change.at)}</span>
      <span className="hn-what">
        <Pill tone={change.kind === "slot_lost" ? "critical" : "neutral"}>
          {KIND_WORDS[change.kind]}
        </Pill>{" "}
        {showHospital && change.hospital_id && (
          <span className="hn-at">{nameOf(change.hospital_id)}</span>
        )}{" "}
        {change.headline}
      </span>
      <span className="hn-who">
        {change.patient_id ? (
          <button type="button" className="btn small" onClick={() => onOpen(change.patient_id!)}>
            {change.patient_id}
          </button>
        ) : (
          <span className="plain hn-small">another patient</span>
        )}
      </span>
    </li>
  );
}

function Board({
  board,
  onOpen,
  nameOf,
}: {
  board: HospitalBoard;
  onOpen: (id: string) => void;
  nameOf: (id: string) => string;
}) {
  const { connector, feed, resources } = board;
  return (
    <div className="stack hn-board">
      <Source connector={connector} />

      {connector.last_error && (
        <Card title={`${connector.hospital_name} is not answering`}>
          <p className="plain">
            The board below is the last good read, from {ago(connector.last_read_at)}. Nothing
            has been guessed in the meantime, and the other hospital is unaffected.
          </p>
          <p className="mono hn-small">{connector.last_error}</p>
        </Card>
      )}

      <Panel title="What changed here" hint="Newest first; anything hitting a travelling family on top">
        {feed.length === 0 ? (
          <EmptyState title="Nothing has changed yet">
            {connector.live
              ? "This system is being read and has reported no movement since the console started."
              : "No system is connected here, so nothing arrives on its own."}
          </EmptyState>
        ) : (
          <ul className="hn-feed">
            {feed.map((change, index) => (
              <FeedRow
                key={`${change.at}-${index}`}
                change={change}
                onOpen={onOpen}
                nameOf={nameOf}
              />
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Machines, people and supplies" hint="Out of service first, then by name">
        {resources.length === 0 ? (
          <EmptyState title="No resources registered here">
            Add them under Registries and disruptions can be raised against them.
          </EmptyState>
        ) : (
          <table className="audit-table">
            <thead>
              <tr>
                <th>What</th>
                <th>State</th>
                <th>Since / until</th>
              </tr>
            </thead>
            <tbody>
              {resources.map((resource) => (
                <ResourceRow key={resource.resource_id} resource={resource} />
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

export function HospitalNow({ onOpen }: { onOpen: (patientId: string) => void }) {
  const world = useWorld();
  const [data, setData] = useState<HospitalNowData | null>(null);
  const [failed, setFailed] = useState(false);
  const [only, setOnly] = useState<string>("all");

  useEffect(() => {
    let alive = true;
    const read = () =>
      hospitalApi
        .now()
        .then((next) => {
          if (!alive) return;
          setData(next);
          setFailed(false);
        })
        .catch(() => alive && setFailed(true));
    read();
    const timer = setInterval(read, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // The live stream says something moved; the interval covers the rest,
    // because a hospital change we have not been told about is the point.
  }, [world.cohortRevision]);

  if (!data) {
    return (
      <Panel title="Hospitals right now">
        {failed ? (
          <EmptyState title="Could not read the hospital boards">
            The console could not reach its own API. Nothing has been assumed in its place.
          </EmptyState>
        ) : (
          <SkeletonRows rows={4} />
        )}
      </Panel>
    );
  }

  const nameOf = (id: string) =>
    data.hospitals.find((b) => b.connector.hospital_id === id)?.connector.hospital_name ?? id;

  const shown =
    only === "all"
      ? data.hospitals
      : data.hospitals.filter((b) => b.connector.hospital_id === only);
  const connected = data.hospitals.filter((b) => b.connector.live);
  const hitting = data.feed.filter((f) => f.hits_booked_patients);
  const down = data.hospitals.flatMap((b) => b.resources.filter((r) => !r.up));

  return (
    <div className="stack">
      <div className="today-tiles">
        <StatTile
          label="Hitting booked patients"
          value={hitting.length}
          tone={hitting.length ? "critical" : "go"}
          hint="changes landing on a family we are already carrying"
        />
        <StatTile
          label="Hospitals being read"
          value={`${connected.length} of ${data.hospitals.length}`}
          tone={connected.length ? "go" : "neutral"}
          hint={connected.map((b) => b.connector.system).join(" · ") || "none connected"}
        />
        <StatTile
          label="Out of service"
          value={down.length}
          tone={down.length ? "wait" : "go"}
          hint="across every hospital"
        />
      </div>

      <Panel
        title="Across every hospital"
        hint="The one view no hospital system can produce: each sees only its own."
      >
        {data.feed.length === 0 ? (
          <EmptyState title="Nothing has changed yet">
            {connected.length
              ? "The connected systems are being read and have reported no movement since the console started."
              : "No hospital system is connected, so nothing arrives here on its own."}
          </EmptyState>
        ) : (
          <ul className="hn-feed">
            {data.feed.map((change, index) => (
              <FeedRow
                key={`${change.hospital_id}-${change.at}-${index}`}
                change={change}
                onOpen={onOpen}
                nameOf={nameOf}
                showHospital
              />
            ))}
          </ul>
        )}
      </Panel>

      {data.hospitals.length > 1 && (
        <div className="hn-picker">
          <label htmlFor="hn-hospital">Hospital</label>
          <select
            id="hn-hospital"
            value={only}
            onChange={(e) => setOnly(e.target.value)}
          >
            <option value="all">All hospitals</option>
            {data.hospitals.map((b) => (
              <option key={b.connector.hospital_id} value={b.connector.hospital_id}>
                {b.connector.hospital_name}
              </option>
            ))}
          </select>
        </div>
      )}

      {shown.map((board) => (
        <Board
          key={board.connector.hospital_id}
          board={board}
          onOpen={onOpen}
          nameOf={nameOf}
        />
      ))}
    </div>
  );
}
