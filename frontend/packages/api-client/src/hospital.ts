/**
 * The hospital's own situation, as the desk's panel reads it.
 *
 * One call. It answers "what has changed, and does any of it land on somebody
 * already travelling" — and, separately and always, whether we are reading a
 * real hospital system or nothing at all. That second part is not decoration:
 * a pane that shows hospital rows without saying where they came from is
 * making a claim it has not earned, so `connector.live` is required rather
 * than optional and every screen that renders this has to say which it is.
 */
import { request } from "./client";

/** What the connector itself is doing. Shown so a stale pane explains itself. */
export interface HospitalConnector {
  hospital_id: string;
  hospital_name: string;
  /** Which system answered: "bahmni", "fhir", or "desks" when none is. */
  system: string;
  /** True only when an HMS is actually being read over the wire. */
  live: boolean;
  /** False until the first successful read; before that there is no baseline. */
  primed: boolean;
  /** Appointments held in the last snapshot. */
  appointments: number;
  last_read_at: string | null;
  /** Empty when the last read succeeded. Shown verbatim; the HMS's own words. */
  last_error: string;
  counts: Record<string, number>;
  window_days?: number;
}

/** One line on the situation board. */
export interface HospitalChange {
  at: string;
  /** A whole sentence, written server-side. Never assembled in the browser. */
  headline: string;
  service: string;
  resource_id: string | null;
  /** Null when the change is somebody else's patient, which is most of them. */
  patient_id: string | null;
  state: string;
  on: string;
  kind: "slot_lost" | "slot_moved" | "slot_booked";
  /** Which hospital this happened at. Two hospitals share one combined feed. */
  hospital_id: string;
  /**
   * Whether this lands on a family we are already carrying. The one thing the
   * board sorts on, above recency.
   */
  hits_booked_patients: boolean;
}

/** A machine, a clinician or a supply, and whether a desk has reported it down. */
export interface HospitalResource {
  resource_id: string;
  display_name: string;
  department: string;
  kind: string;
  hospital_id: string;
  up: boolean;
  down_from: string | null;
  down_until: string | null;
  note: string;
  reported_by: string;
  provider?: string;
}

/** One hospital: which system answers for it, what moved, what is down. */
export interface HospitalBoard {
  connector: HospitalConnector;
  resources: HospitalResource[];
  /** Newest first, with anything hitting a booked patient lifted to the top. */
  feed: HospitalChange[];
}

export interface HospitalNow {
  today: string;
  /** One board per hospital. Hospitals being read live come first. */
  hospitals: HospitalBoard[];
  /**
   * Every hospital's changes in one stream, newest first with urgent rows
   * lifted. This is the view no single HMS can produce — it is the only place
   * a Gaya result and a Mumbai cancellation appear on the same screen.
   */
  feed: HospitalChange[];
}

export const hospital = {
  now: () => request<HospitalNow>("/hospital/now"),
};
