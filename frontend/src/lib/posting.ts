// The rules an ad has to satisfy, independent of whether it is a queued draft
// or a live posting.
//
// These lived inside ReviewPage.tsx, which meant the live-post editor could not
// reach them without importing a 2,000-line page module. Several were also
// implemented more than once in that file and had already drifted apart — the
// head/tail split existed in three places and the city cascade in two, with
// different rules for when the free-text area box gets reseeded.

export type CountyRef = {
  name: string;
  subarea_supported: boolean;
  cities: { city: string; zip: string }[];
};

export type LocationRef = {
  counties: CountyRef[];
  phone_numbers: string[];
  license_number: string;
  service_offered: string;
};

// Craigslist advertises 16,000 characters and rejects below it — measured
// against the live form, 15,945 by this rule was refused and 15,412 published.
// Mirrors POSTING_BODY_LIMIT / effective_body_length in
// backend/app/services/drafts.py; the server is the authority, this is here so
// you find out while typing rather than three failed posting slots later.
export const POSTING_BODY_LIMIT = 15_000;

// Craigslist truncates posting titles around 70 characters. Nothing warned you,
// so an over-long title silently lost its ending — usually the city or the call
// to action, which is the part doing the work.
export const TITLE_LIMIT = 70;

export function effectiveBodyLength(body: string): number {
  // A textarea submits with every line break as CRLF, and the value comes back
  // HTML-escaped — so newlines cost two and each "&" costs five.
  const crlf = body.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\n/g, "\r\n");
  return crlf.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").length;
}

/** The head to store when there is no stored split to trust. */
export function deriveBodyHead(body: string): string {
  return body.split("\n\n.")[0].slice(0, 2000);
}

/**
 * Separate the ad copy from the keyword tail.
 *
 * The generator assembles the body as `head + "\n\n" + tail`, so when the stored
 * head is a prefix of the body the split is exact and needs no guessing. When it
 * is not — hand-written copy, or something already edited — fall back to one
 * textarea. Inferring a split we cannot prove would silently truncate a live ad.
 */
export function splitBody(
  body: string,
  head: string | null,
): { splittable: boolean; head: string; tail: string } {
  const splittable = Boolean(head && body.startsWith(head) && body.length > head.length);
  return {
    splittable,
    head: splittable ? (head as string) : body,
    tail: splittable ? body.slice((head as string).length).replace(/^\n+/, "") : "",
  };
}

export type PostingFormValue = {
  account: string;
  county: string;
  city: string;
  postal_code: string;
  phone_number: string;
  license_number: string;
  title: string;
  body: string;
  /** null when there is no stored split — the body editor drops to one box. */
  body_head: string | null;
  /** Drafts only; "" and unrendered when caps.geographicArea is false. */
  geographic_area: string;
};

/**
 * Changing county invalidates the city and zip, rather than leaving a Broward
 * city filed under Palm Beach.
 */
export function applyCounty(v: PostingFormValue, name: string): PostingFormValue {
  return { ...v, county: name, city: "", postal_code: "" };
}

/**
 * Picking a city fills the zip and, if the free-text area box is still just
 * mirroring the city, follows it along. A widened area the operator typed
 * themselves — "Davie, Plantation" — is theirs and must survive.
 */
export function applyCity(
  v: PostingFormValue,
  city: string,
  locations: LocationRef | null,
): PostingFormValue {
  const hit = locations?.counties
    .find((c) => c.name === v.county)
    ?.cities.find((c) => c.city === city);
  const mirroring = !v.geographic_area.trim() || v.geographic_area === v.city;
  return {
    ...v,
    city,
    postal_code: hit?.zip ?? v.postal_code,
    geographic_area: mirroring ? city : v.geographic_area,
  };
}

const DIRTY_KEYS: (keyof PostingFormValue)[] = [
  "account", "county", "city", "postal_code", "phone_number", "license_number",
  "title", "body", "geographic_area",
];

/**
 * Has anything the operator can see changed?
 *
 * Replaces a hand-written nine-clause comparison that had to be remembered
 * whenever a field was added — and would silently stop guarding the moment
 * somebody forgot.
 */
export function postingDirty(a: PostingFormValue, b: PostingFormValue): boolean {
  return DIRTY_KEYS.some((k) => (a[k] ?? "") !== (b[k] ?? ""));
}
