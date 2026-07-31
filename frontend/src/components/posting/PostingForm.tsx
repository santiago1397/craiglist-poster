// The ad form, shared by everything that edits ad copy: creating a draft,
// editing a draft, and editing a live posting.
//
// It is deliberately presentational — it takes a value and an onChange and has
// no idea what saving means. The three callers differ in their save verb, their
// chrome (modal vs inline panel), their validation gate and their payload shape,
// and none of those belong in here. What they share is the field set, and two
// optional fields is not enough of a difference to justify a third copy: the
// two that already existed had drifted apart before this was extracted.

import { useState } from "react";
import { cn } from "../../lib/cn";
import {
  applyCity,
  applyCounty,
  effectiveBodyLength,
  splitBody,
  POSTING_BODY_LIMIT,
  TITLE_LIMIT,
  type LocationRef,
  type PostingFormValue,
} from "../../lib/posting";

export type PostingFormCaps = {
  /** Drafts only — the live posting's account is fixed by history. */
  accountEditable: boolean;
  /**
   * Drafts only. Craigslist's edit form exposes no subarea control, so a live
   * posting cannot be moved between counties; offering the field would stage a
   * change the desktop can never apply.
   */
  showCounty: boolean;
  /** Drafts only; live postings have no such column. */
  showGeographicArea: boolean;
  /**
   * How the City field behaves. `select` is the routing city of a draft, which
   * drives the zip. `freetext` is a live posting's own area box.
   *
   * These are not the same field wearing two hats. On a live posting, `city`
   * holds whatever Craigslist's free-text area box says — which is often
   * several towns, "Fort Lauderdale, Davie, Plantation". Rendering that as a
   * dropdown of single towns would silently narrow it the first time anyone
   * touched the form.
   */
  cityMode: "select" | "freetext";
};

export function Field(props: {
  label: string;
  value: string;
  onChange: (e: { target: { value: string } }) => void;
  readOnly?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs text-fg-muted">{props.label}</span>
      <input
        value={props.value}
        onChange={props.onChange}
        readOnly={props.readOnly}
        className={cn(
          "w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm",
          props.readOnly && "text-fg-muted cursor-not-allowed",
        )}
      />
    </label>
  );
}

export function BodyCounter({ body }: { body: string }) {
  const n = effectiveBodyLength(body);
  const over = n > POSTING_BODY_LIMIT;
  const near = !over && n > POSTING_BODY_LIMIT * 0.9;
  return (
    <span
      className={cn(
        "text-xs tabular-nums",
        over ? "text-danger-fg font-medium" : near ? "text-warn-fg" : "text-fg-subtle",
      )}
    >
      {n.toLocaleString()} / {POSTING_BODY_LIMIT.toLocaleString()}
      {over && ` — ${(n - POSTING_BODY_LIMIT).toLocaleString()} over, Craigslist will reject this`}
    </span>
  );
}

