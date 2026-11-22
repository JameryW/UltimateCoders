/** TaskProto timestamps are Unix seconds, as specified by the Gateway. */
export function taskTimestampToISO(seconds: bigint): string {
  const date = new Date(Number(seconds) * 1000);
  return Number.isNaN(date.getTime()) ? new Date(0).toISOString() : date.toISOString();
}

/** Event producers use ISO strings or numeric epoch seconds/milliseconds/microseconds/nanoseconds. */
export function eventTimestampToISO(value: string | number | bigint): string {
  if (typeof value === "string" && !/^\d+(\.\d+)?$/.test(value)) return value;
  const epoch = Number(value);
  if (!Number.isFinite(epoch)) {
    return typeof value === "string" ? value : new Date(0).toISOString();
  }
  const milliseconds = epoch >= 1e18
    ? epoch / 1e6
    : epoch >= 1e15
      ? epoch / 1000
      : epoch >= 1e12
        ? epoch
        : epoch * 1000;
  const date = new Date(milliseconds);
  return Number.isNaN(date.getTime())
    ? (typeof value === "string" ? value : new Date(0).toISOString())
    : date.toISOString();
}
