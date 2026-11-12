// Pi's next-action rule, shared by the work summary and the mission live view (kept out of work-summary.js so the
// live view can use it without an import cycle through mission-catalog-view).
const text = v => typeof v === 'string' ? v.trim() : '';
// G8: Pi's own "nothing scheduled" marker (MissionObservability.ts) is an absence, never a next action.
const NO_NEXT_ACTION = 'no further action is scheduled';
const NEXT_ACTION_MAX = 500;
/** @returns {{text:string, at?:string}|undefined} only when Pi supplied non-empty text that is not its absence marker; never inferred. */
export function piNextAction(activity) {
  const t = text(activity?.nextAction);
  if (!t || t.toLowerCase() === NO_NEXT_ACTION) return undefined;
  const at = text(activity.nextActionAt);
  return {text: t.slice(0, NEXT_ACTION_MAX), ...(at && Number.isFinite(Date.parse(at)) ? {at} : {})};
}