export function TitleField({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const over = value.length > TITLE_LIMIT;
  return (
    <label className="block">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-fg-muted">Title</span>
        <span className={cn("text-xs tabular-nums", over ? "text-warn-fg" : "text-fg-subtle")}>
          {value.length}/{TITLE_LIMIT}
        </span>
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-describedby="title-limit-hint"
        className={cn(
          "w-full mt-1 bg-bg border rounded px-2 py-1.5 text-sm",
          over ? "border-warn-border" : "border-border-strong",
        )}
      />
      {/* A soft warning, not a maxLength: hard-truncating a pasted title would
          throw away characters without saying so. */}
      <span
        id="title-limit-hint"
        className={cn("text-xs mt-1 block", over ? "text-warn-fg" : "sr-only")}
      >
        {over
          ? `Craigslist shows about ${TITLE_LIMIT} characters — the last ${
              value.length - TITLE_LIMIT
            } may be cut off.`
          : `Craigslist shows about ${TITLE_LIMIT} characters.`}
      </span>
    </label>
  );
}

/** Options that always include the current value, even if reference data lost it. */
function withCurrent(current: string, options: string[]): string[] {
  return current && !options.includes(current) ? [current, ...options] : options;
}

export function PostingForm(props: {
  value: PostingFormValue;
  onChange: (next: PostingFormValue) => void;
  accounts: string[];
  locations: LocationRef | null;
  caps: PostingFormCaps;
  disabled?: boolean;
}) {
  const { value: v, onChange, caps, locations: L } = props;
  const set = (k: keyof PostingFormValue) => (e: { target: { value: string } }) =>
    onChange({ ...v, [k]: e.target.value });

  const county = L?.counties.find((c) => c.name === v.county) ?? null;
  // A draft may name a county the reference data no longer lists, and silently
  // blanking it on open would rewrite the draft on the next save.
  const countyMissing = Boolean(v.county && L && !county);

  const split = splitBody(v.body, v.body_head);
  const [head, setHead] = useState(split.head);
  const [tail, setTail] = useState(split.tail);
  const [tailUnlocked, setTailUnlocked] = useState(false);
  const [showTail, setShowTail] = useState(false);

  const pushBody = (nextHead: string, nextTail: string) => {
    setHead(nextHead);
    setTail(nextTail);
    onChange({ ...v, body: split.splittable ? `${nextHead}\n\n${nextTail}` : nextHead });
  };

  const selectClass =
    "w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm";

  return (
    <fieldset disabled={props.disabled} className="space-y-3 disabled:opacity-60">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {caps.accountEditable ? (
          props.accounts.length > 0 ? (
            <label className="block">
              <span className="text-xs text-fg-muted">Account</span>
              <select value={v.account} onChange={set("account")} className={selectClass}>
                {withCurrent(v.account, props.accounts).map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <Field label="Account" value={v.account} onChange={set("account")} />
          )
        ) : (
          <Field label="Account" value={v.account} onChange={() => {}} readOnly />
        )}

        {caps.showCounty && (
          <label className="block">
            <span className="text-xs text-fg-muted">County</span>
            <select
              value={v.county}
              onChange={(e) => onChange(applyCounty(v, e.target.value))}
              className={selectClass}
            >
              <option value="">Select…</option>
              {countyMissing && (
                <option value={v.county}>{v.county} (not in reference data)</option>
              )}
              {L?.counties.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                  {c.subarea_supported ? "" : "  (not routable)"}
                </option>
              ))}
            </select>
          </label>
        )}

        {caps.cityMode === "select" ? (
          <label className="block">
            <span className="text-xs text-fg-muted">
              City {county ? `(${county.cities.length})` : ""}
            </span>
            <select
              value={v.city}
              onChange={(e) => onChange(applyCity(v, e.target.value, L))}
              disabled={!county && !countyMissing}
              className={cn(selectClass, "disabled:opacity-40")}
            >
              <option value="">{county ? "Select…" : "Pick a county first"}</option>
              {v.city && !county?.cities.some((c) => c.city === v.city) && (
                <option value={v.city}>{v.city}</option>
              )}
              {county?.cities.map((c) => (
                <option key={c.city} value={c.city}>
                  {c.city}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label className="block sm:col-span-2">
            <span className="text-xs text-fg-muted">
              City or neighborhood — Craigslist's free-text area box. Often more
              than one town; keep whatever breadth the ad already has.
            </span>
            <input
              value={v.city}
              onChange={set("city")}
              list="posting-city-suggestions"
              className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
            />
            <datalist id="posting-city-suggestions">
              {(L?.counties ?? []).flatMap((c) => c.cities).map((c) => (
                <option key={c.city} value={c.city} />
              ))}
            </datalist>
          </label>
        )}

        <Field label="Zip" value={v.postal_code} onChange={set("postal_code")} />

        <label className="block">
          <span className="text-xs text-fg-muted">Phone</span>
          <select value={v.phone_number} onChange={set("phone_number")} className={selectClass}>
            {!(L?.phone_numbers ?? []).includes(v.phone_number) && (
              <option value={v.phone_number}>{v.phone_number || "—"}</option>
            )}
            {L?.phone_numbers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        <Field label="License" value={v.license_number} onChange={set("license_number")} />
      </div>

      {county && !county.subarea_supported && (
        <p className="text-xs text-warn-fg border border-warn-border bg-warn/60 rounded px-2 py-1.5">
          The poster cannot map <strong>{county.name}</strong> to a Craigslist
          subarea — it will fall back to the first option on the form and file the
          ad under the wrong area. Use a different county until that is fixed.
        </p>
      )}

      {caps.showGeographicArea && (
        <label className="block">
          <span className="text-xs text-fg-muted">
            City or neighborhood — goes in Craigslist's free-text area box. Not
            limited to one city: “Fort Lauderdale, Davie, Plantation” or a
            neighbourhood both work, and widen the searches you appear in.
          </span>
          <input
            value={v.geographic_area}
            onChange={set("geographic_area")}
            placeholder={v.city}
            className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm"
          />
        </label>
      )}

      <TitleField value={v.title} onChange={(t) => onChange({ ...v, title: t })} />

      <label className="block">
        <span className="text-xs text-fg-muted">
          {split.splittable
            ? "Ad copy — the part a buyer actually reads. The keyword tail is separate, below."
            : "Body — the keyword tail is part of this text; edit the top section"}
        </span>
        <textarea
          value={head}
          onChange={(e) => pushBody(e.target.value, tail)}
          rows={split.splittable ? 14 : 16}
          className="w-full mt-1 bg-bg border border-border-strong rounded px-2 py-1.5 text-sm font-mono"
        />
        {/* The head's own length is the writing aid; the total against the limit
            is what decides whether Craigslist takes it, so both are shown and
            the total is what gates Save. */}
        <span className="mt-1 flex items-baseline justify-between gap-2">
          <span className="text-xs text-fg-subtle">
            {head.length.toLocaleString()} characters
            {split.splittable && " in the ad copy"}
          </span>
          <BodyCounter body={v.body} />
        </span>
      </label>

      {/* Read-only by default. DESIGN.md decision 7 requires the tail stay
          byte-exact across every ad — it is appended from one stored template —
          so editing it here is deliberate, not incidental. */}
      {split.splittable && (
        <div className="rounded border border-border bg-bg/40">
          <div className="flex flex-wrap items-center gap-2 p-2">
            <button
              type="button"
              onClick={() => setShowTail((s) => !s)}
              aria-expanded={showTail}
              className="text-xs px-2 py-1 rounded border border-border-strong text-fg-muted hover:bg-surface-2"
            >
              {showTail ? "Hide" : "Show"} keyword tail ({tail.length.toLocaleString()} characters)
            </button>
            <span className="text-xs text-fg-subtle">
              Identical on every ad. Appended automatically.
            </span>
            {showTail && !tailUnlocked && (
              <button
                type="button"
                onClick={() => setTailUnlocked(true)}
                className="ml-auto text-xs px-2 py-1 rounded border border-warn-border text-warn-fg hover:bg-warn"
              >
                Edit anyway
              </button>
            )}
          </div>
          {showTail && (
            <textarea
              value={tail}
              readOnly={!tailUnlocked}
              onChange={(e) => pushBody(head, e.target.value)}
              rows={10}
              aria-label="Keyword tail"
              className={cn(
                "w-full border-t border-border px-2 py-1.5 text-xs font-mono bg-transparent",
                !tailUnlocked && "text-fg-subtle",
              )}
            />
          )}
        </div>
      )}
    </fieldset>
  );
}
